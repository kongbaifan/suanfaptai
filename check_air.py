"""Why do `air` / `multi_zone_all_air` stay at ~64% when capacity looks ample?

The previous probe showed allocated airflow far exceeds what is needed and the
coil's leaving humidity is dry enough. So the room SHOULD be at 55%. Something
in the humidity step is adding moisture back. Measure the actual per-room
moisture budget at the settled state.
"""
import sys

sys.path.insert(0, ".")
from app.samples import SAMPLES
from app.schemas import SystemEvaluateRequest
from app.simcore.devices import AIR_DENSITY_KG_M3, VAPORIZATION_HEAT_KJ_KG
from app.simcore.project import build_runtime_layout
from app.simcore.psychrometrics import (
    humidity_ratio_from_rh,
    rh_from_humidity_ratio,
)
from app.simcore.session import SimulationConfig, SimulationSession, SimulationMode

TARGET = 24.0


for name in ("air", "multi_zone_all_air"):
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

    print(f"=== {name}")
    for rid in sorted(snap.rooms):
        r = snap.rooms[rid]
        w = humidity_ratio_from_rh(r.temperature_c, r.relative_humidity_pct)
        print(
            f"  {rid:14s} T={r.temperature_c:5.2f} RH={r.relative_humidity_pct:5.1f} "
            f"RH@24={rh_from_humidity_ratio(TARGET, w):5.1f}% w={w:.6f} "
            f"alloc_air={r.airflow_m3_h:7.0f}"
        )
        print(
            f"      internal_latent={r.internal_latent_kw:6.3f} "
            f"fresh_air_latent={r.fresh_air_latent_kw:6.3f} "
            f"dehumidification={r.dehumidification_kw:6.3f} "
            f"net_latent={r.net_latent_kw:6.3f}"
        )
        print(
            f"      cooling_kw={r.cooling_kw:6.3f} "
            f"internal_gain={r.internal_gain_kw:6.3f} "
            f"envelope={r.envelope_kw:6.3f}"
        )
    # device level
    for node in layout.devices:
        if node.rated.role.value not in {"fcu", "ahu", "fresh_air_unit"}:
            continue
        d = snap.devices[node.instance_id]
        print(
            f"    dev {node.instance_id:10s} {node.rated.model_name:14s} "
            f"air={d.values.get('airflow_m3_h', 0):7.0f} "
            f"kw={d.values.get('cooling_kw', 0):7.2f} "
            f"fan={d.values.get('fan_speed_pct', 0):5.1f} "
            f"valve={d.values.get('valve_opening_pct', 0):5.1f} "
            f"supT={d.values.get('supply_air_temp_c', 0):6.2f}"
        )
    print()
