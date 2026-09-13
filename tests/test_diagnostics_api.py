import json

from fastapi.testclient import TestClient

from app import diagnostics, realtime
from app.main import app

client = TestClient(app)


def test_diagnostics_reports_service_environment_and_data_scale():
    body = client.get("/api/v1/diagnostics").json()

    assert body["status"] in {"ok", "error"}
    assert body["service"]["product"] == "HVACAlgorithmService"
    assert body["service"]["app_version"]
    assert body["service"]["python_version"]
    assert body["service"]["uptime_seconds"] >= 0.0
    assert body["data"]["catalog_items"] > 0
    assert body["data"]["knowledge_entries"] == 11
    assert body["data"]["catalog_types"] > 0


def test_diagnostics_lists_release_resource_checks():
    body = client.get("/api/v1/diagnostics").json()

    names = [check["name"] for check in body["checks"]]
    assert "release_manifest" in names
    assert any(name.startswith("resource:") for name in names)
    assert all({"name", "ok", "required", "detail"} <= set(check) for check in body["checks"])


def test_static_workbench_bundle_is_present_and_self_consistent():
    """工作台构建产物必须存在，且 index.html 引用的带哈希资源也必须存在

    这条测试守的是一个**启动即崩溃**的资源，此前没有任何测试覆盖它：

    `app/main.py` 用 `StaticFiles(directory=STATIC_DIR)` 挂载静态目录，而
    starlette 的 `StaticFiles.__init__` 默认 `check_dir=True`，目录不存在会在
    **导入期**直接抛 `RuntimeError`。也就是说 `app/static` 一旦缺失，
    `uvicorn app.main:app` 起不来，`/api/v1/diagnostics` 也没机会报告缺失——
    诊断接口无法自查这个资源。

    只断言"文件存在"还不够：vite 产物是带内容哈希的文件名，index.html 以字面
    文件名引用它们，而 release.py 的资源审计只调 `Path.exists()`（对目录），
    既不读 index.html 也不校验哈希。因此"index.html 在、被引用的 js/css 不在"
    这种半新半旧的产物能通过审计，却会让工作台白屏。本测试把两者一起钉死。
    """
    import re

    from app.main import STATIC_DIR

    index_path = STATIC_DIR / "index.html"
    assert index_path.is_file(), f"工作台入口缺失：{index_path}（会导致导入期 RuntimeError）"

    html = index_path.read_text(encoding="utf-8")
    # 抓取 /static/assets/xxx.js 与 /static/assets/xxx.css 两类引用
    referenced = re.findall(r"/static/(assets/[^\"']+)", html)
    assert referenced, "index.html 未引用任何构建资源，产物可能不完整"

    for relative in referenced:
        asset = STATIC_DIR / relative
        assert asset.is_file(), (
            f"index.html 引用了不存在的构建资源 {relative}；"
            "产物半新半旧，工作台会白屏。请重新执行 npm run build。"
        )


def test_diagnostics_status_is_error_when_a_required_check_fails(monkeypatch, tmp_path):
    """必需项失败必须让总体状态变红，否则诊断页会谎报正常。"""
    broken = tmp_path / "release_manifest.json"
    broken.write_text("{broken", encoding="utf-8")
    monkeypatch.setattr(
        "app.release.resource_path",
        lambda relative: broken if str(relative).endswith("release_manifest.json") else tmp_path / str(relative),
    )

    body = client.get("/api/v1/diagnostics").json()

    assert body["status"] == "error"
    assert any(not check["ok"] and check["required"] for check in body["checks"])


def test_diagnostics_reports_no_realtime_sessions_when_idle(monkeypatch):
    monkeypatch.setattr(realtime, "_sessions", {})
    monkeypatch.setattr(realtime, "_sim_tasks", {})
    monkeypatch.setattr(realtime, "_subscribers", {})

    body = client.get("/api/v1/diagnostics").json()["realtime"]

    assert body["active_sessions"] == 0
    assert body["running_simulations"] == 0
    assert body["websocket_subscribers"] == 0
    assert body["projects"] == []


def test_realtime_snapshot_includes_ws_only_projects_without_session(monkeypatch):
    """只连了 WebSocket、还没启动仿真的项目也必须可见，否则诊断页看不到挂着的连接。"""
    monkeypatch.setattr(realtime, "_sessions", {})
    monkeypatch.setattr(realtime, "_sim_tasks", {})
    monkeypatch.setattr(realtime, "_subscribers", {"pjt-ws": {object(), object()}})
    monkeypatch.setattr(realtime, "_updated_at", {"pjt-ws": "2026-08-24T10:00:00Z"})

    snapshot = realtime.diagnostics_snapshot()

    assert snapshot["active_sessions"] == 0
    assert snapshot["websocket_subscribers"] == 2
    assert snapshot["projects"] == [
        {
            "project_id": "pjt-ws",
            "tick": 0,
            "running": False,
            "subscribers": 2,
            "updated_at": "2026-08-24T10:00:00Z",
        }
    ]


def test_diagnostics_survives_unreadable_catalog(monkeypatch):
    """单项数据损坏时返回 -1 而不是 500，诊断页本身必须始终可达。"""
    def broken() -> list:
        raise OSError("catalog unreadable")

    monkeypatch.setattr(diagnostics, "load_equipment_catalog", broken)

    body = client.get("/api/v1/diagnostics")

    assert body.status_code == 200
    assert body.json()["data"]["catalog_items"] == -1


def test_diagnostics_does_not_create_sessions_or_touch_history(monkeypatch, tmp_path):
    # 历史目录必须指向 tmp_path：真实目录会被本地在跑的 uvicorn 持续写入，
    # 两次快照之间被外部进程改动会让断言随机失败。
    monkeypatch.setattr(realtime, "HISTORY_DIR", tmp_path / "realtime_history")
    before = _tree_snapshot()

    client.get("/api/v1/diagnostics")

    assert _tree_snapshot() == before
    assert realtime._sessions == {}
    assert realtime._sim_tasks == {}


def _tree_snapshot() -> dict[str, bytes]:
    root = realtime.HISTORY_DIR
    if not root.exists():
        return {}
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
    }


def test_bridge_endpoints_are_gone():
    assert client.get("/api/v1/bridge/status").status_code == 404
    assert client.get("/api/v1/bridge/logs").status_code == 404


def test_diagnostics_response_is_json_serializable():
    response = client.get("/api/v1/diagnostics")

    assert response.status_code == 200
    assert json.loads(response.text)["service"]["platform"]
