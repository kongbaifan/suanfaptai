"""房间数组顺序不得影响任何计算结果。

旧实现里末端设备的 `served_room_ids` 缺省时会「按数组位置取第一个同楼层房间」，
于是重排 `rooms` 会换掉末端服务的房间，制冷能耗最大偏差 61%、室温偏差 1.5 °C，
而 8 个样例里 4 个恰好因为 room id 本来升序而看不出问题。绑定改为房间侧持有
`device_ids` 之后位置不再参与语义，这里用打乱顺序把这个性质钉死。
"""

from __future__ import annotations

import random

import pytest

from app.main import _evaluate
from app.samples import SAMPLES
from app.schemas import SystemEvaluateRequest
from app.steady_solver.solver import _normalized_solver_request

SAMPLE_NAMES = sorted(SAMPLES)


def _request(name: str) -> SystemEvaluateRequest:
    return SystemEvaluateRequest(**SAMPLES[name])


@pytest.mark.parametrize("name", SAMPLE_NAMES)
@pytest.mark.parametrize("seed", [0, 1, 2, 3])
def test_room_order_does_not_change_simulation(name: str, seed: int):
    request = _request(name)
    baseline = _evaluate(request)

    rooms = list(request.rooms)
    random.Random(seed).shuffle(rooms)
    shuffled = _evaluate(request.model_copy(update={"rooms": rooms}))

    assert _comparable(shuffled) == _comparable(baseline)


@pytest.mark.parametrize("name", SAMPLE_NAMES)
def test_both_simulation_blocks_in_one_response_agree(name: str):
    """`simulation` 与 `steady_network.equipment.simulation` 必须是同一个答案。

    两者分别由 `main._evaluate` 和 `steady_solver.solver` 求出，后者对输入做过排序；
    只要绑定不依赖数组位置，两条路径就不可能分叉。
    """
    result = _evaluate(_request(name))
    balance = result.steady_network.equipment if result.steady_network else None

    if result.simulation is None or balance is None:
        pytest.skip("sample produces no steady-state simulation block")
    assert balance.simulation.model_dump() == result.simulation.model_dump()


@pytest.mark.parametrize("name", SAMPLE_NAMES)
def test_solver_normalization_is_a_no_op_for_simulation(name: str):
    """求解器边界的排序不应改变物理结果，只应改变集合的呈现顺序。"""
    request = _request(name)
    normalized = _normalized_solver_request(request)

    assert _comparable(_evaluate(normalized)) == _comparable(_evaluate(request))


def _comparable(result) -> dict:
    """取与房间顺序无关的标量结论。

    管段列表本身会随输入顺序重排（那是呈现顺序，不是物理），所以只比标量与
    按 code 汇总后的问题集合。
    """
    simulation = result.simulation.model_dump() if result.simulation else None
    load = (
        {
            "cooling": result.load.adjusted_cooling_kw,
            "heating": result.load.adjusted_heating_kw,
        }
        if result.load
        else None
    )
    hydraulic = (
        result.hydraulic_network.required_pump_head_m
        if result.hydraulic_network
        else None
    )
    air = (
        result.air_network.required_fan_pressure_pa if result.air_network else None
    )
    return {
        "simulation": simulation,
        "load": load,
        "required_pump_head_m": hydraulic,
        "required_fan_pressure_pa": air,
        "issue_codes": sorted({issue.code for issue in result.issues}),
    }
