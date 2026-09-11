from copy import deepcopy

from fastapi.testclient import TestClient

from app.calculators.air_network import calculate_air_network
from app.calculators.hydraulic_network import calculate_hydraulic_network
from app.calculators.simulation import simulate_steady_state
from app.main import app
from app.schemas import LoadResult, SystemEvaluateRequest


client = TestClient(app)


def _system_payload():
    return {
        "rooms": [{"name": "r1", "area_m2": 100, "people": 10}],
        "devices": [
            {"id": "chiller", "type": "冷源机组", "name": "SCWE-90H+B"},
            {"id": "pump", "type": "循环泵", "name": "EC80"},
            {"id": "tower", "type": "冷却塔", "name": "CT-100"},
            {"id": "terminal", "type": "风机盘管", "name": "MCW1000V", "quantity": 4},
        ],
        "pipes": [
            {"id": "chw_1", "system": "chilled_water", "length_m": 20, "diameter_mm": 65},
            {"id": "chw_2", "system": "chilled_water", "length_m": 20, "diameter_mm": 65},
            {"id": "cw_1", "system": "cooling_water", "length_m": 15, "diameter_mm": 80},
            {"id": "cw_2", "system": "cooling_water", "length_m": 15, "diameter_mm": 80},
            {"id": "duct_1", "system": "air", "length_m": 12, "width_mm": 500, "height_mm": 320},
        ],
        "connections": [
            {"from_id": "chiller", "to_id": "pump", "system": "chilled_water"},
            {"from_id": "pump", "to_id": "chw_1", "system": "chilled_water"},
            {"from_id": "chw_1", "to_id": "terminal", "system": "chilled_water"},
            {"from_id": "terminal", "to_id": "chw_2", "system": "chilled_water"},
            {"from_id": "chw_2", "to_id": "chiller", "system": "chilled_water"},
            {"from_id": "chiller", "to_id": "cw_1", "system": "cooling_water"},
            {"from_id": "cw_1", "to_id": "tower", "system": "cooling_water"},
            {"from_id": "tower", "to_id": "cw_2", "system": "cooling_water"},
            {"from_id": "cw_2", "to_id": "chiller", "system": "cooling_water"},
            {"from_id": "terminal", "to_id": "duct_1", "system": "air"},
        ],
    }


def test_hydraulic_network_calculates_pipe_pressure():
    request = SystemEvaluateRequest(**_system_payload())
    result = calculate_hydraulic_network(request, cooling_load_kw=100, heating_load_kw=50)

    assert result.total_flow_by_system_m3_h["chilled_water"] > 0
    assert result.required_pump_head_m > 0
    assert result.most_unfavorable_pipe_id is not None
    assert result.most_unfavorable_path_id is not None
    assert result.paths
    assert result.pump_check is not None
    assert result.balancing
    assert len(result.pipes) == 4


def test_required_pump_head_uses_only_complete_chilled_water_paths():
    payload = _system_payload()
    for pipe in payload["pipes"]:
        if pipe["system"] == "cooling_water":
            pipe["length_m"] = 500
            pipe["diameter_mm"] = 20
    result = calculate_hydraulic_network(
        SystemEvaluateRequest(**payload), cooling_load_kw=100
    )
    chilled_heads = [
        path.head_m
        for path in result.paths
        if path.system == "chilled_water" and path.complete
    ]
    other_heads = [
        path.head_m
        for path in result.paths
        if path.system != "chilled_water" and path.complete
    ]

    assert max(other_heads) > max(chilled_heads)
    assert result.required_pump_head_m == max(chilled_heads)
    assert result.pump_check is not None
    assert result.pump_check.required_head_m == max(chilled_heads)


def test_required_pump_head_fallback_sums_only_chilled_water_pipes():
    request = SystemEvaluateRequest(
        pipes=[
            {
                "id": "chw",
                "system": "chilled_water",
                "length_m": 10,
                "diameter_mm": 80,
            },
            {
                "id": "cw",
                "system": "cooling_water",
                "length_m": 500,
                "diameter_mm": 20,
            },
        ]
    )

    result = calculate_hydraulic_network(request, cooling_load_kw=100)
    chilled_head = sum(
        pipe.pump_head_m or 0.0
        for pipe in result.pipes
        if pipe.system == "chilled_water"
    )
    cooling_head = sum(
        pipe.pump_head_m or 0.0
        for pipe in result.pipes
        if pipe.system == "cooling_water"
    )

    assert result.paths == []
    assert cooling_head > chilled_head
    assert result.required_pump_head_m == round(chilled_head, 3)


def test_parallel_device_quantity_does_not_multiply_single_path_resistance():
    single_payload = _system_payload()
    single_payload["devices"][-1]["quantity"] = 1
    parallel_payload = deepcopy(single_payload)
    parallel_payload["devices"][-1]["quantity"] = 2

    single = calculate_hydraulic_network(
        SystemEvaluateRequest(**single_payload), cooling_load_kw=100
    )
    parallel = calculate_hydraulic_network(
        SystemEvaluateRequest(**parallel_payload), cooling_load_kw=100
    )

    assert {
        pipe.pipe_id: pipe.pressure_drop_pa for pipe in single.pipes
    } == {
        pipe.pipe_id: pipe.pressure_drop_pa for pipe in parallel.pipes
    }
    assert parallel.required_pump_head_m == single.required_pump_head_m


def test_air_network_calculates_duct_pressure():
    request = SystemEvaluateRequest(**_system_payload())
    result = calculate_air_network(request, cooling_load_kw=80)

    assert result.total_airflow_m3_h > 0
    assert result.required_fan_pressure_pa > 0
    assert result.ducts[0].duct_id == "duct_1"
    assert result.paths
    assert result.most_unfavorable_path_id is not None
    assert result.fan_check is not None


def test_system_evaluate_contains_full_outputs():
    response = client.post("/api/v1/system/evaluate", json=_system_payload())

    assert response.status_code == 200
    body = response.json()
    assert body["hydraulic_network"]["pipes"]
    assert body["air_network"]["ducts"]
    assert body["topology"]["paths"]
    assert body["topology"]["pipe_flow_m3_h"]["chw_1"] > 0
    assert body["hydraulic_network"]["paths"]
    assert body["hydraulic_network"]["pump_check"]["status"] in {"ok", "warning", "error"}
    assert body["air_network"]["paths"]
    assert body["air_network"]["fan_check"]["status"] in {"ok", "warning", "error"}
    assert body["simulation"]["total_energy_kw"] > 0
    assert "score" not in body["report"]["summary"]


def test_design_load_drives_shared_core_energy_and_published_total():
    request = SystemEvaluateRequest(**_system_payload())

    def simulate(cooling_kw: float):
        load = LoadResult(
            total_cooling_kw=cooling_kw,
            total_heating_kw=0.0,
            adjusted_cooling_kw=cooling_kw,
            adjusted_heating_kw=0.0,
            rooms=[],
        )
        return simulate_steady_state(request, load, None, None)

    low = simulate(20.0)
    high = simulate(100.0)

    assert high.cooling_energy_kw > low.cooling_energy_kw > 0.0
    assert high.total_energy_kw == round(
        high.cooling_energy_kw + high.pump_energy_kw + high.fan_energy_kw,
        3,
    )


def test_report_endpoint():
    response = client.post("/api/v1/report/generate", json=_system_payload())

    assert response.status_code == 200
    body = response.json()
    assert body["summary"]["device_count"] == 4
    assert "cold_source" in body["equipment"]


def test_simulation_detects_capacity_deficit():
    payload = _system_payload()
    payload["devices"][0] = {"id": "chiller", "type": "冷源机组", "name": "SCWE-90H+B", "capacity_kw": 5}

    response = client.post("/api/v1/simulation/steady-state", json=payload)

    assert response.status_code == 200
    codes = {issue["code"] for issue in response.json()["issues"]}
    assert "SIM_CAPACITY_DEFICIT" in codes


def test_topology_endpoint_returns_paths_and_open_nodes():
    response = client.post("/api/v1/topology/analyze", json=_system_payload())

    assert response.status_code == 200
    body = response.json()
    assert body["node_count"] > 0
    assert body["edge_count_by_system"]["chilled_water"] > 0
    assert any(path["name"] == "cold_source_pump_terminal" for path in body["paths"])
    assert body["pipe_flow_m3_h"]["chw_1"] > 0


def test_energy_simulation_endpoint_returns_hourly_profile():
    response = client.post("/api/v1/energy/simulate", json={"project": _system_payload(), "hours": 24, "electricity_price_per_kwh": 1.0})

    assert response.status_code == 200
    body = response.json()
    assert body["hours"] == 24
    assert len(body["hourly"]) == 24
    assert body["total_energy_kwh"] > 0
    assert abs(body["estimated_cost"] - body["total_energy_kwh"]) < 0.01


def test_evaluate_endpoint_returns_topology_paths_and_issues():
    response = client.post("/api/v1/system/evaluate", json=_system_payload())

    assert response.status_code == 200
    body = response.json()
    assert body["topology"]["paths"]
    assert isinstance(body["issues"], list)
