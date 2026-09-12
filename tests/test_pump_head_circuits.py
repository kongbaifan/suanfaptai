"""水泵扬程按闭合环路计算

钉死两个缺陷的判据：
1. 环路必须同时含供程与回程。只枚举单管程会漏掉回程阻力，扬程偏小近一半。
2. 串联设备的水阻只能计一次。原先按"逐根相邻管各加一次"摊派，
   环路把供回两根管一起累加时，同一台设备被算了两遍。

期望值全部手算：
  流量 = 0.86 × 50 / 5 = 8.6 m³/h，拓扑分配后每根管 4.3 m³/h
  DN65 内径 0.065 m，面积 = π×0.065²/4 = 0.003318 m²（保留 6 位）
  流速 = (4.3/3600) / 0.003318 = 0.35999 m/s
  动压 = 1000 × 0.35999² / 2 = 64.796 Pa
  摩阻 = 0.025 × (20/0.065) × 64.796 = 498.43 Pa
  局部 = 2.0 × 64.796 = 129.59 Pa
  单管 = 628.024 Pa   环路 = 1256.048 Pa
  扬程 = 1256.048 / (1000 × 9.80665) × 1.1 = 0.141 m
"""

import pytest

from app.calculators.hydraulic_network import calculate_hydraulic_network
from app.schemas import SystemEvaluateRequest


CHILLER = "冷源机组"
PUMP = "循环泵"
FCU = "风机盘管"
FAU = "新风机组"

SINGLE_PIPE_DROP_PA = 628.024
CIRCUIT_DROP_PA = SINGLE_PIPE_DROP_PA * 2
CIRCUIT_HEAD_M = 0.141
# 新风机组 MKS03D4Y/C 的目录水阻，用来分辨"计一次"与"计两次"
FAU_WATER_DROP_PA = 30_600.0


def _pipes():
    return [
        {"id": "chw_supply", "system": "chilled_water", "length_m": 20, "diameter_mm": 65},
        {"id": "chw_return", "system": "chilled_water", "length_m": 20, "diameter_mm": 65},
    ]


def _loop_connections(terminal_id: str):
    return [
        {"from_id": "chiller", "to_id": "pump", "system": "chilled_water"},
        {"from_id": "pump", "to_id": "chw_supply", "system": "chilled_water"},
        {"from_id": "chw_supply", "to_id": terminal_id, "system": "chilled_water"},
        {"from_id": terminal_id, "to_id": "chw_return", "system": "chilled_water"},
        {"from_id": "chw_return", "to_id": "chiller", "system": "chilled_water"},
    ]


def _fcu_loop():
    return SystemEvaluateRequest(
        devices=[
            {"id": "chiller", "type": CHILLER, "name": "SCWE-90H+B"},
            {"id": "pump", "type": PUMP, "name": "EC80"},
            {"id": "fcu", "type": FCU, "name": "MCW1000V"},
        ],
        pipes=_pipes(),
        connections=_loop_connections("fcu"),
    )


def test_pump_head_sums_supply_and_return_legs():
    """闭合环路的阻力 = 供程 + 回程，不是其中一条。"""
    result = calculate_hydraulic_network(_fcu_loop(), cooling_load_kw=50)

    by_pipe = {pipe.pipe_id: pipe for pipe in result.pipes}
    assert by_pipe["chw_supply"].pressure_drop_pa == pytest.approx(SINGLE_PIPE_DROP_PA, abs=1e-3)
    assert by_pipe["chw_return"].pressure_drop_pa == pytest.approx(SINGLE_PIPE_DROP_PA, abs=1e-3)

    circuits = [path for path in result.paths if path.system == "chilled_water"]
    assert len(circuits) == 1
    circuit = circuits[0]
    assert circuit.closed_circuit is True
    # 供回两根管都在同一条环路里——这就是"单管程"缺陷的哨兵
    assert sorted(circuit.pipe_ids) == ["chw_return", "chw_supply"]
    assert circuit.pressure_drop_pa == pytest.approx(CIRCUIT_DROP_PA, abs=1e-3)

    assert result.required_pump_head_m == pytest.approx(CIRCUIT_HEAD_M, abs=1e-3)
    assert result.pump_check is not None
    assert result.pump_check.required_head_m == pytest.approx(CIRCUIT_HEAD_M, abs=1e-3)


def test_series_device_resistance_counted_once_per_circuit():
    """串联设备同时挨着供回水管，水阻只能计一次。

    新风机组 MKS03D4Y/C 目录水阻 30.6 kPa：
    计一次 → 环路 1256.048 + 30600 = 31856.048 Pa
    计两次 → 环路 1256.048 + 61200 = 62456.048 Pa（改前的错误值）
    """
    request = SystemEvaluateRequest(
        devices=[
            {"id": "chiller", "type": CHILLER, "name": "SCWE-90H+B"},
            {"id": "pump", "type": PUMP, "name": "EC80"},
            {"id": "fau", "type": FAU, "name": "MKS03D4Y/C"},
        ],
        pipes=_pipes(),
        connections=_loop_connections("fau"),
    )
    result = calculate_hydraulic_network(request, cooling_load_kw=50)

    circuits = [path for path in result.paths if path.system == "chilled_water"]
    assert len(circuits) == 1
    assert circuits[0].closed_circuit is True
    assert circuits[0].pressure_drop_pa == pytest.approx(
        CIRCUIT_DROP_PA + FAU_WATER_DROP_PA, abs=1e-3
    )

    # 与无设备水阻的同构环路相减，差值必须恰好是一台设备的压降
    plain = calculate_hydraulic_network(_fcu_loop(), cooling_load_kw=50)
    plain_circuit = [path for path in plain.paths if path.system == "chilled_water"][0]
    delta = circuits[0].pressure_drop_pa - plain_circuit.pressure_drop_pa
    assert delta == pytest.approx(FAU_WATER_DROP_PA, abs=1e-3)
    assert delta != pytest.approx(FAU_WATER_DROP_PA * 2, abs=1.0)

    assert result.required_pump_head_m == pytest.approx(3.573, abs=1e-3)


def test_pump_own_pressure_drop_is_not_a_circuit_loss():
    """泵是加压部件，它的压降不计入环路阻力。

    否则等于让泵自己抵消自己的扬程。
    """
    request = SystemEvaluateRequest(
        devices=[
            {"id": "chiller", "type": CHILLER, "name": "SCWE-90H+B"},
            {"id": "pump", "type": PUMP, "name": "EC80", "pressure_drop_pa": 40_000},
            {"id": "fcu", "type": FCU, "name": "MCW1000V"},
        ],
        pipes=_pipes(),
        connections=_loop_connections("fcu"),
    )
    with_pump_drop = calculate_hydraulic_network(request, cooling_load_kw=50)
    baseline = calculate_hydraulic_network(_fcu_loop(), cooling_load_kw=50)

    assert with_pump_drop.required_pump_head_m == baseline.required_pump_head_m
    assert with_pump_drop.required_pump_head_m == pytest.approx(CIRCUIT_HEAD_M, abs=1e-3)


def test_open_loop_falls_back_to_pipe_sum_and_warns():
    """环路不闭合时按管段之和兜底（偏保守），并如实告警。

    两根冷冻水管：供水 20 m（0.07 m）+ 另一根 30 m（0.098 m）= 0.168 m。
    取 max 会得 0.098 m，偏小；教学软件里画到一半是常态，故取和并告警。
    """
    request = SystemEvaluateRequest(
        devices=[
            {"id": "chiller", "type": CHILLER, "name": "SCWE-90H+B"},
            {"id": "pump", "type": PUMP, "name": "EC80"},
            {"id": "fcu", "type": FCU, "name": "MCW1000V"},
        ],
        pipes=[
            {"id": "chw_supply", "system": "chilled_water", "length_m": 20, "diameter_mm": 65},
            {"id": "chw_extra", "system": "chilled_water", "length_m": 30, "diameter_mm": 65},
        ],
        connections=[
            {"from_id": "chiller", "to_id": "pump", "system": "chilled_water"},
            {"from_id": "pump", "to_id": "chw_supply", "system": "chilled_water"},
            {"from_id": "chw_supply", "to_id": "fcu", "system": "chilled_water"},
            {"from_id": "fcu", "to_id": "chw_extra", "system": "chilled_water"},
        ],
    )
    result = calculate_hydraulic_network(request, cooling_load_kw=50)

    assert not any(path.closed_circuit for path in result.paths)
    assert "WATER_CIRCUIT_NOT_CLOSED" in {issue.code for issue in result.issues}

    pipe_sum = sum(
        pipe.pump_head_m or 0.0 for pipe in result.pipes if pipe.system == "chilled_water"
    )
    assert result.required_pump_head_m == pytest.approx(pipe_sum, abs=1e-3)
    assert result.required_pump_head_m == pytest.approx(0.168, abs=1e-3)


def test_air_ring_is_never_a_closed_circuit():
    """风系统是开式的：即使图上画成环，也不能当闭合环路。

    空气不经风管回到风机，送风口就是终点。
    """
    request = SystemEvaluateRequest(
        devices=[
            {"id": "chiller", "type": CHILLER, "name": "SCWE-90H+B"},
            {"id": "pump", "type": PUMP, "name": "EC80"},
            {"id": "fcu", "type": FCU, "name": "MCW1000V"},
            {"id": "diffuser", "type": "送风口", "name": "*"},
        ],
        pipes=_pipes()
        + [
            {
                "id": "duct_1",
                "system": "air",
                "length_m": 12,
                "width_mm": 500,
                "height_mm": 320,
            }
        ],
        connections=_loop_connections("fcu")
        + [
            {"from_id": "fcu", "to_id": "duct_1", "system": "air"},
            {"from_id": "duct_1", "to_id": "diffuser", "system": "air"},
            {"from_id": "diffuser", "to_id": "fcu", "system": "air"},
        ],
    )
    result = calculate_hydraulic_network(request, cooling_load_kw=50)

    assert not any(path.system == "air" for path in result.paths)
    # 水侧仍按环路算，不受风侧成环影响
    assert result.required_pump_head_m == pytest.approx(CIRCUIT_HEAD_M, abs=1e-3)
