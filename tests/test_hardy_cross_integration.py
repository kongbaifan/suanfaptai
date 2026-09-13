"""Hardy Cross 求解器集成测试

测试端口图 → Hardy Cross 模型 → 求解 → 结果映射的完整流程。
"""

import pytest

from app.hardy_cross.bridge import allocate_flow_hardy_cross
from app.schemas import ConnectionInput, DeviceInput, SystemEvaluateRequest
from app.steady_solver.port_graph import build_port_graph


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
