from __future__ import annotations

import pytest

from mobility.coach.feasibility import journey_feasibility


def test_journey_feasibility_feasible() -> None:
    result = journey_feasibility(100.0, battery_kwh=200.0, consumption_kwh_per_km=1.0)

    assert result["energy_required_kwh"] == 100.0
    assert result["usable_energy_kwh"] == 190.0
    assert result["feasible_single_charge"] is True
    assert result["shortfall_kwh"] == 0.0
    assert "min_soc_required" in result
    assert 0.0 <= result["min_soc_required"] <= 2.0


def test_journey_feasibility_infeasible_shortfall() -> None:
    result = journey_feasibility(220.0, battery_kwh=200.0, consumption_kwh_per_km=1.0)

    assert result["feasible_single_charge"] is False
    assert result["shortfall_kwh"] == pytest.approx(30.0)
    assert result["min_soc_required"] == pytest.approx(1.15)
