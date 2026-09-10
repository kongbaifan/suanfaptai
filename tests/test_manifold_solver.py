from copy import deepcopy

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.samples import MANIFOLD_FCU_DUAL_WATER_SAMPLE, MANIFOLD_FCU_SAMPLE
from app.schemas import ConnectionInput, DeviceInput, SystemEvaluateRequest
from app.steady_solver.port_graph import build_port_graph
from app.steady_solver.solver import build_steady_network_result, solve_steady_state
from app.steady_solver.topology_validator import validate_topology


client = TestClient(app)


def _sample_request() -> SystemEvaluateRequest:
    return SystemEvaluateRequest(**deepcopy(MANIFOLD_FCU_SAMPLE))


def _dual_water_request() -> SystemEvaluateRequest:
    return SystemEvaluateRequest(**deepcopy(MANIFOLD_FCU_DUAL_WATER_SAMPLE))


def _binding_snapshot(request: SystemEvaluateRequest) -> list[tuple[str, str, int, str]]:
    graph = build_port_graph(request)
    return sorted(
        (
            binding.manifold_id,
            binding.peer_id,
            binding.port_index,
            binding.kind,
        )
        for binding in graph.manifold_bindings
    )


def test_two_branch_manifold_ports_are_dynamic_and_deterministic():
    request = _sample_request()
    graph = build_port_graph(request)
    reordered = request.model_copy(
        update={
            "devices": list(reversed(request.devices)),
            "pipes": list(reversed(request.pipes)),
            "connections": list(reversed(request.connections)),
        }
    )

    assert [port.port_index for port in graph.device_ports["supply_header"]] == [0, 1, 2]
    assert [port.port_index for port in graph.device_ports["return_header"]] == [0, 1, 2]
    assert _binding_snapshot(request) == _binding_snapshot(reordered)
    assert {
        (binding.peer_id, binding.port_index)
        for binding in graph.manifold_bindings
        if binding.manifold_id == "supply_header"
    } == {
        ("chw_main_supply", 0),
        ("chw_branch_supply_f1", 1),
        ("chw_branch_supply_f2", 2),
    }


def test_direct_manifold_connections_use_stable_peer_order():
    devices = [
        DeviceInput(id="pump", type="循环泵"),
        DeviceInput(id="header", type="分水器"),
        DeviceInput(id="fcu_b", type="风机盘管"),
        DeviceInput(id="fcu_a", type="风机盘管"),
    ]
    connections = [
        ConnectionInput(from_id="pump", to_id="header", system="chilled_water"),
        ConnectionInput(from_id="header", to_id="fcu_b", system="chilled_water"),
        ConnectionInput(from_id="header", to_id="fcu_a", system="chilled_water"),
    ]
    request = SystemEvaluateRequest(devices=devices, connections=connections)
    reordered = SystemEvaluateRequest(
        devices=list(reversed(devices)),
        connections=list(reversed(connections)),
    )

    assert _binding_snapshot(request) == _binding_snapshot(reordered)
    assert _binding_snapshot(request) == [
        ("header", "fcu_a", 1, "branch"),
        ("header", "fcu_b", 2, "branch"),
        ("header", "pump", 0, "main"),
    ]


def test_supply_and_return_manifolds_conserve_published_flow():
    result = build_steady_network_result(
        solve_steady_state(_sample_request(), cooling_load_kw=100.0)
    )
    medium_result = result.media["chilled_water"]
    manifolds = {
        item.manifold_id: item
        for item in medium_result.manifolds
    }

    assert set(manifolds) == {"supply_header", "return_header"}
    assert medium_result.total_demand_m3_h == pytest.approx(17.2)
    # 2 台 FP-204WA，单台额定水流量 1.892 m3/h
    assert medium_result.actual_flow_m3_h == pytest.approx(3.784)
    assert medium_result.unmet_flow_m3_h == pytest.approx(13.416)
    assert medium_result.unmet_load_kw == pytest.approx(78.0)
    assert medium_result.limiting_device_ids == ["fcu_f1", "fcu_f2"]
    for manifold in manifolds.values():
        assert manifold.main_flow_m3_h == pytest.approx(
            medium_result.actual_flow_m3_h,
            abs=0.001,
        )
        assert manifold.main_flow_m3_h == pytest.approx(
            sum(manifold.branch_flows_m3_h.values()),
            abs=0.001,
        )
        assert manifold.residual_m3_h == 0.0
        assert manifold.main_flow_m3_h == round(manifold.main_flow_m3_h, 3)
        assert all(
            flow == round(flow, 3)
            for flow in manifold.branch_flows_m3_h.values()
        )

    pipe_flows = {
        edge.pipe_id: edge.flow_m3_h
        for edge in medium_result.edges
        if edge.pipe_id is not None
    }
    port_flows = {
        (port.owner_id, port.port_index): port.flow_m3_h_by_medium["chilled_water"]
        for port in result.ports
        if "chilled_water" in port.flow_m3_h_by_medium
    }
    for manifold in manifolds.values():
        for peer_id, port_index in manifold.port_bindings.items():
            expected_flow = (
                manifold.main_flow_m3_h
                if port_index == 0
                else manifold.branch_flows_m3_h[peer_id]
            )
            assert pipe_flows[peer_id] == pytest.approx(expected_flow, abs=0.001)
            assert port_flows[(manifold.manifold_id, port_index)] == pytest.approx(
                expected_flow,
                abs=0.001,
            )


def test_manifold_binding_diagnostics_are_bounded():
    missing_main = deepcopy(MANIFOLD_FCU_SAMPLE)
    missing_main["connections"] = [
        connection
        for connection in missing_main["connections"]
        if not (
            connection["from_id"] == "chw_main_supply"
            and connection["to_id"] == "supply_header"
        )
    ]
    missing_issues = validate_topology(
        build_port_graph(SystemEvaluateRequest(**missing_main))
    )
    assert sum(
        issue.code == "manifold_missing_main" and issue.target == "supply_header"
        for issue in missing_issues
    ) == 1

    duplicate_branch = deepcopy(MANIFOLD_FCU_SAMPLE)
    duplicate_branch["connections"].append(
        {
            "from_id": "supply_header",
            "to_id": "chw_branch_supply_f1",
            "system": "chilled_water",
        }
    )
    duplicate_issues = validate_topology(
        build_port_graph(SystemEvaluateRequest(**duplicate_branch))
    )
    assert sum(
        issue.code == "manifold_duplicate_branch"
        and issue.target == "supply_header"
        for issue in duplicate_issues
    ) == 1


def test_manifold_sample_api_exposes_flow_and_port_diagnostics():
    sample_response = client.get("/api/v1/project/sample/manifold_fcu")
    assert sample_response.status_code == 200

    response = client.post("/api/v1/system/evaluate", json=sample_response.json())
    assert response.status_code == 200
    body = response.json()
    medium = body["steady_network"]["media"]["chilled_water"]
    manifolds = medium["manifolds"]

    assert {item["manifold_id"] for item in manifolds} == {
        "supply_header",
        "return_header",
    }
    assert 0.0 < medium["actual_flow_m3_h"] < medium["total_demand_m3_h"]
    assert medium["unmet_flow_m3_h"] > 0.0
    assert medium["unmet_load_kw"] > 0.0
    assert medium["limiting_device_ids"] == ["fcu_f1", "fcu_f2"]
    assert "flow_capacity_limited" in {
        issue["code"] for issue in body["steady_network"]["issues"]
    }
    assert all(item["port_bindings"] for item in manifolds)
    assert all(item["residual_m3_h"] == 0.0 for item in manifolds)


def test_dual_water_manifolds_are_independent_and_deterministic():
    request = _dual_water_request()
    reordered = request.model_copy(
        update={
            "devices": list(reversed(request.devices)),
            "pipes": list(reversed(request.pipes)),
            "connections": list(reversed(request.connections)),
        }
    )
    result = build_steady_network_result(
        solve_steady_state(
            request,
            cooling_load_kw=100.0,
            heating_load_kw=80.0,
        )
    )
    reordered_result = build_steady_network_result(
        solve_steady_state(
            reordered,
            cooling_load_kw=100.0,
            heating_load_kw=80.0,
        )
    )

    assert result.media["chilled_water"].total_demand_m3_h == pytest.approx(17.2)
    assert result.media["hot_water"].total_demand_m3_h == pytest.approx(6.88)
    # 2 台 FP-204WA：冷侧 5K 温差 1.892 m3/h/台，热侧 10K 温差 0.946 m3/h/台
    assert result.media["chilled_water"].actual_flow_m3_h == pytest.approx(3.784)
    assert result.media["hot_water"].actual_flow_m3_h == pytest.approx(1.892)
    assert result.media["chilled_water"].unmet_load_kw == pytest.approx(78.0)
    assert result.media["hot_water"].unmet_load_kw == pytest.approx(58.0)
    assert result.media["chilled_water"].model_dump() == reordered_result.media[
        "chilled_water"
    ].model_dump()
    assert result.media["hot_water"].model_dump() == reordered_result.media[
        "hot_water"
    ].model_dump()

    expected_manifolds = {
        "chilled_water": {"supply_header", "return_header"},
        "hot_water": {"hot_supply_header", "hot_return_header"},
    }
    for medium, manifold_ids in expected_manifolds.items():
        medium_result = result.media[medium]
        manifolds = {
            manifold.manifold_id: manifold
            for manifold in medium_result.manifolds
        }
        assert set(manifolds) == manifold_ids
        assert medium_result.converged is True
        assert medium_result.max_conservation_residual_m3_h == 0.0
        assert medium_result.limiting_device_ids == ["fcu_f1", "fcu_f2"]
        for manifold in manifolds.values():
            assert manifold.main_flow_m3_h == pytest.approx(
                medium_result.actual_flow_m3_h,
                abs=0.001,
            )
            assert manifold.main_flow_m3_h == pytest.approx(
                sum(manifold.branch_flows_m3_h.values()),
                abs=0.001,
            )

    graph = build_port_graph(request)
    for terminal_id in ("fcu_f1", "fcu_f2"):
        terminal_media = {
            port.medium
            for port in graph.device_ports[terminal_id]
        }
        assert {"chilled_water", "hot_water"} <= terminal_media


def test_dual_water_manifold_sample_api_exposes_both_water_media():
    sample_response = client.get(
        "/api/v1/project/sample/manifold_fcu_dual_water"
    )
    assert sample_response.status_code == 200

    response = client.post("/api/v1/system/evaluate", json=sample_response.json())
    assert response.status_code == 200
    media = response.json()["steady_network"]["media"]

    for medium in ("chilled_water", "hot_water"):
        assert media[medium]["converged"] is True
        assert media[medium]["total_demand_m3_h"] > 0.0
        assert 0.0 < media[medium]["actual_flow_m3_h"] <= media[medium][
            "total_demand_m3_h"
        ]
        assert media[medium]["unmet_flow_m3_h"] >= 0.0
        assert media[medium]["limiting_device_ids"] == ["fcu_f1", "fcu_f2"]
        assert len(media[medium]["manifolds"]) == 2
        assert all(
            manifold["main_flow_m3_h"]
            == pytest.approx(media[medium]["actual_flow_m3_h"], abs=0.001)
            for manifold in media[medium]["manifolds"]
        )


def test_manifold_flow_capacity_reports_limiting_pump():
    request = _sample_request()
    request = request.model_copy(
        update={
            "devices": [
                device.model_copy(update={"flow_m3_h": 1.0})
                if device.id == "pump_b1"
                else device
                for device in request.devices
            ]
        }
    )

    result = solve_steady_state(request, cooling_load_kw=100.0)
    network = build_steady_network_result(result)
    medium = network.media["chilled_water"]

    assert medium.total_demand_m3_h == pytest.approx(17.2)
    assert medium.actual_flow_m3_h == pytest.approx(1.0)
    assert medium.unmet_flow_m3_h == pytest.approx(16.2)
    assert medium.unmet_load_kw == pytest.approx(100.0 - 1.0 * 5.0 / 0.86)
    assert medium.limiting_device_ids == ["pump_b1"]
    assert any(
        issue.code == "flow_capacity_limited"
        and issue.target == "chilled_water_network"
        for issue in result.issues or []
    )
