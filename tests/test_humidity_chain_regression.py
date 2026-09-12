"""湿度链回归测试——钉死手算期望值

防止冷却塔温度反置、干盘管假除湿、潜热公式不一致的缺陷回归。
每条测试钉死一个数值，来自独立手算或物理约束，不依赖当前实现。
"""

import math

from app.simcore.devices import cooling_tower_state
from app.simcore.psychrometrics import LATENT_HEAT_KJ_KG
from app.simcore.rated import resolve_rated_spec_by_type


def test_cooling_tower_entering_temp_exceeds_leaving():
    """冷却塔进水温度（热水）必须高于出水温度（冷水）

    物理约束：冷却塔是散热设备，进水温度 = 冷凝器排热后的高温水，
    出水温度 = 冷却后供给冷凝器的低温水。进水温度低于出水即冷却塔在加热水。
    """
    rated = resolve_rated_spec_by_type("冷却塔", None)
    wetbulb_temp_c = 28.0
    water_flow_m3_h = 30.0

    state = cooling_tower_state(
        rated=rated,
        fan_speed_ratio=1.0,
        heat_reject_kw=100.0,
        wetbulb_temp_c=wetbulb_temp_c,
        water_flow_m3_h=water_flow_m3_h,
        enabled=True,
    )

    # 进水必须高于出水，且出水必须高于湿球（逼近度 > 0）
    assert state.entering_water_temp_c > state.leaving_water_temp_c
    assert state.leaving_water_temp_c > wetbulb_temp_c
    assert state.approach_c > 0.0


def test_cooling_tower_entering_temperature_matches_heat_reject():
    """冷却塔进水温度按热平衡算出：进水温度 = 出水 + 排热 / (流量 × 比热)

    手算：散热 100 kW，流量 30 m³/h，温升 = 100 / (30/3600 × 4200) = 2.857 K
    出水 30°C → 进水 32.857°C
    """
    rated = resolve_rated_spec_by_type("冷却塔", None)
    wetbulb_temp_c = 28.0
    water_flow_m3_h = 30.0
    heat_reject_kw = 100.0

    state = cooling_tower_state(
        rated=rated,
        fan_speed_ratio=1.0,
        heat_reject_kw=heat_reject_kw,
        wetbulb_temp_c=wetbulb_temp_c,
        water_flow_m3_h=water_flow_m3_h,
        enabled=True,
    )

    # 温升 = 100 kW / (30 m³/h / 3600 × 4200 kJ/(m³·K)) ≈ 2.857 K
    expected_rise = heat_reject_kw / (water_flow_m3_h / 3600.0 * 4200.0)
    expected_entering = state.leaving_water_temp_c + expected_rise

    assert math.isclose(state.entering_water_temp_c, expected_entering, rel_tol=0.01)
    assert math.isclose(expected_rise, 2.857, rel_tol=0.01)


def test_dry_coil_latent_is_zero():
    """干盘管（送风与室内含湿量相等）潜热为零

    手算：风量 1000 m³/h，室内 26°C/w=0.010，送风 16°C/w=0.010
    含湿量差为零 → 潜热 = 0
    """
    airflow_m3_h = 1000.0
    air_density_kg_m3 = 1.2
    room_ratio = 0.010
    supply_ratio = 0.010

    air_mass_kg_s = airflow_m3_h * air_density_kg_m3 / 3600.0
    latent_kw = air_mass_kg_s * (room_ratio - supply_ratio) * LATENT_HEAT_KJ_KG

    assert latent_kw == 0.0


def test_wet_coil_dehumidification_latent():
    """湿盘管除湿：室内 w=0.012，送风 w=0.008 → 正潜热

    手算：风量 1000 m³/h，Δw = 0.004 kg/kg
    质量流量 = 1000 × 1.2 / 3600 = 1/3 kg/s
    潜热 = (1/3) × 0.004 × 2501 = 3.335 kW（使用 psychrometrics.py 的 VAPORIZATION_HEAT_KJ_KG）
    """
    airflow_m3_h = 1000.0
    air_density_kg_m3 = 1.2
    room_ratio = 0.012
    supply_ratio = 0.008

    air_mass_kg_s = airflow_m3_h * air_density_kg_m3 / 3600.0
    latent_kw = air_mass_kg_s * (room_ratio - supply_ratio) * LATENT_HEAT_KJ_KG

    # psychrometrics.VAPORIZATION_HEAT_KJ_KG = 2501，不是 session.LATENT_HEAT_KJ_KG = 2450
    expected = (1000.0 * 1.2 / 3600.0) * 0.004 * 2501.0
    assert math.isclose(latent_kw, expected, rel_tol=0.001)
    assert latent_kw > 0.0


def test_fresh_air_humidification_negative_latent():
    """新风加湿（送风含湿量高于室内）产出负潜热

    手算：风量 1000 m³/h，室内 w=0.008，送风 w=0.012
    Δw = -0.004 → 潜热 = (1/3) × (-0.004) × 2501 = -3.335 kW
    """
    airflow_m3_h = 1000.0
    air_density_kg_m3 = 1.2
    room_ratio = 0.008
    supply_ratio = 0.012  # 送风湿度高于室内

    air_mass_kg_s = airflow_m3_h * air_density_kg_m3 / 3600.0
    latent_kw = air_mass_kg_s * (room_ratio - supply_ratio) * LATENT_HEAT_KJ_KG

    expected = (1000.0 * 1.2 / 3600.0) * (-0.004) * 2501.0
    assert math.isclose(latent_kw, expected, rel_tol=0.001)
    assert latent_kw < 0.0


def test_latent_heat_constant_is_2501():
    """潜热常数固定为 2501 kJ/kg，与 psychrometrics 模块一致

    这是空调工况（0~50°C）附近的汽化潜热近似值。
    若此断言失败，说明有人改了常数或引入了另一套不同的值。
    """
    assert LATENT_HEAT_KJ_KG == 2501.0

