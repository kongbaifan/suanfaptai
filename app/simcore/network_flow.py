"""Bounded, conservative path allocation for the connected teaching model.

This is a capacity/pressure-envelope model, not a manufacturer's pump-curve
operating-point solver. A path transports one flow through its entire circuit;
shared elements carry the sum of those path flows. Consequently continuity is
an identity, including parallel pumps and source banks.

Progressive filling increases eligible paths until a flow or pressure bound is
reached, then freezes paths using that bound. At least one path freezes per
pass: the work is bounded by the compiled path count, never by convergence.
"""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass

ElementKey = tuple[str, str]


@dataclass(frozen=True, slots=True)
class FlowPath:
    path_id: str
    system: str
    circuit_id: str
    source_id: str
    drive_id: str
    target_id: str
    elements: tuple[ElementKey, ...]
    room_ids: tuple[str, ...] = ()
    drive_ids: tuple[str, ...] = ()
    bypass: bool = False

    @property
    def drives(self) -> tuple[str, ...]:
        return self.drive_ids or (self.drive_id,)


@dataclass(frozen=True, slots=True)
class FlowDrive:
    flow_limit_m3_h: float
    pressure_limit_pa: float


@dataclass(frozen=True, slots=True)
class FlowSolution:
    path_flows: Mapping[str, float]
    element_flows: Mapping[ElementKey, float]
    target_flows: Mapping[ElementKey, float]
    drive_flows: Mapping[ElementKey, float]
    source_flows: Mapping[ElementKey, float]
    pressure_by_path: Mapping[str, float]


def _positive(value: float) -> float:
    return value if math.isfinite(value) and value > 0.0 else 0.0


def _path_drive(path: FlowPath, drives: Mapping[ElementKey, FlowDrive]) -> FlowDrive:
    """Series pumps carry the same flow; their available pressure rises add."""
    members = [drives.get((path.system, key), FlowDrive(0.0, 0.0)) for key in path.drives]
    return FlowDrive(
        min(_positive(d.flow_limit_m3_h) for d in members),
        math.fsum(_positive(d.pressure_limit_pa) for d in members),
    )


def allocate_paths(
    paths: tuple[FlowPath, ...],
    resistance: Mapping[ElementKey, float],
    drives: Mapping[ElementKey, FlowDrive],
    target_caps: Mapping[ElementKey, float],
    reference_caps: Mapping[ElementKey, float],
    valve_openings: Mapping[ElementKey, float],
    source_caps: Mapping[ElementKey, float] | None = None,
) -> FlowSolution:
    """Allocate m³/h subject to Q caps and Σ R_element Q_element² ≤ Δp_drive.

    R has units Pa/(m³/h)². Valve opening is a 0..1 ratio. Its flow envelope is
    referenced to the distinct downstream terminals at full opening, so adding
    a valve does not silently delete the original pipe or multiply its flow.
    Series valves add their resistances; their capacity bounds are intersected,
    rather than multiplying an already reduced final flow a second time.
    """
    ordered = tuple(sorted(paths, key=lambda p: p.path_id))
    by_id = {p.path_id: p for p in ordered}
    flows = {p.path_id: 0.0 for p in ordered}
    element_paths: dict[ElementKey, set[str]] = defaultdict(set)
    groups: dict[tuple[str, str, str], set[str]] = defaultdict(set)
    limits: dict[tuple[str, str, str], float] = {}
    active: set[str] = set()
    path_drives = {p.path_id: _path_drive(p, drives) for p in ordered}
    bypass_caps = {
        p.path_id: min(
            path_drives[p.path_id].flow_limit_m3_h,
            (source_caps or {}).get((p.system, p.source_id), math.inf),
        ) for p in ordered if p.bypass
    }
    for path in ordered:
        drive = path_drives[path.path_id]
        target = (path.system, path.target_id)
        if (
            _positive(drive.flow_limit_m3_h) > 0.0
            and _positive(drive.pressure_limit_pa) > 0.0
            and (path.bypass or _positive(target_caps.get(target, 0.0)) > 0.0)
            and all(valve_openings.get(key, 1.0) > 0.0 for key in path.elements)
        ):
            active.add(path.path_id)
        for key in path.elements:
            element_paths[key].add(path.path_id)
        bounds = [
            ("drive", key, drives.get((path.system, key), FlowDrive(0.0, 0.0)).flow_limit_m3_h)
            for key in path.drives
        ]
        if not path.bypass:
            bounds.append(("target", path.target_id, target_caps.get(target, 0.0)))
        for kind, node_id, limit in bounds:
            key_group = (kind, path.system, node_id)
            groups[key_group].add(path.path_id)
            limits[key_group] = _positive(limit)
        source_key = (path.system, path.source_id)
        if source_caps is not None and source_key in source_caps:
            key_group = ("source", *source_key)
            groups[key_group].add(path.path_id)
            limits[key_group] = _positive(source_caps[source_key])

    for element, opening in sorted(valve_openings.items()):
        members = element_paths.get(element, set())
        valve_targets = {(by_id[key].system, by_id[key].target_id)
                         for key in members if not by_id[key].bypass}
        bypass_routes: dict[ElementKey, list[FlowPath]] = defaultdict(list)
        for path_id in sorted(members):
            path = by_id[path_id]
            if path.bypass:
                bypass_routes[(path.system, path.source_id)].append(path)
        bypass_sources = {}
        for source, routes in bypass_routes.items():
            # Every route crosses one first and one last pump. Distinct pumps
            # at either cut add in parallel; a shared series pump is counted
            # once. These cuts and the source give a finite reference envelope.
            # Taking max(single-route capacity) would halve a two-pump bank
            # even with its common bypass valve fully open.
            cut_limits = [
                math.fsum(drives.get((source[0], drive_id), FlowDrive(0.0, 0.0)).flow_limit_m3_h
                          for drive_id in sorted({p.drives[index] for p in routes}))
                for index in (0, -1)
            ]
            bypass_sources[source] = min(*cut_limits, (source_caps or {}).get(source, math.inf))
        key_group = ("valve", *element)
        groups[key_group] = members
        reference = math.fsum(
            _positive(reference_caps.get(t, 0.0)) for t in sorted(valve_targets)
        ) + math.fsum(bypass_sources.values())
        limits[key_group] = reference * max(min(opening, 1.0), 0.0)

    route_counts: dict[ElementKey, int] = defaultdict(int)
    for path_id in sorted(active):
        path = by_id[path_id]
        route_counts[(path.system, path.source_id if path.bypass else path.target_id)] += 1
    weights = {
        p.path_id: (bypass_caps[p.path_id] if p.bypass else _positive(target_caps.get((p.system, p.target_id), 0.0)))
        / max(route_counts[(p.system, p.source_id if p.bypass else p.target_id)], 1)
        / math.sqrt(
            max(math.fsum(_positive(resistance.get(e, 0.0)) for e in p.elements), 1.0)
        )
        for p in ordered
    }
    largest = max(weights.values(), default=0.0)
    if largest > 0:
        weights = {key: value / largest for key, value in weights.items()}

    # Each pass removes at least one eligible route. It is a finite allocation
    # algorithm (like capped water filling), not an open-ended residual loop.
    for _ in range(len(ordered)):
        if not active:
            break
        node_flow = {
            key: math.fsum(flows[p] for p in sorted(members))
            for key, members in element_paths.items()
        }
        node_slope = {
            key: math.fsum(weights[p] for p in sorted(members & active))
            for key, members in element_paths.items()
        }
        candidates: list[tuple[float, str, set[str]]] = []
        for group_key, members in sorted(groups.items()):
            growing = members & active
            slope = math.fsum(weights[p] for p in sorted(growing))
            if slope <= 0.0:
                continue
            remaining = limits[group_key] - math.fsum(flows[p] for p in sorted(members))
            candidates.append((max(remaining / slope, 0.0), repr(group_key), growing))

        for path in ordered:
            if path.path_id not in active and flows[path.path_id] <= 0.0:
                continue
            drive = path_drives[path.path_id]
            a = math.fsum(
                _positive(resistance.get(e, 0.0)) * node_slope[e] ** 2
                for e in path.elements
            )
            b = 2.0 * math.fsum(
                _positive(resistance.get(e, 0.0)) * node_flow[e] * node_slope[e]
                for e in path.elements
            )
            used = math.fsum(
                _positive(resistance.get(e, 0.0)) * node_flow[e] ** 2
                for e in path.elements
            )
            if a <= 0.0:
                continue
            remaining = max(drive.pressure_limit_pa - used, 0.0)
            # Stable positive root of a*t²+b*t-remaining=0; avoids cancellation.
            denominator = b + math.sqrt(b * b + 4.0 * a * remaining)
            bound = 2.0 * remaining / denominator if denominator > 0.0 else 0.0
            affected = set().union(
                *(
                    element_paths[e] & active
                    for e in path.elements
                    if resistance.get(e, 0.0) > 0.0
                )
            )
            candidates.append((bound, path.path_id, affected))

        if not candidates:
            break
        bound, _, frozen = min(candidates, key=lambda item: (item[0], item[1]))
        for path_id in sorted(active):
            flows[path_id] += weights[path_id] * bound
        active.difference_update(frozen)

    elements = {
        key: math.fsum(flows[p] for p in sorted(members))
        for key, members in element_paths.items()
    }
    targets: dict[ElementKey, float] = defaultdict(float)
    drive_flows: dict[ElementKey, float] = defaultdict(float)
    sources: dict[ElementKey, float] = defaultdict(float)
    pressure: dict[str, float] = {}
    for path in ordered:
        flow = flows[path.path_id]
        if not path.bypass:
            targets[(path.system, path.target_id)] += flow
        for drive_id in path.drives:
            drive_flows[(path.system, drive_id)] += flow
        sources[(path.system, path.source_id)] += flow
        pressure[path.path_id] = math.fsum(
            _positive(resistance.get(e, 0.0)) * elements[e] ** 2 for e in path.elements
        )
    return FlowSolution(
        flows, elements, dict(targets), dict(drive_flows), dict(sources), pressure
    )
