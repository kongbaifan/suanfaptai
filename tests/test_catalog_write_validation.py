"""Every catalogue write rejects invalid data before publishing file or cache."""

import base64
import csv
import json
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from io import BytesIO, StringIO
from pathlib import Path
from threading import Event

import pytest
from fastapi.testclient import TestClient
from httpx import Response
from openpyxl import Workbook

from app.data import equipment
from app.main import app

pytestmark = pytest.mark.usefixtures("isolated_equipment_catalog")
client = TestClient(app, raise_server_exceptions=False)


def _item(name: str = "VALIDATION-PROBE") -> dict:
    return {"type": "测试校验", "name": name, "role": "terminal", "systems": ["chilled_water"], "capacity_kw": 9}


def _write(mode: str, item: dict) -> Response:
    if mode == "item":
        return client.post("/api/v1/catalog/equipment/item", json=item)
    if mode == "replace":
        return client.put("/api/v1/catalog/equipment", json=[item])
    if mode == "json":
        raw = json.dumps([item]).encode()
    else:
        row = {**item, "systems": ";".join(item["systems"])}
        if mode == "csv":
            buffer = StringIO()
            writer = csv.DictWriter(buffer, fieldnames=list(row))
            writer.writeheader()
            writer.writerow(row)
            raw = buffer.getvalue().encode()
        else:
            workbook = Workbook()
            sheet = workbook.active
            assert sheet is not None
            sheet.append(list(row))
            sheet.append(list(row.values()))
            binary = BytesIO()
            workbook.save(binary)
            raw = binary.getvalue()
    return client.post("/api/v1/catalog/equipment/import", json={
        "filename": f"probe.{mode}", "content_base64": base64.b64encode(raw).decode(),
    })


@pytest.mark.parametrize("mode", ["item", "replace", "csv", "json", "xlsx"])
@pytest.mark.parametrize("value", ["NaN", "Infinity", "-Infinity"])
def test_non_finite_values_do_not_change_catalogue(
    mode: str, value: str, isolated_equipment_catalog: Path,
) -> None:
    before = isolated_equipment_catalog.read_bytes()
    cached = deepcopy(equipment.load_equipment_catalog())
    item = {**_item(), "capacity_kw": value}
    response = _write(mode, item)
    if mode in {"item", "replace"}:
        assert response.status_code == 422
        assert response.json()["detail"]["code"] == "CATALOG_ITEM_INVALID"
        assert "capacity_kw" in response.json()["detail"]["message"]
    else:
        assert response.status_code == 200
        assert response.json()["imported"] == 0
        assert any(issue["code"] == "CATALOG_ROW_INVALID" for issue in response.json()["issues"])
    assert isolated_equipment_catalog.read_bytes() == before
    assert equipment.load_equipment_catalog() == cached
    assert client.get("/api/v1/catalog/equipment").status_code == 200


@pytest.mark.parametrize("field,value", [
    ("capacity_kw", -1), ("power_kw", True), ("flow_m3_h", "bad"),
    ("efficiency", 1.01), ("motor_efficiency", -0.1), ("systems", None), ("type", None),
])
def test_invalid_values_return_422_without_saving(
    field: str, value: object, isolated_equipment_catalog: Path,
) -> None:
    before = isolated_equipment_catalog.read_bytes()
    response = client.post("/api/v1/catalog/equipment/item", json={**_item(), field: value})
    assert response.status_code == 422
    assert isolated_equipment_catalog.read_bytes() == before


def test_negative_operating_temperature_is_valid() -> None:
    item = {**_item(), "heating_outdoor_dry_bulb_temp_c": -15, "efficiency": 0.8}
    response = client.post("/api/v1/catalog/equipment/item", json=item)
    assert response.status_code == 200
    assert response.json()["heating_outdoor_dry_bulb_temp_c"] == -15


def test_non_finite_extra_metadata_is_rejected(isolated_equipment_catalog: Path) -> None:
    before = isolated_equipment_catalog.read_bytes()
    response = client.post(
        "/api/v1/catalog/equipment/item",
        content='{"type":"probe","name":"meta","extra":{"number":1e999}}',
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 422
    assert isolated_equipment_catalog.read_bytes() == before


@pytest.mark.parametrize("edit_existing", [False, True])
def test_failed_write_keeps_the_cached_catalogue_and_disk(
    edit_existing: bool, isolated_equipment_catalog: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    before = isolated_equipment_catalog.read_bytes()
    cached_reference = equipment.load_equipment_catalog()
    before_cache = deepcopy(cached_reference)
    item = {**deepcopy(cached_reference[0]), "power_kw": 123} if edit_existing else _item()

    def fail_write(path: Path, value: object) -> None:
        raise OSError("simulated storage failure")

    monkeypatch.setattr(equipment, "write_json_atomic", fail_write)
    with pytest.raises(OSError, match="storage failure"):
        equipment.upsert_equipment_item(item)
    assert cached_reference == before_cache
    assert equipment.load_equipment_catalog() == before_cache
    assert isolated_equipment_catalog.read_bytes() == before
    assert client.get("/api/v1/catalog/equipment").status_code == 200


def test_invalid_import_batch_saves_none_of_the_valid_rows(isolated_equipment_catalog: Path) -> None:
    before = isolated_equipment_catalog.read_bytes()
    rows = [_item("valid"), {**_item("invalid"), "capacity_kw": "NaN"}]
    response = client.post("/api/v1/catalog/equipment/import", json={
        "filename": "batch.json",
        "content_base64": base64.b64encode(json.dumps(rows).encode()).decode(),
    })
    assert response.status_code == 200
    assert response.json()["imported"] == 0
    assert isolated_equipment_catalog.read_bytes() == before


def test_json_import_rejects_a_non_object_row(isolated_equipment_catalog: Path) -> None:
    before = isolated_equipment_catalog.read_bytes()
    response = client.post("/api/v1/catalog/equipment/import", json={
        "filename": "batch.json",
        "content_base64": base64.b64encode(json.dumps([_item("valid"), 42]).encode()).decode(),
    })
    assert response.status_code == 200
    assert response.json()["imported"] == 0
    assert response.json()["issues"][0]["code"] == "CATALOG_ROW_INVALID"
    assert isolated_equipment_catalog.read_bytes() == before


def test_concurrent_catalogue_edits_keep_both_models(monkeypatch: pytest.MonkeyPatch) -> None:
    ready, release, second_started, second_done = Event(), Event(), Event(), Event()
    original_write = equipment.write_json_atomic

    def paused_write(path: Path, value: object) -> None:
        if not ready.is_set():
            ready.set()
            assert release.wait(5)
        original_write(path, value)

    def second_edit() -> None:
        second_started.set()
        equipment.upsert_equipment_item(_item("second"))
        second_done.set()

    monkeypatch.setattr(equipment, "write_json_atomic", paused_write)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(equipment.upsert_equipment_item, _item("first"))
        assert ready.wait(5)
        second = pool.submit(second_edit)
        assert second_started.wait(5)
        try:
            assert not second_done.wait(0.2)
        finally:
            release.set()
        first.result(timeout=5)
        second.result(timeout=5)
    names = {row["name"] for row in equipment.load_equipment_catalog()}
    assert {"first", "second"} <= names
