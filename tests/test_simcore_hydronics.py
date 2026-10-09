"""Topology-aware teaching hydronics and device coupling tests."""

import math

import pytest

from app.schemas import ConnectionInput, RoomInput, SystemEvaluateRequest
from app.simcore.devices import pump_state, terminal_state
from app.simcore.hydronics import (
    MAX_HYDRONIC_DELTA_T_C,
    WATER_KW_PER_M3H_K,
    HydronicBranch,
    HydronicDemand,
    HydronicThermalBranch,
    advance_temperature,
    allocate_branch_flows,
    branch_return_temperature,
    mixed_return_temperature,
    pipe_resistance_index,
    source_supply_temperature,
)
from app.simcore.project import build_project_layout
from app.simcore.rated import resolve_rated_spec_by_type
from app.simcore.session import (
    DeviceNode,
    PlantLayout,
    SimulationConfig,
    SimulationSession,
)


def test_capped_weighted_allocation_conserves_and_redistributes_remaining_flow():
    demands = (
        HydronicDemand("small", flow_cap_m3_h=1.0, weight=1.0),
        HydronicDemand("large", flow_cap_m3_h=10.0, weight=1.0),
    )

    flows = allocate_branch_flows(8.0, demands)

    assert flows == pytest.approx({"large": 7.0, "small": 1.0})
    assert sum(flows.values()) == pytest.approx(8.0)


def test_allocation_respects_caps_resistance_direction_and_input_order():
    low_resistance = pipe_resistance_index(20.0, 80.0, 1.0)
    high_resistance = pipe_resistance_index(40.0, 80.0, 1.0)
    demands = (
        HydronicDemand("low", 10.0, 10.0 / math.sqrt(low_resistance)),
        HydronicDemand("high", 10.0, 10.0 / math.sqrt(high_resistance)),
        HydronicDemand("closed", 0.0, 0.0),
    )

    forward = allocate_branch_flows(10.0, demands)
    reverse = allocate_branch_flows(10.0, tuple(reversed(demands)))

    assert forward == reverse
    assert forward["low"] > forward["high"]
    assert forward["closed"] == 0.0
    assert all(0.0 <= flow <= 10.0 for flow in forward.values())


@pytest.mark.parametrize("available_flow", [0.0, -1.0, math.nan, math.inf])
def test_allocation_rejects_non_positive_or_non_finite_available_flow(
    available_flow: float,
):
    flows = allocate_branch_flows(
        available_flow,
        (HydronicDemand("branch", flow_cap_m3_h=5.0, weight=1.0),),
    )

    assert flows == {"branch": 0.0}


def test_invalid_demand_values_and_empty_input_remain_finite():
    flows = allocate_branch_flows(
        10.0,
        (
            HydronicDemand("nan-cap", math.nan, 1.0),
            HydronicDemand("inf-weight", 5.0, math.inf),
            HydronicDemand("valid", 4.0, 2.0),
        ),
    )

    assert flows == {"inf-weight": 0.0, "nan-cap": 0.0, "valid": 4.0}
    assert allocate_branch_flows(10.0, ()) == {}
    assert all(math.isfinite(value) and value >= 0.0 for value in flows.values())


def test_large_finite_weights_cannot_overflow_the_available_flow():
    available_flow = 1e142
    demands = (
        HydronicDemand("dominant", flow_cap_m3_h=1e160, weight=1e175),
        HydronicDemand("secondary", flow_cap_m3_h=1e160, weight=1e170),
    )

    flows = allocate_branch_flows(available_flow, demands)

    assert all(math.isfinite(flow) and 0.0 <= flow <= 1e160 for flow in flows.values())
    assert sum(flows.values()) == pytest.approx(available_flow)
    assert flows["dominant"] > flows["secondary"]


def test_temperature_response_moves_toward_target_without_overshoot():
    cooling = advance_temperature(30.0, 7.0, 60.0, 300.0)
    heating = advance_temperature(16.0, 45.0, 60.0, 300.0)

    assert 7.0 < cooling < 30.0
    assert 16.0 < heating < 45.0
    assert advance_temperature(cooling, 7.0, 60.0, 300.0) < cooling
    assert advance_temperature(heating, 45.0, 60.0, 300.0) > heating


@pytest.mark.parametrize("heating,return_c,outlet_c", [(False, 17.0, 7.0), (True, 35.0, 45.0)])
def test_source_outlet_matches_hand_calculated_water_energy(heating, return_c, outlet_c):
    # 3.6 m³/h = 1 kg/s; 41.87 kW / (1 kg/s × 4.187 kJ/kg/K) = 10 K.
    assert source_supply_temperature(return_c, 3.6, 41.87, heating=heating) == pytest.approx(outlet_c)
    assert source_supply_temperature(return_c, 3.6, 0.0, heating=heating) == return_c
    assert source_supply_temperature(return_c, 0.0, 0.0, heating=heating) == return_c


def test_temperature_response_is_stable_across_step_partitioning():
    one_step = advance_temperature(30.0, 7.0, 60.0, 300.0)
    two_steps = advance_temperature(
        advance_temperature(30.0, 7.0, 30.0, 300.0),
        7.0,
        30.0,
        300.0,
    )

    assert two_steps == pytest.approx(one_step)
    assert advance_temperature(30.0, 7.0, 1e300, 300.0) == 7.0


@pytest.mark.parametrize("invalid", [0.0, -1.0, math.nan, math.inf])
def test_temperature_response_rejects_invalid_time_inputs(invalid: float):
    assert advance_temperature(20.0, 7.0, invalid, 300.0) == 20.0
    assert advance_temperature(20.0, 7.0, 60.0, invalid) == 20.0


def test_temperature_response_sanitizes_non_finite_temperatures():
    assert advance_temperature(math.nan, 7.0, 60.0, 300.0) == 7.0
    assert advance_temperature(20.0, math.inf, 60.0, 300.0) == 20.0
    assert math.isfinite(
        advance_temperature(math.inf, math.nan, 60.0, 300.0)
    )


def test_branch_return_temperature_has_physical_direction_and_bounded_delta():
    cooling_return = branch_return_temperature(
        7.0, flow_m3_h=10.0, load_kw=58.0, heating=False
    )
    heating_return = branch_return_temperature(
        45.0, flow_m3_h=10.0, load_kw=58.0, heating=True
    )
    bounded = branch_return_temperature(
        7.0, flow_m3_h=0.01, load_kw=1000.0, heating=False
    )

    assert cooling_return > 7.0
    assert heating_return < 45.0
    assert cooling_return - 7.0 == pytest.approx(
        58.0 / (WATER_KW_PER_M3H_K * 10.0)
    )
    assert bounded - 7.0 == MAX_HYDRONIC_DELTA_T_C


@pytest.mark.parametrize("heating", [False, True])
def test_mixed_return_is_flow_weighted_order_independent_and_conserves_energy(
    heating: bool,
):
    supply = 45.0 if heating else 7.0
    branches = (
        HydronicThermalBranch("branch-b", flow_m3_h=20.0, load_kw=80.0),
        HydronicThermalBranch("branch-a", flow_m3_h=10.0, load_kw=40.0),
    )

    forward = mixed_return_temperature(supply, branches, heating=heating)
    reverse = mixed_return_temperature(
        supply, tuple(reversed(branches)), heating=heating
    )
    delta_t = supply - forward if heating else forward - supply

    assert reverse == pytest.approx(forward)
    assert WATER_KW_PER_M3H_K * 30.0 * delta_t == pytest.approx(120.0)


def test_mixed_return_ignores_zero_and_invalid_branches():
    branches = (
        HydronicThermalBranch("zero", flow_m3_h=0.0, load_kw=100.0),
        HydronicThermalBranch("nan", flow_m3_h=math.nan, load_kw=100.0),
        HydronicThermalBranch("negative", flow_m3_h=10.0, load_kw=-1.0),
    )

    assert mixed_return_temperature(7.0, branches, heating=False) == 7.0
    assert branch_return_temperature(7.0, 0.0, 100.0, heating=False) == 7.0


def test_pipe_resistance_is_positive_finite_and_increases_with_length():
    short = pipe_resistance_index(20.0, 80.0, 2.0)
    long = pipe_resistance_index(40.0, 80.0, 2.0)

    assert 0.0 < short < long < math.inf
    assert pipe_resistance_index(math.nan, None, math.inf) == 1.0


def test_pump_actual_flow_preserves_affinity_law_at_available_flow():
    rated = resolve_rated_spec_by_type("循环泵", "EC32")

    legacy = pump_state(rated, 0.5)
    actual = pump_state(rated, 0.5, actual_flow_m3_h=10.255)

    assert actual == legacy
    assert actual.flow_m3_h == pytest.approx(10.255)
    assert actual.power_kw == pytest.approx((rated.power_kw or 0.0) * 0.5**3)


def test_pump_actual_flow_limits_power_but_keeps_speed_based_head():
    rated = resolve_rated_spec_by_type("循环泵", "EC32")

    throttled = pump_state(rated, 0.8, actual_flow_m3_h=2.0)
    zero = pump_state(rated, 0.8, actual_flow_m3_h=0.0)

    assert throttled.flow_m3_h == 2.0
    assert throttled.power_kw == pytest.approx(
        (rated.power_kw or 0.0) * 0.8**2 * 2.0 / (rated.flow_m3_h or 1.0)
    )
    assert zero.head_m == throttled.head_m
    assert zero.flow_m3_h == 0.0
    assert zero.power_kw == 0.0


def test_terminal_actual_flow_limits_capacity_without_counting_valve_twice():
    rated = resolve_rated_spec_by_type("风机盘管", "MCW200V")
    rated_flow = rated.flow_m3_h or 0.0

    full = terminal_state(
        rated,
        valve_opening_ratio=0.2,
        room_temp_c=26.0,
        supply_water_temp_c=7.0,
        actual_water_flow_m3_h=rated_flow,
    )
    half = terminal_state(
        rated,
        valve_opening_ratio=0.2,
        room_temp_c=26.0,
        supply_water_temp_c=7.0,
        actual_water_flow_m3_h=rated_flow / 2.0,
    )

    assert full.water_flow_m3_h == pytest.approx(rated_flow)
    # 流量律由 sqrt(flow_ratio) 经验式改为 ε-NTU：水量下降同时抬高管内侧热阻
    # （UA ∝ V̇^0.8）并改变 C_max，故衰减是物理推导而非经验开方。
    # 此处只锁方向与量级，具体数值由 tests/test_coil_ntu.py 钉死。
    assert 0.7 < half.cooling_kw / full.cooling_kw < 1.0

    # 本测试的核心意图：传了 actual_water_flow 时不得再乘一次阀位。
    # 直接钉死该不变量——阀位对结果必须完全没有影响（传实际流量时它只用于
    # 在缺省情况下推算流量，两者不可叠加）。原断言用 4.0 倍关系间接表达
    # 这一点，那个倍数来自旧 sqrt 经验式，随流量律更换而失效。
    for opening in (0.0, 0.2, 0.5, 1.0):
        same_flow = terminal_state(
            rated,
            valve_opening_ratio=opening,
            room_temp_c=26.0,
            supply_water_temp_c=7.0,
            actual_water_flow_m3_h=rated_flow,
        )
        assert same_flow.cooling_kw == pytest.approx(full.cooling_kw)

    # 未传实际流量时，阀位才决定流量，开度越小流量与换热量越小
    small_opening = terminal_state(rated, 0.2, 26.0, 7.0)
    assert small_opening.water_flow_m3_h < full.water_flow_m3_h
    assert small_opening.cooling_kw < full.cooling_kw


def test_terminal_zero_actual_water_flow_keeps_fan_side_running():
    rated = resolve_rated_spec_by_type("风机盘管", "MCW200V")

    state = terminal_state(
        rated,
        valve_opening_ratio=1.0,
        room_temp_c=18.0,
        supply_water_temp_c=45.0,
        fan_speed_ratio=0.7,
        actual_water_flow_m3_h=0.0,
    )

    assert state.water_flow_m3_h == 0.0
    assert state.cooling_kw == 0.0
    assert state.heating_kw == 0.0
    assert state.airflow_m3_h > 0.0
    assert state.power_kw > 0.0


def test_terminal_extreme_low_flow_respects_bounded_water_side_energy():
    rated = resolve_rated_spec_by_type("风机盘管", "MCW200V")
    flow = (rated.flow_m3_h or 0.0) * 0.001

    state = terminal_state(
        rated,
        valve_opening_ratio=1.0,
        room_temp_c=30.0,
        supply_water_temp_c=7.0,
        actual_water_flow_m3_h=flow,
    )

    assert state.cooling_kw == pytest.approx(
        WATER_KW_PER_M3H_K * flow * MAX_HYDRONIC_DELTA_T_C
    )


@pytest.mark.parametrize("actual_flow", [math.nan, math.inf, -math.inf])
def test_device_actual_flow_rejects_non_finite_values(actual_flow: float):
    pump_rated = resolve_rated_spec_by_type("循环泵", "EC32")
    terminal_rated = resolve_rated_spec_by_type("风机盘管", "MCW200V")

    pump = pump_state(pump_rated, 1.0, actual_flow_m3_h=actual_flow)
    terminal = terminal_state(
        terminal_rated,
        valve_opening_ratio=1.0,
        room_temp_c=26.0,
        supply_water_temp_c=7.0,
        actual_water_flow_m3_h=actual_flow,
    )

    assert pump.flow_m3_h == 0.0
    assert pump.power_kw == 0.0
    assert terminal.water_flow_m3_h == 0.0
    assert terminal.cooling_kw == 0.0


def test_project_without_water_topology_cannot_invent_circulation():
    project = SystemEvaluateRequest(
        devices=[
            {"id": "pump", "type": "循环泵", "name": "EC32"},
            {"id": "terminal", "type": "风机盘管", "name": "MCW200V"},
        ]
    )
    layout = build_project_layout(project).layout

    snapshot = SimulationSession(layout, SimulationConfig()).step()

    assert layout.modeled_water_loops == frozenset()
    assert layout.hydronic_branches == ()
    assert snapshot.chilled_flow_m3_h == 0.0
    assert snapshot.devices["terminal"].values["water_flow_m3_h"] == 0.0


def test_declared_loop_without_terminal_branches_does_not_invent_flow():
    project = SystemEvaluateRequest(
        devices=[{"id": "pump", "type": "循环泵", "name": "EC32"}],
        pipes=[{"id": "orphan", "system": "chilled_water"}],
    )
    layout = build_project_layout(project).layout

    snapshot = SimulationSession(layout, SimulationConfig()).step()

    assert layout.modeled_water_loops == frozenset({"chilled"})
    assert layout.hydronic_branches == ()
    assert snapshot.chilled_flow_m3_h == 0.0
    assert snapshot.devices["pump"].values["flow_m3_h"] == 0.0
    assert snapshot.devices["pump"].values["power_kw"] == 0.0


def test_any_declared_hydronic_topology_disables_other_loop_legacy_fallback():
    project = SystemEvaluateRequest(
        devices=[
            {"id": "chiller", "type": "冷源机组", "name": "YVAA-500"},
            {"id": "pump", "type": "循环泵", "name": "EC32"},
            {"id": "terminal", "type": "风机盘管", "name": "MCW200V"},
        ],
        pipes=[{"id": "hot-only", "system": "hot_water"}],
    )
    layout = build_project_layout(project).layout

    snapshot = SimulationSession(layout, SimulationConfig()).step()

    assert layout.modeled_water_loops == frozenset({"hot"})
    assert snapshot.chilled_flow_m3_h == 0.0
    assert snapshot.devices["pump"].values["flow_m3_h"] == 0.0
    assert snapshot.devices["terminal"].values["water_flow_m3_h"] == 0.0
    assert snapshot.devices["terminal"].values["cooling_kw"] == 0.0


def test_direct_device_connection_and_missing_pipe_size_use_stable_fallbacks():
    direct = SystemEvaluateRequest(
        devices=[
            {"id": "pump", "type": "循环泵", "name": "EC32"},
            {"id": "terminal", "type": "风机盘管", "name": "MCW200V"},
        ],
        connections=[
            {"from_id": "pump", "to_id": "terminal", "system": "chilled_water"}
        ],
    )
    piped = SystemEvaluateRequest(
        devices=direct.devices,
        pipes=[
            {
                "id": "branch",
                "system": "chilled_water",
                "length_m": 12.0,
            }
        ],
        connections=[
            {"from_id": "pump", "to_id": "branch", "system": "chilled_water"},
            {
                "from_id": "branch",
                "to_id": "terminal",
                "system": "chilled_water",
            },
        ],
    )

    direct_branch = build_project_layout(direct).layout.hydronic_branches[0]
    piped_branch = build_project_layout(piped).layout.hydronic_branches[0]

    assert direct_branch.resistance_index == 1.0
    assert math.isfinite(piped_branch.resistance_index)
    assert piped_branch.resistance_index > 0.0


def test_disconnected_terminal_component_cannot_receive_global_pump_flow():
    project = SystemEvaluateRequest(
        devices=[
            {"id": "chiller", "type": "冷源机组", "name": "YVAA-500"},
            {"id": "pump", "type": "循环泵", "name": "EC32"},
            {"id": "served", "type": "风机盘管", "name": "MCW200V"},
            {"id": "isolated", "type": "风机盘管", "name": "MCW200V"},
        ],
        pipes=[
            {"id": "served-pipe", "system": "chilled_water"},
            {"id": "isolated-pipe", "system": "chilled_water"},
        ],
        connections=[
            {"from_id": "chiller", "to_id": "pump", "system": "chilled_water"},
            {
                "from_id": "pump",
                "to_id": "served-pipe",
                "system": "chilled_water",
            },
            {
                "from_id": "served-pipe",
                "to_id": "served",
                "system": "chilled_water",
            },
            {
                "from_id": "served",
                "to_id": "chiller",
                "system": "chilled_water",
            },
            {
                "from_id": "isolated",
                "to_id": "isolated-pipe",
                "system": "chilled_water",
            },
        ],
    )

    project.connections.append(
        ConnectionInput(from_id="served", to_id="chiller", system="chilled_water")
    )
    project.rooms.append(RoomInput(id="served-room", area_m2=50, device_ids=["served"]))
    layout = build_project_layout(project).layout
    snapshot = SimulationSession(layout, SimulationConfig()).step()

    assert [branch.terminal_instance_id for branch in layout.hydronic_branches] == [
        "served"
    ]
    served_flow = snapshot.devices["served"].values["water_flow_m3_h"]
    assert served_flow > 0.0
    assert snapshot.devices["isolated"].values["water_flow_m3_h"] == 0.0
    assert snapshot.chilled_flow_m3_h == pytest.approx(served_flow)


def test_multiple_pumps_share_the_same_conserved_actual_loop_flow():
    pump_rated = resolve_rated_spec_by_type("循环泵", "EC32")
    terminal_rated = resolve_rated_spec_by_type("风机盘管", "MCW200V")
    layout = PlantLayout(
        devices=(
            DeviceNode("pump-a", pump_rated, water_loop="chilled"),
            DeviceNode("pump-b", pump_rated, water_loop="chilled"),
            DeviceNode("terminal", terminal_rated, quantity=10),
        ),
        hydronic_branches=(HydronicBranch("terminal", "chilled", 1.0),),
        modeled_water_loops=frozenset({"chilled"}),
    )

    snapshot = SimulationSession(layout, SimulationConfig()).step()
    pump_a = snapshot.devices["pump-a"].values["flow_m3_h"]
    pump_b = snapshot.devices["pump-b"].values["flow_m3_h"]
    terminal_flow = snapshot.devices["terminal"].values["water_flow_m3_h"]

    assert pump_a == pytest.approx(pump_b)
    assert pump_a + pump_b == pytest.approx(snapshot.chilled_flow_m3_h)
    assert snapshot.chilled_flow_m3_h == pytest.approx(terminal_flow)


def test_heat_pump_is_a_serviceable_chilled_loop_source():
    """热泵作为 chilled 环路源锚：只连热泵的末端组件不应被判不可服务。

    制冷工况热泵换向即冷源（session 容量池同口径）。若 chilled 源锚
    只认冷机，分组件布局里只连热泵的末端会拿不到流量、房间不降温。
    """
    project = SystemEvaluateRequest(
        floors=[{"id": "F1", "name": "一层", "level": 1}],
        rooms=[
            {"id": "R1", "floor_id": "F1", "name": "房A", "area_m2": 50.0,
             "device_ids": ["fcu_a"]},
            {"id": "R2", "floor_id": "F1", "name": "房B", "area_m2": 50.0,
             "device_ids": ["fcu_b"]},
        ],
        devices=[
            {"id": "chiller", "type": "冷源机组", "floor_id": "F1"},
            {"id": "hp", "type": "空气源热泵", "floor_id": "F1"},
            {"id": "pump_a", "type": "循环泵", "floor_id": "F1"},
            {"id": "pump_b", "type": "循环泵", "floor_id": "F1"},
            {"id": "fcu_a", "type": "风机盘管", "floor_id": "F1"},
            {"id": "fcu_b", "type": "风机盘管", "floor_id": "F1"},
        ],
        connections=[
            # 组件 A：泵—冷机—末端
            {"from_id": "pump_a", "to_id": "chiller", "system": "chilled_water"},
            {"from_id": "chiller", "to_id": "fcu_a", "system": "chilled_water"},
            # 组件 B：泵—热泵—末端（无冷机）
            {"from_id": "pump_b", "to_id": "hp", "system": "chilled_water"},
            {"from_id": "hp", "to_id": "fcu_b", "system": "chilled_water"},
        ],
    )
    # Both source islands need a complete return as well as a supply path.
    project.connections.extend([
        ConnectionInput(from_id="fcu_a", to_id="pump_a", system="chilled_water"),
        ConnectionInput(from_id="fcu_b", to_id="pump_b", system="chilled_water"),
    ])
    resolved = build_project_layout(project)

    # 布局层：B 组件末端有水力分支、无不可服务告警。
    assert {b.terminal_instance_id for b in resolved.layout.hydronic_branches} == {
        "fcu_a",
        "fcu_b",
    }
    assert not [
        issue
        for issue in resolved.issues
        if issue.code == "TERMINAL_WATER_UNREACHABLE"
    ]

    # 运行层：B 房间拿到冷量，热泵输出制冷。
    session = SimulationSession(resolved.layout, SimulationConfig())
    for _ in range(6):
        snapshot = session.step()
    assert snapshot.devices["fcu_b"].values["water_flow_m3_h"] > 0.0
    assert snapshot.rooms["R2"].cooling_kw > 0.0
    heat = snapshot.devices["hp"].values
    assert heat["cooling_kw"] > 0.0
    assert heat["heat_reject_kw"] > heat["cooling_kw"]
