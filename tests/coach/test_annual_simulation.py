from __future__ import annotations

import numpy as np
import pandas as pd

from mobility.coach.annual_simulation import simulate_coach_chain_year, simulate_coach_fleet_year
from mobility.coach.year_schedule import annual_dates
from mobility.core.constants import STEP_HOURS_DECISION, STEPS_PER_DAY_DECISION


def _journeys(*, cross_midnight: bool = False) -> pd.DataFrame:
    if cross_midnight:
        start_h = [23.0]
        end_h = [25.0]
        distance = [80.0]
        ids = ["JX"]
        codes = ["VJX"]
        positions = [1]
    else:
        start_h = [8.0, 14.0]
        end_h = [10.0, 16.0]
        distance = [80.0, 90.0]
        ids = ["J1", "J2"]
        codes = ["VJ1", "VJ2"]
        positions = [1, 2]
    return pd.DataFrame(
        {
            "journey_id": ids,
            "vehicle_journey_code": codes,
            "position_in_chain": positions,
            "start_h": start_h,
            "end_h": end_h,
            "distance_km": distance,
            "start_lsoa": ["E01000001"] * len(ids),
            "end_lsoa": ["E01000002"] * len(ids),
        }
    )


def _chains(dates) -> pd.DataFrame:
    records = []
    for active_date in dates:
        records.extend(
            [
                {"journey_id": "J1", "date": active_date, "coach_chain_id": "C1", "position_in_chain": 1},
                {"journey_id": "J2", "date": active_date, "coach_chain_id": "C1", "position_in_chain": 2},
            ]
        )
    return pd.DataFrame(records)


def _fleet() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "EV_ID": ["EV1"],
            "Model": ["Coach EV"],
            "Energy_kWh": [400.0],
            "consumption_kwh_per_km": [0.8],
            "count": [1.0],
        }
    )


def test_simulate_coach_fleet_year_returns_one_chain_and_full_load_profile() -> None:
    dates = annual_dates()

    per_chain, load_profile = simulate_coach_fleet_year(
        _chains(dates),
        _fleet(),
        _journeys(),
        seed=42,
        warm_up_days=0,
    )

    assert len(per_chain) == 1
    assert len(load_profile) == len(dates) * STEPS_PER_DAY_DECISION
    assert per_chain.loc[0, "energy_charged_kwh"] > 0.0
    assert per_chain.loc[0, "n_active_days"] == len(dates)


def test_cross_midnight_chain_soc_is_continuous_at_day_boundary() -> None:
    dates = annual_dates()
    result = simulate_coach_chain_year(
        "CX",
        _journeys(cross_midnight=True),
        {"EV_ID": "EV1", "Energy_kWh": 400.0, "consumption_kwh_per_km": 0.5},
        [dates[0]],
        warm_up_days=0,
        soc_init=1.0,
        terminus_charge_kw=0.0,
    )

    soc = result["soc"]
    assert len(soc) == len(dates) * STEPS_PER_DAY_DECISION
    assert np.isfinite(soc).all()
    expected_step_drop = (80.0 * 0.5 / 400.0) / (2.0 / STEP_HOURS_DECISION)
    left_step_drop = float(soc[STEPS_PER_DAY_DECISION - 2] - soc[STEPS_PER_DAY_DECISION - 1])
    right_step_drop = float(soc[STEPS_PER_DAY_DECISION] - soc[STEPS_PER_DAY_DECISION + 1])
    assert left_step_drop > 0.0
    assert right_step_drop > 0.0
    assert abs(left_step_drop - expected_step_drop) < 1e-9
    assert abs(right_step_drop - expected_step_drop) < 1e-9


def test_warm_up_days_burns_in_soc() -> None:
    dates = annual_dates()
    ev_spec = {"EV_ID": "EV1", "Energy_kWh": 400.0, "consumption_kwh_per_km": 0.8}

    cold = simulate_coach_chain_year(
        "C1",
        _journeys(),
        ev_spec,
        dates,
        warm_up_days=0,
        soc_init=1.0,
        terminus_charge_kw=50.0,
    )
    warmed = simulate_coach_chain_year(
        "C1",
        _journeys(),
        ev_spec,
        dates,
        warm_up_days=14,
        soc_init=1.0,
        terminus_charge_kw=50.0,
    )

    assert warmed["load_kw"].shape[0] == STEPS_PER_DAY_DECISION * len(dates)
    assert warmed["soc_after_warmup"] < 1.0
    assert warmed["n_active_days"] == len(dates)
    assert sum(1 for schedule in warmed["schedules"][: len(dates)] if schedule.trips) == len(dates)
    assert not np.isclose(
        warmed["load_kw"][:STEPS_PER_DAY_DECISION].sum(),
        cold["load_kw"][:STEPS_PER_DAY_DECISION].sum(),
    )


def test_layover_off_lowers_energy_charged_vs_on() -> None:
    dates = annual_dates()
    common = {
        "chain_id": "C1",
        "chain_journeys": _journeys(),
        "ev_spec": {"EV_ID": "EV1", "Energy_kWh": 400.0, "consumption_kwh_per_km": 0.8},
        "active_dates": [dates[0]],
        "warm_up_days": 0,
        "soc_init": 0.5,
        "terminus_charge_kw": 0.0,
    }

    off = simulate_coach_chain_year(**common)
    on = simulate_coach_chain_year(
        **common,
        allow_layover_charging=True,
        layover_charge_kw=50.0,
        min_layover_for_charging_h=1.0,
    )

    assert on["energy_charged_kwh"] > off["energy_charged_kwh"]
    assert on["layover_kwh"] > off["layover_kwh"]
