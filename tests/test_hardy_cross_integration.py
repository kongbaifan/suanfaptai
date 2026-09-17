"""Hardy Cross 求解器集成测试

测试端口图 → Hardy Cross 模型 → 求解 → 结果映射的完整流程。
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

from app.hardy_cross.bridge import _allocation_is_usable, allocate_flow_hardy_cross
from app.schemas import ConnectionInput, DeviceInput, SystemEvaluateRequest
from app.steady_solver.flow_solver import FlowAllocation
from app.steady_solver.port_graph import build_port_graph

REPO_ROOT = Path(__file__).resolve().parents[1]

# 不同的导入顺序，用于回归"导入顺序决定管网平差是否启用"这一静默失效
IMPORT_ORDERS = {
    "本模块自身（顶层已导入 bridge）": "import app.hardy_cross.bridge",
    "solver 直接导入": "import app.steady_solver.solver",
    "经 app.main 间接导入": "import app.main",
    "flow_solver 先导入": "import app.steady_solver.flow_solver",
}


class TestHardyCrossBridge:
    """Hardy Cross 桥接测试"""

    def test_simple_chilled_water_system(self):
        """测试简单冷冻水系统的 Hardy Cross 求解"""
        # 构建简单系统：冷机 → 管道 → FCU → 管道 → 冷机
        request = SystemEvaluateRequest(
            devices=[
                DeviceInput(id="chiller_1", type="chiller"),
                DeviceInput(id="fcu_1", type="fcu"),
            ],
            connections=[
                ConnectionInput(from_id="chiller_1", to_id="fcu_1", system="chilled_water"),
                ConnectionInput(from_id="fcu_1", to_id="chiller_1", system="chilled_water"),
            ],
        )

        # 构建端口图
        port_graph = build_port_graph(request)

        # Hardy Cross 求解 - 显式指定源/汇
        total_flow = 20.0  # m³/h
        terminal_demands = {
            "chiller_1": total_flow,   # 源（注入）
            "fcu_1": -total_flow,      # 汇（消耗）
        }
        result = allocate_flow_hardy_cross(
            port_graph, "chilled_water", total_flow, terminal_demands
        )

        # 验证结果
        assert result is not None
        assert result.total_demand == total_flow
        assert len(result.edge_flow) == 2  # 2条边
        assert len(result.port_flow) > 0  # 有端口流量

        # 验证流量守恒
        for device_id, residual in result.conservation_residuals.items():
            assert residual < 1.0, f"{device_id} 守恒残差过大: {residual}"

    def test_parallel_pipes_with_hardy_cross(self):
        """测试并联管道的 Hardy Cross 求解"""
        # 两条并联管道连接冷机和负荷
        request = SystemEvaluateRequest(
            devices=[
                DeviceInput(id="chiller_1", type="chiller"),
                DeviceInput(id="load_1", type="ahu"),
            ],
            connections=[
                ConnectionInput(from_id="chiller_1", to_id="load_1", system="chilled_water"),
                ConnectionInput(from_id="chiller_1", to_id="load_1", system="chilled_water"),
                ConnectionInput(from_id="load_1", to_id="chiller_1", system="chilled_water"),
            ],
        )

        port_graph = build_port_graph(request)
        total_flow = 30.0  # m³/h
        terminal_demands = {
            "chiller_1": total_flow,
            "load_1": -total_flow,
        }
        result = allocate_flow_hardy_cross(
            port_graph, "chilled_water", total_flow, terminal_demands
        )

        assert result is not None
        assert len(result.edge_flow) == 3

        # 并联管道流量应有所不同（如果直径不同）
        flows = list(result.edge_flow.values())
        assert len(set(flows)) > 0  # 至少有流量分配

    def test_complex_network_convergence(self):
        """测试复杂管网的收敛性"""
        # 构建多负荷系统：串联回路
        request = SystemEvaluateRequest(
            devices=[
                DeviceInput(id="chiller_1", type="chiller"),
                DeviceInput(id="fcu_1", type="fcu"),
                DeviceInput(id="fcu_2", type="fcu"),
                DeviceInput(id="fcu_3", type="fcu"),
            ],
            connections=[
                ConnectionInput(from_id="chiller_1", to_id="fcu_1", system="chilled_water"),
                ConnectionInput(from_id="fcu_1", to_id="fcu_2", system="chilled_water"),
                ConnectionInput(from_id="fcu_2", to_id="fcu_3", system="chilled_water"),
                ConnectionInput(from_id="fcu_3", to_id="chiller_1", system="chilled_water"),
            ],
        )

        port_graph = build_port_graph(request)
        total_flow = 50.0  # m³/h

        # 显式指定源和多个汇
        terminal_demands = {
            "chiller_1": total_flow,      # 源
            "fcu_1": -total_flow / 3,     # 汇1
            "fcu_2": -total_flow / 3,     # 汇2
            "fcu_3": -total_flow / 3,     # 汇3
        }
        result = allocate_flow_hardy_cross(
            port_graph, "chilled_water", total_flow, terminal_demands
        )

        assert result is not None
        assert len(result.edge_flow) == 4

        # 验证总流量数量级正确
        total_edge_flow = sum(abs(f) for f in result.edge_flow.values())
        # 串联回路中，流量应在合理范围内
        avg_flow = total_edge_flow / len(result.edge_flow)
        assert 10 < avg_flow < 100, f"平均流量异常: {avg_flow}"

    def test_empty_system(self):
        """测试空系统（无连接）"""
        request = SystemEvaluateRequest(
            devices=[DeviceInput(id="chiller_1", type="chiller")],
            connections=[],
        )

        port_graph = build_port_graph(request)
        terminal_demands = {"chiller_1": 10.0}
        result = allocate_flow_hardy_cross(
            port_graph, "chilled_water", 10.0, terminal_demands
        )

        # 空系统应返回空结果
        assert len(result.edge_flow) == 0
        assert len(result.port_flow) == 0

    def test_missing_terminal_demands(self):
        """测试缺少 terminal_demands 参数时抛出错误"""
        request = SystemEvaluateRequest(
            devices=[
                DeviceInput(id="chiller_1", type="chiller"),
                DeviceInput(id="fcu_1", type="fcu"),
            ],
            connections=[
                ConnectionInput(from_id="chiller_1", to_id="fcu_1", system="chilled_water"),
            ],
        )

        port_graph = build_port_graph(request)

        # 不传 terminal_demands 应抛出 ValueError
        with pytest.raises(ValueError, match="terminal_demands is required"):
            allocate_flow_hardy_cross(port_graph, "chilled_water", 20.0, {})


class TestHardyCrossResultGuard:
    """Hardy-Cross 结果的数值守卫

    背景：原实现**无条件**采用 Hardy-Cross 的结果。实测一个 12 个并联末端的项目
    里它不收敛并给出 1e10 m³/h 量级（2 的幂次，典型数值溢出）的守恒残差，
    评估随即报出 23 条 `conservation_violation` 错误。末端越多越容易触发，
    而多房间系统正是本平台要支持的场景。
    """

    @staticmethod
    def _allocation(residual: float) -> FlowAllocation:
        return FlowAllocation(
            port_flow={},
            edge_flow={},
            conservation_residuals={"chw_main_supply": residual},
            total_demand=20.0,
        )

    def test_nonphysical_residual_is_rejected(self):
        """残差比总需求大若干数量级 -> 不可用（实测病态值 1.07e10）"""
        assert _allocation_is_usable(self._allocation(10_737_418_240.0), 20.0) is False
        assert _allocation_is_usable(self._allocation(float("inf")), 20.0) is False

    def test_physical_residual_is_accepted_even_above_tolerance(self):
        """只看残差量级，**不看**是否收敛到 0.01 容差

        多支路管网里 50 次迭代后残差仍在容差之上、流量却正确的情形很常见，
        一并否掉会把更准的平差结果换成均分结果。
        """
        assert _allocation_is_usable(self._allocation(0.0), 20.0) is True
        assert _allocation_is_usable(self._allocation(0.9), 20.0) is True
        # 边界：残差恰等于总需求仍算可用（上界含等号）
        assert _allocation_is_usable(self._allocation(20.0), 20.0) is True

    def test_edge_flow_magnitude_alone_does_not_reject(self):
        """边流量幅值不参与判据——正规样例就是"残差 0、边流量 8.4e10"

        这条是反向验证：若把边流量幅值也纳入判据，`four_floor_fcu_fresh_air`
        会被误伤（它的 Hardy-Cross 守恒残差是 0.0000）。边流量的尺度问题另有
        记录，不在这里掩盖。
        """
        allocation = self._allocation(0.0)
        allocation.edge_flow = {0: 84_090_174_987.0}
        assert _allocation_is_usable(allocation, 20.0) is True


class TestHardyCrossFallbackIsTransparent:
    """回退必须显式上报，不能静默"""

    def test_degraded_allocation_emits_warning(self, monkeypatch):
        """桥接层回退 -> 稳态求解器必须报 `HARDY_CROSS_FALLBACK` warning"""
        from app.steady_solver import solver as solver_module

        degraded = FlowAllocation(
            port_flow={},
            edge_flow={},
            conservation_residuals={"chw_main_supply": 999.0},
            total_demand=20.0,
            degraded_reason="hardy_cross_unusable(iterations=50, max_residual=1e10 m3/h)",
        )
        monkeypatch.setattr(
            solver_module, "_hardy_cross_allocator",
            lambda: (lambda *args, **kwargs: degraded),
        )

        request = SystemEvaluateRequest(
            devices=[
                DeviceInput(id="chiller_1", type="冷源机组", name="SCWE-90H+B"),
                DeviceInput(id="pump_1", type="循环泵", name="TPE3 65-150"),
                DeviceInput(id="fcu_1", type="风机盘管", name="FP-204WA"),
            ],
            connections=[
                ConnectionInput(from_id="chiller_1", to_id="pump_1", system="chilled_water"),
                ConnectionInput(from_id="pump_1", to_id="fcu_1", system="chilled_water"),
                ConnectionInput(from_id="fcu_1", to_id="chiller_1", system="chilled_water"),
            ],
        )
        result = solver_module.solve_steady_state(request, cooling_load_kw=20.0)

        warnings = [i for i in result.issues if i.code == "HARDY_CROSS_FALLBACK"]
        assert warnings, [i.code for i in result.issues]
        # 教学化措辞：既要说明降级，也要说清结果还能怎么用
        assert "未收敛" in warnings[0].message
        assert "均分求解器" in warnings[0].message


class TestHardyCrossAvailabilityIsImportOrderIndependent:
    """管网平差的可用性不能取决于导入顺序

    回归的是这个静默失效：`bridge` 反向 import `steady_solver`，而
    `steady_solver/__init__.py` 又 import `solver`，形成包级循环。原实现在顶层
    `try/except ImportError` 里导入 `allocate_flow_hardy_cross`——**先**导入
    `app.hardy_cross.bridge` 时该名字尚未定义，异常被吞掉，
    `HARDY_CROSS_AVAILABLE` 静默变成 `False`，管网平差被无声关闭、退化成均分
    分配且不报任何 warning。
    """

    @pytest.mark.parametrize("order", list(IMPORT_ORDERS))
    def test_allocator_available_regardless_of_import_order(self, order: str):
        """四种导入顺序下分配器都必须可用（子进程隔离，避免测试内已缓存的结果干扰）"""
        code = (
            f"{IMPORT_ORDERS[order]}\n"
            "import app.steady_solver.solver as s\n"
            "allocator = s._hardy_cross_allocator()\n"
            "assert allocator is not None, '管网平差被静默关闭，流量退化成均分分配'\n"
            "assert allocator.__module__ == 'app.hardy_cross.bridge', allocator\n"
        )
        proc = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            cwd=str(REPO_ROOT),
            env={**os.environ, "PYTHONPATH": str(REPO_ROOT)},
            check=False,
        )
        assert proc.returncode == 0, f"{order} 下 Hardy-Cross 不可用：{proc.stderr[-800:]}"
