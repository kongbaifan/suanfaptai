"""Realtime session lifecycle and REST/WebSocket projections."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import WebSocket

from app.data.equipment import flow_for_device, head_for_device, role_for_device
from app.data.field_labels import keys_to_zh
from app.data.project_store import get_project
from app.room_bindings import raise_for_room_binding_errors, resolve_room_bindings
from app.schemas import (
    Issue,
    RatedDeviceView,
    RatedDevicesResult,
    RealtimeControlRequest,
    RealtimeControlsResult,
    RealtimeControlsResetRequest,
    RealtimeControlValue,
    RealtimeDeviceControls,
    RealtimeDeviceState,
    RealtimeHistoryResult,
    RealtimePipeState,
    RealtimePipeStatesResult,
    RealtimeRoomInitialCondition,
    RealtimeRoomState,
    RealtimeRoomStatesResult,
    RealtimeSimulationConfig,
    RealtimeSimulationControl,
    RealtimeSystemState,
    RealtimeUpdateRequest,
    SystemEvaluateRequest,
)
from app.simcore.controls import (
    clamp_controls,
    controls_for,
    default_controls,
    unknown_control_names,
)
from app.simcore.pipes import PipeFlowReading, pipe_flow_readings
from app.simcore.project import (
    ProjectSimulationLayout,
    build_project_layout,
    build_runtime_layout,
    room_instance_id,
)
from app.simcore.session import (
    CHILLER_CAPACITY_LOW_MULTIPLIER,
    PlantLayout,
    PlantSnapshot,
    RoomInitialState,
    SimulationConfig,
    SimulationMode,
    SimulationSession,
)
from app.unity_catalog import DeviceRole


HISTORY_DIR = Path(__file__).parent / "data" / "realtime_history"
MAX_HISTORY_POINTS = 1000
OFFLINE_SECONDS = 10.0

_states: dict[str, dict[str, RealtimeDeviceState]] = {}
_updated_at: dict[str, str] = {}
_alarms: dict[str, list[Issue]] = {}
_summary: dict[str, dict[str, Any]] = {}
_subscribers: dict[str, set[WebSocket]] = {}
_sim_tasks: dict[str, asyncio.Task] = {}
_sim_configs: dict[str, RealtimeSimulationConfig] = {}
_sessions: dict[str, SimulationSession] = {}
_layouts: dict[str, ProjectSimulationLayout] = {}
_control_values: dict[str, dict[str, dict[str, float]]] = {}
_last_floor_states: dict[str, dict[str, dict[str, Any]]] = {}
_last_control_state: dict[str, dict[str, Any]] = {}


class RuntimeConfigValidationError(ValueError):
    def __init__(self, code: str, message: str, room_id: str | None = None) -> None:
        self.detail = {"code": code, "message": message}
        if room_id is not None:
            self.detail["room_id"] = room_id
        super().__init__(message)


class RuntimeConfigRequiresStopError(ValueError):
    detail = {
        "code": "RUNTIME_CONFIG_REQUIRES_STOP",
        "message": "Stop the simulation before changing its mode or initial room state.",
    }


class ProjectEditRequiresStopError(ValueError):
    """项目编辑会重建 session，运行中必须先停机。

    与 `RuntimeConfigRequiresStopError` 同为 409，但两者要能区分：改 mode 和改房间
    面积是不同的操作，客户端提示语不一样。
    """

    detail = {
        "code": "PROJECT_EDIT_REQUIRES_STOP",
        "message": "Stop the simulation before editing the project.",
    }


async def update_realtime_state(request: RealtimeUpdateRequest) -> RealtimeSystemState:
    now = _now()
    project_states = _states.setdefault(request.project_id, {})
    for state in request.states:
        state.project_id = request.project_id
        state.timestamp = state.timestamp or now
        project_states[state.device_id] = state
    _updated_at[request.project_id] = now
    system_state = _build_system_state(request.project_id)
    _append_history(request.project_id, system_state)
    system_state.history_count = len(_read_history(request.project_id))
    await _broadcast(
        request.project_id, "device_state_updated", system_state.model_dump()
    )
    return system_state


def get_realtime_state(project_id: str) -> RealtimeSystemState:
    return _build_system_state(project_id)


def get_realtime_history(project_id: str) -> RealtimeHistoryResult:
    return RealtimeHistoryResult(
        project_id=project_id, points=_read_history(project_id)
    )


def set_runtime_controls(
    project_id: str, request: RealtimeControlRequest
) -> RealtimeControlsResult:
    record = get_project(project_id)
    # Controls are part of the realtime boundary.  Use the same tolerant
    # runtime layout as start/tick so a pre-start control read or write cannot
    # hide incomplete-topology diagnostics from the first published snapshot.
    session = _ensure_session(
        project_id, record.project, tolerate_incomplete=True
    )
    node = next(
        (
            candidate
            for candidate in session.layout.devices
            if candidate.instance_id == request.device_instance_id
        ),
        None,
    )
    if node is None:
        raise KeyError(request.device_instance_id)
    unknown = unknown_control_names(node.rated, request.values, node.quantity)
    if unknown:
        raise ValueError(f"Unsupported controls: {', '.join(unknown)}")
    accepted = session.set_controls(request.device_instance_id, request.values)
    _control_values.setdefault(project_id, {}).setdefault(
        request.device_instance_id, {}
    ).update(accepted)
    return get_runtime_controls(project_id)


def reset_runtime_controls(
    project_id: str, request: RealtimeControlsResetRequest
) -> RealtimeControlsResult:
    record = get_project(project_id)
    session = _ensure_session(
        project_id, record.project, tolerate_incomplete=True
    )
    instance_id = request.device_instance_id
    session.reset_controls(instance_id)
    stored = _control_values.get(project_id)
    if instance_id is None:
        _control_values.pop(project_id, None)
    elif stored is not None:
        stored.pop(instance_id, None)
        if not stored:
            _control_values.pop(project_id, None)
    return get_runtime_controls(project_id)


def get_runtime_controls(project_id: str) -> RealtimeControlsResult:
    record = get_project(project_id)
    session = _ensure_session(
        project_id, record.project, tolerate_incomplete=True
    )
    devices: dict[str, RealtimeDeviceControls] = {}
    for node in session.layout.devices:
        current = session.controls[node.instance_id]
        devices[node.instance_id] = RealtimeDeviceControls(
            model_name=node.rated.model_name,
            control_source=session.control_source(node.instance_id),
            controls={
                spec.name: RealtimeControlValue(
                    label=spec.label,
                    kind=spec.kind,
                    unit=spec.unit,
                    min=spec.minimum,
                    max=spec.maximum,
                    step=spec.step,
                    current=current[spec.name],
                )
                for spec in controls_for(node.rated, node.quantity)
            },
        )
    return RealtimeControlsResult(project_id=project_id, devices=devices)


def get_current_control_snapshot(
    project_id: str, layout: PlantLayout
) -> dict[str, dict[str, float]]:
    """Return copied defaults plus saved user overrides without touching a session."""
    stored = _control_values.get(project_id, {})
    snapshot: dict[str, dict[str, float]] = {}
    for node in layout.devices:
        values = default_controls(node.rated, node.quantity)
        values.update(
            clamp_controls(
                node.rated,
                dict(stored.get(node.instance_id, {})),
                node.quantity,
            )
        )
        snapshot[node.instance_id] = values
    return snapshot


def get_rated_devices(project_id: str) -> RatedDevicesResult:
    record = get_project(project_id)
    resolved = build_project_layout(record.project)
    devices = []
    for device in record.project.devices:
        rated = resolved.rated_by_device[device.id]
        devices.append(
            RatedDeviceView(
                device_instance_id=device.id,
                device_type=rated.device_type,
                model_name=rated.model_name,
                role=rated.role.value,
                quantity=device.quantity,
                source=rated.origin,
                degraded_fields=list(rated.degraded_fields),
                values=keys_to_zh(
                    {
                        "capacity_kw": rated.capacity_kw,
                        "cop": rated.cop,
                        "iplv": rated.iplv,
                        "power_kw": rated.power_kw,
                        "flow_m3_h": rated.flow_m3_h,
                        "cooling_flow_m3_h": rated.cooling_flow_m3_h,
                        "head_m": rated.head_m,
                        "airflow_m3_h": rated.airflow_m3_h,
                        "fan_power_kw": rated.fan_power_kw,
                        "efficiency": rated.efficiency,
                        "approach_c": rated.approach_c,
                        "variable_speed": rated.variable_speed,
                    }
                ),
            )
        )
    return RatedDevicesResult(project_id=project_id, devices=devices)


async def subscribe(project_id: str, websocket: WebSocket) -> None:
    await websocket.accept()
    _subscribers.setdefault(project_id, set()).add(websocket)
    await websocket.send_json(
        {"type": "connected", "data": get_realtime_state(project_id).model_dump()}
    )
    try:
        while True:
            await websocket.receive_text()
    except Exception:
        _subscribers.get(project_id, set()).discard(websocket)


async def start_simulation(
    project_id: str, config: RealtimeSimulationConfig | None = None
) -> RealtimeSimulationControl:
    record = get_project(project_id)
    # Unknown/duplicate/unsupported room references remain structural client
    # errors (HTTP 422).  Incomplete *topology*—missing links, sources, pumps,
    # or isolated pipes—is handled by the bounded runtime solver below.
    raise_for_room_binding_errors(resolve_room_bindings(record.project))
    if config is not None:
        set_simulation_config(project_id, config)
    config = get_simulation_config(project_id)
    if project_id in _sim_tasks and not _sim_tasks[project_id].done():
        return RealtimeSimulationControl(
            project_id=project_id,
            running=True,
            interval_seconds=config.interval_seconds,
        )
    # Runtime teaching deliberately tolerates an incomplete design.  The
    # strict project adapter remains the source of 422s for save/validate and
    # malformed control requests, while startup receives a bounded layout with
    # binding diagnostics attached to its first snapshot.
    session = _ensure_session(project_id, record.project, tolerate_incomplete=True)
    first_state = _simulate_system_tick(project_id, record.project, session, config)
    _store_system_state(project_id, first_state)
    await _broadcast(
        project_id, "simulation_status", {"project_id": project_id, "running": True}
    )
    await _broadcast(project_id, "system_tick", first_state.model_dump())
    task = asyncio.create_task(_simulation_loop(project_id))
    _sim_tasks[project_id] = task
    return RealtimeSimulationControl(
        project_id=project_id,
        running=True,
        interval_seconds=config.interval_seconds,
    )


async def stop_simulation(project_id: str) -> RealtimeSimulationControl:
    # Validate the project before broadcasting a status event or returning success.
    get_project(project_id)
    task = _sim_tasks.get(project_id)
    was_running = task is not None
    if task:
        if not task.done():
            task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception:
            pass
    _sim_tasks.pop(project_id, None)
    if was_running:
        session = _sessions.get(project_id)
        if session is not None:
            session.reset_controls()
        _control_values.pop(project_id, None)
    await _broadcast(
        project_id, "simulation_status", {"project_id": project_id, "running": False}
    )
    return RealtimeSimulationControl(project_id=project_id, running=False)


def invalidate_project_runtime_caches(project_id: str) -> None:
    """Drop caches derived from project data so the next read rebuilds them.

    房间面积、设备和管路都在 layout 构建时读取一次，改完项目不清缓存就会继续用旧
    布局推进仿真。派生投影同理会留下已删除的设备和楼层；手动控制值只保留仍存在的
    设备，否则 `_ensure_session` 重放时会对未知设备抛 KeyError；房间初始条件同理，
    否则 `SimulationSession` 会对未知房间抛 ValueError。

    `_updated_at` 必须跟 `_states` 一起清：设备读数清空后仍留旧时间戳，
    `/realtime/state` 会报出一个"刚刚更新"的空快照。仿真任务与 WebSocket 订阅者
    则保留——编辑项目不该中断正在跑的仿真，也不该踢掉已连接的前端。
    """
    _sessions.pop(project_id, None)
    _layouts.pop(project_id, None)
    for store in (
        _states,
        _updated_at,
        _alarms,
        _summary,
        _last_floor_states,
        _last_control_state,
    ):
        store.pop(project_id, None)
    try:
        project = get_project(project_id).project
    except FileNotFoundError:
        _control_values.pop(project_id, None)
        _sim_configs.pop(project_id, None)
        return

    stored = _control_values.get(project_id)
    if stored is not None:
        known_devices = {device.id for device in project.devices}
        for instance_id in [key for key in stored if key not in known_devices]:
            del stored[instance_id]
        if not stored:
            _control_values.pop(project_id, None)

    config = _sim_configs.get(project_id)
    if config is not None:
        known_rooms = {
            room_instance_id(room, index) for index, room in enumerate(project.rooms)
        }
        kept = [
            condition
            for condition in config.room_initial_conditions
            if condition.room_id in known_rooms
        ]
        if len(kept) != len(config.room_initial_conditions):
            _sim_configs[project_id] = config.model_copy(
                update={"room_initial_conditions": kept}
            )


async def cleanup_project_runtime(project_id: str) -> None:
    """Stop project activity and remove only that project's runtime artifacts."""
    await stop_simulation(project_id)
    for store in (
        _states,
        _updated_at,
        _alarms,
        _summary,
        _subscribers,
        _sim_configs,
        _sessions,
        _layouts,
        _control_values,
        _last_floor_states,
        _last_control_state,
    ):
        store.pop(project_id, None)
    path = _history_path(project_id)
    if path.exists():
        path.unlink()


def is_simulation_running(project_id: str) -> bool:
    task = _sim_tasks.get(project_id)
    return bool(task and not task.done())


def set_simulation_config(
    project_id: str, config: RealtimeSimulationConfig
) -> RealtimeSimulationConfig:
    project = get_project(project_id).project
    normalized = _normalize_simulation_config(project, config)
    current = get_simulation_config(project_id)
    protected_changed = _protected_config_values(current) != _protected_config_values(
        normalized
    )
    if is_simulation_running(project_id) and protected_changed:
        raise RuntimeConfigRequiresStopError
    if protected_changed:
        _sessions.pop(project_id, None)
        _layouts.pop(project_id, None)
    _sim_configs[project_id] = normalized
    return normalized


def get_simulation_config(project_id: str) -> RealtimeSimulationConfig:
    return _sim_configs.get(project_id) or RealtimeSimulationConfig()


def get_room_states(project_id: str) -> RealtimeRoomStatesResult:
    record = get_project(project_id)
    project = record.project
    config = get_simulation_config(project_id)
    session = _sessions.get(project_id)
    if session is None:
        resolved = build_runtime_layout(project)
        session = SimulationSession(
            resolved.layout, _simulation_config(project, config)
        )
    snapshot = session.snapshot
    floor_names = {floor.id: floor.name or floor.id for floor in project.floors}
    rooms = []
    for index, room in enumerate(project.rooms):
        room_id = room_instance_id(room, index)
        reading = snapshot.rooms[room_id]
        floor_id = room.floor_id or "default_floor"
        rooms.append(
            RealtimeRoomState(
                room_id=room_id,
                name=room.name,
                floor_id=floor_id,
                floor_name=floor_names.get(floor_id, floor_id),
                temperature_c=round(reading.temperature_c, 2),
                target_temp_c=round(reading.target_temp_c, 2),
                relative_humidity_pct=round(reading.relative_humidity_pct, 2),
                target_humidity_pct=round(reading.target_humidity_pct, 2),
                airflow_m3_h=round(reading.airflow_m3_h, 3),
                cooling_kw=round(reading.cooling_kw, 3),
                heating_kw=round(reading.heating_kw, 3),
            )
        )
    return RealtimeRoomStatesResult(
        project_id=project_id,
        running=is_simulation_running(project_id),
        simcore_tick=snapshot.tick,
        rooms=rooms,
    )


def get_pipe_states(project_id: str) -> RealtimePipeStatesResult:
    """当前 tick 的逐管段读数

    与 `get_room_states` 同样是只读投影：仿真未启动时返回 tick-zero 快照，
    不创建 session、不追加历史、不广播 WebSocket。
    """
    record = get_project(project_id)
    project = record.project
    config = get_simulation_config(project_id)
    session = _sessions.get(project_id)
    if session is None:
        resolved = build_runtime_layout(project)
        session = SimulationSession(
            resolved.layout, _simulation_config(project, config)
        )
    snapshot = session.snapshot
    readings = pipe_flow_readings(
        project,
        _terminal_water_flows(session, snapshot),
        {"cooling_water": snapshot.cooling_flow_m3_h},
        _pump_device_ids(session),
    )
    pipes = [
        RealtimePipeState(
            pipe_id=reading.pipe_id,
            system=reading.system,
            pipe_type=reading.pipe_type,
            floor_id=reading.floor_id,
            diameter_mm=reading.diameter_mm,
            flow_m3_h=reading.flow_m3_h,
            velocity_m_s=reading.velocity_m_s,
            pressure_drop_pa=reading.pressure_drop_pa,
            head_m=reading.head_m,
            served_terminal_ids=list(reading.served_terminal_ids),
        )
        for reading in readings
    ]
    return RealtimePipeStatesResult(
        project_id=project_id,
        running=is_simulation_running(project_id),
        simcore_tick=snapshot.tick,
        pipes=pipes,
        total_flow_by_system_m3_h=_pipe_flow_by_system(readings),
        most_loaded_pipe_id=_most_loaded_pipe_id(readings),
    )


def _terminal_water_flows(
    session: SimulationSession, snapshot: PlantSnapshot
) -> dict[str, dict[str, float]]:
    """末端的实时水量，按介质分组

    末端读数里 `water_flow_m3_h` 只有一个值：当前模式用的那一侧。制冷时是冷冻水，
    制热时是热水，其余模式两侧都不走水。据此归到对应介质下，另一侧留空——空字典
    会让该介质的所有管流量算成 0，正是停机时该有的读数。
    """
    terminal_roles = {DeviceRole.FCU, DeviceRole.AHU, DeviceRole.FRESH_AIR_UNIT}
    flows = {
        node.instance_id: float(
            snapshot.devices[node.instance_id].values.get("water_flow_m3_h", 0.0)
        )
        for node in session.layout.devices
        if node.rated.role in terminal_roles
        and node.instance_id in snapshot.devices
    }
    if session.config.mode is SimulationMode.HEATING:
        return {"chilled_water": {}, "hot_water": flows}
    if session.config.mode is SimulationMode.COOLING:
        return {"chilled_water": flows, "hot_water": {}}
    return {"chilled_water": {}, "hot_water": {}}


def _pump_device_ids(session: SimulationSession) -> frozenset[str]:
    """水泵：管路常被泵断成两段，遍历必须穿过它才能算到泵上游那段管。"""
    return frozenset(
        node.instance_id
        for node in session.layout.devices
        if node.rated.role
        in {
            DeviceRole.PUMP,
            DeviceRole.PUMP_CHILLED_WATER,
            DeviceRole.PUMP_COOLING_WATER,
        }
    )


def _pipe_flow_by_system(readings: tuple[PipeFlowReading, ...]) -> dict[str, float]:
    """各系统的总流量：取该系统主管流量，主管缺失时退回最大管段流量。

    逐管相加会把同一股水在供回水管上数两遍，不能用来代表系统流量。
    """
    totals: dict[str, float] = {}
    for system in ("chilled_water", "cooling_water", "hot_water"):
        in_system = [reading for reading in readings if reading.system == system]
        if not in_system:
            continue
        mains = [
            reading.flow_m3_h
            for reading in in_system
            if reading.pipe_type in {"main_supply", "main_return"}
        ]
        totals[system] = round(
            max(mains) if mains else max(reading.flow_m3_h for reading in in_system),
            3,
        )
    return totals


def _most_loaded_pipe_id(readings: tuple[PipeFlowReading, ...]) -> str | None:
    """流速最高的管段：教学上最该看的那根，管径不足会先在这里显形。"""
    flowing = [reading for reading in readings if reading.flow_m3_h > 0.0]
    if not flowing:
        return None
    return max(flowing, key=lambda reading: reading.velocity_m_s).pipe_id


async def _simulation_loop(project_id: str) -> None:
    while True:
        config = get_simulation_config(project_id)
        await asyncio.sleep(max(config.interval_seconds, 0.2))
        config = get_simulation_config(project_id)
        record = get_project(project_id)
        session = _ensure_session(
            project_id, record.project, tolerate_incomplete=True
        )
        system_state = _simulate_system_tick(
            project_id, record.project, session, config
        )
        _store_system_state(project_id, system_state)
        await _broadcast(project_id, "system_tick", system_state.model_dump())


def _ensure_session(
    project_id: str,
    project: SystemEvaluateRequest,
    *,
    tolerate_incomplete: bool = False,
) -> SimulationSession:
    existing = _sessions.get(project_id)
    if existing is not None:
        return existing
    resolved = (
        build_runtime_layout(project)
        if tolerate_incomplete
        else build_project_layout(project)
    )
    config = get_simulation_config(project_id)
    session = SimulationSession(
        resolved.layout, _simulation_config(project, config)
    )
    for instance_id, values in _control_values.get(project_id, {}).items():
        session.set_controls(instance_id, values)
    _layouts[project_id] = resolved
    _sessions[project_id] = session
    return session


def _simulation_config(
    project: SystemEvaluateRequest, config: RealtimeSimulationConfig
) -> SimulationConfig:
    mode = {
        "cooling": SimulationMode.COOLING,
        "fault": SimulationMode.COOLING,
        "ventilation": SimulationMode.VENTILATION,
        "heating": SimulationMode.HEATING,
        "standby": SimulationMode.STANDBY,
    }[config.mode]
    return SimulationConfig(
        step_seconds=60.0,
        mode=mode,
        target_temp_c=config.target_temp_c,
        target_humidity_pct=config.target_humidity_pct,
        outdoor_base_temp_c=config.outdoor_base_temp_c,
        outdoor_humidity_pct=config.outdoor_relative_humidity_pct,
        initial_room_temp_c=config.initial_room_temp_c,
        room_initial_states=tuple(
            RoomInitialState(
                room_id=condition.room_id,
                temperature_c=condition.temperature_c,
                relative_humidity_pct=condition.relative_humidity_pct,
            )
            for condition in config.room_initial_conditions
        ),
        chiller_capacity_multiplier=(
            CHILLER_CAPACITY_LOW_MULTIPLIER
            if config.fault_mode == "chiller_capacity_low"
            else 1.0
        ),
        automatic_control=True,
    )


def _simulate_system_tick(
    project_id: str,
    project: SystemEvaluateRequest,
    session: SimulationSession,
    config: RealtimeSimulationConfig,
) -> RealtimeSystemState:
    session.update_runtime_config(_simulation_config(project, config))
    snapshot = session.step(_fault_controls(session, config))
    return _project_snapshot(project_id, project, session, snapshot, config)


def _fault_controls(
    session: SimulationSession, config: RealtimeSimulationConfig
) -> dict[str, dict[str, float]]:
    overrides: dict[str, dict[str, float]] = {}
    if config.fault_mode == "pump_low_flow":
        for node in session.layout.devices:
            if node.loop is not None or node.rated.role is DeviceRole.PUMP:
                overrides[node.instance_id] = {"speed_pct": 45.0}
    elif config.fault_mode == "terminal_capacity_low":
        for node in session.layout.devices:
            if node.rated.role in {
                DeviceRole.FCU,
                DeviceRole.AHU,
                DeviceRole.FRESH_AIR_UNIT,
            }:
                overrides[node.instance_id] = {
                    "valve_opening_pct": 55.0,
                    "fan_speed_pct": 65.0,
                }
    return overrides


def _project_snapshot(
    project_id: str,
    project: SystemEvaluateRequest,
    session: SimulationSession,
    snapshot: PlantSnapshot,
    config: RealtimeSimulationConfig,
) -> RealtimeSystemState:
    now = _now()
    layout = _layouts[project_id]
    devices: dict[str, RealtimeDeviceState] = {}
    for device in project.devices:
        reading = snapshot.devices[device.id]
        values: dict[str, float | str | bool | None] = dict(reading.values)
        values["role"] = _legacy_role(layout.rated_by_device[device.id].role)
        values["parameter_origin"] = reading.parameter_origin
        values["parameters_degraded"] = bool(reading.degraded_fields)
        values["control_source"] = session.control_source(device.id)
        enabled = (
            float(values.get("enabled", 1.0) or 0.0) >= 0.5
            and float(values.get("active_count", 1.0) or 0.0) >= 0.5
        )
        devices[device.id] = RealtimeDeviceState(
            project_id=project_id,
            device_id=device.id,
            timestamp=now,
            status="running" if enabled else "stopped",
            values=values,
        )

    alarms = list(layout.issues)
    alarms.extend(
        Issue(
            level="warning",
            code=f"SIM_{alarm.upper()}",
            message=alarm.replace("_", " "),
        )
        for alarm in snapshot.alarms
    )
    floor_states = _floor_states(project, snapshot, config)
    chiller_ratios = [
        float(state.values.get("load_ratio", 0.0) or 0.0)
        for state in devices.values()
        if state.values.get("role") == "cold_source"
    ]
    heat_source_ratios = [
        float(state.values.get("load_ratio", 0.0) or 0.0)
        for state in devices.values()
        if state.values.get("role") == "heat_source"
    ]
    pump_speeds = [
        float(state.values.get("speed_pct", 0.0) or 0.0)
        for state in devices.values()
        if state.values.get("role") == "pump"
    ]
    chiller_load_ratio = max(chiller_ratios, default=0.0)
    heat_source_load_ratio = max(heat_source_ratios, default=0.0)
    pump_speed_pct = sum(pump_speeds) / len(pump_speeds) if pump_speeds else 0.0
    control_state = {
        "mode": config.mode,
        "hour": round(snapshot.ambient.hour_of_day, 3),
        "target_temp_c": config.target_temp_c,
        "chiller_load_ratio": round(chiller_load_ratio, 3),
        "heat_source_load_ratio": round(heat_source_load_ratio, 3),
        "pump_speed_pct": round(pump_speed_pct, 1),
        "fault_mode": config.fault_mode or "none",
    }
    _last_floor_states[project_id] = floor_states
    _last_control_state[project_id] = control_state
    summary = {
        "device_count": len(devices),
        "running_count": sum(
            1 for state in devices.values() if state.status == "running"
        ),
        "alarm_count": len(alarms),
        "total_power_kw": round(snapshot.total_power_kw, 3),
        "status": "warning" if alarms else "ok",
        "mode": config.mode,
        "outdoor_temp_c": round(snapshot.ambient.dry_bulb_c, 2),
        "outdoor_relative_humidity_pct": round(
            snapshot.ambient.relative_humidity_pct, 2
        ),
        "average_room_temp_c": round(_average_room_temp(snapshot, config), 2),
        "target_room_temp_c": config.target_temp_c,
        "average_room_humidity_pct": round(_average_room_humidity(snapshot, config), 2),
        "target_room_humidity_pct": config.target_humidity_pct,
        "cooling_load_kw": round(snapshot.cooling_load_kw, 3),
        "heating_load_kw": round(snapshot.heating_load_kw, 3),
        "installed_cooling_capacity_kw": round(snapshot.installed_capacity_kw, 3),
        "installed_heating_capacity_kw": round(
            snapshot.installed_heating_capacity_kw, 3
        ),
        "chiller_load_ratio": round(chiller_load_ratio, 3),
        "pump_speed_pct": round(pump_speed_pct, 1),
        "total_flow_m3_h": round(
            snapshot.hot_water_flow_m3_h
            if config.mode == "heating"
            else snapshot.chilled_flow_m3_h,
            3,
        ),
        "chilled_water_flow_m3_h": round(snapshot.chilled_flow_m3_h, 3),
        "cooling_water_flow_m3_h": round(snapshot.cooling_flow_m3_h, 3),
        "hot_water_flow_m3_h": round(snapshot.hot_water_flow_m3_h, 3),
        "supply_water_temp_c": round(
            snapshot.hot_water_supply_temp_c
            if config.mode == "heating"
            else snapshot.supply_water_temp_c,
            2,
        ),
        "return_water_temp_c": round(
            snapshot.hot_water_return_temp_c
            if config.mode == "heating"
            else snapshot.return_water_temp_c,
            2,
        ),
        "hot_water_supply_temp_c": round(snapshot.hot_water_supply_temp_c, 2),
        "hot_water_return_temp_c": round(snapshot.hot_water_return_temp_c, 2),
        "delta_t_c": round(
            snapshot.hot_water_supply_temp_c - snapshot.hot_water_return_temp_c
            if config.mode == "heating"
            else snapshot.return_water_temp_c - snapshot.supply_water_temp_c,
            2,
        ),
        "condenser_water_temp_c": round(snapshot.condenser_water_temp_c, 2),
        "floor_states": floor_states,
        "control_state": control_state,
        "simcore_tick": snapshot.tick,
    }
    return RealtimeSystemState(
        project_id=project_id,
        updated_at=now,
        devices=devices,
        alarms=alarms,
        summary=summary,
        last_result=None,
        history_count=len(_read_history(project_id)),
    )


def _normalize_simulation_config(
    project: SystemEvaluateRequest, config: RealtimeSimulationConfig
) -> RealtimeSimulationConfig:
    known_ids = [room_instance_id(room, index) for index, room in enumerate(project.rooms)]
    by_id: dict[str, RealtimeRoomInitialCondition] = {}
    for condition in config.room_initial_conditions:
        if condition.room_id in by_id:
            raise RuntimeConfigValidationError(
                "RUNTIME_INITIAL_ROOM_DUPLICATE",
                f"Room {condition.room_id} has more than one initial condition.",
                condition.room_id,
            )
        if condition.room_id not in known_ids:
            raise RuntimeConfigValidationError(
                "RUNTIME_INITIAL_ROOM_UNKNOWN",
                f"Room {condition.room_id} does not exist in the project.",
                condition.room_id,
            )
        by_id[condition.room_id] = condition
    return config.model_copy(
        update={
            "room_initial_conditions": [
                by_id[room_id] for room_id in known_ids if room_id in by_id
            ]
        }
    )


def _protected_config_values(config: RealtimeSimulationConfig) -> tuple[Any, ...]:
    return (
        config.mode,
        config.initial_room_temp_c,
        tuple(
            (
                condition.room_id,
                condition.temperature_c,
                condition.relative_humidity_pct,
            )
            for condition in config.room_initial_conditions
        ),
    )


def _floor_states(
    project: SystemEvaluateRequest,
    snapshot: PlantSnapshot,
    config: RealtimeSimulationConfig,
) -> dict[str, dict[str, Any]]:
    grouped: dict[str, list[Any]] = {}
    for index, room in enumerate(project.rooms):
        reading = snapshot.rooms.get(room_instance_id(room, index))
        if reading is not None:
            grouped.setdefault(room.floor_id or "default_floor", []).append(reading)
    floor_names = {floor.id: floor.name or floor.id for floor in project.floors}
    floor_levels = {floor.id: floor.level for floor in project.floors}
    return {
        floor_id: {
            "floor_name": floor_names.get(floor_id, floor_id),
            "level": floor_levels.get(floor_id, 0),
            "room_temp_c": round(
                sum(room.temperature_c for room in rooms) / len(rooms), 2
            ),
            "target_temp_c": config.target_temp_c,
            # 房间读数是显热口径，加回潜热才与 summary 的 cooling_load_kw 同量纲；
            # 同一份载荷里两个同名字段若相差 25%，前端并列展示会自相矛盾。
            "cooling_load_kw": round(
                sum(room.cooling_kw + room.dehumidification_kw for room in rooms), 3
            ),
            "heating_load_kw": round(sum(room.heating_kw for room in rooms), 3),
        }
        for floor_id, rooms in grouped.items()
    }


def _average_room_temp(
    snapshot: PlantSnapshot, config: RealtimeSimulationConfig
) -> float:
    if not snapshot.rooms:
        return config.target_temp_c
    return sum(room.temperature_c for room in snapshot.rooms.values()) / len(
        snapshot.rooms
    )


def _average_room_humidity(
    snapshot: PlantSnapshot, config: RealtimeSimulationConfig
) -> float:
    if not snapshot.rooms:
        return config.target_humidity_pct
    return sum(room.relative_humidity_pct for room in snapshot.rooms.values()) / len(
        snapshot.rooms
    )


def _legacy_role(role: DeviceRole) -> str:
    if role is DeviceRole.CHILLER:
        return "cold_source"
    if role is DeviceRole.HEAT_SOURCE:
        return "heat_source"
    if role in {
        DeviceRole.PUMP,
        DeviceRole.PUMP_CHILLED_WATER,
        DeviceRole.PUMP_COOLING_WATER,
    }:
        return "pump"
    if role is DeviceRole.COOLING_TOWER:
        return "cooling_tower"
    if role in {DeviceRole.FCU, DeviceRole.AHU, DeviceRole.FRESH_AIR_UNIT}:
        return "terminal"
    return role.value


def _store_system_state(project_id: str, system_state: RealtimeSystemState) -> None:
    _states[project_id] = system_state.devices
    _updated_at[project_id] = system_state.updated_at or _now()
    _alarms[project_id] = system_state.alarms
    _summary[project_id] = system_state.summary
    _append_history(project_id, system_state)
    system_state.history_count = len(_read_history(project_id))


def _build_system_state(project_id: str) -> RealtimeSystemState:
    devices = _states.get(project_id, {})
    if _summary.get(project_id, {}).get("floor_states"):
        alarms = _alarms.get(project_id, [])
        summary = dict(_summary.get(project_id, {}))
    else:
        alarms = _evaluate_alarms(project_id, devices)
        summary = _build_summary(devices, alarms)
    _alarms[project_id] = alarms
    _summary[project_id] = summary
    return RealtimeSystemState(
        project_id=project_id,
        updated_at=_updated_at.get(project_id),
        devices=devices,
        alarms=alarms,
        summary=summary,
        last_result=None,
        history_count=len(_read_history(project_id)),
    )


def _evaluate_alarms(
    project_id: str, devices: dict[str, RealtimeDeviceState]
) -> list[Issue]:
    alarms: list[Issue] = []
    now = datetime.now(UTC)
    try:
        project = get_project(project_id).project
        device_specs = {device.id: device for device in project.devices}
    except FileNotFoundError:
        device_specs = {}

    for device_id, state in devices.items():
        age = _age_seconds(now, state.timestamp)
        if age is not None and age > OFFLINE_SECONDS:
            alarms.append(
                Issue(
                    level="warning",
                    code="DEVICE_OFFLINE",
                    message=f"Device {device_id} has no fresh realtime data.",
                    target=device_id,
                )
            )
        if state.status in {"alarm", "offline"}:
            alarms.append(
                Issue(
                    level="warning",
                    code="DEVICE_STATUS_ALARM",
                    message=f"Device {device_id} status is {state.status}.",
                    target=device_id,
                )
            )

        spec = device_specs.get(device_id)
        if spec and role_for_device(spec) == "pump":
            rated_flow = flow_for_device(spec)
            actual_flow = _float_value(state.values.get("flow_m3_h"))
            if (
                rated_flow > 0
                and actual_flow is not None
                and actual_flow < rated_flow * 0.35
            ):
                alarms.append(
                    Issue(
                        level="warning",
                        code="FLOW_LOW",
                        message=f"Pump {device_id} flow is below 35% of rated flow.",
                        target=device_id,
                    )
                )
            rated_head = head_for_device(spec)
            actual_head = _float_value(state.values.get("head_m"))
            if (
                rated_head > 0
                and actual_head is not None
                and actual_head < rated_head * 0.75
            ):
                alarms.append(
                    Issue(
                        level="warning",
                        code="HEAD_LOW",
                        message=f"Pump {device_id} head is below 75% of rated head.",
                        target=device_id,
                    )
                )

        supply = _float_value(state.values.get("supply_temp_c"))
        return_temp = _float_value(state.values.get("return_temp_c"))
        if supply is not None and return_temp is not None:
            delta_t = return_temp - supply
            if delta_t < 2.0 or delta_t > 8.0:
                alarms.append(
                    Issue(
                        level="warning",
                        code="DELTA_T_ABNORMAL",
                        message=f"Device {device_id} water delta-T is {delta_t:.1f} C.",
                        target=device_id,
                    )
                )
    return _dedupe_issues(alarms)


def _build_summary(
    devices: dict[str, RealtimeDeviceState], alarms: list[Issue]
) -> dict[str, Any]:
    total_power = 0.0
    running = 0
    for state in devices.values():
        if state.status == "running":
            running += 1
        total_power += _float_value(state.values.get("power_kw")) or 0.0
        total_power += _float_value(state.values.get("fan_power_kw")) or 0.0
    return {
        "device_count": len(devices),
        "running_count": running,
        "alarm_count": len(alarms),
        "total_power_kw": round(total_power, 3),
        "status": "warning" if alarms else "ok",
    }


async def _broadcast(project_id: str, event_type: str, data: dict[str, Any]) -> None:
    subscribers = list(_subscribers.get(project_id, set()))
    for websocket in subscribers:
        try:
            await websocket.send_json({"type": event_type, "data": data})
        except Exception:
            _subscribers.get(project_id, set()).discard(websocket)


def _append_history(project_id: str, state: RealtimeSystemState) -> None:
    points = _read_history(project_id)
    points.append(
        {
            "timestamp": state.updated_at or _now(),
            "summary": state.summary,
            "floor_states": state.summary.get(
                "floor_states", _last_floor_states.get(project_id, {})
            ),
            "control_state": state.summary.get(
                "control_state", _last_control_state.get(project_id, {})
            ),
            "alarms": [alarm.model_dump() for alarm in state.alarms],
            "devices": {
                key: value.model_dump() for key, value in state.devices.items()
            },
        }
    )
    points = points[-MAX_HISTORY_POINTS:]
    HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    _history_path(project_id).write_text(
        json.dumps(points, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _read_history(project_id: str) -> list[dict[str, Any]]:
    path = _history_path(project_id)
    if not path.exists():
        return []
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []


def _history_path(project_id: str) -> Path:
    safe_id = "".join(
        char for char in project_id if char.isalnum() or char in {"-", "_"}
    )
    return HISTORY_DIR / f"{safe_id}.json"


def _now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _age_seconds(now: datetime, timestamp: str | None) -> float | None:
    if not timestamp:
        return None
    try:
        parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except ValueError:
        return None
    return (now - parsed).total_seconds()


def _float_value(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _dedupe_issues(issues: list[Issue]) -> list[Issue]:
    deduped = []
    seen = set()
    for issue in issues:
        key = (issue.level, issue.code, issue.message, issue.target)
        if key not in seen:
            seen.add(key)
            deduped.append(issue)
    return deduped


def diagnostics_snapshot() -> dict[str, Any]:
    """实时仿真的进程内状态，供 /api/v1/diagnostics 只读汇总。

    只读已有注册表，不创建 session、不推进 tick、不触碰磁盘。
    项目集合取 session 与订阅者的并集：只连了 WS 还没启动仿真的项目也要可见。
    """
    project_ids = sorted(
        set(_sessions) | {pid for pid, sockets in _subscribers.items() if sockets}
    )
    projects: list[dict[str, Any]] = []
    for project_id in project_ids:
        session = _sessions.get(project_id)
        task = _sim_tasks.get(project_id)
        projects.append(
            {
                "project_id": project_id,
                "tick": session.snapshot.tick if session else 0,
                "running": task is not None and not task.done(),
                "subscribers": len(_subscribers.get(project_id, ())),
                "updated_at": _updated_at.get(project_id),
            }
        )
    return {
        "active_sessions": len(_sessions),
        "running_simulations": sum(1 for item in projects if item["running"]),
        "websocket_subscribers": sum(int(item["subscribers"]) for item in projects),
        "projects": projects,
    }
