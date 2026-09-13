"""设备模型单调性 - 单元测试

教学演示的底线是方向不能错：当场出反直觉结果比不精确更糟。
本文件锁死各条参数链路的方向性关系，不断言具体数值——
数值会随物理模型细化而变，方向不会。
"""

from app.simcore.devices import (
    chiller_state,
    cooling_tower_state,
    heat_source_cooling_state,
    heat_source_state,
    pump_state,
    terminal_state,
    valve_state,
    water_delta_t,
)
from app.simcore.rated import resolve_rated_spec_by_type

import pytest


SUPPLY_TEMP_C = 7.0
CONDENSER_TEMP_C = 30.0


class TestChiller:
    """冷机"""

    def test_larger_capacity_lowers_part_load_ratio(self):
        """同样负荷下额定容量越大，负荷率越低"""
        small = resolve_rated_spec_by_type("冷源机组", "SCWE-90H+B")
        large = resolve_rated_spec_by_type("冷源机组", "SCWE-200H+B")

        small_state = chiller_state(small, 300.0, SUPPLY_TEMP_C, CONDENSER_TEMP_C)
        large_state = chiller_state(large, 300.0, SUPPLY_TEMP_C, CONDENSER_TEMP_C)

        assert large_state.load_ratio < small_state.load_ratio

    def test_model_change_moves_readings(self):
        """换型号必须改变读数——legacy 实现里这一条是断的"""
        small = resolve_rated_spec_by_type("冷源机组", "SCWE-90H+B")
        large = resolve_rated_spec_by_type("冷源机组", "SCWE-200H+B")

        small_state = chiller_state(small, 300.0, SUPPLY_TEMP_C, CONDENSER_TEMP_C)
        large_state = chiller_state(large, 300.0, SUPPLY_TEMP_C, CONDENSER_TEMP_C)

        assert small_state.flow_m3_h != large_state.flow_m3_h
        assert small_state.cop != large_state.cop

    def test_higher_condenser_temperature_lowers_cop(self):
        """冷凝温度升高压低 COP、抬高功率"""
        rated = resolve_rated_spec_by_type("冷源机组", "SCWE-90H+B")

        cool = chiller_state(rated, 300.0, SUPPLY_TEMP_C, 28.0)
        hot = chiller_state(rated, 300.0, SUPPLY_TEMP_C, 36.0)

        assert hot.cop < cool.cop
        assert hot.power_kw > cool.power_kw

    def test_condenser_sensitivity_survives_across_the_whole_load_range(self):
        """全负荷区间内冷凝温度必须始终可观测地影响 COP

        原实现的部分负荷曲线是无下界抛物线 `peak - α·(PLR-0.7)²`，在
        PLR→0 时趋于 -∞，于是被 `_clamp(cop, 1.5, 12)` 的下界钳住。钳位一旦
        饱和，冷凝温度与供水温都失去影响——"降低冷却水流量 -> 冷凝温度升高
        -> COP 下降"这条教学链路在低负荷时整段失效（30℃ 与 50℃ 同为 1.5）。

        这里逐负荷率扫过整段区间，要求任意两档冷凝温度都给出不同 COP。
        比只测单一负荷点更严格，能捕获"部分区间被钳死"这类退化。
        """
        rated = resolve_rated_spec_by_type("冷源机组", "SCWE-90H+B")

        for load_kw in (30.0, 60.0, 100.0, 160.0, 220.0, 300.0):
            cool = chiller_state(rated, load_kw, SUPPLY_TEMP_C, 32.0)
            hot = chiller_state(rated, load_kw, SUPPLY_TEMP_C, 40.0)

            assert hot.cop < cool.cop, (
                f"负荷 {load_kw} kW 时冷凝温度未影响 COP："
                f"32℃ -> {cool.cop:.4f}, 40℃ -> {hot.cop:.4f}（疑似钳位饱和）"
            )
            assert hot.power_kw > cool.power_kw, f"负荷 {load_kw} kW 时功率未随冷凝温度上升"

    def test_low_load_cop_stays_physically_plausible(self):
        """极低负荷率下 COP 不得崩塌到非物理值

        真实冷机在低负荷通过卸载/间歇运行维持效率，COP 下降但不会趋零。
        """
        rated = resolve_rated_spec_by_type("冷源机组", "SCWE-90H+B")

        state = chiller_state(rated, 20.0, SUPPLY_TEMP_C, CONDENSER_TEMP_C)

        assert 1.5 <= state.cop <= 6.0
        assert state.power_kw > 0.0

    def test_higher_load_raises_plr_and_power(self):
        """负荷升高使 PLR 与输入功率同时升高（正常部分负荷区间）"""
        # 用与负荷匹配的机型：超大机组在 PLR<0.2 时部分负荷 COP 崩坏，
        # 功率会非单调回升，那是真实物理，不属于本条要锁定的方向性。
        rated = resolve_rated_spec_by_type("冷源机组", "SCWE-90H+B")

        low = chiller_state(rated, 150.0, SUPPLY_TEMP_C, CONDENSER_TEMP_C)
        high = chiller_state(rated, 280.0, SUPPLY_TEMP_C, CONDENSER_TEMP_C)

        assert high.load_ratio > low.load_ratio
        assert high.power_kw > low.power_kw

    def test_oversized_chiller_degrades_below_normal_part_load(self):
        """超大机组在极低负荷下 COP 恶化、功率回升（真实部分负荷特性）"""
        rated = resolve_rated_spec_by_type("冷源机组", "SCWE-200H+B")

        starved = chiller_state(rated, 150.0, SUPPLY_TEMP_C, CONDENSER_TEMP_C)
        healthy = chiller_state(rated, 300.0, SUPPLY_TEMP_C, CONDENSER_TEMP_C)

        assert starved.load_ratio < healthy.load_ratio
        # 负荷率更低，却因部分负荷效率恶化而更耗电
        assert starved.cop < healthy.cop
        assert starved.power_kw > healthy.power_kw

    def test_heat_reject_exceeds_cooling_load(self):
        """排热量等于冷量加上压缩功，必须大于冷量"""
        rated = resolve_rated_spec_by_type("冷源机组", "SCWE-90H+B")
        state = chiller_state(rated, 300.0, SUPPLY_TEMP_C, CONDENSER_TEMP_C)

        assert state.heat_reject_kw > state.cooling_kw

    def test_return_temp_above_supply_temp(self):
        """回水温高于供水温，且供水温设定值被如实采用"""
        rated = resolve_rated_spec_by_type("冷源机组", "SCWE-90H+B")
        state = chiller_state(rated, 300.0, 6.0, CONDENSER_TEMP_C)

        assert state.supply_temp_c == 6.0
        assert state.return_temp_c > state.supply_temp_c

    def test_disabled_chiller_produces_no_load(self):
        """停机时不产生冷量与功耗"""
        rated = resolve_rated_spec_by_type("冷源机组", "SCWE-90H+B")
        state = chiller_state(
            rated, 300.0, SUPPLY_TEMP_C, CONDENSER_TEMP_C, enabled=False
        )

        assert state.cooling_kw == 0.0
        assert state.power_kw == 0.0

    def test_zero_load_does_not_divide_by_zero(self):
        """零负荷不应触发除零"""
        rated = resolve_rated_spec_by_type("冷源机组", "SCWE-90H+B")
        state = chiller_state(rated, 0.0, SUPPLY_TEMP_C, CONDENSER_TEMP_C)

        assert state.cooling_kw == 0.0
        assert state.return_temp_c == state.supply_temp_c


class TestPump:
    """水泵相似定律"""

    def test_affinity_laws_exponents(self):
        """流量一次方、扬程二次方、功率三次方"""
        rated = resolve_rated_spec_by_type("循环泵", "EC32")

        full = pump_state(rated, 1.0)
        half = pump_state(rated, 0.5)

        assert half.flow_m3_h == full.flow_m3_h * 0.5
        assert abs(half.head_m - full.head_m * 0.25) < 1e-9
        assert abs(half.power_kw - full.power_kw * 0.125) < 1e-9

    def test_lower_speed_reduces_all_outputs(self):
        """降转速时流量、扬程、功率同向下降"""
        rated = resolve_rated_spec_by_type("循环泵", "EC32")

        fast = pump_state(rated, 0.9)
        slow = pump_state(rated, 0.4)

        assert slow.flow_m3_h < fast.flow_m3_h
        assert slow.head_m < fast.head_m
        assert slow.power_kw < fast.power_kw

    def test_stopped_pump_has_no_flow(self):
        """停泵后无流量"""
        rated = resolve_rated_spec_by_type("循环泵", "EC32")
        state = pump_state(rated, 1.0, enabled=False)

        assert state.flow_m3_h == 0.0
        assert state.power_kw == 0.0


class TestHeatSource:
    def test_heat_source_outputs_bounded_positive_heating(self):
        rated = resolve_rated_spec_by_type("空气源热泵", "ASHP-240")
        state = heat_source_state(rated, 180.0, 45.0, 5.0, 18.0)

        assert 0.0 < state.heating_kw <= rated.capacity_kw
        assert state.cop > 1.0
        assert state.power_kw > 0.0
        assert state.supply_temp_c > state.return_temp_c
        assert state.flow_m3_h == 18.0

    def test_heat_source_zero_flow_stops_heat_delivery(self):
        rated = resolve_rated_spec_by_type("空气源热泵", "ASHP-240")
        state = heat_source_state(rated, 180.0, 45.0, 5.0, 0.0)

        assert state.heating_kw == 0.0
        assert state.power_kw == 0.0
        assert state.flow_m3_h == 0.0


class TestHeatSourceCooling:
    """热泵制冷工况（四通阀换向后的教学模型）"""

    def test_cooling_outputs_bounded_and_reject_conserves(self):
        rated = resolve_rated_spec_by_type("空气源热泵", None)
        state = heat_source_cooling_state(rated, 200.0, 7.0, 35.0, 45.0)

        assert 0.0 < state.cooling_kw <= rated.capacity_kw
        assert state.heating_kw == 0.0
        assert 1.2 <= state.cop <= 8.0
        assert state.power_kw > 0.0
        # 排热守恒：冷凝侧 = 制冷量 + 电功率
        assert state.heat_reject_kw == pytest.approx(
            state.cooling_kw + state.power_kw
        )
        # 制冷回水温度高于供水（吸热方向与制热相反）
        assert state.return_temp_c > state.supply_temp_c

    def test_zero_flow_stops_cooling_delivery(self):
        rated = resolve_rated_spec_by_type("水源热泵", None)
        state = heat_source_cooling_state(rated, 200.0, 7.0, 35.0, 0.0)

        assert state.cooling_kw == 0.0
        assert state.power_kw == 0.0
        assert state.heat_reject_kw == 0.0

    def test_air_source_high_ambient_reduces_cop_but_stays_bounded(self):
        rated = resolve_rated_spec_by_type("空气源热泵", None)
        mild = heat_source_cooling_state(rated, 200.0, 7.0, 35.0, 45.0)
        hot = heat_source_cooling_state(rated, 200.0, 7.0, 43.0, 45.0)

        assert hot.cop < mild.cop
        assert hot.cop >= 1.2

    def test_water_source_cop_ignores_ambient_temperature(self):
        rated = resolve_rated_spec_by_type("水源热泵", None)
        mild = heat_source_cooling_state(rated, 200.0, 7.0, 20.0, 45.0)
        hot = heat_source_cooling_state(rated, 200.0, 7.0, 40.0, 45.0)

        assert hot.cop == pytest.approx(mild.cop)

    def test_cop_falls_back_to_capacity_over_power(self):
        """目录缺 eer/cop 时的兜底：制冷 COP = 制冷量 / 额定功率。"""
        rated = resolve_rated_spec_by_type("地源热泵", None)
        assert rated.cop is None or rated.cop > 0.0  # 121 条目录已带 cop
        state = heat_source_cooling_state(rated, 200.0, 7.0, 35.0, 45.0)
        assert state.cop > 0.0


class TestWaterLoop:
    """水环路温差"""

    def test_lower_flow_widens_delta_t(self):
        """流量降低使供回水温差拉大——教学演示最直观的一条"""
        wide = water_delta_t(300.0, 30.0)
        narrow = water_delta_t(300.0, 60.0)

        assert wide > narrow

    def test_higher_load_widens_delta_t(self):
        """同流量下负荷越大温差越大"""
        assert water_delta_t(400.0, 60.0) > water_delta_t(200.0, 60.0)

    def test_zero_flow_is_guarded(self):
        """零流量不除零"""
        assert water_delta_t(300.0, 0.0) == 0.0


class TestCoolingTower:
    """冷却塔与冷凝耦合"""

    def test_lower_fan_speed_widens_approach(self):
        """风机降速使逼近度变大、冷凝进水温升高"""
        rated = resolve_rated_spec_by_type("冷却塔", None)

        fast = cooling_tower_state(rated, 1.0, 350.0, 27.0)
        slow = cooling_tower_state(rated, 0.4, 350.0, 27.0)

        assert slow.approach_c > fast.approach_c
        assert slow.entering_water_temp_c > fast.entering_water_temp_c

    def test_higher_wetbulb_raises_condenser_temperature(self):
        """湿球温度升高直接抬高冷凝侧温度"""
        rated = resolve_rated_spec_by_type("冷却塔", None)

        mild = cooling_tower_state(rated, 1.0, 350.0, 24.0)
        humid = cooling_tower_state(rated, 1.0, 350.0, 30.0)

        assert humid.entering_water_temp_c > mild.entering_water_temp_c

    def test_lower_water_flow_widens_temperature_rise(self):
        """冷却水流量降低使塔进出水温差拉大"""
        rated = resolve_rated_spec_by_type("冷却塔", None)

        high = cooling_tower_state(rated, 1.0, 350.0, 27.0, water_flow_m3_h=70.0)
        low = cooling_tower_state(rated, 1.0, 350.0, 27.0, water_flow_m3_h=35.0)

        high_rise = high.entering_water_temp_c - high.leaving_water_temp_c
        low_rise = low.entering_water_temp_c - low.leaving_water_temp_c
        assert low_rise > high_rise


class TestCrossDeviceFeedback:
    """跨设备反馈链"""

    def test_throttling_chilled_pump_widens_delta_t(self):
        """关小冷冻水泵 -> 流量降 -> 供回水温差拉大 -> 回水温升高

        这是教学演示最直观的一条链路：一个可调参数带动四个读数同时变化。
        """
        pump_rated = resolve_rated_spec_by_type("循环泵", "EC100")
        chiller_rated = resolve_rated_spec_by_type("冷源机组", "SCWE-90H+B")

        def chiller_at(pump_speed: float):
            pump = pump_state(pump_rated, pump_speed)
            return chiller_state(
                chiller_rated,
                300.0,
                SUPPLY_TEMP_C,
                CONDENSER_TEMP_C,
                chilled_flow_m3_h=pump.flow_m3_h,
            )

        healthy = chiller_at(1.0)
        throttled = chiller_at(0.6)

        assert throttled.flow_m3_h < healthy.flow_m3_h
        healthy_delta = healthy.return_temp_c - healthy.supply_temp_c
        throttled_delta = throttled.return_temp_c - throttled.supply_temp_c
        assert throttled_delta > healthy_delta

    def test_throttling_cooling_pump_penalises_chiller(self):
        """关小冷却水泵 -> 冷凝温度升 -> COP 降 -> 冷机功率升

        跨三个设备的闭环反馈，比单看一个读数更有说服力。
        """
        pump_rated = resolve_rated_spec_by_type("循环泵", "EC100")
        tower_rated = resolve_rated_spec_by_type("冷却塔", None)
        chiller_rated = resolve_rated_spec_by_type("冷源机组", "SCWE-90H+B")

        def chiller_at(pump_speed: float):
            pump = pump_state(pump_rated, pump_speed)
            tower = cooling_tower_state(
                tower_rated, 1.0, 350.0, 27.0, water_flow_m3_h=pump.flow_m3_h
            )
            return chiller_state(
                chiller_rated, 300.0, SUPPLY_TEMP_C, tower.entering_water_temp_c
            )

        healthy = chiller_at(1.0)
        throttled = chiller_at(0.5)

        assert throttled.cop < healthy.cop
        assert throttled.power_kw > healthy.power_kw

    def test_lower_tower_fan_speed_penalises_chiller(self):
        """冷却塔风机降速同样会通过冷凝温度压低冷机 COP"""
        tower_rated = resolve_rated_spec_by_type("冷却塔", None)
        chiller_rated = resolve_rated_spec_by_type("冷源机组", "SCWE-90H+B")

        def chiller_at(fan_ratio: float):
            tower = cooling_tower_state(tower_rated, fan_ratio, 350.0, 27.0)
            return chiller_state(
                chiller_rated, 300.0, SUPPLY_TEMP_C, tower.leaving_water_temp_c
            )

        assert chiller_at(0.4).cop < chiller_at(1.0).cop

    def test_higher_wetbulb_raises_condensing_and_penalises_chiller(self):
        """湿球升高经冷却塔抬高冷凝温度并压低 COP"""
        tower_rated = resolve_rated_spec_by_type("冷却塔", None)
        chiller_rated = resolve_rated_spec_by_type("冷源机组", "SCWE-90H+B")

        mild_tower = cooling_tower_state(tower_rated, 1.0, 350.0, 23.0)
        humid_tower = cooling_tower_state(tower_rated, 1.0, 350.0, 29.0)
        mild = chiller_state(
            chiller_rated, 300.0, SUPPLY_TEMP_C, mild_tower.leaving_water_temp_c
        )
        humid = chiller_state(
            chiller_rated, 300.0, SUPPLY_TEMP_C, humid_tower.leaving_water_temp_c
        )

        assert humid_tower.leaving_water_temp_c > mild_tower.leaving_water_temp_c
        assert humid.cop < mild.cop


class TestTerminal:
    """末端"""

    def test_larger_opening_increases_cooling(self):
        """阀门开度越大换热量越大，送风温越低"""
        rated = resolve_rated_spec_by_type("风机盘管", "MCW200V")

        small = terminal_state(rated, 0.3, 26.0, SUPPLY_TEMP_C)
        large = terminal_state(rated, 0.9, 26.0, SUPPLY_TEMP_C)

        assert large.cooling_kw > small.cooling_kw
        assert large.supply_air_temp_c < small.supply_air_temp_c

    def test_warmer_supply_water_reduces_capacity(self):
        """供水温越高可用换热温差越小，能力下降"""
        rated = resolve_rated_spec_by_type("风机盘管", "MCW200V")

        cold = terminal_state(rated, 1.0, 26.0, 7.0)
        warm = terminal_state(rated, 1.0, 26.0, 18.0)

        assert warm.cooling_kw < cold.cooling_kw

    def test_supply_air_never_reaches_water_temperature(self):
        """盘管效能有限，送风温必须严格高于供水温"""
        rated = resolve_rated_spec_by_type("风机盘管", "MCW200V")
        state = terminal_state(rated, 1.0, 26.0, 7.0)

        assert state.supply_air_temp_c > 7.0
        assert state.supply_air_temp_c < 26.0

    def test_zero_airflow_does_not_divide_by_zero(self):
        """风机停转时不除零"""
        rated = resolve_rated_spec_by_type("风机盘管", "MCW200V")
        state = terminal_state(rated, 1.0, 26.0, 7.0, fan_speed_ratio=0.0)

        assert state.airflow_m3_h == 0.0
        assert state.supply_air_temp_c == 26.0

    def test_hot_water_produces_heating_and_warm_supply_air(self):
        rated = resolve_rated_spec_by_type("风机盘管", "MCW200V")
        state = terminal_state(rated, 0.8, 18.0, 45.0, fan_speed_ratio=0.7)

        assert state.cooling_kw == 0.0
        assert state.heating_kw > 0.0
        assert 18.0 < state.supply_air_temp_c < 45.0


class TestValve:
    """阀门"""

    def test_closing_valve_raises_resistance(self):
        """开度越小阻力越大"""
        assert valve_state(0.2).resistance_ratio > valve_state(0.8).resistance_ratio

    def test_fully_closed_valve_resistance_is_finite(self):
        """全关时阻力被截断为有限值，保证水力计算不发散"""
        state = valve_state(0.0)

        assert state.opening_ratio == 0.0
        assert state.resistance_ratio < float("inf")
