"""Continuous automatic commands, conservative staging and circuit-local memory."""

import math
from dataclasses import replace

import pytest

from app.data.rules import rule_value
from app.simcore.controls import clamp_controls
from app.simcore.rated import RatedSpec, resolve_rated_spec_by_type
from app.simcore.runtime_network import NetworkCircuit, RuntimeNetworkLayout
from app.simcore.session import (
    DeviceNode,
    PlantLayout,
    RoomNode,
    SimulationConfig,
    SimulationSession,
)
from app.unity_catalog import DeviceRole


def test_automatic_continuous_values_keep_manual_steps_and_discrete_hardware():
    terminal = resolve_rated_spec_by_type("风机盘管", "FP-204WA")
    value = {"valve_opening_pct": 43.25, "active_count": 2.4}
    assert clamp_controls(terminal, value, 6) == {"valve_opening_pct": 43, "active_count": 2}
    assert clamp_controls(terminal, value, 6, snap_to_step=False) == {
        "valve_opening_pct": 43.25, "active_count": 2,
    }
    assert clamp_controls(terminal, {"valve_opening_pct": 150}, snap_to_step=False) == {
        "valve_opening_pct": 100,
    }
    with pytest.raises(ValueError):
        clamp_controls(terminal, {"valve_opening_pct": math.nan}, snap_to_step=False)
    pump = RatedSpec("循环泵", "fixed-speed", DeviceRole.PUMP, variable_speed=False)
    assert clamp_controls(pump, {"speed_pct": 43.25}, snap_to_step=False) == {"speed_pct": 0}
    assert clamp_controls(pump, {"speed_pct": 56.75}, snap_to_step=False) == {"speed_pct": 100}


@pytest.fixture
def terminal_bank():
    spec = resolve_rated_spec_by_type("风机盘管", "FP-204WA")
    node = DeviceNode("bank", spec, quantity=6, room_id="room")
    session = SimulationSession(
        PlantLayout(devices=(node,), rooms=(RoomNode("room"),)),
        SimulationConfig(automatic_control=True, step_seconds=60),
    )
    return session, node


def test_bank_staging_preserves_air_and_water_commands_and_rate_limit(terminal_bank):
    session, node = terminal_bank
    targets = {"enabled": 1.0, "active_count": 2.4, "valve_opening_pct": 40.0, "fan_speed_pct": 60.0}
    previous = {"valve_opening_pct": 100.0, "fan_speed_pct": 100.0}
    counts = set()
    staged_at_target = False
    for _ in range(40):
        session._simulation_seconds += session.config.step_seconds
        old_count = session.controls[node.instance_id]["active_count"]
        accepted = session._terminal_bank_controls(node, targets)
        session._controls[node.instance_id].update(accepted)
        count = accepted["active_count"]
        counts.add(count)
        assert count == int(count) and 1 <= count <= node.quantity
        assert abs(count - old_count) <= 1
        for name, previous_value in previous.items():
            bank_pct = count * accepted[name] / node.quantity
            assert abs(bank_pct - previous_value) <= 8.0 + 1e-9
            if count != old_count:
                assert bank_pct == pytest.approx(previous_value)
                staged_at_target = True
            previous[name] = bank_pct
    assert staged_at_target and len(counts) > 1
    # Hand calculation: 2.4 equivalent units at 40% water / 60% air,
    # redistributed over two integer running units.
    assert count == 2
    assert count * node.rated.flow_m3_h * accepted["valve_opening_pct"] / 100 == pytest.approx(
        2.4 * 1.892 * 0.4,
    )
    assert count * node.rated.airflow_m3_h * accepted["fan_speed_pct"] / 100 == pytest.approx(
        2.4 * 2040 * 0.6,
    )
    off = session._terminal_bank_controls(node, {**targets, "enabled": 0})
    assert off == {"enabled": 0, "active_count": 0, "valve_opening_pct": 0, "fan_speed_pct": 0}


def test_staging_has_hysteresis_hold_and_manual_reset(terminal_bank):
    session, node = terminal_bank
    session._controls[node.instance_id]["active_count"] = 3
    for required in (2.01, 1.99, 2.01, 1.99):
        assert session._next_active_count(node, required) == 3
    session._controls[node.instance_id]["active_count"] = session._next_active_count(node, 1.4)
    assert session.controls[node.instance_id]["active_count"] == 2
    assert session._next_active_count(node, 0.5) == 2
    session._simulation_seconds += float(rule_value("automatic_control.stage_hold_seconds"))
    assert session._next_active_count(node, 0.5) == 1
    session.set_controls(node.instance_id, {"active_count": 5, "valve_opening_pct": 43.25})
    session.step()
    assert session.controls[node.instance_id]["active_count"] == 5
    assert session.controls[node.instance_id]["valve_opening_pct"] == 43
    session.reset_controls(node.instance_id)
    assert session.control_source(node.instance_id) == "auto"
    assert node.instance_id not in session._bank_last_change_seconds


def test_dehumidification_memory_is_local_to_circuit_and_clears_after_sustained_dryness():
    sources = tuple(
        DeviceNode(key, RatedSpec("冷机", key, DeviceRole.CHILLER, capacity_kw=30))
        for key in ("a", "b")
    )
    network = RuntimeNetworkLayout(
        paths=(), elements={}, issues=(),
        circuits=tuple(NetworkCircuit(key, "chilled_water", (key,), (key,), ()) for key in ("a", "b")),
        control_room_ids={("chilled_water", key): (key,) for key in ("a", "b")},
    )
    session = SimulationSession(
        PlantLayout(devices=sources, rooms=(RoomNode("a"), RoomNode("b")), network=network),
        SimulationConfig(initial_room_temp_c=24, target_temp_c=24, target_humidity_pct=55, step_seconds=60),
    )
    session._room_humidity_pct.update(a=70, b=45)
    session._update_dehumidification_requests()
    assert session._humidity_demand_is_active(sources[0])
    assert not session._humidity_demand_is_active(sources[1])
    session.set_controls("b", {"enabled": 0})
    session.reset_controls("b")
    assert session._humidity_demand_is_active(sources[0])
    # Small changes near the entry threshold do not toggle the water target.
    for rh in (56.6, 56.4, 55, 56.6):
        session._room_humidity_pct["a"] = rh
        session._update_dehumidification_requests()
        assert session._humidity_demand_is_active(sources[0])
    session._room_humidity_pct["a"] = 50
    hold = float(rule_value("automatic_control.dehumidification_release_seconds"))
    for _ in range(int(hold / session.config.step_seconds) - 1):
        session._update_dehumidification_requests()
        assert session._humidity_demand_is_active(sources[0])
    session._update_dehumidification_requests()
    assert not session._humidity_demand_is_active(sources[0])
    session._room_humidity_pct["a"] = 70
    session._update_dehumidification_requests()
    session.update_runtime_config(replace(session.config, target_humidity_pct=75))
    assert not session._dehumidifying_circuits
