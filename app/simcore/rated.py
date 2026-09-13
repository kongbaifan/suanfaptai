"""固定参数解析

设备的额定参数由选型决定，运行时只读。本模块负责把设备身份解析成 `RatedSpec`，
并在设备目录缺条目时逐级降级，同时把降级过程标记出来——教学时可以直接告诉学生
"这台设备参数不全，读数是兜底值"，而不是让魔数悄悄生效。

身份桥接：Unity 侧用 `(device_id, model_id)`，Python 设备目录用 `(type, name)`。
两者通过 `DeviceSpec.device_type` / `device_name` 对接，因为
`equipment_catalog.json` 的 `type` 字段直接使用 Unity 中文 DeviceType。
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Any, Literal

from app.data.equipment import find_equipment_spec, load_equipment_catalog
from app.simcore.hydronics import WATER_KW_PER_M3H_K
from app.unity_catalog import DeviceRole, get_device_spec

ParameterOrigin = Literal["catalog", "type_default", "builtin_fallback"]
"""额定参数来源。`catalog` 为型号精确命中，其余两级为降级结果。"""


@dataclass(frozen=True, slots=True)
class RatedSpec:
    """设备额定参数

    所有字段为设计工况下的额定值，运行期不变。实际工况由
    `实际值 = 额定值 × f(可调参数)` 推导，见 `simcore.devices`。

    `flow_m3_h` 对不同角色含义不同：冷机为冷冻侧水流量，水泵为额定流量，
    末端为盘管水流量，冷却塔为冷却水流量。`cooling_flow_m3_h` 仅冷机使用。
    """

    device_type: str
    model_name: str
    role: DeviceRole

    capacity_kw: float | None = None
    cop: float | None = None
    iplv: float | None = None
    power_kw: float | None = None
    flow_m3_h: float | None = None
    cooling_flow_m3_h: float | None = None
    head_m: float | None = None
    airflow_m3_h: float | None = None
    fan_power_kw: float | None = None
    efficiency: float | None = None
    approach_c: float | None = None
    variable_speed: bool = False

    sensible_heat_ratio: float = 0.75
    """显热比（显冷量 / 全冷量），用于把目录全热容量折算成送风温差。

    组合式空调机组的真值随盘管排数变化，由型号名 `N排` 后缀解析得到
    （见 `resolve_rated_spec_by_type`）；其余末端没有可依据的分排数数据，
    保持 `SENSIBLE_HEAT_RATIO` 兜底值。来源见 `docs/设备样本参数.md:255-256`。
    """

    origin: ParameterOrigin = "builtin_fallback"
    degraded_fields: tuple[str, ...] = ()

    @property
    def is_degraded(self) -> bool:
        return self.origin != "catalog" or bool(self.degraded_fields)


# 目录 role 词汇 -> Unity DeviceRole。两者是独立的两套命名，必须显式映射。
_CATALOG_ROLE_TO_DEVICE_ROLE: dict[str, DeviceRole] = {
    "cold_source": DeviceRole.CHILLER,
    "heat_source": DeviceRole.HEAT_SOURCE,
    "pump": DeviceRole.PUMP,
    "cooling_tower": DeviceRole.COOLING_TOWER,
    "terminal": DeviceRole.FCU,
}

# 目录字段名 -> RatedSpec 字段名。冷机的冷冻侧流量映射到通用的 flow_m3_h。
_FIELD_ALIASES: dict[str, str] = {
    "chilled_flow_m3_h": "flow_m3_h",
    "water_flow_m3_h": "flow_m3_h",
    "recommended_airflow_m3_h": "airflow_m3_h",
}

_DIRECT_FIELDS: frozenset[str] = frozenset(
    {
        "capacity_kw",
        "cop",
        "iplv",
        "power_kw",
        "flow_m3_h",
        "cooling_flow_m3_h",
        "head_m",
        "airflow_m3_h",
        "fan_power_kw",
        "efficiency",
        "approach_c",
    }
)

# 各角色参与物理计算的必需字段。缺失即计入 degraded_fields。
_REQUIRED_FIELDS: dict[DeviceRole, tuple[str, ...]] = {
    DeviceRole.CHILLER: ("capacity_kw", "cop", "flow_m3_h"),
    DeviceRole.HEAT_SOURCE: ("capacity_kw", "cop", "flow_m3_h", "power_kw"),
    DeviceRole.PUMP: ("flow_m3_h", "head_m", "power_kw"),
    DeviceRole.PUMP_CHILLED_WATER: ("flow_m3_h", "head_m", "power_kw"),
    DeviceRole.PUMP_COOLING_WATER: ("flow_m3_h", "head_m", "power_kw"),
    DeviceRole.COOLING_TOWER: ("flow_m3_h", "approach_c", "fan_power_kw"),
    DeviceRole.FCU: ("capacity_kw", "airflow_m3_h", "flow_m3_h"),
    DeviceRole.AHU: ("capacity_kw", "airflow_m3_h", "flow_m3_h"),
    DeviceRole.FRESH_AIR_UNIT: ("capacity_kw", "airflow_m3_h", "flow_m3_h"),
}

# 兜底常量。集中放在此处而不散落进物理计算，使用时会被标记为 builtin_fallback。
_BUILTIN_FALLBACK: dict[DeviceRole, dict[str, Any]] = {
    DeviceRole.CHILLER: {
        "capacity_kw": 340.0,
        "cop": 5.6,
        "flow_m3_h": 58.0,
        "cooling_flow_m3_h": 70.0,
        "power_kw": 61.0,
    },
    DeviceRole.HEAT_SOURCE: {
        "capacity_kw": 120.0,
        "cop": 3.2,
        "flow_m3_h": 10.3,
        "power_kw": 37.5,
    },
    DeviceRole.PUMP: {
        "flow_m3_h": 60.0,
        "head_m": 32.0,
        "power_kw": 7.5,
        "efficiency": 0.68,
        "variable_speed": True,
    },
    DeviceRole.COOLING_TOWER: {
        "capacity_kw": 350.0,
        "flow_m3_h": 70.0,
        "power_kw": 5.5,
        "fan_power_kw": 5.5,
        "approach_c": 4.0,
    },
    DeviceRole.FCU: {
        "capacity_kw": 8.0,
        "airflow_m3_h": 2500.0,
        "flow_m3_h": 3.2,
        "fan_power_kw": 0.55,
        "power_kw": 0.55,
    },
}
_BUILTIN_FALLBACK[DeviceRole.PUMP_CHILLED_WATER] = _BUILTIN_FALLBACK[DeviceRole.PUMP]
_BUILTIN_FALLBACK[DeviceRole.PUMP_COOLING_WATER] = _BUILTIN_FALLBACK[DeviceRole.PUMP]

# 组合式空调机组与新风机组的量级与风机盘管差一个数量级，必须各自兜底。
# 三项之间按下面的盘管换算关系自洽，不是各自取整。
_BUILTIN_FALLBACK[DeviceRole.AHU] = {
    "capacity_kw": 90.0,
    "airflow_m3_h": 15000.0,
    "flow_m3_h": 15.5,
    "fan_power_kw": 11.0,
    "power_kw": 11.0,
}
_BUILTIN_FALLBACK[DeviceRole.FRESH_AIR_UNIT] = {
    "capacity_kw": 40.0,
    "airflow_m3_h": 7000.0,
    "flow_m3_h": 6.9,
    "fan_power_kw": 4.0,
    "power_kw": 4.0,
}

# 盘管换算温差。两个数都由目录内 9 个风机盘管型号反解得到，不是独立引入的口径：
# 水侧 9/9 精确对上 ΔT=5.00K；风侧显热温差落在 11.85..14.49K，取均值 12.8K。
_COIL_WATER_DELTA_T_C = 5.0
_COIL_AIR_DELTA_T_C = 12.8

# 组合式空调机组型号名里的盘管排数后缀，如 `AHU-06-07-8排`。
# 排数是**唯一**记录显热比差异的地方：目录没有 `sensible_capacity_kw` 字段
# （`derived_fields` 只列了 `water_flow_m3_h` / `fan_power_kw`），
# 分排数显热比只存在于 `docs/设备样本参数.md:255-256` 的图上。
_AHU_ROW_SUFFIX_PATTERN = re.compile(r"(\d+)\s*排\s*$")


def _sensible_heat_ratio_for(role: DeviceRole, model_name: str) -> float | None:
    """按型号名解析显热比

    组合式空调机组的显热比随盘管排数变化（4/6/8 排 -> 0.845/0.730/0.658，
    见 `docs/设备样本参数.md:255-256`），排数只编码在型号名后缀里。
    非 AHU 角色（风机盘管、新风机组）没有分排数数据，返回 None 由调用方兜底。

    AHU 但型号名解析不出排数时**同样返回 None**：目录里确有这类条目
    （`K39G0608` 等 7 条 placement_only 摆放项，以及 Unity 同步来的裸型号），
    它们没有可依据的排数，只能退回兜底值——这种情况必须被标记出来，
    不能静默当成某个排数。
    """
    if role is not DeviceRole.AHU:
        return None
    from app.simcore.devices import AHU_SENSIBLE_HEAT_RATIO_BY_ROWS

    match = _AHU_ROW_SUFFIX_PATTERN.search(str(model_name))
    if match is None:
        return None
    return AHU_SENSIBLE_HEAT_RATIO_BY_ROWS.get(int(match.group(1)))


def resolve_rated_spec(device_id: str | int, model_id: str | int) -> RatedSpec:
    """按 Unity 设备身份解析额定参数

    `DeviceSpec` 只携带角色与几何，不含性能参数，因此需要再经设备目录取额定值。
    设备库查不到时退化为纯兜底结果。
    """
    spec = _unity_device_spec(device_id, model_id)
    if spec is None:
        return _builtin_spec(
            device_type="",
            model_name="",
            role=DeviceRole.MEASUREMENT,
            reason="unity_device_not_found",
        )
    return resolve_rated_spec_by_type(
        device_type=spec.device_type,
        model_name=spec.device_name,
        role_hint=spec.role,
    )


def resolve_rated_spec_by_type(
    device_type: str,
    model_name: str | None = None,
    role_hint: DeviceRole | None = None,
) -> RatedSpec:
    """按设备类型与型号解析额定参数

    降级顺序：型号精确命中 -> 类型通配/中位型号 -> 内置兜底常量。
    """
    exact = find_equipment_spec(device_type, model_name) if model_name else None
    aliases = {str(alias) for alias in exact.get("aliases", [])} if exact else set()
    if exact is not None and (
        str(exact.get("name")) == str(model_name) or str(model_name) in aliases
    ):
        return _from_catalog_item(
            device_type, exact, origin="catalog", role_hint=role_hint
        )

    fallback_item = exact if exact is not None else _type_default_item(device_type)
    if fallback_item is not None:
        return _from_catalog_item(
            device_type,
            fallback_item,
            origin="type_default",
            role_hint=role_hint,
            requested_model=model_name,
        )

    role = role_hint or _role_for_device_type(device_type) or DeviceRole.MEASUREMENT
    return _builtin_spec(
        device_type=device_type,
        model_name=model_name or "",
        role=role,
        reason="catalog_type_missing",
    )


def _unity_device_spec(device_id: str | int, model_id: str | int):
    try:
        return get_device_spec(int(device_id), int(model_id))
    except (TypeError, ValueError):
        return None


def _type_default_item(device_type: str) -> dict[str, Any] | None:
    """取该类型的代表型号

    优先使用目录中的 `name == "*"` 通配条目；否则按额定容量排序取中位型号，
    保证同一类型的默认选择是确定的。
    """
    wildcard = find_equipment_spec(device_type)
    if wildcard is not None:
        return wildcard

    # 摆放条目没有工程参数，不能作为类型默认代表型号。
    candidates = [
        item
        for item in load_equipment_catalog()
        if item.get("type") == device_type and not item.get("placement_only")
    ]
    if not candidates:
        return None
    candidates.sort(key=lambda item: (_sort_key(item), str(item.get("name", ""))))
    return candidates[len(candidates) // 2]


def _sort_key(item: Mapping[str, Any]) -> float:
    for field in ("capacity_kw", "flow_m3_h", "airflow_m3_h", "power_kw"):
        value = _as_float(item.get(field))
        if value is not None:
            return value
    return 0.0


def _from_catalog_item(
    device_type: str,
    item: Mapping[str, Any],
    origin: ParameterOrigin,
    role_hint: DeviceRole | None,
    requested_model: str | None = None,
) -> RatedSpec:
    role = (
        role_hint
        or _role_for_device_type(device_type)
        or _CATALOG_ROLE_TO_DEVICE_ROLE.get(str(item.get("role", "")))
        or DeviceRole.MEASUREMENT
    )

    values: dict[str, Any] = {}
    for key, raw in item.items():
        field = _FIELD_ALIASES.get(str(key), str(key))
        if field in _DIRECT_FIELDS and field not in values:
            number = _as_float(raw)
            if number is not None:
                values[field] = number

    spec = RatedSpec(
        device_type=device_type,
        model_name=str(item.get("name", "")),
        role=role,
        variable_speed=bool(item.get("variable_speed", False)),
        origin=origin,
        **values,
    )
    # 显热比按型号名后缀解析；AHU 解析不出排数时记一次降级标记。
    # `type_default` 的 AHU 代表型号通常也没有排数后缀，同样标记——
    # 它确实在用兜底显热比，按「降级透明」约定必须说出来。
    sensible_heat_ratio = _sensible_heat_ratio_for(role, spec.model_name)
    ratio_degraded: tuple[str, ...] = ()
    if sensible_heat_ratio is None:
        from app.simcore.devices import SENSIBLE_HEAT_RATIO

        sensible_heat_ratio = SENSIBLE_HEAT_RATIO
        if role is DeviceRole.AHU:
            ratio_degraded = ("default:sensible_heat_ratio",)
    spec = replace(spec, sensible_heat_ratio=sensible_heat_ratio)
    spec, filled = _fill_required_fields(spec, item)
    degraded = (*filled, *_missing_required(spec), *ratio_degraded)
    if origin != "catalog" and requested_model:
        degraded = ("model_not_in_catalog", *degraded)
    return replace(spec, degraded_fields=degraded) if degraded else spec


def _fill_required_fields(
    spec: RatedSpec, item: Mapping[str, Any]
) -> tuple[RatedSpec, tuple[str, ...]]:
    """补齐目录条目缺失的必需字段

    型号在目录里命中、但参与计算的字段是空的，这类条目原先只被标记而不补值。
    字段保持 `None` 会在 `terminal_state()` 被折成 0，末端于是静默零出力——
    这不是"参数不全"应有的表现，读数出得来但标记为降级才是。

    补齐优先级：同条目内可换算的物理量 -> 系列范围代表值 -> 角色兜底常量。
    每个补齐字段都带字段名标记，推导值与目录原值因此可分辨。

    `origin` 保持不变：型号确实精确命中了目录，把它降级等于谎报用的是别的型号。
    补齐属于"目录数据不全"，只记入 `degraded_fields`。
    """
    missing = _missing_required(spec)
    if not missing:
        return spec, ()

    # devices.py 依赖本模块，只能在函数内导入。与 `_role_for_device_type` 同一处置。
    from app.simcore.devices import AIR_KW_PER_M3H_K, SENSIBLE_HEAT_RATIO

    # 全热容量与风量之间的换算系数：目录容量为全热，只有显热部分体现为送风温差。
    #
    # 这里固定用模块兜底 `SENSIBLE_HEAT_RATIO` 而**不用** `spec.sensible_heat_ratio`：
    # 本系数要由风量反推一个缺字段的**全热容量**，是全热/风量之间的口径换算；
    # 而分排数显热比只在 `terminal_state()` 里把全热折成显热送风温差时才有意义。
    # 若用它做这层换算，同一型号的容量回填值会随排数变，等于让显热比去改全热容量，
    # 口径就串了（盘管排数不改变机组的总冷量，只改变显热占比）。
    air_kw_per_m3h = AIR_KW_PER_M3H_K * _COIL_AIR_DELTA_T_C / SENSIBLE_HEAT_RATIO
    water_kw_per_m3h = WATER_KW_PER_M3H_K * _COIL_WATER_DELTA_T_C
    fallback = _BUILTIN_FALLBACK.get(spec.role, {})
    filled: dict[str, tuple[float, str]] = {}

    def take(field: str, value: float | None, source: str) -> float | None:
        if value is None or not math.isfinite(value) or value <= 0.0:
            return None
        filled[field] = (value, source)
        return value

    def builtin(field: str) -> float | None:
        return take(field, _as_float(fallback.get(field)), "builtin_fallback")

    airflow = spec.airflow_m3_h
    capacity = spec.capacity_kw

    # 系列条目先取代表风量。这是一手数据，优先于任何换算。
    if airflow is None and "airflow_m3_h" in missing:
        airflow = take("airflow_m3_h", _series_airflow(item), "series_representative")

    # 再定容量：风量与水量都由它推出，避免用推导值继续推第三个量导致口径漂移。
    if capacity is None and "capacity_kw" in missing:
        capacity = take(
            "capacity_kw",
            airflow * air_kw_per_m3h if airflow is not None else None,
            "derived",
        ) or builtin("capacity_kw")

    if airflow is None and "airflow_m3_h" in missing:
        airflow = take(
            "airflow_m3_h",
            capacity / air_kw_per_m3h if capacity is not None else None,
            "derived",
        ) or builtin("airflow_m3_h")

    if spec.flow_m3_h is None and "flow_m3_h" in missing:
        take(
            "flow_m3_h",
            capacity / water_kw_per_m3h if capacity is not None else None,
            "derived",
        ) or builtin("flow_m3_h")

    # 其余必需字段没有可换算的物理关系，只能取角色常量。缺口仍然标记出来。
    for field in missing:
        if field not in filled:
            builtin(field)

    if not filled:
        return spec, ()
    # 标为 Any：dataclasses.replace 的 kwargs 要逐字段核对类型，
    # 补齐字段只有数值一种，统一成 float 会与 RatedSpec 的 bool/str 字段冲突。
    updates: dict[str, Any] = {field: value for field, (value, _) in filled.items()}
    return (
        replace(spec, **updates),
        tuple(
            f"{source}:{field}" for field, (_, source) in sorted(filled.items())
        ),
    )


def _series_airflow(item: Mapping[str, Any]) -> float | None:
    """系列条目的单机代表风量：风量范围的几何均值

    `configurable_series` 条目给的是整个产品系列的风量范围，不是单机额定值。
    几何均值对应系列的中位机型——产品系列按等比划分机架尺寸，`39HQ` 的 7 个
    alias 机型算出的级差是 1.992，第 4 个机型恰好落在几何均值上。

    不取 `airflow_max_m3_h`：那是系列上限，会造出比真机大一个量级的机组，
    比缺参数更危险，因为量级错了却不显眼。
    """
    low = _as_float(item.get("airflow_min_m3_h"))
    high = _as_float(item.get("airflow_max_m3_h"))
    if low is None or high is None or low <= 0.0 or high <= 0.0:
        return None
    return math.sqrt(low * high)


def _builtin_spec(
    device_type: str, model_name: str, role: DeviceRole, reason: str
) -> RatedSpec:
    values = dict(_BUILTIN_FALLBACK.get(role, {}))
    variable_speed = bool(values.pop("variable_speed", False))
    # 兜底路径同样解析排数后缀：型号名带 `N排` 时排数是已知的一手信息，
    # 没有理由因为容量参数缺失而把显热比一起丢掉。解析不到则保持
    # `RatedSpec` 默认值 0.75；本路径整条都已标记 `reason`，降级是显式的，
    # 不必再重复记一次 `default:sensible_heat_ratio`。
    sensible_heat_ratio = _sensible_heat_ratio_for(role, model_name)
    spec = RatedSpec(
        device_type=device_type,
        model_name=model_name,
        role=role,
        variable_speed=variable_speed,
        origin="builtin_fallback",
        **values,
    )
    if sensible_heat_ratio is not None:
        spec = replace(spec, sensible_heat_ratio=sensible_heat_ratio)
    return replace(spec, degraded_fields=(reason, *_missing_required(spec)))


def _missing_required(spec: RatedSpec) -> tuple[str, ...]:
    return tuple(
        field
        for field in _REQUIRED_FIELDS.get(spec.role, ())
        if getattr(spec, field, None) is None
    )


def _role_for_device_type(device_type: str) -> DeviceRole | None:
    from app.unity_catalog.roles import get_device_role

    return get_device_role(device_type)


def _as_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
