import math

import pytest

from app.calculators.air import calculate_air
from app.calculators.load import calculate_load
from app.calculators.water import calculate_water
from app.schemas import AirRequest, LoadRequest, RoomInput, WaterRequest


KW = pytest.approx


def test_load_components_match_hand_calculation():
    """逐项钉死手算值。

    房间：100 m²、10 人、office（冷指标 115、照明 10、设备 15、人均新风 30）、
    朝南、层高 3 m、默认 26/35℃ 与 -5/20℃、U=1.2、外露系数 1.4、窗墙比 0.25。

    新风量 = 10 × 30 = 300 m³/h，质量流量 = 1.2 × 300 / 3600 = 0.1 kg/s
    人员     = 10 × 0.12                        = 1.2 kW
    照明     = 100 × 10 / 1000                  = 1.0 kW
    设备     = 100 × 15 / 1000                  = 1.5 kW
    新风显热 = 1.005 × 0.1 × (35 - 26)          = 0.9045 kW
    新风潜热 = 0.1 × Δd × 2501，Δd = d(35℃,65%) - d(26℃,55%)
             = 0.0232507 - 0.0115373 = 0.0117135 kg/kg → 2.9295 kW
    太阳     外墙 = √100 × 3 = 30 m²，窗 = 30 × 0.25 = 7.5 m²
             = 7.5 × 140 × 0.55 / 1000           = 0.5775 kW
    围护得热 = 1.2 × (100 × 1.4) × 9 / 1000      = 1.512 kW
    合计冷负荷                                    = 9.6235 kW
    围护失热 = 1.2 × 140 × 25 / 1000            = 4.2 kW
    新风加热 = 1.005 × 0.1 × 25                 = 2.5125 kW
    合计热负荷                                    = 6.7125 kW
    """
    result = calculate_load(
        LoadRequest(rooms=[RoomInput(name="r1", area_m2=100, people=10)])
    )
    room = result.rooms[0]

    assert room.fresh_air_m3_h == KW(300.0)
    assert room.components_kw["people"] == KW(1.2, abs=1e-3)
    assert room.components_kw["lighting"] == KW(1.0, abs=1e-3)
    assert room.components_kw["equipment"] == KW(1.5, abs=1e-3)
    assert room.components_kw["fresh_air_cooling"] == KW(0.9045, abs=1e-3)
    assert room.components_kw["latent_cooling"] == KW(2.9295, abs=1e-3)
    assert room.components_kw["solar"] == KW(0.5775, abs=1e-3)
    assert room.components_kw["envelope_cooling"] == KW(1.512, abs=1e-3)
    assert room.components_kw["envelope_heating"] == KW(4.2, abs=1e-3)
    assert room.components_kw["fresh_air_heating"] == KW(2.5125, abs=1e-3)

    assert room.cooling_kw == KW(9.6235, abs=1e-3)
    assert room.heating_kw == KW(6.7125, abs=1e-3)


def test_area_index_is_a_cross_check_not_a_load_component():
    """面积指标不进总量，只作为校核值并列输出。

    冷指标估算 = 100 × 115 / 1000 = 11.5 kW
    冷偏差 = (9.6235 - 11.5) / 11.5 × 100 = -16.32%
    热指标估算 = 100 × 75 / 1000 = 7.5 kW
    热偏差 = (6.7125 - 7.5) / 7.5 × 100 = -10.5%
    """
    result = calculate_load(
        LoadRequest(rooms=[RoomInput(name="r1", area_m2=100, people=10)])
    )
    room = result.rooms[0]

    assert "area_index" not in room.components_kw
    assert room.index_estimate_kw == KW(11.5, abs=1e-3)
    assert room.index_deviation_pct == KW(-16.32, abs=1e-2)
    # 供暖同样：指标不再用 max() 盖住分项，只作校核
    assert room.heating_index_estimate_kw == KW(7.5, abs=1e-3)
    assert room.heating_index_deviation_pct == KW(-10.5, abs=1e-2)
    # 分项之和恒等于总冷负荷——这条断言就是重复计量的哨兵
    cooling_keys = (
        "people",
        "lighting",
        "equipment",
        "fresh_air_cooling",
        "latent_cooling",
        "solar",
        "envelope_cooling",
    )
    assert math.fsum(room.components_kw[key] for key in cooling_keys) == KW(
        room.cooling_kw, abs=1e-3
    )


def test_latent_load_follows_humidity_ratio_not_relative_humidity():
    """潜热由含湿量差决定。

    室外 35℃/40% 的含湿量 (0.01418) 高于室内 26℃/55% (0.01154)，
    即使相对湿度更低，潜热仍为正——按相对湿度差算会得到 0。
    """
    drier_outdoor = calculate_load(
        LoadRequest(
            rooms=[
                RoomInput(
                    name="r1", area_m2=100, people=10, outdoor_relative_humidity=0.40
                )
            ]
        )
    )
    assert drier_outdoor.rooms[0].components_kw["latent_cooling"] > 0.5

    # 含湿量确实低于室内时才为零：室外 10℃/40%
    no_latent = calculate_load(
        LoadRequest(
            rooms=[
                RoomInput(
                    name="r1",
                    area_m2=100,
                    people=10,
                    outdoor_summer_c=10.0,
                    outdoor_relative_humidity=0.40,
                )
            ]
        )
    )
    assert no_latent.rooms[0].components_kw["latent_cooling"] == 0.0


def test_window_area_scales_with_wall_area_not_floor_area():
    """窗面积取自外墙面积：层高翻倍则太阳得热翻倍，与地面面积无关。"""
    low = calculate_load(
        LoadRequest(rooms=[RoomInput(name="r1", area_m2=100, people=0, height_m=3.0)])
    )
    tall = calculate_load(
        LoadRequest(rooms=[RoomInput(name="r1", area_m2=100, people=0, height_m=6.0)])
    )

    assert tall.rooms[0].components_kw["solar"] == KW(
        low.rooms[0].components_kw["solar"] * 2.0, abs=1e-3
    )


def test_load_totals_apply_simultaneity_and_safety_factors():
    """两房间求和后再乘同时使用系数与安全系数。

    r1 = 9.624 kW / 6.713 kW（见上），r2 面积与人数各半：
    人员 0.6 + 照明 0.5 + 设备 0.75 + 新风显热 0.4523 + 新风潜热 1.4648
      + 太阳 (√50 × 3 × 0.25 = 5.3033 m²) 0.4084 + 围护 0.756 = 4.9314 kW
    热负荷 = 1.2 × 70 × 25 / 1000 + 1.005 × 0.05 × 25 = 2.1 + 1.2563 = 3.3563 kW
    总冷 = 9.624 + 4.931 = 14.555 kW，×0.9×1.1 = 14.409 kW
    总热 = 6.713 + 3.356 = 10.069 kW，×0.9×1.1 = 9.968 kW
    """
    result = calculate_load(
        LoadRequest(
            rooms=[
                RoomInput(name="r1", area_m2=100, people=10),
                RoomInput(name="r2", area_m2=50, people=5),
            ]
        )
    )

    assert len(result.rooms) == 2
    assert result.rooms[1].cooling_kw == KW(4.9314, abs=1e-3)
    assert result.rooms[1].heating_kw == KW(3.3563, abs=1e-3)
    assert result.total_cooling_kw == KW(14.555, abs=1e-3)
    assert result.total_heating_kw == KW(10.069, abs=1e-3)
    assert result.adjusted_cooling_kw == KW(14.409, abs=1e-3)
    assert result.adjusted_heating_kw == KW(9.968, abs=1e-3)


def test_water_flow_and_pressure_increase_with_load():
    small = calculate_water(WaterRequest(load_kw=50, length_m=20))
    large = calculate_water(WaterRequest(load_kw=200, length_m=20))

    assert large.flow_m3_h > small.flow_m3_h
    assert large.selected_pipe["dn"] >= small.selected_pipe["dn"]
    assert large.pressure_drop_pa > 0
    assert large.pump_head_m > 0


def test_air_selects_standard_duct():
    result = calculate_air(AirRequest(cooling_load_kw=80))

    assert result.airflow_m3_h > 0
    assert result.selected_duct["type"] == "rectangular"
    assert result.actual_velocity_m_s > 0

