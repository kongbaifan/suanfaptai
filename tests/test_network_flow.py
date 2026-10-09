"""Hand-calculated hydraulic envelopes and conservative shared path flows."""

import math
from random import Random

import pytest

from app.simcore.network_flow import FlowDrive, FlowPath, allocate_paths
from app.simcore.runtime_network import (
    _serial_pump_pair,
    _strong_components,
    unique_route,
)


def test_one_path_matches_pressure_flow_hand_calculation():
    # Δp = R Q²; 400 Pa / (100 Pa/(m³/h)²) gives Q=2 m³/h.
    path = FlowPath(
        "p", "water", "c", "source", "pump", "terminal", (("water", "pipe"),)
    )
    result = allocate_paths(
        (path,),
        {("water", "pipe"): 100},
        {("water", "pump"): FlowDrive(10, 400)},
        {("water", "terminal"): 10},
        {("water", "terminal"): 10},
        {},
    )
    assert result.path_flows["p"] == pytest.approx(2)
    assert result.pressure_by_path["p"] == pytest.approx(400)


def test_series_pump_heads_add_and_the_smaller_flow_limit_wins():
    path = FlowPath(
        "p", "water", "c", "source", "p1", "terminal", (("water", "pipe"),),
        drive_ids=("p1", "p2"),
    )
    drives = {("water", "p1"): FlowDrive(10, 400), ("water", "p2"): FlowDrive(8, 500)}
    caps = {("water", "terminal"): 10}
    result = allocate_paths((path,), {("water", "pipe"): 100}, drives, caps, caps, {})
    # (400+500) Pa = 100 * Q², hence Q=3 m³/h through BOTH pumps.
    assert result.path_flows["p"] == pytest.approx(3)
    assert result.drive_flows == pytest.approx({("water", "p1"): 3, ("water", "p2"): 3})
    assert result.source_flows[("water", "source")] == pytest.approx(3)
    assert result.pressure_by_path["p"] == pytest.approx(900)
    drives[("water", "p2")] = FlowDrive(2, 500)
    limited = allocate_paths((path,), {("water", "pipe"): 100}, drives, caps, caps, {})
    assert limited.path_flows["p"] == pytest.approx(2)


def test_bypass_competes_for_shared_pump_flow_and_can_be_closed():
    paths = (
        FlowPath("load", "water", "c", "s", "p", "terminal", (("water", "main"), ("water", "load"))),
        FlowPath("bypass", "water", "c", "s", "p", "", (("water", "main"), ("water", "bypass")), bypass=True),
    )
    resistance = {("water", key): 1 for key in ("main", "load", "bypass")}
    caps = {("water", "terminal"): 10}
    drives = {("water", "p"): FlowDrive(10, 5)}
    opened = allocate_paths(paths, resistance, drives, caps, caps, {})
    # Equal reference capacities/resistance: Q_load=Q_bypass=q;
    # shared-main plus branch loss = (2q)² + q² = 5 Pa -> q=1.
    assert opened.path_flows == pytest.approx({"load": 1, "bypass": 1})
    assert opened.source_flows[("water", "s")] == pytest.approx(2)
    assert opened.target_flows == pytest.approx({("water", "terminal"): 1})
    closed = allocate_paths(paths, resistance, drives, caps, caps, {("water", "bypass"): 0})
    assert closed.path_flows["bypass"] == 0
    assert closed.path_flows["load"] == pytest.approx(math.sqrt(5 / 2))


def test_shared_bypass_valve_keeps_both_parallel_pump_budgets():
    paths = tuple(
        FlowPath(str(i), "water", "c", "s", f"p{i}", "", (("water", "valve"),), bypass=True)
        for i in (1, 2)
    )
    drives = {("water", f"p{i}"): FlowDrive(5, 10000) for i in (1, 2)}
    for opening, expected in ((1.0, 10.0), (0.5, 5.0)):
        solved = allocate_paths(
            paths, {("water", "valve"): 1}, drives, {}, {},
            {("water", "valve"): opening}, {("water", "s"): 10},
        )
        assert solved.source_flows[("water", "s")] == pytest.approx(expected)
        assert solved.drive_flows == pytest.approx({("water", "p1"): expected / 2, ("water", "p2"): expected / 2})


def test_structure_detection_matches_an_independent_reachability_reference():
    rng = Random(721)
    for _ in range(300):
        nodes = [str(index) for index in range(8)]
        graph = {a: tuple(b for b in nodes if rng.random() < 0.2) for a in nodes}
        # Floyd-Warshall closure is intentionally different from the production
        # SCC traversal and two-origin propagation, and is small enough here.
        reach = {a: {a, *graph[a]} for a in nodes}
        for via in nodes:
            for start in nodes:
                if via in reach[start]:
                    reach[start].update(reach[via])
        expected = {frozenset(b for b in nodes if b in reach[a] and a in reach[b]) for a in nodes}
        assert {frozenset(component) for component in _strong_components(graph)} == expected
        pumps = set(rng.sample(nodes, 3))
        pair = _serial_pump_pair(graph, pumps)
        assert (pair is not None) == any(a != b and b in reach[a] for a in pumps for b in pumps)
        if pair is not None:
            assert pair[0] != pair[1] and pair[1] in reach[pair[0]]


def test_structure_detection_does_not_recurse_on_long_pipe_chains():
    graph = {str(index): (str((index + 1) % 2000),) for index in range(2000)}
    assert _strong_components(graph) == [set(graph)]


def test_shared_main_uses_total_flow_once():
    # Equal branches Q1=Q2=q. Each route loses (2q)² + q²=5q² Pa.
    # With 5 Pa available: q=1 and the shared main carries 2 m³/h.
    paths = tuple(
        FlowPath(
            str(i),
            "water",
            "c",
            "s",
            "p",
            f"t{i}",
            (("water", "main"), ("water", f"b{i}")),
        )
        for i in (1, 2)
    )
    resistance = {("water", key): 1 for key in ("main", "b1", "b2")}
    caps = {("water", f"t{i}"): 10 for i in (1, 2)}
    result = allocate_paths(
        paths, resistance, {("water", "p"): FlowDrive(20, 5)}, caps, caps, {}
    )
    assert result.path_flows == pytest.approx({"1": 1, "2": 1})
    assert result.element_flows[("water", "main")] == pytest.approx(2)
    assert max(result.pressure_by_path.values()) <= 5 + 1e-12


def test_parallel_pumps_share_terminal_cap_and_stop_independently():
    paths = tuple(
        FlowPath(
            str(i), "water", "c", "source", f"pump{i}", "terminal", (("water", "pipe"),)
        )
        for i in (1, 2)
    )
    caps = {("water", "terminal"): 6}
    drives = {("water", f"pump{i}"): FlowDrive(4, 100) for i in (1, 2)}
    result = allocate_paths(paths, {("water", "pipe"): 1}, drives, caps, caps, {})
    assert sum(result.path_flows.values()) == pytest.approx(6)
    assert result.drive_flows == pytest.approx(
        {("water", "pump1"): 3, ("water", "pump2"): 3}
    )
    drives[("water", "pump1")] = FlowDrive(0, 0)
    stopped = allocate_paths(paths, {("water", "pipe"): 1}, drives, caps, caps, {})
    assert stopped.path_flows == pytest.approx({"1": 0, "2": 4})


def test_closed_path_cannot_receive_residual_redistribution():
    paths = tuple(
        FlowPath(
            str(i),
            "water",
            "c",
            "s",
            "p",
            f"t{i}",
            (("water", "main"), ("water", f"v{i}")),
        )
        for i in (1, 2)
    )
    caps = {("water", f"t{i}"): 10 for i in (1, 2)}
    result = allocate_paths(
        paths,
        {("water", "main"): 1},
        {("water", "p"): FlowDrive(6, 1000)},
        caps,
        caps,
        {("water", "v1"): 0.0},
    )
    assert result.path_flows == pytest.approx({"1": 0, "2": 6})
    assert all(
        math.isfinite(value) and value >= 0 for value in result.element_flows.values()
    )


@pytest.mark.parametrize(
    "graph",
    [
        {"a": ("b", "c"), "b": ("d",), "c": ("d",), "d": ()},
        {"a": ("b",), "b": ("c", "d"), "c": ("b",), "d": ()},
    ],
)
def test_routes_reject_alternatives_and_cycles_without_path_enumeration(graph):
    assert unique_route("a", "d", graph, set()) == ((), True)
