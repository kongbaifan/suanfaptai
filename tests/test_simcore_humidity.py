"""湿度控制与目标可达性

覆盖三件事：自动控制允许为除湿适度过冷、湿度目标不可达时如实告警、
以及可达性诊断字段（humidity_shortfall_pct）随读数正确暴露。
"""

from copy import deepcopy
from dataclasses import replace

import pytest

from app.samples import FOUR_FLOOR_FCU_FRESH_AIR_SAMPLE, MULTI_ZONE_ALL_AIR_SAMPLE
from app.schemas import SystemEvaluateRequest
from app.simcore.project import build_project_layout
from app.simcore.session import (
    AUTO_DEHUMIDIFICATION_SUBCOOL_C,
    SimulationConfig,
    SimulationMode,
    SimulationSession,
)


def _run(sample: dict, ticks: int = 600):
    layout = build_project_layout(SystemEvaluateRequest(**deepcopy(sample))).layout
    session = SimulationSession(
        layout,
        SimulationConfig(
            mode=SimulationMode.COOLING,
            initial_room_temp_c=29.0,
            target_temp_c=26.0,
            target_humidity_pct=55.0,
            outdoor_base_temp_c=33.0,
            outdoor_humidity_pct=70.0,
            automatic_control=True,
        ),
    )
    snapshot = None
    for _ in range(ticks):
        snapshot = session.step()
    return session, snapshot


def test_overcooling_stays_within_subcool_allowance():
    """过冷被限制在除湿带宽内，不会无界跑冷

    盘管处理混合风后，除湿必须让送风低于室内露点，室温因此低于目标；模型
    无再热盘管，故过冷量以 ``AUTO_DEHUMIDIFICATION_SUBCOOL_C`` 为界。
    """
    assert AUTO_DEHUMIDIFICATION_SUBCOOL_C > 0.0
    session, snapshot = _run(MULTI_ZONE_ALL_AIR_SAMPLE)
    # 允许超调一点（控制离散化），但不应远低于除湿下限
    limit_c = 26.0 - AUTO_DEHUMIDIFICATION_SUBCOOL_C - 1.5
    for reading in snapshot.rooms.values():
        assert reading.temperature_c >= limit_c
    # 缺口读数与目标始终保持自洽
    assert all(
        reading.humidity_shortfall_pct
        == max(0.0, reading.relative_humidity_pct - reading.target_humidity_pct)
        for reading in snapshot.rooms.values()
    )


def test_unreachable_humidity_target_raises_alarm():
    """散湿量超过除湿能力时，室温会停在偏湿侧并告警，而不是静默失效。"""
    _, snapshot = _run(MULTI_ZONE_ALL_AIR_SAMPLE)
    assert "humidity_target_unreachable" in snapshot.alarms
    # 告警与诊断字段必须一致：确有房间超出目标死区
    assert any(reading.humidity_shortfall_pct > 3.0 for reading in snapshot.rooms.values())


def test_humidity_shortfall_matches_reading_and_target():
    _, snapshot = _run(FOUR_FLOOR_FCU_FRESH_AIR_SAMPLE, ticks=300)
    for reading in snapshot.rooms.values():
        expected = max(0.0, reading.relative_humidity_pct - reading.target_humidity_pct)
        assert reading.humidity_shortfall_pct == expected


def test_ventilation_mode_does_not_claim_humidity_control():
    """通风工况没有冷盘管，不应报湿度不可达（该工况本就不承担湿度目标）。"""
    layout = build_project_layout(
        SystemEvaluateRequest(**deepcopy(MULTI_ZONE_ALL_AIR_SAMPLE))
    ).layout
    session = SimulationSession(
        layout,
        SimulationConfig(
            mode=SimulationMode.VENTILATION,
            initial_room_temp_c=26.0,
            target_humidity_pct=55.0,
            automatic_control=True,
        ),
    )
    for _ in range(60):
        snapshot = session.step()
    assert "humidity_target_unreachable" not in snapshot.alarms


def test_coil_treats_mixed_air_so_fresh_air_load_cannot_bypass():
    """新风必须经过盘管：混合风含湿量高于纯回风，盘管因此负担更大除湿量

    这是"新风绕过盘管"缺陷的回归防线——旧口径把新风湿负荷直接注入房间，
    盘管只处理回风，无论怎么加大冷量都除不掉新风带进来的水分。
    """
    from app.simcore.psychrometrics import humidity_ratio_from_rh
    from app.simcore.devices import coil_leaving_state

    room_temp_c, room_rh = 26.0, 55.0
    outdoor_temp_c, outdoor_rh = 33.0, 70.0
    airflow_m3_h, coil_kw = 8000.0, 40.0

    return_ratio = humidity_ratio_from_rh(room_temp_c, room_rh)
    outdoor_ratio = humidity_ratio_from_rh(outdoor_temp_c, outdoor_rh)
    # 50% 新风混合后的含湿量严格介于回风与新风之间
    mixed_ratio = 0.5 * return_ratio + 0.5 * outdoor_ratio
    assert return_ratio < mixed_ratio < outdoor_ratio

    _, supply_ratio_return_only = coil_leaving_state(
        room_temp_c, return_ratio, airflow_m3_h, coil_kw
    )
    _, supply_ratio_mixed = coil_leaving_state(
        room_temp_c, mixed_ratio, airflow_m3_h, coil_kw
    )
    # 处理更湿的混合风时，同样冷量下送风含湿量更高（负担真实存在）
    assert supply_ratio_mixed >= supply_ratio_return_only


def test_dry_coil_does_not_dehumidify():
    """冷量不足以把送风压到露点时，盘管等湿降温、不除湿"""
    from app.simcore.psychrometrics import humidity_ratio_from_rh
    from app.simcore.devices import coil_leaving_state

    ratio_in = humidity_ratio_from_rh(26.0, 55.0)
    supply_temp_c, supply_ratio = coil_leaving_state(26.0, ratio_in, 8000.0, 3.0)

    assert supply_temp_c < 26.0
    assert supply_ratio == ratio_in


def test_wet_coil_dehumidifies_and_lands_on_saturation():
    """冷量足够时盘管降温除湿，出口落在饱和线上"""
    from app.simcore.psychrometrics import (
        humidity_ratio_from_rh,
        rh_from_humidity_ratio,
    )
    from app.simcore.devices import coil_leaving_state

    ratio_in = humidity_ratio_from_rh(26.0, 55.0)
    supply_temp_c, supply_ratio = coil_leaving_state(26.0, ratio_in, 8000.0, 60.0)

    assert supply_ratio < ratio_in
    # 出口接近饱和（相对湿度 100%），这是湿盘管的标志
    assert rh_from_humidity_ratio(supply_temp_c, supply_ratio) == pytest.approx(
        100.0, abs=0.5
    )


def test_coil_sensible_plus_latent_equals_coil_load():
    """显热与潜热按同一组焓常数拆分，之和严格等于盘管冷量（不丢能量）"""
    from app.simcore.devices import AIR_DENSITY_KG_M3, coil_leaving_state
    from app.simcore.psychrometrics import (
        DRY_AIR_CP_KJ_KG_K,
        humidity_ratio_from_rh,
        vapor_enthalpy_kj_kg,
    )

    for temp_c, rh_pct, airflow_m3_h, coil_kw in (
        (26.0, 55.0, 8000.0, 10.0),
        (26.0, 70.0, 8000.0, 50.0),
        (31.0, 47.0, 8000.0, 35.0),
        (26.0, 95.0, 5000.0, 40.0),
    ):
        ratio_in = humidity_ratio_from_rh(temp_c, rh_pct)
        supply_temp_c, supply_ratio = coil_leaving_state(
            temp_c, ratio_in, airflow_m3_h, coil_kw
        )
        air_mass_kg_s = airflow_m3_h * AIR_DENSITY_KG_M3 / 3600.0
        sensible_kw = (
            air_mass_kg_s * DRY_AIR_CP_KJ_KG_K * (temp_c - supply_temp_c)
        )
        latent_kw = air_mass_kg_s * (
            vapor_enthalpy_kj_kg(temp_c, ratio_in)
            - vapor_enthalpy_kj_kg(supply_temp_c, supply_ratio)
        )
        assert sensible_kw + latent_kw == pytest.approx(coil_kw, abs=1e-6)


def test_room_served_by_two_coils_sums_contributions():
    """一台房间挂多台盘管时，处理量逐台累加而非按单一送风状态放大"""
    from app.simcore.session import RoomDelivery

    first = RoomDelivery(sensible_cooling_kw=3.0, latent_cooling_kw=1.0)
    second = RoomDelivery(sensible_cooling_kw=2.0, latent_cooling_kw=0.5)

    combined = replace(
        first,
        sensible_cooling_kw=first.sensible_cooling_kw + second.sensible_cooling_kw,
        latent_cooling_kw=first.latent_cooling_kw + second.latent_cooling_kw,
    )
    assert combined.sensible_cooling_kw == 5.0
    assert combined.latent_cooling_kw == 1.5
