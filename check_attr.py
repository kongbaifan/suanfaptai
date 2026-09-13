"""Verify the attribution for `air` and `multi_zone_all_air`.

CLAUDE.md claims they fall short because of partition/allocation issues.
Measure it: for each room, is the coil capable at its ALLOCATED airflow,
or is the shortfall in the allocation itself?

  needed airflow to hit 55% = latent_load / ((w_supply - w_target) * h_fg)
  compare against the airflow the room actually receives.
"""
import sys

sys.path.insert(0, ".")
from app.samples import SAMPLES
from app.schemas import SystemEvaluateRequest
from app.simcore.devices import (
    AIR_DENSITY_KG_M3,
    VAPORIZATION_HEAT_KJ_KG,
    coil_leaving_state,
)
from app.simcore.project import build_runtime_layout
from app.simcore.psychrometrics import (
    humidity_ratio_from_rh,
    rh_from_humidity_ratio,
)
from app.simcore.session import SimulationConfig, SimulationSession, SimulationMode

TARGET = 24.0
NEED = humidity_ratio_from_rh(TARGET, 55.0)


for name in ("air", "multi_zone_all_air", "four_floor_fcu_fresh_air"):
    sample = SAMPLES[name]
    layout = build_runtime_layout(SystemEvaluateRequest(**sample)).layout
    s = SimulationSession(
        layout,
        SimulationConfig(
            mode=SimulationMode.COOLING,
            step_seconds=300.0,
            target_temp_c=TARGET,
            target_humidity_pct=55.0,
            outdoor_humidity_pct=70.0,
            automatic_control=True,
        ),
    )
    for _ in range(4000):
        s.step()
    snap = s.step()

    # who handles fresh air for whom
    handlers = s._fresh_air_handlers()
    print(f"=== {name}")
    print(f"  fresh-air handlers: {handlers}")
    for rid in sorted(snap.rooms):
        r = snap.rooms[rid]
        w_room = humidity_ratio_from_rh(r.temperature_c, r.relative_humidity_pct)
        latent = max(r.internal_latent_kw + r.fresh_air_latent_kw, 0.0)
        flow_actual = r.airflow_m3_h

        # terminal(s) serving this room at their full rated airflow
        flow_rated = 0.0
        kw_rated = 0.0
        for node in layout.devices:
            if rid not in node.effective_room_ids:
                continue
            if node.rated.role.value not in {"fcu", "ahu", "fresh_air_unit"}:
                continue
            q = max(node.quantity, 1.0)
            flow_rated += (node.rated.airflow_m3_h or 0.0) * q
            kw_rated += (getattr(node.rated, "capacity_kw", 0.0) or 0.0) * q

        m = flow_rated * AIR_DENSITY_KG_M3 / 3600.0
        # driest the coil can go at rated airflow & full capacity
        _, w_sup = coil_leaving_state(r.temperature_c, w_room, flow_rated, kw_rated)
        # airflow needed for the room to sit exactly at NEED
        if w_sup < NEED:
            flow_needed = (
                latent
                / ((NEED - w_sup) * VAPORIZATION_HEAT_KJ_KG)
                / AIR_DENSITY_KG_M3
                * 3600.0
            )
        else:
            flow_needed = float("inf")
        print(
            f"  {rid:12s} RH@24={rh_from_humidity_ratio(TARGET, w_room):5.1f}% "
            f"alloc={flow_actual:7.0f} rated={flow_rated:7.0f} "
            f"w_sup_min={w_sup:.6f} (need {NEED:.6f}) "
            f"flow_needed={flow_needed:9.0f}"
        )
    print()
