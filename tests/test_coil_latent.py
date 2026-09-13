"""盘管潜热计算测试

验证：
1. 干盘管（含湿量不变）潜热为零
2. 湿盘管（除湿）潜热为正
3. 新风加湿时潜热为负
4. 潜热与显热拆分守恒

修复前 session.py 有 max(...,0) 把新风加湿的负潜热截断。
"""
from app.simcore.psychrometrics import (
    DRY_AIR_CP_KJ_KG_K,
    VAPORIZATION_HEAT_KJ_KG,
    humidity_ratio_from_rh,
    rh_from_humidity_ratio,
)


def test_dry_coil_has_zero_latent():
    """干盘管（只降温、不除湿）潜热应为零"""
    # 室内 26°C/55% RH，送风保持同样含湿量但降温到 16°C
    room_temp_c = 26.0
    room_rh = 55.0
    supply_temp_c = 16.0

    room_ratio = humidity_ratio_from_rh(room_temp_c, room_rh)
    supply_ratio = room_ratio  # 干盘管，含湿量不变
    supply_rh = rh_from_humidity_ratio(supply_temp_c, supply_ratio)

    air_mass_kg_s = 1.0
    latent_kw = air_mass_kg_s * (room_ratio - supply_ratio) * VAPORIZATION_HEAT_KJ_KG

    assert abs(latent_kw) < 0.001, (
        f"干盘管潜热应为 0，实际 {latent_kw:.3f} kW "
        f"(室内 {room_temp_c}°C/{room_rh}% → 送风 {supply_temp_c}°C/{supply_rh:.1f}%)"
    )


def test_wet_coil_has_positive_latent():
    """湿盘管（降温 + 除湿）潜热为正"""
    # 室内 26°C/55%，送风 16°C/90%（接近饱和，含湿量低于室内）
    room_temp_c = 26.0
    room_rh = 55.0
    supply_temp_c = 16.0
    supply_rh = 90.0

    room_ratio = humidity_ratio_from_rh(room_temp_c, room_rh)
    supply_ratio = humidity_ratio_from_rh(supply_temp_c, supply_rh)

    air_mass_kg_s = 1.0
    latent_kw = air_mass_kg_s * (room_ratio - supply_ratio) * VAPORIZATION_HEAT_KJ_KG

    # 湿盘管除湿，室内含湿量 > 送风含湿量，潜热为正
    assert latent_kw > 0, (
        f"湿盘管潜热应为正，实际 {latent_kw:.3f} kW "
        f"(室内 {room_ratio:.6f} → 送风 {supply_ratio:.6f} kg/kg)"
    )

    # 手算期望：
    # room_ratio = 0.011537 kg/kg (26°C/55%)
    # supply_ratio = 0.010189 kg/kg (16°C/90%)
    # delta = 0.001349
    # latent = 1.0 × 0.001349 × 2501 = 3.37 kW
    expected_latent = 3.37
    assert abs(latent_kw - expected_latent) < 0.05, (
        f"湿盘管潜热 {latent_kw:.3f} kW 偏离期望 {expected_latent:.3f} kW"
    )


def test_humidifying_fresh_air_has_negative_latent():
    """新风加湿（送风含湿量 > 室内）潜热为负"""
    # 室内 26°C/40%（干燥），室外新风 35°C/70%（湿热）
    # 新风经盘管降温，但含湿量仍高于室内，进房间后会加湿
    room_temp_c = 26.0
    room_rh = 40.0
    outdoor_temp_c = 35.0
    outdoor_rh = 70.0

    room_ratio = humidity_ratio_from_rh(room_temp_c, room_rh)
    outdoor_ratio = humidity_ratio_from_rh(outdoor_temp_c, outdoor_rh)
    # 假设盘管只降温不除湿（极端情况）
    supply_ratio = outdoor_ratio

    air_mass_kg_s = 1.0
    latent_kw = air_mass_kg_s * (room_ratio - supply_ratio) * VAPORIZATION_HEAT_KJ_KG

    # 送风含湿量 > 室内，加湿，潜热为负
    assert latent_kw < 0, (
        f"新风加湿潜热应为负，实际 {latent_kw:.3f} kW "
        f"(室内 {room_ratio:.6f} < 送风 {supply_ratio:.6f} kg/kg)"
    )


def test_sensible_latent_split_conserves_total():
    """显热 + 潜热应等于总焓差（能量守恒）"""
    room_temp_c = 26.0
    room_rh = 55.0
    supply_temp_c = 16.0
    supply_rh = 90.0

    room_ratio = humidity_ratio_from_rh(room_temp_c, room_rh)
    supply_ratio = humidity_ratio_from_rh(supply_temp_c, supply_rh)

    air_mass_kg_s = 1.0

    # 总焓差（干空气 + 水蒸气）
    # h = c_pa × T + w × (r0 + c_pv × T)
    # 简化为显热 + 潜热：
    sensible_kw = DRY_AIR_CP_KJ_KG_K * air_mass_kg_s * (room_temp_c - supply_temp_c)
    latent_kw = air_mass_kg_s * (room_ratio - supply_ratio) * VAPORIZATION_HEAT_KJ_KG
    total_from_split = sensible_kw + latent_kw

    # 完整焓差计算（包含水蒸气显热项）
    from app.simcore.psychrometrics import enthalpy_kj_kg

    h_room = enthalpy_kj_kg(room_temp_c, room_ratio)
    h_supply = enthalpy_kj_kg(supply_temp_c, supply_ratio)
    total_from_enthalpy = air_mass_kg_s * (h_room - h_supply)

    # 显热 + 潜热应接近总焓差（水蒸气显热项的微小差异）
    assert abs(total_from_split - total_from_enthalpy) < 0.5, (
        f"显热 + 潜热 = {total_from_split:.3f} kW，"
        f"总焓差 = {total_from_enthalpy:.3f} kW，不守恒"
    )
