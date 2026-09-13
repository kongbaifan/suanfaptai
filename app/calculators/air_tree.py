from __future__ import annotations

import math
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Literal

from app.data.equipment import (
    airflow_for_device,
    catalog_number_for_device,
    role_for_device,
)
from app.schemas import AirNodeBalance, Issue, SystemEvaluateRequest

SUPPLY_PIPE_TYPES = {"main_supply", "branch_supply", "riser_supply"}
RETURN_PIPE_TYPES = {"main_return", "branch_return", "riser_return"}
FLOW_TOLERANCE_M3_H = 0.01
FLOW_SCALE = 1000
MAX_AIRFLOW_M3_H = 1_000_000_000_000.0


@dataclass(slots=True)
class AirTreeSolution:
    flow_by_duct_m3_h: dict[str, float] = field(default_factory=dict)
    flow_source_by_duct: dict[str, str] = field(default_factory=dict)
    side_by_duct: dict[str, str] = field(default_factory=dict)
    node_balances: list[AirNodeBalance] = field(default_factory=list)
    issues: list[Issue] = field(default_factory=list)
    supply_total_m3_h: float = 0.0
    return_total_m3_h: float = 0.0
    succeeded: bool = False
    degraded: bool = False
    fallback_used: bool = False
    steps: int = 0

    @property
    def max_residual_m3_h(self) -> float:
        return max(
            (abs(item.residual_m3_h) for item in self.node_balances),
            default=0.0,
        )


@dataclass(slots=True)
class _DirectedTree:
    root_id: str
    side: Literal["supply", "return"]
    parent_by_node: dict[str, str | None]
    children_by_node: dict[str, tuple[str, ...]]
    pipe_ids: tuple[str, ...]
    unsupported_targets: tuple[str, ...] = ()


def solve_air_trees(
    request: SystemEvaluateRequest,
    required_total_airflow_m3_h: float,
    legacy_flow_by_duct_m3_h: dict[str, float],
) -> AirTreeSolution:
    ducts = sorted(
        (pipe for pipe in request.pipes if pipe.system == "air"),
        key=lambda item: item.id,
    )
    if not ducts:
        return AirTreeSolution(succeeded=True)

    outgoing, incoming = _directed_air_graph(request)
    roles = {device.id: role_for_device(device) for device in request.devices}
    roots = sorted(
        device.id
        for device in request.devices
        if roles.get(device.id) == "terminal"
        and (outgoing.get(device.id) or incoming.get(device.id))
    )
    if not roots:
        issue = Issue(
            level="warning",
            code="AIR_SOURCE_MISSING",
            message="No connected AHU or terminal root could be identified for the air network.",
        )
        return _legacy_solution(
            ducts,
            required_total_airflow_m3_h,
            legacy_flow_by_duct_m3_h,
            [issue],
        )

    trees: list[_DirectedTree] = []
    for root_id in roots:
        supply_tree = _build_tree(
            root_id,
            "supply",
            outgoing,
            roles,
            {pipe.id: pipe.pipe_type for pipe in ducts},
        )
        return_tree = _build_tree(
            root_id,
            "return",
            incoming,
            roles,
            {pipe.id: pipe.pipe_type for pipe in ducts},
        )
        if supply_tree.pipe_ids:
            trees.append(supply_tree)
        if return_tree.pipe_ids:
            trees.append(return_tree)

    unsupported = sorted(
        {
            target
            for tree in trees
            for target in tree.unsupported_targets
        }
    )
    owners: dict[
        str,
        list[tuple[Literal["supply", "return"], str]],
    ] = defaultdict(list)
    for tree in trees:
        for pipe_id in tree.pipe_ids:
            owners[pipe_id].append((tree.side, tree.root_id))
    overlaps = sorted(pipe_id for pipe_id, tree_ids in owners.items() if len(tree_ids) > 1)
    unclassified = sorted(pipe.id for pipe in ducts if pipe.id not in owners)

    structural_issues = [
        Issue(
            level="warning",
            code="AIR_TREE_UNSUPPORTED",
            message="The air network contains a cycle, merge, or overlapping source tree; bounded legacy flow allocation was used.",
            target=target,
        )
        for target in sorted(set(unsupported + overlaps))
    ]
    structural_issues.extend(
        Issue(
            level="warning",
            code="AIR_TERMINAL_DISCONNECTED",
            message="The air duct is not reachable from a supported supply or return tree.",
            target=pipe_id,
        )
        for pipe_id in unclassified
    )
    if not trees:
        if not structural_issues:
            structural_issues.append(
                Issue(
                    level="warning",
                    code="AIR_TREE_UNSUPPORTED",
                    message="The directed air network could not be classified as a supply or return tree.",
                )
            )
        return _legacy_solution(
            ducts,
            required_total_airflow_m3_h,
            legacy_flow_by_duct_m3_h,
            structural_issues,
        )

    invalid_tree_keys = {
        (tree.side, tree.root_id)
        for tree in trees
        if tree.unsupported_targets
    }
    for pipe_id in overlaps:
        invalid_tree_keys.update(owners[pipe_id])
    valid_trees = [
        tree
        for tree in trees
        if (tree.side, tree.root_id) not in invalid_tree_keys
    ]
    if structural_issues and not valid_trees:
        return _legacy_solution(
            ducts,
            required_total_airflow_m3_h,
            legacy_flow_by_duct_m3_h,
            structural_issues,
        )

    solution = AirTreeSolution(
        succeeded=not structural_issues,
        degraded=bool(structural_issues),
        fallback_used=bool(structural_issues),
        issues=list(structural_issues),
    )
    device_map = {device.id: device for device in request.devices}
    pipe_map = {pipe.id: pipe for pipe in ducts}
    root_share = required_total_airflow_m3_h / max(len(roots), 1)
    totals_by_side: dict[str, list[float]] = defaultdict(list)

    for tree in sorted(valid_trees, key=lambda item: (item.side, item.root_id)):
        root_device = device_map[tree.root_id]
        root_target = _finite_nonnegative(airflow_for_device(root_device)) or root_share
        tree_total = _solve_tree(
            request,
            tree,
            pipe_map,
            device_map,
            root_target,
            solution,
        )
        totals_by_side[tree.side].append(tree_total)

    solved_pipe_ids = {
        pipe_id
        for tree in valid_trees
        for pipe_id in tree.pipe_ids
    }
    fallback_ducts = [duct for duct in ducts if duct.id not in solved_pipe_ids]
    _apply_legacy_flows(
        solution,
        fallback_ducts,
        required_total_airflow_m3_h,
        legacy_flow_by_duct_m3_h,
    )

    solution.supply_total_m3_h = math.fsum(totals_by_side.get("supply", []))
    solution.return_total_m3_h = math.fsum(totals_by_side.get("return", []))
    if solution.supply_total_m3_h > 0 and solution.return_total_m3_h > 0:
        mismatch = abs(solution.supply_total_m3_h - solution.return_total_m3_h)
        tolerance = max(
            FLOW_TOLERANCE_M3_H,
            max(solution.supply_total_m3_h, solution.return_total_m3_h) * 0.01,
        )
        if mismatch > tolerance:
            solution.issues.append(
                Issue(
                    level="warning",
                    code="AIR_SUPPLY_RETURN_MISMATCH",
                    message="Solved supply and return airflow totals do not match.",
                )
            )

    solution.degraded = solution.fallback_used
    solution.issues = _sorted_issues(solution.issues)
    return solution


def _build_tree(
    root_id: str,
    side: Literal["supply", "return"],
    graph: dict[str, tuple[str, ...]],
    roles: dict[str, str | None],
    pipe_types: dict[str, str],
) -> _DirectedTree:
    blocked_types = RETURN_PIPE_TYPES if side == "supply" else SUPPLY_PIPE_TYPES
    parent_by_node: dict[str, str | None] = {root_id: None}
    children: dict[str, set[str]] = defaultdict(set)
    unsupported: set[str] = set()
    queue: deque[str] = deque([root_id])

    while queue:
        node_id = queue.popleft()
        if node_id != root_id and roles.get(node_id) in {"air_terminal", "terminal"}:
            continue
        for neighbor in graph.get(node_id, ()):
            if pipe_types.get(neighbor) in blocked_types:
                continue
            if neighbor != root_id and roles.get(neighbor) == "terminal":
                continue
            if neighbor == root_id:
                unsupported.add(node_id)
                continue
            existing_parent = parent_by_node.get(neighbor)
            if neighbor in parent_by_node:
                if existing_parent != node_id:
                    unsupported.add(neighbor)
                continue
            parent_by_node[neighbor] = node_id
            children[node_id].add(neighbor)
            queue.append(neighbor)

    pipe_ids = tuple(sorted(node_id for node_id in parent_by_node if node_id in pipe_types))
    return _DirectedTree(
        root_id=root_id,
        side=side,
        parent_by_node=parent_by_node,
        children_by_node={
            node_id: tuple(sorted(node_children))
            for node_id, node_children in children.items()
        },
        pipe_ids=pipe_ids,
        unsupported_targets=tuple(sorted(unsupported)),
    )


def _solve_tree(
    request: SystemEvaluateRequest,
    tree: _DirectedTree,
    pipe_map: dict,
    device_map: dict,
    root_target_m3_h: float,
    solution: AirTreeSolution,
) -> float:
    parent_pipe: dict[str, str | None] = {}
    for pipe_id in tree.pipe_ids:
        parent_id = tree.parent_by_node.get(pipe_id)
        while parent_id is not None and parent_id not in pipe_map:
            parent_id = tree.parent_by_node.get(parent_id)
        parent_pipe[pipe_id] = parent_id

    child_pipes: dict[str, list[str]] = defaultdict(list)
    root_pipes: list[str] = []
    for pipe_id, upstream_pipe_id in parent_pipe.items():
        if upstream_pipe_id is None:
            root_pipes.append(pipe_id)
        else:
            child_pipes[upstream_pipe_id].append(pipe_id)
    for children in child_pipes.values():
        children.sort()
    root_pipes.sort()

    leaf_ids = sorted(pipe_id for pipe_id in tree.pipe_ids if not child_pipes.get(pipe_id))
    leaf_demand_units: dict[str, int] = {}
    unresolved: list[str] = []
    for pipe_id in leaf_ids:
        pipe = pipe_map[pipe_id]
        explicit = _finite_nonnegative(pipe.flow_m3_h)
        if explicit > 0:
            leaf_demand_units[pipe_id] = _flow_units(explicit)
            solution.flow_source_by_duct[pipe_id] = "explicit_leaf_duct"
            continue
        connected = _connected_device_airflow(request, pipe_id, device_map)
        if connected > 0:
            leaf_demand_units[pipe_id] = _flow_units(connected)
            solution.flow_source_by_duct[pipe_id] = "connected_device"
            continue
        unresolved.append(pipe_id)

    resolved_units = sum(leaf_demand_units.values())
    fallback_units = max(_flow_units(root_target_m3_h) - resolved_units, 0)
    fallback_share, remainder = (
        divmod(fallback_units, len(unresolved))
        if unresolved
        else (0, 0)
    )
    for index, pipe_id in enumerate(unresolved):
        leaf_demand_units[pipe_id] = fallback_share + (
            remainder if index == len(unresolved) - 1 else 0
        )
        solution.flow_source_by_duct[pipe_id] = "equal_share_fallback"
        solution.fallback_used = True
        solution.issues.append(
            Issue(
                level="info",
                code="AIR_LEAF_DEMAND_FALLBACK",
                message="Leaf airflow demand used a deterministic equal-share fallback.",
                target=pipe_id,
            )
        )

    depths = {
        pipe_id: _pipe_depth(pipe_id, parent_pipe)
        for pipe_id in tree.pipe_ids
    }
    tree_flow_units: dict[str, int] = {}
    for pipe_id in sorted(tree.pipe_ids, key=lambda item: (-depths[item], item)):
        children = child_pipes.get(pipe_id, [])
        if children:
            flow_units = sum(tree_flow_units[child_id] for child_id in children)
            source = "aggregated_children"
        else:
            flow_units = leaf_demand_units.get(pipe_id, 0)
            source = solution.flow_source_by_duct.get(pipe_id, "equal_share_fallback")
        tree_flow_units[pipe_id] = flow_units
        solution.flow_by_duct_m3_h[pipe_id] = _flow_value(flow_units)
        solution.flow_source_by_duct[pipe_id] = source
        solution.side_by_duct[pipe_id] = tree.side
        solution.steps += 1

    for pipe_id in sorted(tree.pipe_ids):
        children = child_pipes.get(pipe_id, [])
        inflow_units = tree_flow_units[pipe_id]
        outflow_units = (
            sum(tree_flow_units[child_id] for child_id in children)
            if children
            else leaf_demand_units.get(pipe_id, 0)
        )
        solution.node_balances.append(
            AirNodeBalance(
                node_id=pipe_id,
                network_side=tree.side,
                root_id=tree.root_id,
                inflow_m3_h=_flow_value(inflow_units),
                outflow_m3_h=_flow_value(outflow_units),
                residual_m3_h=_flow_value(inflow_units - outflow_units),
            )
        )
        input_flow = _finite_nonnegative(pipe_map[pipe_id].flow_m3_h)
        solved_flow = _flow_value(tree_flow_units[pipe_id])
        tolerance = max(FLOW_TOLERANCE_M3_H, solved_flow * 0.01)
        if input_flow > 0 and abs(input_flow - solved_flow) > tolerance:
            solution.issues.append(
                Issue(
                    level="warning",
                    code="AIR_INPUT_FLOW_CONFLICT",
                    message="Explicit duct airflow differs from the conserved tree airflow.",
                    target=pipe_id,
                )
            )

    tree_total = _flow_value(sum(tree_flow_units[pipe_id] for pipe_id in root_pipes))
    solution.node_balances.append(
        AirNodeBalance(
            node_id=tree.root_id,
            network_side=tree.side,
            root_id=tree.root_id,
            inflow_m3_h=round(tree_total, 6),
            outflow_m3_h=round(tree_total, 6),
            residual_m3_h=0.0,
        )
    )
    return tree_total


def _legacy_solution(
    ducts: list,
    required_total_airflow_m3_h: float,
    legacy_flow_by_duct_m3_h: dict[str, float],
    issues: list[Issue],
) -> AirTreeSolution:
    solution = AirTreeSolution(
        succeeded=False,
        degraded=True,
        fallback_used=True,
        issues=_sorted_issues(issues),
    )
    _apply_legacy_flows(
        solution,
        ducts,
        required_total_airflow_m3_h,
        legacy_flow_by_duct_m3_h,
    )
    return solution


def _apply_legacy_flows(
    solution: AirTreeSolution,
    ducts: list,
    required_total_airflow_m3_h: float,
    legacy_flow_by_duct_m3_h: dict[str, float],
) -> None:
    equal_share = required_total_airflow_m3_h / max(len(ducts), 1)
    for duct in ducts:
        flow = (
            _finite_nonnegative(legacy_flow_by_duct_m3_h.get(duct.id))
            or _finite_nonnegative(duct.flow_m3_h)
            or equal_share
        )
        solution.flow_by_duct_m3_h[duct.id] = _finite_nonnegative(flow)
        solution.flow_source_by_duct[duct.id] = "legacy_topology"
        solution.side_by_duct[duct.id] = _side_from_pipe_type(duct.pipe_type)
        solution.steps += 1


def _directed_air_graph(
    request: SystemEvaluateRequest,
) -> tuple[dict[str, tuple[str, ...]], dict[str, tuple[str, ...]]]:
    known_ids = {device.id for device in request.devices} | {pipe.id for pipe in request.pipes}
    outgoing: dict[str, set[str]] = defaultdict(set)
    incoming: dict[str, set[str]] = defaultdict(set)
    for connection in request.connections:
        if (
            connection.system != "air"
            or connection.from_id not in known_ids
            or connection.to_id not in known_ids
        ):
            continue
        outgoing[connection.from_id].add(connection.to_id)
        incoming[connection.to_id].add(connection.from_id)
    return (
        {node_id: tuple(sorted(neighbors)) for node_id, neighbors in outgoing.items()},
        {node_id: tuple(sorted(neighbors)) for node_id, neighbors in incoming.items()},
    )


def _connected_device_airflow(request, pipe_id: str, device_map: dict) -> float:
    values: list[float] = []
    for connection in request.connections:
        if connection.system != "air":
            continue
        other_id = None
        if connection.from_id == pipe_id:
            other_id = connection.to_id
        elif connection.to_id == pipe_id:
            other_id = connection.from_id
        device = device_map.get(other_id)
        if device is None:
            continue
        role = role_for_device(device)
        if role not in {"terminal", "air_terminal", "air_accessory"}:
            continue
        values.append(
            max(
                _finite_nonnegative(airflow_for_device(device)),
                _finite_nonnegative(
                    catalog_number_for_device(device, "recommended_airflow_m3_h", 0.0)
                ),
            )
        )
    return max(values, default=0.0)


def _pipe_depth(pipe_id: str, parent_pipe: dict[str, str | None]) -> int:
    depth = 0
    parent_id = parent_pipe.get(pipe_id)
    while parent_id is not None:
        depth += 1
        parent_id = parent_pipe.get(parent_id)
    return depth


def _side_from_pipe_type(pipe_type: str) -> str:
    if pipe_type in SUPPLY_PIPE_TYPES:
        return "supply"
    if pipe_type in RETURN_PIPE_TYPES:
        return "return"
    return "unknown"


def _finite_nonnegative(value) -> float:
    try:
        parsed = float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(parsed) or parsed <= 0:
        return 0.0
    return min(parsed, MAX_AIRFLOW_M3_H)


def _flow_units(value: float) -> int:
    # int() 是显式类型契约：round() 无 ndigits 时已返回 int，但写出 int() 可保证
    # 日后若有人给 round 加 ndigits，本函数仍返回整数，不悄悄变成 float 而破坏
    # 下游整数守恒累加器 tree_flow_units: dict[str, int]。
    return int(round(_finite_nonnegative(value) * FLOW_SCALE))  # noqa: RUF046


def _flow_value(units: int) -> float:
    return units / FLOW_SCALE


def _sorted_issues(issues: list[Issue]) -> list[Issue]:
    return sorted(
        issues,
        key=lambda item: (item.code, item.target or "", item.level, item.message),
    )
