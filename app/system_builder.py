"""系统骨架生成：从楼层与房间生成两族制式的完整拓扑

输入是**建筑**（楼层 + 房间 + 用途/面积/人数）与一个**制式声明**，输出是一套
可直接保存、评估、跑的完整项目（设备 + 管路 + 连接 + 房间末端绑定）。
这把"搭建"从"手抄一份样例再改"变成"生成骨架后再深化"。

## 生成粒度：按层聚合，与内置样例同构

内置样例的设备是**按层聚合**的（`{"id":"fcu_f1","type":"风机盘管","quantity":6}`），
房间的 `device_ids` 指向这些聚合设备，仿真再按设计冷负荷权重把机组能力摊到各房间
（`session._terminal_room_weights`）。本模块沿用同一粒度，因此生成结果与手写样例
在数据形态上完全一致，不需要任何新的仿真支持。

## 关键设计决定

1. **管径/风管尺寸与流量一律不写**，交给既有计算器反算。`analyzers` 的约定是
   "已给的尺寸不改，缺的才反算"（见 `docs/接口文档.md`），因此生成器只声明
   **拓扑与走向**，尺寸是计算派生值。这既是"参数有出处"的纪律，也避免生成器
   抄一份标准尺寸表。
2. **管长按建筑数据估算**：立管取所跨楼层的标高跨度，水平管取楼层面积的平方根
   作特征长度。两者都是**派生值**，不是拍出来的常数；实际平面深化时应当替换。
3. **循环泵分两步定选**：泵的扬程需求来自管网阻力，而管网要先有泵才闭合。
   因此先用型号回退的占位泵把水环路接成闭合回路，再按生成出的管网重新定选泵。
   塔与冷却水泵则直接按所选冷机的目录额定值（冷凝排热量、冷却水流量）匹配。
4. **制式决定末端族与新风归属**：
   - `fcu_fresh_air`：风机盘管承担**室内侧**负荷（总负荷扣除新风显热与新风潜热），
     新风机组承担全部新风；风机盘管的送风经送风静压箱与新风汇合后由送风口送出，
     回风经回风管返回风机盘管——与 `FOUR_FLOOR_FCU_FRESH_AIR_SAMPLE` 同构。
   - `all_air`：组合式空调机组承担该分区**全部**负荷（含新风），风管按房间分支，
     每房间一组"送风口 → 回风阀"——与 `MULTI_ZONE_ALL_AIR_SAMPLE` 同构。
5. **确定性**：楼层按 `(level, elevation, id)` 排序，房间按原顺序，检索按 id 排序，
   不使用集合迭代顺序。同一输入必得同一输出。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from itertools import pairwise
from typing import Any

from app.building import normalized_floors
from app.calculators.load import calculate_load
from app.data.equipment import load_equipment_catalog, resolve_device_type
from app.data.rules import rule_value
from app.equipment_selection import derive_requirements, select_equipment
from app.project_validation import validate_project_data
from app.room_bindings import room_instance_id
from app.schemas import (
    AirRequest,
    ConnectionInput,
    DeviceInput,
    FloorInput,
    GenerationDecision,
    Issue,
    LoadRequest,
    LoadResult,
    PipeInput,
    RoomInput,
    SelectionCandidate,
    SelectionRequirements,
    SelectionTargetRole,
    SystemEvaluateRequest,
    SystemForm,
    SystemGenerateRequest,
    SystemGenerateResult,
)
from app.validators.system_form import check_system_form

# 设备类型名（目录 `type` 字段，与前端设备调色板同名）。
_CHILLER_TYPE = "冷源机组"
_PUMP_TYPE = "循环泵"
_TOWER_TYPE = "冷却塔"
_HEAT_SOURCE_TYPE = "空气源热泵"
_FCU_TYPE = "风机盘管"
_AHU_TYPE = "组合式空调机组"
_FAU_TYPE = "新风机组"
_SUPPLY_OUTLET_TYPE = "送风口"
_RETURN_DAMPER_TYPE = "风阀"

# 算新风负荷的两个分项：新风的显热与潜热都由新风处理设备承担，
# 房间侧末端只承担其余分项（见 `calculators/load.py` 的 `components_kw`）。
_FRESH_AIR_COMPONENTS = ("fresh_air_cooling", "latent_cooling")

# 风机静压裕量取泵扬程裕量区间的中值（`rules.json` 的 `pump_margin`）。
# 平台的 `air_network` 用固定 1.05 判合格，取区间中值是为了既稳过阈值
# 又不至于把风机选得过大；内置样例手写的 1.15 正落在该区间中部。
_MARGIN_SOURCE_PREFIX = "pump_margin"


class SystemGenerationUnavailableError(ValueError):
    """建筑数据不足，无法生成骨架。"""

    def __init__(self, missing: list[str]) -> None:
        self.missing = tuple(missing)
        super().__init__("Required building data is unavailable for system generation.")

    @property
    def detail(self) -> dict[str, Any]:
        return {
            "code": "SYSTEM_GENERATION_REQUIREMENTS_UNAVAILABLE",
            "missing": list(self.missing),
            "message": str(self),
        }


@dataclass(slots=True)
class _FloorGroup:
    """一个楼层（或楼层内的一个分区）的负荷与新风汇总。"""

    floor: FloorInput
    zone_id: str
    room_ids: tuple[str, ...]
    room_indexes: tuple[int, ...]
    cooling_kw: float = 0.0
    # 室内侧冷负荷：总冷负荷扣除新风显热与新风潜热。风机盘管只承担这一份，
    # 新风由独立新风机组处理，两份相加才等于总冷负荷。
    terminal_cooling_kw: float = 0.0
    heating_kw: float = 0.0
    fresh_air_m3_h: float = 0.0
    area_m2: float = 0.0
    # 房间 → 设计冷负荷，用于把分区机组的风量按负荷权重摊到各房间
    # （与 `session._terminal_room_weights` 同一口径）。
    room_cooling_kw: dict[str, float] = field(default_factory=dict)


class _Builder:
    """累积生成中的设备、管路、连接与决策记录。"""

    def __init__(self) -> None:
        self.devices: list[DeviceInput] = []
        self.pipes: list[PipeInput] = []
        self.connections: list[ConnectionInput] = []
        self.decisions: list[GenerationDecision] = []
        self.issues: list[Issue] = []
        self.degraded = False

    def device(
        self,
        device_id: str,
        device_type: str,
        *,
        name: str | None = None,
        floor_id: str | None = None,
        location_type: str | None = None,
        quantity: int = 1,
    ) -> str:
        self.devices.append(
            DeviceInput(
                id=device_id,
                type=device_type,
                name=name,
                floor_id=floor_id,
                location_type=location_type,
                quantity=quantity,
                air_connection_mode="ducted" if device_type == _FCU_TYPE else None,
            )
        )
        return device_id

    def pipe(
        self,
        pipe_id: str,
        system: str,
        *,
        pipe_type: str = "unknown",
        floor_id: str | None = None,
        from_floor_id: str | None = None,
        to_floor_id: str | None = None,
        length_m: float = 10.0,
    ) -> str:
        self.pipes.append(
            PipeInput(
                id=pipe_id,
                system=system,  # type: ignore[arg-type]
                pipe_type=pipe_type,  # type: ignore[arg-type]
                floor_id=floor_id,
                from_floor_id=from_floor_id,
                to_floor_id=to_floor_id,
                length_m=length_m,
            )
        )
        return pipe_id

    def connect(self, from_id: str, to_id: str, system: str) -> None:
        self.connections.append(
            ConnectionInput(from_id=from_id, to_id=to_id, system=system)  # type: ignore[arg-type]
        )

    def chain(self, system: str, *node_ids: str) -> None:
        """把一串节点按顺序连成一条通路。"""
        for left, right in pairwise(node_ids):
            self.connect(left, right, system)

    def decide(
        self, target: str, choice: str, reason: str, source: str
    ) -> None:
        self.decisions.append(
            GenerationDecision(
                target=target,
                choice=choice,
                reason=reason,
                source=source,  # type: ignore[arg-type]
            )
        )

    def warn(self, code: str, message: str, target: str | None = None) -> None:
        self.degraded = True
        self.issues.append(
            Issue(level="warning", code=code, message=message, target=target)
        )


def build_system(request: SystemGenerateRequest) -> SystemGenerateResult:
    """从楼层与房间生成一套完整的两族制式系统骨架。"""
    if not request.rooms:
        raise SystemGenerationUnavailableError(["rooms"])
    floors = normalized_floors(request)
    if not floors:
        raise SystemGenerationUnavailableError(["floors"])

    builder = _Builder()
    rooms = _rooms_with_ids(request.rooms)
    if any(room.id != original.id for room, original in zip(rooms, request.rooms, strict=True)):
        builder.decide(
            target="rooms",
            choice="为未命名的房间补齐稳定 id（room-N）",
            reason=(
                "房间 id 是负荷分项、末端绑定与实时读数的关联键。缺 id 时"
                "`RoomLoadResult.id` 为空，无法把分项负荷归到具体房间上。"
            ),
            source="calculated",
        )

    load = calculate_load(
        LoadRequest(
            rooms=rooms,
            floors=floors,
            simultaneity_factor=request.simultaneity_factor,
            safety_factor=request.safety_factor,
        )
    )
    groups = _floor_groups(request.system_form, floors, rooms, load)

    catalog = load_equipment_catalog()
    placement = _plant_placement(floors)
    builder.decide(
        target="机房与屋面",
        choice=(
            f"机房放在 {placement.plant_floor.id}（最低层），"
            f"冷却塔放在 {placement.roof_floor.id}（最高层）"
        ),
        reason="冷机与水泵需要机房，冷却塔需要屋面通风；取楼层标高极值定位。",
        source="calculated",
    )

    chiller = _place_chiller(builder, request, load, rooms, catalog, placement)
    water_links = _build_water_loops(builder, placement, groups)
    heat_links = (
        _build_heating(builder, request, groups, rooms, catalog, placement)
        if request.options.include_heating
        else {}
    )

    if request.system_form is SystemForm.FCU_FRESH_AIR:
        _build_fcu_fresh_air(
            builder, request, groups, rooms, catalog, placement, water_links, heat_links
        )
    else:
        _build_all_air(builder, request, groups, rooms, catalog, water_links, heat_links)

    _place_condenser_side(builder, request, chiller, catalog, placement)

    project = SystemEvaluateRequest(
        floors=floors,
        rooms=rooms,
        devices=builder.devices,
        pipes=builder.pipes,
        connections=builder.connections,
        simultaneity_factor=request.simultaneity_factor,
        safety_factor=request.safety_factor,
        design_options=dict(request.design_options),
        system_form=request.system_form,
    )

    _select_pumps(builder, project, catalog)
    _size_air_network(builder, catalog, groups, request.system_form)
    _apply_fan_static_pressure(builder, project)
    _refresh_project(project, builder)

    issues = _verify(project, builder)
    return SystemGenerateResult(
        project=project,
        decisions=builder.decisions,
        issues=issues,
        degraded=builder.degraded,
    )


# ---------------------------------------------------------------------------
# 建筑与负荷
# ---------------------------------------------------------------------------


def _rooms_with_ids(rooms: list[RoomInput]) -> list[RoomInput]:
    """给缺 id 的房间补上 `room-N`，让负荷分项可归位。

    生成器**输出**项目，因此可以顺手把房间 id 补齐——这比在后续步骤里
    靠列表位置猜"这份分项负荷属于哪个房间"稳得多。

    一律返回**副本**：调用方传进来的 `SystemGenerateRequest.rooms` 不能被
    生成过程改写（补 id、归一楼层都会改字段）。
    """
    result: list[RoomInput] = []
    for index, room in enumerate(rooms):
        room_id = room.id or room_instance_id(room, index)
        result.append(room.model_copy(update={"id": room_id}))
    return result


def _floor_groups(
    form: SystemForm,
    floors: list[FloorInput],
    rooms: list[RoomInput],
    load: LoadResult,
) -> list[_FloorGroup]:
    """把房间按楼层汇总成生成分组。

    `fcu_fresh_air` 与 `all_air` 都按楼层分区；`all_air` 的分区内再按
    `max_rooms_per_ahu` 细分（见 `_zone_groups`）。这里的 `zone_id` 等于楼层 id。
    """
    load_by_room = {item.id: item for item in load.rooms if item.id}
    floor_by_id = {floor.id: floor for floor in floors}
    order = {floor.id: index for index, floor in enumerate(floors)}

    grouped: dict[str, _FloorGroup] = {}
    for index, room in enumerate(rooms):
        # 楼层引用必须先归一：房间写了一个不存在的 floor_id 时，若原样保留，
        # 生成结果会带上 UNKNOWN_ROOM_FLOOR 错误——那是输入问题，但生成器的
        # 产物必须是自洽的，所以这里把它挂到第一层并留给调用方去核对。
        floor = floor_by_id.get(room.floor_id or "", floors[0])
        if room.floor_id != floor.id:
            room.floor_id = floor.id
        key = floor.id
        group = grouped.get(key)
        if group is None:
            group = _FloorGroup(
                floor=floor,
                zone_id=floor.id,
                room_ids=(),
                room_indexes=(),
            )
            grouped[key] = group
        item = load_by_room.get(room.id or "")
        cooling = item.cooling_kw if item else 0.0
        fresh_air = item.fresh_air_m3_h if item else 0.0
        excluded = 0.0
        if item is not None and form is SystemForm.FCU_FRESH_AIR:
            excluded = math.fsum(
                item.components_kw.get(key_name, 0.0)
                for key_name in _FRESH_AIR_COMPONENTS
            )
        group.cooling_kw += cooling
        group.terminal_cooling_kw += max(cooling - excluded, 0.0)
        group.heating_kw += item.heating_kw if item else 0.0
        group.fresh_air_m3_h += fresh_air
        group.area_m2 += room.area_m2
        group.room_ids = (*group.room_ids, room.id or room_instance_id(room, index))
        group.room_indexes = (*group.room_indexes, index)
        group.room_cooling_kw[room.id or room_instance_id(room, index)] = cooling

    return [grouped[key] for key in sorted(grouped, key=lambda item: order[item])]


@dataclass(frozen=True, slots=True)
class _Placement:
    plant_floor: FloorInput
    roof_floor: FloorInput


def _plant_placement(floors: list[FloorInput]) -> _Placement:
    ordered = sorted(floors, key=lambda floor: (floor.level, floor.elevation_m, floor.id))
    return _Placement(plant_floor=ordered[0], roof_floor=ordered[-1])


def _slug(value: str) -> str:
    """把任意 id 变成可安全拼进设备 id 的形式。"""
    cleaned = "".join(
        character if (character.isalnum() or character in "-_") else "_"
        for character in value
    )
    return cleaned.lower() or "n"


def _floor_run_length(group: _FloorGroup) -> float:
    """水平管/风管的特征长度：取楼层面积的平方根。

    这是**估算值**，量纲核对：[m²] 开方得 [m]，代表气流/水流穿越该层的
    典型行程量级。真实平面深化时应按实际路由替换。
    """
    if group.area_m2 <= 0:
        return 10.0
    return round(math.sqrt(group.area_m2), 3)


def _riser_length(lower: FloorInput, upper: FloorInput) -> float:
    """立管长度：两层顶标高之差的绝对值。

    量纲：[m]。取"下层顶到上层顶"，因为立管要把水/风送到上层的地面以上。
    """
    lower_top = lower.elevation_m + lower.height_m
    upper_top = upper.elevation_m + upper.height_m
    return round(max(abs(upper_top - lower_top), 1.0), 3)


# ---------------------------------------------------------------------------
# 选型
# ---------------------------------------------------------------------------


def _best_of_type(
    outcome_type: SelectionTargetRole,
    project: SystemEvaluateRequest,
    device_type: str,
    catalog: list[dict[str, Any]],
    *,
    requirements: SelectionRequirements | None = None,
) -> SelectionCandidate | None:
    """用平台自己的选型器挑指定设备类型的最佳合格型号。

    `select_equipment` 的 `terminal` 角色覆盖风机盘管、组合式机组、新风机组
    三族，评价口径（容量/水流量/送风量三项下限取最大台数）也是平台既有的，
    所以这里只做**类型过滤**，不重写评价逻辑。

    `requirements` 透传给选型器：风机盘管要按**室内侧**负荷选，而选型器从项目
    派生出来的是总负荷（含新风），这层差额只能由调用方给定。
    """
    outcome = select_equipment(
        project, outcome_type, catalog=catalog, requirements=requirements
    )
    for candidate in outcome.candidates:
        if candidate.device_type == device_type and candidate.eligible:
            return candidate
    return None


def _indoor_requirements(
    request: SystemGenerateRequest,
    group: _FloorGroup,
    rooms: list[RoomInput],
) -> SelectionRequirements:
    """把分组选型需求里的新风部分剔除，得到房间末端的**室内侧**需求。

    做法是**按负荷比例缩放**：选型需求里与负荷成正比的三项
    （`cooling_load_kw`、`chilled_water_flow_m3_h`、`supply_airflow_m3_h`）
    都来自对负荷的线性公式——
      - 冷冻水流量由 `hydraulic_network._required_flow_by_system` 按 `0.86·Q/ΔT` 给出；
      - 送风量由 `calculate_air` 按 `Q·1000·3600/(ρ·cp·ΔT)` 给出。
    两者都对负荷一次齐次，因此乘以 `室内侧负荷 / 总负荷` 即得室内侧的需求，
    **不需要另写一份公式，也不引入新常数**。`required_pump_head_m` 与末端
    选型无关，不参与缩放。
    """
    base = derive_requirements(_group_project(request, group, rooms))
    total = group.cooling_kw
    indoor = group.terminal_cooling_kw
    ratio = (indoor / total) if total > 0 else 1.0
    ratio = min(max(ratio, 0.0), 1.0)
    return base.model_copy(
        update={
            "cooling_load_kw": base.cooling_load_kw * ratio,
            "chilled_water_flow_m3_h": base.chilled_water_flow_m3_h * ratio,
            "supply_airflow_m3_h": base.supply_airflow_m3_h * ratio,
        }
    )


def _catalog_rows(
    catalog: list[dict[str, Any]], device_type: str
) -> list[dict[str, Any]]:
    """目录里可参与选型的某类型型号（排除 `*` 回退行与纯摆放条目）。"""
    resolved = resolve_device_type(device_type)
    rows = [
        row
        for row in catalog
        if resolve_device_type(str(row.get("type"))) == resolved
        and str(row.get("name")) not in {"", "*"}
        and not row.get("placement_only")
        and not row.get("configurable_series")
    ]
    return sorted(rows, key=lambda row: str(row.get("name")))


def _pick_smallest(
    rows: list[dict[str, Any]], requirements: dict[str, float]
) -> dict[str, Any] | None:
    """在满足**全部**下限要求的型号里取最小的一台。

    取最小而不是取最大，是为了让生成结果落在容量裕量区间的下沿附近；
    这与 `rules.json` 的 `capacity_margin.max` 想约束的"别选太大"一致。
    `requirements` 的键必须是目录里真实存在的数值字段。
    """

    def qualifies(row: dict[str, Any]) -> bool:
        for key, minimum in requirements.items():
            value = row.get(key)
            if not isinstance(value, int | float) or float(value) < minimum:
                return False
        return True

    eligible = [row for row in rows if qualifies(row)]
    if not eligible:
        return None
    primary = next(iter(requirements))
    return min(eligible, key=lambda row: float(row.get(primary) or 0.0))


def _rated_number(row: dict[str, Any] | None, key: str) -> float:
    if row is None:
        return 0.0
    value = row.get(key)
    if isinstance(value, (int, float)) and math.isfinite(float(value)):
        return float(value)
    return 0.0


def _place_chiller(
    builder: _Builder,
    request: SystemGenerateRequest,
    load: LoadResult,
    rooms: list[RoomInput],
    catalog: list[dict[str, Any]],
    placement: _Placement,
) -> dict[str, Any] | None:
    """按全楼调整后冷负荷选冷机，并把决策记下来。"""
    chiller_id = "chiller_plant"
    total_kw = load.adjusted_cooling_kw
    builder.decide(
        target="全楼冷负荷",
        choice=f"调整后冷负荷 {total_kw:.1f} kW（同时使用系数 "
        f"{request.simultaneity_factor} × 安全系数 {request.safety_factor}）",
        reason="末端与冷源都按调整后负荷选型，与 `/system/evaluate` 同一口径。",
        source="calculated",
    )
    if total_kw <= 0:
        builder.warn(
            "GENERATION_NO_COOLING_LOAD",
            "全楼调整后冷负荷为 0，冷机按目录最小型号占位；请检查房间面积、"
            "人数与室内外设计参数是否齐全。",
            chiller_id,
        )

    candidate = _best_of_type(
        "cold_source", _bare_project(request, rooms), "冷源机组", catalog
    )
    if candidate is not None:
        builder.device(
            chiller_id,
            _CHILLER_TYPE,
            name=candidate.model_name,
            floor_id=placement.plant_floor.id,
            location_type="equipment_room",
            quantity=candidate.quantity,
        )
        builder.decide(
            target=chiller_id,
            choice=f"{candidate.model_name} × {candidate.quantity} 台",
            reason=(
                "按全楼调整后冷负荷与冷冻水流量选型，容量裕量落在 "
                "`rules.json` 的 capacity_margin 区间内。"
            ),
            source="catalog",
        )
        return _catalog_row(catalog, candidate.device_type, candidate.model_name)

    builder.warn(
        "GENERATION_COLD_SOURCE_UNSELECTED",
        "设备目录中没有合格的冷源型号，已按类型回退行占位，冷源侧参数需人工确认。",
        chiller_id,
    )
    builder.device(
        chiller_id,
        _CHILLER_TYPE,
        floor_id=placement.plant_floor.id,
        location_type="equipment_room",
    )
    return None


def _bare_project(
    request: SystemGenerateRequest, rooms: list[RoomInput]
) -> SystemEvaluateRequest:
    """只含建筑的中间项目，用于"与拓扑无关"的选型（冷机）。

    冷源的选型需求（冷负荷、冷冻水流量）只来自建筑与负荷，不需要任何设备或
    管路，因此可以在装配拓扑之前先把它定下来，用于推导冷却水侧的需求。
    """
    return SystemEvaluateRequest(
        floors=normalized_floors(request),
        rooms=list(rooms),
        simultaneity_factor=request.simultaneity_factor,
        safety_factor=request.safety_factor,
        system_form=request.system_form,
    )


def _catalog_row(
    catalog: list[dict[str, Any]], device_type: str, model_name: str
) -> dict[str, Any] | None:
    for row in _catalog_rows(catalog, device_type):
        if str(row.get("name")) == model_name:
            return row
    return None


def _group_project(
    request: SystemGenerateRequest, group: _FloorGroup, rooms: list[RoomInput]
) -> SystemEvaluateRequest:
    """只含该分组的子项目，用来派生**分组尺度**的选型需求。

    `select_equipment` 的需求是从项目派生出来的（负荷、冷冻水流量、送风量），
    所以把分组做成一个只有该层房间的子项目，就能直接复用平台的选型口径。
    """
    return SystemEvaluateRequest(
        floors=[group.floor],
        rooms=[rooms[index] for index in group.room_indexes],
        simultaneity_factor=request.simultaneity_factor,
        safety_factor=request.safety_factor,
        system_form=request.system_form,
    )


# ---------------------------------------------------------------------------
# 水系统
# ---------------------------------------------------------------------------


def _build_heating(
    builder: _Builder,
    request: SystemGenerateRequest,
    groups: list[_FloorGroup],
    rooms: list[RoomInput],
    catalog: list[dict[str, Any]],
    placement: _Placement,
) -> dict[str, tuple[str, str]]:
    """热水环路：热源 → 热水泵 → 主管 → 立管 → 各层支管 → 末端热水盘管。

    为什么默认生成制热侧：`calculate_load` 会按室内外冬季参数算出供暖负荷，
    没有热源时稳态容量平衡会如实报 `energy_balance_violation`
    （`heating_residual > 0`）——那不是冷源侧的问题，而是**系统缺了制热能力**。
    内置样例都配了空气源热泵与热水环路，本模块沿用同一做法。

    返回 `zone_id → (热水供水支管 id, 热水回水支管 id)`。
    """
    plant = placement.plant_floor.id
    roof = placement.roof_floor.id
    top = groups[-1].floor if groups else placement.roof_floor
    multi_floor = top.id != plant

    candidate = _best_of_type(
        "heat_source", _bare_project(request, rooms), _HEAT_SOURCE_TYPE, catalog
    )
    heat_source_id = "heat_source_roof"
    builder.device(
        heat_source_id,
        _HEAT_SOURCE_TYPE,
        name=candidate.model_name if candidate else None,
        floor_id=roof,
        location_type="roof",
        quantity=candidate.quantity if candidate else 1,
    )
    if candidate is None:
        builder.warn(
            "GENERATION_HEAT_SOURCE_UNSELECTED",
            "设备目录中没有合格的热源型号，热水侧按类型回退行占位，"
            "制热容量需人工确认。",
            heat_source_id,
        )
    else:
        builder.decide(
            target=heat_source_id,
            choice=f"{candidate.model_name} × {candidate.quantity} 台",
            reason=(
                "按全楼供暖负荷与热水流量选型，机组置于屋面（空气源机组需要"
                "室外空气换热）；热水通过 FCU/AHU 的条件热水口接入。"
            ),
            source="catalog",
        )

    builder.device(
        "hw_pump_plant", _PUMP_TYPE, floor_id=plant, location_type="equipment_room"
    )
    builder.pipe(
        "hw_main_supply", "hot_water", pipe_type="main_supply", floor_id=plant,
        length_m=20.0,
    )
    builder.pipe(
        "hw_main_return", "hot_water", pipe_type="main_return", floor_id=plant,
        length_m=20.0,
    )
    # 热源在屋面、泵在机房：先由上至下接到泵，再由泵送到各层。
    if roof != plant:
        source_riser = _riser_length(placement.plant_floor, placement.roof_floor)
        builder.pipe(
            "hw_source_supply_riser", "hot_water", pipe_type="riser_supply",
            from_floor_id=roof, to_floor_id=plant, length_m=source_riser,
        )
        builder.pipe(
            "hw_source_return_riser", "hot_water", pipe_type="riser_return",
            from_floor_id=plant, to_floor_id=roof, length_m=source_riser,
        )
        builder.chain(
            "hot_water",
            heat_source_id,
            "hw_source_supply_riser",
            "hw_pump_plant",
            "hw_main_supply",
        )
        builder.connect(
            "hw_main_return", "hw_source_return_riser", "hot_water"
        )
        builder.connect("hw_source_return_riser", heat_source_id, "hot_water")
    else:
        builder.chain(
            "hot_water", heat_source_id, "hw_pump_plant", "hw_main_supply"
        )
        builder.connect("hw_main_return", heat_source_id, "hot_water")

    if multi_floor:
        riser = _riser_length(placement.plant_floor, top)
        builder.pipe(
            "hw_supply_riser", "hot_water", pipe_type="riser_supply",
            from_floor_id=plant, to_floor_id=top.id, length_m=riser,
        )
        builder.pipe(
            "hw_return_riser", "hot_water", pipe_type="riser_return",
            from_floor_id=top.id, to_floor_id=plant, length_m=riser,
        )
        builder.connect("hw_main_supply", "hw_supply_riser", "hot_water")
        builder.connect("hw_return_riser", "hw_main_return", "hot_water")

    supply_head = "hw_supply_riser" if multi_floor else "hw_main_supply"
    return_head = "hw_return_riser" if multi_floor else "hw_main_return"

    links: dict[str, tuple[str, str]] = {}
    for group in groups:
        tag = _slug(group.zone_id)
        length = _floor_run_length(group)
        supply = builder.pipe(
            f"hw_branch_supply_{tag}", "hot_water", pipe_type="branch_supply",
            floor_id=group.floor.id, length_m=length,
        )
        returns = builder.pipe(
            f"hw_branch_return_{tag}", "hot_water", pipe_type="branch_return",
            floor_id=group.floor.id, length_m=length,
        )
        builder.connect(supply_head, supply, "hot_water")
        builder.connect(returns, return_head, "hot_water")
        links[group.zone_id] = (supply, returns)
    return links


def _build_water_loops(
    builder: _Builder,
    placement: _Placement,
    groups: list[_FloorGroup],
) -> dict[str, tuple[str, str]]:
    """冷冻水环路：冷机 → 冷冻水泵 → 主管 → 立管 → 各层支管 → 末端 → 回水。

    泵在这里是**占位**（不带型号），等管网成形后再按实际阻力定选，见
    `_select_pumps`。

    返回 `zone_id → (供水支管 id, 回水支管 id)`，供末端接线复用。不用模块级
    字典传递：那会让并发调用互相污染，也会把上一次调用的状态漏给下一次。
    """
    plant = placement.plant_floor.id
    top = groups[-1].floor if groups else placement.roof_floor
    multi_floor = top.id != plant

    builder.device(
        "chw_pump_plant", _PUMP_TYPE, floor_id=plant, location_type="equipment_room"
    )
    builder.pipe(
        "chw_main_supply", "chilled_water", pipe_type="main_supply", floor_id=plant,
        length_m=20.0,
    )
    builder.pipe(
        "chw_main_return", "chilled_water", pipe_type="main_return", floor_id=plant,
        length_m=20.0,
    )
    builder.chain(
        "chilled_water",
        "chiller_plant",
        "chw_pump_plant",
        "chw_main_supply",
    )
    if multi_floor:
        riser = _riser_length(placement.plant_floor, top)
        builder.pipe(
            "chw_supply_riser", "chilled_water", pipe_type="riser_supply",
            from_floor_id=plant, to_floor_id=top.id, length_m=riser,
        )
        builder.pipe(
            "chw_return_riser", "chilled_water", pipe_type="riser_return",
            from_floor_id=top.id, to_floor_id=plant, length_m=riser,
        )
        builder.connect("chw_main_supply", "chw_supply_riser", "chilled_water")
        builder.connect("chw_return_riser", "chw_main_return", "chilled_water")
    builder.connect("chw_main_return", "chiller_plant", "chilled_water")

    supply_head = "chw_supply_riser" if multi_floor else "chw_main_supply"
    return_head = "chw_return_riser" if multi_floor else "chw_main_return"

    links: dict[str, tuple[str, str]] = {}
    for group in groups:
        tag = _slug(group.zone_id)
        length = _floor_run_length(group)
        supply = builder.pipe(
            f"chw_branch_supply_{tag}", "chilled_water",
            pipe_type="branch_supply", floor_id=group.floor.id, length_m=length,
        )
        returns = builder.pipe(
            f"chw_branch_return_{tag}", "chilled_water",
            pipe_type="branch_return", floor_id=group.floor.id, length_m=length,
        )
        builder.connect(supply_head, supply, "chilled_water")
        builder.connect(returns, return_head, "chilled_water")
        links[group.zone_id] = (supply, returns)
    return links


def _place_condenser_side(
    builder: _Builder,
    request: SystemGenerateRequest,
    chiller: dict[str, Any] | None,
    catalog: list[dict[str, Any]],
    placement: _Placement,
) -> None:
    """冷却水侧：冷机 ↔ 冷却水泵 ↔ 冷却塔，按冷机目录额定值定容。

    需求全部取自所选冷机的目录字段，不引入新常数：
      冷却水流量 = 冷机 `cooling_flow_m3_h` × 台数
      冷凝排热量 = 冷机 `capacity_kw` + 冷机 `power_kw`（蒸发器冷负荷 + 压缩机输入）
    """
    plant = placement.plant_floor.id
    roof = placement.roof_floor.id
    chiller_quantity = next(
        (d.quantity for d in builder.devices if d.id == "chiller_plant"), 1
    )
    cooling_flow = _rated_number(chiller, "cooling_flow_m3_h") * chiller_quantity
    heat_rejection = (
        _rated_number(chiller, "capacity_kw") + _rated_number(chiller, "power_kw")
    ) * chiller_quantity

    builder.device(
        "cw_pump_plant", _PUMP_TYPE, floor_id=plant, location_type="equipment_room"
    )
    builder.pipe(
        "cw_main_supply", "cooling_water", pipe_type="main_supply", floor_id=plant,
        length_m=18.0,
    )
    builder.pipe(
        "cw_main_return", "cooling_water", pipe_type="main_return", floor_id=plant,
        length_m=18.0,
    )
    builder.chain(
        "cooling_water", "chiller_plant", "cw_pump_plant", "cw_main_supply"
    )

    tower_id = "tower_roof"
    if roof != plant:
        riser = _riser_length(placement.plant_floor, placement.roof_floor)
        builder.pipe(
            "cw_supply_riser", "cooling_water", pipe_type="riser_supply",
            from_floor_id=plant, to_floor_id=roof, length_m=riser,
        )
        builder.pipe(
            "cw_return_riser", "cooling_water", pipe_type="riser_return",
            from_floor_id=roof, to_floor_id=plant, length_m=riser,
        )
        builder.connect("cw_main_supply", "cw_supply_riser", "cooling_water")
        builder.connect("tower_roof", "cw_return_riser", "cooling_water")
        builder.connect("cw_supply_riser", tower_id, "cooling_water")
    else:
        builder.connect("cw_main_supply", tower_id, "cooling_water")
        builder.pipe(
            "cw_return_riser", "cooling_water", pipe_type="main_return",
            floor_id=plant, length_m=10.0,
        )
        builder.connect(tower_id, "cw_return_riser", "cooling_water")
    builder.connect("cw_return_riser", "cw_main_return", "cooling_water")
    builder.connect("cw_main_return", "chiller_plant", "cooling_water")

    if heat_rejection > 0:
        rows = _catalog_rows(catalog, _TOWER_TYPE)
        row = _pick_smallest(
            rows, {"capacity_kw": heat_rejection, "flow_m3_h": cooling_flow}
        )
        if row is None:
            row = max(
                rows, key=lambda item: float(item.get("capacity_kw") or 0.0), default=None
            )
            builder.warn(
                "GENERATION_TOWER_UNDERSIZED",
                f"目录中最大的冷却塔也承担不了 {heat_rejection:.1f} kW 的冷凝排热量，"
                "已取最大型号占位；请并联多台或改用更大的塔。",
                tower_id,
            )
        builder.device(
            tower_id,
            _TOWER_TYPE,
            name=str(row.get("name")) if row else None,
            floor_id=roof,
            location_type="roof",
        )
        builder.decide(
            target=tower_id,
            choice=(
                f"{row.get('name') if row else '类型回退行'}（冷凝排热量 "
                f"{heat_rejection:.1f} kW、冷却水 {cooling_flow:.1f} m³/h）"
            ),
            reason=(
                "冷凝排热量 = 冷机目录制冷量 + 压缩机输入功率（热力学第一定律），"
                "冷却水流量取冷机目录 cooling_flow_m3_h；两项都是目录声明值。"
            ),
            source="catalog",
        )
    else:
        builder.device(tower_id, _TOWER_TYPE, floor_id=roof, location_type="roof")
        builder.warn(
            "GENERATION_CONDENSER_UNKNOWN",
            "冷机型号未定，冷却塔与冷却水泵按类型回退行占位，容量需人工确认。",
            tower_id,
        )


# ---------------------------------------------------------------------------
# 风系统与末端
# ---------------------------------------------------------------------------


def _build_fcu_fresh_air(
    builder: _Builder,
    request: SystemGenerateRequest,
    groups: list[_FloorGroup],
    rooms: list[RoomInput],
    catalog: list[dict[str, Any]],
    placement: _Placement,
    water_links: dict[str, tuple[str, str]],
    heat_links: dict[str, tuple[str, str]],
) -> None:
    """风机盘管 + 新风：风机盘管管室内侧，新风机组管新风，两者风汇合后送房间。

    拓扑与 `FOUR_FLOOR_FCU_FRESH_AIR_SAMPLE` 同构：

        新风取风口 → 新风立管 → 各层新风管 → 新风机组 → 新风送风管 ┐
                                                                    ├→ 送风静压箱 → 送风口
        风机盘管（室内侧负荷）→ 风机盘管送风管 ────────────────────┘        ↓ 室内
        风机盘管 ← 回风管 ←──────────────────────────────────────────────┘
    """
    top = groups[-1].floor if groups else placement.roof_floor
    multi_floor = len(groups) > 1
    total_fresh_air = math.fsum(group.fresh_air_m3_h for group in groups)

    builder.pipe(
        "outdoor_air_intake", "air", floor_id=placement.roof_floor.id, length_m=6.0
    )
    if multi_floor:
        builder.pipe(
            "outdoor_air_riser", "air", pipe_type="riser_supply",
            from_floor_id=top.id, to_floor_id=groups[0].floor.id,
            length_m=_riser_length(groups[0].floor, top),
        )
        builder.connect("outdoor_air_intake", "outdoor_air_riser", "air")

    for group in groups:
        tag = _slug(group.zone_id)
        floor_id = group.floor.id
        length = _floor_run_length(group)
        supply_pipe, return_pipe = water_links[group.zone_id]

        # --- 冷冻水支路：风机盘管与新风机组各一路 ---
        fcu_water_supply = builder.pipe(
            f"chw_fcu_supply_{tag}", "chilled_water", pipe_type="terminal_branch",
            floor_id=floor_id, length_m=length * 0.4,
        )
        fcu_water_return = builder.pipe(
            f"chw_fcu_return_{tag}", "chilled_water", pipe_type="terminal_branch",
            floor_id=floor_id, length_m=length * 0.4,
        )
        fau_water_supply = builder.pipe(
            f"chw_fau_supply_{tag}", "chilled_water", pipe_type="terminal_branch",
            floor_id=floor_id, length_m=length * 0.5,
        )
        fau_water_return = builder.pipe(
            f"chw_fau_return_{tag}", "chilled_water", pipe_type="terminal_branch",
            floor_id=floor_id, length_m=length * 0.5,
        )
        builder.connect(supply_pipe, fcu_water_supply, "chilled_water")
        builder.connect(supply_pipe, fau_water_supply, "chilled_water")
        builder.connect(fcu_water_return, return_pipe, "chilled_water")
        builder.connect(fau_water_return, return_pipe, "chilled_water")

        # --- 末端设备 ---
        fcu_id = f"fcu_{tag}"
        fau_id = f"fau_{tag}"
        outlet_id = f"outlet_{tag}"
        _place_fcu(builder, request, group, rooms, catalog, fcu_id, floor_id)
        _place_fau(builder, group, catalog, fau_id, floor_id)
        builder.device(outlet_id, _SUPPLY_OUTLET_TYPE, floor_id=floor_id)

        builder.connect(fcu_water_supply, fcu_id, "chilled_water")
        builder.connect(fcu_id, fcu_water_return, "chilled_water")
        builder.connect(fau_water_supply, fau_id, "chilled_water")
        builder.connect(fau_id, fau_water_return, "chilled_water")

        # --- 热水支路：只接房间末端的条件热水口 ---
        # 新风机组只处理室外新风的冷负荷，制热不经过它（与四层样例一致），
        # 因此它的条件热水口保持未激活。
        if heat_links:
            hot_supply, hot_return = heat_links[group.zone_id]
            fcu_hot_supply = builder.pipe(
                f"hw_fcu_supply_{tag}", "hot_water", pipe_type="terminal_branch",
                floor_id=floor_id, length_m=length * 0.4,
            )
            fcu_hot_return = builder.pipe(
                f"hw_fcu_return_{tag}", "hot_water", pipe_type="terminal_branch",
                floor_id=floor_id, length_m=length * 0.4,
            )
            builder.chain("hot_water", hot_supply, fcu_hot_supply, fcu_id)
            builder.chain("hot_water", fcu_id, fcu_hot_return, hot_return)

        # --- 风管 ---
        outdoor_duct = builder.pipe(
            f"outdoor_air_duct_{tag}", "air", floor_id=floor_id, length_m=length,
        )
        fau_supply_duct = builder.pipe(
            f"fau_supply_duct_{tag}", "air", floor_id=floor_id, length_m=length * 0.7,
        )
        supply_manifold = builder.pipe(
            f"supply_manifold_{tag}", "air", floor_id=floor_id, length_m=length * 0.5,
        )
        fcu_supply_duct = builder.pipe(
            f"fcu_supply_duct_{tag}", "air", floor_id=floor_id, length_m=length * 0.6,
        )
        return_air_duct = builder.pipe(
            f"return_air_duct_{tag}", "air", floor_id=floor_id, length_m=length * 0.6,
        )
        if multi_floor:
            builder.connect("outdoor_air_riser", outdoor_duct, "air")
        else:
            builder.connect("outdoor_air_intake", outdoor_duct, "air")
        builder.chain(
            "air", outdoor_duct, fau_id, fau_supply_duct, supply_manifold
        )
        builder.chain("air", fcu_id, fcu_supply_duct, supply_manifold)
        builder.chain(
            "air", supply_manifold, outlet_id, return_air_duct, fcu_id
        )
        builder.decide(
            target=f"{outlet_id}",
            choice="新风与风机盘管送风在送风静压箱汇合，共用该层送风口",
            reason=(
                "风机盘管只处理房间回风、新风机组处理 100% 室外新风，两股风在"
                "送入房间前混合。室内段的回风由送风口一侧的回风管返回风机盘管，"
                "与四层样例同构。"
            ),
            source="calculated",
        )

    if total_fresh_air > 0:
        builder.decide(
            target="新风总需求",
            choice=f"全楼卫生新风 {total_fresh_air:.0f} m³/h",
            reason="按各房间人数 × 用途每人新风量累加，与 `calculate_load` 同口径。",
            source="calculated",
        )


def _place_fcu(
    builder: _Builder,
    request: SystemGenerateRequest,
    group: _FloorGroup,
    rooms: list[RoomInput],
    catalog: list[dict[str, Any]],
    fcu_id: str,
    floor_id: str,
) -> None:
    """按分组的**室内侧**冷负荷选风机盘管。

    风机盘管不承担新风，因此不能用分组总负荷选型——总负荷含新风显热与潜热，
    那样会把风机盘管选大。这一份负荷归新风机组。
    """
    fallback = max(group.terminal_cooling_kw, 0.0)
    if fallback <= 0:
        builder.device(fcu_id, _FCU_TYPE, floor_id=floor_id)
        builder.warn(
            "GENERATION_FCU_UNSELECTED",
            f"{group.floor.name or group.floor.id} 的室内侧冷负荷为 0，"
            "风机盘管按类型回退行占位。",
            fcu_id,
        )
        return
    candidate = _best_of_type(
        "terminal",
        _group_project(request, group, rooms),
        _FCU_TYPE,
        catalog,
        requirements=_indoor_requirements(request, group, rooms),
    )
    if candidate is None:
        builder.device(fcu_id, _FCU_TYPE, floor_id=floor_id)
        builder.warn(
            "GENERATION_FCU_UNSELECTED",
            f"{group.floor.name or group.floor.id} 的 {fallback:.1f} kW 室内侧负荷"
            "在目录中没有合格的风机盘管，已按类型回退行占位。",
            fcu_id,
        )
        return
    builder.device(
        fcu_id, _FCU_TYPE, name=candidate.model_name, floor_id=floor_id,
        quantity=candidate.quantity,
    )
    builder.decide(
        target=fcu_id,
        choice=f"{candidate.model_name} × {candidate.quantity} 台",
        reason=(
            f"承担该层室内侧冷负荷 {fallback:.1f} kW（总负荷扣除新风显热与新风潜热），"
            "台数按容量、水流量、送风量三项下限取最大，与教学选型口径一致。"
        ),
        source="catalog",
    )


def _place_fau(
    builder: _Builder,
    group: _FloorGroup,
    catalog: list[dict[str, Any]],
    fau_id: str,
    floor_id: str,
) -> None:
    """按该层卫生新风量选新风机组。

    新风机组的选型依据是**风量**而不是冷量：它处理 100% 室外新风，风量由人数
    决定，与房间冷负荷无关。因此这里直接按目录 `airflow_m3_h` 匹配，
    不用 `select_equipment` 的负荷派生口径（那会把新风机组按冷负荷选型）。
    """
    required = group.fresh_air_m3_h
    if required <= 0:
        builder.device(fau_id, _FAU_TYPE, floor_id=floor_id)
        builder.decide(
            target=fau_id,
            choice="按类型回退行占位（该层无卫生新风需求）",
            reason="房间人数为 0 时按人数算出的新风量为 0，不需要新风处理设备。",
            source="calculated",
        )
        return
    rows = _catalog_rows(catalog, _FAU_TYPE)
    row = _pick_smallest(rows, {"airflow_m3_h": required})
    if row is None:
        row = max(
            rows, key=lambda item: float(item.get("airflow_m3_h") or 0.0), default=None
        )
        builder.warn(
            "GENERATION_FAU_UNDERSIZED",
            f"{group.floor.name or group.floor.id} 的卫生新风 {required:.0f} m³/h "
            "超过目录最大新风机组风量，已取最大型号占位；请并联多台。",
            fau_id,
        )
    quantity = 1
    if row is not None and _rated_number(row, "airflow_m3_h") > 0:
        quantity = max(
            math.ceil(required / _rated_number(row, "airflow_m3_h")), 1
        )
    builder.device(
        fau_id, _FAU_TYPE, name=str(row.get("name")) if row else None,
        floor_id=floor_id, quantity=quantity,
    )
    builder.decide(
        target=fau_id,
        choice=(
            f"{row.get('name') if row else '类型回退行'} × {quantity} 台"
            f"（该层新风 {required:.0f} m³/h）"
        ),
        reason=(
            "新风机组处理 100% 室外新风，选型依据是卫生新风量（人数 × 每人新风量），"
            "与房间冷负荷无关。"
        ),
        source="catalog",
    )


def _build_all_air(
    builder: _Builder,
    request: SystemGenerateRequest,
    groups: list[_FloorGroup],
    rooms: list[RoomInput],
    catalog: list[dict[str, Any]],
    water_links: dict[str, tuple[str, str]],
    heat_links: dict[str, tuple[str, str]],
) -> None:
    """全空气（定风量）：组合式机组处理该分区全部负荷，风管按房间分支。

    拓扑与 `MULTI_ZONE_ALL_AIR_SAMPLE` 同构：

        组合式机组 → 送风连接管 → 送风主管 → 各房间送风支管 → 送风口
                                                                  ↓ 室内
        组合式机组 ← 回风连接管 ← 回风主管 ← 各房间回风支管 ← 回风阀 ←┘
    """
    for group in groups:
        tag = _slug(group.zone_id)
        floor_id = group.floor.id
        length = _floor_run_length(group)
        supply_pipe, return_pipe = water_links[group.zone_id]

        ahu_id = f"ahu_{tag}"
        _place_ahu(builder, request, group, rooms, catalog, ahu_id, floor_id)

        water_supply = builder.pipe(
            f"chw_ahu_supply_{tag}", "chilled_water", pipe_type="terminal_branch",
            floor_id=floor_id, length_m=length * 0.4,
        )
        water_return = builder.pipe(
            f"chw_ahu_return_{tag}", "chilled_water", pipe_type="terminal_branch",
            floor_id=floor_id, length_m=length * 0.4,
        )
        builder.connect(supply_pipe, water_supply, "chilled_water")
        builder.connect(water_supply, ahu_id, "chilled_water")
        builder.connect(ahu_id, water_return, "chilled_water")
        builder.connect(water_return, return_pipe, "chilled_water")

        if heat_links:
            hot_supply, hot_return = heat_links[group.zone_id]
            ahu_hot_supply = builder.pipe(
                f"hw_ahu_supply_{tag}", "hot_water", pipe_type="terminal_branch",
                floor_id=floor_id, length_m=length * 0.4,
            )
            ahu_hot_return = builder.pipe(
                f"hw_ahu_return_{tag}", "hot_water", pipe_type="terminal_branch",
                floor_id=floor_id, length_m=length * 0.4,
            )
            builder.chain("hot_water", hot_supply, ahu_hot_supply, ahu_id)
            builder.chain("hot_water", ahu_id, ahu_hot_return, hot_return)

        supply_connector = builder.pipe(
            f"ahu_supply_connector_{tag}", "air", pipe_type="terminal_branch",
            floor_id=floor_id, length_m=4.0,
        )
        supply_main = builder.pipe(
            f"supply_main_{tag}", "air", pipe_type="main_supply",
            floor_id=floor_id, length_m=length,
        )
        return_connector = builder.pipe(
            f"ahu_return_connector_{tag}", "air", pipe_type="terminal_branch",
            floor_id=floor_id, length_m=4.0,
        )
        return_main = builder.pipe(
            f"return_main_{tag}", "air", pipe_type="main_return",
            floor_id=floor_id, length_m=length,
        )
        builder.chain("air", ahu_id, supply_connector, supply_main)
        builder.chain("air", return_main, return_connector, ahu_id)

        for room_id, room_index in zip(
            group.room_ids, group.room_indexes, strict=True
        ):
            room_tag = _slug(room_id)
            supply_branch = builder.pipe(
                f"supply_{room_tag}", "air", pipe_type="branch_supply",
                floor_id=floor_id, length_m=length * 0.8,
            )
            return_branch = builder.pipe(
                f"return_{room_tag}", "air", pipe_type="branch_return",
                floor_id=floor_id, length_m=length * 0.8,
            )
            outlet_id = f"outlet_{room_tag}"
            damper_id = f"return_damper_{room_tag}"
            builder.device(outlet_id, _SUPPLY_OUTLET_TYPE, floor_id=floor_id)
            builder.device(damper_id, _RETURN_DAMPER_TYPE, floor_id=floor_id)
            builder.chain("air", supply_main, supply_branch, outlet_id)
            builder.chain("air", outlet_id, damper_id, return_branch, return_main)
            # 房间绑定在 `_bind_rooms_to_terminals` 里统一处理。


def _place_ahu(
    builder: _Builder,
    request: SystemGenerateRequest,
    group: _FloorGroup,
    rooms: list[RoomInput],
    catalog: list[dict[str, Any]],
    ahu_id: str,
    floor_id: str,
) -> None:
    """按分组的**全部**负荷选组合式空调机组。

    全空气系统的新风由机组混风箱承担，因此机组要同时覆盖室内负荷与新风负荷，
    用分组总冷负荷选型是正确的（与风机盘管制式的口径刚好相反）。
    """
    if group.cooling_kw <= 0:
        builder.device(ahu_id, _AHU_TYPE, floor_id=floor_id)
        builder.warn(
            "GENERATION_AHU_UNSELECTED",
            f"{group.floor.name or group.floor.id} 的冷负荷为 0，"
            "组合式空调机组按类型回退行占位。",
            ahu_id,
        )
        return
    candidate = _best_of_type(
        "terminal", _group_project(request, group, rooms), _AHU_TYPE, catalog
    )
    if candidate is None:
        builder.device(ahu_id, _AHU_TYPE, floor_id=floor_id)
        builder.warn(
            "GENERATION_AHU_UNSELECTED",
            f"{group.floor.name or group.floor.id} 的 {group.cooling_kw:.1f} kW 负荷"
            "在目录中没有合格的组合式空调机组，已按类型回退行占位。",
            ahu_id,
        )
        return
    builder.device(
        ahu_id, _AHU_TYPE, name=candidate.model_name, floor_id=floor_id,
        quantity=candidate.quantity,
    )
    builder.decide(
        target=ahu_id,
        choice=f"{candidate.model_name} × {candidate.quantity} 台",
        reason=(
            f"承担该分区全部冷负荷 {group.cooling_kw:.1f} kW（含新风），"
            "台数按容量、水流量、送风量三项下限取最大。"
        ),
        source="catalog",
    )


# ---------------------------------------------------------------------------
# 定选、房间绑定与自检
# ---------------------------------------------------------------------------


def _select_pumps(
    builder: _Builder,
    project: SystemEvaluateRequest,
    catalog: list[dict[str, Any]],
) -> None:
    """管网成形后按实际阻力给各环路水泵定选型号。

    泵的扬程需求来自最不利环路阻力，而环路要先有泵才闭合——所以先用占位泵把
    拓扑接好，再回来定选。这一步是"初选 → 水力复核 → 定选"里的复核与定选。

    冷冻水泵用平台自己的选型器（它的需求——冷冻水流量与所需扬程——都能从项目
    派生）。冷却水泵与热水泵的需求不在选型器的口径内（选型器只认冷冻水），
    因此按**各自环路**的目录声明流量与最不利路径扬程匹配。

    **每台泵都必须落到具体型号**：`循环泵` 在目录里没有 `*` 回退行，留下
    `name=None` 会让 `validators/topology.py` 报 `UNKNOWN_DEVICE_CATALOG`
    并跳过系统兼容性检查。
    """
    try:
        candidate = _best_of_type("pump", project, _PUMP_TYPE, catalog)
    except Exception:  # noqa: BLE001 - 需求不可派生时退到目录匹配
        candidate = None
    if candidate is None:
        builder.warn(
            "GENERATION_PUMP_UNSELECTED",
            "管网阻力或冷冻水流量不可派生，冷冻水泵改用目录流量匹配；请确认水环路已闭合。",
            "chw_pump_plant",
        )
    else:
        _assign_pump(
            builder,
            "chw_pump_plant",
            model_name=candidate.model_name,
            quantity=candidate.quantity,
            reason=(
                "冷冻水泵按生成出的水环路实际阻力与冷冻水流量定选（先占位闭合环路、"
                "再水力复核、最后定选），扬程裕量落在 `rules.json` 的 pump_margin 区间内。"
            ),
        )

    # 辅助环路泵：流量取上游机组的目录声明值，扬程取该环路最不利路径。
    for device_id, system, flow, label, source_field in (
        (
            "cw_pump_plant",
            "cooling_water",
            _device_catalog_flow(builder, catalog, "chiller_plant", "cooling_flow_m3_h"),
            "冷却水泵",
            "冷机目录 cooling_flow_m3_h",
        ),
        (
            "hw_pump_plant",
            "hot_water",
            _device_catalog_flow(builder, catalog, "heat_source_roof", "flow_m3_h"),
            "热水泵",
            "热源目录 flow_m3_h（负荷侧热水流量）",
        ),
    ):
        if not any(device.id == device_id for device in builder.devices):
            continue
        head = _loop_head(project, system)
        rows = _catalog_rows(catalog, _PUMP_TYPE)
        row = (
            _pick_smallest(rows, {"flow_m3_h": flow, "head_m": head})
            if flow > 0 and head > 0
            else None
        )
        if row is None:
            row = max(
                rows, key=lambda item: float(item.get("flow_m3_h") or 0.0), default=None
            )
            builder.warn(
                "GENERATION_PUMP_UNDERSIZED",
                f"目录中没有同时满足 {label} {flow:.1f} m³/h 与 {head:.2f} m 扬程的"
                "循环泵，已取最大流量型号；扬程或流量可能不足，请并联多台或改用大型号。",
                device_id,
            )
        if row is None:
            continue
        quantity = max(math.ceil(flow / max(_rated_number(row, "flow_m3_h"), 1e-9)), 1)
        _assign_pump(
            builder,
            device_id,
            model_name=str(row.get("name")),
            quantity=quantity,
            reason=(
                f"{label}流量 {flow:.1f} m³/h 取自{source_field}，"
                f"扬程 {head:.2f} m 取该环路最不利路径（与冷冻水同一口径）。"
            ),
        )


def _assign_pump(
    builder: _Builder,
    device_id: str,
    *,
    model_name: str,
    quantity: int,
    reason: str,
) -> None:
    for device in builder.devices:
        if device.id == device_id:
            device.name = model_name
            device.quantity = quantity
    builder.decide(
        target=device_id,
        choice=f"{model_name} × {quantity} 台",
        reason=reason,
        source="catalog",
    )


def _device_catalog_flow(
    builder: _Builder, catalog: list[dict[str, Any]], device_id: str, field_name: str
) -> float:
    """某台已放置设备的目录声明流量 × 台数。设备缺型号时返回 0。"""
    device = next((item for item in builder.devices if item.id == device_id), None)
    if device is None or not device.name:
        return 0.0
    row = _catalog_row(catalog, device.type, device.name)
    return _rated_number(row, field_name) * device.quantity


def _loop_head(project: SystemEvaluateRequest, system: str) -> float:
    """某个水环路的最不利路径扬程。

    `calculate_hydraulic_network` 的 `required_pump_head_m` 只统计**冷冻水**
    闭合回路（见其 `_required_head_from_paths`），冷却水与热水侧要自己从
    `paths` 里按系统取最大值，口径与冷冻水完全相同。
    """
    from app.calculators.hydraulic_network import calculate_hydraulic_network

    try:
        result = calculate_hydraulic_network(project)
    except Exception:  # noqa: BLE001 - 环路算不出来时退回 0，不阻断生成
        return 0.0
    heads = [
        path.head_m or 0.0
        for path in result.paths
        if path.system == system and (path.head_m or 0.0) > 0
    ]
    return max(heads, default=0.0)


def _device_airflow(
    builder: _Builder, catalog: list[dict[str, Any]], prefix: str, floor_id: str
) -> float:
    """某层某族机组的额定送风量合计（目录 `airflow_m3_h` × 台数）。"""
    total = 0.0
    for device in builder.devices:
        if not device.id.startswith(prefix) or device.floor_id != floor_id:
            continue
        row = _catalog_row(catalog, device.type, device.name or "")
        total += _rated_number(row, "airflow_m3_h") * device.quantity
    return total


def _size_duct(airflow_m3_h: float, target_velocity_m_s: float) -> dict[str, Any]:
    """按风量与目标流速选标准矩形风管。

    复用静态计算器 `calculate_air`：它按 `Q = ṁ·cp·ΔT` 由冷负荷反算风量，
    这里把风量按同一公式**反解**回等效冷负荷再交给它，避免另写一份标准风管
    表的查表逻辑（量纲一致，`ΔT` 取 `AirRequest` 的默认送风温差）。
    """
    from app.calculators.air import AIR_CP_J_KG_K, AIR_DENSITY_KG_M3, calculate_air

    # 送风温差取 `AirRequest` 自己的默认值，不另写常数。
    delta_t = float(AirRequest.model_fields["supply_return_delta_t_c"].default)
    equivalent_kw = (
        airflow_m3_h * AIR_DENSITY_KG_M3 * AIR_CP_J_KG_K * delta_t / (1000.0 * 3600.0)
    )
    if equivalent_kw <= 0:
        return {}
    result = calculate_air(
        AirRequest(cooling_load_kw=equivalent_kw, target_velocity_m_s=target_velocity_m_s)
    )
    duct = result.selected_duct
    if duct.get("type") == "rectangular":
        return {"width_mm": duct.get("width_mm"), "height_mm": duct.get("height_mm")}
    diameter = duct.get("diameter_mm")
    if diameter is None:
        return {}
    # 平台的风管标准表以矩形为主，圆管按等面积折算成方管便于统一校核。
    side = round(math.sqrt(math.pi * float(diameter) ** 2 / 4.0), 1)
    return {"width_mm": side, "height_mm": side}


def _size_air_network(
    builder: _Builder,
    catalog: list[dict[str, Any]],
    groups: list[_FloorGroup],
    form: SystemForm,
) -> None:
    """给每条风管定**风量**与**断面尺寸**。

    两件事都必须做，而且都不能省：

    - **风量**：`solve_air_trees` 把风管的 `flow_m3_h` 当作叶子需求（`explicit_leaf_duct`）。
      不写它就只能退到"按连接设备推断"或"均分兜底"，后者会把风网解法标记为降级，
      并让送/回风总量对不上（`AIR_SUPPLY_RETURN_MISMATCH`）。样例都是显式写的。
    - **尺寸**：与水管不同，风管**不会**被计算器自动反算——缺尺寸会直接报
      `AIR_DUCT_SIZE_MISSING` 并使最不利风路压力偏小。所以生成器必须自己选型。
    """
    target_velocity = float(
        rule_value("air_velocity_m_s.main_duct", {"min": 2.0, "max": 8.0}).get("max", 8.0)
    )
    builder.decide(
        target="空气系统风管",
        choice=f"按风量与目标流速 {target_velocity:.1f} m/s 选标准矩形风管",
        reason=(
            "目标流速取 `rules.json` 的 air_velocity_m_s.main_duct 上限——它是"
            "风管校核的约束边界；风管断面由此反算，尺寸全部来自标准风管表。"
        ),
        source="rules",
    )

    flows: dict[str, float] = {}
    for group in groups:
        tag = _slug(group.zone_id)
        floor_id = group.floor.id
        if form is SystemForm.FCU_FRESH_AIR:
            fcu_flow = _device_airflow(builder, catalog, "fcu_", floor_id)
            fau_flow = _device_airflow(builder, catalog, "fau_", floor_id)
            flows[f"outdoor_air_duct_{tag}"] = fau_flow
            flows[f"fau_supply_duct_{tag}"] = fau_flow
            flows[f"fcu_supply_duct_{tag}"] = fcu_flow
            # 送风静压箱同时接收新风机组与风机盘管的送风。
            flows[f"supply_manifold_{tag}"] = fcu_flow + fau_flow
            # 回风只回到风机盘管（新风机组是全新风机组，不取回风）。
            flows[f"return_air_duct_{tag}"] = fcu_flow
        else:
            ahu_flow = _device_airflow(builder, catalog, "ahu_", floor_id)
            flows[f"ahu_supply_connector_{tag}"] = ahu_flow
            flows[f"supply_main_{tag}"] = ahu_flow
            flows[f"return_main_{tag}"] = ahu_flow
            flows[f"ahu_return_connector_{tag}"] = ahu_flow
            total_room_cooling = math.fsum(group.room_cooling_kw.values())
            for room_id in group.room_ids:
                room_tag = _slug(room_id)
                weight = (
                    group.room_cooling_kw.get(room_id, 0.0) / total_room_cooling
                    if total_room_cooling > 0
                    else 1.0 / max(len(group.room_ids), 1)
                )
                flows[f"supply_{room_tag}"] = ahu_flow * weight
                flows[f"return_{room_tag}"] = ahu_flow * weight

    if form is SystemForm.FCU_FRESH_AIR:
        # 新风干管按各层新风机组风量合计；取风口与立管是同一股风。
        total_fau = math.fsum(
            _device_airflow(builder, catalog, "fau_", group.floor.id)
            for group in groups
        )
        flows["outdoor_air_intake"] = total_fau
        flows["outdoor_air_riser"] = total_fau

    pipe_by_id = {pipe.id: pipe for pipe in builder.pipes}
    for pipe_id, airflow in sorted(flows.items()):
        pipe = pipe_by_id.get(pipe_id)
        if pipe is None or airflow <= 0:
            continue
        pipe.flow_m3_h = round(airflow, 3)
        for key, value in _size_duct(airflow, target_velocity).items():
            setattr(pipe, key, value)


def _apply_fan_static_pressure(
    builder: _Builder, project: SystemEvaluateRequest
) -> None:
    """按最不利风路给 `design_options.fan_static_pressure_pa` 定值。

    风机的静压是**设计选项**而不是设备额定值（`air_network` 从
    `design_options` 读取），所以生成器要把它算好写进去，否则风压校核会报
    `FAN_PRESSURE_UNKNOWN`。
    """
    from app.calculators.air_network import calculate_air_network

    try:
        result = calculate_air_network(project)
    except Exception:  # noqa: BLE001 - 风网未成树时跳过风压定值
        return
    required = result.required_fan_pressure_pa
    if required <= 0:
        return
    head_min = float(rule_value(f"{_MARGIN_SOURCE_PREFIX}.head_min", 1.05))
    head_max = float(rule_value(f"{_MARGIN_SOURCE_PREFIX}.head_max", 1.3))
    margin = (head_min + head_max) / 2.0
    project.design_options["fan_static_pressure_pa"] = round(required * margin, 3)
    builder.decide(
        target="design_options.fan_static_pressure_pa",
        choice=f"{required * margin:.1f} Pa（最不利风路 {required:.1f} Pa × 裕量 {margin:.3f}）",
        reason=(
            "风机静压取最不利风路阻力乘以裕量，裕量取 `rules.json` 中泵扬程裕量"
            "区间的中值——两者是同口径的风机/水泵压头裕量，且必须高于风压校核"
            "的合格阈值 1.05。"
        ),
        source="rules",
    )


def _refresh_project(
    project: SystemEvaluateRequest, builder: _Builder
) -> None:
    """把生成期累积的列表与房间绑定写回项目对象。"""
    project.devices = builder.devices
    project.pipes = builder.pipes
    project.connections = builder.connections
    _bind_rooms_to_terminals(project, builder)


def _bind_rooms_to_terminals(
    project: SystemEvaluateRequest, builder: _Builder
) -> None:
    """把每台房间末端写进它服务的房间的 `device_ids`。

    绑定方向是**房间 → 设备**（`RoomInput.device_ids` 才是权威清单），
    `room_bindings` 再把它倒置给仿真用。制式决定绑谁：

    - `fcu_fresh_air`：房间绑本层风机盘管 + 本层新风机组（两者都在房间里出力）；
    - `all_air`：房间只绑本层组合式空调机组。
    """
    form = project.system_form
    # 制式决定房间末端族：风机盘管制式下房间同时绑风机盘管与新风机组
    # （两者都在房间里出力），全空气制式下只绑组合式机组。
    terminal_prefixes = (
        ("fcu_", "fau_") if form is SystemForm.FCU_FRESH_AIR else ("ahu_",)
    )
    by_floor: dict[str, list[str]] = {}
    for device in builder.devices:
        if device.id.startswith(terminal_prefixes):
            by_floor.setdefault(device.floor_id or "", []).append(device.id)

    for room in project.rooms:
        floor_id = room.floor_id or ""
        room.device_ids = sorted(by_floor.get(floor_id, []))
        outlet_id = (
            f"outlet_{_slug(floor_id)}"
            if form is SystemForm.FCU_FRESH_AIR
            else f"outlet_{_slug(room.id or '')}"
        )
        if any(device.id == outlet_id for device in builder.devices):
            room.supply_air_outlet_ids = [outlet_id]
            room.return_air_outlet_ids = [outlet_id]


def _verify(project: SystemEvaluateRequest, builder: _Builder) -> list[Issue]:
    """自检：生成的骨架必须自身通过通用校验与制式校验。

    制式校验的 error 意味着**生成器自己产出了不合制的拓扑**，属于本模块的缺陷
    而不是用户的输入问题，因此把它们转成显式 warning + `degraded`，让调用方
    看得见，而不是假装生成成功。
    """
    issues: list[Issue] = list(builder.issues)
    validation = validate_project_data(project)
    issues.extend(validation.issues)
    form_check = check_system_form(project)
    issues.extend(form_check.issues)
    if not form_check.compliant or not validation.valid:
        builder.degraded = True
    return issues
