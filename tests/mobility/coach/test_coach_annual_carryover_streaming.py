"""End-to-end coach depot-load carryover tests on a synthetic TxC-like fixture.

Builds the coach stage-0 frames through the REAL chain builder + adapter, then
runs the bus carryover streaming tail (imported, unmodified) with the coach
flags (inter-trip relocation ON). Mirrors the bus golden suite invariants:
resume byte-identity, per-vehicle wall-clock tiling, energy identity, per-date
state checkpoints, determinism — plus the coach-specific relocation accounting.
"""

from __future__ import annotations

import argparse
import datetime as dt
import importlib.util
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pandas.testing as pdt
import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
RUNNER_PATH = REPO_ROOT / "scripts" / "run_bus_annual_depot_load.py"

from mobility.bus.annual_depot_events import haversine_km  # noqa: E402
from mobility.coach.chain_builder import build_coach_chains  # noqa: E402
from mobility.coach.coach_block_templates import build_coach_block_templates, expand_coach_block_instances  # noqa: E402
from mobility.coach.coach_ev_specs import build_ev_coach_specs  # noqa: E402

DATES = ["2026-04-17", "2026-04-18", "2026-04-19", "2026-04-20"]
P1_END = (51.20, -1.20)
P2_START = (51.40, -1.40)
EXPECTED_RELOCATION = haversine_km(P1_END[0], P1_END[1], P2_START[0], P2_START[1])

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
    spec = importlib.util.spec_from_file_location("run_bus_annual_depot_load_coach_tests", RUNNER_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules["run_bus_annual_depot_load_coach_tests"] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _journeys() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "journey_id": ["jA", "jB"],
            "vehicle_journey_code": ["VJ_A", "VJ_B"],
            "operator_code": ["NX", "NX"],
            "start_h": [8.0, 11.0],
            "end_h": [10.0, 14.0],
            "distance_km": [60.0, 70.0],
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


def _inventory() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {"EV_ID": "coach_strong", "LSOA_code": "E1", "Model": "YUTONG TC12", "count": 1, "vehicle_subtype": "coach",
             "Energy_kWh": 281.0, "DC_Power_kW": 150.0, "AC_Power_kW": 22.0, "efficiency_wh_per_km": 1000.0},
            {"EV_ID": "coach_weak", "LSOA_code": "E1", "Model": "YUTONG TC12", "count": 1, "vehicle_subtype": "coach",
             "Energy_kWh": 281.0, "DC_Power_kW": 1.0, "AC_Power_kW": 22.0, "efficiency_wh_per_km": 1000.0},
        ]
    )


def _build_stage0():
    journeys = _journeys()
    date_index = pd.DataFrame(
        [{"journey_id": journey, "date": dt.date.fromisoformat(date)} for date in DATES for journey in ("jA", "jB")]
    )
    chains_long = build_coach_chains(journeys, date_index)
    assert chains_long["coach_chain_id"].nunique() == len(DATES)  # one chain per date
    templates, _ = build_coach_block_templates(journeys, chains_long)
    templates = templates.assign(region_key="London")
    instances, _ = expand_coach_block_instances(templates, chains_long, start_date=DATES[0], end_date=DATES[-1])
    instances = instances.assign(depot_id="opdepot_NX_E1", depot_lsoa="E1")
    specs, _ = build_ev_coach_specs(_inventory())
    specs = specs.assign(
        home_depot_id="opdepot_NX_E1",
        home_depot_lsoa="E1",
        home_depot_lat=51.0,
        home_depot_lon=-1.0,
        home_depot_status="assigned",
    )
    registry = pd.DataFrame(
        {
            "depot_id": ["opdepot_NX_E1"],
            "depot_lat": [51.0],
            "depot_lon": [-1.0],
            "depot_lsoa": ["E1"],
            "depot_confidence": ["high"],
            "is_physical_depot": [False],
            "is_operational_anchor": [True],
        }
    )
    return templates, instances, registry, specs


def _make_args(**overrides) -> argparse.Namespace:
    base = {
        "depot_power_kw": 100.0,
        "default_overnight_end_hour": 6.0,
        "use_trip_level_events": True,
        "scenario_mode": "ev_stock_scale",
        "seed": 20260603,
        "sample_block_multiplier": 1.0,
        "resume": False,
        "warmup_days": 0,
        "idle_vehicle_charging_policy": "home_depot",
        "inter_trip_relocation": True,
        "relocation_speed_kmh": 50.0,
    }
    base.update(overrides)
    return argparse.Namespace(**base)


def _run_carryover(module, out_dir: Path, *, end_iso: str = DATES[-1], resume: bool = False, warmup_days: int = 0) -> dict[str, object]:
    templates, instances, registry, specs = _build_stage0()
    return module._run_carryover_streaming_tail(
        _make_args(resume=resume, warmup_days=warmup_days),
        out_dir=out_dir,
        start_iso=DATES[0],
        end_iso=end_iso,
        preflight={"n_trip_rows": 8, "minibus_row_count": 0, "n_ev_specs_dropped_by_sanity": 0},
        block_templates_lsoa=templates,
        block_instances=instances,
        depot_registry=registry,
        ev_specs=specs,
        home_depot_radius_km=25.0,
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


def test_coach_relocation_energy_in_ledger_and_screen_agree(tmp_path) -> None:
    module = _load_runner_module()
    out_dir = tmp_path / "run"
    _run_carryover(module, out_dir)
    events = _read_table(out_dir, "vehicle_day_events")
    relocations = events.loc[events["event_type"] == "inter_trip_relocation"]
    # One matched chain per day, each with exactly one qualifying relocation gap.
    assert len(relocations) == len(DATES)
    assert np.allclose(relocations["distance_km"], EXPECTED_RELOCATION)
    # Screen/walk agreement: walked movement energy == passenger_distance_km
    # (journeys + relocation) x consumption for every matched vehicle-day.
    summary = _read_table(out_dir, "vehicle_day_soc_summary")
    expected_energy = (60.0 + 70.0 + EXPECTED_RELOCATION) * 1.0
    assert np.allclose(summary["total_energy_kwh"], expected_energy)
    assignments = _read_table(out_dir, "vehicle_day_assignments")
    assert np.allclose(assignments["required_kwh_est"], expected_energy)  # deadhead 0 (home depot at chain ends)
    from mobility.bus.annual_depot_load import depot_load_energy_matches_events

    assert depot_load_energy_matches_events(_read_table(out_dir, "depot_load_15min"), events)


def test_coach_carryover_checkpoints_and_no_overlap(tmp_path) -> None:
    module = _load_runner_module()
    out_dir = tmp_path / "run"
    _run_carryover(module, out_dir)
    for date in DATES:
        path = out_dir / "vehicle_soc_state" / f"service_date={date}" / "part.parquet"
        assert path.exists(), date
        frame = pd.read_parquet(path)
        assert set(frame["vehicle_spec_id"]) == {"evcoach_coach_strong", "evcoach_coach_weak"}
    events = _read_table(out_dir, "vehicle_day_events")
    for spec_id, group in events.groupby("vehicle_spec_id"):
        ordered = group.sort_values("start_datetime", kind="stable")
        previous_end = None
        for row in ordered.itertuples(index=False):
            start = pd.Timestamp(row.start_datetime)
            if previous_end is not None:
                assert start >= previous_end - pd.Timedelta(seconds=1), (spec_id, start, previous_end)
            previous_end = max(previous_end, pd.Timestamp(row.end_datetime)) if previous_end is not None else pd.Timestamp(row.end_datetime)


def test_coach_carryover_full_vs_truncate_plus_resume_identical(tmp_path) -> None:
    module = _load_runner_module()
    ref_dir = tmp_path / "ref"
    res_dir = tmp_path / "res"
    ref_summary = _run_carryover(module, ref_dir)
    _run_carryover(module, res_dir, end_iso=DATES[1])
    resumed_summary = _run_carryover(module, res_dir, resume=True)
    assert resumed_summary["n_vehicle_day_events"] == ref_summary["n_vehicle_day_events"]
    _assert_matches_reference(ref_dir, res_dir)


def test_coach_carryover_determinism_same_seed(tmp_path) -> None:
    module = _load_runner_module()
    dir_a = tmp_path / "a"
    dir_b = tmp_path / "b"
    _run_carryover(module, dir_a)
    _run_carryover(module, dir_b)
    _assert_matches_reference(dir_a, dir_b)


def test_coach_carryover_warmup_flag_and_summary_caveats(tmp_path) -> None:
    module = _load_runner_module()
    out_dir = tmp_path / "run"
    _run_carryover(module, out_dir, warmup_days=2)
    summary = _read_table(out_dir, "vehicle_day_soc_summary")
    by_date = _normalise(summary).groupby("service_date")["is_warmup"].first()
    for date in DATES:
        assert bool(by_date[date]) == (date in set(DATES[:2]))
    summary_text = (out_dir / "run_summary.md").read_text(encoding="utf-8")
    assert "- soc_mode: carryover" in summary_text
    assert "- warmup_days: 2" in summary_text
