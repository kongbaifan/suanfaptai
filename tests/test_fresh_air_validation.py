"""新风处理能力与末端选型的校验规则

三条规则各自对应一类会让仿真静默失真的配置：

- ``fresh_air_no_handler``：房间有新风需求却没有任何承担者；
- ``fresh_air_handler_without_cooling``：承担者没有可用冷源（直膨机型在仿真里
  没有模型、又没接冷冻水），新风湿负荷会两头落空；
- ``terminal_oversized``：房间末端名义冷量远超设计负荷，阀门失去调节余地。

历史背景：四层样例曾用"一体式新风机组"（Carrier 62X，直膨机），系统里既没接
冷冻水、仿真也没有直膨模型，导致新风湿负荷被静默丢弃；校验器当时零告警。
"""

from copy import deepcopy

from app.project_validation import validate_project_data
from app.samples import FOUR_FLOOR_FCU_FRESH_AIR_SAMPLE, MULTI_ZONE_ALL_AIR_SAMPLE
from app.schemas import SystemEvaluateRequest


def _codes(payload: dict) -> list[str]:
    return [issue.code for issue in validate_project_data(
        SystemEvaluateRequest(**deepcopy(payload))
    ).issues]


def test_repaired_samples_raise_no_fresh_air_or_oversize_warnings():
    """改造后的样例应当是干净配置"""
    for sample in (FOUR_FLOOR_FCU_FRESH_AIR_SAMPLE, MULTI_ZONE_ALL_AIR_SAMPLE):
        codes = set(_codes(sample))
        assert codes.isdisjoint(
            {
                "fresh_air_no_handler",
                "fresh_air_handler_without_cooling",
                "terminal_oversized",
            }
        ), f"样例不应命中新风/选型规则: {codes}"


def test_fresh_air_handler_without_cooling_is_flagged():
    """新风机组既无冷冻水、仿真又无直膨模型时必须告警"""
    payload = deepcopy(FOUR_FLOOR_FCU_FRESH_AIR_SAMPLE)
    payload["devices"] = [
        device for device in payload["devices"] if not device["id"].startswith("ahu_")
    ]
    payload["devices"] += [
        {
            "id": f"fau_f{floor}",
            "type": "一体式新风机组",
            "name": "62X-12",
            "floor_id": f"F{floor}",
        }
        for floor in (1, 2, 3)
    ]
    payload["connections"] = [
        connection
        for connection in payload["connections"]
        if "ahu_" not in connection["from_id"] and "ahu_" not in connection["to_id"]
    ]
    for room in payload["rooms"]:
        room["device_ids"] = [
            device_id.replace("ahu_", "fau_") for device_id in room["device_ids"]
        ]

    assert "fresh_air_handler_without_cooling" in _codes(payload)


def test_room_without_any_fresh_air_handler_is_flagged():
    """只剩风机盘管的房间没有任何新风承担者"""
    payload = deepcopy(FOUR_FLOOR_FCU_FRESH_AIR_SAMPLE)
    payload["devices"] = [
        device for device in payload["devices"] if not device["id"].startswith("ahu_")
    ]
    payload["connections"] = [
        connection
        for connection in payload["connections"]
        if "ahu_" not in connection["from_id"] and "ahu_" not in connection["to_id"]
    ]
    for room in payload["rooms"]:
        room["device_ids"] = [
            device_id for device_id in room["device_ids"]
            if not device_id.startswith("ahu_")
        ]

    assert "fresh_air_no_handler" in _codes(payload)


def test_grossly_oversized_terminal_is_flagged():
    """未接热水的新风机组不参与超配评估；把风机盘管放大 40 台应触发告警"""
    payload = deepcopy(FOUR_FLOOR_FCU_FRESH_AIR_SAMPLE)
    for device in payload["devices"]:
        if device["id"].startswith("fcu_"):
            device["quantity"] = 40

    assert "terminal_oversized" in _codes(payload)


def test_sharing_fresh_air_unit_does_not_fake_oversize():
    """一台新风机组服务多个房间时，不应把整机容量摊到每个房间造成误报"""
    payload = deepcopy(MULTI_ZONE_ALL_AIR_SAMPLE)
    # 该样例三区共用一台 AHU，其容量主要处理新风，不应触发超配
    assert "terminal_oversized" not in _codes(payload)
