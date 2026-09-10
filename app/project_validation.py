from app.schemas import Issue, ProjectValidationResult, SystemEvaluateRequest
from app.room_bindings import resolve_room_bindings
from app.validators.system import validate_system


def validate_project_data(request: SystemEvaluateRequest) -> ProjectValidationResult:
    issues: list[Issue] = []
    ids: set[str] = set()
    duplicates: set[str] = set()

    # 设备与管路是两个不同模型，合并成一个列表会让类型退化成 BaseModel，先各自取 id。
    node_ids = [device.id for device in request.devices] + [pipe.id for pipe in request.pipes]
    for node_id in node_ids:
        if node_id in ids:
            duplicates.add(node_id)
        ids.add(node_id)

    for duplicate_id in sorted(duplicates):
        issues.append(Issue(level="error", code="DUPLICATE_ID", message=f"Duplicate node id: {duplicate_id}.", target=duplicate_id))

    if not request.rooms:
        issues.append(Issue(level="warning", code="NO_ROOMS", message="Project data has no rooms."))

    issues.extend(resolve_room_bindings(request).issues)
    issues.extend(validate_system(request, required_cooling_kw=0.0))
    valid = not any(issue.level == "error" for issue in issues)
    return ProjectValidationResult(valid=valid, normalized=request, issues=_dedupe(issues))


def _dedupe(issues: list[Issue]) -> list[Issue]:
    seen = set()
    result = []
    for issue in issues:
        key = (issue.level, issue.code, issue.message, issue.target)
        if key not in seen:
            seen.add(key)
            result.append(issue)
    return result
