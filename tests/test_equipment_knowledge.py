from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

import app.main as main_module
from app import realtime
from app.data import project_store
from app.data.equipment import CATALOG_PATH, load_equipment_catalog
from app.equipment_knowledge import (
    KNOWLEDGE_DEVICE_TYPES,
    KNOWLEDGE_ORDER,
    _validate_records,
    load_equipment_knowledge,
)
from app.main import app
from app.release import load_release_manifest
from app.schemas import EquipmentKnowledgeDetail

client = TestClient(app)


def test_typed_loader_has_exact_order_complete_content_and_copy_isolation():
    records = load_equipment_knowledge()

    assert tuple(record.knowledge_id for record in records) == KNOWLEDGE_ORDER
    assert tuple(record.device_type for record in records) == tuple(
        KNOWLEDGE_DEVICE_TYPES.values()
    )
    assert len(records) == 11
    assert all(isinstance(record, EquipmentKnowledgeDetail) for record in records)
    assert all(record.overview and record.principle for record in records)
    assert all(record.components and record.key_parameters for record in records)
    assert all(record.applicable_systems for record in records)

    original_component_count = len(records[0].components)
    records[0].components.pop()
    assert len(load_equipment_knowledge()[0].components) == original_component_count

    invalid = load_equipment_knowledge()[0].model_dump()
    invalid["catalog_linked"] = 1
    with pytest.raises(ValidationError):
        EquipmentKnowledgeDetail.model_validate(invalid)
    with pytest.raises(ValidationError):
        records[1].name = "不可修改"


def test_loader_validation_rejects_device_type_drift():
    records = load_equipment_knowledge()
    index = next(
        index
        for index, record in enumerate(records)
        if record.knowledge_id == "air_source_heat_pump"
    )
    records[index] = records[index].model_copy(update={"device_type": "压缩机"})

    with pytest.raises(ValueError, match="Device type"):
        _validate_records(records)


def test_catalog_links_and_parameter_fields_match_authoritative_catalog():
    records = load_equipment_knowledge()
    catalog = load_equipment_catalog()

    linked = [record for record in records if record.catalog_linked]
    pure_knowledge = [record for record in records if not record.catalog_linked]
    assert len(linked) == 9
    assert len(pure_knowledge) == 2

    for record in linked:
        assert record.catalog_device_type == record.device_type
        rows = [
            row for row in catalog if row.get("type") == record.catalog_device_type
        ]
        assert rows
        for parameter in record.key_parameters:
            assert parameter.catalog_field is not None
            assert any(parameter.catalog_field in row for row in rows)

    for record in pure_knowledge:
        assert record.catalog_device_type is None
        assert all(
            parameter.catalog_field is None
            for parameter in record.key_parameters
        )


def test_list_api_is_a_deterministic_summary_projection():
    first = client.get("/api/v1/knowledge/equipment")
    second = client.get("/api/v1/knowledge/equipment")

    assert first.status_code == 200
    assert first.json() == second.json()
    assert [item["knowledge_id"] for item in first.json()] == list(KNOWLEDGE_ORDER)
    assert len(first.json()) == 11
    assert all("principle" not in item for item in first.json())
    assert all("components" not in item for item in first.json())
    assert all("key_parameters" not in item for item in first.json())


def test_detail_api_returns_complete_record_and_exact_unknown_id_404():
    response = client.get("/api/v1/knowledge/equipment/circulation_pump")

    assert response.status_code == 200
    detail = response.json()
    assert detail["device_type"] == "循环泵"
    assert detail["principle"]
    assert detail["components"]
    assert detail["key_parameters"]
    assert detail["applicable_systems"]

    unknown = client.get("/api/v1/knowledge/equipment/CIRCULATION_PUMP")
    assert unknown.status_code == 404
    assert unknown.json() == {"detail": "Equipment knowledge not found."}


def test_openapi_uses_explicit_summary_and_detail_response_models():
    paths = app.openapi()["paths"]
    list_schema = paths["/api/v1/knowledge/equipment"]["get"]["responses"]["200"][
        "content"
    ]["application/json"]["schema"]
    detail_schema = paths["/api/v1/knowledge/equipment/{knowledge_id}"]["get"][
        "responses"
    ]["200"]["content"]["application/json"]["schema"]

    assert list_schema == {
        "items": {
            "$ref": "#/components/schemas/EquipmentKnowledgeSummary",
        },
        "type": "array",
        "title": "Response Equipment Knowledge List Api V1 Knowledge Equipment Get",
    }
    assert detail_schema["$ref"] == (
        "#/components/schemas/EquipmentKnowledgeDetail"
    )
    assert paths["/api/v1/knowledge/equipment/{knowledge_id}"]["get"][
        "responses"
    ]["404"]["description"] == "Equipment knowledge not found."


def test_knowledge_requests_do_not_touch_catalog_project_or_realtime_state(
    monkeypatch,
    tmp_path,
):
    # 项目与历史目录必须指向 tmp_path：真实目录会被本地在跑的 uvicorn 持续写入，
    # 两次快照之间被外部进程改动会让断言随机失败。
    monkeypatch.setattr(project_store, "PROJECT_DIR", tmp_path / "projects")
    monkeypatch.setattr(realtime, "HISTORY_DIR", tmp_path / "realtime_history")
    catalog_before = CATALOG_PATH.read_bytes()
    projects_before = _tree_snapshot(project_store.PROJECT_DIR)
    history_before = _tree_snapshot(realtime.HISTORY_DIR)
    stores = _runtime_stores()
    state_before = [dict(store) for store in stores]

    def forbidden(*_args, **_kwargs):
        raise AssertionError("knowledge request crossed a mutable state boundary")

    for name in (
        "load_equipment_catalog",
        "get_project",
        "add_project_history",
        "get_realtime_state",
        "get_runtime_controls",
        "subscribe",
    ):
        monkeypatch.setattr(main_module, name, forbidden)

    assert client.get("/api/v1/knowledge/equipment").status_code == 200
    assert client.get("/api/v1/knowledge/equipment/compressor").status_code == 200

    assert CATALOG_PATH.read_bytes() == catalog_before
    assert _tree_snapshot(project_store.PROJECT_DIR) == projects_before
    assert _tree_snapshot(realtime.HISTORY_DIR) == history_before
    assert [dict(store) for store in stores] == state_before


def test_release_resources_register_the_knowledge_asset_once():
    manifest = load_release_manifest()
    resources = manifest["resources"]
    assert resources.count("app/data/equipment_knowledge.json") == 1


def _tree_snapshot(root: Path) -> dict[str, bytes]:
    if not root.exists():
        return {}
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
    }


def _runtime_stores() -> tuple[dict, ...]:
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
