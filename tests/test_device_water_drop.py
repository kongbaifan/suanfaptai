"""设备水侧压降计入泵扬程

原先 `_water_resistance_by_pipe` 只读 `water_pressure_drop_kpa`，而目录里有这个
字段的只有新风机组；冷机的蒸发器 / 冷凝器压降（`chilled_/cooling_pressure_drop_kpa`，
实时侧 `runtime_network` 早就按这两个字段读）在静态侧一概按 0，且不报降级。
新风机组的额定压降又按常数整额计入，不随实际流量变化。

现口径（与实时侧一致）：
- 冷机按系统取蒸发器 / 冷凝器压降，其余设备取 `water_pressure_drop_kpa`；
- Δp = Δp_额定 × (Q / Q_额定)²，Q 取相邻管段设计流量，Q_额定 = 单台额定水量 × 台数；
- 设备上写的 `pressure_drop_pa` 视为设计工况值，不折算（写 0 即"没有水阻"）；
- 目录与输入都没有时，取 `rules.json` `pressure_defaults.device_water_drop_kpa` 的
  同类典型值，同样按 (Q/Q_额定)² 折算，报 info 级 `DEVICE_WATER_DROP_TYPICAL`；
- 典型值也用不上（类型没登记，或拿不到额定水量）时按 0 计入，报
  warning 级 `DEVICE_WATER_DROP_MISSING`。
"""
from __future__ import annotations

import pytest

from app.calculators.hydraulic_network import calculate_hydraulic_network
from app.data.equipment import find_equipment_spec
from app.samples import SAMPLES
from app.schemas import SystemEvaluateRequest
from app.simcore.project import build_runtime_layout

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
            # 冷却塔显式声明 0：只让冷机的压降出现在冷却水环路上。
            {"id": "tower", "type": "冷却塔", "name": "S15E-1285-06JN", "pressure_drop_pa": 0},
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
    """设备上写的压降不折算：FCU 声明 15 kPa 与声明 0 的差就是 15 kPa。"""
    declared = _drops(_payload("LG340MCF"))
    payload = _payload("LG340MCF")
    payload["devices"][3]["pressure_drop_pa"] = 0
    zero = _drops(payload)

    assert declared["chw_r"] - zero["chw_r"] == pytest.approx(15000, abs=1e-3)


def test_typical_drop_applies_when_catalog_and_input_are_silent():
    """手算：额定水量 = 设计流量时折算系数为 1，压降就是 rules.json 典型值本身。

    风机盘管 30 kPa，冷却塔 50 kPa（`pressure_defaults.device_water_drop_kpa`）。
    """
    payload = _payload("LG340MCF")
    fcu, tower = payload["devices"][3], payload["devices"][4]
    fcu.pop("pressure_drop_pa")
    fcu["flow_m3_h"] = CHILLED_FLOW
    tower.pop("pressure_drop_pa")
    tower["flow_m3_h"] = COOLING_FLOW
    typical = _drops(payload)

    fcu["pressure_drop_pa"] = 0
    tower["pressure_drop_pa"] = 0
    zero = _drops(payload)

    assert typical["chw_r"] - zero["chw_r"] == pytest.approx(30000, rel=1e-3)
    tower_pipe = min("cw_s", "cw_r")  # 每台设备只挂到 id 最小的相邻管段
    assert typical[tower_pipe] - zero[tower_pipe] == pytest.approx(50000, rel=1e-3)


def test_typical_drop_scales_with_flow_ratio_squared():
    """额定水量取设计流量的 2 倍 → 压降为典型值的 1/4。"""
    payload = _payload("LG340MCF")
    fcu = payload["devices"][3]
    fcu.pop("pressure_drop_pa")
    fcu["flow_m3_h"] = CHILLED_FLOW * 2
    halved = _drops(payload)
    fcu["pressure_drop_pa"] = 0
    zero = _drops(payload)

    assert halved["chw_r"] - zero["chw_r"] == pytest.approx(30000 / 4, rel=1e-3)


def test_typical_and_missing_are_reported_separately():
    """典型值报 info、彻底没数据报 warning；声明了压降（含 0）的设备两边都不列。"""
    payload = _payload("LG340MCF")
    payload["devices"][4].pop("pressure_drop_pa")
    issues = _issues(payload)

    missing = {issue.target: issue for issue in issues["DEVICE_WATER_DROP_MISSING"]}
    typical = {issue.target: issue for issue in issues["DEVICE_WATER_DROP_TYPICAL"]}
    assert all(issue.level == "warning" for issue in missing.values())
    assert all(issue.level == "info" for issue in typical.values())
    # LG340MCF 是只有布置信息的目录行：没有额定水量，典型值无从折算，只能报缺失。
    assert set(missing) == {"chilled_water", "cooling_water"}
    assert "chiller" in missing["chilled_water"].message
    # 冷却塔有额定水量，用上了典型值。
    assert set(typical) == {"cooling_water"}
    assert "tower" in typical["cooling_water"].message
    assert "tower" not in missing["cooling_water"].message
    assert all("fcu" not in issue.message for issue in [*missing.values(), *typical.values()])


def test_runtime_network_uses_the_same_typical_drops():
    """实时侧与静态侧同一张表：R = Δp_典型 / (Q_额定 × 台数)²。

    冷却塔曾被实时侧当成冷机去读冷凝器压降 `cooling_pressure_drop_kpa`
    （字段按"在冷却水环路上"选，而非按冷机选），永远读到 0。
    """
    request = SystemEvaluateRequest.model_validate(SAMPLES["four_floor_fcu_fresh_air"])
    elements = build_runtime_layout(request).layout.network.elements

    tower = elements[("cooling_water", "tower_roof")]
    assert tower.parameter_sources == ("default:water_pressure_drop_kpa",)
    assert tower.resistance == pytest.approx(50000 / 107.65**2, rel=1e-6)

    fcu = elements[("chilled_water", "fcu_f1")]
    rated = find_equipment_spec("风机盘管", "FP-204WA")["water_flow_m3_h"] * 6
    assert fcu.parameter_sources == ("default:water_pressure_drop_kpa",)
    assert fcu.resistance == pytest.approx(30000 / rated**2, rel=1e-6)

    chiller = elements[("cooling_water", "chiller_b1")]
    assert chiller.parameter_sources == ("catalog:cooling_pressure_drop_kpa",)


def test_no_missing_warning_when_every_device_has_a_drop():
    assert "DEVICE_WATER_DROP_MISSING" not in _issues(_payload("SCWE-90H+B"))
