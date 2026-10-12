"""静态水力分配的汇合点守恒

`topology._pipe_flow_allocation` 原先按管段类型各自取值、互不扣减：立管拿
`总流量 × 服务楼层权重和`、`branch_supply` 拿 `总流量 × 本层权重`。一层同时有
风盘支管和新风机组支管时，这层流量被计两次——`three_floor_twelve_room` 的
B1F 供水干管 28.473 m³/h 流出后分给立管 28.473 + 三根新风支管 3×9.491，
汇合点凭空多出一倍。`terminal_branch` 又把同层供、回两根支管一起算进分母，
`multi_zone_all_air` 的唯一一根末端供水支管只拿到总流量的一半。

判据：把相互连接的管段端点并成汇合点，**没有接设备**的汇合点上
Σ(流入管段) 必须等于 Σ(流出管段)。接了设备的汇合点不查（冷热源、末端本身
是源/汇），只有分集水器例外——它就是汇合点本身，按设备进出比对。

反向验证：把 `_pipe_flow_allocation` 里的路由分支去掉，参数化用例中
four_floor / multi_zone_all_air / ducted / three_floor / 两个 manifold 失败。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.calculators.hydraulic_network import calculate_hydraulic_network
from app.data.equipment import role_for_device
from app.main import app
from app.samples import SAMPLES
from app.schemas import SystemEvaluateRequest

client = TestClient(app)
WATER_SYSTEMS = {"chilled_water", "cooling_water", "hot_water"}


def _junction_imbalances(request: SystemEvaluateRequest, flows: dict[str, float]) -> list[tuple[float, list]]:
    """返回 (流入 - 流出, 汇合点成员) 列表，只含纯管段汇合点。"""
    pipes = {pipe.id for pipe in request.pipes if pipe.system in WATER_SYSTEMS}
    parent: dict[tuple[str, str], tuple[str, str]] = {}

    def find(node: tuple[str, str]) -> tuple[str, str]:
        parent.setdefault(node, node)
        while parent[node] != node:
            parent[node] = parent[parent[node]]
            node = parent[node]
        return node

    device_attached: set[tuple[str, str]] = set()
    for connection in request.connections:
        from_pipe, to_pipe = connection.from_id in pipes, connection.to_id in pipes
        if from_pipe and to_pipe:
            parent[find((connection.from_id, "out"))] = find((connection.to_id, "in"))
        elif from_pipe:
            device_attached.add((connection.from_id, "out"))
        elif to_pipe:
            device_attached.add((connection.to_id, "in"))

    groups: dict[tuple[str, str], list[tuple[str, str]]] = {}
    for pipe_id in pipes:
        for end in ("in", "out"):
            groups.setdefault(find((pipe_id, end)), []).append((pipe_id, end))

    imbalances = []
    for members in groups.values():
        if len(members) < 2 or any(member in device_attached for member in members):
            continue
        inflow = sum(flows[pipe_id] for pipe_id, end in members if end == "out")
        outflow = sum(flows[pipe_id] for pipe_id, end in members if end == "in")
        imbalances.append((inflow - outflow, sorted(members)))

    # 分集水器这类穿过式设备本身就是汇合点：两侧都只接管段时，进出也必须相等。
    for device in request.devices:
        if role_for_device(device) != "water_accessory":
            continue
        incoming = [c.from_id for c in request.connections if c.to_id == device.id]
        outgoing = [c.to_id for c in request.connections if c.from_id == device.id]
        if not incoming or not outgoing or not all(node in pipes for node in incoming + outgoing):
            continue
        delta = sum(flows[p] for p in incoming) - sum(flows[p] for p in outgoing)
        imbalances.append((delta, [(device.id, "device")]))
    return imbalances


def _evaluate(sample: dict) -> dict:
    response = client.post("/api/v1/system/evaluate", json=sample)
    assert response.status_code == 200
    return response.json()


def _pipe_flows(body: dict) -> dict[str, float]:
    return {item["pipe_id"]: item["flow_m3_h"] for item in body["hydraulic_network"]["pipes"]}


@pytest.mark.parametrize("name", sorted(SAMPLES))
def test_every_pipe_junction_conserves_flow(name):
    sample = SAMPLES[name]
    body = _evaluate(sample)
    request = SystemEvaluateRequest.model_validate(sample)
    flows = _pipe_flows(body)

    # 管段流量取 3 位小数，一个汇合点最多十来根管，舍入误差 < 0.01。
    bad = [(round(delta, 3), members) for delta, members in _junction_imbalances(request, flows) if abs(delta) > 0.01]
    assert bad == [], f"{name} 汇合点不守恒：{bad}"


def test_three_floor_riser_and_fresh_air_branches_split_the_main():
    """干管 = 立管 + 三根新风支管，立管 = 三根风盘支管（原先立管与干管相等）。"""
    body = _evaluate(SAMPLES["three_floor_twelve_room"])
    flows = _pipe_flows(body)
    total = body["hydraulic_network"]["total_flow_by_system_m3_h"]["chilled_water"]

    assert flows["chw_main_supply_b1"] == pytest.approx(total, abs=1e-3)
    assert flows["chw_main_return_b1"] == pytest.approx(total, abs=1e-3)
    fcu_branches = sum(flows[f"chw_branch_supply_f{n}"] for n in (1, 2, 3))
    fau_branches = sum(flows[f"chw_fau_supply_f{n}"] for n in (1, 2, 3))
    assert flows["chw_supply_riser"] == pytest.approx(fcu_branches, abs=0.01)
    assert flows["chw_supply_riser"] + fau_branches == pytest.approx(total, abs=0.01)
    # 三层房间完全相同，三根新风支管必须相等且不为 0。
    assert flows["chw_fau_supply_f1"] > 0
    assert flows["chw_fau_supply_f1"] == pytest.approx(flows["chw_fau_supply_f3"], abs=1e-3)


def test_single_terminal_branch_carries_the_whole_flow():
    """唯一一台组合式机组的供、回支管都应带全部流量（原先按供回两根均分成一半）。"""
    body = _evaluate(SAMPLES["multi_zone_all_air"])
    flows = _pipe_flows(body)
    total = body["hydraulic_network"]["total_flow_by_system_m3_h"]

    for system, prefix in (("chilled_water", "chw"), ("hot_water", "hw")):
        assert flows[f"{prefix}_terminal_supply"] == pytest.approx(total[system], abs=1e-3)
        assert flows[f"{prefix}_terminal_return"] == pytest.approx(total[system], abs=1e-3)


def _hand_calc_payload() -> dict:
    # 一个房间同时由风盘与新风机组服务；两台末端显式声明流量 3 : 1，
    # 与目录无关，便于手算。
    return {
        "floors": [{"id": "F1", "level": 1}],
        "rooms": [{"id": "r1", "floor_id": "F1", "area_m2": 100, "people": 10, "device_ids": ["fcu", "fau"]}],
        "devices": [
            {"id": "chiller", "type": "冷源机组", "name": "SCWE-90H+B", "floor_id": "F1"},
            {"id": "pump", "type": "循环泵", "name": "EC80", "floor_id": "F1"},
            {"id": "fcu", "type": "风机盘管", "name": "FP-85WA", "floor_id": "F1", "flow_m3_h": 3.0},
            {"id": "fau", "type": "新风机组", "name": "MKS02D4Y/C", "floor_id": "F1", "flow_m3_h": 1.0},
        ],
        "pipes": [
            {"id": "main_s", "system": "chilled_water", "pipe_type": "main_supply", "floor_id": "F1", "length_m": 10, "diameter_mm": 65},
            {"id": "fcu_s", "system": "chilled_water", "pipe_type": "branch_supply", "floor_id": "F1", "length_m": 5, "diameter_mm": 50},
            {"id": "fau_s", "system": "chilled_water", "pipe_type": "branch_supply", "floor_id": "F1", "length_m": 5, "diameter_mm": 50},
            {"id": "fcu_r", "system": "chilled_water", "pipe_type": "branch_return", "floor_id": "F1", "length_m": 5, "diameter_mm": 50},
            {"id": "fau_r", "system": "chilled_water", "pipe_type": "branch_return", "floor_id": "F1", "length_m": 5, "diameter_mm": 50},
            {"id": "main_r", "system": "chilled_water", "pipe_type": "main_return", "floor_id": "F1", "length_m": 10, "diameter_mm": 65},
        ],
        "connections": [
            {"from_id": "chiller", "to_id": "pump", "system": "chilled_water"},
            {"from_id": "pump", "to_id": "main_s", "system": "chilled_water"},
            {"from_id": "main_s", "to_id": "fcu_s", "system": "chilled_water"},
            {"from_id": "main_s", "to_id": "fau_s", "system": "chilled_water"},
            {"from_id": "fcu_s", "to_id": "fcu", "system": "chilled_water"},
            {"from_id": "fau_s", "to_id": "fau", "system": "chilled_water"},
            {"from_id": "fcu", "to_id": "fcu_r", "system": "chilled_water"},
            {"from_id": "fau", "to_id": "fau_r", "system": "chilled_water"},
            {"from_id": "fcu_r", "to_id": "main_r", "system": "chilled_water"},
            {"from_id": "fau_r", "to_id": "main_r", "system": "chilled_water"},
            {"from_id": "main_r", "to_id": "chiller", "system": "chilled_water"},
        ],
    }


def test_hand_calc_branches_split_by_terminal_rated_flow():
    """手算：100 kW、ΔT=5 K → 0.86×100/5 = 17.2 m³/h；末端额定 3:1 → 12.9 / 4.3。"""
    result = calculate_hydraulic_network(SystemEvaluateRequest(**_hand_calc_payload()), cooling_load_kw=100)
    flows = {item.pipe_id: item.flow_m3_h for item in result.pipes}

    assert flows["main_s"] == pytest.approx(17.2, abs=1e-3)
    assert flows["main_r"] == pytest.approx(17.2, abs=1e-3)
    assert flows["fcu_s"] == pytest.approx(12.9, abs=1e-3)
    assert flows["fcu_r"] == pytest.approx(12.9, abs=1e-3)
    assert flows["fau_s"] == pytest.approx(4.3, abs=1e-3)
    assert flows["fau_r"] == pytest.approx(4.3, abs=1e-3)


def test_explicit_pipe_flow_is_kept_as_declared():
    """用户在管段上写了流量就照用，路由只填没写的管段。"""
    payload = _hand_calc_payload()
    payload["pipes"][1]["flow_m3_h"] = 10.0
    result = calculate_hydraulic_network(SystemEvaluateRequest(**payload), cooling_load_kw=100)
    flows = {item.pipe_id: item.flow_m3_h for item in result.pipes}

    assert flows["fcu_s"] == pytest.approx(10.0, abs=1e-3)
    assert flows["fau_s"] == pytest.approx(4.3, abs=1e-3)


def test_blank_models_still_route_by_type_role():
    """型号留空时目录角色为 None，路由要退到类型角色表，否则整条环路被误判成环。"""
    payload = _hand_calc_payload()
    for device in payload["devices"]:
        device.pop("name")
    request = SystemEvaluateRequest(**payload)
    result = calculate_hydraulic_network(request, cooling_load_kw=100)
    flows = {item.pipe_id: item.flow_m3_h for item in result.pipes}

    assert flows["fcu_s"] == pytest.approx(12.9, abs=1e-3)
    assert flows["fau_s"] == pytest.approx(4.3, abs=1e-3)
    assert "PIPE_FLOW_ROUTING_FALLBACK" not in {issue.code for issue in result.issues}


def test_ring_without_source_falls_back_with_a_warning():
    """管段首尾相接成环、环上不经过冷热源：无从定序，退回按类型估算并显式告警。"""
    payload = _hand_calc_payload()
    payload["connections"].append({"from_id": "fcu_s", "to_id": "main_s", "system": "chilled_water"})
    result = calculate_hydraulic_network(SystemEvaluateRequest(**payload), cooling_load_kw=100)

    fallback = [issue for issue in result.issues if issue.code == "PIPE_FLOW_ROUTING_FALLBACK"]
    assert len(fallback) == 1
    assert fallback[0].level == "warning"
    assert fallback[0].target == "chilled_water"


def test_unconnected_pipes_keep_the_typed_estimate():
    """没有接线时无从路由，仍按管段类型估算（向后兼容）。"""
    payload = _hand_calc_payload()
    payload["connections"] = []
    result = calculate_hydraulic_network(SystemEvaluateRequest(**payload), cooling_load_kw=100)
    flows = {item.pipe_id: item.flow_m3_h for item in result.pipes}

    assert flows["main_s"] == pytest.approx(17.2, abs=1e-3)
    assert flows["fcu_s"] == pytest.approx(17.2, abs=1e-3)  # 单层权重 1.0
