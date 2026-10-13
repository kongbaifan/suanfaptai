"""三层商业楼样例（按 Unity 平面截图建模，只做夏季）

钉住四件事：

1. **建筑来自图纸**：B1F 机房 + F1~F3 + RF 屋面；房间是各层隔墙围出的分区，
   一层 2 区、二层 5 区、三层 4 区，全部按商场、8 m²/人（GB 50189）。
2. **系统来自生成器**：用同一份楼层与房间重新跑 `build_system`，必须得到冻结的
   那份项目——样例不是手调出来的，生成器的口径就是样例的口径。
3. **只做夏季**：没有热源，制热缺口如实报 `energy_balance_violation`。
4. **能跑**：0 error、制式自检通过、实时冷机连续运行、湿度到目标附近。
"""

from __future__ import annotations

import json
import statistics
from collections import Counter
from copy import deepcopy

from fastapi.testclient import TestClient

from app.main import app
from app.samples import SAMPLES, THREE_FLOOR_RETAIL_SAMPLE
from app.schemas import SystemEvaluateRequest, SystemGenerateRequest
from app.simcore.project import build_project_layout
from app.simcore.psychrometrics import humidity_ratio_from_rh, rh_from_humidity_ratio
from app.simcore.session import SimulationConfig, SimulationMode, SimulationSession
from app.system_builder import build_system

client = TestClient(app)
# 生成器自己写回房间的字段：重新生成时必须从输入里去掉，否则就不是"从建筑生成"
_GENERATED_ROOM_FIELDS = ("device_ids", "supply_air_outlet_ids", "return_air_outlet_ids")


def _sample() -> dict:
    return deepcopy(THREE_FLOOR_RETAIL_SAMPLE)


def _building_request() -> SystemGenerateRequest:
    sample = _sample()
    rooms = [
        {k: v for k, v in room.items() if k not in _GENERATED_ROOM_FIELDS}
        for room in sample["rooms"]
    ]
    return SystemGenerateRequest(
        floors=sample["floors"], rooms=rooms, system_form="fcu_fresh_air",
        options={"include_heating": False},
    )


def test_sample_is_registered_and_served():
    assert "three_floor_retail" in SAMPLES
    payload = client.get("/api/v1/project/sample/three_floor_retail").json()

    assert [f["id"] for f in payload["floors"]] == ["B1F", "F1", "F2", "F3", "RF"]
    assert Counter(r["floor_id"] for r in payload["rooms"]) == {"F1": 2, "F2": 5, "F3": 4}
    assert {r["purpose"] for r in payload["rooms"]} == {"mall"}
    assert sum(r["area_m2"] for r in payload["rooms"]) == 1722
    for room in payload["rooms"]:
        assert room["people"] == round(room["area_m2"] / 8.0)


def test_sample_is_exactly_what_the_generator_builds_from_the_floor_plan():
    """冻结的项目必须与生成器从同一份楼层/房间生成的结果一致

    失败说明生成器口径变了（选型、管路、风管尺寸……）。确认新结果合理后重新冻结：
        project = build_system(<本测试的 _building_request()>).project
        写入 app/data/sample_three_floor_retail.json
        （model_dump(mode="json", exclude_defaults=True)，indent=1，ensure_ascii=False）
    """
    regenerated = build_system(_building_request())
    assert regenerated.degraded is False
    assert regenerated.issues == []

    frozen = SystemEvaluateRequest(**_sample()).model_dump(mode="json")
    fresh = regenerated.project.model_dump(mode="json")
    assert fresh == frozen, "生成器结果与冻结样例不一致，见本测试 docstring 重新冻结"


def test_plant_is_sized_on_the_load_and_summer_only():
    evaluation = client.post("/api/v1/system/evaluate", json=_sample()).json()
    devices = {d["id"]: d for d in _sample()["devices"]}

    assert [i["code"] for i in evaluation["issues"] if i["level"] == "error"] == []
    assert devices["chiller_plant"]["name"] == "SCWE-90H+B"  # 目录最小冷机 308 kW
    assert devices["tower_roof"]["name"] == "S15E-110"  # 排热 360.8 kW / 66.3 m³/h
    assert devices["cw_pump_plant"]["name"] == "TPE3 80-200"
    # 只做夏季：没有热源，制热缺口如实报出
    assert not any(d["type"] in {"空气源热泵", "水源热泵", "地源热泵"} for d in devices.values())
    assert "energy_balance_violation" in {i["code"] for i in evaluation["issues"]}
    # 稳态收敛、冷却水通（只允许回风口/送风口无型号这类参数降级）
    assert {i["code"] for i in evaluation["simulation"]["issues"]} <= {"SIM_DEGRADED_PARAMETERS"}


def test_declared_form_is_compliant():
    body = client.post("/api/v1/system/form-check", json=_sample()).json()
    assert body["declared"] is True
    assert body["compliant"] is True


def test_cooling_runs_continuously_and_dehumidifies_to_target():
    """实时制冷（24 ℃ / 55%，室外 70%）：冷机连续运行，湿度到目标附近

    室温稳定在目标以上 1.4~1.9 K：风机盘管水阀只开 45~50%，没有饱和，偏差来自
    自动控制的比例带（`AUTO_FULL_DEMAND_C` = 4 K），不是末端选小了
    （见 CLAUDE.md「末端超配与自动控制律强耦合」）。
    """
    layout = build_project_layout(SystemEvaluateRequest(**_sample())).layout
    session = SimulationSession(
        layout,
        SimulationConfig(
            mode=SimulationMode.COOLING, step_seconds=300.0, target_temp_c=24.0,
            target_humidity_pct=55.0, outdoor_humidity_pct=70.0, automatic_control=True,
        ),
    )
    for _ in range(3000):
        session.step()
    temps: dict[str, list[float]] = {room.room_id: [] for room in layout.rooms}
    rh: dict[str, list[float]] = {room.room_id: [] for room in layout.rooms}
    chiller_on = 0
    for _ in range(300):
        snapshot = session.step()
        chiller_on += snapshot.devices["chiller_plant"].values["enabled"] > 0.0
        for room_id, series in temps.items():
            reading = snapshot.rooms[room_id]
            series.append(reading.temperature_c)
            ratio = humidity_ratio_from_rh(reading.temperature_c, reading.relative_humidity_pct)
            rh[room_id].append(rh_from_humidity_ratio(24.0, ratio))

    assert chiller_on == 300
    for room_id, series in temps.items():
        assert 24.0 <= statistics.fmean(series) <= 26.5, room_id
        assert statistics.fmean(rh[room_id]) <= 58.0, room_id


def test_frozen_json_is_canonical():
    """冻结文件就是 exclude_defaults 的规范输出，没有手工改动"""
    text = json.dumps(
        SystemEvaluateRequest(**_sample()).model_dump(mode="json", exclude_defaults=True),
        ensure_ascii=False, indent=1,
    )
    assert json.loads(text) == _sample()
