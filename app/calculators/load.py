"""设计负荷计算

口径：**分项法为主，面积指标降为校核**。总冷负荷 = 人员 + 照明 + 设备 +
新风显热 + 新风潜热 + 太阳辐射 + 围护传热；面积指标只作为 `index_estimate_kw`
并列输出，配 `index_deviation_pct` 给出两种方法的偏差。

指标法与分项法是**互相替代**的两种估算，不能相加：面积指标本身已经包含
人员、照明、设备、新风，再把这些分项加上去就是重复计量。
"""

import math
from collections import defaultdict

from app.building import implicit_floor_id, normalized_floors, room_floor_id
from app.data.design_defaults import ORIENTATION_SOLAR_W_M2, PURPOSE_DEFAULTS
from app.schemas import (
    FloorLoadResult,
    Issue,
    LoadFactorBreakdown,
    LoadRequest,
    LoadResult,
    RoomInput,
    RoomLoadBreakdownResult,
    RoomLoadBreakdownRow,
    RoomLoadResult,
)
from app.simcore.psychrometrics import VAPORIZATION_HEAT_KJ_KG, humidity_ratio_from_rh

AIR_DENSITY_KG_M3 = 1.2
AIR_CP_KJ_KG_K = 1.005
# 玻璃遮阳系数与得热修正的合并值（教学口径）
SOLAR_GAIN_FACTOR = 0.55
# 逐房间冷负荷表的列顺序：人员 / 照明 / 设备 / 太阳辐射 / 围护 / 新风显热 / 新风潜热。
# 与 `components_kw` 的插入顺序不同，故显式声明；七项之和恒等于 `cooling_kw`。
COOLING_COMPONENT_KEYS = (
    "people",
    "lighting",
    "equipment",
    "solar",
    "envelope_cooling",
    "fresh_air_cooling",
    "latent_cooling",
)


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
        floor_id = room_floor_id(room, implicit=implicit_floor_id(request))
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


def _load_index_w_m2(cooling_kw: float, area_m2: float | None) -> float | None:
    """单位面积冷负荷指标，W/m²。面积缺失或为 0 时没有意义，返回 None。"""
    if not area_m2 or area_m2 <= 0:
        return None
    return round(cooling_kw / area_m2 * 1000.0, 1)


def build_room_load_breakdown(request: LoadRequest) -> RoomLoadBreakdownResult:
    """把分项法结果摊平成"逐房间 → 楼层小计 → 全楼合计 → 修正后"的表格。

    不重算任何公式：房间行的分项直接取自 `calculate_load` 的 `components_kw`，
    小计行是各房间**已四舍五入的分项**之和（与 `_floor_load_results` 同口径），
    所以表面上的数字可以直接相加核对，不会出现"表里加起来对不上合计"。

    行序：楼层按 `level → id` 排序，房间沿用请求顺序，每个楼层后面紧跟它的小计，
    最后是全楼合计与修正后（逐分项乘 同时使用系数 × 安全系数）。
    """
    result = calculate_load(request)
    floor_meta = {floor.id: floor for floor in normalized_floors(request)}
    combined_factor = request.simultaneity_factor * request.safety_factor

    # 房间负荷与房间输入一一对应（calculate_load 按请求顺序输出），用序号兜底生成稳定 key。
    by_floor: defaultdict[str, list[tuple[str, RoomInput, RoomLoadResult]]] = defaultdict(list)
    for position, room_load in enumerate(result.rooms):
        source = request.rooms[position]
        by_floor[room_load.floor_id].append(
            (room_load.id or f"room_{position}", source, room_load)
        )

    floor_order = [
        floor.id for floor in sorted(floor_meta.values(), key=lambda floor: (floor.level, floor.id))
    ]
    floor_order.extend(floor_id for floor_id in by_floor if floor_id not in floor_meta)

    rows: list[RoomLoadBreakdownRow] = []
    totals = {key: 0.0 for key in COOLING_COMPONENT_KEYS}
    total_area_m2 = 0.0
    total_people = 0
    total_cooling_kw = 0.0

    for floor_id in floor_order:
        entries = by_floor.get(floor_id, [])
        if not entries:
            continue
        floor = floor_meta.get(floor_id)
        floor_name = (floor.name or floor_id) if floor is not None else floor_id
        subtotal = {key: 0.0 for key in COOLING_COMPONENT_KEYS}
        floor_area_m2 = 0.0
        floor_people = 0
        floor_cooling_kw = 0.0

        for key, source, room_load in entries:
            components = {
                component: room_load.components_kw.get(component, 0.0)
                for component in COOLING_COMPONENT_KEYS
            }
            rows.append(
                RoomLoadBreakdownRow(
                    row_type="room",
                    key=key,
                    name=room_load.name,
                    floor_id=floor_id,
                    floor_name=floor_name,
                    area_m2=source.area_m2,
                    people=source.people,
                    components_kw=components,
                    cooling_kw=room_load.cooling_kw,
                    cooling_index_w_m2=_load_index_w_m2(room_load.cooling_kw, source.area_m2),
                    adjusted_cooling_kw=_round(room_load.cooling_kw * combined_factor),
                    fresh_air_m3_h=room_load.fresh_air_m3_h,
                    index_estimate_kw=room_load.index_estimate_kw,
                    index_deviation_pct=room_load.index_deviation_pct,
                )
            )
            for component in COOLING_COMPONENT_KEYS:
                subtotal[component] += components[component]
            floor_area_m2 += source.area_m2
            floor_people += source.people
            floor_cooling_kw += room_load.cooling_kw

        floor_area = _round(floor_area_m2)
        floor_cooling = _round(floor_cooling_kw)
        rows.append(
            RoomLoadBreakdownRow(
                row_type="floor_subtotal",
                key=floor_id,
                name=f"{floor_name}小计",
                floor_id=floor_id,
                floor_name=floor_name,
                area_m2=floor_area,
                people=floor_people,
                components_kw={
                    component: _round(subtotal[component]) for component in COOLING_COMPONENT_KEYS
                },
                cooling_kw=floor_cooling,
                # 指标按**行里显示的**合计与面积算，客户端自己除一遍能得到同一个数。
                cooling_index_w_m2=_load_index_w_m2(floor_cooling, floor_area),
                adjusted_cooling_kw=_round(floor_cooling_kw * combined_factor),
            )
        )
        for component in COOLING_COMPONENT_KEYS:
            totals[component] += subtotal[component]
        total_area_m2 += floor_area_m2
        total_people += floor_people
        total_cooling_kw += floor_cooling_kw

    total_area = _round(total_area_m2)
    total_cooling = _round(total_cooling_kw)
    rows.append(
        RoomLoadBreakdownRow(
            row_type="building_total",
            key="ALL",
            name="全楼合计",
            area_m2=total_area,
            people=total_people,
            components_kw={component: _round(totals[component]) for component in COOLING_COMPONENT_KEYS},
            cooling_kw=total_cooling,
            cooling_index_w_m2=_load_index_w_m2(total_cooling, total_area),
            adjusted_cooling_kw=_round(total_cooling_kw * combined_factor),
        )
    )
    rows.append(
        RoomLoadBreakdownRow(
            row_type="adjusted_total",
            key="ALL_ADJUSTED",
            name=f"修正后（× {combined_factor:.3f}）",
            components_kw={
                component: _round(totals[component] * combined_factor)
                for component in COOLING_COMPONENT_KEYS
            },
            # 修正后行的合计就是修正后口径，不再重复给 adjusted_cooling_kw。
            cooling_kw=_round(total_cooling_kw * combined_factor),
        )
    )

    return RoomLoadBreakdownResult(
        component_keys=list(COOLING_COMPONENT_KEYS),
        factors=LoadFactorBreakdown(
            simultaneity_factor=request.simultaneity_factor,
            safety_factor=request.safety_factor,
            combined_factor=_round(combined_factor),
        ),
        total_area_m2=_round(total_area_m2),
        total_people=total_people,
        rows=rows,
        issues=result.issues,
    )
