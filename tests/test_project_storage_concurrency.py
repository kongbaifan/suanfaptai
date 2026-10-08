"""Project edits, evaluation history and deletion form one storage transaction."""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event

import pytest
from fastapi.testclient import TestClient

from app import main as main_module
from app.data import json_store, project_store
from app.main import app
from app.schemas import ProjectSaveRequest, SystemEvaluateRequest, SystemEvaluateResult


@pytest.mark.parametrize("operation", ["edit", "append", "delete"])
def test_concurrent_history_preserves_edits_entries_and_deletion(
    operation: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    with TestClient(app) as client:
        saved = client.post("/api/v1/projects", json={"name": "original", "project": {}})
        project_id = saved.json()["project_id"]
        history_ready, release_history = Event(), Event()
        second_started, second_finished = Event(), Event()
        original_write = project_store._write_record

        def paused_history_write(record):
            if record.history and not history_ready.is_set():
                history_ready.set()
                assert release_history.wait(5), "history writer was not released"
            original_write(record)

        def other_request():
            second_started.set()
            if operation == "edit":
                response = client.put(
                    f"/api/v1/projects/{project_id}",
                    json={"name": "edited", "description": "kept", "project": {}},
                )
            elif operation == "append":
                response = client.post(f"/api/v1/projects/{project_id}/evaluate")
            else:
                response = client.delete(f"/api/v1/projects/{project_id}")
            second_finished.set()
            return response

        monkeypatch.setattr(project_store, "_write_record", paused_history_write)
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(client.post, f"/api/v1/projects/{project_id}/evaluate")
            assert history_ready.wait(5)
            second = pool.submit(other_request)
            assert second_started.wait(5)
            try:
                assert not second_finished.wait(0.2), "another mutation passed an unfinished history transaction"
            finally:
                release_history.set()
            assert first.result(timeout=5).status_code == 200
            assert second.result(timeout=5).status_code == 200

        current = client.get(f"/api/v1/projects/{project_id}")
        if operation == "delete":
            assert current.status_code == 404
            return
        assert current.status_code == 200
        record = current.json()
        if operation == "edit":
            assert record["name"] == "edited"
            assert record["description"] == "kept"
            assert record["history_count"] == 1
        else:
            assert record["history_count"] == 2
            assert len({entry["history_id"] for entry in record["history"]}) == 2


def test_failed_atomic_replace_keeps_the_previous_project(monkeypatch: pytest.MonkeyPatch) -> None:
    saved = project_store.save_project(ProjectSaveRequest(name="original", project=SystemEvaluateRequest()))
    path = project_store.PROJECT_DIR / f"{saved.project_id}.json"
    before = path.read_bytes()

    def failed_replace(source: Path, target: Path) -> None:
        raise OSError("simulated storage failure")

    monkeypatch.setattr(json_store.os, "replace", failed_replace)
    with pytest.raises(OSError, match="storage failure"):
        project_store.save_project(
            ProjectSaveRequest(name="edited", project=SystemEvaluateRequest()), saved.project_id,
        )
    assert path.read_bytes() == before
    assert project_store.get_project(saved.project_id).name == "original"
    assert list(path.parent.glob(".*.tmp")) == []


def test_failed_serialization_keeps_the_previous_project() -> None:
    saved = project_store.save_project(ProjectSaveRequest(name="original", project=SystemEvaluateRequest()))
    path = project_store.PROJECT_DIR / f"{saved.project_id}.json"
    before = path.read_bytes()
    with pytest.raises(ValueError, match="Out of range float"):
        project_store.save_project(
            ProjectSaveRequest(
                name="invalid",
                project=SystemEvaluateRequest(design_options={"invalid": float("nan")}),
            ),
            saved.project_id,
        )
    assert path.read_bytes() == before


def test_evaluation_deleted_during_calculation_returns_404(monkeypatch: pytest.MonkeyPatch) -> None:
    with TestClient(app) as client:
        created = client.post("/api/v1/projects", json={"name": "temporary", "project": {}})
        project_id = created.json()["project_id"]

        def calculate_then_delete(request: SystemEvaluateRequest) -> SystemEvaluateResult:
            assert project_store.delete_project(project_id)
            return SystemEvaluateResult(load=None, water=None, air=None, issues=[], recommendations=[])

        monkeypatch.setattr(main_module, "_evaluate", calculate_then_delete)
        response = client.post(f"/api/v1/projects/{project_id}/evaluate")
        assert response.status_code == 404
        assert client.get(f"/api/v1/projects/{project_id}").status_code == 404
