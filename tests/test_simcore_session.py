"""仿真会话与稳态 - 单元测试

阶段二关注两件事：动力学是否由设备参数驱动（而不是 legacy 那个与设备无关的
比例系数），以及水侧/冷凝侧耦合是否收敛。断言仍以方向性为主。
"""

import itertools
import math
from dataclasses import replace

import pytest

from app.simcore.hydronics import advance_temperature
from app.simcore.rated import RatedSpec, resolve_rated_spec_by_type
from app.simcore.room import (
    MAX_ROOM_RH_PCT,
    MIN_ROOM_RH_PCT,
    RoomThermalConfig,
    equilibrium_temperature,
    step_room_humidity,
    step_room_temperature,
)
from app.simcore.session import (
    AUTO_DEHUMIDIFICATION_SUBCOOL_C,
    WATER_TEMPERATURE_TIME_CONSTANT_SECONDS,
    DeviceNode,
    PlantLayout,
    RoomInitialState,
    RoomNode,
    SimulationConfig,
    SimulationMode,
    SimulationSession,
)
from app.simcore.steady import DESIGN_HOUR_OF_DAY, solve_steady
from app.simcore.weather import ambient_at, wet_bulb_c
from app.unity_catalog import DeviceRole

ROOM_ID = "room-1"

# 与 K39G0711（35 kW）相匹配的房间规模。legacy RuntimeConfig 的默认房间
# （围护 0.18 kW/K、内扰 2.6 kW）是配那个与设备无关的 2.4 系数用的，
# 换成真实设备能力后量级严重错配，室温会一路跌到钳制下限。
#
# 内扰按显热口径标定：目录容量为全热，进入温度平衡的只有 75%，
# 内扰若按全热配平，稳态室温会高到让盘管温差因子始终饱和，
# 出水温度、水流量这类差异就全被容量上限吃掉，测不出方向性。
DEMO_ROOM = RoomThermalConfig(
    thermal_capacity_kj_k=8000.0,
    envelope_conductance_kw_k=1.2,
    occupancy_people=40,
    lighting_w=4000.0,
    equipment_w=6000.0,
)

# 5 分钟步长：默认 60 秒时 20 个 tick 只推进不到 1℃，测不出趋势。
# 显式欧拉的稳定上限约为 2C/U ≈ 3900 秒，300 秒有充足裕量。
DEMO_CONFIG = SimulationConfig(step_seconds=300.0)


def synthetic_ahu_rated() -> RatedSpec:
    """Synthetic AHU used only to exercise simulation physics."""
    return RatedSpec(
        device_type="测试组合式空调机组",
        model_name="TEST-AHU-35",
        role=DeviceRole.AHU,
        capacity_kw=35.0,
        flow_m3_h=6.02,
        airflow_m3_h=8000.0,
        fan_power_kw=0.64,
        power_kw=0.64,
        origin="catalog",
    )


def build_layout(
    chiller_model: str = "SCWE-90H+B", pump_model: str = "EC100"
) -> PlantLayout:
    """一套最小可运行系统：冷机 + 冷冻泵 + 冷却泵 + 冷却塔 + 末端"""
    return PlantLayout(
        devices=(
            DeviceNode(
                "chiller-1", resolve_rated_spec_by_type("冷源机组", chiller_model)
            ),
            DeviceNode(
                "pump-chilled",
                resolve_rated_spec_by_type("循环泵", pump_model),
                water_loop="chilled",
            ),
            DeviceNode(
                "pump-cooling",
                resolve_rated_spec_by_type("循环泵", pump_model),
                water_loop="cooling",
            ),
            DeviceNode("tower-1", resolve_rated_spec_by_type("冷却塔", None)),
            DeviceNode(
                "ahu-1",
                synthetic_ahu_rated(),
                room_id=ROOM_ID,
            ),
        ),
        rooms=(RoomNode(ROOM_ID, "示例房间", DEMO_ROOM),),
    )


def session_for(layout: PlantLayout | None = None) -> SimulationSession:
    return SimulationSession(layout or build_layout(), DEMO_CONFIG)


def _terminal_rated_capacity_kw(layout: PlantLayout | None = None) -> float:
    """布局中全部末端节点的额定容量之和（kW）

    用于断言"末端出力不得越过额定能力"，而不是硬编码某个型号的数字。
    """
    target = layout or build_layout()
    return sum(
        node.rated.capacity_kw or 0.0
        for node in target.devices
        if node.rated.role in {DeviceRole.FCU, DeviceRole.AHU, DeviceRole.FRESH_AIR_UNIT}
    )


def _terminal_sensible_kw(snapshot) -> float:
    """快照中全部末端的显热出力之和（kW）

    只取显热分量：潜热随房间湿度变化，跨工况比较能力时应剔除，
    否则会把"湿度轨迹不同"误读成"末端能力不同"。
    """
    return sum(
        reading.cooling_kw
        for reading in snapshot.rooms.values()
    )


def build_heating_layout() -> PlantLayout:
    layout = build_layout()
    return replace(
        layout,
        devices=(
            *layout.devices,
            DeviceNode(
                "heat-source-1",
                resolve_rated_spec_by_type("空气源热泵", "ASHP-240"),
            ),
            DeviceNode(
                "pump-hot",
                resolve_rated_spec_by_type("循环泵", "EC50"),
                water_loop="hot",
            ),
        ),
    )


def run(session: SimulationSession, ticks: int = 60):
    snapshot = session.snapshot
    for _ in range(ticks):
        snapshot = session.step()
    return snapshot


class TestWeather:
    """室外气象"""

    def test_wet_bulb_never_exceeds_dry_bulb(self):
        """湿球温度不可能高于干球"""
        for humidity in (10.0, 50.0, 95.0):
            assert wet_bulb_c(33.0, humidity) <= 33.0

    def test_higher_humidity_raises_wet_bulb(self):
        """湿度越高湿球越接近干球"""
        assert wet_bulb_c(33.0, 80.0) > wet_bulb_c(33.0, 40.0)

    def test_daily_cycle_peaks_in_afternoon(self):
        """日温度峰值出现在下午，谷值出现在凌晨"""
        afternoon = ambient_at(14 * 3600.0, base_temp_c=33.0)
        dawn = ambient_at(2 * 3600.0, base_temp_c=33.0)

        assert afternoon.dry_bulb_c > dawn.dry_bulb_c


class TestRoom:
    """房间热平衡"""

    def test_more_cooling_lowers_temperature(self):
        """制冷量越大室温下降越多"""
        config = RoomThermalConfig()

        weak = step_room_temperature(28.0, 33.0, 2.0, config, 60.0)
        strong = step_room_temperature(28.0, 33.0, 10.0, config, 60.0)

        assert strong.temperature_c < weak.temperature_c

    def test_hotter_outdoor_raises_temperature(self):
        """室外越热室温上升越快"""
        config = RoomThermalConfig()

        mild = step_room_temperature(26.0, 30.0, 0.0, config, 60.0)
        hot = step_room_temperature(26.0, 38.0, 0.0, config, 60.0)

        assert hot.temperature_c > mild.temperature_c

    def test_temperature_is_clamped(self):
        """极端参数下室温被钳制在物理范围内"""
        config = RoomThermalConfig(thermal_capacity_kj_k=1.0)
        balance = step_room_temperature(26.0, 33.0, 5000.0, config, 60.0)

        assert balance.temperature_c >= 10.0

    def test_equilibrium_matches_zero_net_heat(self):
        """稳态温度处净热流为零"""
        config = RoomThermalConfig()
        cooling_kw = 4.0
        equilibrium = equilibrium_temperature(33.0, cooling_kw, config)

        balance = step_room_temperature(equilibrium, 33.0, cooling_kw, config, 60.0)
        assert abs(balance.net_kw) < 1e-6

    def test_zero_thermal_capacity_rejected(self):
        """热容非正应当直接报错而不是产生无穷大温升"""
        with pytest.raises(ValueError):
            RoomThermalConfig(thermal_capacity_kj_k=0.0)


class TestSessionDrivenByEquipment:
    """设备参数驱动"""

    def test_cooling_pulls_room_below_ambient(self):
        """系统运行后室温应低于室外"""
        snapshot = run(session_for())

        assert snapshot.rooms[ROOM_ID].temperature_c < snapshot.ambient.dry_bulb_c

    def test_room_initial_state_is_snapshot_owned_and_humidity_advances(self):
        second_room = RoomNode(
            "room-2",
            "second",
            DEMO_ROOM,
            initial_relative_humidity_pct=63.0,
        )
        layout = replace(build_layout(), rooms=(*build_layout().rooms, second_room))
        config = replace(
            DEMO_CONFIG,
            initial_room_temp_c=28.0,
            room_initial_states=(
                RoomInitialState(ROOM_ID, 31.0, 47.0),
            ),
        )

        session = SimulationSession(layout, config)
        initial = session.snapshot
        stepped = session.step()

        assert initial.rooms[ROOM_ID].temperature_c == 31.0
        assert initial.rooms[ROOM_ID].relative_humidity_pct == 47.0
        assert initial.rooms["room-2"].temperature_c == 28.0
        assert initial.rooms["room-2"].relative_humidity_pct == 63.0
        # 初值由快照持有，之后按湿平衡推进：人员散湿使湿度上升，
        # 且始终落在物理边界内。
        for room_id in (ROOM_ID, "room-2"):
            advanced = stepped.rooms[room_id].relative_humidity_pct
            assert advanced != initial.rooms[room_id].relative_humidity_pct
            assert MIN_ROOM_RH_PCT <= advanced <= MAX_ROOM_RH_PCT
        # 取湿平衡分项：除湿量由盘管实际出风状态决定（混合风过盘管），
        # 也可能是干盘管（高风量、低冷量时送风未到露点，除湿为 0）。
        reading = stepped.rooms[ROOM_ID]
        assert reading.target_humidity_pct == config.target_humidity_pct
        net = reading.net_latent_kw
        assert net == pytest.approx(
            reading.fresh_air_latent_kw
            + reading.internal_latent_kw
            - reading.dehumidification_kw,
            abs=0.01,
        )
        # 湿度推进方向必须与净潜热一致：净湿为正则升高，反之降低
        delta = reading.relative_humidity_pct - initial.rooms[ROOM_ID].relative_humidity_pct
        if net > 0.0:
            assert delta >= 0.0
        elif net < 0.0:
            assert delta <= 0.0

    def test_humidity_balance_terms_reach_snapshot(self):
        """湿平衡分项随快照给出，且各项自洽"""
        session = session_for()
        reading = run(session, 20).rooms[ROOM_ID]

        assert reading.net_latent_kw == pytest.approx(
            reading.fresh_air_latent_kw
            + reading.internal_latent_kw
            - reading.dehumidification_kw
        )
        assert reading.internal_latent_kw > 0.0
        assert reading.dehumidification_kw > 0.0

    def test_standby_admits_no_fresh_air_but_ventilation_does(self):
        """待机不引入新风，通风工况必须引入

        通风时没有冷源，室温与散湿都会上行，新风项可能变为负值（室内比室外更湿，
        新风反而带走水分）。故只断言"是否发生交换"，不断言方向。
        """
        room = RoomNode(ROOM_ID, "示例房间", DEMO_ROOM, fresh_air_m3_h=800.0)
        layout = replace(build_layout(), rooms=(room,))
        humid_outdoor = replace(DEMO_CONFIG, outdoor_humidity_pct=85.0)

        standby = SimulationSession(
            layout, replace(humid_outdoor, mode=SimulationMode.STANDBY)
        )
        ventilation = SimulationSession(
            layout, replace(humid_outdoor, mode=SimulationMode.VENTILATION)
        )

        assert run(standby, 5).rooms[ROOM_ID].fresh_air_latent_kw == 0.0
        assert run(ventilation, 5).rooms[ROOM_ID].fresh_air_latent_kw != 0.0

    def test_fresh_air_unit_capacity_offsets_outdoor_latent_load(self):
        """新风机组的潜热能力先抵扣新风湿负荷

        直接比对湿平衡函数：同一输入下只改变新风机组潜热能力，
        避免"增加一台设备"同时改变显热而混淆结论。
        """
        room = RoomThermalConfig(
            thermal_capacity_kj_k=8000.0,
            envelope_conductance_kw_k=1.2,
            occupancy_people=40,
            volume_m3=300.0,
        )
        kwargs = {
            "current_rh_pct": 55.0,
            "current_temp_c": 26.0,
            "next_temp_c": 26.0,
            "outdoor_temp_c": 33.0,
            "outdoor_rh_pct": 85.0,
            "latent_cooling_kw": 0.0,
            "config": room,
            "step_seconds": 60.0,
            "fresh_air_m3_h": 1200.0,
        }

        untreated = step_room_humidity(**kwargs)
        treated = step_room_humidity(**kwargs, fresh_air_latent_capacity_kw=3.0)

        assert untreated.infiltration_latent_kw > 0.0
        assert treated.infiltration_latent_kw == pytest.approx(
            untreated.infiltration_latent_kw - 3.0
        )
        assert treated.relative_humidity_pct < untreated.relative_humidity_pct

    def test_fresh_air_unit_offset_never_becomes_humidification(self):
        """抵扣不得反向：室内比室外湿时，新风机组能力不应变成加湿"""
        room = RoomThermalConfig(volume_m3=300.0, occupancy_people=0)
        balance = step_room_humidity(
            current_rh_pct=80.0,
            current_temp_c=26.0,
            next_temp_c=26.0,
            outdoor_temp_c=20.0,
            outdoor_rh_pct=30.0,
            latent_cooling_kw=0.0,
            config=room,
            step_seconds=60.0,
            fresh_air_m3_h=1200.0,
            fresh_air_latent_capacity_kw=50.0,
        )

        # 干燥室外空气本就在带走水分，抵扣项不参与
        assert balance.infiltration_latent_kw < 0.0

    def test_humidity_balance_never_publishes_non_finite_terms(self):
        """非有限的冷量或风量不得把 NaN 带进分项读数

        相对湿度有钳制兜底，但四个分项是原样输出的，
        一旦带出非有限值就会污染快照。
        """
        room = RoomThermalConfig(volume_m3=300.0, occupancy_people=8)
        base = {
            "current_rh_pct": 55.0,
            "current_temp_c": 26.0,
            "next_temp_c": 26.0,
            "outdoor_temp_c": 33.0,
            "outdoor_rh_pct": 70.0,
            "config": room,
            "step_seconds": 60.0,
        }
        hostile = (
            {"latent_cooling_kw": math.nan, "fresh_air_m3_h": 240.0},
            {"latent_cooling_kw": math.inf, "fresh_air_m3_h": 240.0},
            {"latent_cooling_kw": 0.0, "fresh_air_m3_h": math.nan},
            {"latent_cooling_kw": 0.0, "fresh_air_m3_h": math.inf},
            {
                "latent_cooling_kw": 0.0,
                "fresh_air_m3_h": 240.0,
                "fresh_air_latent_capacity_kw": math.nan,
            },
        )

        for overrides in hostile:
            balance = step_room_humidity(**base, **overrides)
            for value in (
                balance.relative_humidity_pct,
                balance.humidity_ratio,
                balance.infiltration_latent_kw,
                balance.internal_latent_kw,
                balance.dehumidification_kw,
                balance.net_latent_kw,
            ):
                assert math.isfinite(value), overrides

    def test_humidity_demand_acts_within_subcool_allowance(self):
        """湿度需求在过冷带宽内有效，压到除湿下限即停

        制冷除湿必须让送风低于室内露点，房间因此必然低于目标温度；模型无
        再热盘管，这份过冷无法回收，故只在 ``AUTO_DEHUMIDIFICATION_SUBCOOL_C``
        允许的带宽内继续要冷量，到达下限就停，避免一味过冷。
        """
        session = SimulationSession(
            build_layout(),
            replace(DEMO_CONFIG, target_humidity_pct=30.0, automatic_control=True),
        )
        # 室温在目标与下限之间：允许继续除湿
        session._room_temps[ROOM_ID] = (
            session.config.target_temp_c - AUTO_DEHUMIDIFICATION_SUBCOOL_C / 2.0
        )
        session._room_humidity_pct[ROOM_ID] = 80.0
        assert session._room_humidity_demand(ROOM_ID) > 0.0

        # 已到除湿下限：不再增加冷量
        session._room_temps[ROOM_ID] = (
            session.config.target_temp_c - AUTO_DEHUMIDIFICATION_SUBCOOL_C
        )
        assert session._room_humidity_demand(ROOM_ID) == 0.0

    def test_runtime_config_update_preserves_dynamic_and_manual_state(self):
        session = SimulationSession(
            build_layout(),
            replace(DEMO_CONFIG, initial_room_temp_c=30.0),
        )
        session.set_controls("pump-chilled", {"speed_pct": 55.0})
        before = session.step()
        changed = replace(
            session.config,
            target_temp_c=22.0,
            outdoor_base_temp_c=38.0,
            outdoor_humidity_pct=88.0,
        )

        session.update_runtime_config(changed)
        after = session.step()

        assert after.tick == before.tick + 1
        assert after.simulation_seconds > before.simulation_seconds
        assert after.rooms[ROOM_ID].temperature_c != 30.0
        assert after.rooms[ROOM_ID].target_temp_c == 22.0
        assert after.ambient.relative_humidity_pct == 88.0
        assert session.control_source("pump-chilled") == "manual_override"
        assert session.controls["pump-chilled"]["speed_pct"] == 55.0

        with pytest.raises(ValueError, match="mode changes require"):
            session.update_runtime_config(
                replace(changed, mode=SimulationMode.HEATING)
            )

    def test_cooling_supply_temperature_advances_once_per_tick(self):
        config = replace(DEMO_CONFIG, step_seconds=60.0, initial_room_temp_c=30.0)
        session = SimulationSession(build_layout(), config)

        first = session.step()
        expected = advance_temperature(
            30.0,
            7.0,
            config.step_seconds,
            WATER_TEMPERATURE_TIME_CONSTANT_SECONDS,
        )

        assert session.snapshot.tick == 1
        assert first.supply_water_temp_c == pytest.approx(expected)
        assert 7.0 < first.supply_water_temp_c < 30.0
        assert first.hot_water_supply_temp_c == 30.0

    def test_cooling_supply_temperature_is_monotonic_and_turns_smoothly(self):
        config = replace(DEMO_CONFIG, step_seconds=60.0, initial_room_temp_c=30.0)
        session = SimulationSession(build_layout(), config)
        temperatures = [session.step().supply_water_temp_c for _ in range(10)]

        assert temperatures == sorted(temperatures, reverse=True)
        assert all(7.0 < temperature < 30.0 for temperature in temperatures)

        previous = temperatures[-1]
        session.set_controls("chiller-1", {"supply_temp_c": 15.0})
        redirected = session.step().supply_water_temp_c
        assert previous < redirected < 15.0

    def test_heating_supply_temperature_is_monotonic_and_loop_isolated(self):
        session = SimulationSession(
            build_heating_layout(),
            replace(
                DEMO_CONFIG,
                step_seconds=60.0,
                mode=SimulationMode.HEATING,
                initial_room_temp_c=16.0,
                target_temp_c=22.0,
                outdoor_base_temp_c=5.0,
            ),
        )

        snapshots = [session.step() for _ in range(4)]
        hot_temperatures = [item.hot_water_supply_temp_c for item in snapshots]

        assert hot_temperatures == sorted(hot_temperatures)
        assert all(16.0 < temperature < 45.0 for temperature in hot_temperatures)
        assert all(
            16.0 <= item.supply_water_temp_c < item.hot_water_supply_temp_c
            for item in snapshots
        )

    def test_changing_chiller_model_changes_readings(self):
        """换冷机型号必须改变系统读数——legacy 实现里这条是断的"""
        small = run(session_for(build_layout(chiller_model="SCWE-90H+B")))
        large = run(session_for(build_layout(chiller_model="SCWE-200H+B")))

        assert small.installed_capacity_kw != large.installed_capacity_kw
        assert (
            small.devices["chiller-1"].model_name
            != large.devices["chiller-1"].model_name
        )

    def test_source_capacity_above_terminal_delivery_does_not_invent_load(self):
        """冷源不得虚构出末端额定能力之外的冷量。

        本测试原断言"换更大冷机室温不变"，那是旧盘管模型的产物：旧式
        `capacity·clamp(ΔT/12,0,1)` 在 ΔT>=12 K 时饱和，供水温完全不参与
        换热，因此冷机大小只影响供水温读数、不影响末端出力。

        盘管改为 ε-NTU 后供水温按物理进入换热，冷机更大 -> 供水温更低 ->
        末端出力更高 -> 室温更低，这正是本测试第 508 行早就在断言的方向
        （large.supply_water_temp_c < small.supply_water_temp_c）的必然结果。

        真正需要守住的约束是：末端出力不得越过其额定能力，冷源再大也不能
        凭空造出末端额定之外的冷量。下面按此重新钉死。
        """
        config = replace(DEMO_CONFIG, design_cooling_load_kw=500.0)
        small = run(SimulationSession(build_layout("SCWE-90H+B"), config), 60)
        large = run(SimulationSession(build_layout("SCWE-200H+B"), config), 60)

        # 冷源更大仍给出更低供水温、更低负荷率（方向不变）
        assert large.supply_water_temp_c < small.supply_water_temp_c
        assert (
            large.devices["chiller-1"].values["load_ratio"]
            < small.devices["chiller-1"].values["load_ratio"]
        )

        # 供水温更低使末端出力更高，但不得越过末端额定能力上限。
        terminal_capacity_kw = _terminal_rated_capacity_kw()
        for session in (small, large):
            delivered_kw = sum(
                state.values.get("cooling_kw", 0.0)
                for state in session.devices.values()
                if state.role == "terminal"
            )
            assert delivered_kw <= terminal_capacity_kw * 1.001, (
                f"末端出力 {delivered_kw:.3f} kW 越过额定 {terminal_capacity_kw:.3f} kW"
            )

        # 冷源充裕时末端出力应更接近其额定能力，而不是被冷源拖住。
        #
        # 注意比较的是**显热**出力而非 `cooling_load_kw`：后者含潜热分量，
        # 而潜热取决于房间湿度，两台冷机因供水温不同会走出不同的湿度轨迹，
        # 用总负荷比较会把"湿度不同"误判成"能力不同"。
        small_sensible = run(SimulationSession(build_layout("SCWE-90H+B"), config), 60)
        large_sensible = run(SimulationSession(build_layout("SCWE-200H+B"), config), 60)
        assert _terminal_sensible_kw(large_sensible) >= _terminal_sensible_kw(
            small_sensible
        )

    def test_design_load_caps_terminal_demand_and_keeps_source_energy_conserved(self):
        session = SimulationSession(
            build_layout(),
            replace(
                DEMO_CONFIG,
                step_seconds=60.0,
                initial_room_temp_c=30.0,
                design_cooling_load_kw=5.0,
            ),
        )

        snapshot = session.step()
        terminal_cooling = snapshot.devices["ahu-1"].values["cooling_kw"]
        chiller_cooling = snapshot.devices["chiller-1"].values["cooling_kw"]

        assert snapshot.cooling_load_kw == pytest.approx(5.0)
        assert terminal_cooling == pytest.approx(snapshot.cooling_load_kw)
        assert chiller_cooling == pytest.approx(snapshot.cooling_load_kw)

    def test_design_load_must_be_finite(self):
        with pytest.raises(ValueError, match="finite and non-negative"):
            replace(DEMO_CONFIG, design_cooling_load_kw=math.nan)

    def test_changing_pump_model_changes_flow(self):
        """泵额定点改变水流量；正式目录当前只有一台泵，因此使用测试额定点。"""
        base = build_layout()

        def layout_with_flow(flow_m3_h: float) -> PlantLayout:
            return replace(
                base,
                devices=tuple(
                    replace(node, rated=replace(node.rated, flow_m3_h=flow_m3_h))
                    if node.instance_id in {"pump-chilled", "pump-cooling"}
                    else node
                    for node in base.devices
                ),
            )

        small = run(session_for(layout_with_flow(10.0)), 10)
        large = run(session_for(layout_with_flow(30.0)), 10)

        assert large.chilled_flow_m3_h > small.chilled_flow_m3_h

    def test_throttling_chilled_pump_widens_delta_t(self):
        """降冷冻泵转速使供回水温差拉大"""

        def delta_at(speed_pct: float) -> float:
            session = session_for()
            session.set_controls("pump-chilled", {"speed_pct": speed_pct})
            snapshot = run(session, 20)
            return snapshot.return_water_temp_c - snapshot.supply_water_temp_c

        assert delta_at(50.0) > delta_at(100.0)

    def test_throttling_cooling_pump_raises_condenser_temp(self):
        """降冷却泵转速抬高冷凝温度"""

        def condenser_at(speed_pct: float) -> float:
            session = session_for()
            session.set_controls("pump-cooling", {"speed_pct": speed_pct})
            return run(session, 20).condenser_water_temp_c

        assert condenser_at(50.0) > condenser_at(100.0)

    def test_lower_supply_setpoint_increases_cooling(self):
        """降低出水温设定值提高末端制冷量"""

        def load_at(supply_temp_c: float) -> float:
            session = session_for()
            session.set_controls("chiller-1", {"supply_temp_c": supply_temp_c})
            return run(session, 20).cooling_load_kw

        assert load_at(6.0) > load_at(13.0)


class TestSessionGuards:
    """异常与边界"""

    def test_disabling_chiller_stops_cooling(self):
        """关掉冷机后不应再有制冷量，室温转为回升"""
        session = session_for()
        run(session, 20)
        cooled = session.snapshot.rooms[ROOM_ID].temperature_c

        session.set_controls("chiller-1", {"enabled": 0.0})
        after = run(session, 20)

        assert after.cooling_load_kw == 0.0
        assert after.rooms[ROOM_ID].temperature_c > cooled
        assert after.return_water_temp_c == after.supply_water_temp_c
        assert after.devices["ahu-1"].values["cooling_kw"] == 0.0

    def test_stopping_chilled_pump_stops_cooling(self):
        """冷冻水流量为零时同样不应产生制冷量"""
        session = session_for()
        session.set_controls("pump-chilled", {"speed_pct": 0.0})
        snapshot = run(session, 10)

        assert snapshot.chilled_flow_m3_h == 0.0
        assert snapshot.cooling_load_kw == 0.0
        assert snapshot.return_water_temp_c == snapshot.supply_water_temp_c
        assert "no_chilled_flow" in snapshot.alarms

    def test_zero_water_flow_is_not_replaced_by_rated_flow(self):
        """停泵后的显式零流量不能被设备额定流量哨兵覆盖。"""
        session = session_for()
        session.set_controls("pump-cooling", {"speed_pct": 0.0})

        snapshot = run(session, 2)

        assert snapshot.cooling_flow_m3_h == 0.0
        assert snapshot.devices["tower-1"].values["flow_m3_h"] == 0.0

    def test_stopped_tower_does_not_drive_condenser_temperature(self):
        """停机冷却塔不能以零风机逼近度参与冷凝温度最大值。"""
        layout = build_layout()
        second_tower = DeviceNode("tower-2", resolve_rated_spec_by_type("冷却塔", None))
        session = session_for(replace(layout, devices=(*layout.devices, second_tower)))
        session.set_controls("tower-1", {"enabled": 0.0})

        snapshot = run(session, 2)

        active_entering = snapshot.devices["tower-2"].values["entering_water_temp_c"]
        assert snapshot.condenser_water_temp_c == active_entering

    def test_active_count_scales_banked_equipment(self):
        """设备组运行台数按整机比例缩放容量、流量与功率。"""
        per_unit = resolve_rated_spec_by_type("循环泵", "EC100")
        node = DeviceNode("pump-bank", per_unit, quantity=3, water_loop="chilled")
        session = SimulationSession(PlantLayout(devices=(node,)), DEMO_CONFIG)
        full = session.step().devices["pump-bank"].values

        session.set_controls("pump-bank", {"active_count": 1.0})
        partial = session.step().devices["pump-bank"].values

        assert partial["flow_m3_h"] == pytest.approx(full["flow_m3_h"] / 3.0)
        assert partial["power_kw"] == pytest.approx(full["power_kw"] / 3.0)

        accepted = session.set_controls("pump-bank", {"active_count": 1.4})
        assert accepted["active_count"] == 1.0

    def test_standby_mode_disables_everything(self):
        """待机模式下所有设备停机"""
        session = SimulationSession(
            build_layout(), replace(DEMO_CONFIG, mode=SimulationMode.STANDBY)
        )
        snapshot = run(session, 5)

        assert snapshot.cooling_load_kw == 0.0

    def test_mode_never_delivers_opposite_water_side_load(self):
        cooling = SimulationSession(
            build_layout(),
            replace(DEMO_CONFIG, step_seconds=60.0, initial_room_temp_c=10.0),
        )
        cooling.set_controls("chiller-1", {"supply_temp_c": 15.0})
        cooling_snapshot = cooling.step()

        heating = SimulationSession(
            build_heating_layout(),
            replace(
                DEMO_CONFIG,
                step_seconds=60.0,
                mode=SimulationMode.HEATING,
                initial_room_temp_c=40.0,
            ),
        )
        heating.set_controls("heat-source-1", {"supply_temp_c": 30.0})
        heating_snapshot = heating.step()

        assert cooling_snapshot.cooling_load_kw == 0.0
        assert cooling_snapshot.heating_load_kw == 0.0
        assert heating_snapshot.cooling_load_kw == 0.0
        assert heating_snapshot.heating_load_kw == 0.0

    def test_heating_uses_heat_source_hot_loop_and_warms_room(self):
        session = SimulationSession(
            build_heating_layout(),
            replace(
                DEMO_CONFIG,
                mode=SimulationMode.HEATING,
                initial_room_temp_c=16.0,
                target_temp_c=22.0,
                outdoor_base_temp_c=5.0,
            ),
        )
        initial = session.snapshot.rooms[ROOM_ID].temperature_c
        snapshot = run(session, 5)

        assert snapshot.heating_load_kw > 0.0
        assert snapshot.hot_water_flow_m3_h > 0.0
        assert snapshot.hot_water_supply_temp_c > snapshot.hot_water_return_temp_c
        assert snapshot.rooms[ROOM_ID].heating_kw > 0.0
        assert snapshot.rooms[ROOM_ID].temperature_c > initial
        assert snapshot.devices["heat-source-1"].values["power_kw"] > 0.0

    @pytest.mark.parametrize(
        ("mode", "enabled", "disabled"),
        [
            # 制冷工况热泵换向供冷（不再是互锁对象）；互锁对象改为
            # 热水泵泵——环路仍与工况绑定。
            (SimulationMode.COOLING, "chiller-1", "pump-hot"),
            (SimulationMode.HEATING, "heat-source-1", "chiller-1"),
            (SimulationMode.VENTILATION, "ahu-1", "chiller-1"),
            (SimulationMode.STANDBY, None, "heat-source-1"),
        ],
    )
    def test_mode_interlocks_override_manual_enable(self, mode, enabled, disabled):
        session = SimulationSession(
            build_heating_layout(), replace(DEMO_CONFIG, mode=mode)
        )
        session.set_controls(disabled, {"enabled": 1.0})
        snapshot = session.step()

        if enabled is not None:
            assert snapshot.devices[enabled].values["enabled"] == 1.0
        assert snapshot.devices[disabled].values["enabled"] == 0.0

    def test_cooling_mode_heat_pump_is_enabled_and_serves_cooling(self):
        """制冷工况：热泵换向供冷，读数与冷机同构（cooling_kw/reject）。

        这是热泵制冷任务的核心回归——互锁不再禁用热泵，纯热泵项目
        也能提供冷源。
        """
        session = SimulationSession(
            build_heating_layout(), replace(DEMO_CONFIG, mode=SimulationMode.COOLING)
        )
        snapshot = session.step()
        heat = snapshot.devices["heat-source-1"].values
        assert heat["enabled"] == 1.0
        assert heat["cooling_kw"] > 0.0
        assert heat["heat_reject_kw"] > heat["cooling_kw"]
        assert heat["heating_kw"] == 0.0
        assert snapshot.installed_capacity_kw > 0.0

    def test_automatic_control_is_bounded_and_manual_override_is_device_scoped(self):
        session = SimulationSession(
            build_layout(),
            replace(
                DEMO_CONFIG,
                automatic_control=True,
                initial_room_temp_c=30.0,
                target_temp_c=26.0,
            ),
        )
        first = session.step()
        second = session.step()
        first_speed = first.devices["pump-chilled"].values["speed_pct"]
        second_speed = second.devices["pump-chilled"].values["speed_pct"]
        assert 0.0 <= second_speed <= 100.0
        assert abs(second_speed - first_speed) <= 8.0

        # 控制必须在有限步内实际动作，且每步不超过 8% 的速率限幅。
        # 起始室温 30℃、目标 26℃ 时偏差很大，前若干 tick 需求会顶到 100%，
        # 此时相邻两步相同属于正常的速率限幅饱和，不代表控制器失效。
        # 因此扫到控制真正开始下调为止，而不是要求第 2 步就必须不同。
        speeds = [first_speed, second_speed]
        for _ in range(10):
            if speeds[-1] != speeds[-2]:
                break
            speeds.append(session.step().devices["pump-chilled"].values["speed_pct"])
        assert len(set(speeds)) > 1, f"控制器始终未动作：{speeds}"
        for earlier, later in itertools.pairwise(speeds):
            assert abs(later - earlier) <= 8.0

        session.set_controls("pump-chilled", {"speed_pct": 50.0})
        manual = session.step()
        assert manual.devices["pump-chilled"].values["speed_pct"] == 50.0
        assert session.control_source("pump-chilled") == "manual_override"
        assert session.control_source("tower-1") == "auto"

        faulted = session.step({"pump-chilled": {"speed_pct": 45.0}})
        recovered = session.step()
        assert faulted.devices["pump-chilled"].values["speed_pct"] == 45.0
        assert recovered.devices["pump-chilled"].values["speed_pct"] == 50.0

        session.reset_controls("pump-chilled")
        assert session.control_source("pump-chilled") == "auto"

    def test_automatic_control_defaults_off_for_steady_compatibility(self):
        session = session_for()
        snapshot = session.step()

        assert snapshot.devices["pump-chilled"].values["speed_pct"] == 100.0

    def test_automatic_control_deadband_keeps_dynamic_equipment_stopped(self):
        quiet_room = RoomThermalConfig(
            thermal_capacity_kj_k=8000.0,
            envelope_conductance_kw_k=0.0,
            occupancy_people=0,
            lighting_w=0.0,
            equipment_w=0.0,
        )
        layout = replace(
            build_layout(), rooms=(RoomNode(ROOM_ID, "示例房间", quiet_room),)
        )
        session = SimulationSession(
            layout,
            replace(
                DEMO_CONFIG,
                automatic_control=True,
                initial_room_temp_c=26.1,
                target_temp_c=26.0,
                outdoor_base_temp_c=26.0,
            ),
        )

        first = session.step()
        second = session.step()
        assert first.devices["chiller-1"].values["enabled"] == 0.0
        assert second.devices["chiller-1"].values["enabled"] == 0.0

    def test_undersized_plant_raises_supply_temperature(self):
        """装机偏小时供水温爬升，这是选型不足最直观的表现"""
        layout = PlantLayout(
            devices=(
                DeviceNode(
                    "chiller-1", resolve_rated_spec_by_type("冷源机组", "SCWE-90H+B")
                ),
                DeviceNode(
                    "pump-chilled",
                    resolve_rated_spec_by_type("循环泵", "EC100"),
                    water_loop="chilled",
                ),
                DeviceNode(
                    "pump-cooling",
                    resolve_rated_spec_by_type("循环泵", "EC100"),
                    water_loop="cooling",
                ),
                DeviceNode("tower-1", resolve_rated_spec_by_type("冷却塔", None)),
            )
            + tuple(
                DeviceNode(
                    f"ahu-{index}",
                    synthetic_ahu_rated(),
                    room_id=ROOM_ID,
                )
                for index in range(30)
            ),
            rooms=(RoomNode(ROOM_ID, "示例房间", DEMO_ROOM),),
        )
        snapshot = run(session_for(layout), 5)

        assert snapshot.supply_water_temp_c > 7.0
        assert "capacity_deficit" in snapshot.alarms
        terminal_cooling = sum(
            reading.values.get("cooling_kw", 0.0)
            for instance_id, reading in snapshot.devices.items()
            if instance_id.startswith("ahu-")
        )
        assert snapshot.cooling_load_kw == pytest.approx(terminal_cooling)
        assert snapshot.devices["chiller-1"].values["cooling_kw"] == pytest.approx(
            terminal_cooling
        )

    def test_controls_are_clamped_to_range(self):
        """越界控制值按可调范围钳制"""
        session = session_for()

        accepted = session.set_controls("chiller-1", {"supply_temp_c": 99.0})
        assert accepted["supply_temp_c"] == 15.0

        accepted = session.set_controls("pump-chilled", {"speed_pct": -20.0})
        assert accepted["speed_pct"] == 0.0

    def test_unknown_control_is_ignored(self):
        """不支持的参数名不写入状态"""
        accepted = session_for().set_controls("chiller-1", {"not_a_control": 1.0})

        assert accepted == {}

    def test_unknown_device_raises(self):
        """向不存在的设备下发控制应报错而不是静默丢弃"""
        with pytest.raises(KeyError):
            session_for().set_controls("nope", {"enabled": 0.0})

    def test_degraded_parameters_are_reported(self):
        """使用降级参数时给出告警并在读数里标出来源"""
        layout = PlantLayout(
            devices=(
                DeviceNode(
                    "chiller-1",
                    resolve_rated_spec_by_type("冷源机组", "NOT_IN_CATALOG"),
                ),
            ),
            rooms=(RoomNode(ROOM_ID, "示例房间", DEMO_ROOM),),
        )
        snapshot = run(session_for(layout), 2)

        assert "degraded_parameters" in snapshot.alarms
        assert snapshot.devices["chiller-1"].parameter_origin == "type_default"

    def test_empty_layout_does_not_crash(self):
        """空系统应能安全推进"""
        snapshot = run(session_for(PlantLayout()), 3)

        assert snapshot.cooling_load_kw == 0.0
        assert snapshot.devices == {}


class TestSteady:
    """稳态求解"""

    def test_converges_to_fixed_point(self):
        """稳态即动态核的不动点"""
        result = solve_steady(build_layout(), DEMO_CONFIG)

        assert result.converged is True
        assert result.ticks < 500
        assert result.final_drift_c <= 0.01

    def test_active_water_temperature_prevents_early_convergence(self):
        result = solve_steady(
            build_layout(),
            replace(DEMO_CONFIG, step_seconds=60.0, initial_room_temp_c=30.0),
            max_ticks=1,
            tolerance_c=1.0,
        )

        assert result.converged is False
        assert result.final_drift_c > 1.0

    def test_no_room_and_no_active_load_converges_in_one_tick(self):
        result = solve_steady(PlantLayout(), DEMO_CONFIG)

        assert result.converged is True
        assert result.ticks == 1
        assert result.final_drift_c == 0.0

    def test_weather_is_frozen_at_design_hour(self):
        """稳态必须冻结气象，否则室温跟着日周期摆动，不存在不动点"""
        result = solve_steady(build_layout(), DEMO_CONFIG)

        assert result.snapshot.ambient.hour_of_day == DESIGN_HOUR_OF_DAY

    def test_steady_temperature_is_stable(self):
        """收敛后继续推进温度不再明显变化

        阈值不能取到收敛容差量级：判据是单步漂移 ≤ tolerance，而几何衰减的
        剩余总位移约为 `tolerance / (1 - 衰减因子)`，比单步漂移大一个量级。
        这里只需证明温度不再跑掉，而不是完全静止。
        """
        design_config = replace(DEMO_CONFIG, fixed_hour_of_day=DESIGN_HOUR_OF_DAY)
        result = solve_steady(build_layout(), design_config)
        temperature = result.snapshot.rooms[ROOM_ID].temperature_c

        session = SimulationSession(build_layout(), design_config)
        for _ in range(result.ticks + 20):
            session.step()

        assert abs(session.snapshot.rooms[ROOM_ID].temperature_c - temperature) < 0.3

    def test_iteration_limit_is_enforced(self):
        """达到迭代上限时返回未收敛而不是挂死"""
        result = solve_steady(
            build_layout(), DEMO_CONFIG, max_ticks=2, tolerance_c=1e-9
        )

        assert result.converged is False
        assert result.ticks == 2

    def test_converged_result_survives_further_integration(self):
        """声称收敛的结果必须是真不动点：继续积分不得漂移

        原判据只看**单步**漂移 ≤ tolerance。若系统处于极限环，每步变化在
        每个周期内两次穿过零——也就是在每个折返点都会满足该判据，于是
        `converged=True` 会在振荡的折返点被误报。实测在四层样例上，被判定
        收敛的点继续积分后漂移达 3.34 K，远超声称的 0.0098 K。

        本测试用**独立于判据本身**的方式复核：回到收敛点后继续推进若干 tick，
        看温度是否留在容差带内。

        注意：`solve_steady` 会把 `fixed_hour_of_day=None` 替换成设计时刻
        （见 steady.py 的 `DESIGN_HOUR_OF_DAY`），复现时必须用同一份冻结配置，
        否则气象按日周期变化，算的根本不是同一个不动点。
        """
        frozen = replace(DEMO_CONFIG, fixed_hour_of_day=DESIGN_HOUR_OF_DAY)
        result = solve_steady(build_layout(), frozen)
        assert result.converged is True

        reported = result.snapshot.rooms[ROOM_ID].temperature_c
        session = SimulationSession(build_layout(), frozen)
        for _ in range(result.ticks):
            session.step()

        # 收敛点必须可精确复现，否则"继续积分"验证的就不是同一个点
        assert session.snapshot.rooms[ROOM_ID].temperature_c == pytest.approx(reported)

        # 继续积分：真不动点应停在原处，极限环折返点则会明显走开
        session2 = SimulationSession(build_layout(), frozen)
        for _ in range(result.ticks + 200):
            session2.step()
        drifted = abs(session2.snapshot.rooms[ROOM_ID].temperature_c - reported)

        assert drifted <= 0.1, (
            f"声称收敛的点继续积分后漂移 {drifted:.4f} K，"
            "说明 converged=True 是在极限环折返点上误报的"
        )

    def test_oscillating_system_is_not_reported_as_converged(self, monkeypatch):
        """A periodic trajectory must not pass at a stationary turning point.

        Use a controlled trajectory so improved physical/control models do not
        turn this convergence-criterion regression into a test requiring a
        particular teaching sample to remain unstable forever.
        """
        from app.simcore import steady as steady_module

        class PeriodicSession(SimulationSession):
            def step(self, transient_controls=None):
                self._tick += 1
                temperature = 24.0 + math.cos(2.0 * math.pi * self._tick / 200.0)
                self._snapshot = replace(
                    self._snapshot,
                    tick=self._tick,
                    rooms={key: replace(room, temperature_c=temperature)
                           for key, room in self._snapshot.rooms.items()},
                )
                return self._snapshot

        monkeypatch.setattr(steady_module, "SimulationSession", PeriodicSession)
        result = solve_steady(build_layout(), SimulationConfig(), max_ticks=500)
        assert result.converged is False
        assert result.final_drift_c > 0.01

    def test_invalid_limits_rejected(self):
        """非法迭代参数直接报错"""
        with pytest.raises(ValueError):
            solve_steady(build_layout(), max_ticks=0)
        with pytest.raises(ValueError):
            solve_steady(build_layout(), tolerance_c=0.0)
        with pytest.raises(ValueError):
            solve_steady(build_layout(), humidity_tolerance_pct=math.nan)

    def test_humidity_oscillation_prevents_convergence_with_stationary_temperatures(self, monkeypatch):
        from app.simcore import steady as steady_module

        class HumidSession(SimulationSession):
            def step(self, transient_controls=None):
                self._tick += 1
                rh = 55.0 + 5.0 * math.cos(2.0 * math.pi * self._tick / 200.0)
                self._snapshot = replace(
                    self._snapshot, tick=self._tick,
                    rooms={key: replace(room, temperature_c=24.0, relative_humidity_pct=rh)
                           for key, room in self._snapshot.rooms.items()},
                )
                return self._snapshot

        monkeypatch.setattr(steady_module, "SimulationSession", HumidSession)
        result = solve_steady(build_layout(), SimulationConfig(), max_ticks=500)
        assert result.converged is False
        assert result.final_drift_c == 0.0
        assert result.final_humidity_drift_pct > 0.05
