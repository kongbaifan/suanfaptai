PURPOSE_DEFAULTS = {
    "office": {"cooling_index_w_m2": 115.0, "heating_index_w_m2": 75.0, "fresh_air_m3_h_person": 30.0, "lighting_w_m2": 10.0, "equipment_w_m2": 15.0},
    "meeting": {"cooling_index_w_m2": 150.0, "heating_index_w_m2": 85.0, "fresh_air_m3_h_person": 35.0, "lighting_w_m2": 12.0, "equipment_w_m2": 10.0},
    "mall": {"cooling_index_w_m2": 180.0, "heating_index_w_m2": 90.0, "fresh_air_m3_h_person": 25.0, "lighting_w_m2": 18.0, "equipment_w_m2": 20.0},
    "hotel": {"cooling_index_w_m2": 125.0, "heating_index_w_m2": 80.0, "fresh_air_m3_h_person": 30.0, "lighting_w_m2": 8.0, "equipment_w_m2": 10.0},
    "classroom": {"cooling_index_w_m2": 140.0, "heating_index_w_m2": 85.0, "fresh_air_m3_h_person": 30.0, "lighting_w_m2": 11.0, "equipment_w_m2": 8.0},
    # 商场类细分：超市人流与冷柜设备密度高、餐饮有明火与排风、
    # 中庭是高大玻璃共享空间（太阳辐射与照明主导）、商铺层高低人员密集。
    # 指标取 GB 50189 / 暖通设计手册的工程估算区间。
    "supermarket": {"cooling_index_w_m2": 200.0, "heating_index_w_m2": 95.0, "fresh_air_m3_h_person": 20.0, "lighting_w_m2": 20.0, "equipment_w_m2": 30.0},
    "restaurant": {"cooling_index_w_m2": 190.0, "heating_index_w_m2": 90.0, "fresh_air_m3_h_person": 25.0, "lighting_w_m2": 14.0, "equipment_w_m2": 25.0},
    "atrium": {"cooling_index_w_m2": 160.0, "heating_index_w_m2": 110.0, "fresh_air_m3_h_person": 15.0, "lighting_w_m2": 25.0, "equipment_w_m2": 5.0},
    "shop": {"cooling_index_w_m2": 175.0, "heating_index_w_m2": 88.0, "fresh_air_m3_h_person": 30.0, "lighting_w_m2": 22.0, "equipment_w_m2": 12.0},
}

ORIENTATION_SOLAR_W_M2 = {
    "north": 60.0,
    "south": 140.0,
    "east": 110.0,
    "west": 160.0,
    "internal": 0.0,
}

