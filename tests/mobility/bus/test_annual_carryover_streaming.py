"""Golden orchestration tests for the carryover streaming tail (plan v2 §8/§15):
lag-one finalize, per-date vehicle_soc_state checkpoints, resume equivalence,
per-vehicle wall-clock non-overlap, warmup flag propagation."""

from __future__ import annotations

import argparse
import importlib.util
import sys
from pathlib import Path

import pandas as pd
import pandas.testing as pdt
import pytest


REPO_ROOT = Path(__file__).resolve().parents[3]
RUNNER_PATH = REPO_ROOT / "scripts" / "run_bus_annual_depot_load.py"

DATES = ["2026-04-17", "2026-04-18", "2026-04-19", "2026-04-20"]
TABLES = [
    "depot_load_15min",
    "depot_daily_summary",
    "vehicle_day_events",
    "vehicle_day_soc_summary",
    "bus_trip_records",
    "bus_charging_events",
    "bus_ev_state_records",
    "vehicle_day_assignments",
    "vehicle_day_assignment_diagnostics",
    "unmatched_sampled_blocks",
    "vehicle_soc_state",
]
SORT_KEYS = {
    "depot_load_15min": ["depot_id", "service_date", "slot_start_datetime", "slot_index"],
    "depot_daily_summary": ["depot_id", "service_date", "slot_date"],
    "vehicle_day_events": ["service_date", "vehicle_day_id", "event_seq", "start_datetime"],
    "vehicle_day_soc_summary": ["service_date", "vehicle_day_id"],
    "bus_trip_records": ["vehicle_day_id", "trip_sequence_id"],
    "bus_charging_events": ["vehicle_day_id", "event_seq", "start_datetime"],
    "bus_ev_state_records": ["vehicle_day_id", "event_seq", "slot_start_datetime", "slot_end_datetime"],
    "vehicle_day_assignments": ["service_date", "vehicle_day_id"],
    "vehicle_day_assignment_diagnostics": ["service_date"],
    "unmatched_sampled_blocks": ["service_date", "block_instance_id"],
    "vehicle_soc_state": ["service_date", "vehicle_spec_id"],
}


def _load_runner_module():
    spec = importlib.util.spec_from_file_location("run_bus_annual_depot_load_carryover_tests", RUNNER_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules["run_bus_annual_depot_load_carryover_tests"] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _inputs(*, n_specs: int = 1, day3_start_h: float = 8.0, ev2_ac_kw: float = 0.5, ev1_ac_kw: float = 50.0, day2_distance_km: float = 20.0):
    """One 20 km block per day; spec ev1 strong, optional ev2 with weak AC charging.

    ``day3_start_h`` < 6 exercises the early-pull-out overnight truncation.
    ``day2_distance_km`` > the fleet range forces an idle day 2.
    """
    rows = []
    for date in DATES:
        start_h = day3_start_h if date == "2026-04-19" else 8.0
        start_dt = pd.Timestamp(date) + pd.to_timedelta(start_h, unit="h")
        rows.append(
            {
                "service_date": date,
                "block_instance_id": f"bi_{date}",
                "block_template_id": "bt1",
                "agency_id": "OP",
                "service_id": "S1",
                "block_id": "B1",
                "block_source": "native",
                "start_datetime": start_dt,
                "end_datetime": start_dt + pd.Timedelta(hours=2),
                "start_h": start_h,
                "end_h": start_h + 2.0,
                "duration_h": 2.0,
                "passenger_distance_km": day2_distance_km if date == "2026-04-18" else 20.0,
                "start_lat": 51.0,
                "start_lon": -1.0,
                "end_lat": 51.0,
                "end_lon": -1.0,
                "start_lsoa": "E1",
                "end_lsoa": "E1",
                "region_key": "London",
                "depot_id": "opdepot_OP_E1",
                "depot_lsoa": "E1",
            }
        )
    block_instances = pd.DataFrame(rows)
    block_templates = pd.DataFrame(
        {
            "block_template_id": ["bt1"],
            "start_lsoa": ["E1"],
            "end_lsoa": ["E1"],
            "trip_ids": [["trip_a"]],
            "trip_start_times": [[8.0]],
            "trip_end_times": [[10.0]],
            "trip_start_lats": [[51.0]],
            "trip_start_lons": [[-1.0]],
            "trip_end_lats": [[51.0]],
            "trip_end_lons": [[-1.0]],
            "trip_distances_km": [[20.0]],
            "trip_start_lsoas": [["E1"]],
            "trip_end_lsoas": [["E1"]],
        }
    )
    spec_rows = []
    for index in range(1, n_specs + 1):
        spec_rows.append(
            {
                "vehicle_spec_id": f"ev{index}",
                "battery_kwh": 100.0,
                "consumption_kwh_per_km": 1.0,
                "ac_charge_kw_max": ev1_ac_kw if index == 1 else ev2_ac_kw,
                "usable_soc_min": 0.10,
                "usable_soc_max": 0.95,
                "home_depot_id": "opdepot_OP_E1",
                "home_depot_lsoa": "E1",
                "home_depot_lat": 51.0,
                "home_depot_lon": -1.0,
                "home_depot_status": "assigned",
            }
        )
    ev_specs = pd.DataFrame(spec_rows)
    depot_registry = pd.DataFrame(
        {
            "depot_id": ["opdepot_OP_E1"],
            "depot_lat": [51.0],
            "depot_lon": [-1.0],
            "depot_lsoa": ["E1"],
            "depot_confidence": ["high"],
            "is_physical_depot": [False],
            "is_operational_anchor": [True],
        }
    )
    return block_instances, block_templates, ev_specs, depot_registry


def _make_args(**overrides) -> argparse.Namespace:
    base = {
        "depot_power_kw": 100.0,
        "default_overnight_end_hour": 6.0,
        "use_trip_level_events": False,
        "scenario_mode": "ev_stock_scale",
        "seed": 20260603,
        "sample_block_multiplier": 1.0,
        "resume": False,
        "warmup_days": 0,
        "idle_vehicle_charging_policy": "home_depot",
    }
    base.update(overrides)
    return argparse.Namespace(**base)


def _run_carryover(module, out_dir: Path, *, end_iso: str = DATES[-1], resume: bool = False, warmup_days: int = 0, n_specs: int = 1, day3_start_h: float = 8.0, ev1_ac_kw: float = 50.0, day2_distance_km: float = 20.0) -> dict[str, object]:
    block_instances, block_templates, ev_specs, depot_registry = _inputs(n_specs=n_specs, day3_start_h=day3_start_h, ev1_ac_kw=ev1_ac_kw, day2_distance_km=day2_distance_km)
    return module._run_carryover_streaming_tail(
        _make_args(resume=resume, warmup_days=warmup_days),
        out_dir=out_dir,
        start_iso=DATES[0],
        end_iso=end_iso,
        preflight={"n_trip_rows": 4, "minibus_row_count": 0, "n_ev_specs_dropped_by_sanity": 0},
        block_templates_lsoa=block_templates,
        block_instances=block_instances,
        depot_registry=depot_registry,
        ev_specs=ev_specs,
        home_depot_radius_km=10.0,
        block_sampling="uniform",
    )


def _read_table(out_dir: Path, name: str) -> pd.DataFrame:
    dataset = out_dir / name
    if dataset.exists():
        return pd.read_parquet(dataset)
    parquet = out_dir / f"{name}.parquet"
    if parquet.exists():
        return pd.read_parquet(parquet)
    return pd.DataFrame()


def _normalise(frame: pd.DataFrame) -> pd.DataFrame:
    out = frame.copy()
    for column in out.select_dtypes(include="category").columns:
        out[column] = out[column].astype(str)
    for column in ["service_date", "slot_date"]:
        if column in out.columns:
            out[column] = out[column].astype(str)
    return out


def _sort_table(name: str, frame: pd.DataFrame) -> pd.DataFrame:
    keys = [column for column in SORT_KEYS[name] if column in frame.columns]
    return frame.sort_values(keys, kind="stable").reset_index(drop=True) if keys else frame.reset_index(drop=True)


def _assert_matches_reference(ref_dir: Path, res_dir: Path) -> None:
    for name in TABLES:
        expected = _sort_table(name, _normalise(_read_table(ref_dir, name)))
        actual = _sort_table(name, _normalise(_read_table(res_dir, name)))
        actual = actual.loc[:, expected.columns]
        pdt.assert_frame_equal(actual, expected, check_dtype=False, rtol=1e-9, atol=1e-9)


def test_carryover_checkpoint_written_per_date(tmp_path) -> None:
    module = _load_runner_module()
    out_dir = tmp_path / "run"
    _run_carryover(module, out_dir, n_specs=2)
    for date in DATES:
        path = out_dir / "vehicle_soc_state" / f"service_date={date}" / "part.parquet"
        assert path.exists(), date
        frame = pd.read_parquet(path)
        assert len(frame) == 2
        assert set(frame["vehicle_spec_id"]) == {"ev1", "ev2"}


def test_carryover_overnight_stitches_to_next_day_first_event(tmp_path) -> None:
    # Single spec, deterministic chain. Day 3 pulls out at 04:00 (< 06:00 seam):
    # day-2 overnight truncates to 04:00 and day 3 has no pre window. Normal
    # days: overnight ends at the 06:00 seam and a pre window [06:00, 08:00]
    # opens the next day.
    module = _load_runner_module()
    out_dir = tmp_path / "run"
    _run_carryover(module, out_dir, day3_start_h=4.0)
    events = _normalise(_read_table(out_dir, "vehicle_day_events"))

    day1_overnight = events.loc[(events["service_date"] == DATES[0]) & events["event_type"].isin({"depot_parking_overnight", "depot_parking_post"})]
    assert len(day1_overnight) == 1
    assert pd.Timestamp(day1_overnight.iloc[0]["end_datetime"]) == pd.Timestamp("2026-04-18 06:00")

    day2_pre = events.loc[(events["service_date"] == DATES[1]) & (events["event_type"] == "depot_parking_pre")]
    assert len(day2_pre) == 1
    assert pd.Timestamp(day2_pre.iloc[0]["start_datetime"]) == pd.Timestamp("2026-04-18 06:00")

    day2_overnight = events.loc[(events["service_date"] == DATES[1]) & events["event_type"].isin({"depot_parking_overnight", "depot_parking_post"})]
    assert pd.Timestamp(day2_overnight.iloc[0]["end_datetime"]) == pd.Timestamp("2026-04-19 04:00")
    day3_pre = events.loc[(events["service_date"] == DATES[2]) & (events["event_type"] == "depot_parking_pre")]
    assert day3_pre.empty


def test_carryover_no_wallclock_overlap_per_vehicle(tmp_path) -> None:
    # §10.3 core invariant: a vehicle's events (duty + idle, across days) tile
    # the wall clock without overlap.
    module = _load_runner_module()
    out_dir = tmp_path / "run"
    _run_carryover(module, out_dir, n_specs=2, day3_start_h=4.0)
    events = _read_table(out_dir, "vehicle_day_events")
    for spec_id, group in events.groupby("vehicle_spec_id"):
        ordered = group.sort_values("start_datetime", kind="stable")
        previous_end = None
        for row in ordered.itertuples(index=False):
            start = pd.Timestamp(row.start_datetime)
            if previous_end is not None:
                assert start >= previous_end - pd.Timedelta(seconds=1), (
                    f"{spec_id}: event at {start} overlaps previous end {previous_end}"
                )
            previous_end = max(previous_end, pd.Timestamp(row.end_datetime)) if previous_end is not None else pd.Timestamp(row.end_datetime)


def test_carryover_idle_vehicle_state_ticks_and_emits_only_when_below_max(tmp_path) -> None:
    # Deterministic forced-idle scenario: a single 0.5 kW-AC spec drives day 1
    # (overnight refills only 10 of the 20 kWh used), cannot serve the 200 km
    # day-2 block, so day 2 is an idle top-up at the home depot. Day-3/4 idle
    # state would be at usable_soc_max -> no further idle events.
    module = _load_runner_module()
    out_dir = tmp_path / "run"
    _run_carryover(module, out_dir, ev1_ac_kw=0.5, day2_distance_km=200.0)
    state = _read_table(out_dir, "vehicle_soc_state")
    assert set(state.groupby("service_date")["vehicle_spec_id"].count()) == {1}
    events = _read_table(out_dir, "vehicle_day_events")
    # Day-1 overnight at 0.5 kW over 20 h adds exactly 10 kWh (75 -> 85).
    day1_overnight = events.loc[(events["service_date"].astype(str) == DATES[0]) & events["event_type"].isin({"depot_parking_overnight", "depot_parking_post"})]
    assert float(day1_overnight.iloc[0]["charge_kwh_added"]) == pytest.approx(10.0)
    # Exactly one idle event on day 2 topping up the remaining 10 kWh.
    idle = events.loc[events["event_type"] == "idle_home_depot_charging"]
    assert len(idle) == 1
    assert str(idle.iloc[0]["service_date"]) == DATES[1]
    assert float(idle.iloc[0]["charge_kwh_added"]) == pytest.approx(10.0)
    assert idle.iloc[0]["vehicle_spec_id"] == "ev1"
    # Day 2's block stays unmatched on the carried-SOC screen.
    unmatched = _read_table(out_dir, "unmatched_sampled_blocks")
    day2 = unmatched.loc[unmatched["service_date"].astype(str) == DATES[1]]
    assert day2.iloc[0]["unmatched_reason"] == "no_feasible_vehicle_in_radius"
    # Day-3 walk starts from the fully recovered 95 kWh.
    soc_summary = _read_table(out_dir, "vehicle_day_soc_summary")
    day3 = soc_summary.loc[soc_summary["service_date"].astype(str) == DATES[2]]
    assert float(day3.iloc[0]["start_soc_kwh"]) == pytest.approx(95.0)


def test_carryover_full_run_vs_truncate_plus_resume_identical(tmp_path) -> None:
    # THE golden test: 4 days in one go == 2 days, then resume to 4 days.
    module = _load_runner_module()
    ref_dir = tmp_path / "ref"
    res_dir = tmp_path / "res"
    ref_summary = _run_carryover(module, ref_dir, n_specs=2, day3_start_h=4.0)
    _run_carryover(module, res_dir, end_iso=DATES[1], n_specs=2, day3_start_h=4.0)
    resumed_summary = _run_carryover(module, res_dir, resume=True, n_specs=2, day3_start_h=4.0)
    assert resumed_summary["n_vehicle_day_events"] == ref_summary["n_vehicle_day_events"]
    _assert_matches_reference(ref_dir, res_dir)


def test_carryover_resume_redoes_truncated_state_partition(tmp_path) -> None:
    module = _load_runner_module()
    ref_dir = tmp_path / "ref"
    res_dir = tmp_path / "res"
    _run_carryover(module, ref_dir, n_specs=2)
    _run_carryover(module, res_dir, end_iso=DATES[2], n_specs=2)
    victim = res_dir / "vehicle_soc_state" / f"service_date={DATES[1]}" / "part.parquet"
    victim.write_bytes(victim.read_bytes()[:10])
    _run_carryover(module, res_dir, resume=True, n_specs=2)
    _assert_matches_reference(ref_dir, res_dir)


def test_carryover_resume_rejects_daily_reset_tree(tmp_path) -> None:
    module = _load_runner_module()
    out_dir = tmp_path / "run"
    (out_dir / "vehicle_day_events").mkdir(parents=True)
    try:
        _run_carryover(module, out_dir, resume=True)
    except RuntimeError as exc:
        assert "daily_reset" in str(exc)
    else:
        raise AssertionError("expected RuntimeError for a daily_reset run tree")


def test_carryover_warmup_flag_propagates(tmp_path) -> None:
    module = _load_runner_module()
    out_dir = tmp_path / "run"
    result = _run_carryover(module, out_dir, warmup_days=2)
    for name in ("vehicle_day_events", "vehicle_day_soc_summary", "vehicle_day_assignments", "depot_load_15min", "depot_daily_summary"):
        frame = _normalise(_read_table(out_dir, name))
        assert "is_warmup" in frame.columns, name
        if not frame.empty:
            by_date = frame.groupby("service_date")["is_warmup"].first()
            for date in by_date.index:
                assert bool(by_date[date]) == (date in set(DATES[:2])), (name, date)
    assert result["n_vehicle_day_events"] > 0
    summary_text = (out_dir / "run_summary.md").read_text(encoding="utf-8")
    assert "- soc_mode: carryover" in summary_text
    assert "- warmup_days: 2" in summary_text
    assert "## Carry-over configuration" in summary_text
    assert "Models multi-day SOC carry-over" in summary_text


def test_carryover_energy_identity_including_idle(tmp_path) -> None:
    module = _load_runner_module()
    out_dir = tmp_path / "run"
    result = _run_carryover(module, out_dir, n_specs=2)
    event_charge = float(result["n_vehicle_day_events"])  # presence sanity
    assert event_charge > 0
    events = _read_table(out_dir, "vehicle_day_events")
    load = _read_table(out_dir, "depot_load_15min")
    from mobility.bus.annual_depot_load import depot_load_energy_matches_events

    assert depot_load_energy_matches_events(load, events)


def test_carryover_determinism_same_seed(tmp_path) -> None:
    module = _load_runner_module()
    dir_a = tmp_path / "a"
    dir_b = tmp_path / "b"
    _run_carryover(module, dir_a, n_specs=2, day3_start_h=4.0)
    _run_carryover(module, dir_b, n_specs=2, day3_start_h=4.0)
    _assert_matches_reference(dir_a, dir_b)
