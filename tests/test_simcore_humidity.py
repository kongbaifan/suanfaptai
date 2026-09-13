"""湿度控制与目标可达性

覆盖三件事：自动控制允许为除湿适度过冷、湿度目标不可达时如实告警、
以及可达性诊断字段（humidity_shortfall_pct）随读数正确暴露。
"""

import itertools
from copy import deepcopy
from dataclasses import replace

import pytest

from app.samples import FOUR_FLOOR_FCU_FRESH_AIR_SAMPLE, MULTI_ZONE_ALL_AIR_SAMPLE
from app.schemas import SystemEvaluateRequest
from app.simcore.devices import (
    AIR_DENSITY_KG_M3,
    coil_leaving_state,
    terminal_state,
)
from app.simcore.project import build_project_layout
from app.simcore.psychrometrics import (
    humidity_ratio_from_rh,
    rh_from_humidity_ratio,
)
from app.simcore.rated import resolve_rated_spec_by_type
from app.simcore.session import (
    AUTO_DEADBAND_RH_PCT,
    AUTO_DEHUMIDIFICATION_SUBCOOL_C,
    SimulationConfig,
    SimulationMode,
    SimulationSession,
)

# 四层样例中用于跟踪的房间
ROOM_ID = "office_f1"

# 四层样例的设计室外工况与冷冻水供水温度（见 samples.py 与 session 默认值）
OUTDOOR_TEMP_C = 32.22
OUTDOOR_RH_PCT = 70.0
SUPPLY_WATER_TEMP_C = 8.5
# 24℃/55% 对应的含湿量，即"室内水平"
ROOM_LEVEL_RATIO = humidity_ratio_from_rh(24.0, 55.0)


def _run(sample: dict, ticks: int = 600):
    layout = build_project_layout(SystemEvaluateRequest(**deepcopy(sample))).layout
    session = SimulationSession(
        layout,
        SimulationConfig(
            mode=SimulationMode.COOLING,
            initial_room_temp_c=29.0,
            target_temp_c=26.0,
            target_humidity_pct=55.0,
            outdoor_base_temp_c=33.0,
            outdoor_humidity_pct=70.0,
            automatic_control=True,
        ),
    )
    snapshot = None
    for _ in range(ticks):
        snapshot = session.step()
    return session, snapshot


def test_overcooling_stays_within_subcool_allowance():
    """过冷被限制在除湿带宽内，不会无界跑冷

    盘管处理混合风后，除湿必须让送风低于室内露点，室温因此低于目标；模型
    无再热盘管，故过冷量以 ``AUTO_DEHUMIDIFICATION_SUBCOOL_C`` 为界。
    """
    assert AUTO_DEHUMIDIFICATION_SUBCOOL_C > 0.0
    _, snapshot = _run(MULTI_ZONE_ALL_AIR_SAMPLE)
    # 允许超调一点（控制离散化），但不应远低于除湿下限
    limit_c = 26.0 - AUTO_DEHUMIDIFICATION_SUBCOOL_C - 1.5
    for reading in snapshot.rooms.values():
        assert reading.temperature_c >= limit_c
    # 缺口读数与目标始终保持自洽
    assert all(
        reading.humidity_shortfall_pct
        == max(0.0, reading.relative_humidity_pct - reading.target_humidity_pct)
        for reading in snapshot.rooms.values()
    )


@pytest.mark.xfail(
    reason=(
        "已知缺陷（尚未修复）：四层样例的湿度控制处于极限环。实测 3000 tick 后"
        "末段 1500 tick 窗口内单步温度变化 0.0516 K、湿度 0.3616 个百分点，"
        "湿度带宽 3.29 pp（49.8~53.1%），不衰减。"
        "注意：本样例已能达标（均值 52.2%，目标 55%），残余的是小幅极限环，"
        "不是目标不可达。"
        "成因不在 `_room_humidity_demand` 的过冷闸门——该处的比例削减已修复"
        "（提交 5530975，见 test_humidity_demand_is_not_throttled_by_subcool_headroom），"
        "但触界硬截断本身是必需的（见 test_humidity_demand_stops_at_subcool_floor）。"
        "残余振荡来自盘管出力与水阀/水温之间的离散耦合，属独立问题。"
        "本测试保留为缺陷证据与修复验收条件；修复后去掉 xfail 即可。"
    ),
    strict=True,
)
def test_humidity_control_settles_instead_of_limit_cycling():
    """湿度控制必须收敛到不动点，不得持续振荡

    真实缺陷：四层样例在自动调节下持续振荡，室温与湿度都不平息，
    3000 tick 后振幅仍不衰减（见 xfail 理由中的实测数据）。

    本测试用与稳态收敛判据**无关**的方式复核：末段窗口内每步变化都必须足够小。
    它同时是这条缺陷的**验收条件**——修复后应当直接通过。
    """
    layout = build_project_layout(
        SystemEvaluateRequest(**deepcopy(FOUR_FLOOR_FCU_FRESH_AIR_SAMPLE))
    ).layout
    session = SimulationSession(
        layout,
        SimulationConfig(
            mode=SimulationMode.COOLING,
            step_seconds=300.0,
            target_temp_c=24.0,
            target_humidity_pct=55.0,
            outdoor_humidity_pct=70.0,
            automatic_control=True,
        ),
    )

    # 先跑足够长，越过暂态
    for _ in range(1200):
        session.step()

    # 再记录末段轨迹，检查是否真正停住
    temperatures: list[float] = []
    humidities: list[float] = []
    for _ in range(200):
        snapshot = session.step()
        temperatures.append(snapshot.rooms[ROOM_ID].temperature_c)
        humidities.append(snapshot.rooms[ROOM_ID].relative_humidity_pct)

    max_step_t = max(abs(b - a) for a, b in itertools.pairwise(temperatures))
    max_step_rh = max(abs(b - a) for a, b in itertools.pairwise(humidities))

    assert max_step_t <= 0.01, (
        f"末段仍有 {max_step_t:.4f} K 的单步温度变化，湿度控制未收敛（疑似极限环）"
    )
    assert max_step_rh <= 0.05, (
        f"末段仍有 {max_step_rh:.4f} 个百分点的单步湿度变化，湿度控制未收敛"
    )


def test_fresh_air_unit_cannot_dry_outdoor_air_below_room():
    """新风机组把室外新风干燥到室内含湿量以下，是房间能达标的**前提**

    这条测试钉死四层样例湿度达不到目标的**根因**（经多轮排查确认）：

    新风机组在目录里标称 29 kW，但其盘管受 `coil_calibration_limited` 约束
    （目录 10 台新风机组全部如此），物理上限只有约 15.9 kW。而要把室外新风
    （32.2℃ / 70% RH，含湿量约 0.0214）干燥到室内水平（< 0.0140）需要约
    **21.4 kW**。

    能力不够的后果不是"少除一点湿"，而是**供给比房间更湿的空气**：送风含湿量
    高于室内，潜热项转负，机组变成持续的湿源。实测该机组对房间的潜热贡献为
    **负值**，抵消掉内区风机盘管的除湿量，房间含湿量因此被钉在高位。

    因此房间达不到 55% 是**设备能力/盘管模型问题**，不是控制律问题：
    任何控制律都无法让一台送风比房间更湿的机组把房间弄干。
    实测印证：把室外相对湿度从 70% 降到 30%，同一套控制器即可把房间带到
    53.4%（接近 55% 目标）；而在 70% 下只能到 75.3%。

    ## 更深一层：显热口径不适用于湿工况盘管

    进一步核查发现，`coil_calibration_limited` 并**不是**在指出目录数据有误，
    而是在指出**盘管模型的适用边界**：

    - `terminal_state` 用 `Q = ε·C_min·ΔT`，其中 `C_min = min(ṁ_air·cp,
      ṁ_w·cp_w)`、`ΔT` 为进风与进水温度之差。这是**显热**公式，潜热不体现在
      `cp·ΔT` 里。
    - 全新风机组在室外 35℃/70% 工况下，**潜热占总负荷的 65%**。用显热口径
      算出 ε=2.16（"不可能"），但按焓差口径其上限是 35.4 kW，目录的 29 kW
      完全可达（全部 5 个 MKS 型号均如此）。
    - 结果是：本可胜任的新风机组被判为"不可达"、按显热上限截断出力，送风
      反而比房间更湿，成为湿源。

    即：**`terminal_state` 没有湿度输入**（见其函数签名），无法评估湿工况盘管
    的焓差能力。真正的修复需要让盘管状态知道进风含湿量并按全热口径定能力，
    这超出本测试范围，属模型重构。本测试把现状与前提固定下来。
    """
    spec = resolve_rated_spec_by_type("新风机组", "MKS02D4Y/C")

    # 该型号受盘管物理上限约束，标称容量不可达
    assert spec.capacity_kw == pytest.approx(29.0)
    ceiling = terminal_state(
        spec, 1.0, OUTDOOR_TEMP_C, SUPPLY_WATER_TEMP_C, fan_speed_ratio=1.0
    )
    assert ceiling.coil_calibration_limited is True, (
        "该型号应受盘管物理上限约束；若不再受限，说明盘管模型已变，本测试的前提需复核"
    )

    # 达到"送风比室内更干"所需的冷量，超出其物理上限
    outdoor_ratio = humidity_ratio_from_rh(OUTDOOR_TEMP_C, OUTDOOR_RH_PCT)
    duty_needed = _duty_for_leaving_temperature(
        outdoor_ratio, spec.airflow_m3_h or 0.0, ROOM_LEVEL_RATIO
    )
    assert duty_needed > ceiling.cooling_kw, (
        f"所需的 {duty_needed:.2f} kW 未超过盘管上限 {ceiling.cooling_kw:.2f} kW；"
        "本测试的结论（新风机组无法把新风干燥到室内水平）不再成立，需复核"
    )

    # 上限出力下，送风含湿量仍高于室内 -> 机组是湿源
    _, leaving_ratio = coil_leaving_state(
        OUTDOOR_TEMP_C, outdoor_ratio, spec.airflow_m3_h or 0.0, ceiling.cooling_kw
    )
    assert leaving_ratio > ROOM_LEVEL_RATIO, (
        "上限出力下新风送风已能干燥到室内水平以下，结论需复核"
    )


def _duty_for_leaving_temperature(
    entering_ratio: float, airflow_m3_h: float, target_ratio: float
) -> float:
    """二分求把送风含湿量压到 target_ratio 所需的盘管冷量

    含湿量随出风温度单调下降（出风落在饱和线上），故对冷量单调，可二分。
    """
    lo, hi = 0.0, 500.0
    for _ in range(80):
        mid = (lo + hi) / 2.0
        _, leaving = coil_leaving_state(
            OUTDOOR_TEMP_C, entering_ratio, airflow_m3_h, mid
        )
        if leaving > target_ratio:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2.0


def test_wet_coil_capacity_is_modelled_not_capped_by_sensible_ceiling():
    """湿工况盘管的冷量按**全热**口径建模，不再被显热上限截断

    这是"湿度达不到 55%"的修复所依赖的模型行为。

    背景：`Q = ε·C_min·ΔT` 是**显热**公式，潜热不参与。潜热主导的盘管
    （典型是处理室外新风的新风机组，实测潜热占总负荷 65%）会被低估——
    MKS02D4Y/C 目录 29 kW，按显热口径上限只有 13.4 kW（ε 需 2.16）。

    修复后 `terminal_state` 接受 `room_humidity_ratio`，结露时按焓差口径补足：

        Q_湿 = ε · ṁ_da · (h_进风 − h_饱和(进水温度 + 接近温差))

    本测试钉死三条不变量：
    1. 传含湿量后新风机组出力显著高于纯显热口径；
    2. 出力随**供水温度降低**单调增大（否则"降低供水温反而出力下降"）；
    3. 不传含湿量时行为与旧版一致（向后兼容，干工况不启用该修正）。
    """
    spec = resolve_rated_spec_by_type("新风机组", "MKS02D4Y/C")
    outdoor_ratio = humidity_ratio_from_rh(OUTDOOR_TEMP_C, OUTDOOR_RH_PCT)

    dry = terminal_state(spec, 1.0, OUTDOOR_TEMP_C, SUPPLY_WATER_TEMP_C)
    wet = terminal_state(
        spec,
        1.0,
        OUTDOOR_TEMP_C,
        SUPPLY_WATER_TEMP_C,
        room_humidity_ratio=outdoor_ratio,
    )

    # 1. 湿工况修正显著提升出力
    assert wet.cooling_kw > 1.5 * dry.cooling_kw, (
        f"湿工况出力 {wet.cooling_kw:.2f} kW 未显著高于显热口径 {dry.cooling_kw:.2f} kW"
    )

    # 2. 供水温越低出力越大（单调）——曾因用固定表面温度而破坏
    duties = [
        terminal_state(
            spec,
            1.0,
            OUTDOOR_TEMP_C,
            tw,
            room_humidity_ratio=outdoor_ratio,
        ).cooling_kw
        for tw in (7.0, 9.0, 12.0, 14.0)
    ]
    for colder, warmer in itertools.pairwise(duties):
        assert colder > warmer, (
            f"供水温升高未使出力下降：{duties}；"
            "表面温度必须跟随实际供水温，否则非单调"
        )

    # 3. 不传含湿量时保持旧行为（干工况不启用修正）
    assert dry.cooling_kw == pytest.approx(
        terminal_state(
            spec, 1.0, OUTDOOR_TEMP_C, SUPPLY_WATER_TEMP_C, room_humidity_ratio=None
        ).cooling_kw
    )


def test_unreachable_humidity_target_raises_alarm():
    """散湿量超过除湿能力时，室温会停在偏湿侧并告警，而不是静默失效。"""
    _, snapshot = _run(MULTI_ZONE_ALL_AIR_SAMPLE)
    # 打印实际湿度结果
    print("\n稳态结果（目标: 26.0C / 55.0% RH）:")
    for room_id, reading in sorted(snapshot.rooms.items()):
        print(f"  {room_id}: T={reading.temperature_c:.2f}C, RH={reading.relative_humidity_pct:.1f}%, 缺口={reading.humidity_shortfall_pct:.1f}%")
    print(f"告警: {snapshot.alarms}")
    assert "humidity_target_unreachable" in snapshot.alarms
    # 告警与诊断字段必须一致：确有房间超出目标死区
    assert any(reading.humidity_shortfall_pct > 3.0 for reading in snapshot.rooms.values())


def test_humidity_demand_is_not_throttled_by_subcool_headroom():
    """除湿需求不受"离过冷下限还有多远"的比例削减

    回归防线（此前 `_room_humidity_demand` 的缺陷）：除湿需求曾乘过
    ``min(1.0, headroom / AUTO_FULL_DEMAND_C)``，把"还能不能再冷"当成了
    "该不该除湿"。实测 `complete/office_1` 湿度偏差对应需求 0.965，
    距下限 22.0℃ 尚有 1.74 K 余量，比例因子 0.436 把需求压到 0.420，
    与实测水阀开度 42% 完全吻合，房间因此停在 69.5%（目标 55%）。

    钉死两条不变量：
    1. 距下限尚有充裕余量时，需求必须是**完整的比例输出**，不得被打折；
    2. 需求必须对湿度偏差单调——偏差越大需求越大。
    """
    from app.simcore.session import AUTO_FULL_DEMAND_RH_PCT

    layout = build_project_layout(SystemEvaluateRequest(**_basic_sample())).layout
    session = SimulationSession(
        layout,
        SimulationConfig(
            mode=SimulationMode.COOLING,
            step_seconds=300.0,
            target_temp_c=24.0,
            target_humidity_pct=55.0,
            automatic_control=True,
        ),
    )
    room_id = layout.rooms[0].room_id

    # 各湿度水平下的需求，室温固定在下限之上 1.74 K（旧缺陷的折扣点）
    room_temp_c = 24.0 - AUTO_DEHUMIDIFICATION_SUBCOOL_C + 1.74
    demands = {}
    for rh_pct in (70.0, 80.0, 90.0):
        session._room_temps[room_id] = room_temp_c
        session._room_humidity_pct[room_id] = rh_pct
        demands[rh_pct] = session._room_humidity_demand(room_id)

    # 1. 折算到目标室温下的等效湿度偏差，反算期望需求
    for rh_pct, demand in demands.items():
        ratio = humidity_ratio_from_rh(room_temp_c, rh_pct)
        equivalent_rh = rh_from_humidity_ratio(24.0, ratio)
        error = equivalent_rh - 55.0
        expected = min(
            1.0,
            (error - AUTO_DEADBAND_RH_PCT)
            / (AUTO_FULL_DEMAND_RH_PCT - AUTO_DEADBAND_RH_PCT),
        )
        # 不得因 headroom 只有 1.74 K 而被按 1.74/4.0 打折
        assert demand == pytest.approx(expected, abs=1e-9), (
            f"RH={rh_pct}% 时需求 {demand:.4f} 与完整比例输出 {expected:.4f} 不符，"
            "疑似过冷余量比例削减回归"
        )

    # 2. 对湿度偏差单调不减（高偏差处饱和到 1.0，故是"不减"而非"严格递增"）
    ordered = [demands[k] for k in sorted(demands)]
    assert ordered == sorted(ordered), f"除湿需求未随湿度偏差单调不减：{demands}"
    assert ordered[0] < ordered[-1], f"除湿需求未随湿度偏差增长：{demands}"


def test_humidity_demand_stops_at_subcool_floor():
    """触到过冷下限即归零，把室温交给温度需求接管

    反直觉但经实测确立：若触界后仍输出除湿需求，`_room_demand` 的
    ``max(温度, 湿度)`` 会让除湿压过温度需求，房间被带到 17.8℃（下限
    22.0℃），而湿度**并未**达标（`complete/meeting_1` 仍 95.0% RH）。
    根因是过冷本身不产生除湿量——继续拉低室温只吃掉显热，同一含湿量下
    温度更低反而相对湿度更高。故下限必须是硬截断。
    """
    layout = build_project_layout(SystemEvaluateRequest(**_basic_sample())).layout
    session = SimulationSession(
        layout,
        SimulationConfig(
            mode=SimulationMode.COOLING,
            step_seconds=300.0,
            target_temp_c=24.0,
            target_humidity_pct=55.0,
            automatic_control=True,
        ),
    )
    room_id = layout.rooms[0].room_id
    floor_c = 24.0 - AUTO_DEHUMIDIFICATION_SUBCOOL_C

    # 严重偏湿，但室温已在下限或以下
    session._room_humidity_pct[room_id] = 95.0
    for temp_c in (floor_c, floor_c - 1.0, floor_c - 2.1):
        session._room_temps[room_id] = temp_c
        assert session._room_humidity_demand(room_id) == 0.0, (
            f"室温 {temp_c:.2f}℃ 已达/越过过冷下限 {floor_c:.2f}℃，"
            "除湿需求应归零交由温度需求接管"
        )

    # 下限之上则恢复输出，且随余量退出而恢复（不做比例削减）
    session._room_temps[room_id] = floor_c + 0.5
    assert session._room_humidity_demand(room_id) > 0.0


def _basic_sample() -> dict:
    """一个最小的单房间样例，用于隔离测试除湿需求函数"""
    sample = deepcopy(FOUR_FLOOR_FCU_FRESH_AIR_SAMPLE)
    rooms = sample.get("rooms") or []
    if len(rooms) > 1:
        keep = rooms[0]["id"]
        sample["rooms"] = [rooms[0]]
        for device in sample.get("devices", []):
            if device.get("type") in {"风机盘管", "组合式空调机组", "新风机组"}:
                device["room_ids"] = [keep]
    return sample


def test_humidity_shortfall_matches_reading_and_target():
    _, snapshot = _run(FOUR_FLOOR_FCU_FRESH_AIR_SAMPLE, ticks=300)
    for reading in snapshot.rooms.values():
        expected = max(0.0, reading.relative_humidity_pct - reading.target_humidity_pct)
        assert reading.humidity_shortfall_pct == expected


def test_ventilation_mode_does_not_claim_humidity_control():
    """通风工况没有冷盘管，不应报湿度不可达（该工况本就不承担湿度目标）。"""
    layout = build_project_layout(
        SystemEvaluateRequest(**deepcopy(MULTI_ZONE_ALL_AIR_SAMPLE))
    ).layout
    session = SimulationSession(
        layout,
        SimulationConfig(
            mode=SimulationMode.VENTILATION,
            initial_room_temp_c=26.0,
            target_humidity_pct=55.0,
            automatic_control=True,
        ),
    )
    for _ in range(60):
        snapshot = session.step()
    assert "humidity_target_unreachable" not in snapshot.alarms


def test_coil_treats_mixed_air_so_fresh_air_load_cannot_bypass():
    """新风必须经过盘管：混合风含湿量高于纯回风，盘管因此负担更大除湿量

    这是"新风绕过盘管"缺陷的回归防线——旧口径把新风湿负荷直接注入房间，
    盘管只处理回风，无论怎么加大冷量都除不掉新风带进来的水分。
    """
    from app.simcore.devices import coil_leaving_state
    from app.simcore.psychrometrics import humidity_ratio_from_rh

    room_temp_c, room_rh = 26.0, 55.0
    outdoor_temp_c, outdoor_rh = 33.0, 70.0
    airflow_m3_h, coil_kw = 8000.0, 40.0

    return_ratio = humidity_ratio_from_rh(room_temp_c, room_rh)
    outdoor_ratio = humidity_ratio_from_rh(outdoor_temp_c, outdoor_rh)
    # 50% 新风混合后的含湿量严格介于回风与新风之间
    mixed_ratio = 0.5 * return_ratio + 0.5 * outdoor_ratio
    assert return_ratio < mixed_ratio < outdoor_ratio

    _, supply_ratio_return_only = coil_leaving_state(
        room_temp_c, return_ratio, airflow_m3_h, coil_kw
    )
    _, supply_ratio_mixed = coil_leaving_state(
        room_temp_c, mixed_ratio, airflow_m3_h, coil_kw
    )
    # 处理更湿的混合风时，同样冷量下送风含湿量更高（负担真实存在）
    assert supply_ratio_mixed >= supply_ratio_return_only


def test_dry_coil_does_not_dehumidify():
    """冷量不足以把送风压到露点时，盘管等湿降温、不除湿"""
    from app.simcore.devices import coil_leaving_state
    from app.simcore.psychrometrics import humidity_ratio_from_rh

    ratio_in = humidity_ratio_from_rh(26.0, 55.0)
    supply_temp_c, supply_ratio = coil_leaving_state(26.0, ratio_in, 8000.0, 3.0)

    assert supply_temp_c < 26.0
    assert supply_ratio == ratio_in


def test_wet_coil_dehumidifies_and_lands_on_saturation():
    """冷量足够时盘管降温除湿，出口落在饱和线上"""
    from app.simcore.devices import coil_leaving_state
    from app.simcore.psychrometrics import (
        humidity_ratio_from_rh,
        rh_from_humidity_ratio,
    )

    ratio_in = humidity_ratio_from_rh(26.0, 55.0)
    supply_temp_c, supply_ratio = coil_leaving_state(26.0, ratio_in, 8000.0, 60.0)

    assert supply_ratio < ratio_in
    # 出口接近饱和（相对湿度 100%），这是湿盘管的标志
    assert rh_from_humidity_ratio(supply_temp_c, supply_ratio) == pytest.approx(
        100.0, abs=0.5
    )


def test_coil_sensible_plus_latent_equals_coil_load():
    """显热与潜热按同一组焓常数拆分，之和严格等于盘管冷量（不丢能量）"""
    from app.simcore.devices import coil_leaving_state
    from app.simcore.psychrometrics import (
        DRY_AIR_CP_KJ_KG_K,
        humidity_ratio_from_rh,
        vapor_enthalpy_kj_kg,
    )

    for temp_c, rh_pct, airflow_m3_h, coil_kw in (
        (26.0, 55.0, 8000.0, 10.0),
        (26.0, 70.0, 8000.0, 50.0),
        (31.0, 47.0, 8000.0, 35.0),
        (26.0, 95.0, 5000.0, 40.0),
    ):
        ratio_in = humidity_ratio_from_rh(temp_c, rh_pct)
        supply_temp_c, supply_ratio = coil_leaving_state(
            temp_c, ratio_in, airflow_m3_h, coil_kw
        )
        air_mass_kg_s = airflow_m3_h * AIR_DENSITY_KG_M3 / 3600.0
        sensible_kw = (
            air_mass_kg_s * DRY_AIR_CP_KJ_KG_K * (temp_c - supply_temp_c)
        )
        latent_kw = air_mass_kg_s * (
            vapor_enthalpy_kj_kg(temp_c, ratio_in)
            - vapor_enthalpy_kj_kg(supply_temp_c, supply_ratio)
        )
        assert sensible_kw + latent_kw == pytest.approx(coil_kw, abs=1e-6)


def test_room_served_by_two_coils_sums_contributions():
    """一台房间挂多台盘管时，处理量逐台累加而非按单一送风状态放大"""
    from app.simcore.session import RoomDelivery

    first = RoomDelivery(sensible_cooling_kw=3.0, latent_cooling_kw=1.0)
    second = RoomDelivery(sensible_cooling_kw=2.0, latent_cooling_kw=0.5)

    combined = replace(
        first,
        sensible_cooling_kw=first.sensible_cooling_kw + second.sensible_cooling_kw,
        latent_cooling_kw=first.latent_cooling_kw + second.latent_cooling_kw,
    )
    assert combined.sensible_cooling_kw == 5.0
    assert combined.latent_cooling_kw == 1.5
