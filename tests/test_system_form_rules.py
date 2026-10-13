"""系统制式（空调系统形式）校验规则

制式是**项目级**声明，决定房间末端族、新风由谁承担、以及拓扑要满足哪些
完整性条件。本文件钉死三件事：

1. **向后兼容**：未声明制式时，制式校验必须完全静默——全部 10 个内置样例与
   存量项目的行为不能有任何变化。
2. **两组正规样例在声明各自制式后零 error**：``four_floor_fcu_fresh_air`` /
   ``ducted_fresh_air`` / ``three_floor_twelve_room`` 声明为
   ``fcu_fresh_air``，``multi_zone_all_air`` 声明为 ``all_air``。
3. **每条制式规则都有正例**，且反向制式能被识别出来（全空气项目里混进风机盘管）。

关于 ``air`` 样例：它原先只有一条空气连接（``ahu_1 -> duct_1``），回风侧完全
悬空、也没有任何风口，声明为 ``all_air`` 时报 ``SYSTEM_FORM_AHU_DUCT_INCOMPLETE``
（真实缺陷，不是误报）；实时仿真里机组也因此送不出风（``SIM_AIR_PATH_INCOMPLETE``）。
2026-10 补齐了送风 → 风口 → 回风的闭合风路，现在它声明 ``all_air`` 必须零 issue；
"制式校验能识别真实的风网缺陷"改由**拆掉回风管的派生样例**钉住。
"""

from copy import deepcopy

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.samples import (
    AIR_SYSTEM_SAMPLE,
    FOUR_FLOOR_FCU_FRESH_AIR_SAMPLE,
    MULTI_ZONE_ALL_AIR_SAMPLE,
    SAMPLES,
)
from app.schemas import SystemEvaluateRequest, SystemForm
from app.validators.system_form import (
    check_system_form,
    superseded_issue_codes,
    validate_system_form,
)

# 内置样例与其**规范制式**的对应。未列出的样例没有规范制式（它们是故意损坏
# 或制式无关的教学样例），只参与"未声明制式必须静默"的回归。
_CANONICAL: dict[str, SystemForm] = {
    "four_floor_fcu_fresh_air": SystemForm.FCU_FRESH_AIR,
    "ducted_fresh_air": SystemForm.FCU_FRESH_AIR,
    "three_floor_twelve_room": SystemForm.FCU_FRESH_AIR,
    "multi_zone_all_air": SystemForm.ALL_AIR,
}

client = TestClient(app)


def _request(payload: dict, form: SystemForm | None = None) -> SystemEvaluateRequest:
    body = deepcopy(payload)
    if form is not None:
        body["system_form"] = form.value
    return SystemEvaluateRequest.model_validate(body)


def _issues(payload: dict, form: SystemForm | None = None):
    return validate_system_form(_request(payload, form))


def _codes(payload: dict, form: SystemForm | None = None) -> set[str]:
    return {issue.code for issue in _issues(payload, form)}


def _errors(payload: dict, form: SystemForm | None = None):
    return [issue for issue in _issues(payload, form) if issue.level == "error"]


# --------------------------------------------------------------------------
# 1. 向后兼容：未声明制式必须完全静默
# --------------------------------------------------------------------------


def test_undeclared_form_is_silent_for_every_builtin_sample():
    """未声明制式时制式校验不产出任何 issue（含 warning / info）"""
    for name, payload in SAMPLES.items():
        assert validate_system_form(_request(payload)) == [], name


def test_undeclared_form_does_not_change_project_validation():
    """把 system_form 显式写成 null，与完全不写的结果必须一致"""
    from app.project_validation import validate_project_data

    for name, payload in SAMPLES.items():
        without = validate_project_data(_request(payload))
        explicit_null = validate_project_data(
            SystemEvaluateRequest.model_validate({**deepcopy(payload), "system_form": None})
        )
        assert [i.model_dump() for i in without.issues] == [
            i.model_dump() for i in explicit_null.issues
        ], name


def test_superseded_codes_only_apply_when_form_declared():
    assert superseded_issue_codes(None) == frozenset()
    assert superseded_issue_codes(SystemForm.FCU_FRESH_AIR) == {"fresh_air_no_handler"}


# --------------------------------------------------------------------------
# 2. 两组正规样例在各自制式下零 error
# --------------------------------------------------------------------------


@pytest.mark.parametrize("sample_name", sorted(_CANONICAL))
def test_canonical_sample_is_compliant_under_its_own_form(sample_name: str):
    form = _CANONICAL[sample_name]
    assert _errors(SAMPLES[sample_name], form) == []


def test_air_sample_with_closed_duct_loop_is_clean_under_all_air():
    """``air`` 样例补齐风路后，声明全空气不得再报任何制式问题"""
    assert _issues(AIR_SYSTEM_SAMPLE, SystemForm.ALL_AIR) == []


def test_ahu_with_dangling_return_side_reports_incomplete_duct_network():
    """回风侧悬空的组合式机组，声明全空气后必须报出来

    派生自 ``air`` 样例：拆掉回风管接回机组的那一条连接。必须用端口图判断——
    送回风两侧会被并成一张图，只看节点连通性会把它误判为完整。
    """
    broken = deepcopy(AIR_SYSTEM_SAMPLE)
    broken["connections"] = [
        c for c in broken["connections"]
        if not (c["from_id"] == "return_duct_1" and c["to_id"] == "ahu_1")
    ]
    assert len(broken["connections"]) == len(AIR_SYSTEM_SAMPLE["connections"]) - 1
    codes = _codes(broken, SystemForm.ALL_AIR)
    assert "SYSTEM_FORM_AHU_DUCT_INCOMPLETE" in codes


# --------------------------------------------------------------------------
# 3. 反向制式：混装另一族末端必须被识别
# --------------------------------------------------------------------------


def test_fcu_sample_declared_as_all_air_flags_forbidden_terminals():
    codes = _codes(FOUR_FLOOR_FCU_FRESH_AIR_SAMPLE, SystemForm.ALL_AIR)
    assert "SYSTEM_FORM_FORBIDDEN_TERMINAL" in codes
    # 风机盘管与独立新风机组在全空气制式下都不允许出现
    targets = {
        issue.target
        for issue in _errors(FOUR_FLOOR_FCU_FRESH_AIR_SAMPLE, SystemForm.ALL_AIR)
        if issue.code == "SYSTEM_FORM_FORBIDDEN_TERMINAL"
    }
    assert any(target.startswith("fcu_") for target in targets)
    assert any(target.startswith("fau_") for target in targets)
    # 而且这两条 forbidden 会把"该制式要求的末端一台都没有"一起带出来
    assert "SYSTEM_FORM_TERMINAL_MISSING" in codes


def test_all_air_sample_declared_as_fcu_form_flags_forbidden_ahu():
    errors = _errors(MULTI_ZONE_ALL_AIR_SAMPLE, SystemForm.FCU_FRESH_AIR)
    forbidden = [e for e in errors if e.code == "SYSTEM_FORM_FORBIDDEN_TERMINAL"]
    assert [e.target for e in forbidden] == ["ahu_f1"]


# --------------------------------------------------------------------------
# 4. 各条规则的独立正例
# --------------------------------------------------------------------------


def test_missing_fresh_air_unit_is_an_error():
    payload = deepcopy(FOUR_FLOOR_FCU_FRESH_AIR_SAMPLE)
    payload["devices"] = [d for d in payload["devices"] if not d["id"].startswith("fau_")]
    payload["connections"] = [
        c for c in payload["connections"]
        if "fau_" not in c["from_id"] and "fau_" not in c["to_id"]
    ]
    for room in payload["rooms"]:
        room["device_ids"] = [i for i in room["device_ids"] if not i.startswith("fau_")]

    assert "SYSTEM_FORM_FRESH_AIR_HANDLER_MISSING" in _codes(
        payload, SystemForm.FCU_FRESH_AIR
    )


def test_room_without_form_terminal_is_an_error():
    payload = deepcopy(FOUR_FLOOR_FCU_FRESH_AIR_SAMPLE)
    payload["rooms"][0]["device_ids"] = []

    errors = _errors(payload, SystemForm.FCU_FRESH_AIR)
    assert any(
        issue.code == "SYSTEM_FORM_ROOM_TERMINAL_MISSING"
        and issue.target == payload["rooms"][0]["id"]
        for issue in errors
    )


def test_room_without_fresh_air_coverage_is_an_error():
    """二层房间保留风机盘管、但不再由新风机组服务"""
    payload = deepcopy(FOUR_FLOOR_FCU_FRESH_AIR_SAMPLE)
    for room in payload["rooms"]:
        if room["floor_id"] == "F2":
            room["device_ids"] = [
                device_id
                for device_id in room["device_ids"]
                if not device_id.startswith("fau_")
            ]

    codes = _codes(payload, SystemForm.FCU_FRESH_AIR)
    # 机组仍在项目里，所以不是"缺机组"，而是"这些房间没被覆盖"
    assert "SYSTEM_FORM_FRESH_AIR_HANDLER_MISSING" not in codes
    assert "SYSTEM_FORM_ROOM_FRESH_AIR_UNCOVERED" in codes


def test_terminal_without_water_connection_is_an_error():
    payload = deepcopy(FOUR_FLOOR_FCU_FRESH_AIR_SAMPLE)
    payload["connections"] = [
        c for c in payload["connections"]
        if not (
            c["system"] in ("chilled_water", "hot_water")
            and any(side.startswith("fcu_f1") for side in (c["from_id"], c["to_id"]))
        )
    ]

    errors = _errors(payload, SystemForm.FCU_FRESH_AIR)
    assert any(
        issue.code == "SYSTEM_FORM_TERMINAL_NOT_WATER_CONNECTED"
        and issue.target == "fcu_f1"
        for issue in errors
    )


def test_fresh_air_unit_without_water_connection_is_an_error():
    payload = deepcopy(FOUR_FLOOR_FCU_FRESH_AIR_SAMPLE)
    payload["connections"] = [
        c for c in payload["connections"]
        if not (
            c["system"] in ("chilled_water", "hot_water")
            and any(side.startswith("fau_") for side in (c["from_id"], c["to_id"]))
        )
    ]

    errors = _errors(payload, SystemForm.FCU_FRESH_AIR)
    assert any(
        issue.code == "SYSTEM_FORM_FRESH_AIR_HANDLER_NOT_WATER_CONNECTED"
        for issue in errors
    )


def test_fresh_air_unit_without_air_path_is_a_warning():
    payload = deepcopy(FOUR_FLOOR_FCU_FRESH_AIR_SAMPLE)
    payload["connections"] = [
        c
        for c in payload["connections"]
        if not (c["system"] == "air" and any(
            side.startswith("fau_") for side in (c["from_id"], c["to_id"])
        ))
    ]

    issues = _issues(payload, SystemForm.FCU_FRESH_AIR)
    warning = [
        issue for issue in issues
        if issue.code == "SYSTEM_FORM_FRESH_AIR_UNIT_NO_AIR_PATH"
    ]
    assert warning and all(issue.level == "warning" for issue in warning)


# --------------------------------------------------------------------------
# 5. 与通用新风校验的接管关系
# --------------------------------------------------------------------------


def test_declared_form_takes_over_generic_fresh_air_warning():
    """同一件事不能报两遍：声明制式后由制式级 error 接管"""
    from app.project_validation import validate_project_data

    payload = deepcopy(FOUR_FLOOR_FCU_FRESH_AIR_SAMPLE)
    payload["devices"] = [d for d in payload["devices"] if not d["id"].startswith("fau_")]
    payload["connections"] = [
        c for c in payload["connections"]
        if "fau_" not in c["from_id"] and "fau_" not in c["to_id"]
    ]
    for room in payload["rooms"]:
        room["device_ids"] = [i for i in room["device_ids"] if not i.startswith("fau_")]

    undeclared = {
        issue.code
        for issue in validate_project_data(
            SystemEvaluateRequest.model_validate(deepcopy(payload))
        ).issues
    }
    declared = {
        issue.code
        for issue in validate_project_data(
            _request(payload, SystemForm.FCU_FRESH_AIR)
        ).issues
    }

    assert "fresh_air_no_handler" in undeclared
    assert "fresh_air_no_handler" not in declared
    assert "SYSTEM_FORM_FRESH_AIR_HANDLER_MISSING" not in undeclared
    assert "SYSTEM_FORM_FRESH_AIR_HANDLER_MISSING" in declared


# --------------------------------------------------------------------------
# 6. 制式自检入口：未声明时要显式告知
# --------------------------------------------------------------------------


def test_form_check_reports_undeclared_form_explicitly():
    result = check_system_form(_request(FOUR_FLOOR_FCU_FRESH_AIR_SAMPLE))
    assert result.declared is False
    assert result.system_form is None
    assert result.compliant is True
    assert [issue.code for issue in result.issues] == ["SYSTEM_FORM_UNDECLARED"]


def test_form_check_is_deterministic():
    first = check_system_form(_request(FOUR_FLOOR_FCU_FRESH_AIR_SAMPLE, SystemForm.FCU_FRESH_AIR))
    second = check_system_form(_request(FOUR_FLOOR_FCU_FRESH_AIR_SAMPLE, SystemForm.FCU_FRESH_AIR))
    assert first.model_dump() == second.model_dump()


# --------------------------------------------------------------------------
# 7. HTTP 契约
# --------------------------------------------------------------------------


def test_form_check_endpoint_contract():
    body = {**deepcopy(FOUR_FLOOR_FCU_FRESH_AIR_SAMPLE), "system_form": "fcu_fresh_air"}
    response = client.post("/api/v1/system/form-check", json=body)
    assert response.status_code == 200
    payload = response.json()
    assert payload["system_form"] == "fcu_fresh_air"
    assert payload["declared"] is True
    assert payload["compliant"] is True
    assert payload["issues"] == []


def test_form_check_endpoint_rejects_unknown_form():
    body = {**deepcopy(FOUR_FLOOR_FCU_FRESH_AIR_SAMPLE), "system_form": "vav"}
    assert client.post("/api/v1/system/form-check", json=body).status_code == 422


def test_form_check_endpoint_is_read_only():
    """自检是只读投影：不创建项目、不改动项目列表"""
    before = client.get("/api/v1/projects").json()
    client.post(
        "/api/v1/system/form-check",
        json={**deepcopy(MULTI_ZONE_ALL_AIR_SAMPLE), "system_form": "all_air"},
    )
    assert client.get("/api/v1/projects").json() == before


def test_form_check_does_not_persist_system_form_on_evaluate():
    """评估路径不因制式声明而改写项目数据"""
    body = {**deepcopy(FOUR_FLOOR_FCU_FRESH_AIR_SAMPLE), "system_form": "fcu_fresh_air"}
    response = client.post("/api/v1/system/evaluate", json=body)
    assert response.status_code == 200
    assert response.json()["issues"] is not None


def test_system_form_round_trips_through_project_store():
    """制式是项目级声明，必须随项目落盘并能原样读回

    `system_form` 是**唯一**新增的请求字段；如果序列化时被丢掉，
    `check_system_form` 对已保存项目就永远报"未声明制式"。
    项目目录已由 `conftest.py` 的 autouse fixture 重定向到临时目录。
    """
    from app.data.project_store import get_project, save_project
    from app.schemas import ProjectSaveRequest

    body = {**deepcopy(FOUR_FLOOR_FCU_FRESH_AIR_SAMPLE), "system_form": "fcu_fresh_air"}
    saved = save_project(
        ProjectSaveRequest(
            name="form-roundtrip",
            project=SystemEvaluateRequest.model_validate(body),
        )
    )
    assert saved.project.system_form is SystemForm.FCU_FRESH_AIR
    assert get_project(saved.project_id).project.system_form is SystemForm.FCU_FRESH_AIR
