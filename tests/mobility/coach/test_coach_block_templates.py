"""Unit tests for the coach -> bus-schema stage-0 adapter (plan 2026-06-05)."""

from __future__ import annotations

import datetime as dt

import numpy as np
import pandas as pd
import pytest

from mobility.bus.annual_depot_events import haversine_km
from mobility.coach.coach_block_templates import (
    COACH_BLOCK_SOURCE,
    attach_journey_endpoints,
    build_coach_block_templates,
    expand_coach_block_instances,
)
from mobility.coach.coach_ev_specs import build_ev_coach_specs, coach_ev_specs_summary


P1_END = (51.20, -1.20)   # journey A ends here
P2_START = (51.40, -1.40)  # journey B starts here (~30 km relocation)


def _journeys() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "journey_id": ["jA", "jB"],
            "vehicle_journey_code": ["VJ_A", "VJ_B"],
            "operator_code": ["NX", "NX"],
            "start_h": [8.0, 11.0],
            "end_h": [10.0, 25.5],  # jB crosses midnight (TxC end_h <= 48 convention)
            "distance_km": [120.0, 150.0],
            "start_lat": [51.00, P2_START[0]],
            "start_lon": [-1.00, P2_START[1]],
            "end_lat": [P1_END[0], 51.00],
            "end_lon": [P1_END[1], -1.00],
            "start_lsoa": ["E1", "E2"],
            "end_lsoa": ["E2", "E1"],
            "start_stop": ["S1", "S2"],
            "end_stop": ["S2", "S1"],
        }
    )


def _chains_long(dates: list[str]) -> pd.DataFrame:
    rows = []
    for date in dates:
        for position, journey in enumerate(["jA", "jB"], start=1):
            rows.append(
                {
                    "journey_id": journey,
                    "date": dt.date.fromisoformat(date),
                    "coach_chain_id": f"NX_{date}_001",
                    "position_in_chain": position,
                    "coach_chain_template_id": "NX_abc123",
                    "operator_code": "NX",
                }
            )
    return pd.DataFrame(rows)


EXPECTED_RELOCATION = haversine_km(P1_END[0], P1_END[1], P2_START[0], P2_START[1])


def test_template_fields_match_hand_computed() -> None:
    templates, diagnostics = build_coach_block_templates(_journeys(), _chains_long(["2026-04-17"]))
    assert len(templates) == 1
    template = templates.iloc[0]
    assert template["block_template_id"] == "ct_NX_abc123"
    assert template["agency_id"] == "NX"
    assert template["service_id"] == "ct_NX_abc123"
    assert template["block_source"] == COACH_BLOCK_SOURCE
    assert template["n_trips"] == 2
    assert template["start_h"] == pytest.approx(8.0)
    assert template["end_h"] == pytest.approx(25.5)
    assert template["duration_h"] == pytest.approx(17.5)
    assert template["coach_passenger_km"] == pytest.approx(270.0)
    assert template["relocation_km_total"] == pytest.approx(EXPECTED_RELOCATION)
    # Energy-relevant on-vehicle km: journeys + relocations (screen budgets both).
    assert template["passenger_distance_km"] == pytest.approx(270.0 + EXPECTED_RELOCATION)
    assert template["trip_ids"] == ["jA", "jB"]
    assert template["trip_start_times"] == [8.0, 11.0]
    assert template["trip_end_times"] == [10.0, 25.5]
    assert template["trip_distances_km"] == [120.0, 150.0]
    assert template["start_lat"] == pytest.approx(51.0)
    assert template["end_lat"] == pytest.approx(51.0)
    assert int(diagnostics.iloc[0]["n_cross_midnight_templates"]) == 1
    assert float(diagnostics.iloc[0]["total_relocation_km"]) == pytest.approx(EXPECTED_RELOCATION)


def test_short_relocation_gap_not_counted() -> None:
    journeys = _journeys()
    # Move jB's start to ~0 km from jA's end: below the qualifying threshold.
    journeys.loc[journeys["journey_id"] == "jB", ["start_lat", "start_lon"]] = [P1_END[0], P1_END[1]]
    templates, _ = build_coach_block_templates(journeys, _chains_long(["2026-04-17"]))
    assert templates.iloc[0]["relocation_km_total"] == pytest.approx(0.0)
    assert templates.iloc[0]["passenger_distance_km"] == pytest.approx(270.0)


def test_template_invariant_violation_raises() -> None:
    chains = _chains_long(["2026-04-17", "2026-04-18"])
    # Same template hash but day 2 chains only jA -> ordering mismatch must be fatal.
    chains = chains.loc[~((chains["date"] == dt.date(2026, 4, 18)) & (chains["journey_id"] == "jB"))]
    with pytest.raises(RuntimeError, match="invariant violated"):
        build_coach_block_templates(_journeys(), chains)


def test_instances_expand_per_chain_date_with_cross_midnight() -> None:
    dates = ["2026-04-17", "2026-04-18", "2026-04-19"]
    templates, _ = build_coach_block_templates(_journeys(), _chains_long(dates))
    templates = templates.assign(region_key="London")
    instances, diagnostics = expand_coach_block_instances(templates, _chains_long(dates), start_date="2026-04-17", end_date="2026-04-18")
    # Range clipping: only 2 of 3 dates.
    assert sorted(instances["service_date"].unique()) == ["2026-04-17", "2026-04-18"]
    assert len(instances) == 2
    first = instances.iloc[0]
    assert pd.Timestamp(first["start_datetime"]) == pd.Timestamp("2026-04-17 08:00")
    # end_h 25.5 -> next calendar day 01:30 (cross-midnight preserved).
    assert pd.Timestamp(first["end_datetime"]) == pd.Timestamp("2026-04-18 01:30")
    assert instances["block_instance_id"].is_unique
    assert bool(diagnostics.iloc[0]["block_instance_ids_unique"])
    required_bus_columns = {
        "service_date",
        "block_instance_id",
        "block_template_id",
        "agency_id",
        "service_id",
        "block_id",
        "block_source",
        "start_datetime",
        "end_datetime",
        "duration_h",
        "passenger_distance_km",
        "start_lat",
        "start_lon",
        "end_lat",
        "end_lon",
        "region_key",
    }
    assert required_bus_columns.issubset(instances.columns)


def test_attach_journey_endpoints_from_stop_sequences() -> None:
    journeys = _journeys().drop(columns=["start_lat", "start_lon", "end_lat", "end_lon", "start_stop", "end_stop"])
    stop_sequences = pd.DataFrame(
        {
            "journey_id": ["jA", "jA", "jB", "jB"],
            "stop_sequence": [1, 2, 1, 2],
            "stop_point_ref": ["S1", "S2", "S2", "S1"],
            "lat": [51.0, P1_END[0], P2_START[0], 51.0],
            "lon": [-1.0, P1_END[1], P2_START[1], -1.0],
        }
    )
    out = attach_journey_endpoints(journeys, stop_sequences)
    row = out.loc[out["journey_id"] == "jA"].iloc[0]
    assert row["start_lat"] == pytest.approx(51.0)
    assert row["end_lat"] == pytest.approx(P1_END[0])
    assert row["start_stop"] == "S1"
    assert row["end_stop"] == "S2"


def _inventory(n_tc9: int = 1) -> pd.DataFrame:
    rows = [
        {"EV_ID": "coach_1", "LSOA_code": "E1", "Model": "YUTONG TC12", "count": 2, "vehicle_subtype": "coach",
         "Energy_kWh": 281.0, "DC_Power_kW": 150.0, "AC_Power_kW": 22.0, "efficiency_wh_per_km": 810.0},
        {"EV_ID": "coach_2", "LSOA_code": "E1", "Model": "YUTONG TC12", "count": 2, "vehicle_subtype": "coach",
         "Energy_kWh": 281.0, "DC_Power_kW": 150.0, "AC_Power_kW": 22.0, "efficiency_wh_per_km": 810.0},
        {"EV_ID": "car_1", "LSOA_code": "E1", "Model": "SOME CAR", "count": 1, "vehicle_subtype": "car",
         "Energy_kWh": 60.0, "DC_Power_kW": 100.0, "AC_Power_kW": 7.0, "efficiency_wh_per_km": 160.0},
    ]
    for index in range(n_tc9):
        rows.append(
            {"EV_ID": f"coach_tc9_{index}", "LSOA_code": "E2", "Model": "YUTONG TC9", "count": n_tc9, "vehicle_subtype": "coach",
             "Energy_kWh": np.nan, "DC_Power_kW": np.nan, "AC_Power_kW": 22.0, "efficiency_wh_per_km": 810.0}
        )
    return pd.DataFrame(rows)


def test_coach_specs_row_as_vehicle_not_count_expanded() -> None:
    specs, _ = build_ev_coach_specs(_inventory())
    # 3 coach rows -> 3 specs; count column (sums to 5) is never expanded.
    assert len(specs) == 3
    assert specs["vehicle_spec_id"].is_unique
    assert (specs["vehicle_subtype"] == "coach").all()


def test_tc9_imputed_from_tc12_and_flagged() -> None:
    specs, _ = build_ev_coach_specs(_inventory())
    tc9 = specs.loc[specs["vehicle_model"] == "YUTONG TC9"]
    assert len(tc9) == 1
    assert tc9.iloc[0]["battery_kwh"] == pytest.approx(281.0)
    assert tc9.iloc[0]["battery_source"] == "imputed_from_tc12"
    assert tc9.iloc[0]["ac_charge_kw_max"] == pytest.approx(150.0)  # dc channel default
    summary = coach_ev_specs_summary(_inventory(), specs, _)
    assert summary["n_battery_imputed_from_tc12"] == 1


def test_tc9_dropped_when_imputation_disabled() -> None:
    specs, diagnostics = build_ev_coach_specs(_inventory(), impute_tc9=False)
    assert len(specs) == 2
    dropped = diagnostics.loc[~diagnostics["sanity_valid"]]
    assert (dropped["drop_reason"] == "invalid_battery_kwh").all()


def test_charge_side_channels() -> None:
    dc_specs, _ = build_ev_coach_specs(_inventory())
    ac_specs, _ = build_ev_coach_specs(_inventory(), charge_side="ac")
    assert (dc_specs["ac_charge_kw_max"] == 150.0).all()
    assert (ac_specs["ac_charge_kw_max"] == 22.0).all()
    assert (dc_specs["ac_charge_kw_max_inventory"] == 22.0).all()
    with pytest.raises(ValueError):
        build_ev_coach_specs(_inventory(), charge_side="both")
