"""风口/风阀的端口布局：单连接点，不再产生结构性 isolated_port

回归背景
--------
`air_outlet` 与 `air_valve` 原先走 `_role_port_entries` 的默认分支，各拿到
**2 个双向端口**。但拓扑上两者都只是串联在风路/管路里的单个连接点：
`_find_matching_port` 永远返回第一个匹配口（不做占用检查），于是端口 0
吃掉全部边、端口 1 永远空置，每个风口/风阀白白报一条 `isolated_port`。

该警告本意是「接线漏了」。被这类结构性冗余淹没后，真正的漏接（例如
`bad` 样例冷机四个水口全空、`manifold_fcu` 的冷却水侧完全没接）就不显眼了。
实测：`four_floor_fcu_fresh_air` 3 条 → 0 条、`multi_zone_all_air`
6 条 → 0 条、`ducted_fresh_air` 4 条 → 0 条，而所有真实漏接一条未减。

本文件把「结构性冗余必须是 0」与「真实漏接必须继续报」两个方向都钉死。
"""

from collections import Counter

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.samples import SAMPLES
from app.schemas import SystemEvaluateRequest
from app.simcore.rated import resolve_rated_spec_by_type
from app.steady_solver.port_graph import build_port_graph

client = TestClient(app)

# 只在风口/风阀上成立：角色声明了几何上必然用不到的端口
SINGLE_PORT_ROLES = ("air_outlet", "air_valve")


def _isolated_ports(sample: dict) -> list[tuple[str, str, int]]:
    """返回 (设备id, 角色, 端口号) 形式的孤立端口列表。"""
    graph = build_port_graph(SystemEvaluateRequest(**sample))
    roles = {
        device["id"]: resolve_rated_spec_by_type(
            device.get("type", ""), device.get("name")
        ).role.value
        for device in sample["devices"]
    }
    return [
        (port.device_id, roles.get(port.device_id, "?"), port.port_index)
        for port in graph.ports
        if not graph.adjacency.get((port.device_id, port.port_index))
    ]


@pytest.mark.parametrize("role", SINGLE_PORT_ROLES)
def test_series_devices_declare_exactly_one_port(role: str):
    """风口与风阀在几何上只有一个连接点，必须只声明 1 个端口

    声明 2 个会让第 2 个永远空置并触发 isolated_port 噪音。
    """
    from app.steady_solver.port_graph import _role_port_entries

    entries = _role_port_entries(role)
    assert len(entries) == 1, (
        f"{role} 声明了 {len(entries)} 个端口；"
        "它串联在管路里，第二个端口永远用不到，会持续产生 isolated_port 噪音"
    )


@pytest.mark.parametrize(
    "sample_name",
    [
        "four_floor_fcu_fresh_air",
        "multi_zone_all_air",
        "ducted_fresh_air",
        "three_floor_twelve_room",
    ],
)
def test_wired_samples_have_no_isolated_ports(sample_name: str):
    """接线完整的样例不得出现任何孤立端口

    这三个样例的风口都接了进出两条边。改动前它们分别报 3 / 6 / 4 条
    isolated_port，全部来自风口的空置第二端口。
    """
    isolated = _isolated_ports(SAMPLES[sample_name])
    assert isolated == [], (
        f"{sample_name} 仍有孤立端口：{isolated}；"
        "接线完整的样例不该出现该警告"
    )


def test_air_outlet_uses_a_single_port_for_both_edges():
    """风口的两条边（进、出）挂在同一个端口上

    钉死「单连接点」这一语义：不是「只需接一条边」，而是同一个点既进又出。
    """
    graph = build_port_graph(SystemEvaluateRequest(**SAMPLES["ducted_fresh_air"]))
    outlet_ports = [
        port for port in graph.ports if port.device_id.startswith("supply_outlet_")
    ]
    assert outlet_ports, "样例里没有送风口，测试前置条件不成立"
    for port in outlet_ports:
        assert port.port_index == 0
        edges = graph.adjacency.get((port.device_id, port.port_index), [])
        assert len(edges) == 2, (
            f"{port.device_id} 端口 0 有 {len(edges)} 条边，期望 2（进+出）"
        )


def test_genuinely_unwired_devices_still_report_isolated_ports():
    """真实漏接必须继续报——降噪不能把真信号一起消掉

    `bad` 是故意接错的样例，`manifold_fcu` 的冷机冷却水侧完全没接。
    两者的孤立端口都来自**同介质端口全空**的真实漏接，不是结构性冗余。
    """
    bad = _isolated_ports(SAMPLES["bad"])
    assert bad, "bad 样例不再报孤立端口；降噪把真实漏接一起消掉了"
    assert all(role not in SINGLE_PORT_ROLES for _, role, _ in bad), (
        f"bad 样例的孤立端口仍含风口/风阀：{bad}"
    )
    assert {role for _, role, _ in bad} & {"chiller", "fcu", "cooling_tower"}

    manifold = _isolated_ports(SAMPLES["manifold_fcu"])
    assert any(
        role == "chiller" for _, role, _ in manifold
    ), "manifold_fcu 冷机的冷却水漏接未报出"


def test_all_samples_still_work_after_port_change():
    """端口数改变不得影响任何样例的评估结果可用性"""
    for name, sample in SAMPLES.items():
        response = client.post("/api/v1/system/evaluate", json=sample)
        assert response.status_code == 200, f"{name} 评估失败：{response.text[:200]}"


def test_isolated_port_counts_match_the_measured_baseline():
    """把各样例的 isolated_port 条数钉在实测值上

    这些数字全部来自真实漏接（角色同介质端口全空），改动端口布局不得减少。
    任何一条下降都说明有真信号被误消。
    """
    expected = {
        "complete": 2,
        "air": 1,
        "bad": 10,
        "multi_floor": 8,
        "manifold_fcu": 6,
        "manifold_fcu_dual_water": 6,
        "four_floor_fcu_fresh_air": 0,
        "multi_zone_all_air": 0,
        "ducted_fresh_air": 0,
        "three_floor_twelve_room": 0,
    }
    actual = {}
    for name, sample in SAMPLES.items():
        body = client.post("/api/v1/system/evaluate", json=sample).json()
        codes = Counter(issue["code"] for issue in body.get("issues", []))
        actual[name] = codes.get("isolated_port", 0)

    assert actual == expected, (
        f"isolated_port 条数偏离实测基线：\n"
        f"  期望 {expected}\n  实际 {actual}"
    )
