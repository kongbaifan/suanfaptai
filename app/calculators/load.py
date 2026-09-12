"""设计负荷计算

口径：**分项法为主，面积指标降为校核**。总冷负荷 = 人员 + 照明 + 设备 +
新风显热 + 新风潜热 + 太阳辐射 + 围护传热；面积指标只作为 `index_estimate_kw`
并列输出，配 `index_deviation_pct` 给出两种方法的偏差。

指标法与分项法是**互相替代**的两种估算，不能相加：面积指标本身已经包含
人员、照明、设备、新风，再把这些分项加上去就是重复计量。
"""

import math
from collections import defaultdict

from app.building import normalized_floors, room_floor_id
from app.data.design_defaults import ORIENTATION_SOLAR_W_M2, PURPOSE_DEFAULTS
from app.schemas import FloorLoadResult, Issue, LoadRequest, LoadResult, RoomLoadResult
from app.simcore.psychrometrics import VAPORIZATION_HEAT_KJ_KG, humidity_ratio_from_rh


AIR_DENSITY_KG_M3 = 1.2
AIR_CP_KJ_KG_K = 1.005
# 玻璃遮阳系数与得热修正的合并值（教学口径）
SOLAR_GAIN_FACTOR = 0.55


def _round(value: float) -> float:
    return round(value, 3)


def _deviation_pct(itemized_kw: float, index_kw: float) -> float:
    """分项法相对指标法的偏差百分比。指标为 0（面积或指标缺失）时无从比较，返回 0。"""
    if index_kw <= 0:
        return 0.0
    return (itemized_kw - index_kw) / index_kw * 100.0


def calculate_load(request: LoadRequest) -> LoadResult:
    issues: list[Issue] = []
    room_results: list[RoomLoadResult] = []
    floor_meta = {floor.id: floor for floor in normalized_floors(request)}
    floor_stats: defaultdict[str, dict[str, float]] = defaultdict(
        lambda: {
            "area_m2": 0.0,
            "people": 0.0,
            "room_count": 0.0,
            "cooling_kw": 0.0,
            "heating_kw": 0.0,
        }
    )

    for room in request.rooms:
        floor_id = room_floor_id(room)
        defaults = PURPOSE_DEFAULTS.get(room.purpose, {})
        cooling_index_w_m2 = defaults.get("cooling_index_w_m2", room.cooling_index_w_m2)
        heating_index_w_m2 = defaults.get("heating_index_w_m2", room.heating_index_w_m2)
        fresh_air_m3_h_person = defaults.get("fresh_air_m3_h_person", room.fresh_air_m3_h_person)
        lighting_w_m2 = defaults.get("lighting_w_m2", room.lighting_w_m2)
        equipment_w_m2 = defaults.get("equipment_w_m2", room.equipment_w_m2)
        summer_delta = max(room.outdoor_summer_c - room.indoor_summer_c, 0.0)
        winter_delta = max(room.indoor_winter_c - room.outdoor_winter_c, 0.0)
        fresh_air_m3_h = room.people * fresh_air_m3_h_person

        index_cooling_kw = room.area_m2 * cooling_index_w_m2 / 1000.0
        people_cooling_kw = room.people * 0.12
        lighting_cooling_kw = room.area_m2 * lighting_w_m2 / 1000.0
        equipment_cooling_kw = room.area_m2 * equipment_w_m2 / 1000.0
        fresh_air_mass_kg_s = AIR_DENSITY_KG_M3 * fresh_air_m3_h / 3600.0
        fresh_air_cooling_kw = AIR_CP_KJ_KG_K * fresh_air_mass_kg_s * summer_delta
        # 新风潜热按含湿量差算，不能按相对湿度差算：相对湿度是含湿量与当前温度
        # 饱和值的比，35℃ 的 65% 与 26℃ 的 55% 含湿量差了一倍，直接相减没有物理含义。
        humidity_ratio_delta = max(
            humidity_ratio_from_rh(room.outdoor_summer_c, room.outdoor_relative_humidity * 100.0)
            - humidity_ratio_from_rh(room.indoor_summer_c, room.indoor_relative_humidity * 100.0),
            0.0,
        )
        latent_cooling_kw = fresh_air_mass_kg_s * humidity_ratio_delta * VAPORIZATION_HEAT_KJ_KG
        # 窗墙比是**外墙**面积的比例，不是地面面积的比例。房间按方形足迹估算：
        # 迎风外墙宽 √A、高 h，朝向字段只给一个方位，故只计一面外墙。
        external_wall_area_m2 = math.sqrt(room.area_m2) * room.height_m
        window_area_m2 = external_wall_area_m2 * room.window_wall_ratio
        solar_cooling_kw = (
            window_area_m2 * ORIENTATION_SOLAR_W_M2.get(room.orientation, 100.0) * SOLAR_GAIN_FACTOR / 1000.0
        )

        exposed_area_m2 = room.area_m2 * room.exposed_area_factor
        envelope_cooling_kw = room.envelope_u_w_m2k * exposed_area_m2 * summer_delta / 1000.0
        envelope_heating_kw = room.envelope_u_w_m2k * exposed_area_m2 * winter_delta / 1000.0
        fresh_air_heating_kw = AIR_CP_KJ_KG_K * fresh_air_mass_kg_s * winter_delta
        index_heating_kw = room.area_m2 * heating_index_w_m2 / 1000.0

        cooling_kw = (
            people_cooling_kw
            + lighting_cooling_kw
            + equipment_cooling_kw
            + fresh_air_cooling_kw
            + latent_cooling_kw
            + solar_cooling_kw
            + envelope_cooling_kw
        )
        # 供暖同样以分项为主：原来的 max() 会让面积指标反过来盖住分项结果。
        heating_kw = envelope_heating_kw + fresh_air_heating_kw

        if room.area_m2 > 0 and cooling_kw / room.area_m2 > 0.35:
            issues.append(
                Issue(
                    level="warning",
                    code="HIGH_COOLING_LOAD_DENSITY",
                    message=f"{room.name} cooling load density is high.",
                    target=room.name,
                )
            )

        room_results.append(
            RoomLoadResult(
                id=room.id,
                floor_id=floor_id,
                name=room.name,
                cooling_kw=_round(cooling_kw),
                heating_kw=_round(heating_kw),
                fresh_air_m3_h=_round(fresh_air_m3_h),
                index_estimate_kw=_round(index_cooling_kw),
                index_deviation_pct=_round(_deviation_pct(cooling_kw, index_cooling_kw)),
                heating_index_estimate_kw=_round(index_heating_kw),
                heating_index_deviation_pct=_round(_deviation_pct(heating_kw, index_heating_kw)),
                # 各分项之和恒等于 cooling_kw（供暖两项单列）。面积指标不在其中——
                # 它是校核值，走 index_estimate_kw。
                components_kw={
                    "people": _round(people_cooling_kw),
                    "lighting": _round(lighting_cooling_kw),
                    "equipment": _round(equipment_cooling_kw),
                    "fresh_air_cooling": _round(fresh_air_cooling_kw),
                    "latent_cooling": _round(latent_cooling_kw),
                    "solar": _round(solar_cooling_kw),
                    "envelope_cooling": _round(envelope_cooling_kw),
                    "envelope_heating": _round(envelope_heating_kw),
                    "fresh_air_heating": _round(fresh_air_heating_kw),
                },
            )
        )
        floor_stats[floor_id]["area_m2"] += room.area_m2
        floor_stats[floor_id]["people"] += room.people
        floor_stats[floor_id]["room_count"] += 1
        floor_stats[floor_id]["cooling_kw"] += _round(cooling_kw)
        floor_stats[floor_id]["heating_kw"] += _round(heating_kw)

    total_cooling_kw = sum(room.cooling_kw for room in room_results)
    total_heating_kw = sum(room.heating_kw for room in room_results)
    floor_loads = _floor_load_results(floor_meta, floor_stats, total_cooling_kw, request.simultaneity_factor, request.safety_factor)

    return LoadResult(
        total_cooling_kw=_round(total_cooling_kw),
        total_heating_kw=_round(total_heating_kw),
        adjusted_cooling_kw=_round(total_cooling_kw * request.simultaneity_factor * request.safety_factor),
        adjusted_heating_kw=_round(total_heating_kw * request.simultaneity_factor * request.safety_factor),
        rooms=room_results,
        floor_loads=floor_loads,
        issues=issues,
    )


def _floor_load_results(floor_meta, floor_stats, total_cooling_kw, simultaneity_factor, safety_factor) -> list[FloorLoadResult]:
    results: list[FloorLoadResult] = []
    for floor_id, floor in floor_meta.items():
        stats = floor_stats[floor_id]
        cooling_kw = _round(stats["cooling_kw"])
        heating_kw = _round(stats["heating_kw"])
        results.append(
            FloorLoadResult(
                floor_id=floor_id,
                floor_name=floor.name or floor_id,
                level=floor.level,
                area_m2=_round(stats["area_m2"]),
                people=int(stats["people"]),
                room_count=int(stats["room_count"]),
                cooling_kw=cooling_kw,
                heating_kw=heating_kw,
                adjusted_cooling_kw=_round(cooling_kw * simultaneity_factor * safety_factor),
                adjusted_heating_kw=_round(heating_kw * simultaneity_factor * safety_factor),
                cooling_share=_round(cooling_kw / total_cooling_kw if total_cooling_kw > 0 else 0.0),
            )
        )
    for floor_id, stats in floor_stats.items():
        if floor_id in floor_meta:
            continue
        cooling_kw = _round(stats["cooling_kw"])
        heating_kw = _round(stats["heating_kw"])
        results.append(
            FloorLoadResult(
                floor_id=floor_id,
                floor_name=floor_id,
                level=0,
                area_m2=_round(stats["area_m2"]),
                people=int(stats["people"]),
                room_count=int(stats["room_count"]),
                cooling_kw=cooling_kw,
                heating_kw=heating_kw,
                adjusted_cooling_kw=_round(cooling_kw * simultaneity_factor * safety_factor),
                adjusted_heating_kw=_round(heating_kw * simultaneity_factor * safety_factor),
                cooling_share=_round(cooling_kw / total_cooling_kw if total_cooling_kw > 0 else 0.0),
            )
        )
    return sorted(results, key=lambda item: (item.level, item.floor_id))
