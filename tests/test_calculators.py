from app.calculators.air import calculate_air
from app.calculators.load import calculate_load
from app.calculators.water import calculate_water
from app.schemas import AirRequest, LoadRequest, RoomInput, WaterRequest


def test_load_calculation_returns_adjusted_totals():
    result = calculate_load(
        LoadRequest(
            rooms=[
                RoomInput(name="r1", area_m2=100, people=10),
                RoomInput(name="r2", area_m2=50, people=5),
            ]
        )
    )

    assert result.total_cooling_kw > 0
    assert result.total_heating_kw > 0
    assert result.adjusted_cooling_kw > result.total_cooling_kw * 0.9
    assert len(result.rooms) == 2


def test_water_flow_and_pressure_increase_with_load():
    small = calculate_water(WaterRequest(load_kw=50, length_m=20))
    large = calculate_water(WaterRequest(load_kw=200, length_m=20))

    assert large.flow_m3_h > small.flow_m3_h
    assert large.selected_pipe["dn"] >= small.selected_pipe["dn"]
    assert large.pressure_drop_pa > 0
    assert large.pump_head_m > 0


def test_air_selects_standard_duct():
    result = calculate_air(AirRequest(cooling_load_kw=80))

    assert result.airflow_m3_h > 0
    assert result.selected_duct["type"] == "rectangular"
    assert result.actual_velocity_m_s > 0

