"""实时管段读数

仿真核心的水力只到"每个末端一条等效支路"（`hydronics.HydronicBranch`），
没有管段这一层。本模块把末端的实时水流量沿项目拓扑向上累加，得到每根管的
瞬时流量，再用静态水力那套 `calculate_water_from_flow` 换算流速与压降。

**口径限制**：这是按流量反算，不是压力平衡求解。管段流量完全信任 simcore 的
支路分流结果，本模块只做汇总与换算。因此它看不出"管径过小形成压力壁垒把流量
挤到别处"——那种情况只会表现为该管流速偏高。教学上足够（流速红了就是管径不够），
但不能替代 `hydraulic-network` 的设计校核，更不是工程平差。

与 `calculators/hydraulic_network.py` 的分工：后者按设计负荷算一次，是选型依据；
本模块跟着仿真 tick 走，反映当前阀位与转速下的实际水量分布。
"""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Mapping

from app.calculators.water import (
    GRAVITY_M_S2,
    WATER_DENSITY_KG_M3,
    calculate_water_from_flow,
)
from app.schemas import PipeInput, SystemEvaluateRequest


WATER_SYSTEMS: tuple[str, ...] = ("chilled_water", "cooling_water", "hot_water")

# 冷却水没有末端：冷机与冷却塔串在同一环上，每根管都过全部环路流量。
_LOOP_ONLY_SYSTEMS: frozenset[str] = frozenset({"cooling_water"})


@dataclass(frozen=True, slots=True)
class PipeFlowReading:
    """一根管在当前 tick 的读数"""

    pipe_id: str
    system: str
    pipe_type: str
    floor_id: str | None
    flow_m3_h: float
    velocity_m_s: float
    pressure_drop_pa: float
    head_m: float
    diameter_mm: float | None
    # 该管服务的末端。空元组表示拓扑上够不到任何末端，流量按 0 计。
    served_terminal_ids: tuple[str, ...]


def pipe_flow_readings(
    project: SystemEvaluateRequest,
    terminal_flows_by_system: Mapping[str, Mapping[str, float]],
    loop_flows_m3_h: Mapping[str, float],
    pass_through_device_ids: frozenset[str] = frozenset(),
) -> tuple[PipeFlowReading, ...]:
    """把末端实时水量摊到每根水管上

    `terminal_flows_by_system` 为 `{system: {terminal_id: flow_m3_h}}`：冷冻水与
    热水各自的末端水量由 simcore 分流得到。`loop_flows_m3_h` 为 `{system: flow}`，
    供冷却水这类无末端的单环路系统使用。

    `pass_through_device_ids` 是流量穿过而非终止的设备，实际就是水泵：管路常被
    水泵断成两段（如 `热源 -> 立管 -> 水泵 -> 主管`），不穿过泵的话泵上游那段
    管会算不到任何末端。
    """
    readings: list[PipeFlowReading] = []
    for system in WATER_SYSTEMS:
        pipes = [pipe for pipe in project.pipes if pipe.system == system]
        if not pipes:
            continue
        if system in _LOOP_ONLY_SYSTEMS:
            flow = max(loop_flows_m3_h.get(system, 0.0), 0.0)
            flows = {pipe.id: flow for pipe in pipes}
            served: dict[str, tuple[str, ...]] = {pipe.id: () for pipe in pipes}
        else:
            terminal_flows = terminal_flows_by_system.get(system, {})
            served = _served_terminals(
                project, system, frozenset(terminal_flows), pass_through_device_ids
            )
            flows = {
                pipe.id: sum(
                    terminal_flows.get(terminal_id, 0.0)
                    for terminal_id in served[pipe.id]
                )
                for pipe in pipes
            }
        for pipe in pipes:
            readings.append(
                _reading_for_pipe(pipe, flows.get(pipe.id, 0.0), served[pipe.id])
            )
    return tuple(readings)


def _reading_for_pipe(
    pipe: PipeInput,
    flow_m3_h: float,
    served_terminal_ids: tuple[str, ...],
) -> PipeFlowReading:
    """单管换算。零流量直接短路，否则 calculate_water_from_flow 会在 0 上选最小管径。"""
    if flow_m3_h <= 0.0:
        return PipeFlowReading(
            pipe_id=pipe.id,
            system=pipe.system,
            pipe_type=pipe.pipe_type,
            floor_id=pipe.floor_id,
            flow_m3_h=0.0,
            velocity_m_s=0.0,
            pressure_drop_pa=0.0,
            head_m=0.0,
            diameter_mm=pipe.diameter_mm,
            served_terminal_ids=served_terminal_ids,
        )
    result = calculate_water_from_flow(
        flow_m3_h=flow_m3_h,
        length_m=pipe.length_m,
        local_resistance_coefficient=pipe.local_resistance_coefficient,
        diameter_mm=pipe.diameter_mm,
        system=pipe.system,
    )
    head_m = result.pressure_drop_pa / (WATER_DENSITY_KG_M3 * GRAVITY_M_S2)
    return PipeFlowReading(
        pipe_id=pipe.id,
        system=pipe.system,
        pipe_type=pipe.pipe_type,
        floor_id=pipe.floor_id,
        flow_m3_h=round(flow_m3_h, 3),
        velocity_m_s=result.actual_velocity_m_s,
        pressure_drop_pa=result.pressure_drop_pa,
        head_m=round(head_m, 3),
        diameter_mm=pipe.diameter_mm,
        served_terminal_ids=served_terminal_ids,
    )


def _served_terminals(
    project: SystemEvaluateRequest,
    system: str,
    terminal_ids: frozenset[str],
    pass_through_device_ids: frozenset[str],
) -> dict[str, tuple[str, ...]]:
    """每根管服务哪些末端

    连接是有向的（`from_id -> to_id`），方向即水流方向，必须照着走：供水管服务它
    **下游**的末端，回水管服务它**上游**的末端。用无向遍历会从末端上行到立管后
    拐回兄弟支管，把 `branch_supply_f2` 也算成服务 `fcu_f1`。

    两个方向合并取并集，因此不依赖 `pipe_type`——`unknown` 类型的管一样能定位，
    供水管的上游只会走到冷热源（不是末端），回水管的下游同理。
    """
    downstream = _adjacency(project, system, reverse=False)
    upstream = _adjacency(project, system, reverse=True)
    barriers = _barrier_device_ids(project, terminal_ids, pass_through_device_ids)
    served: dict[str, set[str]] = defaultdict(set)
    for pipe in project.pipes:
        if pipe.system != system:
            continue
        reachable = _reachable_terminals(
            downstream, pipe.id, terminal_ids, barriers
        ) | _reachable_terminals(upstream, pipe.id, terminal_ids, barriers)
        served[pipe.id] = reachable
    return {
        pipe.id: tuple(sorted(served.get(pipe.id, set())))
        for pipe in project.pipes
        if pipe.system == system
    }


def _barrier_device_ids(
    project: SystemEvaluateRequest,
    terminal_ids: frozenset[str],
    pass_through_device_ids: frozenset[str],
) -> frozenset[str]:
    """遍历遇到就停下的设备：冷热源、冷却塔等流量的起止点。

    末端本身也是终点，但要先被记下来，所以不列入这里。水泵是通路，也不列入。
    """
    return frozenset(
        device.id
        for device in project.devices
        if device.id not in terminal_ids and device.id not in pass_through_device_ids
    )


def _reachable_terminals(
    adjacency: Mapping[str, list[str]],
    start: str,
    terminal_ids: frozenset[str],
    barriers: frozenset[str],
) -> set[str]:
    """沿单一方向走，收集能到达的末端。碰到末端或屏障设备就不再往下。"""
    visited = {start}
    found: set[str] = set()
    queue: deque[str] = deque([start])
    while queue:
        node = queue.popleft()
        for neighbor in adjacency.get(node, ()):
            if neighbor in visited:
                continue
            visited.add(neighbor)
            if neighbor in terminal_ids:
                found.add(neighbor)
                continue
            if neighbor in barriers:
                continue
            queue.append(neighbor)
    return found


def _adjacency(
    project: SystemEvaluateRequest, system: str, reverse: bool
) -> dict[str, list[str]]:
    known = {device.id for device in project.devices} | {
        pipe.id for pipe in project.pipes
    }
    graph: dict[str, list[str]] = defaultdict(list)
    for connection in project.connections:
        if connection.system != system:
            continue
        if connection.from_id not in known or connection.to_id not in known:
            continue
        if reverse:
            graph[connection.to_id].append(connection.from_id)
        else:
            graph[connection.from_id].append(connection.to_id)
    return graph
