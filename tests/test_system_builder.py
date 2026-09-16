"""系统骨架生成：从楼层与房间生成两族制式的完整拓扑

本文件钉住生成器的四类契约：

1. **自洽**：两族制式生成出的项目都通过通用校验与制式自检，且 0 error。
2. **结构正确**：水环路闭合、全空气机组的送/回风两侧都接出去、房间末端
   按制式绑定（风机盘管制式绑风机盘管 + 新风机组，全空气制式只绑组合式机组）。
3. **选型口径正确**：新风机组按**新风量**选（不是按冷负荷），组合式机组按
   分区**总负荷**选，风机盘管按**室内侧负荷**选。
4. **能跑**：生成结果能评估、能跑实时仿真并收敛，且守恒（末端水量之和等于
   水泵流量）。

生成器是**纯函数**：同输入必得同输出，且不改写调用方传进来的请求对象。

一个必须知情的对照事实（见 `test_generated_issues_are_a_subset_of_sample_issues`）：
生成结果的 issue 集合是**对应手写样例的真子集**。剩余 warning 全部是平台的
既有基线行为（`HYDRAULIC_IMBALANCE`、`AIR_TREE_UNSUPPORTED`、
`SIM_DEGRADED_PARAMETERS` 等），手写样例同样报——不是生成器引入的。
"""

import math
import statistics
from copy import deepcopy

import pytest
from fastapi.testclient import TestClient

from app.calculators.load import calculate_load
from app.data.equipment import load_equipment_catalog
from app.main import app
from app.samples import (
    FOUR_FLOOR_FCU_FRESH_AIR_SAMPLE,
    MULTI_ZONE_ALL_AIR_SAMPLE,
)
from app.schemas import (
    LoadRequest,
    SystemEvaluateRequest,
    SystemForm,
    SystemGenerateRequest,
)
from app.simcore.project import build_project_layout
from app.simcore.session import SimulationConfig, SimulationMode, SimulationSession
from app.system_builder import SystemGenerationUnavailableError, build_system
from app.validators.system_form import check_system_form

client = TestClient(app)

_FCU = "风机盘管"
_AHU = "组合式空调机组"
_FAU = "新风机组"
_CATALOG = load_equipment_catalog()


def _generate(raw: dict, form: str, **options) -> SystemGenerateRequest:
    body: dict = {
        "floors": deepcopy(raw["floors"]),
        "rooms": deepcopy(raw["rooms"]),
        "system_form": form,
    }
    if options:
        body["options"] = options
    return SystemGenerateRequest.model_validate(body)


def _build(raw: dict, form: str, **options):
    return build_system(_generate(raw, form, **options))


def _catalog_row(device_type: str, model_name: str | None) -> dict:
    for row in _CATALOG:
        if row["type"] == device_type and row["name"] == model_name:
            return row
    return {}


def _installed_airflow(project, device_type: str) -> float:
    """某族机组的额定送风量合计（目录 airflow_m3_h × 台数）。"""
    total = 0.0
    for device in project.devices:
        if device.type != device_type:
            continue
        row = _catalog_row(device_type, device.name)
        total += float(row.get("airflow_m3_h") or 0.0) * device.quantity
    return total


def _fresh_air_demand(raw: dict) -> float:
    load = calculate_load(
        LoadRequest(rooms=raw["rooms"], floors=raw["floors"], simultaneity_factor=0.9, safety_factor=1.1)
    )
    return math.fsum(item.fresh_air_m3_h for item in load.rooms)


# ---------------------------------------------------------------------------
# 1. 自洽：两族制式都 0 error，且制式自检通过
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "form"),
    [
        (FOUR_FLOOR_FCU_FRESH_AIR_SAMPLE, "fcu_fresh_air"),
        (MULTI_ZONE_ALL_AIR_SAMPLE, "fcu_fresh_air"),
        (FOUR_FLOOR_FCU_FRESH_AIR_SAMPLE, "all_air"),
        (MULTI_ZONE_ALL_AIR_SAMPLE, "all_air"),
    ],
)
def test_generation_is_error_free_and_form_compliant(raw: dict, form: str):
    result = _build(raw, form)

    errors = [issue for issue in result.issues if issue.level == "error"]
    assert errors == [], [issue.code for issue in errors]
    assert result.degraded is False
    assert check_system_form(result.project).compliant is True


def test_generated_issues_are_a_subset_of_sample_issues():
    """生成结果不比手写样例更"脏"——剩余 warning 都是平台既有基线行为

    对照同一栋楼、同一制式的手写样例：生成结果允许报出样例也报的码，
    但不允许出现样例没有的新 warning（那才是生成器引入的问题）。
    """
    for raw, form in (
        (FOUR_FLOOR_FCU_FRESH_AIR_SAMPLE, "fcu_fresh_air"),
        (MULTI_ZONE_ALL_AIR_SAMPLE, "all_air"),
    ):
        generated = {
            issue.code for issue in _build(raw, form).issues if issue.level != "info"
        }
        sample = {
            issue["code"]
            for issue in client.post("/api/v1/system/evaluate", json=raw).json()["issues"]
            if issue["level"] != "info"
        }
        assert generated <= sample, generated - sample


def test_generation_is_deterministic():
    first = _build(FOUR_FLOOR_FCU_FRESH_AIR_SAMPLE, "fcu_fresh_air")
    second = _build(FOUR_FLOOR_FCU_FRESH_AIR_SAMPLE, "fcu_fresh_air")
    assert first.model_dump(mode="json") == second.model_dump(mode="json")


def test_generation_does_not_mutate_the_request():
    request = _generate(FOUR_FLOOR_FCU_FRESH_AIR_SAMPLE, "fcu_fresh_air")
    before = request.model_dump(mode="json")
    build_system(request)
    assert request.model_dump(mode="json") == before


def test_generation_requires_rooms():
    with pytest.raises(SystemGenerationUnavailableError) as excinfo:
        build_system(SystemGenerateRequest.model_validate(
            {"rooms": [], "system_form": "all_air"}
        ))
    assert excinfo.value.detail["missing"] == ["rooms"]


# ---------------------------------------------------------------------------
# 2. 结构正确：环路闭合、风管成环、房间绑定
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("form", ["fcu_fresh_air", "all_air"])
def test_water_loops_are_closed(form: str):
    project = _build(FOUR_FLOOR_FCU_FRESH_AIR_SAMPLE, form).project

    hydraulic = client.post(
        "/api/v1/system/evaluate", json=project.model_dump(mode="json")
    ).json()["hydraulic_network"]

    # 冷冻水与冷却水都必须形成闭合回路，否则扬程会退化成 0。
    assert hydraulic["required_pump_head_m"] > 0
    closed_systems = {
        path["system"] for path in hydraulic["paths"] if path["closed_circuit"]
    }
    assert "chilled_water" in closed_systems
    assert "cooling_water" in closed_systems


def test_all_air_ahu_ducts_connect_both_sides():
    """全空气机组的送风与回风两侧都要接出去（制式校验的 error 项）"""
    project = _build(MULTI_ZONE_ALL_AIR_SAMPLE, "all_air").project
    codes = {issue.code for issue in check_system_form(project).issues}
    assert "SYSTEM_FORM_AHU_DUCT_INCOMPLETE" not in codes


def test_rooms_are_bound_to_the_terminal_family_of_the_form():
    fcu_project = _build(FOUR_FLOOR_FCU_FRESH_AIR_SAMPLE, "fcu_fresh_air").project
    assert fcu_project.rooms
    for room in fcu_project.rooms:
        assert any(device_id.startswith("fcu_") for device_id in room.device_ids)
        assert any(device_id.startswith("fau_") for device_id in room.device_ids)

    all_air_project = _build(FOUR_FLOOR_FCU_FRESH_AIR_SAMPLE, "all_air").project
    for room in all_air_project.rooms:
        assert room.device_ids
        assert all(device_id.startswith("ahu_") for device_id in room.device_ids)


def test_generated_rooms_always_have_ids():
    """房间 id 是负荷分项与末端绑定的关联键，生成器必须补齐"""
    raw = deepcopy(FOUR_FLOOR_FCU_FRESH_AIR_SAMPLE)
    for room in raw["rooms"]:
        room.pop("id", None)
    project = _build(raw, "fcu_fresh_air").project
    assert all(room.id for room in project.rooms)
    assert len({room.id for room in project.rooms}) == len(project.rooms)


def test_air_ducts_are_sized_and_carry_flow():
    """风管必须显式给风量与断面：平台不会自动反算风管尺寸"""
    for form in ("fcu_fresh_air", "all_air"):
        project = _build(MULTI_ZONE_ALL_AIR_SAMPLE, form).project
        ducts = [pipe for pipe in project.pipes if pipe.system == "air"]
        assert ducts
        for pipe in ducts:
            assert pipe.flow_m3_h and pipe.flow_m3_h > 0, pipe.id
            assert pipe.width_mm and pipe.height_mm, pipe.id


# ---------------------------------------------------------------------------
# 3. 选型口径
# ---------------------------------------------------------------------------


def test_fresh_air_unit_is_sized_on_fresh_air_volume():
    """新风机组按卫生新风量选，不按冷负荷选"""
    project = _build(FOUR_FLOOR_FCU_FRESH_AIR_SAMPLE, "fcu_fresh_air").project
    demand = _fresh_air_demand(FOUR_FLOOR_FCU_FRESH_AIR_SAMPLE)
    installed = _installed_airflow(project, _FAU)
    assert demand > 0
    # 选出的机组风量必须覆盖新风需求（整数台数只会超配，不会欠配）。
    assert installed >= demand


def _lab_building(fresh_air_per_person: float) -> dict:
    """一栋"实验室"用途的建筑：新风量可独立调节，而室内负荷不变。

    用途 `lab` 不在 `PURPOSE_DEFAULTS` 里，因此 `load.py` 会回退到房间自己的
    字段（`defaults.get(key, room.<field>)`）。这样就能**只改新风量、不动室内
    负荷**——正是区分"按室内侧负荷选型"与"按总负荷选型"所需要的对照条件。
    """
    return {
        "floors": [
            {"id": "F1", "name": "一层", "level": 1, "elevation_m": 0.0, "height_m": 3.9}
        ],
        "rooms": [
            {
                "id": "lab_1",
                "floor_id": "F1",
                "name": "实验室",
                "area_m2": 200,
                "people": 30,
                "purpose": "lab",
                "cooling_index_w_m2": 115.0,
                "heating_index_w_m2": 75.0,
                "lighting_w_m2": 10.0,
                "equipment_w_m2": 15.0,
                "fresh_air_m3_h_person": fresh_air_per_person,
            }
        ],
    }


def _by_type(project, device_type: str) -> set[tuple[str | None, int]]:
    return {
        (device.name, device.quantity)
        for device in project.devices
        if device.type == device_type
    }


def test_fan_coil_is_sized_on_indoor_load_only():
    """风机盘管不承担新风：新风量翻三倍，它的选型必须**一点不变**

    用一栋用途未知（`lab`）的建筑把新风量与室内负荷解耦，这条断言才有区分力：
    若风机盘管错用了含新风的总负荷，新风量变化必然带动它的型号或台数。
    对照之下，承担新风的新风机组必须跟着变——否则是这条测试自己失效了
    （两边都没变会造成假通过）。新风量跨度取得足够大（600 vs 3600 m³/h），
    以保证对照断言真的会跨过型号档位。
    """
    low = _build(_lab_building(20.0), "fcu_fresh_air").project
    high = _build(_lab_building(120.0), "fcu_fresh_air").project

    assert _by_type(low, _FCU), "应当生成风机盘管"
    assert _by_type(low, _FCU) == _by_type(high, _FCU), "风机盘管不应随新风量变化"
    assert _by_type(low, _FAU) != _by_type(high, _FAU), "新风机组应随新风量变化"


def test_all_air_unit_is_sized_on_total_load_including_fresh_air():
    """全空气制式相反：新风由机组混风箱承担，所以它的选型**必须**随新风量变化"""
    low = _build(_lab_building(20.0), "all_air").project
    high = _build(_lab_building(120.0), "all_air").project

    assert _by_type(low, _AHU), "应当生成组合式空调机组"
    assert _by_type(low, _AHU) != _by_type(high, _AHU), "组合式机组应随新风量变化"


def test_multi_floor_all_air_evaluates_without_errors():
    """多层全空气建筑必须能无 error 评估

    每层一台组合式机组、各带自己的风管，风网本来就由多个互相独立的子系统
    组成。历史实现的连通性校验把"多个连通分量"一律判为断路，会让**任何**
    多层全空气项目报 `disconnected_network` error。
    """
    building = {
        "floors": [
            {"id": "B1", "name": "机房", "level": -1, "elevation_m": -4.0, "height_m": 4.0},
            {"id": "L1", "name": "一层", "level": 1, "elevation_m": 0.0, "height_m": 4.2},
            {"id": "L2", "name": "二层", "level": 2, "elevation_m": 4.2, "height_m": 3.9},
            {"id": "RF", "name": "屋面", "level": 3, "elevation_m": 8.1, "height_m": 2.0},
        ],
        "rooms": [
            {"id": "l1_a", "floor_id": "L1", "name": "一层办公", "area_m2": 200, "people": 34, "purpose": "office"},
            {"id": "l2_a", "floor_id": "L2", "name": "二层办公", "area_m2": 180, "people": 30, "purpose": "office"},
        ],
    }
    project = _build(building, "all_air").project
    assert len({device.floor_id for device in project.devices if device.type == _AHU}) == 2

    body = client.post(
        "/api/v1/system/evaluate", json=project.model_dump(mode="json")
    ).json()
    errors = [issue["code"] for issue in body["issues"] if issue["level"] == "error"]
    assert errors == []


def test_heat_source_and_hot_water_loop_are_generated_by_default():
    project = _build(FOUR_FLOOR_FCU_FRESH_AIR_SAMPLE, "fcu_fresh_air").project
    assert any(device.id == "heat_source_roof" for device in project.devices)
    assert {pipe.system for pipe in project.pipes} >= {"hot_water"}


def test_include_heating_false_omits_the_heating_side():
    project = _build(
        FOUR_FLOOR_FCU_FRESH_AIR_SAMPLE, "fcu_fresh_air", include_heating=False
    ).project
    assert not [pipe for pipe in project.pipes if pipe.system == "hot_water"]
    assert not [device for device in project.devices if device.id == "heat_source_roof"]


def test_without_heating_the_heating_gap_is_reported_honestly():
    """关掉制热侧后，评估必须如实报出制热侧的容量缺口

    这不是缺陷：`calculate_load` 按冬季参数算出了供暖负荷，而系统没有热源。
    生成器不掩盖它，评估器把它报成 `energy_balance_violation`。
    """
    project = _build(
        FOUR_FLOOR_FCU_FRESH_AIR_SAMPLE, "fcu_fresh_air", include_heating=False
    ).project
    issues = client.post(
        "/api/v1/system/evaluate", json=project.model_dump(mode="json")
    ).json()["issues"]
    codes = {issue["code"] for issue in issues}
    assert "energy_balance_violation" in codes


def test_every_device_resolves_to_a_concrete_model():
    """每台设备都要能被目录解析，否则会报 `UNKNOWN_DEVICE_CATALOG` 并跳过兼容性检查

    两条合法路径：给出具体型号，或该类型有 `*` 回退行（风口与风阀有回退行，
    **循环泵没有**，所以泵必须落到具体型号）。目录里没有回退行的类型如果
    留下 `name=None`，校验就会退化成"没查过"。
    """
    fallback_types = {
        row["type"] for row in _CATALOG if str(row.get("name")) == "*"
    }
    for form in ("fcu_fresh_air", "all_air"):
        project = _build(FOUR_FLOOR_FCU_FRESH_AIR_SAMPLE, form).project
        unresolved = [
            device.id
            for device in project.devices
            if not device.name and device.type not in fallback_types
        ]
        assert unresolved == []


# ---------------------------------------------------------------------------
# 4. 能跑：评估、守恒、实时收敛
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "form"),
    [
        (FOUR_FLOOR_FCU_FRESH_AIR_SAMPLE, "fcu_fresh_air"),
        (MULTI_ZONE_ALL_AIR_SAMPLE, "all_air"),
    ],
)
def test_generated_project_evaluates_without_errors(raw: dict, form: str):
    project = _build(raw, form).project
    response = client.post("/api/v1/system/evaluate", json=project.model_dump(mode="json"))
    assert response.status_code == 200
    body = response.json()
    assert [issue for issue in body["issues"] if issue["level"] == "error"] == []
    equipment = body["steady_network"]["equipment"]
    # 冷源与末端容量都必须盖住负荷，制冷侧余量应为 0。
    assert equipment["cooling_balance_residual_kw"] == pytest.approx(0.0, abs=0.01)


def test_generated_topology_conserves_chilled_water_flow():
    """末端水量之和必须等于冷冻水泵流量（与四层样例同一守恒断言）"""
    project = _build(FOUR_FLOOR_FCU_FRESH_AIR_SAMPLE, "fcu_fresh_air").project
    layout = build_project_layout(project).layout
    session = SimulationSession(
        layout,
        SimulationConfig(
            mode=SimulationMode.COOLING,
            initial_room_temp_c=29.0,
            target_temp_c=24.0,
            automatic_control=True,
        ),
    )
    snapshot = session.step()

    terminal_flow = math.fsum(
        snapshot.devices[device.id].values["water_flow_m3_h"]
        for device in project.devices
        if device.id.startswith(("fcu_", "fau_"))
    )
    assert terminal_flow > 0
    assert snapshot.chilled_flow_m3_h == pytest.approx(terminal_flow, abs=0.01)


@pytest.mark.parametrize(
    ("raw", "form"),
    [
        (FOUR_FLOOR_FCU_FRESH_AIR_SAMPLE, "fcu_fresh_air"),
        (MULTI_ZONE_ALL_AIR_SAMPLE, "all_air"),
    ],
)
def test_generated_project_runs_and_holds_room_temperature(raw: dict, form: str):
    """跑到暂态之后，室温应稳定在目标附近且冷机不振荡

    口径与内置样例一致：先把房间从 28℃ 带到目标，再统计窗口内的室温分布；
    同时统计冷机开机率，用于发现启停振荡。
    """
    project = _build(raw, form).project
    layout = build_project_layout(project).layout
    session = SimulationSession(
        layout,
        SimulationConfig(
            mode=SimulationMode.COOLING,
            step_seconds=300.0,
            initial_room_temp_c=28.0,
            target_temp_c=24.0,
            target_humidity_pct=55.0,
            outdoor_humidity_pct=70.0,
            automatic_control=True,
        ),
    )
    for _ in range(300):
        session.step()

    temps: list[float] = []
    chiller_on = 0
    window = 60
    for _ in range(window):
        snapshot = session.step()
        temps.extend(session._room_temp(room.id) for room in project.rooms)
        chiller_id = next(
            (key for key in snapshot.devices if "chiller" in key), None
        )
        if chiller_id and snapshot.devices[chiller_id].values.get("enabled"):
            chiller_on += 1

    assert statistics.mean(temps) == pytest.approx(24.0, abs=2.0)
    assert min(temps) > 18.0
    assert max(temps) < 30.0
    assert chiller_on == window, "冷机不应在稳态附近启停振荡"


# ---------------------------------------------------------------------------
# 5. HTTP 契约
# ---------------------------------------------------------------------------


def test_generate_endpoint_contract():
    response = client.post(
        "/api/v1/system/generate",
        json={
            "floors": FOUR_FLOOR_FCU_FRESH_AIR_SAMPLE["floors"],
            "rooms": FOUR_FLOOR_FCU_FRESH_AIR_SAMPLE["rooms"],
            "system_form": "fcu_fresh_air",
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["degraded"] is False
    assert body["decisions"], "生成必须给出可解释的决策记录"
    assert {item["source"] for item in body["decisions"]} <= {
        "catalog",
        "rules",
        "calculated",
        "default",
    }
    project = SystemEvaluateRequest.model_validate(body["project"])
    assert project.system_form is SystemForm.FCU_FRESH_AIR
    assert project.devices and project.pipes and project.connections


def test_generate_endpoint_rejects_missing_rooms_and_unknown_form():
    empty = client.post(
        "/api/v1/system/generate", json={"rooms": [], "system_form": "all_air"}
    )
    assert empty.status_code == 422
    assert empty.json()["detail"]["code"] == "SYSTEM_GENERATION_REQUIREMENTS_UNAVAILABLE"

    unknown = client.post(
        "/api/v1/system/generate", json={"rooms": [{"name": "r"}], "system_form": "vav"}
    )
    assert unknown.status_code == 422


def test_generate_endpoint_requires_an_explicit_form():
    """生成没有默认制式：不说清生成哪一族就不该返回结果"""
    response = client.post(
        "/api/v1/system/generate",
        json={"rooms": [{"name": "r"}], "floors": []},
    )
    assert response.status_code == 422


def test_generate_endpoint_is_read_only():
    """生成不落盘：不新增项目、不改动已有项目列表"""
    before = client.get("/api/v1/projects").json()
    client.post(
        "/api/v1/system/generate",
        json={
            "floors": MULTI_ZONE_ALL_AIR_SAMPLE["floors"],
            "rooms": MULTI_ZONE_ALL_AIR_SAMPLE["rooms"],
            "system_form": "all_air",
        },
    )
    assert client.get("/api/v1/projects").json() == before


def test_generated_project_can_be_saved_and_reloaded():
    """端到端：生成 → 保存 → 读回 → 评估，制式与拓扑都要保住"""
    generated = client.post(
        "/api/v1/system/generate",
        json={
            "floors": FOUR_FLOOR_FCU_FRESH_AIR_SAMPLE["floors"],
            "rooms": FOUR_FLOOR_FCU_FRESH_AIR_SAMPLE["rooms"],
            "system_form": "fcu_fresh_air",
        },
    ).json()["project"]

    saved = client.post(
        "/api/v1/projects", json={"name": "generated-fcu", "project": generated}
    )
    assert saved.status_code == 200
    project_id = saved.json()["project_id"]

    reloaded = client.get(f"/api/v1/projects/{project_id}").json()["project"]
    assert reloaded["system_form"] == "fcu_fresh_air"
    assert len(reloaded["devices"]) == len(generated["devices"])
    assert len(reloaded["connections"]) == len(generated["connections"])

    evaluated = client.post(
        "/api/v1/system/form-check", json=reloaded
    )
    assert evaluated.status_code == 200
    assert evaluated.json()["compliant"] is True
