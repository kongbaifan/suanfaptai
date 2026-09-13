"""盘管换热 ε-NTU 模型 - 回归测试

本文件与 `test_simcore_monotonic.py` 互补：那边只锁方向性关系，方向对了
但数值平台化（例如供水温整段不参与计算）依然能全绿。本文件**钉死具体数值**，
用于捕获这一类"单调但仍错误"的静默失效。

## 缺陷背景

旧模型 `capacity * sqrt(flow_ratio) * fan * clamp(dT/12, 0, 1)` 的温差项在
`dT >= 12 K` 时被钳到 1.0。而冷冻水系统实际温差是 19 K（室温 26、供水 7），
永远落在饱和区，供水温因此**完全不参与换热计算**：

    room=26C, Tw=4..12C  ->  cooling 恒为额定值 10.000 kW
    Tw=7C,   Tr=22..30C  ->  cooling 恒为额定值 10.000 kW

后果是改供水温设定值对房间温度几乎无影响，README 宣称的"供水温逐 Tick
趋近设定值"在末端侧不可观测，教学演示失效。制热侧同构（dT/20）。

## ε-NTU 模型

盘管按逆流换热器处理，效能 ε 由传热单元数 NTU 决定：

    C_air = V_air * rho * cp / 3600        [kW/K]
    C_w   = V_w * rho_w * cp_w / 3600      [kW/K]
    Cr    = C_min / C_max
    NTU   = UA / C_min
    ε     = (1 - exp(-NTU(1-Cr))) / (1 - Cr*exp(-NTU(1-Cr)))     (逆流)
    Q     = ε * C_min * (T_air_in - T_w_in)

UA 由目录额定工况反标定：每个型号在额定点的 Q 严格等于 `capacity_kw`，
因此本模型不改变设计工况下的选型结论，只修正偏离额定点时的响应。
"""

import itertools
import math

import pytest

from app.simcore.devices import (
    AIR_KW_PER_M3H_K,
    WATER_KW_PER_M3H_K,
    terminal_state,
)
from app.simcore.rated import resolve_rated_spec_by_type

FCU = "风机盘管"
FCU_MODEL = "MCW200V"

# 目录额定工况（由 catalog 反解，见 devices.py 的模块常量注释）
RATED_AIR_ENTERING_C = 27.0
RATED_WATER_ENTERING_C = 7.0


@pytest.fixture
def fcu():
    return resolve_rated_spec_by_type(FCU, FCU_MODEL)


def test_supply_water_temperature_materially_changes_capacity(fcu):
    """核心缺陷回归：供水温必须实质影响冷量

    旧模型下 4C 与 12C 供水给出完全相同的冷量（都是 10.000 kW），
    因为温差项在两端都被钳到 1.0。修复后低温供水必须显著更强。
    """
    cold = terminal_state(fcu, 1.0, RATED_AIR_ENTERING_C, 4.0)
    warm = terminal_state(fcu, 1.0, RATED_AIR_ENTERING_C, 12.0)

    assert cold.cooling_kw > warm.cooling_kw, "供水温升高必须降低冷量"
    # 4C 与 12C 的换热温差相差 8/19，冷量差异必须显著而非浮点噪声
    assert cold.cooling_kw > warm.cooling_kw * 1.15


def test_capacity_is_not_saturated_across_the_working_range(fcu):
    """工作区间内不得出现数值平台：相邻档位必须严格单调

    这是旧模型最本质的病症——平台期。逐档检查可同时捕获"整段钳死"
    与"部分钳死"，比只比较两个端点更严格。
    """
    supplies = [4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0, 11.0, 12.0]
    values = [
        terminal_state(fcu, 1.0, RATED_AIR_ENTERING_C, tw).cooling_kw
        for tw in supplies
    ]

    points = list(zip(supplies, values, strict=True))
    for (tw_cool, cooler), (tw_warm, warmer) in itertools.pairwise(points):
        assert warmer < cooler, (
            f"供水温 {tw_cool}C -> {tw_warm}C 冷量未下降："
            f"{cooler:.4f} -> {warmer:.4f}（疑似饱和平台）"
        )


def test_room_temperature_materially_changes_capacity(fcu):
    """室温也必须实质影响冷量（同一饱和缺陷的另一面）"""
    cool_room = terminal_state(fcu, 1.0, 22.0, 7.0)
    warm_room = terminal_state(fcu, 1.0, 30.0, 7.0)

    assert warm_room.cooling_kw > cool_room.cooling_kw * 1.5


def test_rated_point_reproduces_catalog_capacity(fcu):
    """额定工况冷量必须等于目录容量——修复不得改变选型结论

    这是本模型的标定基准：UA 由该点反解，因此该点必须精确复现。
    """
    state = terminal_state(fcu, 1.0, RATED_AIR_ENTERING_C, RATED_WATER_ENTERING_C)

    assert state.cooling_kw == pytest.approx(fcu.capacity_kw, rel=1e-6)


def test_capacity_never_exceeds_the_rated_point_at_normal_conditions(fcu):
    """额定点是工作区间的能力上限：偏离额定工况只能更差

    供水温高于额定、或室温低于额定，都必须给出更低冷量。这保证目录容量
    仍是选型的安全上限，不会因新模型被突破。
    """
    rated = terminal_state(fcu, 1.0, RATED_AIR_ENTERING_C, RATED_WATER_ENTERING_C)

    for tw, tr in ((8.0, 26.0), (12.0, 26.0), (7.0, 24.0), (7.0, 20.0)):
        state = terminal_state(fcu, 1.0, tr, tw)
        assert state.cooling_kw < rated.cooling_kw, f"Tw={tw} Tr={tr} 超过额定点"


def test_supply_air_temperature_tracks_water_temperature(fcu):
    """送风温必须随供水温变化，且永远严格高于供水温

    旧模型下送风温也被同一饱和项钳死，同样对供水温不敏感。
    """
    cold = terminal_state(fcu, 1.0, RATED_AIR_ENTERING_C, 7.0)
    warm = terminal_state(fcu, 1.0, RATED_AIR_ENTERING_C, 12.0)

    assert warm.supply_air_temp_c > cold.supply_air_temp_c
    for state, tw in ((cold, 7.0), (warm, 12.0)):
        assert state.supply_air_temp_c > tw, "盘管效能有限，送风温不可能达到供水温"
        assert state.supply_air_temp_c < RATED_AIR_ENTERING_C


def test_water_flow_uses_ntu_law_not_square_root(fcu):
    """流量影响走 ε-NTU，不再用 sqrt 经验式

    ε-NTU 下流量降低同时削弱 UA 侧的 C_min，衰减比 sqrt 更快；
    这里钉死半流量时的期望比值，防止回退到旧经验式。
    """
    rated_flow = fcu.flow_m3_h or 0.0

    full = terminal_state(
        fcu,
        valve_opening_ratio=1.0,
        room_temp_c=RATED_AIR_ENTERING_C,
        supply_water_temp_c=RATED_WATER_ENTERING_C,
        actual_water_flow_m3_h=rated_flow,
    )
    half = terminal_state(
        fcu,
        valve_opening_ratio=1.0,
        room_temp_c=RATED_AIR_ENTERING_C,
        supply_water_temp_c=RATED_WATER_ENTERING_C,
        actual_water_flow_m3_h=rated_flow / 2.0,
    )

    ratio = half.cooling_kw / full.cooling_kw
    # 水侧在此工况是 C_max（空气侧为 C_min），半流量下仍是 C_max，故 C_min 与
    # NTU 分母不变；衰减来自 UA 随流量的 0.8 次幂下降。实测 0.847。
    assert 0.75 < ratio < 0.95, f"半流量冷量比为 {ratio:.4f}，偏离 ε-NTU 预期"
    assert ratio != pytest.approx(math.sqrt(0.5), abs=1e-3), "不得回退 sqrt 经验式"
    # 流量继续下降时水侧接替成为 C_min，能力加速衰减
    quarter = terminal_state(
        fcu,
        valve_opening_ratio=1.0,
        room_temp_c=RATED_AIR_ENTERING_C,
        supply_water_temp_c=RATED_WATER_ENTERING_C,
        actual_water_flow_m3_h=rated_flow / 4.0,
    )
    assert quarter.cooling_kw / full.cooling_kw < 0.7, "低流量必须显著削弱能力"


def test_heating_side_shares_the_same_ntu_structure(fcu):
    """制热侧同构：热水温必须实质影响供热量"""
    cool_water = terminal_state(fcu, 1.0, 18.0, 40.0)
    hot_water = terminal_state(fcu, 1.0, 18.0, 55.0)

    assert hot_water.heating_kw > cool_water.heating_kw * 1.2
    assert cool_water.cooling_kw == 0.0 and hot_water.cooling_kw == 0.0


def test_air_flow_remains_a_first_order_limit(fcu):
    """风量仍是显式限制项：风机停转不出冷量"""
    stopped = terminal_state(
        fcu, 1.0, RATED_AIR_ENTERING_C, RATED_WATER_ENTERING_C, fan_speed_ratio=0.0
    )

    assert stopped.airflow_m3_h == 0.0
    assert stopped.cooling_kw == 0.0
    assert stopped.supply_air_temp_c == RATED_AIR_ENTERING_C


def test_water_side_energy_balance_still_bounds_capacity(fcu):
    """水侧能量守恒仍是硬约束：Q <= c * V_w * dT_max"""
    flow = (fcu.flow_m3_h or 0.0) * 0.001

    state = terminal_state(
        fcu,
        valve_opening_ratio=1.0,
        room_temp_c=30.0,
        supply_water_temp_c=7.0,
        actual_water_flow_m3_h=flow,
    )

    from app.simcore.devices import MAX_HYDRONIC_DELTA_T_C

    assert state.cooling_kw == pytest.approx(
        WATER_KW_PER_M3H_K * flow * MAX_HYDRONIC_DELTA_T_C
    )


def test_degenerate_inputs_stay_finite(fcu):
    """非有限与越界输入不得产生 NaN/Inf"""
    for tw, tr, fan in (
        (math.nan, 26.0, 1.0),
        (7.0, math.nan, 1.0),
        (7.0, 26.0, math.nan),
        (-10.0, 26.0, 1.0),
        (60.0, 26.0, 1.0),
    ):
        state = terminal_state(fcu, 1.0, tr, tw, fan_speed_ratio=fan)
        assert math.isfinite(state.cooling_kw)
        assert math.isfinite(state.heating_kw)
        assert math.isfinite(state.supply_air_temp_c)
        assert state.cooling_kw >= 0.0
        assert state.heating_kw >= 0.0


def test_air_side_capacitance_constant_matches_module_definition():
    """C_air 口径必须与项目统一的空气物性一致"""
    assert AIR_KW_PER_M3H_K == pytest.approx(1.2 * 1.005 / 3600.0)


def test_calibratable_terminals_report_no_limitation(fcu):
    """风机盘管在 27℃ 额定工况下可标定，不得报降级"""
    state = terminal_state(fcu, 1.0, RATED_AIR_ENTERING_C, RATED_WATER_ENTERING_C)

    assert state.coil_calibration_limited is False
    assert state.cooling_kw == pytest.approx(fcu.capacity_kw, rel=1e-6)


def test_fresh_air_unit_reports_calibration_limitation():
    """新风机组必须显式报降级，不得静默钳制

    新风机组是 100% 室外新风工况，盘管负荷远高于同风量回风工况机组，
    目录容量超过单程盘管 C_min·ΔT 上限（ε 需 1.8..2.2）。按「降级透明」
    约定必须暴露，而不是悄悄按上限出数。
    """
    ahu = resolve_rated_spec_by_type("新风机组", "MKS05D4Y/C")

    state = terminal_state(ahu, 1.0, RATED_AIR_ENTERING_C, RATED_WATER_ENTERING_C)

    assert state.coil_calibration_limited is True
    # 仍要出有限的正数结果，且不超过热力学上限
    assert state.cooling_kw > 0.0
    c_min = min(
        AIR_KW_PER_M3H_K * (ahu.airflow_m3_h or 0.0),
        WATER_KW_PER_M3H_K * (ahu.flow_m3_h or 0.0),
    )
    assert state.cooling_kw <= c_min * (RATED_AIR_ENTERING_C - RATED_WATER_ENTERING_C) * 1.0001


def test_calibration_flag_describes_the_model_not_the_operating_point():
    """降级标记描述的是该型号的标定性质，与当前工况无关

    标记回答的是"这条目录项能否被本模型忠实复现"，因此风机停转时 MKS 仍应
    为 True——它的额定容量本来就超出单程盘管上限，这一点不随阀位/风机变化。
    """
    ahu = resolve_rated_spec_by_type("新风机组", "MKS05D4Y/C")

    stopped = terminal_state(
        ahu, 1.0, RATED_AIR_ENTERING_C, RATED_WATER_ENTERING_C, fan_speed_ratio=0.0
    )

    assert stopped.airflow_m3_h == 0.0
    assert stopped.cooling_kw == 0.0
    assert stopped.coil_calibration_limited is True


def test_fan_coil_is_never_calibration_limited_at_any_operating_point(fcu):
    """可标定型号在任何工况下都不得被标记降级"""
    for fan, tw, tr in ((1.0, 7.0, 27.0), (0.5, 12.0, 26.0), (0.0, 7.0, 27.0)):
        state = terminal_state(fcu, 1.0, tr, tw, fan_speed_ratio=fan)
        assert state.coil_calibration_limited is False
