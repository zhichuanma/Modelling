"""Single-charge feasibility checks for coach journeys."""

from __future__ import annotations


def journey_feasibility(
    distance_km: float,
    *,
    battery_kwh: float,
    consumption_kwh_per_km: float,
    safety_margin: float = 0.05,
) -> dict:
    """Return a transparent single-charge feasibility summary."""
    if distance_km is None:
        raise ValueError("distance_km must be known for feasibility checks.")
    if distance_km < 0.0:
        raise ValueError("distance_km must be non-negative.")
    if battery_kwh <= 0.0:
        raise ValueError("battery_kwh must be positive.")
    if consumption_kwh_per_km <= 0.0:
        raise ValueError("consumption_kwh_per_km must be positive.")
    if not 0.0 <= safety_margin < 1.0:
        raise ValueError("safety_margin must be in [0, 1).")

    energy_required_kwh = float(distance_km) * float(consumption_kwh_per_km)
    usable_energy_kwh = float(battery_kwh) * (1.0 - float(safety_margin))
    shortfall_kwh = max(0.0, energy_required_kwh - usable_energy_kwh)
    min_soc_required = energy_required_kwh / float(battery_kwh) + float(safety_margin)
    return {
        "feasible_single_charge": bool(shortfall_kwh == 0.0),
        "energy_required_kwh": float(energy_required_kwh),
        "usable_energy_kwh": float(usable_energy_kwh),
        "shortfall_kwh": float(shortfall_kwh),
        "min_soc_required": float(min_soc_required),
    }
