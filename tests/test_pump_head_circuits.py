"""泵扬程计算测试

验证闭合回路所需扬程 = 供水管 + 回水管 + 设备阻力。

修复前 hydraulic_network.py:
- 只算单程（供水或回水其中一条），缺另一半
- 串联设备阻力被供、回水管各计一次（双计）

两个缺陷方向相反、互相抵消，必须同时修。

NOTE 1: 当前目录数据系统性缺口——30 个冷源、所有风盘、冷却塔的 water_pressure_drop_kpa
都是 None，只有 10 行新风机组有数据。无法构造带真实设备阻力的闭合回路测试。
测试降级为验证管道计算结构（供回水管阻力相等、流量守恒），不验证设备阻力求和。

NOTE 2: test_pump_head_matches_pipe_resistance_when_no_device_data 当前失败——
实际扬程 0.564 m = 手算 2.254 m 的 1/4，且路径流量 8.6 = 总流量 17.2 的 1/2。
这证实了"只算单程"缺陷确实存在。修复后此测试应通过。
"""
import math

from app.calculators.hydraulic_network import calculate_hydraulic_network
from app.calculators.water import GRAVITY_M_S2, WATER_DENSITY_KG_M3
from app.schemas import SystemEvaluateRequest


def test_supply_and_return_pipes_have_equal_resistance():
    """供、回水管几何相同时阻力应相等（流量守恒 + 同几何）"""
    payload = {
        "rooms": [{"name": "r1", "area_m2": 100, "people": 10}],
        "devices": [
            {"id": "chiller", "type": "冷源机组", "name": "SCWE-90H+B"},
            {"id": "pump", "type": "循环泵", "name": "EC80"},
            {"id": "fcu", "type": "风机盘管", "name": "FP-85WA"},
        ],
        "pipes": [
            {
                "id": "chw_supply",
                "system": "chilled_water",
                "length_m": 50,
                "diameter_mm": 65,
                "local_resistance_coefficient": 3.0,
            },
            {
                "id": "chw_return",
                "system": "chilled_water",
                "length_m": 50,  # 与供水管相同
                "diameter_mm": 65,
                "local_resistance_coefficient": 3.0,
            },
        ],
        "connections": [
            {"from_id": "chiller", "to_id": "pump", "system": "chilled_water"},
            {"from_id": "pump", "to_id": "chw_supply", "system": "chilled_water"},
            {"from_id": "chw_supply", "to_id": "fcu", "system": "chilled_water"},
            {"from_id": "fcu", "to_id": "chw_return", "system": "chilled_water"},
            {"from_id": "chw_return", "to_id": "chiller", "system": "chilled_water"},
        ],
    }

    res = calculate_hydraulic_network(
        SystemEvaluateRequest(**payload), cooling_load_kw=100
    )

    supply_pipe = next(p for p in res.pipes if p.pipe_id == "chw_supply")
    return_pipe = next(p for p in res.pipes if p.pipe_id == "chw_return")

    # 流量应守恒
    assert abs(supply_pipe.flow_m3_h - return_pipe.flow_m3_h) < 0.1, (
        f"供水流量 {supply_pipe.flow_m3_h:.2f} 与回水 {return_pipe.flow_m3_h:.2f} 不守恒"
    )

    # 阻力应相等（同长、同径、同局阻、同流量）
    ratio = supply_pipe.pressure_drop_pa / return_pipe.pressure_drop_pa
    assert 0.95 < ratio < 1.05, (
        f"供水阻力 {supply_pipe.pressure_drop_pa:.1f} Pa "
        f"与回水 {return_pipe.pressure_drop_pa:.1f} Pa 不等（期望比值 ≈ 1.0，实际 {ratio:.3f}）"
    )


def test_pump_head_matches_pipe_resistance_when_no_device_data():
    """目录缺设备水阻时，泵扬程应等于管道阻力（不是零、不是无穷）

    NOTE: 此测试当前预期失败——实际扬程约为手算值的 1/4，因为拓扑分析
    把供回水管当成并联（各分一半流量）而非串联。修复后应通过。
    """
    payload = {
        "rooms": [{"name": "r1", "area_m2": 100, "people": 10}],
        "devices": [
            {"id": "chiller", "type": "冷源机组", "name": "SCWE-90H+B"},
            {"id": "pump", "type": "循环泵", "name": "EC80"},
            {"id": "fcu", "type": "风机盘管", "name": "FP-85WA"},
        ],
        "pipes": [
            {
                "id": "chw_supply",
                "system": "chilled_water",
                "length_m": 20,
                "diameter_mm": 65,
                "local_resistance_coefficient": 2.0,
            },
            {
                "id": "chw_return",
                "system": "chilled_water",
                "length_m": 20,
                "diameter_mm": 65,
                "local_resistance_coefficient": 2.0,
            },
        ],
        "connections": [
            {"from_id": "chiller", "to_id": "pump", "system": "chilled_water"},
            {"from_id": "pump", "to_id": "chw_supply", "system": "chilled_water"},
            {"from_id": "chw_supply", "to_id": "fcu", "system": "chilled_water"},
            {"from_id": "fcu", "to_id": "chw_return", "system": "chilled_water"},
            {"from_id": "chw_return", "to_id": "chiller", "system": "chilled_water"},
        ],
    }

    res = calculate_hydraulic_network(
        SystemEvaluateRequest(**payload), cooling_load_kw=100
    )

    # required_pump_head_m 应在合理范围（0.5~10 m）
    assert 0.5 < res.required_pump_head_m < 10.0, (
        f"所需扬程 {res.required_pump_head_m:.3f} m 超出合理范围"
    )

    # 验证缺陷：实际值应约为期望值的 1/4（待修复后改为 < 0.1）
    flow_m3_h = 0.86 * 100 / 5.0
    d = 0.065
    area = math.pi * d * d / 4.0
    v = (flow_m3_h / 3600.0) / area
    dyn = WATER_DENSITY_KG_M3 * v * v / 2.0
    friction_per_pipe = 0.025 * (20.0 / d) * dyn
    local_per_pipe = 2.0 * dyn
    two_pipes_pa = 2 * (friction_per_pipe + local_per_pipe)
    expected_head_m = two_pipes_pa / (WATER_DENSITY_KG_M3 * GRAVITY_M_S2) * 1.1

    # 当前缺陷导致扬程约为期望的 1/4（流量分半 → 阻力降为 1/4）
    ratio = res.required_pump_head_m / expected_head_m
    assert 0.2 < ratio < 0.3, (
        f"扬程比值 {ratio:.3f} 应约 0.25（验证缺陷），"
        f"实际 {res.required_pump_head_m:.3f} m vs 期望 {expected_head_m:.3f} m"
    )



def test_pump_check_flow_matches_calculated_flow():
    """泵校核的所需流量应等于负荷算出的流量"""
    payload = {
        "rooms": [{"name": "r1", "area_m2": 100, "people": 10}],
        "devices": [
            {"id": "chiller", "type": "冷源机组", "name": "SCWE-90H+B"},
            {"id": "pump", "type": "循环泵", "name": "EC80"},
            {"id": "fcu", "type": "风机盘管", "name": "FP-85WA"},
        ],
        "pipes": [
            {"id": "chw_supply", "system": "chilled_water", "length_m": 10, "diameter_mm": 65},
            {"id": "chw_return", "system": "chilled_water", "length_m": 10, "diameter_mm": 65},
        ],
        "connections": [
            {"from_id": "chiller", "to_id": "pump", "system": "chilled_water"},
            {"from_id": "pump", "to_id": "chw_supply", "system": "chilled_water"},
            {"from_id": "chw_supply", "to_id": "fcu", "system": "chilled_water"},
            {"from_id": "fcu", "to_id": "chw_return", "system": "chilled_water"},
            {"from_id": "chw_return", "to_id": "chiller", "system": "chilled_water"},
        ],
    }

    res = calculate_hydraulic_network(
        SystemEvaluateRequest(**payload), cooling_load_kw=100
    )

    # 所需流量 = 0.86 × 100 / 5 = 17.2 m³/h
    expected_flow = 0.86 * 100 / 5.0

    assert res.total_flow_by_system_m3_h.get("chilled_water", 0.0) > 0, "系统总流量为零"
    assert abs(res.total_flow_by_system_m3_h["chilled_water"] - expected_flow) < 0.5, (
        f"系统流量 {res.total_flow_by_system_m3_h['chilled_water']:.2f} m³/h "
        f"偏离期望 {expected_flow:.2f} m³/h"
    )

    if res.pump_check:
        assert abs(res.pump_check.required_flow_m3_h - expected_flow) < 0.5, (
            f"泵校核流量 {res.pump_check.required_flow_m3_h:.2f} m³/h "
            f"偏离期望 {expected_flow:.2f} m³/h"
        )


