"""内置样例的冷却水回路与风路必须闭合，水冷冷机才能开机

`complete` / `air` 缺冷却水泵、`multi_floor` 的冷却塔没接线、两个 manifold 样例
根本没有冷却塔。连接驱动仿真按实际接线判断冷却水，冷却水不通时水冷冷机按设计
联锁停机（`SIM_CONDENSER_FLOW_MISSING`），这 5 个样例的实时与稳态房温都卡在
`MAX_ROOM_TEMP_C = 40`。`air` 另有风路不通（回风侧悬空、没有风口），组合式机组
任何模式都送不出风（`SIM_AIR_PATH_INCOMPLETE`）。

补齐后同时暴露出房间湿度显式欧拉在稳态（300 s 步长、手动控制风机全开）下的
失稳：`complete` 会议室 Δt·ṁ/M = 2.61 > 2，湿度每步在 75% 与 95% 之间跳、
`SIM_NOT_CONVERGED`。现改为指数欧拉，见 `room.step_room_humidity`。
"""

from __future__ import annotations

import math
from copy import deepcopy

import pytest

from app.main import _evaluate
from app.samples import SAMPLES
from app.schemas import SystemEvaluateRequest
from app.simcore.project import build_project_layout
from app.simcore.psychrometrics import LATENT_HEAT_KJ_KG, humidity_ratio_from_rh
from app.simcore.room import RoomThermalConfig, step_room_humidity
from app.simcore.session import SimulationConfig, SimulationMode, SimulationSession

REPAIRED = ("complete", "air", "multi_floor", "manifold_fcu", "manifold_fcu_dual_water")


@pytest.mark.parametrize("name", REPAIRED)
def test_repaired_samples_have_a_closed_condenser_loop(name: str):
    result = _evaluate(SystemEvaluateRequest(**deepcopy(SAMPLES[name])))
    codes = {issue.code for issue in result.issues}

    assert not codes & {
        "SIM_CONDENSER_FLOW_MISSING",
        "SIM_WATER_CIRCUIT_INCOMPLETE",
        "MISSING_COOLING_TOWER",
        "COOLING_TOWER_NOT_IN_COOLING_WATER",
        "INCOMPLETE_COOLING_WATER_PATH",
        "incomplete_cooling_water_system",
        "SIM_AIR_PATH_INCOMPLETE",
        "SIM_NOT_CONVERGED",
    }
    simulation = result.simulation
    assert simulation is not None
    # 补齐前全部是 40.0 ℃（房间温度上限）
    assert 20.0 < simulation.estimated_room_temp_c < 27.0
    assert simulation.cooling_energy_kw > 0.0


@pytest.mark.parametrize("name", REPAIRED)
def test_repaired_samples_run_their_chiller_in_realtime(name: str):
    layout = build_project_layout(SystemEvaluateRequest(**deepcopy(SAMPLES[name]))).layout
    session = SimulationSession(
        layout,
        SimulationConfig(
            mode=SimulationMode.COOLING, step_seconds=300.0, target_temp_c=24.0,
            target_humidity_pct=55.0, outdoor_humidity_pct=70.0, automatic_control=True,
        ),
    )
    for _ in range(300):
        snapshot = session.step()

    assert "no_cold_source" not in snapshot.alarms
    chillers = [d for d in snapshot.devices.values() if d.role == "chiller"]
    assert chillers and all(d.values["cooling_kw"] > 0.0 for d in chillers)
    towers = [d for d in snapshot.devices.values() if d.role == "cooling_tower"]
    assert towers and all(d.values["flow_m3_h"] > 0.0 for d in towers)
    for reading in snapshot.rooms.values():
        assert reading.temperature_c < 30.0


def test_air_sample_delivers_air_to_both_rooms():
    layout = build_project_layout(SystemEvaluateRequest(**deepcopy(SAMPLES["air"]))).layout
    session = SimulationSession(
        layout, SimulationConfig(mode=SimulationMode.VENTILATION, step_seconds=60.0)
    )
    snapshot = session.step()

    assert snapshot.devices["ahu_1"].values["airflow_m3_h"] > 0.0
    for reading in snapshot.rooms.values():
        assert reading.airflow_m3_h > 0.0


def test_exponential_euler_does_not_overshoot_a_stiff_coil_exchange():
    """Δt·ṁ/M > 2 时显式欧拉越过平衡点，指数欧拉不越过且平衡点不变

    房间 100 kg 空气，盘管 1 kg/s 把房间往送风含湿量 w_送 拉，Δt = 300 s，
    x = Δt·ṁ/M = 3。湿量速率 = ṁ·(w − w_送)·r。
    """
    config = RoomThermalConfig(occupancy_people=0, volume_m3=100.0 / 1.2)
    temp_c, rh_pct = 24.0, 80.0
    w = humidity_ratio_from_rh(temp_c, rh_pct)
    w_supply = humidity_ratio_from_rh(temp_c, 50.0)
    latent_kw = 1.0 * (w - w_supply) * LATENT_HEAT_KJ_KG

    def step(exchange: float) -> float:
        return step_room_humidity(
            rh_pct, temp_c, temp_c, temp_c, 50.0, latent_kw, config, 300.0,
            air_exchange_kg_s=exchange,
        ).humidity_ratio

    explicit = step(0.0)
    exponential = step(1.0)
    # 显式欧拉：w + (w_送 − w)·3，越过平衡点到另一侧
    assert explicit < w_supply
    # 指数欧拉：w_送 + (w − w_送)·e^{-3}，从同一侧逼近
    assert w_supply < exponential < w
    assert exponential == pytest.approx(w_supply + (w - w_supply) * math.exp(-3.0), rel=1e-3)

    # 平衡点处速率为 0，两种积分都原地不动
    at_rest = step_room_humidity(
        50.0, temp_c, temp_c, temp_c, 50.0, 0.0, config, 300.0, air_exchange_kg_s=1.0,
    )
    assert at_rest.humidity_ratio == pytest.approx(w_supply, rel=1e-9)
