from app.room_bindings import resolve_room_bindings
from app.schemas import Issue, ProjectValidationResult, SystemEvaluateRequest
from app.validators.fresh_air import validate_fresh_air_and_terminals
from app.validators.system import validate_system
from app.validators.system_form import superseded_issue_codes


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
    # 声明制式后，新风覆盖问题由制式规则以 error 级接管（它知道本制式的新风
    # 该由哪一族设备承担），通用的房间级 warning 会变成同一件事的第二次播报。
    superseded = superseded_issue_codes(request.system_form)
    issues.extend(
        issue
        for issue in validate_fresh_air_and_terminals(request)
        if issue.code not in superseded
    )
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
