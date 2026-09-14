"""三层十二房间样例（每层 3 办公 + 1 会议）

覆盖三件事：

1. **结构完整**：5 层（含机房与屋面）/ 12 房间 / 每层一套新风机组与送回风口；
2. **新风由专用机组承担**：12 个房间的新风各自归属本层新风机组，
   房间不出现 `fresh_air_no_handler`（否则新风会按室外状态直注房间）；
3. **温湿度保持在教学验收范围且冷机不振荡**：这是「风机盘管 + 独立新风机组」
   架构的设计验证。同层房间共用末端，验收 24℃±2℃，不承诺各房间精确恒温。

关于 `DUCT_OVERSIZE_REQUIRED`：本样例会报该警告，且**是预期行为**。
风机盘管与新风机组的额定风量汇总为 48720 m³/h；评估中的负荷风量另按
`calculate_air` 反算风管面积并触发超标准表提示（最大标准档为 2.0 m²）。
这是当前教学模型的汇总选型提示；本拓扑没有承载全部楼层风量的单一管段，
不能据此断言某一真实管段必须扩大。本文件保留该降级提示。
"""

import itertools
import statistics
from copy import deepcopy

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.samples import SAMPLES, THREE_FLOOR_TWELVE_ROOM_SAMPLE
from app.schemas import SystemEvaluateRequest
from app.simcore.project import build_project_layout
from app.simcore.psychrometrics import (
    humidity_ratio_from_rh,
    rh_from_humidity_ratio,
)
from app.simcore.session import SimulationConfig, SimulationMode, SimulationSession

client = TestClient(app)
SAMPLE_PATH = "/api/v1/project/sample/three_floor_twelve_room"

TARGET_TEMP_C = 24.0
TARGET_RH_PCT = 55.0
# 本样例的教学验收带：下界保留模型允许的 2℃ 除湿过冷，
# 上界允许同层共享末端的分区温差；不是工程舒适性标准。
TEMPERATURE_TOLERANCE_C = 2.0

ROOM_SUFFIXES = ("a", "b", "c", "m")
FKEYS = ("f1", "f2", "f3")


def _sample() -> dict:
    return deepcopy(THREE_FLOOR_TWELVE_ROOM_SAMPLE)


def _evaluation(payload: dict) -> dict:
    response = client.post("/api/v1/system/evaluate", json=payload)
    assert response.status_code == 200
    return response.json()


def _session(sample: dict) -> tuple[SimulationSession, object]:
    layout = build_project_layout(SystemEvaluateRequest(**sample)).layout
    session = SimulationSession(
        layout,
        SimulationConfig(
            mode=SimulationMode.COOLING,
            step_seconds=300.0,
            target_temp_c=TARGET_TEMP_C,
            target_humidity_pct=TARGET_RH_PCT,
            outdoor_humidity_pct=70.0,
            automatic_control=True,
        ),
    )
    return session, layout


def test_sample_is_registered_and_served():
    assert "three_floor_twelve_room" in SAMPLES

    response = client.get(SAMPLE_PATH)
    assert response.status_code == 200
    payload = response.json()

    # 5 层：机房 + 地上三层 + 屋面
    assert [floor["id"] for floor in payload["floors"]] == ["B1F", "F1", "F2", "F3", "RF"]
    # 12 房间：每层 3 办公 + 1 会议
    assert len(payload["rooms"]) == 12
    for fkey, fid in zip(FKEYS, ("F1", "F2", "F3"), strict=True):
        ids = {f"room_{fkey}_{s}" for s in ROOM_SUFFIXES}
        assert {r["id"] for r in payload["rooms"] if r["floor_id"] == fid} == ids


def test_every_room_is_served_by_its_own_floor_terminals():
    """每个房间绑定本层的风机盘管与新风机组，不得跨层"""
    payload = _sample()
    for fkey in FKEYS:
        for suffix in ROOM_SUFFIXES:
            room = next(r for r in payload["rooms"] if r["id"] == f"room_{fkey}_{suffix}")
            assert room["device_ids"] == [f"fcu_{fkey}", f"fau_{fkey}"], (
                f"{room['id']} 绑定了 {room['device_ids']}，应为本层的 fcu_{fkey}/fau_{fkey}"
            )


def test_fresh_air_is_handled_per_floor():
    """12 个房间的新风各归本层新风机组，没有房间缺少新风承担者"""
    session, layout = _session(_sample())
    handlers = session._fresh_air_handlers()

    assert set(handlers) == {room.room_id for room in layout.rooms}
    for fkey in FKEYS:
        for suffix in ROOM_SUFFIXES:
            assert handlers[f"room_{fkey}_{suffix}"] == f"fau_{fkey}"

    codes = {issue["code"] for issue in _evaluation(_sample())["issues"]}
    assert "fresh_air_no_handler" not in codes
    assert "fresh_air_handler_without_cooling" not in codes


def test_design_a_whole_building_network():
    """水系统与风系统都构成完整网络，且无孤立设备"""
    payload = _sample()
    codes = {issue["code"] for issue in _evaluation(payload)["issues"]}

    # 缺件类问题不应出现
    for code in (
        "MISSING_COLD_SOURCE",
        "MISSING_COOLING_TOWER",
        "MISSING_PUMP",
        "ISOLATED_DEVICE",
        "OPEN_WATER_LOOP",
    ):
        assert code not in codes, f"样例缺少 {code}"

    # 每层都要有冷冻水支路与风管
    pipe_ids = {p["id"] for p in payload["pipes"]}
    for fkey in FKEYS:
        assert f"chw_branch_supply_{fkey}" in pipe_ids
        assert f"chw_branch_return_{fkey}" in pipe_ids
        assert f"fau_supply_duct_{fkey}" in pipe_ids
        assert f"return_air_duct_{fkey}" in pipe_ids


def test_design_fresh_air_and_header_flow_are_consistent():
    """卫生新风按人数手算，总管额定风量与三个支路守恒。"""
    payload = _sample()
    body = _evaluation(payload)
    # 每层 51 名办公人员 × 30 + 22 名会议人员 × 35 = 2300 m³/h。
    assert sum(room["fresh_air_m3_h"] for room in body["load"]["rooms"]) == pytest.approx(6900.0)

    pipes = {pipe["id"]: pipe for pipe in payload["pipes"]}
    branch_flow = sum(pipes[f"outdoor_air_duct_{fkey}"]["flow_m3_h"] for fkey in FKEYS)
    assert branch_flow == pytest.approx(12000.0)
    for pipe_id in ("outdoor_air_intake", "outdoor_air_riser"):
        assert pipes[pipe_id]["flow_m3_h"] == pytest.approx(branch_flow)

    fan = body["air_network"]["fan_check"]
    assert fan["required_pressure_pa"] > 0.0
    assert fan["available_pressure_pa"] >= fan["required_pressure_pa"]


def test_duct_oversize_warning_is_expected_and_pinned():
    """系统汇总选型超出标准风管表，保留现有教学模型的提示

    `calculate_air` 按全楼调整后冷负荷 165.542 kW 和默认送回风温差反算
    负荷风量，再按目标风速求所需面积。面积超过
    `STANDARD_RECT_DUCTS` 的最大档（2000×1000 = 2.0 m²），于是报
    `DUCT_OVERSIZE_REQUIRED` 并选中最大档。

    这条测试钉住该行为：如果有人"修掉"这个警告（例如放大风管表、
    或静默钳位），说明他改变了选型语义——应当先想清楚再动。
    """
    from app.calculators.air import STANDARD_RECT_DUCTS

    body = _evaluation(_sample())
    codes = {issue["code"] for issue in body["issues"]}
    assert "DUCT_OVERSIZE_REQUIRED" in codes

    selected = body["air"]["selected_duct"]
    max_area = max(w / 1000.0 * h / 1000.0 for w, h in STANDARD_RECT_DUCTS)
    assert selected["area_m2"] == pytest.approx(max_area), (
        "超表时应退回标准表的最大档，而不是静默放大或钳位"
    )

    # 风网另按设备额定量汇总：3 × (6 × 2040 + 4000)，与负荷风量口径不同。
    fan = body["air_network"]["fan_check"]
    assert fan["required_airflow_m3_h"] == pytest.approx(48720.0)


def test_climate_settles_and_chiller_does_not_cycle():
    """长窗口统计：温度保持在验收带、湿度接近目标，冷机不启停振荡

    样例是周期性的，单点采样不可靠——必须先跨过暂态再取窗口均值
    （见 CLAUDE.md 的「测量纪律」）。
    """
    session, layout = _session(_sample())
    for _ in range(4000):
        session.step()

    series = {room.room_id: [] for room in layout.rooms}
    temperature_series = {room.room_id: [] for room in layout.rooms}
    on_ticks = 0
    window = 300
    snapshot = None
    for _ in range(window):
        snapshot = session.step()
        if any(
            reading.values.get("enabled", 0.0) > 0.0
            for reading in snapshot.devices.values()
            if reading.role in {"chiller", "heat_source"}
        ):
            on_ticks += 1
        for room_id, values in series.items():
            room = snapshot.rooms[room_id]
            temperature_series[room_id].append(room.temperature_c)
            ratio = humidity_ratio_from_rh(
                room.temperature_c, room.relative_humidity_pct
            )
            values.append(rh_from_humidity_ratio(TARGET_TEMP_C, ratio))

    assert snapshot is not None

    for room_id, temperatures in sorted(temperature_series.items()):
        assert min(temperatures) >= TARGET_TEMP_C - TEMPERATURE_TOLERANCE_C, (
            f"{room_id} 最低温度 {min(temperatures):.2f}℃ 低于教学验收下限"
        )
        assert max(temperatures) <= TARGET_TEMP_C + TEMPERATURE_TOLERANCE_C, (
            f"{room_id} 最高温度 {max(temperatures):.2f}℃ 高于教学验收上限"
        )
        assert max(temperatures) - min(temperatures) <= 1.0, (
            f"{room_id} 温度窗口波动超过 1℃，尚未稳定"
        )

    # 办公房间的除湿由新风机组承担，应能做到目标附近
    for room_id, values in sorted(series.items()):
        mean = statistics.fmean(values)
        assert mean <= TARGET_RH_PCT + 6.0, (
            f"{room_id} 湿度均值 {mean:.1f}% 明显高于目标 {TARGET_RH_PCT}%"
        )
        max_step = max(abs(b - a) for a, b in itertools.pairwise(values))
        assert max_step <= 5.0, (
            f"{room_id} 湿度单步最大变化 {max_step:.2f} pp，控制疑似振荡"
        )

    assert on_ticks == window, (
        f"冷机在末段 {window} tick 内仅开机 {on_ticks} 次，出现启停振荡"
    )
