"""新风机组 + 送回风口 + 完整风管拓扑样例

覆盖四件事：

1. **回风口设备类型可用**：`回风口` 与 `送风口` 同属风系统拓扑节点
   （`DeviceRole.AIR_OUTLET`），不是未知类型回退到 `measurement`；
2. **送回风口成对出现在房间两侧**：送风口 → 回风口 代表室内空气，
   回风口再接回风管回到末端，形成完整的送风—室内—回风通路；
3. **风系统选型闭环**：最不利风路静压余量落在 `rules.json` 的
   `[1.05, 1.30]` 区间，风管风速落在 `air_velocity_m_s.main_duct` 区间；
4. **新风机组承担全部新风**：房间不出现 `fresh_air_no_handler`，
   湿度能收敛到目标附近且冷机不启停振荡。
"""

import itertools
import statistics
from copy import deepcopy

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.samples import DUCTED_FRESH_AIR_SAMPLE, SAMPLES
from app.schemas import SystemEvaluateRequest
from app.simcore.project import build_project_layout
from app.simcore.psychrometrics import (
    humidity_ratio_from_rh,
    rh_from_humidity_ratio,
)
from app.simcore.rated import resolve_rated_spec_by_type
from app.simcore.session import SimulationConfig, SimulationMode, SimulationSession
from app.unity_catalog import DeviceRole

client = TestClient(app)
SAMPLE_PATH = "/api/v1/project/sample/ducted_fresh_air"

# 风管干管风速区间与选型余量区间，与 app/data/rules.json 同源
VENT_VELOCITY_MIN = 2.0
VENT_VELOCITY_MAX = 8.0
MARGIN_MIN = 1.05
MARGIN_MAX = 1.30

TARGET_TEMP_C = 24.0
TARGET_RH_PCT = 55.0


def _sample() -> dict:
    return deepcopy(DUCTED_FRESH_AIR_SAMPLE)


def _evaluation(payload: dict) -> dict:
    response = client.post("/api/v1/system/evaluate", json=payload)
    assert response.status_code == 200
    return response.json()


def test_sample_is_registered_and_served():
    assert "ducted_fresh_air" in SAMPLES

    response = client.get(SAMPLE_PATH)
    assert response.status_code == 200
    payload = response.json()

    assert [floor["id"] for floor in payload["floors"]] == ["B1F", "F1", "F2", "RF"]
    assert {room["id"] for room in payload["rooms"]} == {
        "office_f1",
        "meeting_f1",
        "office_f2",
        "training_f2",
    }
    devices = {device["id"]: device for device in payload["devices"]}
    assert devices["fau_b1"]["type"] == "新风机组"
    assert {device["type"] for device in payload["devices"]} >= {
        "送风口",
        "回风口",
    }


@pytest.mark.parametrize("device_type", ["送风口", "回风口"])
def test_both_grille_types_map_to_air_outlet_role(device_type: str):
    """送风口与回风口同属风系统拓扑节点，且不是「未知类型」回退

    回风口在目录中原本不存在。未登记的类型会静默回退到 `measurement`，
    并带 `catalog_type_missing` 降级标记（实测：`回风口` 未登记时即如此）。
    本测试钉死它已正式登记，防止映射被误删。

    注意风口**故意**以 `origin="type_default"` 解析，而不是 `catalog`：
    目录里风口的唯一工程行名为 `"*"`，实体设备名匹配不上，
    强行命名反而会引入 `model_not_in_catalog`。故这里只断言
    「没有 catalog_type_missing」，而不是断言 `is_degraded is False`。
    """
    spec = resolve_rated_spec_by_type(device_type, None)
    assert spec.role is DeviceRole.AIR_OUTLET
    assert "catalog_type_missing" not in spec.degraded_fields, (
        f"{device_type} 回退成了未知类型（degraded={spec.degraded_fields}）；"
        "检查 DEVICE_TYPE_TO_ROLE 是否漏登记"
    )

    # 反向对照：真正未登记的类型必须出现该标记，确认判据本身有效
    unknown = resolve_rated_spec_by_type("不存在的风口类型", None)
    assert "catalog_type_missing" in unknown.degraded_fields


def test_return_grille_closes_the_supply_room_return_path():
    """送风口 → 回风口 → 回风管 → 末端 构成完整通路

    这是本样例相对旧样例的改进点：旧样例把送风口直接接回风管，房间只有
    一个风口节点；现在送风与回风各有一个风口，室内段由 送风口→回风口 表示。
    """
    payload = _sample()
    connections = payload["connections"]
    air = [c for c in connections if c["system"] == "air"]

    def targets(node: str) -> set[str]:
        return {c["to_id"] for c in air if c["from_id"] == node}

    for floor in ("f1", "f2"):
        supply = f"supply_outlet_{floor}"
        ret = f"return_outlet_{floor}"
        # 送风口由静压箱供风，再进入回风口（室内段）
        assert ret in targets(supply), f"{supply} 未接 {ret}"
        # 回风口接回风管，回到末端
        assert targets(ret) == {f"return_air_duct_{floor}"}
        assert f"fcu_{floor}" in targets(f"return_air_duct_{floor}")

    # 风口不是末端，不能绑定到房间（与既有 送风口 语义一致）
    bindable = {
        device_id
        for room in payload["rooms"]
        for device_id in room["device_ids"]
        if device_id.startswith(("supply_outlet_", "return_outlet_"))
    }
    assert bindable == set()


def test_air_network_is_sized_within_the_rule_ranges():
    """最不利风路静压余量与干管风速都落在 rules.json 区间内

    静压余量口径与 `rules.json` 的 `pump_margin.head` 一致（下限 1.05）；
    上限 1.30 用于避免选型过大。风速取 `air_velocity_m_s.main_duct`。
    """
    body = _evaluation(_sample())

    fan = body["air_network"]["fan_check"]
    assert fan["required_pressure_pa"] > 0.0
    margin = fan["available_pressure_pa"] / fan["required_pressure_pa"]
    assert MARGIN_MIN <= margin <= MARGIN_MAX, (
        f"静压余量 {margin:.3f} 不在 [{MARGIN_MIN}, {MARGIN_MAX}] 内；"
        "design_options.fan_static_pressure_pa 需按最不利风路重新选型"
    )
    codes = {issue["code"] for issue in body["issues"]}
    assert "FAN_PRESSURE_LOW" not in codes

    for duct in body["air_network"]["ducts"]:
        velocity = duct.get("velocity_m_s")
        if velocity is None:
            continue
        assert VENT_VELOCITY_MIN <= velocity <= VENT_VELOCITY_MAX, (
            f"风管 {duct['duct_id']} 风速 {velocity:.2f} m/s 超出 "
            f"[{VENT_VELOCITY_MIN}, {VENT_VELOCITY_MAX}]"
        )


def test_fresh_air_unit_carries_all_fresh_air_load():
    """新风机组承担全部房间新风，房间不出现「无承担者」

    否则新风会按室外状态直接注入房间（见 session.py 的有意建模选择），
    房间湿度将明显偏离目标。
    """
    layout = build_project_layout(SystemEvaluateRequest(**_sample())).layout
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
    handlers = session._fresh_air_handlers()
    assert set(handlers) == {room.room_id for room in layout.rooms}
    assert set(handlers.values()) == {"fau_b1"}

    # 新风机组进风必须是室外新风（而非室温回风）
    fau = next(n for n in layout.devices if n.rated.role is DeviceRole.FRESH_AIR_UNIT)
    assert session._terminal_room_reference_temp(fau) == pytest.approx(
        session._ambient().dry_bulb_c
    )

    # 机组额定风量应覆盖房间新风需求
    required = sum(room.fresh_air_m3_h for room in layout.rooms)
    assert (fau.rated.airflow_m3_h or 0.0) >= required

    codes = {
        issue["code"] for issue in _evaluation(_sample())["issues"]
    }
    assert "fresh_air_no_handler" not in codes
    assert "fresh_air_handler_without_cooling" not in codes


def test_humidity_settles_near_target_without_chiller_cycling():
    """湿度收敛到目标附近，且冷机不启停振荡

    长窗口统计（burn-in 3000 + 窗口 300）：样例是周期性的，单点采样不可靠。
    """
    layout = build_project_layout(SystemEvaluateRequest(**_sample())).layout
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
    for _ in range(3000):
        session.step()

    series = {room.room_id: [] for room in layout.rooms}
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
            ratio = humidity_ratio_from_rh(
                room.temperature_c, room.relative_humidity_pct
            )
            values.append(rh_from_humidity_ratio(TARGET_TEMP_C, ratio))

    assert snapshot is not None
    for room_id, values in sorted(series.items()):
        mean = statistics.fmean(values)
        assert mean <= TARGET_RH_PCT + 5.0, (
            f"{room_id} 湿度均值 {mean:.1f}% 明显高于目标 {TARGET_RH_PCT}%"
        )
        max_step = max(abs(b - a) for a, b in itertools.pairwise(values))
        assert max_step <= 5.0, (
            f"{room_id} 湿度单步最大变化 {max_step:.2f} pp，控制疑似振荡"
        )

    assert on_ticks == window, (
        f"冷机在末段 {window} tick 内仅开机 {on_ticks} 次，出现启停振荡"
    )
