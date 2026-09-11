"""实时管段读数

`GET /api/v1/realtime/pipes/{project_id}` 的行为契约与守恒断言。管段读数是
按末端实时水量反算的派生量，测试重点在三处：流量守恒、拓扑归属、只读语义。
"""

from fastapi.testclient import TestClient

from app import realtime
from app.data import project_store
from app.main import app
from app.schemas import RealtimePipeStatesResult


client = TestClient(app)


def _create_four_floor_project(name: str = "pytest_pipes"):
    sample = client.get("/api/v1/project/sample/four_floor_fcu_fresh_air").json()
    response = client.post("/api/v1/projects", json={"name": name, "project": sample})
    assert response.status_code == 200
    return response.json()["project_id"], sample


def _by_id(body: dict) -> dict[str, dict]:
    return {pipe["pipe_id"]: pipe for pipe in body["pipes"]}


def test_pipe_states_cover_every_water_pipe_and_skip_air():
    """水管全部出现且只出现一次；风管不在水力读数里。"""
    project_id, sample = _create_four_floor_project()
    try:
        body = client.get(f"/api/v1/realtime/pipes/{project_id}").json()

        water_ids = [
            pipe["id"]
            for pipe in sample["pipes"]
            if pipe["system"] in {"chilled_water", "cooling_water", "hot_water"}
        ]
        air_ids = {pipe["id"] for pipe in sample["pipes"] if pipe["system"] == "air"}
        returned = [pipe["pipe_id"] for pipe in body["pipes"]]

        assert sorted(returned) == sorted(water_ids)
        assert len(returned) == len(set(returned))
        assert not (set(returned) & air_ids)
        assert RealtimePipeStatesResult.model_validate(body).model_dump() == body
    finally:
        client.delete(f"/api/v1/projects/{project_id}")


def test_idle_project_reports_zero_flow_snapshot():
    """仿真未启动时是 tick-zero 快照：全零流量，不是缺数据。"""
    project_id, _ = _create_four_floor_project("pytest_pipes_idle")
    try:
        body = client.get(f"/api/v1/realtime/pipes/{project_id}").json()

        assert body["project_id"] == project_id
        assert body["running"] is False
        assert body["simcore_tick"] == 0
        assert body["most_loaded_pipe_id"] is None
        assert all(pipe["flow_m3_h"] == 0.0 for pipe in body["pipes"])
        assert all(pipe["velocity_m_s"] == 0.0 for pipe in body["pipes"])
        assert all(pipe["pressure_drop_pa"] == 0.0 for pipe in body["pipes"])
        assert all(pipe["head_m"] == 0.0 for pipe in body["pipes"])
        assert body["total_flow_by_system_m3_h"] == {
            "chilled_water": 0.0,
            "cooling_water": 0.0,
            "hot_water": 0.0,
        }
    finally:
        client.delete(f"/api/v1/projects/{project_id}")


def test_running_cooling_conserves_flow_from_terminals_to_mains():
    """主管流量必须等于末端水量之和，支管等于它服务的那台末端。

    这是本端点唯一的物理承诺：读数是末端实时水量的汇总，不能凭空多也不能漏。
    """
    project_id, _ = _create_four_floor_project("pytest_pipes_cooling")
    try:
        with TestClient(app) as runtime_client:
            try:
                assert runtime_client.post(
                    f"/api/v1/realtime/simulate/start/{project_id}",
                    json={"mode": "cooling", "interval_seconds": 10.0},
                ).status_code == 200

                state = runtime_client.get(
                    f"/api/v1/realtime/state/{project_id}"
                ).json()
                body = runtime_client.get(
                    f"/api/v1/realtime/pipes/{project_id}"
                ).json()

                assert body["running"] is True
                assert body["simcore_tick"] == 1

                terminal_flows = {
                    device_id: reading["values"]["water_flow_m3_h"]
                    for device_id, reading in state["devices"].items()
                    if reading["values"].get("role") == "terminal"
                }
                total = sum(terminal_flows.values())
                assert total > 0.0

                pipes = _by_id(body)
                assert pipes["chw_main_supply_b1"]["flow_m3_h"] == round(total, 3)
                assert pipes["chw_main_return_b1"]["flow_m3_h"] == round(total, 3)
                assert pipes["chw_supply_riser"]["flow_m3_h"] == round(total, 3)
                for floor in ("f1", "f2", "f3"):
                    expected = round(terminal_flows[f"fcu_{floor}"], 3)
                    assert pipes[f"chw_branch_supply_{floor}"]["flow_m3_h"] == expected
                    assert pipes[f"chw_branch_return_{floor}"]["flow_m3_h"] == expected
            finally:
                runtime_client.post(f"/api/v1/realtime/simulate/stop/{project_id}")
    finally:
        client.delete(f"/api/v1/projects/{project_id}")


def test_branch_serves_only_its_own_terminal():
    """支管的归属必须顺着水流方向定，不能把兄弟支管算成服务同一台末端。

    无向遍历会从 fcu_f1 上行到立管再拐进 branch_supply_f2，让每根支管都显示
    服务全部末端、流量翻三倍。
    """
    project_id, _ = _create_four_floor_project("pytest_pipes_branch")
    try:
        with TestClient(app) as runtime_client:
            try:
                runtime_client.post(
                    f"/api/v1/realtime/simulate/start/{project_id}",
                    json={"mode": "cooling", "interval_seconds": 10.0},
                )
                pipes = _by_id(
                    runtime_client.get(f"/api/v1/realtime/pipes/{project_id}").json()
                )

                for floor in ("f1", "f2", "f3"):
                    assert pipes[f"chw_branch_supply_{floor}"][
                        "served_terminal_ids"
                    ] == [f"fcu_{floor}"]
                    assert pipes[f"chw_branch_return_{floor}"][
                        "served_terminal_ids"
                    ] == [f"fcu_{floor}"]
                assert pipes["chw_main_supply_b1"]["served_terminal_ids"] == [
                    "fcu_f1",
                    "fcu_f2",
                    "fcu_f3",
                ]
                # 冷却水没有末端，服务列表为空但流量照常。
                assert pipes["cw_main_supply_b1"]["served_terminal_ids"] == []
                assert pipes["cw_main_supply_b1"]["flow_m3_h"] > 0.0
            finally:
                runtime_client.post(f"/api/v1/realtime/simulate/stop/{project_id}")
    finally:
        client.delete(f"/api/v1/projects/{project_id}")


def test_pump_upstream_pipe_is_not_orphaned():
    """水泵上游的管必须算得到末端。

    热水侧是 `热源 -> hw_source_supply_riser -> 水泵 -> 主管`，遍历若在泵处止步，
    hw_source_supply_riser 会永远显示零流量。
    """
    project_id, _ = _create_four_floor_project("pytest_pipes_pump")
    try:
        with TestClient(app) as runtime_client:
            try:
                runtime_client.post(
                    f"/api/v1/realtime/simulate/start/{project_id}",
                    json={
                        "mode": "heating",
                        "interval_seconds": 10.0,
                        "initial_room_temp_c": 12.0,
                        "target_temp_c": 24.0,
                        "outdoor_base_temp_c": 2.0,
                    },
                )
                body = runtime_client.get(
                    f"/api/v1/realtime/pipes/{project_id}"
                ).json()
                pipes = _by_id(body)

                main_flow = pipes["hw_main_supply_b1"]["flow_m3_h"]
                assert main_flow > 0.0
                assert pipes["hw_source_supply_riser"]["flow_m3_h"] == main_flow
                assert pipes["hw_source_return_riser"]["flow_m3_h"] == main_flow
                assert pipes["hw_source_supply_riser"]["served_terminal_ids"] == [
                    "fcu_f1",
                    "fcu_f2",
                    "fcu_f3",
                ]
                # 制热模式下冷冻水侧不走水。
                assert pipes["chw_main_supply_b1"]["flow_m3_h"] == 0.0
            finally:
                runtime_client.post(f"/api/v1/realtime/simulate/stop/{project_id}")
    finally:
        client.delete(f"/api/v1/projects/{project_id}")


def test_system_totals_match_realtime_summary():
    """按系统汇总的流量必须与 realtime summary 对得上，否则两个端点自相矛盾。"""
    project_id, _ = _create_four_floor_project("pytest_pipes_totals")
    try:
        with TestClient(app) as runtime_client:
            try:
                runtime_client.post(
                    f"/api/v1/realtime/simulate/start/{project_id}",
                    json={"mode": "cooling", "interval_seconds": 10.0},
                )
                summary = runtime_client.get(
                    f"/api/v1/realtime/state/{project_id}"
                ).json()["summary"]
                body = runtime_client.get(
                    f"/api/v1/realtime/pipes/{project_id}"
                ).json()

                totals = body["total_flow_by_system_m3_h"]
                assert totals["chilled_water"] == summary["chilled_water_flow_m3_h"]
                assert totals["cooling_water"] == summary["cooling_water_flow_m3_h"]
                assert totals["hot_water"] == summary["hot_water_flow_m3_h"]

                flowing = [
                    pipe for pipe in body["pipes"] if pipe["flow_m3_h"] > 0.0
                ]
                fastest = max(flowing, key=lambda pipe: pipe["velocity_m_s"])
                assert body["most_loaded_pipe_id"] == fastest["pipe_id"]
            finally:
                runtime_client.post(f"/api/v1/realtime/simulate/stop/{project_id}")
    finally:
        client.delete(f"/api/v1/projects/{project_id}")


def test_velocity_and_pressure_drop_follow_flow():
    """同管径下流量越大流速越高、压降越大；零流量三项全零。"""
    project_id, _ = _create_four_floor_project("pytest_pipes_monotonic")
    try:
        with TestClient(app) as runtime_client:
            try:
                runtime_client.post(
                    f"/api/v1/realtime/simulate/start/{project_id}",
                    json={"mode": "cooling", "interval_seconds": 10.0},
                )
                body = runtime_client.get(
                    f"/api/v1/realtime/pipes/{project_id}"
                ).json()

                for pipe in body["pipes"]:
                    if pipe["flow_m3_h"] == 0.0:
                        assert pipe["velocity_m_s"] == 0.0
                        assert pipe["pressure_drop_pa"] == 0.0
                        assert pipe["head_m"] == 0.0
                    else:
                        assert pipe["velocity_m_s"] > 0.0
                        assert pipe["pressure_drop_pa"] > 0.0
                        assert pipe["head_m"] > 0.0

                # 主管与支管同属冷冻水但管径不同，直接比流速无意义；
                # 取同管径的三根支管，流量大的流速必须更高。
                branches = sorted(
                    (
                        pipe
                        for pipe in body["pipes"]
                        if pipe["pipe_id"].startswith("chw_branch_supply_")
                    ),
                    key=lambda pipe: pipe["flow_m3_h"],
                )
                assert len({pipe["diameter_mm"] for pipe in branches}) == 1
                velocities = [pipe["velocity_m_s"] for pipe in branches]
                assert velocities == sorted(velocities)
            finally:
                runtime_client.post(f"/api/v1/realtime/simulate/stop/{project_id}")
    finally:
        client.delete(f"/api/v1/projects/{project_id}")


def test_pipe_states_are_read_only():
    """只读投影：不建 session、不写项目、不追加历史、不留运行态。"""
    project_id, _ = _create_four_floor_project("pytest_pipes_readonly")
    try:
        project_before = client.get(f"/api/v1/projects/{project_id}").json()
        project_path = project_store.PROJECT_DIR / f"{project_id}.json"
        bytes_before = project_path.read_bytes()
        mtime_before = project_path.stat().st_mtime_ns

        assert client.get(f"/api/v1/realtime/pipes/{project_id}").status_code == 200

        assert client.get(f"/api/v1/projects/{project_id}").json() == project_before
        assert project_path.read_bytes() == bytes_before
        assert project_path.stat().st_mtime_ns == mtime_before
        assert (
            client.get(f"/api/v1/realtime/history/{project_id}").json()["points"] == []
        )
        assert not (realtime.HISTORY_DIR / f"{project_id}.json").exists()
        for store in (
            realtime._sessions,
            realtime._layouts,
            realtime._states,
            realtime._updated_at,
            realtime._alarms,
            realtime._summary,
            realtime._subscribers,
            realtime._sim_tasks,
            realtime._control_values,
            realtime._last_floor_states,
            realtime._last_control_state,
        ):
            assert project_id not in store
    finally:
        client.delete(f"/api/v1/projects/{project_id}")


def test_unknown_project_and_pipeless_project_contract():
    """未知项目 404；没有水管的项目返回 200 和空列表。"""
    missing = client.get("/api/v1/realtime/pipes/missing-project")
    assert missing.status_code == 404
    assert missing.json() == {"detail": "Project not found."}

    paths = client.get("/openapi.json").json()["paths"]
    assert paths["/api/v1/realtime/pipes/{project_id}"]["get"]["responses"]["404"][
        "description"
    ] == "Project not found."

    sample = client.get("/api/v1/project/sample/complete").json()
    sample["pipes"] = []
    sample["connections"] = []
    response = client.post(
        "/api/v1/projects", json={"name": "pipeless", "project": sample}
    )
    assert response.status_code == 200
    project_id = response.json()["project_id"]
    try:
        body = client.get(f"/api/v1/realtime/pipes/{project_id}").json()
        assert body == {
            "project_id": project_id,
            "running": False,
            "simcore_tick": 0,
            "pipes": [],
            "total_flow_by_system_m3_h": {},
            "most_loaded_pipe_id": None,
        }
    finally:
        client.delete(f"/api/v1/projects/{project_id}")
