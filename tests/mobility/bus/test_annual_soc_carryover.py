"""Unit tests for SOC carry-over: state machine, stitching, idle charging,
carryover SOC walk, carryover assignment screening (plan v2 §3.3/§5/§8/§10.3)."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from mobility.bus.annual_depot_events import build_vehicle_day_events
from mobility.bus.annual_depot_load import aggregate_depot_load_15min, depot_load_energy_matches_events
from mobility.bus.annual_depot_outputs import (
    CARRYOVER_LIMITATIONS,
    REQUIRED_LIMITATIONS,
    build_run_summary_markdown,
    limitations_for_soc_mode,
)
from mobility.bus.annual_depot_soc import apply_depot_only_soc
from mobility.bus.annual_soc_state import (
    IDLE_EVENT_TYPE,
    PendingWindow,
    advance_state_after_walk,
    available_from_by_spec,
    finalize_day_frames,
    first_event_start_by_spec,
    initialize_soc_state,
    project_available_kwh,
    soc_init_by_vehicle_day,
    soc_state_from_frame,
    soc_state_to_frame,
    stitch_pendings,
)
from mobility.bus.annual_vehicle_day_assignment import (
    assign_vehicle_days_for_date,
    build_matching_context,
)


def _specs(n: int = 1, *, battery: float = 100.0, ac: float = 50.0, home_status: str = "assigned") -> pd.DataFrame:
    return pd.DataFrame(
        {
            "vehicle_spec_id": [f"ev{i}" for i in range(1, n + 1)],
            "battery_kwh": [battery] * n,
            "consumption_kwh_per_km": [1.0] * n,
            "ac_charge_kw_max": [ac] * n,
            "usable_soc_min": [0.10] * n,
            "usable_soc_max": [0.95] * n,
            "home_depot_id": ["opdepot_OP_E1"] * n,
            "home_depot_lsoa": ["E1"] * n,
            "home_depot_lat": [51.0] * n,
            "home_depot_lon": [-1.0] * n,
            "home_depot_status": [home_status] * n,
        }
    )


def _pending(
    *,
    is_idle: bool = False,
    window_start: str = "2026-04-17 22:00",
    seam_end: str = "2026-04-18 06:00",
    power: float = 5.0,
    soc_at_open: float = 30.0,
    vehicle_day_id: str = "vd_2026-04-17_00000_ev1",
    service_date: str = "2026-04-17",
) -> PendingWindow:
    return PendingWindow(
        vehicle_day_id=vehicle_day_id,
        service_date=service_date,
        is_idle=is_idle,
        depot_id="opdepot_OP_E1",
        depot_lsoa="E1",
        depot_lat=51.0,
        depot_lon=-1.0,
        window_start=pd.Timestamp(window_start),
        seam_end=pd.Timestamp(seam_end),
        charge_power_kw=power,
        soc_at_open_kwh=soc_at_open,
    )


def _state_with_pending(pending: PendingWindow):
    state = initialize_soc_state(_specs(), start_ts=pd.Timestamp("2026-04-17"))
    state["ev1"].pending = pending
    state["ev1"].soc_kwh = pending.soc_at_open_kwh
    state["ev1"].last_event_end_ts = pending.window_start
    return state


def test_initialize_state_at_usable_max() -> None:
    state = initialize_soc_state(_specs(), start_ts=pd.Timestamp("2026-04-17"))
    assert state["ev1"].soc_kwh == pytest.approx(95.0)
    assert state["ev1"].pending is None
    assert state["ev1"].valid_params

    unassigned = initialize_soc_state(_specs(home_status="unassigned"), start_ts=pd.Timestamp("2026-04-17"))
    assert not unassigned["ev1"].valid_params


def test_project_available_kwh_charges_pending_to_seam() -> None:
    # 8 h at 5 kW from 30 kWh -> 70 kWh at the seam; floor is 10 kWh.
    state = _state_with_pending(_pending())
    available = project_available_kwh(state)
    assert available["ev1"] == pytest.approx(70.0 - 10.0)


def test_project_available_clamps_at_usable_max() -> None:
    state = _state_with_pending(_pending(power=50.0))
    available = project_available_kwh(state)
    assert available["ev1"] == pytest.approx(95.0 - 10.0)


def test_stitch_truncates_at_next_first_event() -> None:
    state = _state_with_pending(_pending())
    result = stitch_pendings(state, {"ev1": pd.Timestamp("2026-04-18 04:00")})["ev1"]
    assert result.stitch_ts == pd.Timestamp("2026-04-18 04:00")
    # 6 h at 5 kW from 30 kWh.
    assert result.soc_at_stitch_kwh == pytest.approx(60.0)


def test_stitch_unmatched_runs_to_seam() -> None:
    state = _state_with_pending(_pending())
    result = stitch_pendings(state, {})["ev1"]
    assert result.stitch_ts == pd.Timestamp("2026-04-18 06:00")
    assert result.soc_at_stitch_kwh == pytest.approx(70.0)


def test_available_from_is_pending_window_start() -> None:
    state = _state_with_pending(_pending())
    assert available_from_by_spec(state)["ev1"] == pd.Timestamp("2026-04-17 22:00")


def test_soc_state_checkpoint_roundtrip() -> None:
    state = _state_with_pending(_pending())
    frame = soc_state_to_frame(state, "2026-04-17")
    rebuilt = soc_state_from_frame(frame)
    assert rebuilt["ev1"].soc_kwh == pytest.approx(state["ev1"].soc_kwh)
    assert rebuilt["ev1"].pending is not None
    assert rebuilt["ev1"].pending.window_start == state["ev1"].pending.window_start
    assert rebuilt["ev1"].pending.seam_end == state["ev1"].pending.seam_end
    assert rebuilt["ev1"].pending.charge_power_kw == pytest.approx(5.0)
    assert rebuilt["ev1"].pending.is_idle == state["ev1"].pending.is_idle


# --- events: carryover stitch windows ---------------------------------------


def _event_inputs(*, service_date: str = "2026-04-18", start_h: float = 8.0, end_h: float = 10.0):
    start_dt = pd.Timestamp(service_date) + pd.to_timedelta(start_h, unit="h")
    end_dt = pd.Timestamp(service_date) + pd.to_timedelta(end_h, unit="h")
    assignments = pd.DataFrame(
        {
            "service_date": [service_date],
            "vehicle_day_id": [f"vd_{service_date}_00000_ev1"],
            "vehicle_spec_id": ["ev1"],
            "block_instance_id": [f"bi_{service_date}"],
            "block_template_id": ["bt1"],
            "agency_id": ["OP"],
            "service_id": ["S1"],
            "block_id": ["B1"],
            "depot_id": ["opdepot_OP_E1"],
            "scenario_mode": ["ev_stock_scale"],
        }
    )
    block_instances = pd.DataFrame(
        {
            "service_date": [service_date],
            "block_instance_id": [f"bi_{service_date}"],
            "block_template_id": ["bt1"],
            "agency_id": ["OP"],
            "service_id": ["S1"],
            "block_id": ["B1"],
            "start_datetime": [start_dt],
            "end_datetime": [end_dt],
            "duration_h": [end_h - start_h],
            "passenger_distance_km": [20.0],
            "start_lat": [51.0],
            "start_lon": [-1.0],
            "end_lat": [51.0],
            "end_lon": [-1.0],
            "start_lsoa": ["E1"],
            "end_lsoa": ["E1"],
            "region_key": ["London"],
            "depot_lsoa": ["E1"],
        }
    )
    block_templates = pd.DataFrame({"block_template_id": ["bt1"]})
    depot_registry = pd.DataFrame(
        {
            "depot_id": ["opdepot_OP_E1"],
            "depot_lat": [51.0],
            "depot_lon": [-1.0],
            "depot_lsoa": ["E1"],
            "depot_confidence": ["high"],
        }
    )
    specs = _specs().drop(columns=["home_depot_status"]).assign(home_depot_status="assigned")
    return assignments, block_instances, block_templates, specs, depot_registry


def _build_events(stitch_start_by_spec, **kwargs):
    assignments, block_instances, block_templates, specs, depot_registry = _event_inputs(**kwargs)
    return build_vehicle_day_events(
        assignments,
        block_instances,
        block_templates,
        specs,
        depot_registry,
        use_trip_level_events=False,
        stitch_start_by_spec=stitch_start_by_spec,
    )


def test_carryover_pre_window_opens_at_seam_not_midnight() -> None:
    events = _build_events({"ev1": pd.Timestamp("2026-04-18 06:00")})
    pre = events.loc[events["event_type"] == "depot_parking_pre"]
    assert len(pre) == 1
    assert pd.Timestamp(pre.iloc[0]["start_datetime"]) == pd.Timestamp("2026-04-18 06:00")


def test_carryover_no_pre_window_for_early_pullout() -> None:
    events = _build_events({"ev1": pd.Timestamp("2026-04-18 06:00")}, start_h=4.0, end_h=6.0)
    assert not events["event_type"].eq("depot_parking_pre").any()
    assert pd.Timestamp(events.iloc[0]["start_datetime"]) == pd.Timestamp("2026-04-18 04:00")


def test_missing_stitch_start_is_fatal() -> None:
    with pytest.raises(KeyError, match="missing stitch start"):
        _build_events({})


def test_daily_reset_pre_window_still_opens_at_midnight() -> None:
    events = _build_events(None)
    pre = events.loc[events["event_type"] == "depot_parking_pre"]
    assert len(pre) == 1
    assert pd.Timestamp(pre.iloc[0]["start_datetime"]) == pd.Timestamp("2026-04-18 00:00")


# --- SOC walk: carryover init ------------------------------------------------


def test_carryover_walk_uses_injected_start_soc() -> None:
    events = _build_events({"ev1": pd.Timestamp("2026-04-18 06:00")})
    vd = str(events.iloc[0]["vehicle_day_id"])
    walked, summary = apply_depot_only_soc(events, soc_mode="carryover", soc_init_by_vehicle_day={vd: 50.0})
    assert walked.iloc[0]["soc_start_kwh"] == pytest.approx(50.0)
    assert summary.iloc[0]["start_soc_kwh"] == pytest.approx(50.0)
    assert "end_soc_kwh" in summary.columns


def test_carryover_walk_missing_state_raises() -> None:
    events = _build_events({"ev1": pd.Timestamp("2026-04-18 06:00")})
    with pytest.raises(KeyError, match="missing SOC state"):
        apply_depot_only_soc(events, soc_mode="carryover", soc_init_by_vehicle_day={})


def test_carryover_walk_requires_init_mapping() -> None:
    events = _build_events({"ev1": pd.Timestamp("2026-04-18 06:00")})
    with pytest.raises(ValueError, match="requires soc_init_by_vehicle_day"):
        apply_depot_only_soc(events, soc_mode="carryover")


def test_daily_reset_walk_unchanged_and_reports_end_soc() -> None:
    events = _build_events(None)
    walked, summary = apply_depot_only_soc(events)
    assert walked.iloc[0]["soc_start_kwh"] == pytest.approx(95.0)
    assert summary.iloc[0]["start_soc_kwh"] == pytest.approx(95.0)
    assert summary.iloc[0]["end_soc_kwh"] == pytest.approx(float(walked.iloc[-1]["soc_end_kwh"]))


def test_carryover_negative_soc_carries_unfloored_and_recovers() -> None:
    # A day ending below usable_min carries its unclamped SOC forward (§5.2);
    # recovery happens only through explicit charging in the pending window.
    pending = _pending(power=2.0, soc_at_open=-65.0, window_start="2026-04-17 22:00", seam_end="2026-04-18 06:00")
    state = _state_with_pending(pending)
    result = stitch_pendings(state, {})["ev1"]
    # 8 h at 2 kW from -65 kWh: recovers to -49, never silently floored.
    assert result.soc_at_stitch_kwh == pytest.approx(-49.0)
    # With enough power the same night recovers fully but never above usable_max.
    strong = _state_with_pending(_pending(power=50.0, soc_at_open=-65.0))
    assert stitch_pendings(strong, {})["ev1"].soc_at_stitch_kwh == pytest.approx(95.0)


# --- finalize: overnight truncation + idle events ----------------------------


def _walked_day_one():
    """Build + walk a full day-1 ledger with a low-power spec (1 kW effective)."""
    assignments, block_instances, block_templates, specs, depot_registry = _event_inputs(service_date="2026-04-17")
    specs = specs.assign(ac_charge_kw_max=1.0)
    events = build_vehicle_day_events(
        assignments,
        block_instances,
        block_templates,
        specs,
        depot_registry,
        use_trip_level_events=False,
        stitch_start_by_spec={"ev1": pd.Timestamp("2026-04-17 00:00")},
    )
    vd = str(assignments.iloc[0]["vehicle_day_id"])
    events, summary = apply_depot_only_soc(events, soc_mode="carryover", soc_init_by_vehicle_day={vd: 95.0})
    state = initialize_soc_state(specs, start_ts=pd.Timestamp("2026-04-17"))
    stitch0 = stitch_pendings(state, first_event_start_by_spec(events))
    advance_state_after_walk(
        state,
        stitch0,
        events,
        assignments,
        service_date="2026-04-17",
        depot_power_kw=100.0,
        default_overnight_end_hour=6.0,
    )
    return events, summary, state, assignments


def test_finalize_truncates_overnight_and_keeps_energy_identity() -> None:
    events, summary, state, _ = _walked_day_one()
    # Walked placeholder: overnight [10:00 -> 06:00 next day] at 1 kW from 75 kWh -> +20 kWh.
    overnight = events.loc[events["event_type"].isin({"depot_parking_overnight", "depot_parking_post"})].iloc[0]
    assert float(overnight["charge_kwh_added"]) == pytest.approx(20.0)
    # Next-day pull-out at 04:30 truncates the window to 18.5 h -> 18.5 kWh.
    stitch = stitch_pendings(state, {"ev1": pd.Timestamp("2026-04-18 04:30")})
    final_events, final_summary, stats = finalize_day_frames(events, summary, stitch, state)
    final_overnight = final_events.loc[final_events["event_type"].isin({"depot_parking_overnight", "depot_parking_post"})].iloc[0]
    assert pd.Timestamp(final_overnight["end_datetime"]) == pd.Timestamp("2026-04-18 04:30")
    assert float(final_overnight["charge_kwh_added"]) == pytest.approx(18.5)
    assert float(final_overnight["soc_end_kwh"]) == pytest.approx(75.0 + 18.5)
    assert stats["overnight_truncation_kwh"] == pytest.approx(1.5)
    assert float(final_summary.iloc[0]["end_soc_kwh"]) == pytest.approx(93.5)
    assert float(final_summary.iloc[0]["total_charge_kwh"]) == pytest.approx(18.5)
    # Energy identity: load aggregation over the finalized frame reconciles.
    registry = pd.DataFrame({"depot_id": ["opdepot_OP_E1"], "depot_lat": [51.0], "depot_lon": [-1.0], "depot_lsoa": ["E1"], "depot_confidence": ["high"]})
    load, _ = aggregate_depot_load_15min(final_events, registry, final_summary)
    assert depot_load_energy_matches_events(load, final_events)


def test_idle_event_emitted_only_below_usable_max() -> None:
    below = _state_with_pending(_pending(is_idle=True, vehicle_day_id="idle_2026-04-17_ev1", soc_at_open=30.0, power=5.0))
    stitch = stitch_pendings(below, {})
    events, _, stats = finalize_day_frames(
        pd.DataFrame(columns=_build_events(None).columns), pd.DataFrame(), stitch, below
    )
    idle = events.loc[events["event_type"] == IDLE_EVENT_TYPE]
    assert len(idle) == 1
    assert float(idle.iloc[0]["charge_kwh_added"]) == pytest.approx(40.0)
    assert idle.iloc[0]["vehicle_spec_id"] == "ev1"
    assert idle.iloc[0]["depot_id"] == "opdepot_OP_E1"
    assert float(idle.iloc[0]["battery_kwh"]) == pytest.approx(100.0)
    assert stats["n_idle_charging_events"] == 1

    at_max = _state_with_pending(_pending(is_idle=True, vehicle_day_id="idle_2026-04-17_ev1", soc_at_open=95.0, power=5.0))
    stitch_max = stitch_pendings(at_max, {})
    events_max, _, stats_max = finalize_day_frames(pd.DataFrame(columns=_build_events(None).columns), pd.DataFrame(), stitch_max, at_max)
    assert not events_max["event_type"].eq(IDLE_EVENT_TYPE).any() if not events_max.empty else True
    assert stats_max["n_idle_charging_events"] == 0
    # State still ticks: the stitch result exists and carries the SOC forward.
    assert stitch_max["ev1"].soc_at_stitch_kwh == pytest.approx(95.0)


def test_advance_state_ticks_idle_and_matched_specs() -> None:
    events, _, state, assignments = _walked_day_one()
    # ev1 matched -> duty pending taken from the walked overnight window.
    pending = state["ev1"].pending
    assert pending is not None and not pending.is_idle
    assert pending.window_start == pd.Timestamp("2026-04-17 10:00")
    assert pending.seam_end == pd.Timestamp("2026-04-18 06:00")
    assert pending.soc_at_open_kwh == pytest.approx(75.0)

    # An idle spec gets an idle pending the same day (state ticks without events).
    specs2 = _specs(n=2, ac=1.0)
    state2 = initialize_soc_state(specs2, start_ts=pd.Timestamp("2026-04-17"))
    stitch = stitch_pendings(state2, {})
    advance_state_after_walk(
        state2, stitch, pd.DataFrame(), pd.DataFrame(), service_date="2026-04-17", depot_power_kw=100.0, default_overnight_end_hour=6.0
    )
    for spec_id in ("ev1", "ev2"):
        assert state2[spec_id].pending is not None
        assert state2[spec_id].pending.is_idle
        assert state2[spec_id].pending.seam_end == pd.Timestamp("2026-04-18 06:00")


def test_idle_policy_none_keeps_window_with_zero_power() -> None:
    specs = _specs()
    state = initialize_soc_state(specs, start_ts=pd.Timestamp("2026-04-17"))
    state["ev1"].soc_kwh = 30.0
    stitch = stitch_pendings(state, {})
    advance_state_after_walk(
        state,
        stitch,
        pd.DataFrame(),
        pd.DataFrame(),
        service_date="2026-04-17",
        depot_power_kw=100.0,
        default_overnight_end_hour=6.0,
        idle_vehicle_charging_policy="none",
    )
    assert state["ev1"].pending is not None
    assert state["ev1"].pending.charge_power_kw == 0.0
    # No energy is ever added without an event: SOC stays put at the next stitch.
    assert stitch_pendings(state, {})["ev1"].soc_at_stitch_kwh == pytest.approx(30.0)


# --- carryover assignment screening ------------------------------------------


def _day_blocks(*, distance_km: float = 50.0, start: str = "2026-04-18 08:00") -> pd.DataFrame:
    start_dt = pd.Timestamp(start)
    return pd.DataFrame(
        {
            "service_date": ["2026-04-18"],
            "block_instance_id": ["bi_1"],
            "block_template_id": ["bt1"],
            "agency_id": ["OP"],
            "service_id": ["S1"],
            "block_id": ["B1"],
            "depot_id": ["opdepot_OP_E1"],
            "region_key": ["London"],
            "passenger_distance_km": [distance_km],
            "duration_h": [2.0],
            "start_datetime": [start_dt],
            "end_datetime": [start_dt + pd.Timedelta(hours=2)],
            "start_lat": [51.0],
            "start_lon": [-1.0],
            "end_lat": [51.0],
            "end_lon": [-1.0],
            "depot_lat": [51.0],
            "depot_lon": [-1.0],
        }
    )


def test_carryover_screening_uses_carried_soc() -> None:
    context = build_matching_context(_specs(), home_depot_radius_km=10.0)
    low = assign_vehicle_days_for_date(
        _day_blocks(),
        context,
        service_date="2026-04-18",
        seed=1,
        soc_mode="carryover",
        available_kwh_by_spec={"ev1": 20.0},
        available_from_by_spec={"ev1": pd.Timestamp("2026-04-17 00:00")},
    )
    assert not low[0]
    assert low[2][0]["unmatched_reason"] == "no_feasible_vehicle_in_radius"

    high = assign_vehicle_days_for_date(
        _day_blocks(),
        context,
        service_date="2026-04-18",
        seed=1,
        soc_mode="carryover",
        available_kwh_by_spec={"ev1": 60.0},
        available_from_by_spec={"ev1": pd.Timestamp("2026-04-17 00:00")},
    )
    assert len(high[0]) == 1
    assert high[0][0]["available_kwh_at_assignment"] == pytest.approx(60.0)
    assert high[0][0]["daily_soc_mode"] == "carryover"
    assert high[1][0]["daily_soc_mode"] == "carryover"


def test_carryover_temporal_guard_vehicle_busy_overnight() -> None:
    context = build_matching_context(_specs(), home_depot_radius_km=10.0)
    records = assign_vehicle_days_for_date(
        _day_blocks(start="2026-04-18 05:00"),
        context,
        service_date="2026-04-18",
        seed=1,
        soc_mode="carryover",
        available_kwh_by_spec={"ev1": 85.0},
        # Vehicle returns from the previous duty after this block's pull-out.
        available_from_by_spec={"ev1": pd.Timestamp("2026-04-18 07:30")},
    )
    assert not records[0]
    assert records[2][0]["unmatched_reason"] == "vehicle_busy_overnight"
    assert records[1][0]["n_unmatched_vehicle_busy_overnight"] == 1
    assert records[1][0]["n_unmatched_no_feasible_vehicle"] == 1


def test_carryover_missing_available_kwh_is_fatal() -> None:
    context = build_matching_context(_specs(), home_depot_radius_km=10.0)
    with pytest.raises(KeyError):
        assign_vehicle_days_for_date(
            _day_blocks(),
            context,
            service_date="2026-04-18",
            seed=1,
            soc_mode="carryover",
            available_kwh_by_spec={},
            available_from_by_spec={"ev1": pd.Timestamp("2026-04-17 00:00")},
        )


def test_soc_init_by_vehicle_day_missing_spec_is_fatal() -> None:
    assignments = pd.DataFrame({"vehicle_day_id": ["vd_x"], "vehicle_spec_id": ["ev_unknown"]})
    with pytest.raises(KeyError, match="missing SOC state"):
        soc_init_by_vehicle_day(assignments, {})


# --- run summary / limitations -----------------------------------------------


def test_limitations_switch_by_soc_mode() -> None:
    daily = limitations_for_soc_mode("daily_reset")
    assert daily == REQUIRED_LIMITATIONS
    carry = limitations_for_soc_mode("carryover")
    assert all(line in carry for line in CARRYOVER_LIMITATIONS)
    assert not any("does not model multi-day SOC carry-over" in line for line in carry)


def test_run_summary_records_carryover_fields() -> None:
    markdown = build_run_summary_markdown(
        preflight_summary={"n_trip_rows": 1},
        block_templates=pd.DataFrame(),
        block_instances=pd.DataFrame(),
        depot_registry=pd.DataFrame(),
        ev_bus_specs=pd.DataFrame(),
        vehicle_day_assignments=pd.DataFrame(),
        assignment_diagnostics=None,
        vehicle_day_soc_summary=pd.DataFrame(),
        depot_load_15min=pd.DataFrame(),
        depot_daily_summary=pd.DataFrame(),
        feed_year_start="2026-04-17",
        feed_year_end="2027-01-17",
        scenario_mode="ev_stock_scale",
        soc_mode="carryover",
        preaggregated_stats={
            "soc_day_boundary_hour": 6.0,
            "warmup_days": 14,
            "warmup_start_date": "2026-04-17",
            "warmup_end_date": "2026-04-30",
            "idle_vehicle_charging_policy": "home_depot",
            "n_idle_charging_events": 12,
            "idle_charge_kwh": 345.6,
            "idle_charge_share": 0.01,
            "n_temporal_overlap_exclusions": 7,
            "n_matched_but_walk_infeasible": 3,
            "calendar_decay_suspect_dates": ["2027-01-10"],
            "calendar_decay_median_active_blocks": 55000.0,
            "calendar_decay_floor": 27500.0,
        },
    )
    assert "- soc_mode: carryover" in markdown
    assert "## Carry-over configuration" in markdown
    assert "- warmup_days: 14" in markdown
    assert "- idle_vehicle_charging_policy: home_depot" in markdown
    assert "- n_idle_charging_events: 12" in markdown
    assert "- n_temporal_overlap_exclusions: 7" in markdown
    assert "- n_matched_but_walk_infeasible: 3" in markdown
    assert "## Calendar-coverage caveat" in markdown
    assert "2027-01-10" in markdown
    assert "Models multi-day SOC carry-over" in markdown
    assert "does not model multi-day SOC carry-over" not in markdown
