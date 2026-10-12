"""设备水侧压降计入泵扬程

原先 `_water_resistance_by_pipe` 只读 `water_pressure_drop_kpa`，而目录里有这个
字段的只有新风机组；冷机的蒸发器 / 冷凝器压降（`chilled_/cooling_pressure_drop_kpa`，
实时侧 `runtime_network` 早就按这两个字段读）在静态侧一概按 0，且不报降级。
新风机组的额定压降又按常数整额计入，不随实际流量变化。

现口径（与实时侧一致）：
- 冷机按系统取蒸发器 / 冷凝器压降，其余设备取 `water_pressure_drop_kpa`；
- Δp = Δp_额定 × (Q / Q_额定)²，Q 取相邻管段设计流量，Q_额定 = 单台额定水量 × 台数；
- 设备上写的 `pressure_drop_pa` 视为设计工况值，不折算；
- 都没有时按 0 计入，并报 `DEVICE_WATER_DROP_MISSING`。
"""
from __future__ import annotations

import pytest

from app.calculators.hydraulic_network import calculate_hydraulic_network
from app.data.equipment import find_equipment_spec
from app.schemas import SystemEvaluateRequest

# 100 kW、ΔT = 5 K → 0.86 × 100 / 5 = 17.2 m³/h；冷却水取 1.2 倍 = 20.64 m³/h。
CHILLED_FLOW = 17.2
COOLING_FLOW = 20.64


def _payload(chiller_name: str = "SCWE-90H+B", **chiller_overrides) -> dict:
    chiller = {"id": "chiller", "type": "冷源机组", "name": chiller_name, **chiller_overrides}
    pipe = {"length_m": 20, "diameter_mm": 80}
    return {
        "rooms": [{"id": "r1", "name": "r1", "area_m2": 100, "people": 10, "device_ids": ["fcu"]}],
        "devices": [
            chiller,
            {"id": "pump", "type": "循环泵", "name": "EC80"},
            {"id": "cw_pump", "type": "循环泵", "name": "EC80"},
            {"id": "fcu", "type": "风机盘管", "name": "FP-85WA", "pressure_drop_pa": 15000},
            {"id": "tower", "type": "冷却塔", "name": "S15E-1285-06JN"},
        ],
        "pipes": [
            {"id": "chw_s", "system": "chilled_water", **pipe},
            {"id": "chw_r", "system": "chilled_water", **pipe},
            {"id": "cw_s", "system": "cooling_water", **pipe},
            {"id": "cw_r", "system": "cooling_water", **pipe},
        ],
        "connections": [
            {"from_id": "chiller", "to_id": "pump", "system": "chilled_water"},
            {"from_id": "pump", "to_id": "chw_s", "system": "chilled_water"},
            {"from_id": "chw_s", "to_id": "fcu", "system": "chilled_water"},
            {"from_id": "fcu", "to_id": "chw_r", "system": "chilled_water"},
            {"from_id": "chw_r", "to_id": "chiller", "system": "chilled_water"},
            {"from_id": "chiller", "to_id": "cw_pump", "system": "cooling_water"},
            {"from_id": "cw_pump", "to_id": "cw_s", "system": "cooling_water"},
            {"from_id": "cw_s", "to_id": "tower", "system": "cooling_water"},
            {"from_id": "tower", "to_id": "cw_r", "system": "cooling_water"},
            {"from_id": "cw_r", "to_id": "chiller", "system": "cooling_water"},
        ],
    }


def _drops(payload: dict) -> dict[str, float]:
    result = calculate_hydraulic_network(SystemEvaluateRequest(**payload), cooling_load_kw=100)
    return {item.pipe_id: item.pressure_drop_pa for item in result.pipes}


def _issues(payload: dict) -> dict[str, list]:
    result = calculate_hydraulic_network(SystemEvaluateRequest(**payload), cooling_load_kw=100)
    grouped: dict[str, list] = {}
    for issue in result.issues:
        grouped.setdefault(issue.code, []).append(issue)
    return grouped


def test_catalog_carries_scwe_and_shaf_rated_drops():
    """数据出处：docs/设备样本参数.md 的 SCWE 水侧表与 SHAF 性能表。"""
    scwe = find_equipment_spec("冷源机组", "SCWE-90H+B")
    assert (scwe["chilled_pressure_drop_kpa"], scwe["chilled_flow_m3_h"]) == (40.9, 53.02)
    assert (scwe["cooling_pressure_drop_kpa"], scwe["cooling_flow_m3_h"]) == (53.4, 66.28)
    shaf = find_equipment_spec("空气源热泵", "SHAF-80H")
    assert (shaf["water_pressure_drop_kpa"], shaf["flow_m3_h"]) == (50.2, 45.4)


def test_chiller_drops_scale_with_flow_ratio_squared():
    """手算：蒸发器 40.9 kPa × (17.2/53.02)² = 4304 Pa；冷凝器 53.4 kPa × (20.64/66.28)² = 5178 Pa。

    与目录里没有压降数据的同类冷机（LG340MCF）对比，差值就是冷机压降本身。
    """
    with_data = _drops(_payload("SCWE-90H+B"))
    without = _drops(_payload("LG340MCF"))

    assert with_data["chw_r"] - without["chw_r"] == pytest.approx(40900 * (CHILLED_FLOW / 53.02) ** 2, rel=1e-3)
    assert with_data["cw_r"] - without["cw_r"] == pytest.approx(53400 * (COOLING_FLOW / 66.28) ** 2, rel=1e-3)
    # 冷机只挂一根管：供水管不重复计入。
    assert with_data["chw_s"] == pytest.approx(without["chw_s"], abs=1e-3)


def test_parallel_units_share_the_flow():
    """两台并联：Q_额定翻倍，压降降为 1/4。"""
    single = _drops(_payload("SCWE-90H+B"))
    double = _drops(_payload("SCWE-90H+B", quantity=2))
    without = _drops(_payload("LG340MCF"))

    single_drop = single["chw_r"] - without["chw_r"]
    double_drop = double["chw_r"] - without["chw_r"]
    assert single_drop > 0  # 否则 0 == 0/4 也能通过
    assert double_drop == pytest.approx(single_drop / 4, rel=1e-3)


def test_declared_device_drop_is_used_as_is():
    """设备上写的压降不折算：FCU 声明 15 kPa 就按 15 kPa 计入它挂的那根管。"""
    declared = _drops(_payload("LG340MCF"))
    payload = _payload("LG340MCF")
    payload["devices"][3].pop("pressure_drop_pa")
    undeclared = _drops(payload)

    assert declared["chw_r"] - undeclared["chw_r"] == pytest.approx(15000, abs=1e-3)


def test_missing_drop_is_reported_per_system():
    issues = _issues(_payload("LG340MCF"))["DEVICE_WATER_DROP_MISSING"]

    by_target = {issue.target: issue for issue in issues}
    assert set(by_target) == {"chilled_water", "cooling_water"}
    assert all(issue.level == "warning" for issue in issues)
    assert "chiller" in by_target["chilled_water"].message
    # FCU 声明了压降，不在缺数据清单里；冷却塔目录没有水阻数据。
    assert "fcu" not in by_target["chilled_water"].message
    assert "tower" in by_target["cooling_water"].message


def test_no_missing_warning_when_every_device_has_a_drop():
    payload = _payload("SCWE-90H+B")
    payload["devices"][4]["pressure_drop_pa"] = 30000

    assert "DEVICE_WATER_DROP_MISSING" not in _issues(payload)
