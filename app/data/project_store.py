import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from app.room_bindings import raise_for_room_binding_errors, resolve_room_bindings
from app.schemas import (
    ProjectHistoryEntry,
    ProjectRecord,
    ProjectSaveRequest,
    ProjectSummary,
    SystemEvaluateResult,
)

PROJECT_DIR = Path(__file__).with_name("projects")


def list_projects() -> list[ProjectSummary]:
    PROJECT_DIR.mkdir(parents=True, exist_ok=True)
    summaries = []
    for path in sorted(PROJECT_DIR.glob("*.json")):
        record = _read_record(path)
        summaries.append(_summary_from_record(record))
    return sorted(summaries, key=lambda item: item.updated_at, reverse=True)


def save_project(request: ProjectSaveRequest, project_id: str | None = None) -> ProjectRecord:
    raise_for_room_binding_errors(resolve_room_bindings(request.project))
    PROJECT_DIR.mkdir(parents=True, exist_ok=True)
    now = _now()
    if project_id:
        path = _path_for_project(project_id)
        existing = _read_record(path) if path.exists() else None
        created_at = existing.created_at if existing else now
        history = existing.history if existing else []
    else:
        project_id = uuid4().hex[:12]
        created_at = now
        history = []

    record = ProjectRecord(
        project_id=project_id,
        name=request.name,
        description=request.description,
        tags=request.tags,
        created_at=created_at,
        updated_at=now,
        history_count=len(history),
        project=request.project,
        history=history,
    )
    _write_record(record)
    return record


def get_project(project_id: str) -> ProjectRecord:
    path = _path_for_project(project_id)
    if not path.exists():
        raise FileNotFoundError(project_id)
    return _read_record(path)


def delete_project(project_id: str) -> bool:
    path = _path_for_project(project_id)
    if not path.exists():
        return False
    path.unlink()
    return True


def add_project_history(project_id: str, result: SystemEvaluateResult) -> ProjectHistoryEntry:
    record = get_project(project_id)
    entry = ProjectHistoryEntry(
        history_id=uuid4().hex[:12],
        project_id=project_id,
        created_at=_now(),
        issue_count=len(result.issues),
        summary=result.report.summary if result.report else {},
        result=result.model_dump(),
    )
    record.history.insert(0, entry.model_dump())
    record.history = record.history[:50]
    record.updated_at = entry.created_at
    record.history_count = len(record.history)
    _write_record(record)
    return entry


def list_project_history(project_id: str) -> list[ProjectHistoryEntry]:
    record = get_project(project_id)
    return [ProjectHistoryEntry(**entry) for entry in record.history]


def _path_for_project(project_id: str) -> Path:
    safe_id = "".join(char for char in project_id if char.isalnum() or char in {"-", "_"})
    return PROJECT_DIR / f"{safe_id}.json"


def _read_record(path: Path) -> ProjectRecord:
    with path.open("r", encoding="utf-8") as file:
        data = json.load(file)
    # 存量文件里还留着 last_score 与历史 score，schema 已不含这些字段，
    # pydantic 默认忽略多余键，读取时自然丢弃，不再回写。
    data["history_count"] = len(data.get("history", []))
    return ProjectRecord(**data)


def _write_record(record: ProjectRecord) -> None:
    PROJECT_DIR.mkdir(parents=True, exist_ok=True)
    with _path_for_project(record.project_id).open("w", encoding="utf-8") as file:
        json.dump(record.model_dump(), file, ensure_ascii=False, indent=2)
        file.write("\n")


def _summary_from_record(record: ProjectRecord) -> ProjectSummary:
    return ProjectSummary(
        project_id=record.project_id,
        name=record.name,
        description=record.description,
        tags=record.tags,
        created_at=record.created_at,
        updated_at=record.updated_at,
        history_count=len(record.history),
    )


def _now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
