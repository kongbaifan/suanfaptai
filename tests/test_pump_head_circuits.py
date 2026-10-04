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


def _dual_pump_payload() -> dict:
    """冷冻环路 + 冷却环路各一台泵的最小项目

    冷冻泵选 TPE3 40-80（12.31 m³/h，小于所需 17.2），冷却泵选
    TPE3 100-200（128 m³/h）。修复前两台泵的流量被一起计入冷冻水校核，
    装机 140.31 m³/h → flow_margin 8.16 → 把 PUMP_FLOW_LOW 盖掉了。
    """
    return {
        "rooms": [{"name": "r1", "area_m2": 100, "people": 10}],
        "devices": [
            {"id": "chiller", "type": "冷源机组", "name": "SCWE-90H+B"},
            {"id": "chw_pump", "type": "循环泵", "name": "TPE3 40-80"},
            {"id": "cw_pump", "type": "循环泵", "name": "TPE3 100-200"},
            {"id": "tower", "type": "冷却塔", "name": "S15E-1285-06JN"},
            {"id": "fcu", "type": "风机盘管", "name": "FP-85WA"},
        ],
        "pipes": [
            {"id": "chw_supply", "system": "chilled_water", "length_m": 10, "diameter_mm": 65},
            {"id": "chw_return", "system": "chilled_water", "length_m": 10, "diameter_mm": 65},
            {"id": "cw_supply", "system": "cooling_water", "length_m": 10, "diameter_mm": 80},
            {"id": "cw_return", "system": "cooling_water", "length_m": 10, "diameter_mm": 80},
        ],
        "connections": [
            {"from_id": "chiller", "to_id": "chw_pump", "system": "chilled_water"},
            {"from_id": "chw_pump", "to_id": "chw_supply", "system": "chilled_water"},
            {"from_id": "chw_supply", "to_id": "fcu", "system": "chilled_water"},
            {"from_id": "fcu", "to_id": "chw_return", "system": "chilled_water"},
            {"from_id": "chw_return", "to_id": "chiller", "system": "chilled_water"},
            {"from_id": "chiller", "to_id": "cw_supply", "system": "cooling_water"},
            {"from_id": "cw_supply", "to_id": "tower", "system": "cooling_water"},
            {"from_id": "tower", "to_id": "cw_return", "system": "cooling_water"},
            {"from_id": "cw_return", "to_id": "cw_pump", "system": "cooling_water"},
            {"from_id": "cw_pump", "to_id": "chiller", "system": "cooling_water"},
        ],
    }


def test_pump_check_counts_only_chilled_water_pumps():
    """泵校核的装机流量只统计冷冻水泵，冷却水泵不得混入"""
    res = calculate_hydraulic_network(
        SystemEvaluateRequest(**_dual_pump_payload()), cooling_load_kw=100
    )

    assert res.pump_check is not None
    # TPE3 40-80 单台 12.31 m³/h；若混入冷却泵（128）会是 140.31
    assert res.pump_check.installed_flow_m3_h == 12.31
    assert res.pump_check.duty_pump_count == 1


def test_undersized_chilled_pump_is_reported_even_with_large_cooling_pump():
    """冷却泵再大也不能掩盖冷冻泵选小了"""
    res = calculate_hydraulic_network(
        SystemEvaluateRequest(**_dual_pump_payload()), cooling_load_kw=100
    )

    codes = {issue.code for issue in res.issues}
    assert "PUMP_FLOW_LOW" in codes
    assert res.pump_check.status == "warning"
    # 所需 17.2 m³/h，装机 12.31 → margin 约 0.716
    assert abs(res.pump_check.flow_margin - 12.31 / 17.2) < 0.01


def test_unconnected_pump_still_counted_in_chilled_check():
    """没有任何水系统连接的泵无法归属，仍应参与冷冻水校核（半成品项目不失去检查）"""
    payload = _dual_pump_payload()
    payload["connections"] = [
        c for c in payload["connections"] if "cw_pump" not in (c["from_id"], c["to_id"])
    ]
    res = calculate_hydraulic_network(
        SystemEvaluateRequest(**payload), cooling_load_kw=100
    )

    assert res.pump_check is not None
    # 冷却泵脱线后无法归属 → 保留；冷冻泵照常计入
    assert res.pump_check.installed_flow_m3_h == 12.31 + 128.0


def test_pump_check_none_when_no_pump_serves_chilled_water():
    """唯一的泵明确归属冷却水时，冷冻水校核应为 None 而不是拿冷却泵凑数"""
    payload = _dual_pump_payload()
    # 摘掉冷却泵，把冷冻泵改接到冷却水环路（真实换接线，不是只改 system 字段）
    payload["devices"] = [d for d in payload["devices"] if d["id"] != "cw_pump"]
    payload["connections"] = [
        c
        for c in payload["connections"]
        if "chw_pump" not in (c["from_id"], c["to_id"]) and "cw_pump" not in (c["from_id"], c["to_id"])
    ] + [
        {"from_id": "chiller", "to_id": "chw_pump", "system": "cooling_water"},
        {"from_id": "chw_pump", "to_id": "cw_supply", "system": "cooling_water"},
        {"from_id": "cw_return", "to_id": "chiller", "system": "cooling_water"},
    ]
    res = calculate_hydraulic_network(
        SystemEvaluateRequest(**payload), cooling_load_kw=100
    )

    assert res.pump_check is None


def _rewire_cooling_pump_between_devices(payload: dict) -> None:
    """冷却泵改为只经设备间连接接入冷却水：冷却塔 → 冷却泵 → 冷机

    泵两侧都直接连设备、不挨任何管段，connection.system 写明 cooling_water；
    冷却塔出水直接进泵，原回水管 cw_return 随之撤掉。
    """
    payload["pipes"] = [p for p in payload["pipes"] if p["id"] != "cw_return"]
    payload["connections"] = [
        c
        for c in payload["connections"]
        if "cw_pump" not in (c["from_id"], c["to_id"]) and "cw_return" not in (c["from_id"], c["to_id"])
    ] + [
        {"from_id": "tower", "to_id": "cw_pump", "system": "cooling_water"},
        {"from_id": "cw_pump", "to_id": "chiller", "system": "cooling_water"},
    ]


def _rewire_chilled_pump_between_devices(payload: dict) -> None:
    """冷冻泵改为只经设备间连接接入冷冻水：冷机 → 冷冻泵 → 止回阀 → 供水管

    泵出口先接止回阀再进供水管（常见接法），泵两侧都是设备↔设备连接。
    """
    payload["devices"].append({"id": "check_valve", "type": "止回阀"})
    payload["connections"] = [
        c for c in payload["connections"] if "chw_pump" not in (c["from_id"], c["to_id"])
    ] + [
        {"from_id": "chiller", "to_id": "chw_pump", "system": "chilled_water"},
        {"from_id": "chw_pump", "to_id": "check_valve", "system": "chilled_water"},
        {"from_id": "check_valve", "to_id": "chw_supply", "system": "chilled_water"},
    ]


def test_pump_wired_only_between_devices_on_cooling_water_is_excluded():
    """只经设备间连接接入冷却水的泵不计入冷冻水校核

    冷却塔 → 冷却泵 → 冷机两段都是设备↔设备连接，泵不挨任何管段。只认
    「泵↔管段」时它被判为无法归属、计回冷冻水校核：装机 12.31 + 128 =
    140.31 m³/h、扬程取 15.5 m，flow_margin 8.16，PUMP_FLOW_LOW 被盖掉。
    """
    payload = _dual_pump_payload()
    _rewire_cooling_pump_between_devices(payload)
    res = calculate_hydraulic_network(
        SystemEvaluateRequest(**payload), cooling_load_kw=100
    )

    assert res.pump_check is not None
    assert res.pump_check.installed_flow_m3_h == 12.31
    assert res.pump_check.available_head_m == 5.4
    assert res.pump_check.duty_pump_count == 1
    assert "PUMP_FLOW_LOW" in {issue.code for issue in res.issues}


def test_pump_wired_only_between_devices_on_chilled_water_is_counted():
    """只经设备间连接接入冷冻水的泵计入冷冻水校核

    冷机 → 冷冻泵 → 止回阀、冷却塔 → 冷却泵 → 冷机，两台泵都不挨管段，
    只能靠 connection.system 归属：冷冻泵计入，冷却泵剔除。只认「泵↔管段」时
    两台都无法归属、一起计回冷冻水校核：装机 140.31 m³/h、2 台、扬程 15.5 m。
    """
    payload = _dual_pump_payload()
    _rewire_chilled_pump_between_devices(payload)
    _rewire_cooling_pump_between_devices(payload)
    res = calculate_hydraulic_network(
        SystemEvaluateRequest(**payload), cooling_load_kw=100
    )

    assert res.pump_check is not None
    assert res.pump_check.installed_flow_m3_h == 12.31
    assert res.pump_check.available_head_m == 5.4
    assert res.pump_check.duty_pump_count == 1


def test_pump_wired_only_to_air_duct_is_not_attributed():
    """只接到风管上的泵不算归属任何水系统，按无法归属保留受查

    归属只认三种水系统。若把风管的 air 也当成归属，这台接错线的泵会被判为
    「明确不属于冷冻水」而剔除，从校核里静默消失（唯一的泵接错时 pump_check
    直接变成 None，一条 PUMP_* 都不报）。
    """
    payload = _dual_pump_payload()
    payload["pipes"].append(
        {"id": "duct", "system": "air", "length_m": 5, "width_mm": 400, "height_mm": 300}
    )
    payload["connections"] = [
        c for c in payload["connections"] if "cw_pump" not in (c["from_id"], c["to_id"])
    ] + [{"from_id": "cw_pump", "to_id": "duct", "system": "air"}]
    res = calculate_hydraulic_network(
        SystemEvaluateRequest(**payload), cooling_load_kw=100
    )

    assert res.pump_check is not None
    # 冷却泵只挨风管 → 无法归属 → 保留；冷冻泵照常计入
    assert res.pump_check.installed_flow_m3_h == 12.31 + 128.0
    assert res.pump_check.duty_pump_count == 2
