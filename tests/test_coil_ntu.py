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
import json
import math
from collections import Counter
from dataclasses import replace
from pathlib import Path

import pytest

from app.simcore.devices import (
    AIR_KW_PER_M3H_K,
    SENSIBLE_HEAT_RATIO,
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


# --- 分排数显热比 -----------------------------------------------------------
#
# 缺陷背景：`SENSIBLE_HEAT_RATIO = 0.75` 曾是**模块常量**，所有末端共用一个值。
# 但组合式空调机组的显热比随盘管排数显著变化（`docs/设备样本参数.md:255-256`）：
#
#     4 排：显冷 8.2 / 总冷 9.7  = 0.845
#     6 排：显冷 9.2 / 总冷 12.6 = 0.730
#     8 排：显冷 10.4 / 总冷 15.8 = 0.658
#
# 用 0.75 去算 8 排机组，显热被高估 0.75/0.658 = 1.14 倍，送风温降随之虚高
# 约 14%——教学演示里"8 排盘管除湿更强、送风更接近机器露点"这条结论会被算反：
# 8 排算出比 4 排更低的送风温，而真值恰好相反（8 排除湿多、显热少、送风温更高）。
#
# 注意：显热比**不参与** UA 标定（`_rated_coil_ua` 用的是目录全热容量），
# 因此这一族改动只修正送风温，不改变末端冷量，也不改变 coil_calibration_limited。

AHU = "组合式空调机组"


def test_ahu_sensible_heat_ratio_follows_row_count():
    """AHU 显热比必须按型号名的盘管排数取值，而不是共用 0.75

    数值直接来自 `docs/设备样本参数.md:255-256` 的 06-07 号机摘录，
    三个比值是该行显冷量/总冷量的精确结果（8.2/9.7、9.2/12.6、10.4/15.8）。
    """
    expected = {"4排": 0.845, "6排": 0.730, "8排": 0.658}

    for suffix, ratio in expected.items():
        spec = resolve_rated_spec_by_type(AHU, f"AHU-06-07-{suffix}")
        assert spec.sensible_heat_ratio == pytest.approx(ratio, abs=1e-9), (
            f"AHU-06-07-{suffix} 显热比应为 {ratio}"
        )


def test_sensible_heat_ratio_matches_the_documented_ratio_of_capacities():
    """显热比必须等于图上显冷量/总冷量——钉死"数据来源"而非只钉死数字

    06-07 号机三档的显冷/总冷在原图上都是明确数字。文档给出的是**取三位
    小数**后的比值（0.845/0.730/0.658），未取整的商为
    8.2/9.7=0.845361、9.2/12.6=0.730159、10.4/15.8=0.658228。
    此处按文档口径核对到 1e-3，并额外确认未取整的商也落在同一档内——
    两者都成立才说明常量确实来自这张图，而不是凑出来的数。
    """
    samples = {
        "4排": (8.2, 9.7),
        "6排": (9.2, 12.6),
        "8排": (10.4, 15.8),
    }

    for suffix, (sensible, total) in samples.items():
        spec = resolve_rated_spec_by_type(AHU, f"AHU-06-07-{suffix}")
        assert spec.sensible_heat_ratio == pytest.approx(sensible / total, abs=1e-3), (
            f"AHU-06-07-{suffix} 显热比与图上 {sensible}/{total} 不符"
        )


def test_row_count_ordering_is_monotonic_in_capacity_and_sensible_ratio():
    """排数越多显热比越低——这是本表唯一的物理方向，必须单调

    排数增加 -> 盘管表面温度更低 -> 除湿能力增强 -> 显热占比下降。
    若有人把 0.845/0.730/0.658 填反，方向性断言会先于数值断言暴露问题。
    """
    ratios = [
        resolve_rated_spec_by_type(AHU, f"AHU-06-07-{s}").sensible_heat_ratio
        for s in ("4排", "6排", "8排")
    ]

    assert ratios == sorted(ratios, reverse=True), f"显热比未随排数单调下降：{ratios}"


def test_sensible_capacity_ordering_follows_row_count():
    """显冷量必须随排数递增——三档在**同一风量**下才有可比性

    06-07 号机三档风量相同（2016 m3/h），但**总冷量不同**（9.7/12.6/15.8 kW），
    所以送风温不能直接横向比（8 排总冷量大得多，即使显热比更低，显冷量仍更高）。
    可比的口径是"目录全热 × 本型号显热比"：

        4 排：9.7  × 0.845 = 8.20 kW
        6 排：12.6 × 0.730 = 9.20 kW
        8 排：15.8 × 0.658 = 10.40 kW

    三者与 `docs/设备样本参数.md:239` 的显冷量列（8.2 / 9.2 / 10.4）逐一吻合。
    这同时验证了两件事：显热比按排数取值正确，且目录容量与图上总冷量一致。
    """
    expected_sensible = {"4排": 8.2, "6排": 9.2, "8排": 10.4}

    for suffix, expected in expected_sensible.items():
        spec = resolve_rated_spec_by_type(AHU, f"AHU-06-07-{suffix}")
        sensible_kw = (spec.capacity_kw or 0.0) * spec.sensible_heat_ratio
        assert sensible_kw == pytest.approx(expected, abs=0.01), (
            f"AHU-06-07-{suffix} 显冷量 {sensible_kw:.2f} kW 与图上 {expected} kW 不符"
        )


def test_eight_row_ahu_supply_air_is_warmed_by_the_new_ratio():
    """8 排送风温必须因显热比修正而**升高**约 1.84 K

    这是本次改动的直接可观测效果。8 排真值 SHR=0.658 低于旧常量 0.75，
    显热被高估 0.75/0.658 = 1.140 倍，送风温降相应虚高约 14%：

        SHR=0.75  -> 温降 15.00 K -> 送风温 12.00 C
        SHR=0.658 -> 温降 13.16 K -> 送风温 13.84 C

    旧值 12.00 C 恰好等于 27-15，是 0.75 这个数带来的巧合，不是任何物理边界；
    这里钉死修正后的 13.84 C，防止回退。
    """
    spec = resolve_rated_spec_by_type(AHU, "AHU-06-07-8排")

    state = terminal_state(spec, 1.0, RATED_AIR_ENTERING_C, RATED_WATER_ENTERING_C)

    assert state.supply_air_temp_c == pytest.approx(13.84, abs=0.01)
    # 与旧常量给出的 12.00C 相差 1.84 K，远大于浮点噪声
    assert state.supply_air_temp_c > 12.5


def test_eight_row_ahu_supply_air_temperature_is_pinned():
    """钉死 8 排机组在额定工况的送风温，防止回退到 0.75

    实测：AHU-06-07-8排 在 SHR=0.75 下送风温 12.00C，SHR=0.658 下 13.84C。
    该机组额定容量超出单程盘管上限（coil_calibration_limited=True），实际
    冷量按上限 13.5072 kW 出力，故 13.84C 是本模型在该点的确定输出。
    """
    spec = resolve_rated_spec_by_type(AHU, "AHU-06-07-8排")

    state = terminal_state(spec, 1.0, RATED_AIR_ENTERING_C, RATED_WATER_ENTERING_C)

    assert spec.sensible_heat_ratio == pytest.approx(0.658)
    assert state.cooling_kw == pytest.approx(13.5072, rel=1e-4)
    assert state.supply_air_temp_c == pytest.approx(13.84, abs=0.01)
    # 旧常量会给出 12.00C，两者相差 1.84 K，远大于任何浮点噪声
    assert state.supply_air_temp_c > 13.0


def test_four_row_ahu_supply_air_temperature_is_pinned():
    """钉死 4 排机组在额定工况的送风温

    4 排 SHR=0.845 高于旧常量 0.75，显热更多、送风更冷：16.23C -> 14.86C。
    4 排机组可标定，冷量严格等于目录容量 9.7 kW。
    """
    spec = resolve_rated_spec_by_type(AHU, "AHU-06-07-4排")

    state = terminal_state(spec, 1.0, RATED_AIR_ENTERING_C, RATED_WATER_ENTERING_C)

    assert spec.sensible_heat_ratio == pytest.approx(0.845)
    assert state.cooling_kw == pytest.approx(9.7, rel=1e-6)
    assert state.supply_air_temp_c == pytest.approx(14.86, abs=0.01)


def test_sensible_heat_ratio_does_not_change_capacity():
    """显热比是**送风温**的换算系数，不得改变末端冷量或标定结论

    这是本族改动最重要的边界：`_rated_coil_ua` 用目录全热容量标定 UA，
    完全不看显热比。若有人把显热比接进 UA 标定（例如误以为应当用显热去标定），
    冷量与 coil_calibration_limited 都会随之漂移，本测试会立刻失败。
    """
    spec = resolve_rated_spec_by_type(AHU, "AHU-06-07-4排")
    baseline = terminal_state(spec, 1.0, RATED_AIR_ENTERING_C, RATED_WATER_ENTERING_C)

    # 把显热比换成三个不同的值，冷量必须纹丝不动
    for ratio in (0.50, 0.75, 0.95):
        tweaked = replace(spec, sensible_heat_ratio=ratio)
        state = terminal_state(tweaked, 1.0, RATED_AIR_ENTERING_C, RATED_WATER_ENTERING_C)
        assert state.cooling_kw == pytest.approx(baseline.cooling_kw, rel=1e-12)
        assert state.coil_calibration_limited is baseline.coil_calibration_limited


def test_ahu_without_row_suffix_is_flagged_degraded():
    """解析不出排数的 AHU 必须显式标记降级，不得静默按 0.75 出数

    目录里有不带 `N排` 后缀的 AHU 型号（如 `K39G0608` 等摆放项，以及 Unity
    同步来的裸型号）。这类条目没有可依据的排数，只能退回兜底显热比——按
    「降级透明」约定，这件事必须出现在 `degraded_fields` 里。
    """
    item = {
        "type": AHU,
        "name": "AHU-NO-ROWS",
        "capacity_kw": 50.0,
        "airflow_m3_h": 10000.0,
        "flow_m3_h": 8.6,
    }

    # 目录里没有这个型号，走类型默认（代表型号恰好带 6排 后缀，故为 0.730），
    # 这里要检验的是**目录条目解析函数**对无后缀型号的处置，直接调用它。
    from app.simcore.rated import _from_catalog_item
    from app.unity_catalog import DeviceRole

    exact = _from_catalog_item(
        AHU, item, origin="catalog", role_hint=DeviceRole.AHU
    )
    assert exact.sensible_heat_ratio == pytest.approx(SENSIBLE_HEAT_RATIO)
    assert "default:sensible_heat_ratio" in exact.degraded_fields, (
        f"无排数后缀的 AHU 未报降级：{exact.degraded_fields}"
    )

    with_rows = _from_catalog_item(
        AHU,
        dict(item, name="AHU-06-07-8排"),
        origin="catalog",
        role_hint=DeviceRole.AHU,
    )
    assert with_rows.sensible_heat_ratio == pytest.approx(0.658)
    assert "default:sensible_heat_ratio" not in with_rows.degraded_fields


def test_fan_coil_and_fresh_air_unit_keep_the_default_ratio():
    """风机盘管与新风机组无分排数数据，保持兜底显热比且不记为降级

    这两族的目录条目本就没有排数概念（风机盘管无后缀，新风机组是全新风工况），
    用 0.75 不是"参数缺失"而是"数据本身的粒度"，因此不得报降级——
    否则降级标记会退化成噪音，真降级反而被淹没。
    """
    fcu_spec = resolve_rated_spec_by_type(FCU, FCU_MODEL)
    mks_spec = resolve_rated_spec_by_type("新风机组", "MKS05D4Y/C")

    assert fcu_spec.sensible_heat_ratio == pytest.approx(SENSIBLE_HEAT_RATIO)
    assert mks_spec.sensible_heat_ratio == pytest.approx(SENSIBLE_HEAT_RATIO)
    assert "default:sensible_heat_ratio" not in fcu_spec.degraded_fields
    assert "default:sensible_heat_ratio" not in mks_spec.degraded_fields


def test_eight_row_ahu_remains_calibration_limited():
    """8 排 AHU 仍不可标定——显热比改动**不能**、也不应该清除该标记

    这是对一次**错误预期**的钉死。曾有一个假设："8 排 AHU 越界是因为
    SENSIBLE_HEAT_RATIO=0.75 高估显热，改成分排数显热比就能标定"。
    该假设不成立：`_rated_coil_ua` 的分母用的是目录**全热**容量
    （ε = Q_total/(C_min·20K)），显热比在 `terminal_state` 里才被用到，
    且只影响送风温。实测 AHU-06-07-8排 在 SHR=0.845/0.75/0.658 下
    ε 恒为 1.1697。

    真正越界的原因是**风侧全热温差**：Q_total/C_air ≈ 23.3 K > 20 K 的
    单程上限（空气侧是 C_min）。要让它可标定必须改额定工况或重新解释
    目录容量口径，两者都属模型改动，不应顺手做掉。
    """
    spec = resolve_rated_spec_by_type(AHU, "AHU-06-07-8排")

    state = terminal_state(spec, 1.0, RATED_AIR_ENTERING_C, RATED_WATER_ENTERING_C)

    assert state.coil_calibration_limited is True
    # 目录容量确实超出上限：Q_total > C_air · ΔT
    c_air = AIR_KW_PER_M3H_K * (spec.airflow_m3_h or 0.0)
    implied_air_delta = (spec.capacity_kw or 0.0) / c_air
    assert implied_air_delta > RATED_AIR_ENTERING_C - RATED_WATER_ENTERING_C
    assert implied_air_delta == pytest.approx(23.3, abs=0.1)


def test_catalog_wide_calibration_limited_count_is_25_of_64():
    """目录范围回归：可标定失败的末端恒为 25/64

    改动显热比前后该计数完全一致（显热比不进入 UA 标定）。把它钉死可以让
    "顺手让 8 排 AHU 变得可标定"这类改动立刻失败——那需要显式决策，而不是
    悄悄调参。
    """
    from app.simcore.devices import is_terminal

    catalog = json.loads(
        (Path(__file__).resolve().parents[1] / "app/data/equipment_catalog.json").read_text(
            encoding="utf-8"
        )
    )
    limited: Counter[str] = Counter()
    total = 0
    for entry in catalog:
        if entry.get("placement_only") or not entry.get("capacity_kw"):
            continue
        spec = resolve_rated_spec_by_type(entry["type"], entry["name"])
        if not is_terminal(spec.role):
            continue
        total += 1
        state = terminal_state(
            spec, 1.0, RATED_AIR_ENTERING_C, RATED_WATER_ENTERING_C
        )
        if state.coil_calibration_limited:
            limited[entry["type"]] += 1

    assert total == 64
    assert sum(limited.values()) == 25, f"可标定失败数变化：{dict(limited)}"
    assert limited["新风机组"] == 10
    assert limited["组合式空调机组"] == 15
    assert limited["风机盘管"] == 0
