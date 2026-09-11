"""湿度控制与目标可达性

覆盖三件事：自动控制允许为除湿适度过冷、湿度目标不可达时如实告警、
以及可达性诊断字段（humidity_shortfall_pct）随读数正确暴露。
"""

from copy import deepcopy

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


def test_humidity_control_does_not_overcool_when_capacity_is_short():
    """除湿能力不足时不得靠继续制冷硬压——那样只会"越冷越湿"。

    实测验证：放开室温闸门对两类样例均无收益（除湿能力由盘管潜热份额
    固定，小于人员散湿+未处理新风湿负荷），反而把房间压得过冷。因此
    控制量不应低于目标室温超过一个小裕量，现状改由告警如实暴露。
    """
    assert AUTO_DEHUMIDIFICATION_SUBCOOL_C == 0.0
    session, snapshot = _run(MULTI_ZONE_ALL_AIR_SAMPLE)
    # 不得出现"为了除湿把房间压到明显过冷"：室温不应大幅低于目标
    # （控制量本身已设为 0；残余偏差来自多分区共用末端的分摊，不是除湿引起）
    for reading in snapshot.rooms.values():
        assert reading.temperature_c >= 26.0 - 2.0
    # 湿度确实压不下来时，必须由告警与缺口读数如实暴露，而不是静默达标
    assert "humidity_target_unreachable" in snapshot.alarms
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
