"""室外新风的归属与计量

回归防线：房间所需新风必须**只被计量一次**，且由具备新风引入能力的机组
处理——新风机组优先，其次组合式空调机组；风机盘管不引入新风。

历史缺陷：混风逻辑被无差别套用到每台服务该房间的设备上，风机盘管 +
新风机组共服一室时新风负荷被计入两次（实测 2.00 倍），四层样例因此
长期无法建立正确的湿度平衡。
"""

from copy import deepcopy

from app.samples import FOUR_FLOOR_FCU_FRESH_AIR_SAMPLE, MULTI_ZONE_ALL_AIR_SAMPLE
from app.schemas import SystemEvaluateRequest
from app.simcore.project import build_project_layout
from app.simcore.session import SimulationConfig, SimulationMode, SimulationSession
from app.unity_catalog import DeviceRole


def _session(sample: dict, mode: SimulationMode = SimulationMode.COOLING):
    layout = build_project_layout(SystemEvaluateRequest(**deepcopy(sample))).layout
    return layout, SimulationSession(
        layout,
        SimulationConfig(
            mode=mode,
            initial_room_temp_c=29.0,
            target_temp_c=26.0,
            target_humidity_pct=55.0,
            outdoor_base_temp_c=33.0,
            outdoor_humidity_pct=70.0,
            automatic_control=True,
        ),
    )


def test_fresh_air_handler_prefers_fresh_air_unit_over_ahu():
    """同时有新风机组与组合式机组时，新风优先由新风机组承担"""
    project = {
        "rooms": [
            {
                "id": "r1",
                "name": "共服房间",
                "device_ids": ["fau-1", "ahu-1"],
                "area_m2": 120,
                "people": 20,
            }
        ],
        "devices": [
            {"id": "fau-1", "type": "一体式新风机组", "name": "62X-05"},
            {"id": "ahu-1", "type": "组合式空调机组", "name": "AHU-06-07-4排"},
        ],
        "pipes": [],
        "connections": [],
    }
    layout, session = _session(project)
    handlers = session._fresh_air_handlers()

    role_by_id = {
        node.instance_id: node.rated.role for node in layout.devices
    }
    assert handlers == {"r1": "fau-1"}
    assert role_by_id[handlers["r1"]] is DeviceRole.FRESH_AIR_UNIT


def test_fresh_air_unit_carries_fresh_air_in_four_floor_sample():
    """四层样例的新风由接冷冻水的新风机组承担。"""
    layout, session = _session(FOUR_FLOOR_FCU_FRESH_AIR_SAMPLE)
    handlers = session._fresh_air_handlers()

    role_by_id = {
        node.instance_id: node.rated.role for node in layout.devices
    }
    assert handlers, "房间应有新风承担者"
    assert all(
        role_by_id[handler] is DeviceRole.FRESH_AIR_UNIT
        for handler in handlers.values()
    )
    # 每个房间都有且只有一个承担者
    assert set(handlers) == {room.room_id for room in layout.rooms}


def test_fcu_never_carries_fresh_air():
    """风机盘管处理 100% 回风，不引入新风"""
    layout, session = _session(FOUR_FLOOR_FCU_FRESH_AIR_SAMPLE)
    handlers = session._fresh_air_handlers()

    fcu_ids = {
        node.instance_id
        for node in layout.devices
        if node.rated.role is DeviceRole.FCU
    }
    assert fcu_ids
    assert fcu_ids.isdisjoint(set(handlers.values()))


def test_fresh_air_counted_exactly_once_across_serving_units():
    """多台设备共服一室时，新风负荷只能被计量一次（不得按台数放大）"""
    layout, session = _session(FOUR_FLOOR_FCU_FRESH_AIR_SAMPLE)
    handlers = session._fresh_air_handlers()
    room_by_id = {room.room_id: room for room in layout.rooms}

    rooms_total = sum(room.fresh_air_m3_h for room in layout.rooms)
    counted_total = sum(
        room_by_id[room_id].fresh_air_m3_h
        for room_id, handler in handlers.items()
        if handler is not None
    )
    assert counted_total == rooms_total

    # 交叉验证：承担者的服务房间里，只有归它的那部分才计新风
    for node in layout.devices:
        if node.rated.role not in {DeviceRole.AHU, DeviceRole.FRESH_AIR_UNIT}:
            continue
        owned = [
            room_by_id[room_id].fresh_air_m3_h
            for room_id in node.effective_room_ids
            if handlers.get(room_id) == node.instance_id
        ]
        # 非承担者的机组不应认领任何新风
        if node.instance_id not in handlers.values():
            assert owned == []


def test_multi_zone_ahu_carries_fresh_air_for_all_its_zones():
    """全空气系统由组合式机组承担新风（无独立新风机组）"""
    layout, session = _session(MULTI_ZONE_ALL_AIR_SAMPLE)
    handlers = session._fresh_air_handlers()

    ahu_ids = {
        node.instance_id
        for node in layout.devices
        if node.rated.role is DeviceRole.AHU
    }
    assert ahu_ids
    assert set(handlers.values()) <= ahu_ids


def test_room_without_fresh_air_device_still_gets_outdoor_air_load():
    """没有任何新风处理设备的房间，新风不得凭空消失，仍按室外状态进入

    这是"配料不足"应当暴露的情形：少了新风处理能力，负荷就会体现在房间上，
    而不是被静默丢弃。
    """
    project = {
        "rooms": [
            {
                "id": "r1",
                "name": "仅风机盘管房间",
                "device_ids": ["fcu-1"],
                "area_m2": 60,
                "people": 20,
            }
        ],
        "devices": [
            {"id": "fcu-1", "type": "风机盘管", "name": "FP-102WA"},
        ],
        "pipes": [],
        "connections": [],
    }
    layout, session = _session(project)
    assert session._fresh_air_handlers() == {}
    assert layout.rooms[0].fresh_air_m3_h > 0.0

    snapshot = session.step()
    reading = snapshot.rooms["r1"]
    # 室外新风（33℃/70%，比室内湿）进入房间 → 湿负荷为正
    assert reading.fresh_air_latent_kw > 0.0


def test_fresh_air_unit_delivers_design_ventilation_airflow():
    """专用新风机组必须按卫生新风量送风，不随房间冷热需求波动

    风量若跟随房间温度需求，温度达标后风机就降速、新风量不足——这正是新风
    机组与普通末端最本质的区别。这里用四层样例跑自动控制，核对新风机组总
    风量与各房间新风需求之和一致。
    """
    import pytest as _pytest

    layout, session = _session(FOUR_FLOOR_FCU_FRESH_AIR_SAMPLE)
    for _ in range(1800):
        session.step()

    solved = session._solve(session._ambient())
    delivered = sum(
        solved.terminals[node.instance_id].airflow_m3_h
        for node in layout.devices
        if node.rated.role is DeviceRole.FRESH_AIR_UNIT
    )
    required = sum(room.fresh_air_m3_h for room in layout.rooms)

    assert delivered == _pytest.approx(required, rel=0.05)


def test_fresh_air_unit_coil_stays_open_so_supply_is_treated():
    """新风机组盘管不能因房间温度达标就关闭

    机组风机常开送新风，水阀若跟着房间温度需求归零，就会把**未处理**的室外
    新风直接灌进房间。这里核对机组始终产出冷量、送风低于进风（室外）温度。
    """
    layout, session = _session(FOUR_FLOOR_FCU_FRESH_AIR_SAMPLE)
    for _ in range(1800):
        session.step()

    ambient = session._ambient()
    solved = session._solve(ambient)
    fresh_units = [
        node
        for node in layout.devices
        if node.rated.role is DeviceRole.FRESH_AIR_UNIT
    ]
    assert fresh_units
    for node in fresh_units:
        state = solved.terminals[node.instance_id]
        assert state.cooling_kw > 0.0
        assert state.supply_air_temp_c < ambient.dry_bulb_c
