"""Deterministic reduced-order hydronic flow allocation for teaching simulation."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Iterable, Literal


HydronicLoop = Literal["chilled", "hot"]

# Q(kW) = V(m3/h) * rho * cp / 3600 * delta-T
WATER_KW_PER_M3H_K = 1000.0 * 4.187 / 3600.0
MIN_HYDRONIC_TEMPERATURE_C = -20.0
MAX_HYDRONIC_TEMPERATURE_C = 100.0
MAX_HYDRONIC_DELTA_T_C = 20.0


@dataclass(frozen=True, slots=True)
class HydronicBranch:
    """A terminal branch represented by a finite relative resistance index."""

    terminal_instance_id: str
    loop: HydronicLoop
    resistance_index: float


@dataclass(frozen=True, slots=True)
class HydronicDemand:
    """One tick's capped demand and allocation weight for a terminal branch."""

    terminal_instance_id: str
    flow_cap_m3_h: float
    weight: float


@dataclass(frozen=True, slots=True)
class HydronicThermalBranch:
    """One terminal branch's final conserved flow and delivered load."""

    terminal_instance_id: str
    flow_m3_h: float
    load_kw: float


def advance_temperature(
    current_temp_c: float,
    target_temp_c: float,
    step_seconds: float,
    time_constant_seconds: float,
) -> float:
    """Advance a bounded first-order response without overshooting its target."""
    finite_target = _bounded_temperature(target_temp_c, 20.0)
    current = _bounded_temperature(current_temp_c, finite_target)
    target = _bounded_temperature(target_temp_c, current)
    if (
        not math.isfinite(step_seconds)
        or step_seconds <= 0.0
        or not math.isfinite(time_constant_seconds)
        or time_constant_seconds <= 0.0
    ):
        return current

    alpha = -math.expm1(-step_seconds / time_constant_seconds)
    advanced = current + alpha * (target - current)
    lower, upper = sorted((current, target))
    return min(max(_bounded_temperature(advanced, current), lower), upper)


def branch_return_temperature(
    supply_temp_c: float,
    flow_m3_h: float,
    load_kw: float,
    *,
    heating: bool,
) -> float:
    """Calculate one branch return temperature from its final water-side load."""
    supply = _bounded_temperature(supply_temp_c, 20.0)
    if (
        not math.isfinite(flow_m3_h)
        or flow_m3_h <= 0.0
        or not math.isfinite(load_kw)
        or load_kw < 0.0
    ):
        return supply
    delta_t = min(
        load_kw / (WATER_KW_PER_M3H_K * flow_m3_h),
        MAX_HYDRONIC_DELTA_T_C,
    )
    return _bounded_temperature(
        supply - delta_t if heating else supply + delta_t,
        supply,
    )


def mixed_return_temperature(
    supply_temp_c: float,
    branches: Iterable[HydronicThermalBranch],
    *,
    heating: bool,
) -> float:
    """Flow-weight valid branch returns in stable terminal-ID order."""
    supply = _bounded_temperature(supply_temp_c, 20.0)
    active = sorted(
        (
            branch
            for branch in branches
            if math.isfinite(branch.flow_m3_h)
            and branch.flow_m3_h > 0.0
            and math.isfinite(branch.load_kw)
            and branch.load_kw >= 0.0
        ),
        key=lambda branch: branch.terminal_instance_id,
    )
    if not active:
        return supply

    max_flow = max(branch.flow_m3_h for branch in active)
    scaled_flows = [branch.flow_m3_h / max_flow for branch in active]
    total_scaled_flow = math.fsum(scaled_flows)
    if total_scaled_flow <= 0.0:
        return supply
    weighted_return = math.fsum(
        scaled_flow
        * branch_return_temperature(
            supply,
            branch.flow_m3_h,
            branch.load_kw,
            heating=heating,
        )
        for branch, scaled_flow in zip(active, scaled_flows, strict=True)
    )
    return _bounded_temperature(weighted_return / total_scaled_flow, supply)


def pipe_resistance_index(
    length_m: float | None,
    diameter_mm: float | None,
    local_resistance_coefficient: float | None = 0.0,
) -> float:
    """Return a positive dimensionless resistance used only for relative splits."""
    if length_m is not None and not math.isfinite(length_m):
        return 1.0
    if diameter_mm is not None and not math.isfinite(diameter_mm):
        return 1.0
    if local_resistance_coefficient is not None and not math.isfinite(
        local_resistance_coefficient
    ):
        return 1.0

    length = max(length_m if length_m is not None else 1.0, 1.0)
    diameter = max(diameter_mm if diameter_mm is not None else 100.0, 10.0)
    local = max(local_resistance_coefficient or 0.0, 0.0)
    resistance = length * (100.0 / diameter) ** 4.87 * (1.0 + local)
    return resistance if math.isfinite(resistance) and resistance > 0.0 else 1.0


def normalized_resistance_index(value: float) -> float:
    """Keep saved or calculated branch resistance usable by the allocator."""
    return value if math.isfinite(value) and value > 0.0 else 1.0


def allocate_branch_flows(
    available_flow_m3_h: float,
    demands: Iterable[HydronicDemand],
) -> dict[str, float]:
    """Allocate available flow by stable capped weighted water filling."""
    ordered = sorted(demands, key=lambda item: item.terminal_instance_id)
    flows = {demand.terminal_instance_id: 0.0 for demand in ordered}
    if not math.isfinite(available_flow_m3_h) or available_flow_m3_h <= 0.0:
        return flows

    caps = {
        demand.terminal_instance_id: _finite_positive(demand.flow_cap_m3_h)
        for demand in ordered
    }
    weights = {
        demand.terminal_instance_id: _finite_positive(demand.weight)
        for demand in ordered
    }
    active = [
        branch_id
        for branch_id in flows
        if caps[branch_id] > 0.0 and weights[branch_id] > 0.0
    ]
    eligible = tuple(active)
    target = min(available_flow_m3_h, sum(caps[branch_id] for branch_id in active))
    remaining = target

    while active and remaining > 0.0:
        max_weight = max(weights[branch_id] for branch_id in active)
        scaled_weights = {
            branch_id: weights[branch_id] / max_weight for branch_id in active
        }
        total_weight = math.fsum(scaled_weights.values())
        if total_weight <= 0.0:
            break

        saturated = [
            branch_id
            for branch_id in active
            if remaining * (scaled_weights[branch_id] / total_weight)
            >= caps[branch_id] - flows[branch_id]
        ]
        if not saturated:
            assigned = 0.0
            for branch_id in active[:-1]:
                share = remaining * (scaled_weights[branch_id] / total_weight)
                flows[branch_id] += share
                assigned += share
            last = active[-1]
            flows[last] += min(
                max(remaining - assigned, 0.0), caps[last] - flows[last]
            )
            remaining = 0.0
            break

        for branch_id in saturated:
            headroom = caps[branch_id] - flows[branch_id]
            flows[branch_id] += headroom
            remaining -= headroom
        active = [branch_id for branch_id in active if branch_id not in saturated]

    residual = target - sum(flows.values())
    if residual > 0.0:
        for branch_id in reversed(eligible):
            headroom = caps[branch_id] - flows[branch_id]
            addition = min(residual, max(headroom, 0.0))
            flows[branch_id] += addition
            residual -= addition
            if residual <= 1e-12:
                break
    return flows


def _finite_positive(value: float) -> float:
    if not math.isfinite(value) or value <= 0.0:
        return 0.0
    return value


def _bounded_temperature(value: float, fallback: float) -> float:
    finite = value if math.isfinite(value) else fallback
    return min(
        max(finite, MIN_HYDRONIC_TEMPERATURE_C),
        MAX_HYDRONIC_TEMPERATURE_C,
    )
