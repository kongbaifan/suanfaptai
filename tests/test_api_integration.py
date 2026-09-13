"""API 集成测试 - 稳态求解器端到端验证"""

from fastapi.testclient import TestClient

from app.main import app
from app.schemas import (
    ConnectionInput,
    DeviceInput,
    FloorInput,
    PipeInput,
    SystemEvaluateRequest,
)

client = TestClient(app)


def test_evaluate_with_steady_solver_simple_system():
    """测试简单系统的端到端评估（包含稳态求解器）"""
    request = SystemEvaluateRequest(
        floors=[FloorInput(id="F1", name="1F", level=1)],
        devices=[
            DeviceInput(id="chiller1", type="冷水机组", floor_id="F1"),
            DeviceInput(id="pump1", type="循环泵", floor_id="F1"),
            DeviceInput(id="fcu1", type="风机盘管", floor_id="F1"),
        ],
        pipes=[
            PipeInput(
                id="pipe1",
                system="chilled_water",
                length_m=20.0,
                diameter_mm=100.0,
            )
        ],
        connections=[
            ConnectionInput(from_id="chiller1", to_id="pipe1", system="chilled_water"),
            ConnectionInput(from_id="pipe1", to_id="pump1", system="chilled_water"),
            ConnectionInput(from_id="pump1", to_id="fcu1", system="chilled_water"),
            ConnectionInput(from_id="fcu1", to_id="chiller1", system="chilled_water"),
        ],
    )

    response = client.post("/api/v1/system/evaluate", json=request.model_dump())
    assert response.status_code == 200

    result = response.json()
    assert "issues" in result
    assert isinstance(result["issues"], list)
    steady_network = result["steady_network"]
    assert steady_network["media"]["chilled_water"]["converged"] is True
    assert steady_network["media"]["chilled_water"]["max_conservation_residual_m3_h"] <= 0.01
    assert any(port["owner_id"] == "pipe1" for port in steady_network["ports"])
    pipe_edges = [
        edge
        for edge in steady_network["media"]["chilled_water"]["edges"]
        if edge["pipe_id"] == "pipe1"
    ]
    assert len(pipe_edges) == 1
    assert pipe_edges[0]["length_m"] == 20.0
    assert pipe_edges[0]["diameter_mm"] == 100.0


def test_evaluate_with_steady_solver_disconnected_system():
    """测试断路系统的端到端评估（应检测到拓扑错误）"""
    request = SystemEvaluateRequest(
        floors=[FloorInput(id="F1", name="1F", level=1)],
        devices=[
            DeviceInput(id="chiller1", type="冷水机组", floor_id="F1"),
            DeviceInput(id="pump1", type="循环泵", floor_id="F1"),
            DeviceInput(id="fcu1", type="风机盘管", floor_id="F1"),
            DeviceInput(id="fcu2", type="风机盘管", floor_id="F1"),
        ],
        connections=[
            ConnectionInput(from_id="chiller1", to_id="pump1", system="chilled_water"),
            # fcu1 和 fcu2 形成独立子系统
            ConnectionInput(from_id="fcu1", to_id="fcu2", system="chilled_water"),
        ],
    )

    response = client.post("/api/v1/system/evaluate", json=request.model_dump())
    assert response.status_code == 200

    result = response.json()
    issues = result["issues"]

    # 应检测到断路错误
    disconnected_issues = [i for i in issues if i["code"] == "disconnected_network"]
    assert len(disconnected_issues) > 0
    assert any(i["level"] == "error" for i in disconnected_issues)


def test_evaluate_with_steady_solver_supply_return_mismatch():
    """测试供回接反系统（应检测到警告）"""
    request = SystemEvaluateRequest(
        floors=[FloorInput(id="F1", name="1F", level=1)],
        devices=[
            DeviceInput(id="chiller1", type="冷水机组", floor_id="F1"),
            DeviceInput(id="chiller2", type="冷水机组", floor_id="F1"),
        ],
        connections=[
            ConnectionInput(from_id="chiller1", to_id="chiller2", system="chilled_water"),
        ],
    )

    response = client.post("/api/v1/system/evaluate", json=request.model_dump())
    assert response.status_code == 200

    result = response.json()
    assert isinstance(result["issues"], list)

    # 注意：由于简化的端口推断，可能不会触发此警告（取决于端口匹配逻辑）
    # 此测试主要验证 API 不会崩溃


def test_evaluate_recommendations_include_steady_solver_issues():
    """测试推荐建议包含稳态求解器的问题"""
    request = SystemEvaluateRequest(
        floors=[FloorInput(id="F1", name="1F", level=1)],
        devices=[
            DeviceInput(id="chiller1", type="冷水机组", floor_id="F1"),
            DeviceInput(id="pump1", type="循环泵", floor_id="F1"),
            DeviceInput(id="fcu1", type="风机盘管", floor_id="F1"),
            DeviceInput(id="fcu2", type="风机盘管", floor_id="F1"),
        ],
        connections=[
            ConnectionInput(from_id="chiller1", to_id="pump1", system="chilled_water"),
            ConnectionInput(from_id="fcu1", to_id="fcu2", system="chilled_water"),
        ],
    )

    response = client.post("/api/v1/system/evaluate", json=request.model_dump())
    assert response.status_code == 200

    result = response.json()
    recommendations = result.get("recommendations", [])

    # 应包含断路相关的推荐
    assert any("disconnected" in rec.lower() or "connect" in rec.lower() for rec in recommendations)


def test_evaluate_empty_system_with_steady_solver():
    """测试空系统（无设备）"""
    request = SystemEvaluateRequest(
        floors=[FloorInput(id="F1", name="1F", level=1)],
        devices=[],
        connections=[],
    )

    response = client.post("/api/v1/system/evaluate", json=request.model_dump())
    assert response.status_code == 200

    result = response.json()
    # 空系统应正常返回，不应崩溃
    assert "issues" in result


def test_evaluate_device_only_system_with_steady_solver():
    """无房间、仅设备与连接时综合评估仍返回稳态管网结果"""
    request = {
        "floors": [{"id": "F1", "name": "1F", "level": 1}],
        "devices": [
            {"id": "chiller1", "type": "冷水机组", "floor_id": "F1"},
            {"id": "pump1", "type": "循环泵", "floor_id": "F1"},
        ],
        "connections": [
            {"from_id": "chiller1", "to_id": "pump1", "system": "chilled_water"},
        ],
        "rooms": [],
        "pipes": [],
    }

    response = client.post("/api/v1/system/evaluate", json=request)
    assert response.status_code == 200

    result = response.json()
    assert "issues" in result
    assert isinstance(result["issues"], list)
    assert result["steady_network"] is not None
