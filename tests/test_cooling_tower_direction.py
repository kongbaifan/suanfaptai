"""冷却塔进出水温方向测试

验证冷却塔的进出水温关系：冷却水应该是「进热出冷」，离塔温度 < 入塔温度。
修复前 devices.py:382-389 写成 `leaving = entering + rise`，变成加热器。
"""
from app.simcore.devices import cooling_tower_state
from app.simcore.rated import RatedSpec
from app.unity_catalog import DeviceRole


def test_cooling_tower_leaving_is_cooler_than_entering():
    """冷却塔出水温度应低于入水温度"""
    rated = RatedSpec(
        device_type="冷却塔",
        model_name="test-ct",
        role=DeviceRole.COOLING_TOWER,
        flow_m3_h=100.0,
        approach_c=4.0,
        fan_power_kw=5.0,
    )

    # 假设冷却塔散热 500 kW，流量 100 m³/h
    # 温升 = 500 / (1.163 × 100) ≈ 4.3 K
    heat_reject_kw = 500.0
    wetbulb_c = 28.0

    result = cooling_tower_state(
        rated=rated,
        fan_speed_ratio=1.0,
        heat_reject_kw=heat_reject_kw,
        wetbulb_temp_c=wetbulb_c,
        water_flow_m3_h=100.0,
    )

    # 冷却塔是散热设备，出水必须比入水冷
    assert result.leaving_water_temp_c < result.entering_water_temp_c, (
        f"冷却塔出水 {result.leaving_water_temp_c:.2f}°C "
        f">= 入水 {result.entering_water_temp_c:.2f}°C，方向反了"
    )

    # 手算期望：leaving = wetbulb + approach = 28 + 4 = 32°C
    expected_leaving = wetbulb_c + rated.approach_c
    assert abs(result.leaving_water_temp_c - expected_leaving) < 0.1, (
        f"出水温度 {result.leaving_water_temp_c:.2f}°C "
        f"偏离期望 {expected_leaving:.2f}°C"
    )

    # 温升 ≈ 500 / (1.163 × 100) ≈ 4.3 K
    # entering = leaving + rise ≈ 32 + 4.3 = 36.3°C
    expected_rise = heat_reject_kw / (1.163 * 100.0)
    expected_entering = expected_leaving + expected_rise
    assert abs(result.entering_water_temp_c - expected_entering) < 0.2, (
        f"入水温度 {result.entering_water_temp_c:.2f}°C "
        f"偏离期望 {expected_entering:.2f}°C"
    )


def test_cooling_tower_power_scales_with_fan_speed():
    """冷却塔功率应随风机转速三次方变化（风机调速）"""
    rated = RatedSpec(
        device_type="冷却塔",
        model_name="test-ct",
        role=DeviceRole.COOLING_TOWER,
        flow_m3_h=100.0,
        approach_c=4.0,
        fan_power_kw=5.0,
    )

    full_power = cooling_tower_state(
        rated=rated,
        fan_speed_ratio=1.0,
        heat_reject_kw=500.0,
        wetbulb_temp_c=28.0,
    ).fan_power_kw

    half_power = cooling_tower_state(
        rated=rated,
        fan_speed_ratio=0.5,
        heat_reject_kw=500.0,
        wetbulb_temp_c=28.0,
    ).fan_power_kw

    # 风机功率 ∝ 转速³
    # 0.5 转速 → 0.5³ = 0.125 功率
    expected_half = full_power * (0.5 ** 3)
    assert abs(half_power - expected_half) < 0.01, (
        f"半转速功率 {half_power:.3f} kW "
        f"偏离期望 {expected_half:.3f} kW"
    )


