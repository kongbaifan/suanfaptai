"""仿真会话

持有唯一的系统状态，按 tick 推进。REST 与 WS 两条链路共用同一个会话，
因此 Unity 与 Vue 看到的是同一份数据，而不是同一套公式算出的两份。

水侧与冷凝侧互为输入——冷机要知道冷凝温度才能算功率，冷却塔要知道排热量
才能算冷凝温度——故采用固定次数不动点迭代。不用残差判据是刻意的：
步数固定才能保证每 tick 耗时可预测，实时推送不会偶发卡顿。
"""

from __future__ import annotations

import math
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field, replace
from enum import Enum
from threading import RLock
from types import MappingProxyType
from typing import Any, Literal
from uuid import uuid4

from app.data.rules import rule_value
from app.schemas import RealtimeNetworkResult
from app.simcore.controls import clamp_controls, default_controls
from app.simcore.devices import (
    AIR_DENSITY_KG_M3,
    COIL_RATED_WATER_TEMP_C,
    SENSIBLE_HEAT_RATIO,
    ChillerState,
    CoolingTowerState,
    HeatSourceState,
    PumpState,
    TerminalState,
    chiller_state,
    coil_leaving_state,
    cooling_tower_state,
    heat_source_cooling_state,
    heat_source_state,
    is_pump,
    is_terminal,
    is_valve,
    pump_state,
    terminal_state,
    valve_state,
)
from app.simcore.hydronics import (
    HydronicBranch,
    HydronicDemand,
    HydronicLoop,
    HydronicThermalBranch,
    advance_temperature,
    allocate_branch_flows,
    mixed_return_temperature,
    normalized_resistance_index,
)
from app.simcore.psychrometrics import (
    DRY_AIR_CP_KJ_KG_K,
    LATENT_HEAT_KJ_KG,
)
from app.simcore.rated import RatedSpec
from app.simcore.room import (
    MAX_ROOM_RH_PCT,
    MIN_ROOM_RH_PCT,
    RoomBalance,
    RoomHumidityBalance,
    RoomThermalConfig,
    humidity_ratio_from_rh,
    rh_from_humidity_ratio,
    step_room_humidity,
    step_room_temperature,
)
from app.simcore.runtime_network import RuntimeNetworkLayout
from app.simcore.synchronization import session_locked
from app.simcore.weather import Ambient, ambient_at
from app.unity_catalog import DeviceRole

# 水侧/冷凝侧耦合的不动点迭代次数
FIXED_POINT_ITERATIONS = 3
# 冷量不足时供水温度的最大抬升
MAX_SUPPLY_TEMP_PENALTY_C = 6.0
# Equivalent loop-water inertia for the reduced-order teaching model.
WATER_TEMPERATURE_TIME_CONSTANT_SECONDS = 300.0
# Shared derate used by realtime fault injection and teaching scenarios.
CHILLER_CAPACITY_LOW_MULTIPLIER = 0.55

# Realtime automatic-control teaching constants. They are deliberately internal:
# tests constrain bounded direction and stability rather than these percentages.
AUTO_DEADBAND_C = 0.2
AUTO_FULL_DEMAND_C = 4.0
# 湿度需求的死区与满载区间，量纲为百分点。取值比温度宽——
# 相对湿度本身随室温摆动，死区过窄会让设备跟着湿度读数频繁调节。
#
# 死区决定稳态湿度能贴到目标多近：控制器把"距目标不足死区"视为已满足，
# 因此稳态湿度约等于 `目标 + 死区`。实测四层样例（目标 55%）：
#
#   死区 3.0 -> 稳态 57.9%，湿度单步变化最大值 0.955
#   死区 1.5 -> 稳态 56.3%，湿度单步变化最大值 0.362
#   死区 1.0 -> 稳态 56.0%，湿度单步变化最大值 0.342
#
# 3.0 是原先的取值，"过窄会频繁调节"的顾虑实测**不成立**：1.5 反而比 3.0
# 更平稳（摆幅 0.362 vs 0.955），因为更小的死区让控制器更早、更小幅度地
# 微调，而不是等到偏差累积后大幅动作。取 1.5 兼顾贴近目标与平稳性；
# 再收到 1.0 收益已很小而摆幅开始回升。
AUTO_DEADBAND_RH_PCT = 1.5
AUTO_FULL_DEMAND_RH_PCT = 15.0
# 为除湿允许的最大过冷幅度（℃）。
#
# 制冷除湿在物理上必须把送风压到室内露点以下，因此房间温度必然被带到目标
# 以下——模型没有再热盘管，这份过冷无法回收。给一个小带宽，让盘管在接近
# 目标温度时仍保持"湿态"继续除湿；没有它，控制器一到目标温度就收冷量，
# 盘管转为等湿降温，湿度只升不降。
#
# 该值配合"盘管处理混合风"模型生效（见 `coil_leaving_state`）：新风与回风
# 混合后过盘管，新风湿负荷无处可绕。带宽越大湿度越低，但室温也越低。
AUTO_DEHUMIDIFICATION_SUBCOOL_C = 2.0
AUTO_MIN_WATER_OUTPUT_PCT = 40.0
AUTO_MIN_AIR_OUTPUT_PCT = 35.0
AUTO_MAX_PCT_CHANGE_PER_TICK = 8.0
AUTO_MAX_TEMP_CHANGE_PER_TICK = 1.0

WaterLoop = Literal["chilled", "cooling", "hot"]
ControlSource = Literal["auto", "manual_override"]


class SimulationMode(str, Enum):
    COOLING = "cooling"
    HEATING = "heating"
    VENTILATION = "ventilation"
    STANDBY = "standby"


@dataclass(frozen=True, slots=True)
class DeviceNode:
    """参与仿真的一台设备

    `rated` 为选型锁定的固定参数；`water_loop` 标明水泵属于冷冻侧还是冷却侧，
    未标注时按角色推断。
    """

    instance_id: str
    rated: RatedSpec
    quantity: int = 1
    room_id: str | None = None
    water_loop: WaterLoop | None = None
    served_room_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.quantity < 1:
            raise ValueError("device quantity must be positive")
        if len(set(self.served_room_ids)) != len(self.served_room_ids):
            raise ValueError("served room ids must be unique")

    @property
    def effective_room_ids(self) -> tuple[str, ...]:
        if self.served_room_ids:
            return self.served_room_ids
        if self.room_id is not None:
            return (self.room_id,)
        return ()

    @property
    def loop(self) -> WaterLoop | None:
        if self.water_loop is not None:
            return self.water_loop
        if self.rated.role is DeviceRole.PUMP_CHILLED_WATER:
            return "chilled"
        if self.rated.role is DeviceRole.PUMP_COOLING_WATER:
            return "cooling"
        return None


@dataclass(frozen=True, slots=True)
class RoomNode:
    room_id: str
    name: str = ""
    thermal: RoomThermalConfig = field(default_factory=RoomThermalConfig)
    design_cooling_kw: float = 0.0
    design_heating_kw: float = 0.0
    fresh_air_m3_h: float = 0.0
    initial_relative_humidity_pct: float = 50.0

    def __post_init__(self) -> None:
        if (
            not math.isfinite(self.initial_relative_humidity_pct)
            or not MIN_ROOM_RH_PCT
            <= self.initial_relative_humidity_pct
            <= MAX_ROOM_RH_PCT
        ):
            raise ValueError(
                f"room humidity must be finite and in "
                f"[{MIN_ROOM_RH_PCT:g}, {MAX_ROOM_RH_PCT:g}]"
            )


@dataclass(frozen=True, slots=True)
class PlantLayout:
    devices: tuple[DeviceNode, ...] = ()
    rooms: tuple[RoomNode, ...] = ()
    hydronic_branches: tuple[HydronicBranch, ...] = ()
    modeled_water_loops: frozenset[WaterLoop] = frozenset()
    # None is reserved for explicitly assembled device-model test benches.
    # Every project DTO gets a compiled network, including incomplete projects.
    network: RuntimeNetworkLayout | None = None


@dataclass(frozen=True, slots=True)
class RoomInitialState:
    room_id: str
    temperature_c: float
    relative_humidity_pct: float

    def __post_init__(self) -> None:
        if not self.room_id:
            raise ValueError("room initial state requires a room id")
        if not math.isfinite(self.temperature_c) or not 10.0 <= self.temperature_c <= 40.0:
            raise ValueError("room initial temperature must be finite and in [10, 40]")
        if (
            not math.isfinite(self.relative_humidity_pct)
            or not MIN_ROOM_RH_PCT <= self.relative_humidity_pct <= MAX_ROOM_RH_PCT
        ):
            raise ValueError(
                f"room initial humidity must be finite and in "
                f"[{MIN_ROOM_RH_PCT:g}, {MAX_ROOM_RH_PCT:g}]"
            )


@dataclass(frozen=True, slots=True)
class SimulationConfig:
    step_seconds: float = 60.0
    mode: SimulationMode = SimulationMode.COOLING
    target_temp_c: float = 26.0
    target_humidity_pct: float = 55.0
    outdoor_base_temp_c: float = 33.0
    outdoor_humidity_pct: float = 70.0
    initial_room_temp_c: float | None = None
    room_initial_states: tuple[RoomInitialState, ...] = ()
    fixed_hour_of_day: float | None = None
    design_cooling_load_kw: float | None = None
    chiller_capacity_multiplier: float = 1.0
    automatic_control: bool = False
    """冻结室外气象于指定时刻

    实时推送留空，让室外温度按日周期变化。稳态求解必须设定——
    气象随时间变化时房间温度会一直跟着摆动，不存在不动点。
    """

    def __post_init__(self) -> None:
        if not math.isfinite(self.step_seconds) or self.step_seconds <= 0.0:
            raise ValueError("step_seconds must be positive")
        for name, value, minimum, maximum in (
            ("target temperature", self.target_temp_c, 10.0, 40.0),
            ("target humidity", self.target_humidity_pct, 30.0, 80.0),
            ("outdoor humidity", self.outdoor_humidity_pct, 5.0, 99.0),
        ):
            if not math.isfinite(value) or not minimum <= value <= maximum:
                raise ValueError(
                    f"{name} must be finite and in [{minimum:g}, {maximum:g}]"
                )
        if not math.isfinite(self.outdoor_base_temp_c):
            raise ValueError("outdoor base temperature must be finite")
        if self.initial_room_temp_c is not None and (
            not math.isfinite(self.initial_room_temp_c)
            or not 10.0 <= self.initial_room_temp_c <= 40.0
        ):
            raise ValueError("initial room temperature must be finite and in [10, 40]")
        if (
            self.design_cooling_load_kw is not None
            and (
                not math.isfinite(self.design_cooling_load_kw)
                or self.design_cooling_load_kw < 0.0
            )
        ):
            raise ValueError("design cooling load must be finite and non-negative")
        if (
            not math.isfinite(self.chiller_capacity_multiplier)
            or not 0.0 < self.chiller_capacity_multiplier <= 1.0
        ):
            raise ValueError("chiller capacity multiplier must be finite and in (0, 1]")
        room_ids = [state.room_id for state in self.room_initial_states]
        if len(room_ids) != len(set(room_ids)):
            raise ValueError("room initial state ids must be unique")


@dataclass(frozen=True, slots=True)
class DeviceReading:
    """面向前端的设备读数"""

    instance_id: str
    role: str
    model_name: str
    values: Mapping[str, float]
    parameter_origin: str
    degraded_fields: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RoomReading:
    room_id: str
    name: str
    temperature_c: float
    target_temp_c: float
    relative_humidity_pct: float
    target_humidity_pct: float
    humidity_shortfall_pct: float
    """当前相对湿度超出目标的百分点（不低于 0）。

    大于自动控制死区且持续存在时，说明除湿能力不足以把湿度压到目标——
    模型无再热盘管、盘管潜热份额固定，需要再热或独立新风除湿才能解决。
    供前端与告警如实提示，而不是让目标湿度变成一个沉默的摆设。
    """

    airflow_m3_h: float
    cooling_kw: float
    """房间获得的显热冷量

    制冷时为末端全热能力的显热份额，潜热份额见 `dehumidification_kw`。
    需要末端全热口径请用 `PlantSnapshot.cooling_load_kw`，两者相差
    `SENSIBLE_HEAT_RATIO`，不可混用求和。
    """

    heating_kw: float
    envelope_kw: float
    internal_gain_kw: float
    fresh_air_latent_kw: float
    internal_latent_kw: float
    dehumidification_kw: float
    net_latent_kw: float


@dataclass(frozen=True, slots=True)
class RoomDelivery:
    airflow_m3_h: float = 0.0
    cooling_kw: float = 0.0
    heating_kw: float = 0.0
    fresh_air_cooling_kw: float = 0.0
    """其中由新风机组提供的冷量

    新风机组的任务是处理室外新风，其潜热能力应先抵扣新风湿负荷，
    不能与房间内区盘管的除湿量重复计入，故单独记账。
    """

    sensible_cooling_kw: float = 0.0
    latent_cooling_kw: float = 0.0
    """按盘管物理算出的、真正注入房间的显热与潜热冷量

    与 `cooling_kw`（目录全热、用于分摊与展示）不同，这两个量是"送风状态
    与室内状态之差 × 该房间风量"逐台机组累加的结果，可跨多台机组相加。
    一台房间挂多台盘管（如风机盘管 + 新风机组）时必须逐台累加，
    不能拿某一台的送风状态去乘房间总风量——那会凭空放大该机的处理能力。
    """


@dataclass(frozen=True, slots=True)
class PlantSnapshot:
    tick: int
    simulation_seconds: float
    ambient: Ambient
    supply_water_temp_c: float
    return_water_temp_c: float
    condenser_water_temp_c: float
    chilled_flow_m3_h: float
    cooling_flow_m3_h: float
    hot_water_flow_m3_h: float
    hot_water_supply_temp_c: float
    hot_water_return_temp_c: float
    cooling_load_kw: float
    heating_load_kw: float
    installed_capacity_kw: float
    installed_heating_capacity_kw: float
    total_power_kw: float
    devices: Mapping[str, DeviceReading]
    rooms: Mapping[str, RoomReading]
    alarms: tuple[str, ...]
    network: RealtimeNetworkResult | None = None
    applied_control_revision: int = 0
    control_sources: Mapping[str, ControlSource] = field(default_factory=dict)
    effective_controls: Mapping[str, Mapping[str, float]] = field(default_factory=dict)


class SimulationSession:
    """一个项目的仿真状态机"""

    def __init__(self, layout: PlantLayout, config: SimulationConfig | None = None):
        self.runtime_lock = RLock()
        self.session_id = uuid4().hex
        self.control_revision = 0
        self._applied_control_revision = 0
        self.layout = layout
        self.config = config or SimulationConfig()
        known_room_ids = {room.room_id for room in layout.rooms}
        unknown_room_ids = {
            state.room_id for state in self.config.room_initial_states
        } - known_room_ids
        if unknown_room_ids:
            raise ValueError(
                f"unknown room initial state ids: {', '.join(sorted(unknown_room_ids))}"
            )
        self._tick = 0
        self._simulation_seconds = 0.0
        self._controls: dict[str, dict[str, float]] = {
            node.instance_id: default_controls(node.rated, node.quantity)
            for node in layout.devices
        }
        self._manual_overrides: set[str] = set()
        self._bank_last_change_seconds: dict[str, float] = {}
        self._dehumidifying_circuits: set[str] = set()
        self._dehumidification_release_seconds: dict[str, float] = {}
        self._effective_controls: dict[str, dict[str, float]] = (
            self._mode_interlocked_controls(self._controls)
        )
        initial_by_room = {
            state.room_id: state for state in self.config.room_initial_states
        }
        fallback_temp = (
            self.config.initial_room_temp_c
            if self.config.initial_room_temp_c is not None
            else self.config.outdoor_base_temp_c - 3.0
        )
        self._room_temps: dict[str, float] = {
            room.room_id: (
                initial_by_room[room.room_id].temperature_c
                if room.room_id in initial_by_room
                else fallback_temp
            )
            for room in layout.rooms
        }
        self._room_humidity_pct: dict[str, float] = {
            room.room_id: (
                initial_by_room[room.room_id].relative_humidity_pct
                if room.room_id in initial_by_room
                else room.initial_relative_humidity_pct
            )
            for room in layout.rooms
        }
        initial_water_temp = self._average_room_temp()
        self._chilled_supply_temp_c = initial_water_temp
        self._hot_supply_temp_c = initial_water_temp
        self._network_supply_temps: dict[str, float] = {}
        self._network_air_room_flows: dict[str, dict[str, float]] = {}
        self._snapshot = self._build_snapshot(
            ambient=self._ambient(),
            solved=_SolveResult.empty(initial_water_temp),
            balances={},
            deliveries={},
        )

    @property
    def snapshot(self) -> PlantSnapshot:
        return self._snapshot

    @property
    def controls(self) -> Mapping[str, Mapping[str, float]]:
        return MappingProxyType(
            {
                key: MappingProxyType(dict(value))
                for key, value in self._controls.items()
            }
        )

    @property
    def effective_controls(self) -> Mapping[str, Mapping[str, float]]:
        return MappingProxyType(
            {
                key: MappingProxyType(dict(value))
                for key, value in self._effective_controls.items()
            }
        )

    def control_source(self, instance_id: str) -> ControlSource:
        if self._node(instance_id) is None:
            raise KeyError(instance_id)
        return "manual_override" if instance_id in self._manual_overrides else "auto"

    @session_locked
    def set_controls(
        self, instance_id: str, values: Mapping[str, float]
    ) -> dict[str, float]:
        """下发可调参数，越界值按可调范围钳制后生效"""
        node = self._node(instance_id)
        if node is None:
            raise KeyError(instance_id)
        accepted = clamp_controls(node.rated, dict(values), node.quantity)
        self._controls[instance_id].update(accepted)
        if accepted:
            self._manual_overrides.add(instance_id)
            self._bank_last_change_seconds.pop(instance_id, None)
            self.control_revision += 1
        return accepted

    @session_locked
    def reset_controls(self, instance_id: str | None = None) -> None:
        """Restore defaults and clear one or all device-level manual overrides."""
        nodes: tuple[DeviceNode, ...]
        if instance_id is not None:
            node = self._node(instance_id)
            if node is None:
                raise KeyError(instance_id)
            nodes = (node,)
        else:
            nodes = self.layout.devices
        for node in nodes:
            self._controls[node.instance_id] = default_controls(
                node.rated, node.quantity
            )
            self._manual_overrides.discard(node.instance_id)
            self._bank_last_change_seconds.pop(node.instance_id, None)
        if instance_id is None:
            self._dehumidifying_circuits.clear()
            self._dehumidification_release_seconds.clear()
        self._effective_controls = self._mode_interlocked_controls(self._controls)
        self.control_revision += 1

    @session_locked
    def update_runtime_config(self, config: SimulationConfig) -> None:
        """Replace runtime inputs without rebuilding dynamic session state."""
        if config.mode is not self.config.mode:
            raise ValueError("runtime mode changes require a new session")
        if config.initial_room_temp_c != self.config.initial_room_temp_c:
            raise ValueError("runtime initial temperature changes require a new session")
        if config.room_initial_states != self.config.room_initial_states:
            raise ValueError("runtime room initial state changes require a new session")
        if (config.target_temp_c, config.target_humidity_pct) != (
            self.config.target_temp_c, self.config.target_humidity_pct,
        ):
            self._dehumidifying_circuits.clear()
            self._dehumidification_release_seconds.clear()
        self.config = config
        self._effective_controls = self._mode_interlocked_controls(self._controls)

    @session_locked
    def step(
        self,
        transient_controls: Mapping[str, Mapping[str, float]] | None = None,
    ) -> PlantSnapshot:
        """推进一个时间步"""
        self._tick += 1
        self._simulation_seconds += self.config.step_seconds
        self._applied_control_revision = self.control_revision

        if self.config.automatic_control:
            self._apply_automatic_controls()
        effective = {
            instance_id: dict(values)
            for instance_id, values in self._controls.items()
        }
        for instance_id, values in (transient_controls or {}).items():
            node = self._node(instance_id)
            if node is None:
                continue
            effective[instance_id].update(
                clamp_controls(node.rated, dict(values), node.quantity)
            )
        self._effective_controls = self._mode_interlocked_controls(effective)
        ambient = self._ambient()
        solved = self._solve(ambient)
        deliveries = self._allocate_room_deliveries(solved, ambient)
        balances, humidity_balances = self._advance_rooms(ambient, deliveries)
        self._snapshot = self._build_snapshot(
            ambient, solved, balances, deliveries, humidity_balances
        )
        return self._snapshot

    def _ambient(self) -> Ambient:
        seconds = (
            self.config.fixed_hour_of_day * 3600.0
            if self.config.fixed_hour_of_day is not None
            else self._simulation_seconds
        )
        return ambient_at(
            seconds,
            self.config.outdoor_base_temp_c,
            self.config.outdoor_humidity_pct,
        )

    # ---------- 求解 ----------

    def _solve(self, ambient: Ambient) -> _SolveResult:
        if self.layout.network is not None:
            from app.simcore.connected import solve_connected

            return solve_connected(self, ambient)
        chillers = self._nodes_with_role(DeviceRole.CHILLER)
        heat_sources = self._nodes_with_role(DeviceRole.HEAT_SOURCE)
        towers = self._nodes_with_role(DeviceRole.COOLING_TOWER)
        terminals = [
            node for node in self.layout.devices if is_terminal(node.rated.role)
        ]
        valves = [node for node in self.layout.devices if is_valve(node.rated.role)]

        available_pumps = self._pump_states()
        available_chilled_flow = sum(
            state.flow_m3_h
            for node, state in available_pumps.items()
            if self._loop_of(node) == "chilled"
        )
        cooling_flow = sum(
            state.flow_m3_h
            for node, state in available_pumps.items()
            if self._loop_of(node) == "cooling"
        )
        available_hot_flow = sum(
            state.flow_m3_h
            for node, state in available_pumps.items()
            if self._loop_of(node) == "hot"
        )
        chilled_terminal_flows = self._allocate_terminal_flows(
            terminals, "chilled", available_chilled_flow
        )
        hot_terminal_flows = self._allocate_terminal_flows(
            terminals, "hot", available_hot_flow
        )
        topology_aware_hydronics = bool(self.layout.modeled_water_loops)
        chilled_flow = (
            sum(chilled_terminal_flows.values())
            if topology_aware_hydronics
            else available_chilled_flow
        )
        hot_flow = (
            sum(hot_terminal_flows.values())
            if topology_aware_hydronics
            else available_hot_flow
        )
        pumps = self._pump_states(
            {
                "chilled": chilled_flow,
                "cooling": cooling_flow,
                "hot": hot_flow,
            }
        )
        actual_terminal_flows: Mapping[str, float] | None = None
        if (
            self.config.mode is SimulationMode.COOLING
            and topology_aware_hydronics
        ):
            actual_terminal_flows = chilled_terminal_flows
        elif (
            self.config.mode is SimulationMode.HEATING
            and topology_aware_hydronics
        ):
            actual_terminal_flows = hot_terminal_flows

        # 制冷工况的热泵参与冷源池：四通阀换向后热泵即冷源（教学口径，
        # 三类热泵目录 capacity_kw 均为制冷量口径）。制热工况冷源仅冷机。
        cooling_sources = (
            [*chillers, *heat_sources]
            if self.config.mode is SimulationMode.COOLING
            else chillers
        )
        installed_capacity = sum(
            (self._operating_rated(node).capacity_kw or 0.0)
            for node in cooling_sources
            if self._enabled(node.instance_id)
        )
        installed_heating_capacity = sum(
            (self._operating_rated(node).capacity_kw or 0.0)
            for node in heat_sources
            if self._enabled(node.instance_id)
        )
        design_load_kw = (
            self.config.design_cooling_load_kw
            if (
                self.config.mode is SimulationMode.COOLING
                and self.config.design_cooling_load_kw is not None
            )
            else self._terminal_design_load(terminals)
        )
        design_heating_load_kw = (
            self._terminal_design_load(terminals)
            if self.config.mode is SimulationMode.HEATING
            else 0.0
        )
        setpoint = self._supply_setpoint(cooling_sources)
        hot_setpoint = self._hot_supply_setpoint(heat_sources)
        cooling_available = (
            self.config.mode is SimulationMode.COOLING
            and installed_capacity > 0.0
            and chilled_flow > 0.0
        )
        heating_available = (
            self.config.mode is SimulationMode.HEATING
            and installed_heating_capacity > 0.0
            and hot_flow > 0.0
        )
        room_reference_temp = self._average_room_temp()
        chilled_target = (
            setpoint + _shortfall_penalty(design_load_kw, installed_capacity)
            if cooling_available
            else room_reference_temp
        )
        hot_target = (
            hot_setpoint
            - _shortfall_penalty(
                design_heating_load_kw, installed_heating_capacity
            )
            if heating_available
            else room_reference_temp
        )
        self._chilled_supply_temp_c = advance_temperature(
            self._chilled_supply_temp_c,
            chilled_target,
            self.config.step_seconds,
            WATER_TEMPERATURE_TIME_CONSTANT_SECONDS,
        )
        self._hot_supply_temp_c = advance_temperature(
            self._hot_supply_temp_c,
            hot_target,
            self.config.step_seconds,
            WATER_TEMPERATURE_TIME_CONSTANT_SECONDS,
        )
        supply_water = self._chilled_supply_temp_c
        hot_supply_water = self._hot_supply_temp_c
        condenser_temp = ambient.wet_bulb_c + _average_approach(towers)

        terminal_states: dict[str, TerminalState] = {}
        chiller_states: dict[str, ChillerState] = {}
        heat_source_states: dict[str, HeatSourceState] = {}
        tower_states: dict[str, CoolingTowerState] = {}
        load_kw = 0.0
        heating_load_kw = 0.0

        for _ in range(FIXED_POINT_ITERATIONS):
            terminal_supply = (
                hot_supply_water
                if self.config.mode is SimulationMode.HEATING
                else supply_water
            )
            terminal_states = self._terminal_states(
                terminals,
                terminal_supply,
                actual_terminal_flows,
                conditioning_available=cooling_available or heating_available,
            )
            delivered_terminal_load = sum(
                state.cooling_kw for state in terminal_states.values()
            )
            delivered_heating_load = sum(
                state.heating_kw for state in terminal_states.values()
            )
            if (
                cooling_available
                and self.config.design_cooling_load_kw is not None
                and delivered_terminal_load > design_load_kw
            ):
                factor = design_load_kw / delivered_terminal_load
                terminal_states = {
                    instance_id: replace(
                        state, cooling_kw=state.cooling_kw * factor
                    )
                    for instance_id, state in terminal_states.items()
                }
                delivered_terminal_load = design_load_kw
            load_kw = delivered_terminal_load if cooling_available else 0.0
            heating_load_kw = (
                delivered_heating_load if heating_available else 0.0
            )

            chiller_states = self._chiller_states(
                chillers, load_kw, supply_water, condenser_temp, chilled_flow
            )
            if self.config.mode is SimulationMode.COOLING:
                # 制冷工况：热泵换向供冷，与冷机共用冷源容量份额分摊。
                # 制热工况保持原有 heating 语义（heating_load_kw 为 0）。
                joint_shares = self._cooling_source_shares(
                    chillers, heat_sources
                )
                chiller_states = self._chiller_states(
                    chillers,
                    load_kw,
                    supply_water,
                    condenser_temp,
                    chilled_flow,
                    shares=joint_shares,
                )
                heat_source_states = self._heat_source_cooling_states(
                    heat_sources,
                    load_kw,
                    supply_water,
                    ambient.dry_bulb_c,
                    chilled_flow,
                    shares=joint_shares,
                )
            else:
                heat_source_states = self._heat_source_states(
                    heat_sources,
                    heating_load_kw,
                    hot_supply_water,
                    ambient.dry_bulb_c,
                    hot_flow,
                )
            source_cooling_kw = sum(
                state.cooling_kw for state in chiller_states.values()
            ) + sum(
                state.cooling_kw for state in heat_source_states.values()
            )
            if delivered_terminal_load > source_cooling_kw >= 0.0:
                factor = source_cooling_kw / delivered_terminal_load
                terminal_states = {
                    instance_id: replace(
                        state, cooling_kw=state.cooling_kw * factor
                    )
                    for instance_id, state in terminal_states.items()
                }
                load_kw = source_cooling_kw
            source_heating_kw = sum(
                state.heating_kw for state in heat_source_states.values()
            )
            if delivered_heating_load > source_heating_kw >= 0.0:
                factor = (
                    source_heating_kw / delivered_heating_load
                    if delivered_heating_load > 0.0
                    else 0.0
                )
                terminal_states = {
                    instance_id: replace(
                        state, heating_kw=state.heating_kw * factor
                    )
                    for instance_id, state in terminal_states.items()
                }
                heating_load_kw = source_heating_kw
            heat_reject = sum(
                state.heat_reject_kw for state in chiller_states.values()
            ) + sum(
                state.heat_reject_kw for state in heat_source_states.values()
            )

            tower_states = self._tower_states(
                towers, heat_reject, ambient.wet_bulb_c, cooling_flow
            )
            active_tower_states = [
                state
                for node in towers
                if self._enabled(node.instance_id)
                for state in (tower_states[node.instance_id],)
            ]
            if active_tower_states:
                condenser_temp = max(
                    state.entering_water_temp_c for state in active_tower_states
                )
            else:
                # 无冷却塔（风冷或未搭全）时冷凝侧跟随室外干球
                condenser_temp = ambient.dry_bulb_c + 8.0

        load_kw = sum(state.cooling_kw for state in terminal_states.values())
        heating_load_kw = sum(
            state.heating_kw for state in terminal_states.values()
        )
        if topology_aware_hydronics:
            return_water = mixed_return_temperature(
                supply_water,
                (
                    HydronicThermalBranch(
                        instance_id,
                        state.water_flow_m3_h,
                        state.cooling_kw,
                    )
                    for instance_id, state in terminal_states.items()
                ),
                heating=False,
            )
            hot_return_water = mixed_return_temperature(
                hot_supply_water,
                (
                    HydronicThermalBranch(
                        instance_id,
                        state.water_flow_m3_h,
                        state.heating_kw,
                    )
                    for instance_id, state in terminal_states.items()
                ),
                heating=True,
            )
            chiller_states = {
                instance_id: replace(state, return_temp_c=return_water)
                for instance_id, state in chiller_states.items()
            }
            heat_source_states = {
                instance_id: replace(state, return_temp_c=hot_return_water)
                for instance_id, state in heat_source_states.items()
            }
        else:
            return_water = max(
                (state.return_temp_c for state in chiller_states.values()),
                default=supply_water,
            )
            hot_return_water = min(
                (state.return_temp_c for state in heat_source_states.values()),
                default=hot_supply_water,
            )
        return _SolveResult(
            supply_water_temp_c=supply_water,
            return_water_temp_c=return_water,
            condenser_temp_c=condenser_temp,
            chilled_flow_m3_h=chilled_flow,
            cooling_flow_m3_h=cooling_flow,
            hot_water_flow_m3_h=hot_flow,
            hot_water_supply_temp_c=hot_supply_water,
            hot_water_return_temp_c=hot_return_water,
            cooling_load_kw=load_kw,
            heating_load_kw=heating_load_kw,
            design_load_kw=design_load_kw,
            design_heating_load_kw=design_heating_load_kw,
            installed_capacity_kw=installed_capacity,
            installed_heating_capacity_kw=installed_heating_capacity,
            chillers=chiller_states,
            heat_sources=heat_source_states,
            pumps={node.instance_id: state for node, state in pumps.items()},
            towers=tower_states,
            terminals=terminal_states,
            valves={
                node.instance_id: valve_state(
                    self._control(node.instance_id, "opening_pct", 100.0) / 100.0,
                    self._enabled(node.instance_id),
                )
                for node in valves
            },
        )

    def _terminal_states(
        self,
        terminals: list[DeviceNode],
        supply_water_temp_c: float,
        actual_water_flows: Mapping[str, float] | None = None,
        *,
        conditioning_available: bool = True,
    ) -> dict[str, TerminalState]:
        water_conditioning_active = self.config.mode in {
            SimulationMode.COOLING,
            SimulationMode.HEATING,
        }
        states: dict[str, TerminalState] = {}
        for node in terminals:
            room_temp = self._terminal_room_reference_temp(node)
            effective_supply_temp = (
                supply_water_temp_c if conditioning_available else room_temp
            )
            opening = (
                self._control(node.instance_id, "valve_opening_pct", 100.0) / 100.0
            )
            fan_ratio = self._control(node.instance_id, "fan_speed_pct", 100.0) / 100.0
            if not water_conditioning_active:
                opening = 0.0
            state = terminal_state(
                self._operating_rated(node),
                valve_opening_ratio=opening,
                room_temp_c=room_temp,
                supply_water_temp_c=effective_supply_temp,
                fan_speed_ratio=fan_ratio,
                enabled=self._enabled(node.instance_id),
                actual_water_flow_m3_h=(
                    actual_water_flows.get(node.instance_id, 0.0)
                    if actual_water_flows is not None
                    else None
                ),
                # 进风含湿量：湿工况盘管的结露换热量取决于它。
                # 缺了它，`terminal_state` 只能用显热口径，潜热主导的新风机组
                # 会被低估到 1/3 左右，出力被截断后送风反而比房间更湿。
                room_humidity_ratio=self._terminal_entering_humidity_ratio(node),
            )
            if self.config.mode is SimulationMode.COOLING and state.heating_kw > 0.0:
                state = replace(state, heating_kw=0.0, supply_air_temp_c=room_temp)
            elif self.config.mode is SimulationMode.HEATING and state.cooling_kw > 0.0:
                state = replace(state, cooling_kw=0.0, supply_air_temp_c=room_temp)
            states[node.instance_id] = state
        return states

    def _terminal_design_load(self, terminals: list[DeviceNode]) -> float:
        """Return enabled terminal demand at the selected operating point.

        Capacity sizing is independent of the current room temperature. Using
        delivered coil cooling here lets an undersized plant clear its own
        deficit after the room state changes, even though the connected design
        load has not changed.
        """
        if self.config.mode not in {SimulationMode.COOLING, SimulationMode.HEATING}:
            return 0.0
        return sum(
            (self._operating_rated(node).capacity_kw or 0.0)
            * self._control(node.instance_id, "valve_opening_pct", 100.0)
            / 100.0
            * self._control(node.instance_id, "fan_speed_pct", 100.0)
            / 100.0
            for node in terminals
            if self._enabled(node.instance_id)
            and self._terminal_connected_to_active_loop(node.instance_id)
        )

    def _allocate_terminal_flows(
        self,
        terminals: list[DeviceNode],
        loop: HydronicLoop,
        available_flow_m3_h: float,
    ) -> dict[str, float]:
        if loop not in self.layout.modeled_water_loops:
            return {}
        mode_matches = (
            loop == "chilled" and self.config.mode is SimulationMode.COOLING
        ) or (loop == "hot" and self.config.mode is SimulationMode.HEATING)
        terminal_by_id = {node.instance_id: node for node in terminals}
        demands: list[HydronicDemand] = []
        for branch in self.layout.hydronic_branches:
            if branch.loop != loop:
                continue
            node = terminal_by_id.get(branch.terminal_instance_id)
            if node is None:
                continue
            rated_flow = self._operating_rated(node).flow_m3_h or 0.0
            opening = (
                self._control(node.instance_id, "valve_opening_pct", 100.0) / 100.0
            )
            flow_cap = (
                max(rated_flow, 0.0) * max(min(opening, 1.0), 0.0)
                if mode_matches and self._enabled(node.instance_id)
                else 0.0
            )
            resistance = normalized_resistance_index(branch.resistance_index)
            demands.append(
                HydronicDemand(
                    terminal_instance_id=node.instance_id,
                    flow_cap_m3_h=flow_cap,
                    weight=flow_cap / math.sqrt(resistance),
                )
            )
        return allocate_branch_flows(available_flow_m3_h, demands)

    def _terminal_connected_to_active_loop(self, instance_id: str) -> bool:
        if self.config.mode is SimulationMode.COOLING:
            loop: HydronicLoop = "chilled"
        elif self.config.mode is SimulationMode.HEATING:
            loop = "hot"
        else:
            return True
        if loop not in self.layout.modeled_water_loops:
            return True
        return any(
            branch.loop == loop and branch.terminal_instance_id == instance_id
            for branch in self.layout.hydronic_branches
        )

    def _chiller_states(
        self,
        chillers: list[DeviceNode],
        load_kw: float,
        supply_water_temp_c: float,
        condenser_temp_c: float,
        chilled_flow_m3_h: float,
        shares: Mapping[str, float] | None = None,
    ) -> dict[str, ChillerState]:
        if shares is None:
            shares = _capacity_shares(
                [
                    (node, self._operating_rated(node).capacity_kw or 0.0)
                    for node in chillers
                ],
                self._enabled,
            )
        states: dict[str, ChillerState] = {}
        for node in chillers:
            share = shares.get(node.instance_id, 0.0)
            states[node.instance_id] = chiller_state(
                self._operating_rated(node),
                load_kw=load_kw * share,
                supply_temp_c=supply_water_temp_c,
                condenser_temp_c=condenser_temp_c,
                chilled_flow_m3_h=chilled_flow_m3_h * share,
                enabled=self._enabled(node.instance_id),
            )
        return states

    def _cooling_source_shares(
        self,
        chillers: list[DeviceNode],
        heat_sources: list[DeviceNode],
    ) -> dict[str, float]:
        """制冷工况下冷机+热泵的联合容量份额

        与各自单独分摊不同：混装系统里负荷必须按同一分母分摊，
        否则冷机+热泵的份额和会大于 1，末端负荷被放大。
        """
        return _capacity_shares(
            [
                (node, self._operating_rated(node).capacity_kw or 0.0)
                for node in (*chillers, *heat_sources)
            ],
            self._enabled,
        )

    def _heat_source_cooling_states(
        self,
        heat_sources: list[DeviceNode],
        load_kw: float,
        supply_water_temp_c: float,
        outdoor_temp_c: float,
        chilled_flow_m3_h: float,
        shares: Mapping[str, float] | None = None,
    ) -> dict[str, HeatSourceState]:
        if shares is None:
            shares = _capacity_shares(
                [
                    (node, self._operating_rated(node).capacity_kw or 0.0)
                    for node in heat_sources
                ],
                self._enabled,
            )
        states: dict[str, HeatSourceState] = {}
        for node in heat_sources:
            share = shares.get(node.instance_id, 0.0)
            states[node.instance_id] = heat_source_cooling_state(
                self._operating_rated(node),
                load_kw=load_kw * share,
                supply_temp_c=supply_water_temp_c,
                outdoor_temp_c=outdoor_temp_c,
                chilled_flow_m3_h=chilled_flow_m3_h * share,
                enabled=self._enabled(node.instance_id),
            )
        return states

    def _heat_source_states(
        self,
        heat_sources: list[DeviceNode],
        load_kw: float,
        supply_water_temp_c: float,
        outdoor_temp_c: float,
        hot_water_flow_m3_h: float,
    ) -> dict[str, HeatSourceState]:
        shares = _capacity_shares(
            [
                (node, self._operating_rated(node).capacity_kw or 0.0)
                for node in heat_sources
            ],
            self._enabled,
        )
        states: dict[str, HeatSourceState] = {}
        for node in heat_sources:
            share = shares.get(node.instance_id, 0.0)
            states[node.instance_id] = heat_source_state(
                self._operating_rated(node),
                load_kw=load_kw * share,
                supply_temp_c=supply_water_temp_c,
                outdoor_temp_c=outdoor_temp_c,
                hot_water_flow_m3_h=hot_water_flow_m3_h * share,
                enabled=self._enabled(node.instance_id),
            )
        return states

    def _tower_states(
        self,
        towers: list[DeviceNode],
        heat_reject_kw: float,
        wet_bulb_c: float,
        cooling_flow_m3_h: float,
    ) -> dict[str, CoolingTowerState]:
        shares = _capacity_shares(
            [
                (
                    node,
                    self._operating_rated(node).capacity_kw
                    or self._operating_rated(node).flow_m3_h
                    or 0.0,
                )
                for node in towers
            ],
            self._enabled,
        )
        states: dict[str, CoolingTowerState] = {}
        for node in towers:
            share = shares.get(node.instance_id, 0.0)
            states[node.instance_id] = cooling_tower_state(
                self._operating_rated(node),
                fan_speed_ratio=self._control(node.instance_id, "fan_speed_pct", 100.0)
                / 100.0,
                heat_reject_kw=heat_reject_kw * share,
                wetbulb_temp_c=wet_bulb_c,
                water_flow_m3_h=cooling_flow_m3_h * share,
                enabled=self._enabled(node.instance_id),
            )
        return states

    def _pump_states(
        self,
        actual_loop_flows: Mapping[WaterLoop, float] | None = None,
    ) -> dict[DeviceNode, PumpState]:
        states: dict[DeviceNode, PumpState] = {}
        for node in self.layout.devices:
            if self._loop_of(node) is None:
                continue
            states[node] = pump_state(
                self._operating_rated(node),
                speed_ratio=self._control(node.instance_id, "speed_pct", 100.0) / 100.0,
                enabled=self._enabled(node.instance_id),
            )
        if actual_loop_flows is None:
            return states

        for loop, actual_loop_flow in actual_loop_flows.items():
            loop_nodes = sorted(
                (node for node in states if self._loop_of(node) == loop),
                key=lambda node: node.instance_id,
            )
            available_flow = sum(states[node].flow_m3_h for node in loop_nodes)
            target_flow = max(min(actual_loop_flow, available_flow), 0.0)
            assigned = 0.0
            for index, node in enumerate(loop_nodes):
                if available_flow <= 0.0:
                    device_flow = 0.0
                elif index == len(loop_nodes) - 1:
                    device_flow = target_flow - assigned
                else:
                    device_flow = target_flow * states[node].flow_m3_h / available_flow
                    assigned += device_flow
                states[node] = pump_state(
                    self._operating_rated(node),
                    speed_ratio=self._control(node.instance_id, "speed_pct", 100.0)
                    / 100.0,
                    enabled=self._enabled(node.instance_id),
                    actual_flow_m3_h=device_flow,
                )
        return states

    # ---------- 房间推进 ----------

    def _allocate_room_deliveries(
        self, solved: _SolveResult, ambient: Ambient
    ) -> dict[str, RoomDelivery]:
        """把各末端的冷热风分配到房间

        制冷时先按**设备**算出唯一的盘管出风状态，再把该送风状态带给其服务的
        每个房间；房间侧的除湿/降温由"送风与室内的状态差 × 该房间风量"决定。

        进风按设备类型区分（一台机组只有一种进风构成）：
        - **新风机组**：100% 室外新风（专用新风处理机）；
        - **组合式空调机组**：室外新风 + 房间回风混合（混风箱）；
        - **风机盘管**：100% 房间回风，不引入新风。

        房间所需的新风由**唯一一台**具备新风引入能力的设备承担（优先新风机组，
        其次组合式机组）。若让每台服务该房间的设备都各算一份房间新风，新风负荷
        会被按设备台数成倍放大——风机盘管 + 新风机组共服一室时实测翻了一倍。
        """
        deliveries: dict[str, RoomDelivery] = {}
        for node in self.layout.devices:
            state = solved.terminals.get(node.instance_id)
            weights = self._terminal_room_weights(node)
            if state is None or not weights:
                continue
            airflow = _allocate_conserved(state.airflow_m3_h, weights)
            cooling = _allocate_conserved(state.cooling_kw, weights)
            heating = _allocate_conserved(state.heating_kw, weights)
            is_fresh_air = node.rated.role is DeviceRole.FRESH_AIR_UNIT

            supply_temp_c: float | None = None
            supply_ratio: float | None = None
            if (
                self.config.mode is SimulationMode.COOLING
                and state.airflow_m3_h > 0.0
                and state.cooling_kw > 0.0
            ):
                mix_temp_c, mix_ratio = self._terminal_air_entering_state(node, state.airflow_m3_h)
                supply_temp_c, supply_ratio = coil_leaving_state(
                    mix_temp_c, mix_ratio, state.airflow_m3_h, state.cooling_kw
                )

                # 逐房间按"本房间分摊到的风量"折算该机组的实际贡献，
                # 这样多台机组（风机盘管 + 新风机组）的贡献可以直接相加。
                # 送风比室内更干/更冷时才是制冷除湿，反之为加热加湿，
                # 均取 0 由另一项负责，避免同一股风在两侧重复计入。
                for room_id, _ in weights:
                    if airflow[room_id] <= 0.0:
                        continue
                    room_temp_c = self._room_temp(room_id)
                    room_ratio = humidity_ratio_from_rh(
                        room_temp_c, self._room_humidity_pct[room_id]
                    )
                    air_mass_kg_s = airflow[room_id] * AIR_DENSITY_KG_M3 / 3600.0
                    # 显热/潜热按同一组焓常数拆分：两者之和恰好等于
                    # 送风与室内的比焓差，盘管冷量因此严格守恒。
                    device_sensible_kw = max(
                        air_mass_kg_s
                        * DRY_AIR_CP_KJ_KG_K
                        * (room_temp_c - supply_temp_c),
                        0.0,
                    )
                    # 潜热 = 含湿量差 × 汽化潜热。干盘管（含湿量不变）潜热为零，
                    # 湿盘管才除湿。新风加湿时为负值（送风含湿量高于室内）。
                    device_latent_kw = (
                        air_mass_kg_s
                        * (room_ratio - supply_ratio)
                        * LATENT_HEAT_KJ_KG
                    )
                    current = deliveries.get(room_id, RoomDelivery())
                    deliveries[room_id] = RoomDelivery(
                        airflow_m3_h=current.airflow_m3_h,
                        cooling_kw=current.cooling_kw,
                        heating_kw=current.heating_kw,
                        fresh_air_cooling_kw=current.fresh_air_cooling_kw,
                        sensible_cooling_kw=current.sensible_cooling_kw
                        + device_sensible_kw,
                        latent_cooling_kw=current.latent_cooling_kw
                        + device_latent_kw,
                    )

            for room_id, _ in weights:
                current = deliveries.get(room_id, RoomDelivery())
                deliveries[room_id] = replace(
                    current,
                    airflow_m3_h=current.airflow_m3_h + airflow[room_id],
                    cooling_kw=current.cooling_kw + cooling[room_id],
                    heating_kw=current.heating_kw + heating[room_id],
                    fresh_air_cooling_kw=(
                        current.fresh_air_cooling_kw
                        + (cooling[room_id] if is_fresh_air else 0.0)
                    ),
                )
        return deliveries

    def _fresh_air_handlers(self) -> dict[str, str]:
        """每个房间的室外新风由哪台设备承担（房间 id → 设备实例 id）

        只有具备新风引入能力的设备参与分配：**新风机组**（专用新风处理机）
        优先，其次是**组合式空调机组**（带混风箱）。风机盘管不引入新风，因此
        不承担；同一房间若有多台同类机组，按实例 id 排序取第一台，保证结果确定。

        分配是"独占"的：一个房间的新风只算给一台机组，避免多台设备重复计入。
        """
        role_priority = {
            DeviceRole.FRESH_AIR_UNIT: 0,
            DeviceRole.AHU: 1,
        }
        candidates = sorted(
            (
                node
                for node in self.layout.devices
                if node.rated.role in role_priority
            ),
            key=lambda node: (role_priority[node.rated.role], node.instance_id),
        )
        handlers: dict[str, str] = {}
        for node in candidates:
            for room_id in node.effective_room_ids:
                handlers.setdefault(room_id, node.instance_id)
        return handlers

    def _terminal_air_entering_state(self, node: DeviceNode, airflow_m3_h: float) -> tuple[float, float]:
        """One inlet state for coil capacity, delivered heat/moisture and display."""
        ambient = self._ambient()
        outdoor_ratio = humidity_ratio_from_rh(ambient.dry_bulb_c, ambient.relative_humidity_pct)
        if node.rated.role is DeviceRole.FRESH_AIR_UNIT:
            return ambient.dry_bulb_c, outdoor_ratio
        weights = self._terminal_room_weights(node)
        if not weights or airflow_m3_h <= 0.0:
            return self._terminal_room_reference_temp(node), self._terminal_entering_humidity_ratio(node)
        air = _allocate_conserved(airflow_m3_h, weights)
        total = math.fsum(air.values())
        return_temp = math.fsum(self._room_temp(key) * air[key] for key, _ in weights) / total
        return_ratio = math.fsum(humidity_ratio_from_rh(self._room_temp(key), self._room_humidity_pct[key]) * air[key] for key, _ in weights) / total
        handlers = self._fresh_air_handlers()
        room_by_id = {room.room_id: room for room in self.layout.rooms}
        fresh = min(math.fsum(room_by_id[key].fresh_air_m3_h for key, weight in weights if weight > 0.0 and handlers.get(key) == node.instance_id), airflow_m3_h)
        fraction = fresh / airflow_m3_h
        return (return_temp * (1.0 - fraction) + ambient.dry_bulb_c * fraction,
                return_ratio * (1.0 - fraction) + outdoor_ratio * fraction)

    def _terminal_room_reference_temp(self, node: DeviceNode) -> float:
        """末端盘管的**进风**温度

        专用新风机组处理 100% 室外新风，其进风是室外状态而非房间状态——
        这与 `_allocate_room_deliveries` 里 `coil_leaving_state` 对新风机组
        取 `ambient` 的判断一致（见该处的新风分支）。若这里返回室温，同一台
        机组会在"进风温度"与"出风状态"两处用上两股不同的风，湿工况能力被
        按室内温差算小，出力随之被截断。
        """
        if node.rated.role is DeviceRole.FRESH_AIR_UNIT:
            return self._ambient().dry_bulb_c
        weights = self._terminal_room_weights(node)
        if not weights:
            return self.config.target_temp_c
        return math.fsum(
            self._room_temp(room_id) * weight for room_id, weight in weights
        )

    def _terminal_entering_humidity_ratio(self, node: DeviceNode) -> float:
        """末端盘管的进风含湿量

        与 `_terminal_room_reference_temp` 同一口径：新风机组取室外状态
        （进风即室外新风），其余末端按服务房间加权平均取回风状态。

        含量纲：kg/kg 干空气。没有服务房间且非新风机组时返回 0（视为干空气，
        等价于不启用湿工况修正）。
        """
        if node.rated.role is DeviceRole.FRESH_AIR_UNIT:
            ambient = self._ambient()
            return humidity_ratio_from_rh(
                ambient.dry_bulb_c, ambient.relative_humidity_pct
            )
        weights = self._terminal_room_weights(node)
        if not weights:
            return 0.0
        return math.fsum(
            humidity_ratio_from_rh(
                self._room_temp(room_id), self._room_humidity_pct[room_id]
            )
            * weight
            for room_id, weight in weights
        )

    def _terminal_room_weights(
        self, node: DeviceNode
    ) -> tuple[tuple[str, float], ...]:
        actual = self._network_air_room_flows.get(node.instance_id)
        if actual:
            total = math.fsum(actual.values())
            if total > 0.0:
                return tuple((key, value / total) for key, value in sorted(actual.items()))
        return self._base_terminal_room_weights(node)

    def _base_terminal_room_weights(
        self, node: DeviceNode
    ) -> tuple[tuple[str, float], ...]:
        room_by_id = {room.room_id: room for room in self.layout.rooms}
        served_rooms = [
            room_by_id[room_id]
            for room_id in node.effective_room_ids
            if room_id in room_by_id
        ]
        if not served_rooms:
            return ()
        if self.config.mode is SimulationMode.HEATING:
            raw = [room.design_heating_kw for room in served_rooms]
        elif self.config.mode is SimulationMode.VENTILATION:
            raw = [room.fresh_air_m3_h for room in served_rooms]
        else:
            raw = [room.design_cooling_kw for room in served_rooms]
        finite = [
            value if math.isfinite(value) and value > 0.0 else 0.0
            for value in raw
        ]
        total = math.fsum(finite)
        if total <= 0.0:
            equal_weight = 1.0 / len(served_rooms)
            return tuple((room.room_id, equal_weight) for room in served_rooms)
        return tuple(
            (room.room_id, finite[index] / total)
            for index, room in enumerate(served_rooms)
        )

    def _advance_rooms(
        self, ambient: Ambient, deliveries: Mapping[str, RoomDelivery]
    ) -> tuple[dict[str, RoomBalance], dict[str, RoomHumidityBalance]]:
        # 新风承担者：与 `_allocate_room_deliveries` 用同一份映射，保证"谁处理新风"
        # 与"谁不用再往房间注入新风"判断一致。
        fresh_handler_by_room = self._fresh_air_handlers()
        design_gain_adjustments: dict[str, float] = {}
        if (
            self.config.mode is SimulationMode.COOLING
            and self.config.design_cooling_load_kw is not None
            and self.layout.rooms
        ):
            connected_terminals = [
                node
                for node in self.layout.devices
                if is_terminal(node.rated.role)
            ]
            effective_design_gain_kw = min(
                self.config.design_cooling_load_kw,
                self._terminal_design_load(connected_terminals),
            )
            target_gains = {
                room.room_id: max(
                    room.thermal.envelope_conductance_kw_k
                    * (ambient.dry_bulb_c - self.config.target_temp_c)
                    + room.thermal.internal_gain_kw,
                    0.0,
                )
                for room in self.layout.rooms
            }
            total_target_gain = math.fsum(target_gains.values())
            for room in self.layout.rooms:
                weight = (
                    target_gains[room.room_id] / total_target_gain
                    if total_target_gain > 0.0
                    else 1.0 / len(self.layout.rooms)
                )
                requested_gain = effective_design_gain_kw * weight
                modeled_gain = (
                    room.thermal.envelope_conductance_kw_k
                    * (ambient.dry_bulb_c - self.config.target_temp_c)
                    + room.thermal.internal_gain_kw
                )
                design_gain_adjustments[room.room_id] = (
                    requested_gain - modeled_gain
                )

        balances: dict[str, RoomBalance] = {}
        humidity_balances: dict[str, RoomHumidityBalance] = {}
        for room in self.layout.rooms:
            thermal = room.thermal
            adjustment_kw = design_gain_adjustments.get(room.room_id, 0.0)
            if adjustment_kw != 0.0:
                # The external design load replaces the target-condition total
                # room gain while preserving the normal envelope temperature slope.
                thermal = replace(
                    thermal,
                    equipment_w=thermal.equipment_w + adjustment_kw * 1000.0,
                )
            current_temp_c = self._room_temp(room.room_id)
            delivery = deliveries.get(room.room_id, RoomDelivery())

            # 各盘管已在设备级按"混合风过盘管"逐台累加出注入本房间的显热与
            # 潜热（见 `_allocate_room_deliveries`）。多台机组可直接相加。
            # 新风已随混合风穿过盘管，因此不再作为独立湿源注入，避免重复计账。
            supply_airflow_m3_h = delivery.airflow_m3_h
            fresh_air_m3_h = (
                0.0
                if self.config.mode is SimulationMode.STANDBY
                else min(room.fresh_air_m3_h, supply_airflow_m3_h)
            )
            if self.config.mode is SimulationMode.COOLING:
                sensible_cooling_kw = delivery.sensible_cooling_kw
                room_latent_cooling_kw = delivery.latent_cooling_kw
                if supply_airflow_m3_h <= 0.0 and delivery.cooling_kw > 0.0:
                    # 退化情形：末端有冷量却无送风量（没有可依托的盘管气流），
                    # 沿用按显热比拆分，避免这份冷量凭空消失。
                    sensible_cooling_kw = delivery.cooling_kw * SENSIBLE_HEAT_RATIO
                    room_latent_cooling_kw = delivery.cooling_kw * (
                        1.0 - SENSIBLE_HEAT_RATIO
                    )
            else:
                sensible_cooling_kw = delivery.cooling_kw
                room_latent_cooling_kw = 0.0

            # 新风已进盘管参与处理，不能再作为独立湿源注入房间（重复计账）。
            # 制冷时新风只有在**被某台设备承担**（进盘管处理）时才不注入房间。
            # 若房间没有任何具备新风引入能力的设备（既无新风机组也无组合式机组，
            # 例如只配风机盘管的系统），这份按人数算出的新风无处处理，仍按室外
            # 状态直接进入房间——负荷不能凭空消失，而且"没有新风处理设备"
            # 恰恰是应当暴露给学习者的结论。
            fresh_air_injection_m3_h = (
                0.0
                if (
                    self.config.mode is SimulationMode.COOLING
                    and fresh_handler_by_room.get(room.room_id) is not None
                )
                else fresh_air_m3_h
            )

            balance = step_room_temperature(
                current_temp_c=current_temp_c,
                outdoor_temp_c=ambient.dry_bulb_c,
                cooling_kw=sensible_cooling_kw,
                config=thermal,
                step_seconds=self.config.step_seconds,
                heating_kw=delivery.heating_kw,
            )
            self._room_temps[room.room_id] = balance.temperature_c
            balances[room.room_id] = balance
            humidity = step_room_humidity(
                current_rh_pct=self._room_humidity_pct[room.room_id],
                current_temp_c=current_temp_c,
                next_temp_c=balance.temperature_c,
                outdoor_temp_c=ambient.dry_bulb_c,
                outdoor_rh_pct=ambient.relative_humidity_pct,
                latent_cooling_kw=room_latent_cooling_kw,
                config=thermal,
                step_seconds=self.config.step_seconds,
                fresh_air_m3_h=fresh_air_injection_m3_h,
                fresh_air_latent_capacity_kw=0.0,
            )
            self._room_humidity_pct[room.room_id] = humidity.relative_humidity_pct
            humidity_balances[room.room_id] = humidity
        return balances, humidity_balances

    # ---------- 快照 ----------

    def _build_snapshot(
        self,
        ambient: Ambient,
        solved: _SolveResult,
        balances: Mapping[str, RoomBalance],
        deliveries: Mapping[str, RoomDelivery],
        humidity_balances: Mapping[str, RoomHumidityBalance] | None = None,
    ) -> PlantSnapshot:
        devices: dict[str, DeviceReading] = {}
        total_power = 0.0
        for node in self.layout.devices:
            values = solved.values_for(node.instance_id)
            if solved.network is not None and is_valve(node.rated.role):
                values["flow_m3_h"] = math.fsum(
                    item.flow_m3_h for item in solved.network.devices
                    if item.node_id == node.instance_id
                )
            for name, value in self._effective_controls.get(
                node.instance_id, {}
            ).items():
                values.setdefault(name, value)
            values["enabled"] = 1.0 if self._enabled(node.instance_id) else 0.0
            total_power += values.get("power_kw", 0.0) + values.get("fan_power_kw", 0.0)
            devices[node.instance_id] = DeviceReading(
                instance_id=node.instance_id,
                role=node.rated.role.value,
                model_name=node.rated.model_name,
                values=MappingProxyType(values),
                parameter_origin=node.rated.origin,
                degraded_fields=node.rated.degraded_fields,
            )

        rooms: dict[str, RoomReading] = {}
        for room in self.layout.rooms:
            balance = balances.get(room.room_id)
            humidity = (humidity_balances or {}).get(room.room_id)
            delivery = deliveries.get(room.room_id, RoomDelivery())
            rooms[room.room_id] = RoomReading(
                room_id=room.room_id,
                name=room.name or room.room_id,
                temperature_c=self._room_temp(room.room_id),
                target_temp_c=self.config.target_temp_c,
                relative_humidity_pct=self._room_humidity_pct[room.room_id],
                target_humidity_pct=self.config.target_humidity_pct,
                humidity_shortfall_pct=max(
                    0.0,
                    self._room_humidity_pct[room.room_id]
                    - self.config.target_humidity_pct,
                ),
                airflow_m3_h=delivery.airflow_m3_h,
                cooling_kw=balance.cooling_kw if balance else 0.0,
                heating_kw=balance.heating_kw if balance else 0.0,
                envelope_kw=balance.envelope_kw if balance else 0.0,
                internal_gain_kw=balance.internal_gain_kw if balance else 0.0,
                fresh_air_latent_kw=(
                    humidity.infiltration_latent_kw if humidity else 0.0
                ),
                internal_latent_kw=(
                    humidity.internal_latent_kw if humidity else 0.0
                ),
                dehumidification_kw=(
                    humidity.dehumidification_kw if humidity else 0.0
                ),
                net_latent_kw=humidity.net_latent_kw if humidity else 0.0,
            )

        network = solved.network
        if network is None and self.layout.network is not None:
            from app.simcore.connected import network_projection

            network = network_projection(self.layout.network)
        return PlantSnapshot(
            tick=self._tick,
            simulation_seconds=self._simulation_seconds,
            ambient=ambient,
            supply_water_temp_c=solved.supply_water_temp_c,
            return_water_temp_c=solved.return_water_temp_c,
            condenser_water_temp_c=solved.condenser_temp_c,
            chilled_flow_m3_h=solved.chilled_flow_m3_h,
            cooling_flow_m3_h=solved.cooling_flow_m3_h,
            hot_water_flow_m3_h=solved.hot_water_flow_m3_h,
            hot_water_supply_temp_c=solved.hot_water_supply_temp_c,
            hot_water_return_temp_c=solved.hot_water_return_temp_c,
            cooling_load_kw=solved.cooling_load_kw,
            heating_load_kw=solved.heating_load_kw,
            installed_capacity_kw=solved.installed_capacity_kw,
            installed_heating_capacity_kw=solved.installed_heating_capacity_kw,
            total_power_kw=total_power,
            devices=MappingProxyType(devices),
            rooms=MappingProxyType(rooms),
            alarms=self._alarms(solved),
            network=network,
            applied_control_revision=self._applied_control_revision,
            control_sources=MappingProxyType({node.instance_id: self.control_source(node.instance_id) for node in self.layout.devices}),
            effective_controls=self.effective_controls,
        )

    def _alarms(self, solved: _SolveResult) -> tuple[str, ...]:
        alarms: list[str] = []
        if self.config.mode is SimulationMode.COOLING:
            bound_terminals = {node.instance_id for node in self.layout.devices if node.effective_room_ids}
            missing_served_path = self.layout.network is not None and not any(
                path.system == "chilled_water" and path.target_id in bound_terminals
                for path in self.layout.network.paths
            )
            has_source = any(node.rated.role in {DeviceRole.CHILLER, DeviceRole.HEAT_SOURCE} for node in self.layout.devices)
            if solved.installed_capacity_kw <= 0.0 and self.layout.devices:
                alarms.append("no_cold_source")
            elif solved.design_load_kw > solved.installed_capacity_kw > 0.0:
                alarms.append("capacity_deficit")
            if (
                solved.chilled_flow_m3_h <= 0.0
                and (
                    solved.installed_capacity_kw > 0.0
                    or (has_source and missing_served_path)
                )
            ):
                alarms.append("no_chilled_flow")
            if solved.cooling_flow_m3_h <= 0.0 and solved.towers:
                alarms.append("no_cooling_flow")
            # 湿度目标不可达：盘管处理混合风、无再热盘管，当除湿能力不足以
            # 处理人员散湿与新风湿负荷（或多分区共用末端导致分配不均）时，
            # 湿度会持续偏高。如实告知需要再热或独立新风除湿，而不是让目标
            # 值静默失效。
            if any(
                self._room_humidity_pct[room.room_id]
                > self.config.target_humidity_pct + AUTO_DEADBAND_RH_PCT
                for room in self.layout.rooms
            ):
                alarms.append("humidity_target_unreachable")
        elif self.config.mode is SimulationMode.HEATING:
            if solved.installed_heating_capacity_kw <= 0.0 and self.layout.devices:
                alarms.append("no_heat_source")
            elif (
                solved.design_heating_load_kw
                > solved.installed_heating_capacity_kw
                > 0.0
            ):
                alarms.append("heating_capacity_deficit")
            if (
                solved.hot_water_flow_m3_h <= 0.0
                and solved.installed_heating_capacity_kw > 0.0
            ):
                alarms.append("no_hot_water_flow")
        if any(node.rated.is_degraded for node in self.layout.devices):
            alarms.append("degraded_parameters")
        return tuple(alarms)

    # ---------- 辅助 ----------

    def _apply_automatic_controls(self) -> None:
        self._update_dehumidification_requests()
        served_room_ids = {
            room_id
            for node in self.layout.devices
            if is_terminal(node.rated.role)
            for room_id in node.effective_room_ids
        }
        aggregate_demand = max(
            (
                self._room_demand(room_id)
                for room_id in (
                    served_room_ids
                    or {room.room_id for room in self.layout.rooms}
                )
            ),
            default=0.0,
        )
        for node in self.layout.devices:
            if node.instance_id in self._manual_overrides:
                continue
            role = node.rated.role
            if is_valve(role) and self.layout.network is not None:
                # Standalone valves have no implicit project-wide thermostat.
                # Their default is open; only an explicit command/fault closes
                # them. Terminal coil valves retain their automatic controller.
                continue
            demand = (
                max(
                    (
                        self._room_demand(room_id)
                        for room_id in node.effective_room_ids
                    ),
                    default=0.0,
                )
                if is_terminal(role)
                else aggregate_demand
            )
            if self.layout.network is not None and not is_terminal(role):
                system = "hot_water" if self.config.mode is SimulationMode.HEATING else "chilled_water"
                if self._loop_of(node) == "cooling" or role is DeviceRole.COOLING_TOWER:
                    system = "cooling_water"
                controlled_rooms = self.layout.network.control_room_ids.get((system, node.instance_id), ())
                demand = max((self._room_demand(room_id) for room_id in controlled_rooms), default=0.0)
            if not is_terminal(role):
                demand = max(demand, self._fresh_air_plant_demand(node))
            if self.config.mode is SimulationMode.VENTILATION:
                demand = 0.5 if role in {DeviceRole.AHU, DeviceRole.FRESH_AIR_UNIT} else 0.0
            if is_pump(role):
                loop = self._loop_of(node)
                if (
                    self.config.mode is SimulationMode.COOLING
                    and loop not in {"chilled", "cooling"}
                ) or (
                    self.config.mode is SimulationMode.HEATING and loop != "hot"
                ):
                    demand = 0.0

            targets: dict[str, float] = {}
            if role in {
                DeviceRole.CHILLER,
                DeviceRole.HEAT_SOURCE,
                DeviceRole.PUMP,
                DeviceRole.PUMP_CHILLED_WATER,
                DeviceRole.PUMP_COOLING_WATER,
                DeviceRole.COOLING_TOWER,
                DeviceRole.FCU,
                DeviceRole.AHU,
                DeviceRole.FRESH_AIR_UNIT,
            }:
                targets["enabled"] = 1.0 if demand > 0.0 else 0.0

            if "active_count" in self._controls[node.instance_id]:
                targets["active_count"] = (
                    1.0 + (node.quantity - 1.0) * demand if is_terminal(role)
                    else self._next_active_count(node, demand * node.quantity)
                )
            if "speed_pct" in self._controls[node.instance_id]:
                targets["speed_pct"] = (
                    AUTO_MIN_WATER_OUTPUT_PCT
                    + (100.0 - AUTO_MIN_WATER_OUTPUT_PCT) * demand
                    if demand > 0.0
                    else 0.0
                )
            if "fan_speed_pct" in self._controls[node.instance_id]:
                if role is DeviceRole.FRESH_AIR_UNIT:
                    # 专用新风机组的风量由**卫生新风需求**决定，不随房间冷热
                    # 需求波动：房间温度到位后若把风机降速，新风量就不够了。
                    # 目标开度直接取"应送新风量 / 额定风量"。
                    targets["fan_speed_pct"] = self._fresh_air_fan_target(node)
                    if targets["fan_speed_pct"] > 0.0:
                        targets["enabled"] = 1.0
                else:
                    targets["fan_speed_pct"] = (
                        AUTO_MIN_AIR_OUTPUT_PCT
                        + (100.0 - AUTO_MIN_AIR_OUTPUT_PCT) * demand
                        if demand > 0.0
                        else 0.0
                    )
            if "valve_opening_pct" in self._controls[node.instance_id]:
                if role is DeviceRole.FRESH_AIR_UNIT:
                    # 新风机组的风机常开送新风，盘管必须同步处理这份新风，
                    # 不能因为房间温度达标就把水阀关掉、改送未经处理的新风。
                    targets["valve_opening_pct"] = 100.0 * max(
                        demand, self._fresh_air_coil_demand(node)
                    )
                else:
                    targets["valve_opening_pct"] = 100.0 * demand
            if "opening_pct" in self._controls[node.instance_id]:
                targets["opening_pct"] = 100.0 * demand
            if "supply_temp_c" in self._controls[node.instance_id]:
                if role is DeviceRole.CHILLER:
                    # 出水温度目标由**除湿需求**与温度需求共同决定。
                    #
                    # 除湿能力对出水温度极其敏感：实测同一系统里 7℃ 出水可把
                    # 房间压到 43.7%、10℃ 只能到 52.9%（目标 55%）。仅用
                    # `12 - 5*demand` 时，湿度偏差经过等效相对湿度归一后
                    # 需求常年在 0.4 上下，出水停在 10℃——刚好不够除湿。
                    #
                    # 取两者较大：温度需求维持原有的 12→7℃ 响应；一旦需要
                    # 除湿，把出水压到设计工况（7℃）附近，让盘管表面低于
                    # 室内露点、真正产生凝结。除湿量对出水温度极敏感，实测
                    # 同一系统 10℃ 只能到 52.9%、7℃ 可到 43.7%（目标 55%），
                    # 因此这里必须给到设计值而不是折中值。
                    temp_target = 12.0 - 5.0 * demand
                    if self._humidity_demand_is_active(node):
                        temp_target = min(temp_target, COIL_RATED_WATER_TEMP_C)
                    targets["supply_temp_c"] = temp_target
                elif role is DeviceRole.HEAT_SOURCE:
                    targets["supply_temp_c"] = 35.0 + 10.0 * demand

            if is_terminal(role) and node.quantity > 1:
                self._controls[node.instance_id].update(
                    self._terminal_bank_controls(node, targets)
                )
                continue
            bounded = {
                name: self._slew_value(node, name, target)
                for name, target in targets.items()
            }
            self._controls[node.instance_id].update(bounded)

    def _next_active_count(self, node: DeviceNode, required_units: float) -> float:
        """Stage up for capacity, stage down after a hold and a Schmitt margin.

        Rules express the hold in seconds and hysteresis in equivalent units;
        600 s is two loop-water time constants, allowing a stage to settle.
        At most one unit changes per tick. A zero request remains an immediate
        off command, including the room's hard overcooling interlock.
        """
        current = self._controls[node.instance_id].get("active_count", float(node.quantity))
        if required_units <= 0.0:
            result = 0.0
        elif required_units > current:
            result = min(float(node.quantity), current + 1.0)
        else:
            margin = float(rule_value("automatic_control.stage_hysteresis_units"))
            hold = float(rule_value("automatic_control.stage_hold_seconds"))
            elapsed = self._simulation_seconds - self._bank_last_change_seconds.get(
                node.instance_id, -hold,
            )
            result = (
                max(1.0, current - 1.0)
                if required_units <= current - 1.0 - margin and elapsed >= hold
                else current
            )
        if result != current:
            self._bank_last_change_seconds[node.instance_id] = self._simulation_seconds
        return result

    def _terminal_bank_controls(
        self, node: DeviceNode, targets: Mapping[str, float],
    ) -> dict[str, float]:
        """Slew the bank's total air/water command before splitting over units.

        V_bank = quantity * V_rated * bank_ratio = count * V_rated * unit_ratio.
        Replace ceil(quantity*demand) with a continuous equivalent count
        1 + (quantity-1)*demand between the one-unit and full-bank endpoints.
        Total requested air/water is independent of the selected integer count.
        At a stage transfer redistribute the SAME flow; otherwise slew at
        8 pp/tick of the installed bank. Drop a unit only after the remaining
        units can carry the existing command with the stage margin.
        """
        controls = self._controls[node.instance_id]
        if targets.get("enabled", 0.0) <= 0.0:
            self._next_active_count(node, 0.0)
            return {"enabled": 0.0, "active_count": 0.0,
                    "valve_opening_pct": 0.0, "fan_speed_pct": 0.0}
        current_count = controls["active_count"] if controls["enabled"] > 0.0 else 0.0
        fresh_air = node.rated.role is DeviceRole.FRESH_AIR_UNIT
        bank_values = {}
        for name in ("valve_opening_pct", "fan_speed_pct"):
            current = controls[name] * current_count / node.quantity
            target = targets[name] if fresh_air else targets[name] * targets["active_count"] / node.quantity
            change = max(-AUTO_MAX_PCT_CHANGE_PER_TICK, min(
                AUTO_MAX_PCT_CHANGE_PER_TICK, target - current,
            ))
            bank_values[name] = max(0.0, min(100.0, current + change))
        carried_units = current_count * max(controls["valve_opening_pct"], controls["fan_speed_pct"]) / 100.0
        required_units = node.quantity * max(bank_values.values()) / 100.0
        count = self._next_active_count(node, max(required_units, carried_units))
        if count != current_count and current_count > 0.0:
            bank_values = {name: controls[name] * current_count / node.quantity for name in bank_values}
        return {
            "enabled": targets["enabled"], "active_count": count,
            **{name: min(100.0, value * node.quantity / count) if count else 0.0
               for name, value in bank_values.items()},
        }

    def _fresh_air_plant_demand(self, node: DeviceNode) -> float:
        """Keep the connected plant available for an active outdoor-air coil.

        Room error can vanish while ventilation still imports heat/moisture.
        The minimum plant command is that load's share of connected rated coil
        water flow, a dimensionless ratio, rather than an arbitrary thermostat
        floor or forcing every pump to full speed. Actual flow is still solved
        by the network and bounded by each valve and source.
        """
        if self.config.mode is not SimulationMode.COOLING:
            return 0.0
        network = self.layout.network
        if network is None:
            terminal_ids = {n.instance_id for n in self.layout.devices if is_terminal(n.rated.role)}
        else:
            sources = {
                p.source_id for p in network.paths if p.system == "cooling_water"
                and node.instance_id in {p.source_id, *p.drives, p.target_id}
            }
            terminal_ids = {
                p.target_id for p in network.paths if p.system == "chilled_water" and not p.bypass
                and (node.instance_id in {p.source_id, *p.drives} or p.source_id in sources)
            }
        terminals = [n for n in self.layout.devices if n.instance_id in terminal_ids]
        total = math.fsum((n.rated.flow_m3_h or 0.0) * n.quantity for n in terminals)
        required = math.fsum(
            (n.rated.flow_m3_h or 0.0) * n.quantity * self._fresh_air_coil_demand(n)
            for n in terminals if n.rated.role is DeviceRole.FRESH_AIR_UNIT
            and self._fresh_air_room_requirement_sum(n) > 0.0
            and (n.instance_id not in self._manual_overrides or self._controls[n.instance_id]["enabled"] > 0.0)
        )
        return min(1.0, required / total) if total > 0.0 else 0.0

    def _fresh_air_room_requirement_sum(self, node: DeviceNode) -> float:
        return math.fsum(self._fresh_air_room_requirement(room_id) for room_id in node.effective_room_ids)

    def _fresh_air_fan_target(self, node: DeviceNode) -> float:
        """新风机组风机目标开度：按应送新风量占额定风量的比例。

        专用新风机组承担卫生新风，风量不随房间冷热需求变化；房间温度达标就
        降速会导致新风不足。
        """
        required = math.fsum(
            self._fresh_air_room_requirement(room_id)
            for room_id in node.effective_room_ids
        )
        rated = (node.rated.airflow_m3_h or 0.0) * node.quantity
        if required <= 0.0 or rated <= 0.0:
            return 0.0
        base = max(AUTO_MIN_AIR_OUTPUT_PCT, min(100.0, required / rated * 100.0))
        if self.layout.network is not None and self._snapshot.tick > 0:
            # A fan must also overcome the connected duct pressure loss. The
            # former Q/Q_r feedforward alone cannot meet ventilation after a
            # resistance change. Correct by measured flow, with the same bounded
            # slew used by other actuators; the network caps delivery at demand.
            reading = self._snapshot.devices.get(node.instance_id)
            actual = reading.values.get("airflow_m3_h", 0.0) if reading else 0.0
            controls = self._controls[node.instance_id]
            current = controls.get("fan_speed_pct", base) * controls.get(
                "active_count", float(node.quantity),
            ) / node.quantity
            return max(base, min(100.0, current + (required - actual) / rated * 100.0))
        return base

    def _fresh_air_coil_demand(self, node: DeviceNode) -> float:
        """新风机组盘管的最小出力：保证送入房间的新风已被处理。

        进风是室外新风，与房间目标温差越大，需要的盘管出力越高；否则房间
        温度达标时水阀会关闭，机组改送未经处理的新风。
        """
        if self.config.mode is not SimulationMode.COOLING:
            return 0.0
        ambient = self._ambient()
        delta = ambient.dry_bulb_c - self.config.target_temp_c
        if delta <= 0.0:
            return 0.0
        return min(1.0, delta / AUTO_FULL_DEMAND_C)

    def _fresh_air_room_requirement(self, room_id: str | None) -> float:
        for room in self.layout.rooms:
            if room.room_id == room_id:
                return room.fresh_air_m3_h
        return 0.0

    def _humidity_demand_is_active(self, node: DeviceNode) -> bool:
        """Only a source's own chilled-water circuits may request cold water."""
        if self.layout.network is None:
            return "chilled" in self._dehumidifying_circuits
        return any(
            circuit.circuit_id in self._dehumidifying_circuits
            for circuit in self.layout.network.circuits
            if circuit.system == "chilled_water" and node.instance_id in circuit.source_ids
        )

    def _update_dehumidification_requests(self) -> None:
        if self.config.mode is not SimulationMode.COOLING:
            self._dehumidifying_circuits.clear()
            self._dehumidification_release_seconds.clear()
            return
        network = self.layout.network
        groups = (
            {c.circuit_id: {room_id for source in c.source_ids
                           for room_id in network.control_room_ids.get((c.system, source), ())}
             for c in network.circuits if c.system == "chilled_water"}
            if network is not None else {"chilled": {r.room_id for r in self.layout.rooms}}
        )
        hold = float(rule_value("automatic_control.dehumidification_release_seconds"))
        for circuit_id, room_ids in groups.items():
            error = max((self._room_humidity_error(room_id) for room_id in room_ids), default=0.0)
            if error > AUTO_DEADBAND_RH_PCT:
                self._dehumidifying_circuits.add(circuit_id)
            if error < -AUTO_DEADBAND_RH_PCT:
                elapsed = self._dehumidification_release_seconds.get(circuit_id, 0.0)
                elapsed += self.config.step_seconds
                self._dehumidification_release_seconds[circuit_id] = elapsed
                if elapsed >= hold:
                    self._dehumidifying_circuits.discard(circuit_id)
            else:
                self._dehumidification_release_seconds[circuit_id] = 0.0

    def _room_humidity_error(self, room_id: str | None) -> float:
        ratio = humidity_ratio_from_rh(self._room_temp(room_id), self._room_humidity(room_id))
        return rh_from_humidity_ratio(self.config.target_temp_c, ratio) - self.config.target_humidity_pct

    def _slew_value(self, node: DeviceNode, name: str, target: float) -> float:
        current = self._controls[node.instance_id][name]
        if name in {"enabled"}:
            candidate = target
        elif name == "active_count":
            candidate = current + max(-1.0, min(1.0, target - current))
        else:
            limit = (
                AUTO_MAX_TEMP_CHANGE_PER_TICK
                if name == "supply_temp_c"
                else AUTO_MAX_PCT_CHANGE_PER_TICK
            )
            candidate = current + max(-limit, min(limit, target - current))
        return clamp_controls(
            node.rated, {name: candidate}, node.quantity, snap_to_step=False,
        ).get(name, current)

    def _room_demand(self, room_id: str | None) -> float:
        """房间对冷热量的需求，取温度与湿度需求的较大者

        制冷时湿度也要参与：只看温度，室温到位后设备停机，
        新风与人员散湿会把湿度一路推到饱和，目标湿度就成了摆设。
        """
        return max(
            self._room_temp_demand(room_id),
            self._room_humidity_demand(room_id),
        )

    def _room_temp_demand(self, room_id: str | None) -> float:
        room_temp = self._room_temp(room_id)
        if self.config.mode is SimulationMode.COOLING:
            error = room_temp - self.config.target_temp_c
        elif self.config.mode is SimulationMode.HEATING:
            error = self.config.target_temp_c - room_temp
        else:
            return 0.0
        if error <= AUTO_DEADBAND_C:
            return 0.0
        return min(
            1.0,
            (error - AUTO_DEADBAND_C) / (AUTO_FULL_DEMAND_C - AUTO_DEADBAND_C),
        )

    def _room_humidity_demand(self, room_id: str | None) -> float:
        """湿度偏高时的除湿需求

        只有制冷工况能除湿；制热与通风没有冷盘管，湿度需求无处可去。

        控制量取**含湿量**而非相对湿度：相对湿度随室温变化，制冷本身会
        抬高它（同一含湿量下温度越低湿度越高），直接拿它做反馈会形成
        "越冷越湿、越湿越冷"的正反馈。这里把当前含湿量折算成"室温已回到
        目标温度时的相对湿度"，得到一个与温度无关的湿度偏差，单调反映
        空气中水分的多少。

        需求受除湿过冷下限约束：允许把室温压到目标以下
        ``AUTO_DEHUMIDIFICATION_SUBCOOL_C``。制冷除湿在物理上必须让送风低于室内
        露点，房间因此必然被带到目标温度以下；没有这份过冷，盘管在接近目标
        温度时就转入等湿降温，湿度只升不降。

        **过冷下限的斜坡量程就是过冷带本身**。原先除湿需求乘的是
        ``min(1.0, headroom / AUTO_FULL_DEMAND_C)``，斜坡长度取了
        ``AUTO_FULL_DEMAND_C``（4.0 K），而合法过冷带只有
        ``AUTO_DEHUMIDIFICATION_SUBCOOL_C``（2.0 K）——量程错了一倍。
        后果是**室温还在目标以上就开始打折**：实测 `complete/office_1`
        室温 23.74℃、目标 24.0℃，湿度偏差对应需求 0.965，却被
        ``headroom / 4.0 = 1.74/4.0 = 0.436`` 压到 **0.420**，与实测水阀开度
        **42%** 完全吻合，房间停在 69.5%（目标 55%）。

        改成以过冷带本身为斜坡长度后，``headroom / 2.0`` 在室温等于目标时
        恰好为 1.0（室温更高则被 min 截到 1），即**目标以上不打折、进入过冷带
        才线性收力**。这既用足了合法过冷空间，又在触界前平滑收敛。

        斜坡不可直接删掉。实测把它整个去掉（触界前恒为满需求）虽能进一步降低
        湿度，却会让房间在过冷下限附近来回穿越、除湿需求在 0 与满值之间跳变，
        导致冷机反复启停：`complete` 冷机开机率 400/400 → **297/400**，
        湿度单步最大跳变达 **21 个百分点**。保留斜坡则 400/400 稳定运行。
        故斜坡是**抑制极限环**的必要结构，只是量程必须取过冷带。

        实测三档对比（burn-in 4000 tick，统计 400 tick，RH@24℃ 均值）：

        | 样例 | 原斜坡 4.0 K | 无斜坡 | 斜坡 2.0 K（现行） |
        | --- | --- | --- | --- |
        | `complete` | 73.7 / 70.7 | 66.6 / 65.2（冷机 297/400） | **71.6 / 68.9** |
        | `multi_floor` | 71.7 / 74.3 / 71.3 | 65.4 / 67.3 / 64.0（369/400） | **69.9 / 72.2 / 69.9** |
        | `air` | 66.6 / 66.1 | 65.5 / 65.1 | **65.7 / 65.2** |
        | `multi_zone_all_air` | 66.7 / 66.2 / 67.0 | 64.1 / 63.6 / 64.3 | **65.2 / 64.8 / 65.5** |

        即：以过冷带为斜坡长度，在**每个样例上都优于原实现**，同时保持冷机
        连续运行，没有振荡。

        若压到下限仍无法达标（``humidity_shortfall_pct`` 持续为正），说明
        除湿能力确实不足或分区分配不均——这是**真实的选型结论**，由
        ``humidity_target_unreachable`` 告警如实提示，不应靠继续压低室温或
        凭空增加冷量掩盖。
        """
        if self.config.mode is not SimulationMode.COOLING:
            return 0.0
        room_temp_c = self._room_temp(room_id)
        current_ratio = humidity_ratio_from_rh(
            room_temp_c, self._room_humidity(room_id)
        )
        # 折算到目标室温下的等效相对湿度：与温度无关的"潮湿度"
        equivalent_rh = rh_from_humidity_ratio(
            self.config.target_temp_c, current_ratio
        )
        error = equivalent_rh - self.config.target_humidity_pct
        if error <= AUTO_DEADBAND_RH_PCT:
            return 0.0
        floor_c = self.config.target_temp_c - AUTO_DEHUMIDIFICATION_SUBCOOL_C
        headroom = room_temp_c - floor_c
        if headroom <= 0.0:
            return 0.0
        demand = min(
            1.0,
            (error - AUTO_DEADBAND_RH_PCT)
            / (AUTO_FULL_DEMAND_RH_PCT - AUTO_DEADBAND_RH_PCT),
        )
        # 斜坡量程取合法过冷带本身：室温达目标时为 1.0，触下限时为 0，
        # 用足过冷空间的同时在触界前平滑收敛，避免冷机启停振荡（见上方注释）。
        return demand * min(1.0, headroom / AUTO_DEHUMIDIFICATION_SUBCOOL_C)

    def _mode_interlocked_controls(
        self, controls: Mapping[str, Mapping[str, float]]
    ) -> dict[str, dict[str, float]]:
        effective = {
            instance_id: dict(values) for instance_id, values in controls.items()
        }
        for node in self.layout.devices:
            values = effective[node.instance_id]
            if not self._mode_allows(node):
                if "enabled" in values:
                    values["enabled"] = 0.0
                if "active_count" in values:
                    values["active_count"] = 0.0
            if (
                self.config.mode is SimulationMode.VENTILATION
                and "valve_opening_pct" in values
            ):
                values["valve_opening_pct"] = 0.0
        return effective

    def _heat_pump_serves_cooling(self, node: DeviceNode) -> bool:
        """热泵在制冷工况是否可供冷

        三类热泵目录 systems 均声明 chilled_water，制冷工况一律视为换向
        可用（四通阀属于机组内部）。热泵非泵类，`_loop_of` 恒返回
        None，环路互锁条件对它不生效——热水环路的禁用只作用于泵。
        """
        return node.rated.role is DeviceRole.HEAT_SOURCE

    def _mode_allows(self, node: DeviceNode) -> bool:
        role = node.rated.role
        if self.config.mode is SimulationMode.STANDBY:
            return False
        if self.config.mode is SimulationMode.COOLING:
            # 热泵冷热两用：制冷工况四通阀换向后即冷源，不放行会让纯热泵
            # 项目无冷源可用。热水环路的泵仍被互锁（环路与工况绑定）。
            return (
                role is not DeviceRole.HEAT_SOURCE
                or self._heat_pump_serves_cooling(node)
            ) and self._loop_of(node) != "hot"
        if self.config.mode is SimulationMode.HEATING:
            return (
                role not in {DeviceRole.CHILLER, DeviceRole.COOLING_TOWER}
                and self._loop_of(node) not in {"chilled", "cooling"}
            )
        if self.config.mode is SimulationMode.VENTILATION:
            return role in {
                DeviceRole.AHU,
                DeviceRole.FRESH_AIR_UNIT,
                DeviceRole.AIR_VALVE,
                DeviceRole.FIRE_DAMPER,
                DeviceRole.AIR_OUTLET,
            }
        return True

    def _node(self, instance_id: str) -> DeviceNode | None:
        for node in self.layout.devices:
            if node.instance_id == instance_id:
                return node
        return None

    def _nodes_with_role(self, role: DeviceRole) -> list[DeviceNode]:
        return [node for node in self.layout.devices if node.rated.role is role]

    def _loop_of(self, node: DeviceNode) -> WaterLoop | None:
        loop = node.loop
        if loop is not None:
            return loop
        if node.rated.role is DeviceRole.PUMP:
            # 未标注归属的通用泵按冷冻侧处理，保证水环路至少有流量
            return "chilled"
        return None

    def _control(self, instance_id: str, name: str, default: float) -> float:
        return float(self._effective_controls.get(instance_id, {}).get(name, default))

    def _enabled(self, instance_id: str) -> bool:
        if self.config.mode is SimulationMode.STANDBY:
            return False
        node = self._node(instance_id)
        quantity = node.quantity if node is not None else 1
        return (
            self._control(instance_id, "enabled", 1.0) >= 0.5
            and self._control(instance_id, "active_count", float(quantity)) >= 0.5
        )

    def _operating_rated(self, node: DeviceNode) -> RatedSpec:
        """Scale per-unit rated values by the selected running unit count."""
        active_count = self._control(
            node.instance_id, "active_count", float(node.quantity)
        )

        def scaled(value: float | None) -> float | None:
            return value * active_count if value is not None else None

        capacity = scaled(node.rated.capacity_kw)
        if node.rated.role is DeviceRole.CHILLER and capacity is not None:
            capacity *= self.config.chiller_capacity_multiplier
        return replace(
            node.rated,
            capacity_kw=capacity,
            power_kw=scaled(node.rated.power_kw),
            flow_m3_h=scaled(node.rated.flow_m3_h),
            cooling_flow_m3_h=scaled(node.rated.cooling_flow_m3_h),
            airflow_m3_h=scaled(node.rated.airflow_m3_h),
            fan_power_kw=scaled(node.rated.fan_power_kw),
        )

    def _room_temp(self, room_id: str | None) -> float:
        if room_id is None:
            return self.config.target_temp_c
        return self._room_temps.get(room_id, self.config.target_temp_c)

    def _room_humidity(self, room_id: str | None) -> float:
        if room_id is None:
            return self.config.target_humidity_pct
        return self._room_humidity_pct.get(
            room_id, self.config.target_humidity_pct
        )

    def _average_room_temp(self) -> float:
        if not self._room_temps:
            return self.config.target_temp_c
        return sum(self._room_temps.values()) / len(self._room_temps)

    def _supply_setpoint(self, cooling_sources: list[DeviceNode]) -> float:
        """制冷工况冷冻水出水设定

        冷机沿用 `supply_temp_c` 控制（默认 7°C）。热泵的 `supply_temp_c`
        控件是热水口径（默认 45°C），制冷时不能并入均值——同一物理键在
        两种工况下含义不同，直接平均会把冷冻水抬到温水。热泵换向制冷
        时统一按冷机默认 7°C 出水（教学口径：冷冻侧无独立设定入口）。
        """
        chiller_values = [
            self._control(node.instance_id, "supply_temp_c", 7.0)
            for node in cooling_sources
            if node.rated.role is DeviceRole.CHILLER
        ]
        return (
            sum(chiller_values) / len(chiller_values) if chiller_values else 7.0
        )

    def _hot_supply_setpoint(self, heat_sources: list[DeviceNode]) -> float:
        values = [
            self._control(node.instance_id, "supply_temp_c", 45.0)
            for node in heat_sources
        ]
        return sum(values) / len(values) if values else 45.0


@dataclass(frozen=True, slots=True)
class _SolveResult:
    supply_water_temp_c: float
    return_water_temp_c: float
    condenser_temp_c: float
    chilled_flow_m3_h: float
    cooling_flow_m3_h: float
    hot_water_flow_m3_h: float
    hot_water_supply_temp_c: float
    hot_water_return_temp_c: float
    cooling_load_kw: float
    heating_load_kw: float
    design_load_kw: float
    design_heating_load_kw: float
    installed_capacity_kw: float
    installed_heating_capacity_kw: float
    chillers: Mapping[str, ChillerState] = field(default_factory=dict)
    heat_sources: Mapping[str, HeatSourceState] = field(default_factory=dict)
    pumps: Mapping[str, PumpState] = field(default_factory=dict)
    towers: Mapping[str, CoolingTowerState] = field(default_factory=dict)
    terminals: Mapping[str, TerminalState] = field(default_factory=dict)
    valves: Mapping[str, object] = field(default_factory=dict)
    network: RealtimeNetworkResult | None = None

    @classmethod
    def empty(cls, initial_water_temp_c: float = 20.0) -> _SolveResult:
        return cls(
            supply_water_temp_c=initial_water_temp_c,
            return_water_temp_c=initial_water_temp_c,
            condenser_temp_c=30.0,
            chilled_flow_m3_h=0.0,
            cooling_flow_m3_h=0.0,
            hot_water_flow_m3_h=0.0,
            hot_water_supply_temp_c=initial_water_temp_c,
            hot_water_return_temp_c=initial_water_temp_c,
            cooling_load_kw=0.0,
            heating_load_kw=0.0,
            design_load_kw=0.0,
            design_heating_load_kw=0.0,
            installed_capacity_kw=0.0,
            installed_heating_capacity_kw=0.0,
        )

    def values_for(self, instance_id: str) -> dict[str, float]:
        sources: tuple[Mapping[str, Any], ...] = (
            self.chillers,
            self.heat_sources,
            self.pumps,
            self.towers,
            self.terminals,
            self.valves,
        )
        for source in sources:
            state = source.get(instance_id)
            if state is not None:
                return {
                    key: float(getattr(state, key))
                    for key in state.__slots__
                    if isinstance(getattr(state, key), (int, float))
                }
        return {}


def _allocate_conserved(
    total: float, weights: tuple[tuple[str, float], ...]
) -> dict[str, float]:
    allocations: dict[str, float] = {}
    assigned: list[float] = []
    for index, (room_id, weight) in enumerate(weights):
        value = (
            total - math.fsum(assigned)
            if index == len(weights) - 1
            else total * weight
        )
        allocations[room_id] = value
        assigned.append(value)
    return allocations


def _capacity_shares(
    pairs: Iterable[tuple[DeviceNode, float]],
    enabled: Callable[[str], bool],
) -> dict[str, float]:
    """按额定容量比例分摊负荷，停机设备不分摊"""
    active = [
        (node, max(weight, 0.0)) for node, weight in pairs if enabled(node.instance_id)
    ]
    total = sum(weight for _, weight in active)
    if total <= 0.0:
        if not active:
            return {}
        even = 1.0 / len(active)
        return {node.instance_id: even for node, _ in active}
    return {node.instance_id: weight / total for node, weight in active}


def _average_approach(towers: list[DeviceNode]) -> float:
    values = [node.rated.approach_c for node in towers if node.rated.approach_c]
    return sum(values) / len(values) if values else 5.0


def _shortfall_penalty(load_kw: float, capacity_kw: float) -> float:
    """冷量不足时供水温度抬升

    装机容量顶不住负荷时供水温会爬升，这是教学里"选型偏小"最直观的表现。
    """
    if capacity_kw <= 0.0 or load_kw <= capacity_kw:
        return 0.0
    shortfall = (load_kw - capacity_kw) / capacity_kw
    return min(shortfall, 1.0) * MAX_SUPPLY_TEMP_PENALTY_C
