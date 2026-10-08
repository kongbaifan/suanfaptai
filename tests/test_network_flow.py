"""Hand-calculated hydraulic envelopes and conservative shared path flows."""

import math

import pytest

from app.simcore.network_flow import FlowDrive, FlowPath, allocate_paths
from app.simcore.runtime_network import unique_route


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
