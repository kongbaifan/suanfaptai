"""Immutable teaching presets and side-effect-free steady comparisons."""

from __future__ import annotations

import math
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass, replace
from typing import Literal

from app.data.project_store import get_project
from app.realtime import get_current_control_snapshot, get_simulation_config
from app.schemas import (
    SystemEvaluateRequest,
    TeachingComparisonConvergence,
    TeachingComparisonResult,
    TeachingConditionResult,
    TeachingExplanation,
    TeachingMetricComparison,
    TeachingMetricDirection,
    TeachingScenarioControl,
    TeachingScenarioDescriptor,
    TeachingScenarioEffect,
    TeachingSteadyConvergence,
)
from app.simcore.controls import clamp_controls, default_controls
from app.simcore.devices import is_terminal
from app.simcore.project import build_project_layout, project_humidity_pct
from app.simcore.session import (
    CHILLER_CAPACITY_LOW_MULTIPLIER,
    DeviceNode,
    PlantLayout,
    PlantSnapshot,
    SimulationConfig,
    SimulationMode,
)
from app.simcore.steady import SteadyResult, solve_steady
from app.unity_catalog import DeviceRole

ScenarioTarget = Literal[
    "all_equipment",
    "chiller",
    "chilled_water_pump",
    "cooling_water_pump",
    "cooling_tower",
    "terminal",
]


@dataclass(frozen=True, slots=True)
class _ExpectedEffect:
    metric: str
    direction: TeachingMetricDirection
    code: str
    message: str
    requires_equal_cooling: bool = False


@dataclass(frozen=True, slots=True)
class _ControlOverlay:
    target: ScenarioTarget
    values: tuple[tuple[str, float], ...]


@dataclass(frozen=True, slots=True)
class _ScenarioDefinition:
    id: str
    name: str
    description: str
    catalog_controls: tuple[TeachingScenarioControl, ...]
    effects: tuple[_ExpectedEffect, ...]
    overlays: tuple[_ControlOverlay, ...] = ()
    restore_defaults: bool = False
    chiller_capacity_multiplier: float = 1.0


class ScenarioNotApplicableError(ValueError):
    def __init__(self, scenario_id: str) -> None:
        self.scenario_id = scenario_id
        super().__init__("Scenario is not applicable to this project.")

    @property
    def detail(self) -> dict[str, str]:
        return {
            "code": "SCENARIO_NOT_APPLICABLE",
            "scenario_id": self.scenario_id,
            "message": str(self),
        }


_SCENARIOS = (
    _ScenarioDefinition(
        id="normal_operation",
        name="正常运行",
        description="恢复设备启用状态、运行台数和额定转速或开度。",
        catalog_controls=(
            TeachingScenarioControl(
                scope="device_control",
                target_role="all_equipment",
                name="enabled",
                value=1.0,
            ),
            TeachingScenarioControl(
                scope="device_control",
                target_role="equipment_bank",
                name="active_count",
                value="installed_quantity",
            ),
            TeachingScenarioControl(
                scope="device_control",
                target_role="pump",
                name="speed_pct",
                value=100.0,
            ),
            TeachingScenarioControl(
                scope="device_control",
                target_role="cooling_tower",
                name="fan_speed_pct",
                value=100.0,
            ),
            TeachingScenarioControl(
                scope="device_control",
                target_role="terminal",
                name="valve_opening_pct",
                value=100.0,
            ),
            TeachingScenarioControl(
                scope="device_control",
                target_role="terminal",
                name="fan_speed_pct",
                value=100.0,
            ),
        ),
        effects=(),
        restore_defaults=True,
    ),
    _ScenarioDefinition(
        id="chiller_capacity_low",
        name="冷机容量不足",
        description="将冷机可用容量降至额定值的 55%，观察能力不足的影响。",
        catalog_controls=(
            TeachingScenarioControl(
                scope="simulation_config",
                target_role="chiller",
                name="chiller_capacity_multiplier",
                value=CHILLER_CAPACITY_LOW_MULTIPLIER,
            ),
        ),
        effects=(
            _ExpectedEffect(
                metric="average_room_temp_c",
                direction="increase",
                code="CHILLER_CAPACITY_LOW_ROOM_TEMPERATURE",
                message="冷机可用容量降低后，供冷不足会使室内温度升高。",
            ),
        ),
        chiller_capacity_multiplier=CHILLER_CAPACITY_LOW_MULTIPLIER,
    ),
    _ScenarioDefinition(
        id="chilled_water_low_flow",
        name="冷冻水低流量",
        description="降低冷冻水泵转速，观察流量与供回水温差变化。",
        catalog_controls=(
            TeachingScenarioControl(
                scope="device_control",
                target_role="chilled_water_pump",
                name="speed_pct",
                value=45.0,
            ),
        ),
        effects=(
            _ExpectedEffect(
                metric="total_flow_m3_h",
                direction="decrease",
                code="CHILLED_WATER_LOW_FLOW_FLOW",
                message="冷冻水泵降速后，系统冷冻水流量应下降。",
            ),
            _ExpectedEffect(
                metric="delta_t_c",
                direction="increase",
                code="CHILLED_WATER_LOW_FLOW_DELTA_T",
                message="相同冷量由更低流量输送时，供回水温差应增大。",
            ),
        ),
        overlays=(
            _ControlOverlay(
                target="chilled_water_pump", values=(("speed_pct", 45.0),)
            ),
        ),
    ),
    _ScenarioDefinition(
        id="cooling_performance_low",
        name="冷却效果不足",
        description="降低冷却水泵和冷却塔风机转速，观察冷凝温度与能效变化。",
        catalog_controls=(
            TeachingScenarioControl(
                scope="device_control",
                target_role="cooling_water_pump",
                name="speed_pct",
                value=45.0,
            ),
            TeachingScenarioControl(
                scope="device_control",
                target_role="cooling_tower",
                name="fan_speed_pct",
                value=45.0,
            ),
        ),
        effects=(
            _ExpectedEffect(
                metric="condenser_water_temp_c",
                direction="increase",
                code="COOLING_PERFORMANCE_LOW_CONDENSER_TEMPERATURE",
                message="冷却水量和塔风量降低后，冷凝水温度应升高。",
            ),
            _ExpectedEffect(
                metric="system_cop",
                direction="decrease",
                code="COOLING_PERFORMANCE_LOW_COP",
                message="冷凝温度升高会压低冷机 COP。",
            ),
            _ExpectedEffect(
                metric="chiller_power_kw",
                direction="increase",
                code="COOLING_PERFORMANCE_LOW_CHILLER_POWER",
                message=(
                    "在制冷量相同的条件下，COP 降低使冷机功率上升；"
                    "若冷却水限流同时降低了制冷量，功率也可能下降。"
                    "请按 P=Q/COP 联合判断，不能只看功率。"
                ),
                requires_equal_cooling=True,
            ),
        ),
        overlays=(
            _ControlOverlay(
                target="cooling_water_pump", values=(("speed_pct", 45.0),)
            ),
            _ControlOverlay(
                target="cooling_tower", values=(("fan_speed_pct", 45.0),)
            ),
        ),
    ),
    _ScenarioDefinition(
        id="terminal_capacity_low",
        name="末端能力不足",
        description="降低末端水阀开度和风机转速，观察房间温度变化。",
        catalog_controls=(
            TeachingScenarioControl(
                scope="device_control",
                target_role="terminal",
                name="valve_opening_pct",
                value=55.0,
            ),
            TeachingScenarioControl(
                scope="device_control",
                target_role="terminal",
                name="fan_speed_pct",
                value=65.0,
            ),
        ),
        effects=(
            _ExpectedEffect(
                metric="average_room_temp_c",
                direction="increase",
                code="TERMINAL_CAPACITY_LOW_ROOM_TEMPERATURE",
                message="末端换热能力降低后，房间温度应升高。",
            ),
        ),
        overlays=(
            _ControlOverlay(
                target="terminal",
                values=(("valve_opening_pct", 55.0), ("fan_speed_pct", 65.0)),
            ),
        ),
    ),
)

_SCENARIO_BY_ID = {scenario.id: scenario for scenario in _SCENARIOS}

_METRIC_UNITS = {
    "average_room_temp_c": "C",
    "total_power_kw": "kW",
    "chiller_power_kw": "kW",
    "total_flow_m3_h": "m3/h",
    "delta_t_c": "C",
    "condenser_water_temp_c": "C",
    "system_cop": "",
}

_METRIC_TOLERANCES = {
    "average_room_temp_c": 0.01,
    "total_power_kw": 0.001,
    "chiller_power_kw": 0.001,
    "total_flow_m3_h": 0.001,
    "delta_t_c": 0.01,
    "condenser_water_temp_c": 0.01,
    "system_cop": 0.001,
}


def list_teaching_scenarios() -> list[TeachingScenarioDescriptor]:
    return [_descriptor(scenario) for scenario in _SCENARIOS]


def compare_teaching_scenario(
    project_id: str, scenario_id: str
) -> TeachingComparisonResult:
    record = get_project(project_id)
    scenario = _SCENARIO_BY_ID.get(scenario_id)
    if scenario is None:
        raise KeyError(scenario_id)

    resolved = build_project_layout(record.project)
    targeted = _targeted_devices(resolved.layout, scenario)
    baseline_controls = get_current_control_snapshot(project_id, resolved.layout)
    scenario_controls = _scenario_controls(
        baseline_controls, resolved.layout, scenario, targeted
    )
    baseline_config = _comparison_config(project_id, record.project)
    scenario_config = replace(
        baseline_config,
        chiller_capacity_multiplier=scenario.chiller_capacity_multiplier,
    )

    baseline = solve_steady(
        resolved.layout, baseline_config, initial_controls=baseline_controls
    )
    scenario_result = solve_steady(
        resolved.layout, scenario_config, initial_controls=scenario_controls
    )
    baseline_metrics = _extract_metrics(baseline.snapshot, resolved.layout)
    scenario_metrics = _extract_metrics(scenario_result.snapshot, resolved.layout)
    comparisons = _compare_metrics(baseline_metrics, scenario_metrics)

    return TeachingComparisonResult(
        project_id=project_id,
        scenario=_descriptor(scenario),
        baseline=TeachingConditionResult(
            metrics=baseline_metrics,
            alarms=list(baseline.snapshot.alarms),
        ),
        scenario_result=TeachingConditionResult(
            metrics=scenario_metrics,
            alarms=list(scenario_result.snapshot.alarms),
        ),
        metrics=comparisons,
        explanations=_explanations(scenario, targeted, comparisons),
        convergence=TeachingComparisonConvergence(
            baseline=_convergence(baseline),
            scenario=_convergence(scenario_result),
        ),
    )


def _descriptor(scenario: _ScenarioDefinition) -> TeachingScenarioDescriptor:
    return TeachingScenarioDescriptor(
        id=scenario.id,
        name=scenario.name,
        description=scenario.description,
        controls=[control.model_copy(deep=True) for control in scenario.catalog_controls],
        effects=[
            TeachingScenarioEffect(metric=effect.metric, direction=effect.direction)
            for effect in scenario.effects
        ],
    )


def _targeted_devices(
    layout: PlantLayout, scenario: _ScenarioDefinition
) -> tuple[DeviceNode, ...]:
    if scenario.restore_defaults:
        if not layout.devices:
            raise ScenarioNotApplicableError(scenario.id)
        return layout.devices

    groups = tuple(overlay.target for overlay in scenario.overlays)
    if scenario.chiller_capacity_multiplier != 1.0:
        groups = ("chiller",)
    targeted: list[DeviceNode] = []
    for target in groups:
        matches = _nodes_for_target(layout, target)
        if not matches:
            raise ScenarioNotApplicableError(scenario.id)
        for node in matches:
            if node not in targeted:
                targeted.append(node)
    return tuple(targeted)


def _nodes_for_target(layout: PlantLayout, target: ScenarioTarget) -> list[DeviceNode]:
    if target == "all_equipment":
        return list(layout.devices)
    if target == "chiller":
        return [node for node in layout.devices if node.rated.role is DeviceRole.CHILLER]
    if target == "chilled_water_pump":
        return [
            node
            for node in layout.devices
            if node.loop == "chilled"
            or (node.rated.role is DeviceRole.PUMP and node.loop is None)
        ]
    if target == "cooling_water_pump":
        return [node for node in layout.devices if node.loop == "cooling"]
    if target == "cooling_tower":
        return [
            node
            for node in layout.devices
            if node.rated.role is DeviceRole.COOLING_TOWER
        ]
    if target == "terminal":
        return [node for node in layout.devices if is_terminal(node.rated.role)]
    return []


def _scenario_controls(
    baseline: Mapping[str, Mapping[str, float]],
    layout: PlantLayout,
    scenario: _ScenarioDefinition,
    targeted: tuple[DeviceNode, ...],
) -> dict[str, dict[str, float]]:
    controls = deepcopy({key: dict(values) for key, values in baseline.items()})
    if scenario.restore_defaults:
        for node in targeted:
            controls[node.instance_id] = default_controls(node.rated, node.quantity)
        return controls

    targeted_ids = {node.instance_id for node in targeted}
    for overlay in scenario.overlays:
        for node in _nodes_for_target(layout, overlay.target):
            if node.instance_id not in targeted_ids:
                continue
            accepted = clamp_controls(node.rated, dict(overlay.values), node.quantity)
            controls[node.instance_id].update(accepted)
    return controls


def _comparison_config(
    project_id: str, project: SystemEvaluateRequest
) -> SimulationConfig:
    current = get_simulation_config(project_id)
    mode = {
        "cooling": SimulationMode.COOLING,
        "fault": SimulationMode.COOLING,
        "ventilation": SimulationMode.VENTILATION,
        "heating": SimulationMode.VENTILATION,
        "standby": SimulationMode.STANDBY,
    }[current.mode]
    return SimulationConfig(
        step_seconds=60.0,
        mode=mode,
        target_temp_c=current.target_temp_c,
        outdoor_base_temp_c=current.outdoor_base_temp_c,
        outdoor_humidity_pct=project_humidity_pct(project),
    )


def _extract_metrics(
    snapshot: PlantSnapshot, layout: PlantLayout
) -> dict[str, float | None]:
    chillers = [
        node for node in layout.devices if node.rated.role is DeviceRole.CHILLER
    ]
    chilled_pumps = _nodes_for_target(layout, "chilled_water_pump")
    chiller_cooling = sum(
        snapshot.devices[node.instance_id].values.get("cooling_kw", 0.0)
        for node in chillers
    )
    chiller_power = sum(
        snapshot.devices[node.instance_id].values.get("power_kw", 0.0)
        for node in chillers
    )
    average_room_temp = None
    if snapshot.rooms:
        average_room_temp = sum(
            room.temperature_c for room in snapshot.rooms.values()
        ) / len(snapshot.rooms)

    return {
        "average_room_temp_c": _rounded(average_room_temp),
        "total_power_kw": _rounded(snapshot.total_power_kw)
        if layout.devices
        else None,
        "chiller_power_kw": _rounded(chiller_power) if chillers else None,
        "total_flow_m3_h": _rounded(snapshot.chilled_flow_m3_h)
        if chilled_pumps
        else None,
        "delta_t_c": _rounded(
            snapshot.return_water_temp_c - snapshot.supply_water_temp_c
        )
        if chillers and chilled_pumps
        else None,
        "condenser_water_temp_c": _rounded(snapshot.condenser_water_temp_c)
        if chillers
        else None,
        "system_cop": _rounded(chiller_cooling / chiller_power)
        if chiller_power > 0.0
        else None,
    }


def _compare_metrics(
    before_metrics: Mapping[str, float | None],
    after_metrics: Mapping[str, float | None],
) -> dict[str, TeachingMetricComparison]:
    comparisons: dict[str, TeachingMetricComparison] = {}
    for metric, unit in _METRIC_UNITS.items():
        before = before_metrics[metric]
        after = after_metrics[metric]
        applicable = before is not None and after is not None
        change = None
        change_pct = None
        direction = None
        if applicable:
            assert before is not None and after is not None
            raw_change = after - before
            change = _rounded(raw_change)
            if before != 0.0:
                change_pct = _rounded(raw_change / abs(before) * 100.0)
            direction = _direction(raw_change, _METRIC_TOLERANCES[metric])
        comparisons[metric] = TeachingMetricComparison(
            before=before,
            after=after,
            change=change,
            change_pct=change_pct,
            direction=direction,
            unit=unit,
            applicable=applicable,
        )
    return comparisons


def _direction(change: float, tolerance: float) -> TeachingMetricDirection:
    if change > tolerance:
        return "increase"
    if change < -tolerance:
        return "decrease"
    return "unchanged"


def _explanations(
    scenario: _ScenarioDefinition,
    targeted: tuple[DeviceNode, ...],
    comparisons: Mapping[str, TeachingMetricComparison],
) -> list[TeachingExplanation]:
    related_devices = [node.instance_id for node in targeted]
    explanations = []
    for effect in scenario.effects:
        observed = comparisons[effect.metric].direction
        condition_met = True
        if effect.requires_equal_cooling:
            power = comparisons["chiller_power_kw"]
            cop = comparisons["system_cop"]
            if power.before is None or power.after is None or cop.before is None or cop.after is None:
                condition_met = False
            else:
                condition_met = math.isclose(power.before * cop.before, power.after * cop.after, rel_tol=0.001, abs_tol=0.001)
        explanations.append(
            TeachingExplanation(
                code=effect.code,
                message=effect.message,
                related_devices=related_devices,
                related_metrics=[effect.metric],
                expected_direction=effect.direction,
                observed_direction=observed,
                expectation_met=(observed == effect.direction)
                if observed is not None and condition_met
                else None,
            )
        )
    return explanations


def _convergence(result: SteadyResult) -> TeachingSteadyConvergence:
    return TeachingSteadyConvergence(
        converged=result.converged,
        ticks=result.ticks,
        final_room_drift_c=_rounded(result.final_drift_c) or 0.0,
    )


def _rounded(value: float | None) -> float | None:
    return round(value, 6) if value is not None else None
