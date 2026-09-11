"""端口布局解析：型号无别名时也必须按设备类型给出正确端口

历史缺陷：``_enumerate_device_ports`` 在型号没有 Unity 侧别名时，会跳过按
设备类型解析角色这一步，直接退回"2 个通用双向口"。组合式空调机组（目录里有
9 款型号、但都没有别名）因此把冷冻水供回、热水供回、送风回风共 6 条连接全部
挤在同一个端口上，触发 ``conservation_violation``——四层样例改造时被这个缺陷
挡住，排查了多轮。

目录查询接口 ``/api/v1/devices/ports`` 走的是另一条路径（``get_device_role``），
两条路径必须给出一致的端口布局。
"""

from app.schemas import SystemEvaluateRequest
from app.steady_solver.port_graph import (
    _role_port_entries,
    build_port_graph,
    query_device_port_layout,
)


def _sample_request() -> SystemEvaluateRequest:
    from copy import deepcopy

    from app.samples import FOUR_FLOOR_FCU_FRESH_AIR_SAMPLE

    return SystemEvaluateRequest(**deepcopy(FOUR_FLOOR_FCU_FRESH_AIR_SAMPLE))


def test_ahu_without_alias_exposes_water_and_air_ports():
    """组合式空调机组必须把冷冻水与风侧分开成独立端口

    四层样例里新风机组只承担新风冷却（未接热水），故展开为
    冷冻水2 + 风侧2；关键是它们不能再挤进同一个通用端口。
    """
    graph = build_port_graph(_sample_request())
    for floor in ("f1", "f2", "f3"):
        media = [port.medium for port in graph.device_ports[f"ahu_{floor}"]]
        assert media.count("chilled_water") == 2
        assert media.count("air") == 2
        # 未连热水时条件性热水口不展开
        assert media.count("hot_water") == 0


def test_ahu_ports_are_not_all_collapsed_onto_one_port():
    """每条连接必须落在自己的端口上，不能全部挤到同一个端口"""
    graph = build_port_graph(_sample_request())
    ports = graph.device_ports["ahu_f1"]
    for index in range(len(ports)):
        key = ("ahu_f1", index)
        # 任一端口都不应同时挂满水侧与风侧的所有连接
        assert len(graph.adjacency.get(key, [])) <= 2


def test_project_port_graph_matches_catalog_port_query():
    """项目端口枚举与目录端口查询必须给出同构布局"""
    catalog = query_device_port_layout("组合式空调机组", "AHU-07-10-8排")
    assert catalog is not None
    catalog_shape = [
        (entry.direction, entry.medium, entry.conditional)
        for entry in catalog.entries
    ]
    role_shape = [
        (entry.direction, entry.medium, entry.conditional)
        for entry in _role_port_entries("ahu")
    ]
    assert catalog_shape == role_shape

    graph = build_port_graph(_sample_request())
    project_shape = [
        (port.direction, port.medium) for port in graph.device_ports["ahu_f1"]
    ]
    # 样例未给新风机组接热水，条件性热水口不展开：冷冻水2 + 风侧2
    non_conditional = [
        (direction, medium)
        for direction, medium, conditional in role_shape
        if not conditional
    ]
    assert project_shape == non_conditional
