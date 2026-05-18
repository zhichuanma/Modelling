from __future__ import annotations

import pandas as pd

from mobility.coach.annual_simulation import simulate_coach_chain_year, simulate_coach_chain_year_with_retry
from mobility.coach.year_schedule import annual_dates


def _chain(*, distance_km: float = 80.0, layover_lsoa: str = "E01_OK") -> pd.DataFrame:
    return pd.DataFrame(
        {
            "journey_id": ["J1", "J2"],
            "vehicle_journey_code": ["VJ1", "VJ2"],
            "position_in_chain": [1, 2],
            "start_h": [8.0, 14.0],
            "end_h": [10.0, 16.0],
            "distance_km": [distance_km, distance_km],
            "start_lsoa": ["E01_START", layover_lsoa],
            "end_lsoa": [layover_lsoa, "E01_END"],
        }
    )


def _run_retry(chain: pd.DataFrame, *, eligible_lsoas: set[str], battery_kwh: float = 120.0) -> dict:
    return simulate_coach_chain_year_with_retry(
        "C1",
        chain,
        {"EV_ID": "EV1", "Energy_kWh": battery_kwh, "consumption_kwh_per_km": 1.0},
        [annual_dates()[0]],
        eligible_layover_lsoas=eligible_lsoas,
        layover_charge_kw_for_retry=120.0,
        min_layover_for_charging_h_for_retry=1.0,
        warm_up_days=0,
        soc_init=1.0,
        terminus_charge_kw=0.0,
    )


def test_retry_not_used_for_feasible_chain() -> None:
    result = _run_retry(_chain(distance_km=20.0), eligible_lsoas={"E01_OK"}, battery_kwh=400.0)

    assert result["retry_used"] is False
    assert result["feasible"] is True


def test_retry_not_used_without_eligible_lsoa() -> None:
    result = _run_retry(_chain(), eligible_lsoas=set())

    assert result["retry_used"] is False
    assert result["retry_reason"] == "no_eligible_lsoa_on_chain"
    assert result["feasible"] is False


def test_retry_can_make_infeasible_chain_feasible() -> None:
    chain = _chain()
    pass1 = simulate_coach_chain_year(
        "C1",
        chain,
        {"EV_ID": "EV1", "Energy_kWh": 120.0, "consumption_kwh_per_km": 1.0},
        [annual_dates()[0]],
        warm_up_days=0,
        soc_init=1.0,
        terminus_charge_kw=0.0,
    )
    result = _run_retry(chain, eligible_lsoas={"E01_OK"})

    assert result["retry_used"] is True
    assert result["retry_reason"] == "infeasible_pass1_eligible_lsoa_present"
    assert result["pass1_feasible"] is False
    assert result["feasible"] is True
    assert result["energy_charged_kwh"] > pass1["energy_charged_kwh"]


def test_retry_not_used_when_eligible_lsoa_not_on_chain() -> None:
    result = _run_retry(_chain(layover_lsoa="E01_X"), eligible_lsoas={"E01_Z"})

    assert result["retry_used"] is False
    assert result["retry_reason"] == "no_eligible_lsoa_on_chain"
    assert result["feasible"] is False
