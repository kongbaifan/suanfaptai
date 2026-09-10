from collections import defaultdict

from app.building import normalized_floors, room_floor_id
from app.data.design_defaults import ORIENTATION_SOLAR_W_M2, PURPOSE_DEFAULTS
from app.schemas import FloorLoadResult, Issue, LoadRequest, LoadResult, RoomLoadResult


AIR_DENSITY_KG_M3 = 1.2
AIR_CP_KJ_KG_K = 1.005
AIR_LATENT_KW_PER_M3H_RH_DELTA = 0.00072


def _round(value: float) -> float:
    return round(value, 3)


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
        fresh_air_cooling_kw = AIR_DENSITY_KG_M3 * AIR_CP_KJ_KG_K * fresh_air_m3_h * summer_delta / 3600.0
        latent_cooling_kw = max(room.outdoor_relative_humidity - room.indoor_relative_humidity, 0.0) * fresh_air_m3_h * AIR_LATENT_KW_PER_M3H_RH_DELTA
        window_area_m2 = room.area_m2 * room.exposed_area_factor * room.window_wall_ratio
        solar_cooling_kw = window_area_m2 * ORIENTATION_SOLAR_W_M2.get(room.orientation, 100.0) * 0.55 / 1000.0

        exposed_area_m2 = room.area_m2 * room.exposed_area_factor
        envelope_heating_kw = room.envelope_u_w_m2k * exposed_area_m2 * winter_delta / 1000.0
        fresh_air_heating_kw = AIR_DENSITY_KG_M3 * AIR_CP_KJ_KG_K * fresh_air_m3_h * winter_delta / 3600.0
        index_heating_kw = room.area_m2 * heating_index_w_m2 / 1000.0

        cooling_kw = (
            index_cooling_kw
            + people_cooling_kw
            + lighting_cooling_kw
            + equipment_cooling_kw
            + fresh_air_cooling_kw
            + latent_cooling_kw
            + solar_cooling_kw
        )
        heating_kw = max(index_heating_kw, envelope_heating_kw + fresh_air_heating_kw)

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
                components_kw={
                    "area_index": _round(index_cooling_kw),
                    "people": _round(people_cooling_kw),
                    "lighting": _round(lighting_cooling_kw),
                    "equipment": _round(equipment_cooling_kw),
                    "fresh_air_cooling": _round(fresh_air_cooling_kw),
                    "latent_cooling": _round(latent_cooling_kw),
                    "solar": _round(solar_cooling_kw),
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
