import math
from copy import deepcopy

import pytest
from fastapi.testclient import TestClient

from app import realtime
from app.calculators import air_tree
from app.calculators.air_network import calculate_air_network
from app.data import project_store
from app.main import app
from app.samples import MULTI_ZONE_ALL_AIR_SAMPLE
from app.schemas import PipeInput, SystemEvaluateRequest

client = TestClient(app)


def _request(payload: dict | None = None) -> SystemEvaluateRequest:
    return SystemEvaluateRequest(**(payload or deepcopy(MULTI_ZONE_ALL_AIR_SAMPLE)))


def _ducts(result) -> dict[str, object]:
    return {duct.duct_id: duct for duct in result.ducts}


def test_multi_zone_supply_and_return_trees_conserve_leaf_airflow():
    result = calculate_air_network(_request())
    ducts = _ducts(result)

    assert result.solver.method == "directed_tree_aggregation"
    assert result.solver.succeeded is True
    assert result.solver.degraded is False
    assert result.total_airflow_m3_h == 15000.0
    assert ducts["ahu_supply_connector"].airflow_m3_h == 15000.0
    assert ducts["supply_main"].airflow_m3_h == 15000.0
    assert ducts["return_main"].airflow_m3_h == 15000.0
    assert ducts["ahu_return_connector"].airflow_m3_h == 15000.0
    assert ducts["supply_main"].airflow_m3_h == sum(
        ducts[duct_id].airflow_m3_h
        for duct_id in ("supply_office", "supply_meeting", "supply_training")
    )
    assert ducts["return_main"].airflow_m3_h == sum(
        ducts[duct_id].airflow_m3_h
        for duct_id in ("return_office", "return_meeting", "return_training")
    )
    assert result.solver.max_flow_residual_m3_h == 0.0
    assert result.node_balances
    assert all(balance.residual_m3_h == 0.0 for balance in result.node_balances)
    assert all(duct.flow_source for duct in result.ducts)
    assert all(duct.network_side in {"supply", "return"} for duct in result.ducts)

    by_duct = {duct.duct_id: duct.pressure_drop_pa for duct in result.ducts}
    for path in result.paths:
        assert path.pressure_drop_pa == pytest.approx(
            sum(by_duct[duct_id] for duct_id in path.pipe_ids),
            abs=0.002,
        )
        assert path.balancing_pressure_pa == pytest.approx(
            result.required_fan_pressure_pa - path.pressure_drop_pa,
            abs=0.002,
        )


def test_leaf_change_updates_only_its_tree_and_reports_input_conflicts():
    baseline = calculate_air_network(_request())
    payload = deepcopy(MULTI_ZONE_ALL_AIR_SAMPLE)
    next(
        pipe for pipe in payload["pipes"] if pipe["id"] == "supply_office"
    )["flow_m3_h"] = 6000
    changed = calculate_air_network(_request(payload))
    before = _ducts(baseline)
    after = _ducts(changed)

    assert after["supply_main"].airflow_m3_h == 16000.0
    assert after["ahu_supply_connector"].airflow_m3_h == 16000.0
    assert after["return_main"].airflow_m3_h == before["return_main"].airflow_m3_h
    assert after["supply_office"].pressure_drop_pa > before["supply_office"].pressure_drop_pa
    codes_by_target = {(issue.code, issue.target) for issue in changed.issues}
    assert ("AIR_INPUT_FLOW_CONFLICT", "supply_main") in codes_by_target
    assert ("AIR_SUPPLY_RETURN_MISMATCH", None) in codes_by_target


def test_connection_and_pipe_order_do_not_change_solved_results():
    payload = deepcopy(MULTI_ZONE_ALL_AIR_SAMPLE)
    reordered = deepcopy(payload)
    reordered["pipes"] = list(reversed(reordered["pipes"]))
    reordered["connections"] = list(reversed(reordered["connections"]))

    first = calculate_air_network(_request(payload))
    second = calculate_air_network(_request(reordered))

    assert [duct.model_dump() for duct in first.ducts] == [
        duct.model_dump() for duct in second.ducts
    ]
    assert [path.model_dump() for path in first.paths] == [
        path.model_dump() for path in second.paths
    ]
    assert [item.model_dump() for item in first.node_balances] == [
        item.model_dump() for item in second.node_balances
    ]
    assert first.solver == second.solver


def test_cycle_returns_bounded_degraded_result_with_stable_diagnostic():
    payload = deepcopy(MULTI_ZONE_ALL_AIR_SAMPLE)
    payload["connections"].append(
        {"from_id": "supply_office", "to_id": "supply_main", "system": "air"}
    )

    result = calculate_air_network(_request(payload))

    assert result.solver.succeeded is False
    assert result.solver.degraded is True
    assert result.solver.fallback_used is True
    assert result.solver.method == "legacy_bounded_fallback"
    assert all(duct.airflow_m3_h >= 0 for duct in result.ducts)
    assert all(duct.pressure_drop_pa >= 0 for duct in result.ducts)
    assert any(issue.code == "AIR_TREE_UNSUPPORTED" for issue in result.issues)


def test_unsupported_supply_side_preserves_the_valid_return_tree():
    payload = deepcopy(MULTI_ZONE_ALL_AIR_SAMPLE)
    payload["connections"].append(
        {"from_id": "supply_office", "to_id": "supply_main", "system": "air"}
    )
    next(
        pipe for pipe in payload["pipes"] if pipe["id"] == "return_main"
    )["flow_m3_h"] = None

    result = calculate_air_network(_request(payload))
    ducts = _ducts(result)

    assert result.solver.succeeded is False
    assert result.solver.degraded is True
    assert ducts["return_main"].flow_source == "aggregated_children"
    assert ducts["return_main"].airflow_m3_h == sum(
        ducts[duct_id].airflow_m3_h
        for duct_id in ("return_office", "return_meeting", "return_training")
    )
    assert any(
        balance.network_side == "return"
        for balance in result.node_balances
    )
    assert all(
        ducts[duct_id].flow_source == "legacy_topology"
        for duct_id in (
            "ahu_supply_connector",
            "supply_main",
            "supply_office",
            "supply_meeting",
            "supply_training",
        )
    )


def test_equal_share_fallback_conserves_published_precision(
    monkeypatch: pytest.MonkeyPatch,
):
    payload = deepcopy(MULTI_ZONE_ALL_AIR_SAMPLE)
    leaf_ids = {
        "supply_office",
        "supply_meeting",
        "supply_training",
        "return_office",
        "return_meeting",
        "return_training",
    }
    for pipe in payload["pipes"]:
        if pipe["id"] in leaf_ids:
            pipe["flow_m3_h"] = None
    monkeypatch.setattr(
        air_tree,
        "airflow_for_device",
        lambda device: 1.0 if device.id == "ahu_f1" else 0.0,
    )
    monkeypatch.setattr(
        air_tree,
        "catalog_number_for_device",
        lambda *_args, **_kwargs: 0.0,
    )

    result = calculate_air_network(_request(payload))
    ducts = _ducts(result)

    assert ducts["supply_main"].airflow_m3_h == 1.0
    assert ducts["supply_main"].airflow_m3_h == sum(
        ducts[duct_id].airflow_m3_h
        for duct_id in ("supply_office", "supply_meeting", "supply_training")
    )
    assert ducts["return_main"].airflow_m3_h == sum(
        ducts[duct_id].airflow_m3_h
        for duct_id in ("return_office", "return_meeting", "return_training")
    )
    assert result.solver.max_flow_residual_m3_h == 0.0
    assert all(balance.residual_m3_h == 0.0 for balance in result.node_balances)


def test_missing_source_and_size_return_bounded_diagnostics():
    request = SystemEvaluateRequest(
        pipes=[
            {
                "id": "orphan_duct",
                "system": "air",
                "length_m": 0,
                "flow_m3_h": 100,
            }
        ]
    )

    result = calculate_air_network(request)

    assert result.solver.succeeded is False
    assert result.ducts[0].airflow_m3_h == 100.0
    assert result.ducts[0].velocity_m_s >= 0
    assert {issue.code for issue in result.issues} >= {
        "AIR_SOURCE_MISSING",
        "AIR_DUCT_SIZE_MISSING",
    }


def test_non_finite_direct_inputs_never_escape_in_calculation_results():
    request = SystemEvaluateRequest.model_construct(
        devices=[],
        pipes=[
            PipeInput.model_construct(
                id="invalid_duct",
                system="air",
                pipe_type="unknown",
                floor_id=None,
                from_floor_id=None,
                to_floor_id=None,
                length_m=float("inf"),
                flow_m3_h=float("inf"),
                diameter_mm=float("inf"),
                width_mm=None,
                height_mm=None,
                local_resistance_coefficient=float("inf"),
            )
        ],
        connections=[],
        rooms=[],
        floors=[],
        design_options={},
    )

    result = calculate_air_network(request, cooling_load_kw=float("inf"))
    values = [
        result.total_airflow_m3_h,
        result.required_fan_pressure_pa,
        result.ducts[0].airflow_m3_h,
        result.ducts[0].velocity_m_s,
        result.ducts[0].pressure_drop_pa,
    ]

    assert all(math.isfinite(value) for value in values)
    assert result.ducts[0].input_airflow_m3_h is None


def test_extreme_finite_airflow_is_bounded_without_overflow():
    request = SystemEvaluateRequest(
        pipes=[
            {
                "id": "extreme_duct",
                "system": "air",
                "length_m": 1e308,
                "flow_m3_h": 1e308,
                "diameter_mm": 1e-100,
                "local_resistance_coefficient": 1e308,
            }
        ]
    )

    result = calculate_air_network(request)
    duct = result.ducts[0]
    values = [
        result.total_airflow_m3_h,
        result.required_fan_pressure_pa,
        duct.airflow_m3_h,
        duct.velocity_m_s,
        duct.pressure_drop_pa,
        *(
            value
            for value in duct.size.values()
            if isinstance(value, (int, float))
        ),
    ]

    assert all(math.isfinite(value) for value in values)


def test_project_air_network_endpoint_is_read_only_and_matches_shared_result(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
):
    monkeypatch.setattr(project_store, "PROJECT_DIR", tmp_path)
    payload = deepcopy(MULTI_ZONE_ALL_AIR_SAMPLE)
    created = client.post(
        "/api/v1/projects",
        json={"name": "air-tree", "project": payload},
    )
    assert created.status_code == 200
    project_id = created.json()["project_id"]
    project_path = tmp_path / f"{project_id}.json"
    before_bytes = project_path.read_bytes()
    before_mtime = project_path.stat().st_mtime_ns
    runtime_keys_before = {
        "sessions": set(realtime._sessions),
        "layouts": set(realtime._layouts),
        "states": set(realtime._states),
        "controls": set(realtime._control_values),
        "subscribers": set(realtime._subscribers),
    }

    project_result = client.get(f"/api/v1/projects/{project_id}/air-network")
    direct_result = client.post("/api/v1/air-network/calculate", json=payload)
    evaluation = client.post("/api/v1/system/evaluate", json=payload)

    assert project_result.status_code == 200
    assert direct_result.status_code == 200
    assert evaluation.status_code == 200
    assert project_result.json() == direct_result.json()
    assert evaluation.json()["air_network"] == direct_result.json()
    assert evaluation.json()["steady_network"]["air"] == direct_result.json()
    assert evaluation.json()["report"]["air"]["ducts"] == direct_result.json()["ducts"]
    assert project_path.read_bytes() == before_bytes
    assert project_path.stat().st_mtime_ns == before_mtime
    assert client.get(f"/api/v1/projects/{project_id}/history").json() == []
    assert runtime_keys_before == {
        "sessions": set(realtime._sessions),
        "layouts": set(realtime._layouts),
        "states": set(realtime._states),
        "controls": set(realtime._control_values),
        "subscribers": set(realtime._subscribers),
    }

    missing = client.get("/api/v1/projects/missing-project/air-network")
    assert missing.status_code == 404
    assert missing.json()["detail"] == "Project not found."


def test_openapi_exposes_typed_air_solver_contracts():
    schema = client.get("/openapi.json").json()
    operation = schema["paths"]["/api/v1/projects/{project_id}/air-network"]["get"]
    response_ref = operation["responses"]["200"]["content"]["application/json"]["schema"]["$ref"]

    assert response_ref.endswith("/AirNetworkResult")
    air_schema = schema["components"]["schemas"]["AirNetworkResult"]
    assert air_schema["properties"]["solver"]["$ref"].endswith(
        "/AirNetworkSolverStatus"
    )
    assert air_schema["properties"]["node_balances"]["items"]["$ref"].endswith(
        "/AirNodeBalance"
    )
    assert air_schema["properties"]["balancing"]["$ref"].endswith(
        "/AirNetworkBalancing"
    )
    assert air_schema["properties"]["ducts"]["items"]["$ref"].endswith(
        "/DuctCalculation"
    )
    assert air_schema["properties"]["paths"]["items"]["$ref"].endswith(
        "/NetworkPathCalculation"
    )
    assert air_schema["properties"]["issues"]["items"]["$ref"].endswith(
        "/Issue"
    )
    assert operation["responses"]["404"]["description"] == "Project not found."


@pytest.mark.parametrize(
    "sample_name",
    ["complete", "air", "four_floor_fcu_fresh_air", "multi_zone_all_air"],
)
def test_supported_samples_keep_bounded_air_network_results(sample_name: str):
    payload = client.get(f"/api/v1/project/sample/{sample_name}").json()

    response = client.post("/api/v1/air-network/calculate", json=payload)

    assert response.status_code == 200
    body = response.json()
    assert body["solver"]["steps"] == len(body["ducts"])
    assert body["total_airflow_m3_h"] >= 0
    assert all(duct["airflow_m3_h"] >= 0 for duct in body["ducts"])
    assert all(duct["velocity_m_s"] >= 0 for duct in body["ducts"])
    assert all(duct["pressure_drop_pa"] >= 0 for duct in body["ducts"])


def test_air_network_post_keeps_framework_validation_errors():
    response = client.post(
        "/api/v1/air-network/calculate",
        json={
            "pipes": [
                {
                    "id": "invalid",
                    "system": "air",
                    "flow_m3_h": 0,
                }
            ]
        },
    )

    assert response.status_code == 422
    assert response.json()["detail"][0]["type"] == "greater_than"
