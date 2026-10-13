"""房间显热平衡必须计入新风显热

旧实现的 `step_room_temperature` 只有围护、室内得热与盘管冷热量三项，新风只进了
湿平衡。制热时新风机组把 −7 ℃ 的室外风送进房间，房间却感受不到，被内部得热
顶到 30~40 ℃（`multi_zone_all_air` 会议区触到 `MAX_ROOM_TEMP_C`）；通风模式与
只配风机盘管的制冷房间同样漏算。

修法：送风对房间的显热 ṁ·c_p·(T_送 − T_室) 拆成盘管处理量 ṁ·c_p·(T_混 − T_送)
与新风带入量 ṁ·c_p·(T_混 − T_室)，后者作为 `ventilation_kw` 进入房间平衡；没有
任何机组承担新风的房间，新风按室外状态直接计入（显热与湿量用同一风量）。
"""

from __future__ import annotations

import statistics
from copy import deepcopy

import pytest

from app.samples import SAMPLES
from app.schemas import SystemEvaluateRequest
from app.simcore.project import build_project_layout
from app.simcore.psychrometrics import DRY_AIR_CP_KJ_KG_K
from app.simcore.room import AIR_DENSITY_KG_M3, RoomThermalConfig, step_room_temperature
from app.simcore.session import SimulationConfig, SimulationMode, SimulationSession


def test_untreated_fresh_air_sensible_hand_calculation():
    """手算：300 m³/h 室外 −5 ℃ 新风直接进入 20 ℃ 房间

        ṁ = 300 × 1.2 / 3600 = 0.1 kg/s
        Q = 0.1 × 1.006 × (−5 − 20) = −2.515 kW
        ΔT = Q · Δt / C = −2.515 × 60 / 10000 = −0.01509 K
    """
    config = RoomThermalConfig(
        thermal_capacity_kj_k=10000.0, envelope_conductance_kw_k=0.0,
        occupancy_people=0, lighting_w=0.0, equipment_w=0.0,
    )
    balance = step_room_temperature(
        current_temp_c=20.0, outdoor_temp_c=-5.0, cooling_kw=0.0, config=config,
        step_seconds=60.0, fresh_air_m3_h=300.0,
    )

    expected_kw = 300.0 * AIR_DENSITY_KG_M3 / 3600.0 * DRY_AIR_CP_KJ_KG_K * (-25.0)
    assert expected_kw == pytest.approx(-2.515, abs=1e-3)
    assert balance.ventilation_kw == pytest.approx(expected_kw)
    assert balance.net_kw == pytest.approx(expected_kw)
    assert balance.temperature_c == pytest.approx(20.0 + expected_kw * 60.0 / 10000.0)


def test_treated_ventilation_term_enters_net_heat_flow():
    config = RoomThermalConfig(envelope_conductance_kw_k=0.0, occupancy_people=0,
                               lighting_w=0.0, equipment_w=0.0)
    balance = step_room_temperature(
        current_temp_c=22.0, outdoor_temp_c=0.0, cooling_kw=0.0, config=config,
        step_seconds=60.0, heating_kw=3.0, ventilation_kw=-5.0,
    )
    assert balance.ventilation_kw == pytest.approx(-5.0)
    assert balance.net_kw == pytest.approx(-2.0)


def _session(name: str, mode: SimulationMode, target_c: float, outdoor_c: float) -> SimulationSession:
    layout = build_project_layout(SystemEvaluateRequest(**deepcopy(SAMPLES[name]))).layout
    return SimulationSession(
        layout,
        SimulationConfig(
            mode=mode, step_seconds=300.0, target_temp_c=target_c, target_humidity_pct=55.0,
            outdoor_base_temp_c=outdoor_c, outdoor_humidity_pct=70.0, fixed_hour_of_day=14.0,
            initial_room_temp_c=20.0, automatic_control=True,
        ),
    )


@pytest.mark.parametrize(
    "name", ["four_floor_fcu_fresh_air", "multi_zone_all_air", "complete", "manifold_fcu_dual_water"]
)
@pytest.mark.parametrize(
    "mode", [SimulationMode.COOLING, SimulationMode.HEATING, SimulationMode.VENTILATION]
)
def test_room_balance_terms_reproduce_the_temperature_step(name: str, mode: SimulationMode):
    """每个房间每一步：ΔT · C / Δt = 围护 + 室内得热 + 新风显热 + 制热 − 制冷

    房间温度触到边界会被钳住，这里只检查未被钳的房间。
    """
    session = _session(name, mode, 24.0 if mode is not SimulationMode.HEATING else 20.0,
                       31.0 if mode is not SimulationMode.HEATING else 0.0)
    for _ in range(30):
        session.step()
    before = session.snapshot
    after = session.step()
    capacity = {room.room_id: room.thermal.thermal_capacity_kj_k for room in session.layout.rooms}

    checked = 0
    for room_id, reading in after.rooms.items():
        if not 10.0 < reading.temperature_c < 40.0:
            continue
        net_kw = (
            reading.envelope_kw + reading.internal_gain_kw + reading.ventilation_kw
            + reading.heating_kw - reading.cooling_kw
        )
        delta_c = reading.temperature_c - before.rooms[room_id].temperature_c
        assert delta_c * capacity[room_id] / 300.0 == pytest.approx(net_kw, abs=1e-9)
        checked += 1
    assert checked > 0


def test_fan_coil_only_rooms_take_outdoor_air_sensible_when_fans_run():
    """只配风机盘管的房间：新风按室外状态直接进入，显热 = ṁ·c_p·(T_外 − T_室)

    `complete` 缺冷却水回路、冷机联锁停机，但风机盘管风机照常运行——正好
    单独观察未经处理的新风。旧实现这一项为 0（新风只加湿、不加热房间）。
    """
    session = _session("complete", SimulationMode.COOLING, 24.0, 31.0)
    for _ in range(20):
        session.step()
    before = session.snapshot
    after = session.step()
    rooms = {room.room_id: room for room in session.layout.rooms}

    for room_id, reading in after.rooms.items():
        fresh_m3_h = min(rooms[room_id].fresh_air_m3_h, reading.airflow_m3_h)
        expected = (
            fresh_m3_h * AIR_DENSITY_KG_M3 / 3600.0 * DRY_AIR_CP_KJ_KG_K
            * (after.ambient.dry_bulb_c - before.rooms[room_id].temperature_c)
        )
        assert fresh_m3_h > 0.0
        assert reading.ventilation_kw == pytest.approx(expected, rel=1e-9)


def test_heating_holds_rooms_near_target_once_fresh_air_cools_them():
    """新风机组按卫生新风量常开：制热时房间被新风冷却，由风机盘管补热到目标附近

    修复前同一工况为 24.7~36.8 ℃（目标 20 ℃），内部得热无处可去。

    注：`multi_zone_all_air`（组合式机组兼管新风）此处**不适用**——组合式机组的风机
    跟随房间需求启停，房间一旦高于目标就整机停下，新风随之中断，房间靠内部得热
    停在 26~37 ℃。修复前后一致，属控制律缺口，见 CLAUDE.md 已知缺陷。
    """
    session = _session("four_floor_fcu_fresh_air", SimulationMode.HEATING, 20.0, 0.0)
    for _ in range(1200):
        session.step()
    means: dict[str, list[float]] = {room.room_id: [] for room in session.layout.rooms}
    for _ in range(100):
        snapshot = session.step()
        for room_id, values in means.items():
            values.append(snapshot.rooms[room_id].temperature_c)
        assert sum(r.ventilation_kw for r in snapshot.rooms.values()) < 0.0

    for room_id, values in means.items():
        assert 18.5 <= statistics.fmean(values) <= 21.0, (room_id, statistics.fmean(values))


def test_heating_without_heat_source_settles_where_losses_balance_gains():
    """`ducted_fresh_air` 没有热源：制热模式下房间应冷到围护 + 新风散热与内部得热平衡

    稳态时 0 = UA·(T_外 − T) + Q_内 + ṁ·c_p·(T_外 − T)，即
    T = T_外 + Q_内 / (UA + ṁ·c_p)。旧实现没有新风项，房间停在 22~28 ℃。
    """
    session = _session("ducted_fresh_air", SimulationMode.HEATING, 20.0, 0.0)
    for _ in range(1500):
        session.step()
    snapshot = session.step()
    outdoor_c = snapshot.ambient.dry_bulb_c

    for room in session.layout.rooms:
        reading = snapshot.rooms[room.room_id]
        temp_c = reading.temperature_c
        mass_cp = reading.ventilation_kw / (outdoor_c - temp_c)
        expected = outdoor_c + room.thermal.internal_gain_kw / (
            room.thermal.envelope_conductance_kw_k + mass_cp
        )
        assert temp_c == pytest.approx(expected, abs=0.02)
        # 没有新风项时平衡在 T_外 + Q_内 / UA，这里必须明显更冷
        without_ventilation = outdoor_c + room.thermal.internal_gain_kw / (
            room.thermal.envelope_conductance_kw_k
        )
        assert mass_cp > 0.0
        assert temp_c < without_ventilation - 5.0
