"""稳态房间得热只取设计负荷中的房间显热

旧实现把 `load.adjusted_cooling_kw` **整份**折算成房间显热得热，而这个总数
含新风显热与潜热（新风由承担它的盘管在机组侧处理，潜热走房间湿平衡）。
房间因此多背两份负荷，末端冷量又被封顶在总负荷，评估页的
`estimated_room_temp_c` 在四个完整样例上全部是 37~39 ℃——高于设计室外 37 ℃。

修法：`design_room_sensible_kw` 只取照明 + 设备 + 太阳辐射 + 围护 + 人员显热，
总负荷仍只用于末端封顶。
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from app.calculators.load import calculate_load
from app.calculators.simulation import (
    design_outdoor_conditions,
    design_room_sensible_kw,
)
from app.main import _evaluate
from app.samples import SAMPLES
from app.schemas import LoadRequest, LoadResult, RoomInput, SystemEvaluateRequest
from app.simcore.project import build_project_layout
from app.simcore.room import PERSON_SENSIBLE_KW
from app.simcore.session import SimulationConfig, SimulationSession
from app.simcore.weather import DEFAULT_SWING_C, PEAK_HOUR, ambient_at


def _single_office_request() -> SystemEvaluateRequest:
    room = RoomInput(
        id="r1", name="办公", area_m2=100.0, height_m=3.0, people=10,
        purpose="office", orientation="internal",
    )
    return SystemEvaluateRequest(rooms=[room], simultaneity_factor=0.9, safety_factor=1.1)


def test_room_sensible_matches_hand_calculation():
    """手算样例：100 m² 内区办公、10 人，默认 35/26 ℃ 设计工况

        照明  100 × 10 W/m² = 1.000 kW
        设备  100 × 15 W/m² = 1.500 kW
        太阳  内区朝向 0 W/m²       = 0
        围护  1.2 W/m²K × 100 × 1.4 × 9 K = 1.512 kW
        人员显热  10 × 0.075 kW    = 0.750 kW
        房间显热合计               = 4.762 kW

    新风 300 m³/h 的显热与潜热、人员潜热都不在其中。总负荷含同时使用系数与
    安全系数，房间显热按份额取，系数自然一并带上。
    """
    request = _single_office_request()
    load = calculate_load(LoadRequest(rooms=request.rooms, simultaneity_factor=0.9, safety_factor=1.1))

    expected_raw = 1.0 + 1.5 + 0.0 + 1.512 + 10 * PERSON_SENSIBLE_KW
    expected = expected_raw * 0.9 * 1.1

    assert design_room_sensible_kw(request, load) == pytest.approx(expected, rel=1e-3)
    # 守恒：总负荷 = 房间显热 + 新风显热 + 新风潜热 + 人员潜热（同乘两个系数）
    components = load.rooms[0].components_kw
    excluded_raw = (
        components["fresh_air_cooling"]
        + components["latent_cooling"]
        + components["people"] - 10 * PERSON_SENSIBLE_KW
    )
    assert design_room_sensible_kw(request, load) + excluded_raw * 0.9 * 1.1 == pytest.approx(
        load.adjusted_cooling_kw, rel=1e-3
    )


def test_total_only_load_gives_the_same_room_sensible():
    """稳态管网求解器只传总负荷（`rooms=[]`），必须得到同一个房间显热

    否则同一响应里 `simulation` 与 `steady_network.equipment.simulation`
    会分叉（`test_room_order_invariance` 钉着两者相等）。
    """
    request = SystemEvaluateRequest(**SAMPLES["four_floor_fcu_fresh_air"])
    full = calculate_load(
        LoadRequest(
            rooms=request.rooms, floors=request.floors,
            simultaneity_factor=request.simultaneity_factor, safety_factor=request.safety_factor,
        )
    )
    total_only = LoadResult(
        total_cooling_kw=full.total_cooling_kw, total_heating_kw=0.0,
        adjusted_cooling_kw=full.adjusted_cooling_kw, adjusted_heating_kw=0.0, rooms=[],
    )

    assert design_room_sensible_kw(request, total_only) == pytest.approx(
        design_room_sensible_kw(request, full), rel=1e-12
    )


def test_no_load_or_no_rooms_does_not_rewrite_room_gain():
    request = _single_office_request()
    empty = LoadResult(
        total_cooling_kw=0.0, total_heating_kw=0.0,
        adjusted_cooling_kw=0.0, adjusted_heating_kw=0.0, rooms=[],
    )

    assert design_room_sensible_kw(request, None) is None
    assert design_room_sensible_kw(request, empty) is None
    assert design_room_sensible_kw(SystemEvaluateRequest(), empty) is None


def test_total_design_load_alone_no_longer_rewrites_room_gain():
    """只给总负荷（封顶用）时房间得热保持模型自身的值，不再被总负荷改写"""
    request = SystemEvaluateRequest(**SAMPLES["four_floor_fcu_fresh_air"])
    layout = build_project_layout(request).layout
    session = SimulationSession(
        layout, SimulationConfig(step_seconds=300.0, design_cooling_load_kw=122.255)
    )
    snapshot = session.step()

    for room in layout.rooms:
        assert snapshot.rooms[room.room_id].internal_gain_kw == pytest.approx(
            room.thermal.internal_gain_kw
        )


def test_design_room_sensible_must_be_finite():
    with pytest.raises(ValueError, match="design room sensible load"):
        replace(SimulationConfig(), design_room_sensible_kw=float("nan"))
    with pytest.raises(ValueError, match="design room sensible load"):
        replace(SimulationConfig(), design_room_sensible_kw=-1.0)


def test_design_outdoor_conditions_reproduce_static_design_point():
    """稳态设计时刻的室外 = 静态负荷用的室外（35 ℃ / 65%）

    稳态固定在 `PEAK_HOUR`，`ambient_at` 在该时刻给出 基准 + 振幅，
    所以反解出的基准必须让 `_ambient()` 精确回到设计干球。
    """
    request = SystemEvaluateRequest(**SAMPLES["four_floor_fcu_fresh_air"])
    outdoor = design_outdoor_conditions(request)
    assert outdoor is not None
    base_c, humidity_pct = outdoor

    ambient = ambient_at(PEAK_HOUR * 3600.0, base_c, humidity_pct)
    assert ambient.dry_bulb_c == pytest.approx(35.0)
    assert ambient.relative_humidity_pct == pytest.approx(65.0)


def test_design_outdoor_conditions_average_rooms_and_clamp_humidity():
    rooms = [
        RoomInput(id="a", area_m2=50.0, outdoor_summer_c=34.0, outdoor_relative_humidity=0.0),
        RoomInput(id="b", area_m2=50.0, outdoor_summer_c=36.0, outdoor_relative_humidity=0.0),
    ]
    base_c, humidity_pct = design_outdoor_conditions(SystemEvaluateRequest(rooms=rooms)) or (0.0, 0.0)

    assert base_c + DEFAULT_SWING_C == pytest.approx(35.0)
    # 静态侧允许 0%，仿真核只接受 [5, 99] %
    assert humidity_pct == pytest.approx(5.0)
    assert design_outdoor_conditions(SystemEvaluateRequest()) is None


# 修复前四个样例分别是 37.44 / 38.88 / 37.17 / 38.53 ℃。
# 只修房间显热时是 27.88 / 26.80 / 28.92 / 30.12 ℃：剩余偏高来自稳态室外取默认
# 气象 37 ℃/70%，而静态负荷按 35 ℃/65%。两处都对齐后是 25.045 / 23.434 / 25.969 /
# 27.164 ℃。补上新风显热之后（`test_room_ventilation.py`），三个新风机组样例又升了
# 0.01~0.15 K：末端封顶后新风机组送风 25.5 ℃，比会议室还热，旧实现把这份加热截成 0。
# 与设计室内 26 ℃ 的剩余偏差（−2.6 ~ +1.2 K）尚未逐项拆解。可能的来源：末端封顶
# 按比例分摊到新风盘管与房间盘管、稳态手动控制下阀门不随室温调节、仿真核的房间
# 得热与静态负荷的分项口径不完全一致。
@pytest.mark.parametrize(
    ("name", "expected_room_temp_c"),
    [
        ("four_floor_fcu_fresh_air", 25.198),
        ("multi_zone_all_air", 23.434),
        ("ducted_fresh_air", 26.040),
        ("three_floor_twelve_room", 27.177),
    ],
)
def test_steady_room_temperature_is_pinned_near_design(name: str, expected_room_temp_c: float):
    result = _evaluate(SystemEvaluateRequest(**SAMPLES[name]))
    simulation = result.simulation
    assert simulation is not None

    assert simulation.estimated_room_temp_c == pytest.approx(expected_room_temp_c, abs=0.01)
    # 设计工况下末端在运行，房间温度必须落在设计室内温度附近
    assert abs(simulation.estimated_room_temp_c - 26.0) < 3.0
    assert simulation.supply_air_temp_c < simulation.estimated_room_temp_c
