"""端口布局解析：型号无别名时也必须按设备类型给出正确端口

历史缺陷：``_enumerate_device_ports`` 在型号没有 Unity 侧别名时，会跳过按
设备类型解析角色这一步，直接退回"2 个通用双向口"。组合式空调机组（目录里有
9 款型号、但都没有别名）因此把冷冻水供回、热水供回、送风回风共 6 条连接全部
挤在同一个端口上，触发 ``conservation_violation``——四层样例改造时被这个缺陷
挡住，排查了多轮。

目录查询接口 ``/api/v1/devices/ports`` 走的是另一条路径（``get_device_role``），
两条路径必须给出一致的端口布局。
"""

import pytest
from fastapi.testclient import TestClient

from app.data.equipment import load_equipment_catalog
from app.schemas import DeviceInput, SystemEvaluateRequest
from app.steady_solver.port_graph import (
    _role_port_entries,
    build_port_graph,
    query_device_port_layout,
)


def _first_model(device_type: str) -> str | None:
    """该类型在目录里的第一个正式型号名（无型号类型返回 None）。"""
    rows = sorted(
        str(row.get("name"))
        for row in load_equipment_catalog()
        if row.get("type") == device_type
        and not row.get("placement_only")
        and str(row.get("name")) not in {"", "*"}
    )
    return rows[0] if rows else None


def _sample_request() -> SystemEvaluateRequest:
    from copy import deepcopy

    from app.samples import FOUR_FLOOR_FCU_FRESH_AIR_SAMPLE

    return SystemEvaluateRequest(**deepcopy(FOUR_FLOOR_FCU_FRESH_AIR_SAMPLE))


def test_fresh_air_unit_without_alias_exposes_water_and_air_ports():
    """新风机组必须把冷冻水与风侧分开成独立端口

    新风机组是冷冻水盘管处理 100% 室外新风，端口为冷冻水2 + 风侧2。
    此前该角色只定义了 2 个风口，机组无法接水、冷量恒为 0。
    """
    graph = build_port_graph(_sample_request())
    for floor in ("f1", "f2", "f3"):
        media = [port.medium for port in graph.device_ports[f"fau_{floor}"]]
        assert media.count("chilled_water") == 2
        assert media.count("air") == 2
        # 未连热水时条件性热水口不展开
        assert media.count("hot_water") == 0


def test_fresh_air_unit_ports_are_not_all_collapsed_onto_one_port():
    """每条连接必须落在自己的端口上，不能全部挤到同一个端口"""
    graph = build_port_graph(_sample_request())
    ports = graph.device_ports["fau_f1"]
    for index in range(len(ports)):
        key = ("fau_f1", index)
        # 任一端口都不应同时挂满水侧与风侧的所有连接
        assert len(graph.adjacency.get(key, [])) <= 2


def test_project_port_graph_matches_catalog_port_query():
    """项目端口枚举与目录端口查询必须给出同构布局"""
    catalog = query_device_port_layout("新风机组", "MKS02D4Y/C")
    assert catalog is not None
    catalog_shape = [
        (entry.direction, entry.medium, entry.conditional)
        for entry in catalog.entries
    ]
    role_shape = [
        (entry.direction, entry.medium, entry.conditional)
        for entry in _role_port_entries("fresh_air_unit")
    ]
    assert catalog_shape == role_shape

    graph = build_port_graph(_sample_request())
    project_shape = [
        (port.direction, port.medium) for port in graph.device_ports["fau_f1"]
    ]
    # 样例未给新风机组接热水，条件性热水口不展开：冷冻水2 + 风侧2
    non_conditional = [
        (direction, medium)
        for direction, medium, conditional in role_shape
        if not conditional
    ]
    assert project_shape == non_conditional


@pytest.mark.parametrize(
    "device_type",
    ["冷源机组", "循环泵", "冷却塔", "风机盘管", "组合式空调机组", "新风机组",
     "空气源热泵", "水源热泵", "地源热泵"],
)
def test_port_layout_does_not_depend_on_the_model_being_given(device_type: str):
    """端口布局不得因为"没填型号"而变化

    `_enumerate_device_ports` 原先的顺序是「按类型名子串启发式 → 权威角色表」。
    型号留空（UI 明确提供「留空（不指定型号）」选项）时 Unity 侧型号规格为空，
    于是走启发式而不是 `get_device_role`，而启发式有两个偏差：
      * 热泵类被 `"泵"` 子串命中判成 pump，给出 2 个**无介质双向**口；
      * 风机盘管分支缺条件性热水口。
    后果：热水侧连接无处落地，`four_floor_fcu_fresh_air` 的清空型号项目报出
    8 条 error 级 `conservation_violation`（实测）。现在权威角色表优先，
    两条输入给出同一布局。
    """
    with_model = build_port_graph(
        SystemEvaluateRequest(
            devices=[DeviceInput(id="dev", type=device_type, name=_first_model(device_type))],
            floors=[], rooms=[], pipes=[], connections=[],
        )
    ).device_ports["dev"]
    without_model = build_port_graph(
        SystemEvaluateRequest(
            devices=[DeviceInput(id="dev", type=device_type, name=None)],
            floors=[], rooms=[], pipes=[], connections=[],
        )
    ).device_ports["dev"]

    def shape(ports):
        return [(port.direction, port.medium) for port in ports]

    assert shape(with_model) == shape(without_model), (
        f"{device_type} 的端口布局随型号是否填写而变：\n"
        f"  有型号 {shape(with_model)}\n  无型号 {shape(without_model)}"
    )
    # 反向：不得退化成「2 个 bidirectional + 无介质」这一通用兜底形状。
    # 注意泵类**合法**地为 (return,None)/(supply,None)（介质待连接推断），
    # 判据要认准 bidirectional 这个兜底特征，不能只查 medium is None。
    degraded = all(p.direction == "bidirectional" and p.medium is None for p in without_model)
    assert not degraded, f"{device_type} 无型号时退化成了通用兜底端口：{shape(without_model)}"


def test_blank_model_fcu_keeps_its_hot_water_ports():
    """风机盘管留空型号后，热水口仍须存在

    这是本次修复的核心：启发式的 fcu 分支只给 4 个口（无热水），
    导致热水支管连接无处落地。`four_floor_fcu_fresh_air` 因此一次报出
    8 条 error 级 `conservation_violation`，其中 6 条落在风机盘管所在的热水支管。
    """
    from copy import deepcopy

    from app.samples import FOUR_FLOOR_FCU_FRESH_AIR_SAMPLE

    payload = deepcopy(FOUR_FLOOR_FCU_FRESH_AIR_SAMPLE)
    for device in payload["devices"]:
        if device["type"] == "风机盘管":
            device["name"] = None

    graph = build_port_graph(SystemEvaluateRequest(**payload))
    for floor in ("f1", "f2", "f3"):
        media = [port.medium for port in graph.device_ports[f"fcu_{floor}"]]
        assert media.count("hot_water") == 2, (
            f"fcu_{floor} 留空型号后热水口丢失：{media}"
        )


def test_blank_model_in_heating_loop_keeps_flow_conservation():
    """型号留空不得再制造守恒违反

    范围说明：清空**全部**型号时，`four_floor_fcu_fresh_air` 仍有 2 条
    `hw_supply_riser` / `hw_return_riser` 的守恒违反，但**与本缺陷无关**——
    空气源热泵在无型号时解析为 `type_default`（947.6 kW / 162.9 m³/h，目录真实
    型号 SHAF-80H 为 264.2 kW / 45.4 m³/h，差 3.6 倍），热水环路的需求与降级流量
    不匹配。实测把型号**写错**成不存在的型号得到完全相同的 2 条——即参数降级的
    后果，不是端口布局问题（端口已确认有/无型号完全一致）。

    因此本测试钉住的是端口侧可修的那部分：清空**末端**（风机盘管）型号时，
    不得出现任何守恒违反。
    """
    from copy import deepcopy

    from app.main import app
    from app.samples import FOUR_FLOOR_FCU_FRESH_AIR_SAMPLE

    payload = deepcopy(FOUR_FLOOR_FCU_FRESH_AIR_SAMPLE)
    for device in payload["devices"]:
        if device["type"] == "风机盘管":
            device["name"] = None

    with TestClient(app) as client:
        body = client.post("/api/v1/system/evaluate", json=payload).json()

    violations = [i for i in body["issues"] if i["code"] == "conservation_violation"]
    assert not violations, (
        "末端型号留空仍报守恒违反："
        + "; ".join(f"{i.get('target')}: {i['message']}" for i in violations)
    )
