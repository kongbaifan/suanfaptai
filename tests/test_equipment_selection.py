from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app import equipment_selection, realtime
from app.data import project_store
from app.data.equipment import CATALOG_PATH, load_equipment_catalog
from app.equipment_selection import recommend_equipment
from app.main import app
from app.samples import COMPLETE_SYSTEM_SAMPLE
from app.schemas import SelectionRecommendationResult, SelectionRequirements


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(project_store, "PROJECT_DIR", tmp_path / "projects")
    monkeypatch.setattr(realtime, "HISTORY_DIR", tmp_path / "realtime_history")
    for store in _runtime_stores():
        store.clear()
    yield TestClient(app)
    for task in tuple(realtime._sim_tasks.values()):
        task.cancel()
    for store in _runtime_stores():
        store.clear()


def _runtime_stores():
    return (
        realtime._states,
        realtime._updated_at,
        realtime._alarms,
        realtime._summary,
        realtime._subscribers,
        realtime._sim_tasks,
        realtime._sim_configs,
        realtime._sessions,
        realtime._layouts,
        realtime._control_values,
        realtime._last_floor_states,
        realtime._last_control_state,
    )


def _network_project() -> dict:
    return deepcopy(COMPLETE_SYSTEM_SAMPLE)


def _load_only_project() -> dict:
    project = _network_project()
    project["devices"] = []
    project["pipes"] = []
    project["connections"] = []
    # 设备清空后房间的 device_ids 会指向不存在的设备，落盘会被 ROOM_BINDING_UNKNOWN 拒掉。
    for room in project["rooms"]:
        room["device_ids"] = []
    return project


def _save_project(client: TestClient, project: dict | None = None) -> str:
    response = client.post(
        "/api/v1/projects",
        json={"name": "selection-test", "project": project or _network_project()},
    )
    assert response.status_code == 200, response.text
    return response.json()["project_id"]


def _recommend(client: TestClient, project_id: str, role: str):
    return client.post(
        f"/api/v1/selection/recommend/{project_id}",
        json={"target_role": role},
    )


def _requirements(
    *, cooling: float = 100.0, heating: float = 80.0,
    flow: float = 10.0, hot_water_flow: float = 8.0,
    head: float = 20.0, airflow: float = 10_000.0,
) -> SelectionRequirements:
    return SelectionRequirements(
        cooling_load_kw=cooling,
        heating_load_kw=heating,
        chilled_water_flow_m3_h=flow,
        hot_water_flow_m3_h=hot_water_flow,
        required_pump_head_m=head,
        supply_airflow_m3_h=airflow,
    )


def _stub_project_selection(
    monkeypatch: pytest.MonkeyPatch,
    rows: list[dict],
    requirements: SelectionRequirements,
) -> None:
    record = SimpleNamespace(project=SimpleNamespace(rooms=[object()]))
    monkeypatch.setattr(equipment_selection, "get_project", lambda _project_id: record)
    monkeypatch.setattr(
        equipment_selection, "_derive_requirements", lambda _record: requirements
    )
    monkeypatch.setattr(equipment_selection, "load_equipment_catalog", lambda: rows)


def test_cold_source_high_margin_is_advisory_and_boundaries_are_inclusive(
    monkeypatch: pytest.MonkeyPatch,
):
    rows = [
        {
            "type": "冷源机组",
            "name": "LOWER",
            "role": "cold_source",
            "capacity_kw": 100.0,
            "chilled_flow_m3_h": 10.0,
        },
        {
            "type": "冷源机组",
            "name": "UPPER",
            "role": "cold_source",
            "capacity_kw": 135.0,
            "chilled_flow_m3_h": 10.0,
        },
        {
            "type": "冷源机组",
            "name": "OVERSIZE",
            "role": "cold_source",
            "capacity_kw": 136.0,
            "chilled_flow_m3_h": 10.0,
        },
        {
            "type": "冷源机组",
            "name": "*",
            "role": "cold_source",
            "capacity_kw": 100.0,
            "chilled_flow_m3_h": 10.0,
        },
    ]
    _stub_project_selection(monkeypatch, rows, _requirements())

    result = recommend_equipment("project", "cold_source")

    assert result.status == "recommended"
    assert [candidate.model_name for candidate in result.candidates] == [
        "LOWER",
        "UPPER",
        "OVERSIZE",
    ]
    assert [candidate.eligible for candidate in result.candidates] == [
        True,
        True,
        True,
    ]
    assert result.candidates[1].margins.capacity == 1.35
    assert result.candidates[2].reasons[0].code == "CAPACITY_MARGIN_HIGH"


def test_pump_parallel_flow_and_power_scale_but_head_stays_per_unit(
    monkeypatch: pytest.MonkeyPatch,
):
    _stub_project_selection(
        monkeypatch,
        [
            {
                "type": "循环泵",
                "name": "P-10",
                "role": "pump",
                "flow_m3_h": 10.0,
                "head_m": 22.0,
                "power_kw": 1.5,
            }
        ],
        _requirements(flow=25.0, head=20.0),
    )

    candidate = recommend_equipment("project", "pump").candidates[0]

    assert candidate.quantity == 3
    assert candidate.provided.flow_m3_h == 30.0
    assert candidate.provided.power_kw == 4.5
    assert candidate.provided.head_m == 22.0
    assert candidate.margins.flow == 1.2
    assert candidate.margins.head == 1.1
    assert candidate.eligible is True


def test_catalog_pump_models_cover_initial_selection_flow_range():
    rows = [row for row in load_equipment_catalog() if row.get("role") == "pump"]

    assert [
        (row["name"], row["flow_m3_h"], row["head_m"], row["power_kw"])
        for row in rows
    ] == [
        ("TPE3 40-200", 20.51, 14.9, 1.119),
        ("TPE3 40-80", 12.31, 5.4, 0.246),
        ("TPE3 65-150", 32.71, 9.8, 1.119),
        ("TPE3 80-150", 44.52, 9.9, 1.491),
        ("TPE3 80-200", 82.0, 14.9, 4.7),
        ("TPE3 100-200", 128.0, 15.5, 7.5),
        ("TPE3 100-250", 128.0, 23.3, 11.0),
    ]
    assert all(row["brand"] == "Grundfos" for row in rows)
    assert all(row["variable_speed"] is True for row in rows)
    # 有厂家样本出处的四款；大流量三款为同系列相似律派生，标 derived_fields 且无订货码
    with_source = [row for row in rows if row.get("source_url")]
    assert {row["name"] for row in with_source} == {"TPE3 40-200", "TPE3 40-80", "TPE3 65-150", "TPE3 80-150"}
    # 全部泵的流量都来自厂家曲线或同系列相似律，统一标 derived_fields
    derived = [row for row in rows if "flow_m3_h" in row.get("derived_fields", [])]
    assert {row["name"] for row in derived} == {row["name"] for row in rows}


def test_pump_exact_flow_and_head_rule_boundaries_are_inclusive(
    monkeypatch: pytest.MonkeyPatch,
):
    _stub_project_selection(
        monkeypatch,
        [
            {
                "type": "循环泵",
                "name": "LOWER",
                "role": "pump",
                "flow_m3_h": 10.0,
                "head_m": 21.0,
            },
            {
                "type": "循环泵",
                "name": "UPPER",
                "role": "pump",
                "flow_m3_h": 12.5,
                "head_m": 26.0,
            },
        ],
        _requirements(flow=10.0, head=20.0),
    )

    result = recommend_equipment("project", "pump")

    assert all(candidate.eligible for candidate in result.candidates)
    assert {candidate.margins.flow for candidate in result.candidates} == {1.0, 1.25}
    assert {candidate.margins.head for candidate in result.candidates} == {1.05, 1.3}


def test_high_reasons_are_advisory_but_low_and_missing_reasons_block(
    monkeypatch: pytest.MonkeyPatch,
):
    _stub_project_selection(
        monkeypatch,
        [
            {
                "type": "循环泵",
                "name": "HIGH",
                "role": "pump",
                "flow_m3_h": 13.0,
                "head_m": 27.0,
            },
            {
                "type": "循环泵",
                "name": "LOW",
                "role": "pump",
                "flow_m3_h": 10.0,
                "head_m": 20.0,
            },
            {
                "type": "循环泵",
                "name": "MISSING",
                "role": "pump",
                "head_m": 22.0,
            },
        ],
        _requirements(flow=10.0, head=20.0),
    )

    result = recommend_equipment("project", "pump")
    candidates = {candidate.model_name: candidate for candidate in result.candidates}

    assert result.status == "recommended"
    assert candidates["HIGH"].eligible is True
    assert {reason.code for reason in candidates["HIGH"].reasons} == {
        "FLOW_MARGIN_HIGH",
        "HEAD_MARGIN_HIGH",
    }
    assert candidates["LOW"].eligible is False
    assert [reason.code for reason in candidates["LOW"].reasons] == [
        "HEAD_MARGIN_LOW"
    ]
    assert candidates["MISSING"].eligible is False
    assert [reason.code for reason in candidates["MISSING"].reasons] == [
        "MISSING_RATED_PARAMETER"
    ]


def test_raw_requirement_precision_controls_eligibility_before_response_rounding(
    monkeypatch: pytest.MonkeyPatch,
):
    _stub_project_selection(
        monkeypatch,
        [
            {
                "type": "循环泵",
                "name": "BOUNDARY",
                "role": "pump",
                "flow_m3_h": 10.0,
                "head_m": 21.0,
            }
        ],
        _requirements(flow=10.0, head=20.00049),
    )

    result = recommend_equipment("project", "pump")
    candidate = result.candidates[0]

    assert result.requirements.required_pump_head_m == 20.0
    assert candidate.margins.head == 1.05
    assert candidate.eligible is False
    assert [reason.code for reason in candidate.reasons] == ["HEAD_MARGIN_LOW"]


def test_derive_requirements_preserves_calculator_precision(
    client: TestClient, monkeypatch: pytest.MonkeyPatch,
):
    project_id = _save_project(client)
    monkeypatch.setattr(
        equipment_selection,
        "calculate_load",
        lambda _request: SimpleNamespace(
            adjusted_cooling_kw=100.00049,
            adjusted_heating_kw=80.00049,
        ),
    )
    monkeypatch.setattr(
        equipment_selection,
        "calculate_hydraulic_network",
        lambda _project, cooling_load_kw, heating_load_kw: SimpleNamespace(
            total_flow_by_system_m3_h={
                "chilled_water": 17.20049,
                "hot_water": 6.88049,
            },
            required_pump_head_m=20.00049,
        ),
    )
    monkeypatch.setattr(
        equipment_selection,
        "calculate_air",
        lambda _request: SimpleNamespace(airflow_m3_h=10_000.00049),
    )

    requirements = equipment_selection._derive_requirements(
        project_store.get_project(project_id)
    )

    assert requirements.cooling_load_kw == 100.00049
    assert requirements.heating_load_kw == 80.00049
    assert requirements.chilled_water_flow_m3_h == 17.20049
    assert requirements.hot_water_flow_m3_h == 6.88049
    assert requirements.required_pump_head_m == 20.00049
    assert requirements.supply_airflow_m3_h == 10_000.00049


def test_terminal_uses_largest_quantity_and_scales_catalog_fan_power(
    monkeypatch: pytest.MonkeyPatch,
):
    _stub_project_selection(
        monkeypatch,
        [
            {
                "type": "风机盘管",
                "name": "TERMINAL",
                "role": "terminal",
                "capacity_kw": 100.0,
                "water_flow_m3_h": 10.0,
                "airflow_m3_h": 6000.0,
                "fan_power_kw": 0.4,
                "flow_m3_h": 999.0,
                "head_m": 999.0,
                "cop": 9.0,
            }
        ],
        _requirements(cooling=100.0, flow=10.0, airflow=10_000.0),
    )

    candidate = recommend_equipment("project", "terminal").candidates[0]

    assert candidate.quantity == 2
    assert candidate.rated.power_kw == 0.4
    assert candidate.provided.power_kw == 0.8
    assert candidate.rated.flow_m3_h is None
    assert candidate.rated.head_m is None
    assert candidate.rated.cop is None


def test_blank_missing_and_wildcard_names_are_not_catalog_candidates(
    monkeypatch: pytest.MonkeyPatch,
):
    rows = [
        {
            "type": "循环泵",
            "name": name,
            "role": "pump",
            "flow_m3_h": 10.0,
            "head_m": 22.0,
        }
        for name in (None, "", "   ", "*", " * ", " REAL ")
    ]
    _stub_project_selection(monkeypatch, rows, _requirements())

    result = recommend_equipment("project", "pump")

    assert [candidate.model_name for candidate in result.candidates] == ["REAL"]


def test_configurable_series_is_display_only_not_a_selection_candidate(
    monkeypatch: pytest.MonkeyPatch,
):
    rows = [
        {
            "type": "组合式空调机组",
            "name": "SERIES",
            "role": "terminal",
            "configurable_series": True,
        },
        {
            "type": "风机盘管",
            "name": "FIXED-MODEL",
            "role": "terminal",
            "capacity_kw": 100.0,
            "water_flow_m3_h": 10.0,
            "airflow_m3_h": 10_000.0,
            "fan_power_kw": 0.4,
        },
    ]
    _stub_project_selection(monkeypatch, rows, _requirements(airflow=10_000.0))

    result = recommend_equipment("project", "terminal")

    assert [candidate.model_name for candidate in result.candidates] == [
        "FIXED-MODEL"
    ]


def test_incomplete_real_model_is_returned_as_ineligible(
    monkeypatch: pytest.MonkeyPatch,
):
    _stub_project_selection(
        monkeypatch,
        [
            {
                "type": "循环泵",
                "name": "INCOMPLETE",
                "role": "pump",
                "head_m": 22.0,
            }
        ],
        _requirements(),
    )

    result = recommend_equipment("project", "pump")
    candidate = result.candidates[0]

    assert result.status == "no_eligible_candidates"
    assert candidate.quantity == 1
    assert candidate.rated.flow_m3_h is None
    assert candidate.provided.flow_m3_h is None
    assert [reason.code for reason in candidate.reasons] == [
        "MISSING_RATED_PARAMETER"
    ]


def test_deterministic_ties_use_device_type_then_model_name(
    monkeypatch: pytest.MonkeyPatch,
):
    rows = [
        {
            "type": "B型",
            "name": "MODEL-B",
            "role": "cold_source",
            "capacity_kw": 100.0,
            "chilled_flow_m3_h": 10.0,
        },
        {
            "type": "A型",
            "name": "MODEL-Z",
            "role": "cold_source",
            "capacity_kw": 100.0,
            "chilled_flow_m3_h": 10.0,
        },
        {
            "type": "A型",
            "name": "MODEL-A",
            "role": "cold_source",
            "capacity_kw": 100.0,
            "chilled_flow_m3_h": 10.0,
        },
    ]
    _stub_project_selection(monkeypatch, rows, _requirements())

    first = recommend_equipment("project", "cold_source")
    second = recommend_equipment("project", "cold_source")

    assert first == second
    assert [candidate.model_name for candidate in first.candidates] == [
        "MODEL-A",
        "MODEL-Z",
        "MODEL-B",
    ]
    assert [candidate.rank for candidate in first.candidates] == [1, 2, 3]


def _terminal_row(name: str, capacity: float, water: float, airflow: float) -> dict:
    return {
        "type": "组合式空调机组", "name": name, "role": "terminal",
        "capacity_kw": capacity, "water_flow_m3_h": water, "airflow_m3_h": airflow,
        "fan_power_kw": 1.0,
    }


def test_equal_compliance_prefers_fewer_units_over_a_closer_margin(
    monkeypatch: pytest.MonkeyPatch,
):
    """越界条数相同时，台数少者优先，即使裕量距离稍远

    组合式机组按风量定台数，容量裕量全部越界（都带 CAPACITY_MARGIN_HIGH）。
    旧排序只比裕量距离，小机组凑台数更贴近需求风量，于是总选出一堆小机组：
    171 kW 的商场选了 13 台 `AHU-07-10`。

        需求 100 kW / 10 m³/h / 10000 m³/h，容量裕量区间 [1.0, 1.35]
        SMALL × 3：容量 3×60 = 180 → 1.80，风量 3×3500 = 10500 → 1.05
        BIG   × 1：容量 190 → 1.90，风量 11000 → 1.10
    两者都带 1 条 CAPACITY_MARGIN_HIGH；SMALL 距离更近，旧排序选 SMALL × 3。
    """
    _stub_project_selection(
        monkeypatch,
        [
            _terminal_row("SMALL", 60.0, 4.0, 3500.0),
            _terminal_row("BIG", 190.0, 12.0, 11000.0),
        ],
        _requirements(cooling=100.0, flow=10.0, airflow=10_000.0),
    )

    result = recommend_equipment("project", "terminal")
    small = next(c for c in result.candidates if c.model_name == "SMALL")
    big = next(c for c in result.candidates if c.model_name == "BIG")

    assert (small.quantity, big.quantity) == (3, 1)
    assert [r.code for r in small.reasons] == [r.code for r in big.reasons] == [
        "CAPACITY_MARGIN_HIGH"
    ]
    assert small.margins.capacity < big.margins.capacity  # SMALL 更贴近需求
    assert [c.model_name for c in result.candidates] == ["BIG", "SMALL"]


def test_compliance_still_outranks_unit_count(monkeypatch: pytest.MonkeyPatch):
    """台数不能压过规则符合度：落在裕量区间里的多台方案，排在越界的单台方案前面

        需求 100 kW / 10 m³/h / 10000 m³/h
        PAIR × 2：容量 2×60 = 120 → 1.20（区间内），风量 2×5500 → 1.10
        HUGE × 1：容量 200 → 2.00（越界 CAPACITY_MARGIN_HIGH）
    """
    _stub_project_selection(
        monkeypatch,
        [
            _terminal_row("PAIR", 60.0, 6.0, 5500.0),
            _terminal_row("HUGE", 200.0, 20.0, 12000.0),
        ],
        _requirements(cooling=100.0, flow=10.0, airflow=10_000.0),
    )

    result = recommend_equipment("project", "terminal")

    assert [(c.model_name, c.quantity) for c in result.candidates] == [
        ("PAIR", 2), ("HUGE", 1)
    ]
    assert result.candidates[0].reasons == []
    assert [r.code for r in result.candidates[1].reasons] == ["CAPACITY_MARGIN_HIGH"]


@pytest.mark.parametrize(
    "role", ["cold_source", "heat_source", "pump", "terminal"]
)
def test_api_accepts_all_supported_roles(
    client: TestClient, role: str,
):
    project_id = _save_project(client)

    response = _recommend(client, project_id, role)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["target_role"] == role
    assert body["requirements"]["cooling_load_kw"] > 0
    assert body["candidates"]
    SelectionRecommendationResult.model_validate(body)


@pytest.mark.parametrize("role", ["cold_source", "pump"])
def test_complete_sample_recommends_eligible_core_equipment(
    client: TestClient, role: str,
):
    project_id = _save_project(client)

    response = _recommend(client, project_id, role)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "recommended"
    assert any(candidate["eligible"] for candidate in body["candidates"])


def test_api_rejects_unsupported_role_and_extra_fields(client: TestClient):
    project_id = _save_project(client)

    unsupported = _recommend(client, project_id, "cooling_tower")
    extra = client.post(
        f"/api/v1/selection/recommend/{project_id}",
        json={"target_role": "pump", "required_flow_m3_h": 999},
    )

    assert unsupported.status_code == 422
    assert unsupported.json()["detail"][0]["type"] == "literal_error"
    assert extra.status_code == 422
    assert extra.json()["detail"][0]["type"] == "extra_forbidden"


def test_api_returns_unknown_project_and_structured_requirement_errors(
    client: TestClient,
):
    unknown = _recommend(client, "does-not-exist", "cold_source")
    no_rooms_id = _save_project(
        client,
        {"rooms": [], "devices": [], "pipes": [], "connections": []},
    )
    no_rooms = _recommend(client, no_rooms_id, "cold_source")
    load_only_id = _save_project(client, _load_only_project())
    no_head = _recommend(client, load_only_id, "pump")

    assert unknown.status_code == 404
    assert unknown.json() == {"detail": "Project not found."}
    assert no_rooms.status_code == 422
    assert no_rooms.json()["detail"] == {
        "code": "SELECTION_REQUIREMENTS_UNAVAILABLE",
        "target_role": "cold_source",
        "missing": ["rooms"],
        "message": "Required project data is unavailable for equipment selection.",
    }
    assert no_head.status_code == 422
    assert no_head.json()["detail"]["target_role"] == "pump"
    assert no_head.json()["detail"]["missing"] == ["required_pump_head_m"]


def test_api_distinguishes_all_three_success_statuses(
    client: TestClient, monkeypatch: pytest.MonkeyPatch,
):
    project_id = _save_project(client)
    requirements = equipment_selection._derive_requirements(
        project_store.get_project(project_id)
    )
    monkeypatch.setattr(
        equipment_selection,
        "load_equipment_catalog",
        lambda: [
            {
                "type": "冷源机组",
                "name": "EXACT",
                "role": "cold_source",
                "capacity_kw": requirements.cooling_load_kw,
                "chilled_flow_m3_h": requirements.chilled_water_flow_m3_h,
            }
        ],
    )
    recommended = _recommend(client, project_id, "cold_source")

    monkeypatch.setattr(
        equipment_selection,
        "load_equipment_catalog",
        lambda: [
            {
                "type": "循环泵",
                "name": "NO-HEAD",
                "role": "pump",
                "flow_m3_h": 1000.0,
            }
        ],
    )
    no_eligible = _recommend(client, project_id, "pump")
    no_catalog = _recommend(client, project_id, "terminal")

    assert recommended.status_code == 200
    assert recommended.json()["status"] == "recommended"
    assert any(item["eligible"] for item in recommended.json()["candidates"])
    assert no_eligible.status_code == 200
    assert no_eligible.json()["status"] == "no_eligible_candidates"
    assert no_eligible.json()["candidates"]
    assert no_catalog.status_code == 200
    assert no_catalog.json()["status"] == "no_catalog_candidates"
    assert no_catalog.json()["candidates"] == []


def test_api_returns_every_fixed_model_and_excludes_non_selectable_rows(
    client: TestClient,
):
    project_id = _save_project(client)

    for role in ("cold_source", "heat_source", "pump", "terminal"):
        body = _recommend(client, project_id, role).json()
        expected = [
            row
            for row in load_equipment_catalog()
            if row.get("role") == role
            and row.get("name") != "*"
            and not row.get("configurable_series")
            and not row.get("placement_only")
        ]
        assert len(body["candidates"]) == len(expected)
        assert all(candidate["model_name"] != "*" for candidate in body["candidates"])


def test_repeated_api_requests_are_equal_and_do_not_mutate_persistent_or_runtime_state(
    client: TestClient,
):
    project_id = _save_project(client)
    controls_response = client.get(f"/api/v1/realtime/controls/{project_id}")
    assert controls_response.status_code == 200
    device_id, device = next(iter(controls_response.json()["devices"].items()))
    control_name, control = next(iter(device["controls"].items()))
    set_response = client.post(
        f"/api/v1/realtime/control/{project_id}",
        json={
            "device_instance_id": device_id,
            "values": {control_name: control["current"]},
        },
    )
    assert set_response.status_code == 200

    project_path = project_store.PROJECT_DIR / f"{project_id}.json"
    history_path = realtime.HISTORY_DIR / f"{project_id}.json"
    catalog_before = CATALOG_PATH.read_bytes()
    project_before = project_path.read_bytes()
    history_before = history_path.read_bytes() if history_path.exists() else None
    session_before = realtime._sessions[project_id]
    snapshot_before = session_before.snapshot
    controls_before = deepcopy(realtime._control_values)
    layouts_before = dict(realtime._layouts)
    configs_before = deepcopy(realtime._sim_configs)

    first = _recommend(client, project_id, "cold_source")
    second = _recommend(client, project_id, "cold_source")

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json() == second.json()
    assert CATALOG_PATH.read_bytes() == catalog_before
    assert project_path.read_bytes() == project_before
    assert (history_path.read_bytes() if history_path.exists() else None) == history_before
    assert project_store.get_project(project_id).history == []
    assert realtime._sessions[project_id] is session_before
    assert realtime._sessions[project_id].snapshot is snapshot_before
    assert realtime._control_values == controls_before
    assert realtime._layouts == layouts_before
    assert realtime._sim_configs == configs_before
