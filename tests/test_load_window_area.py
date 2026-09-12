"""窗面积计算测试

验证窗面积 = 外墙面积 × 窗墙比，而非地板面积 × 窗墙比。
修复前 load.py:49 写成 `room.area_m2 * exposed_area_factor * window_wall_ratio`。
"""
from app.calculators.load import calculate_load
from app.schemas import LoadRequest, RoomInput


def test_window_area_derived_from_wall_not_floor():
    """窗面积应从外墙面积推导，不是地板面积"""
    # 房间 10m × 10m × 3m，窗墙比 30%
    # 外墙宽 = √100 = 10 m（假设方形足迹）
    # 外墙面积 = 10 × 3 = 30 m²
    # 窗面积 = 30 × 0.3 = 9 m²
    # 太阳辐射 south 140 W/m²，遮阳系数 0.55
    # 太阳得热 = 9 × 140 × 0.55 / 1000 = 0.693 kW
    result = calculate_load(
        LoadRequest(
            rooms=[
                RoomInput(
                    name="单面外墙",
                    area_m2=100.0,
                    height_m=3.0,
                    people=0,
                    window_wall_ratio=0.3,
                    orientation="south",
                )
            ]
        )
    )

    room = result.rooms[0]
    solar = room.components_kw.get("solar", 0.0)

    # 手算期望：
    # wall_area = sqrt(100) × 3 = 30 m²
    # window_area = 30 × 0.3 = 9 m²
    # solar = 9 × 140 × 0.55 / 1000 = 0.693 kW
    expected_solar_kw = 0.693

    assert abs(solar - expected_solar_kw) < 0.01, (
        f"太阳得热 {solar:.3f} kW 偏离期望 {expected_solar_kw:.3f} kW"
    )


def test_window_area_zero_when_no_exposed_wall():
    """无窗房间的太阳得热应为零"""
    result = calculate_load(
        LoadRequest(
            rooms=[
                RoomInput(
                    name="内区无窗",
                    area_m2=100.0,
                    height_m=3.0,
                    people=0,
                    window_wall_ratio=0.0,  # 无窗
                    orientation="south",
                )
            ]
        )
    )

    solar = result.rooms[0].components_kw.get("solar", 0.0)
    assert solar == 0.0, f"无窗房间太阳得热应为 0，实际 {solar:.3f} kW"
