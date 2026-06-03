from __future__ import annotations

import argparse
import importlib.util
import sys
from pathlib import Path

import pandas as pd
import pandas.testing as pdt


REPO_ROOT = Path(__file__).resolve().parents[3]
RUNNER_PATH = REPO_ROOT / "scripts" / "run_bus_annual_depot_load.py"


def _load_runner_module():
    spec = importlib.util.spec_from_file_location("run_bus_annual_depot_load", RUNNER_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules["run_bus_annual_depot_load"] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _make_args(**overrides) -> argparse.Namespace:
    base = {
        "depot_power_kw": 100.0,
        "default_overnight_end_hour": 6.0,
        "use_trip_level_events": True,
        "scenario_mode": "ev_stock_scale",
        "date_chunk_size": 1,
    }
    base.update(overrides)
    return argparse.Namespace(**base)


def _inputs():
    dates = ["2026-04-17", "2026-04-18"]
    assignments = pd.DataFrame(
        {
            "service_date": dates,
            "vehicle_day_id": ["vd_2026-04-17_00000_ev1", "vd_2026-04-18_00000_ev1"],
            "vehicle_spec_id": ["ev1", "ev1"],
            "block_instance_id": ["bi_2026-04-17", "bi_2026-04-18"],
            "block_template_id": ["bt1", "bt1"],
            "agency_id": ["OP", "OP"],
            "service_id": ["S1", "S1"],
            "block_id": ["B1", "B1"],
            "depot_id": ["opdepot_OP_E1", "opdepot_OP_E1"],
            "region_key": ["London", "London"],
            "scenario_mode": ["ev_stock_scale", "ev_stock_scale"],
            "n_available_block_instances_for_service_date": [1, 1],
            "n_assigned_block_instances_for_service_date": [1, 1],
            "n_unassigned_block_instances_for_service_date": [0, 0],
            "daily_assignment_coverage_share": [1.0, 1.0],
        }
    )
    block_instances = pd.DataFrame(
        {
            "service_date": dates,
            "block_instance_id": ["bi_2026-04-17", "bi_2026-04-18"],
            "block_template_id": ["bt1", "bt1"],
            "agency_id": ["OP", "OP"],
            "service_id": ["S1", "S1"],
            "block_id": ["B1", "B1"],
            "block_source": ["native", "native"],
            "start_datetime": [pd.Timestamp("2026-04-17 08:00"), pd.Timestamp("2026-04-18 08:00")],
            "end_datetime": [pd.Timestamp("2026-04-17 10:00"), pd.Timestamp("2026-04-18 10:00")],
            "start_h": [8.0, 8.0],
            "end_h": [10.0, 10.0],
            "duration_h": [2.0, 2.0],
            "passenger_distance_km": [20.0, 20.0],
            "start_lat": [51.0, 51.0],
            "start_lon": [-1.0, -1.0],
            "end_lat": [51.0, 51.0],
            "end_lon": [-1.0, -1.0],
            "start_lsoa": ["E1", "E1"],
            "end_lsoa": ["E1", "E1"],
            "region_key": ["London", "London"],
            "depot_id": ["opdepot_OP_E1", "opdepot_OP_E1"],
            "depot_lsoa": ["E1", "E1"],
        }
    )
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
    ev_specs = pd.DataFrame(
        {
            "vehicle_spec_id": ["ev1"],
            "battery_kwh": [100.0],
            "consumption_kwh_per_km": [1.0],
            "ac_charge_kw_max": [50.0],
            "usable_soc_min": [0.10],
            "usable_soc_max": [0.95],
        }
    )
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
    return assignments, block_instances, block_templates, ev_specs, depot_registry


def _run_tail(module, out_dir: Path, stream: bool, date_chunk_size: int = 1) -> dict[str, object]:
    assignments, block_instances, block_templates, ev_specs, depot_registry = _inputs()
    args = _make_args(date_chunk_size=date_chunk_size)
    kwargs = {
        "args": args,
        "out_dir": out_dir,
        "start_iso": "2026-04-17",
        "end_iso": "2026-04-18",
        "preflight": {"n_trip_rows": 2, "minibus_row_count": 0, "n_ev_specs_dropped_by_sanity": 0},
        "block_templates_lsoa": block_templates,
        "block_instances": block_instances,
        "depot_registry": depot_registry,
        "ev_specs": ev_specs,
        "assignments": assignments,
    }
    if stream:
        return module._run_streaming_pipeline_tail(**kwargs)
    return module._run_batch_pipeline_tail(**kwargs)


TABLES = [
    "depot_load_15min",
    "depot_daily_summary",
    "vehicle_day_events",
    "vehicle_day_soc_summary",
    "bus_trip_records",
    "bus_charging_events",
    "bus_ev_state_records",
]

SORT_KEYS = {
    "depot_load_15min": ["depot_id", "service_date", "slot_start_datetime", "slot_index"],
    "depot_daily_summary": ["depot_id", "service_date", "slot_date"],
    "vehicle_day_events": ["vehicle_day_id", "event_seq"],
    "vehicle_day_soc_summary": ["service_date", "vehicle_day_id"],
    "bus_trip_records": ["vehicle_day_id", "trip_sequence_id"],
    "bus_charging_events": ["vehicle_day_id", "event_seq"],
    "bus_ev_state_records": ["vehicle_day_id", "event_seq", "slot_start_datetime", "slot_end_datetime"],
}


def _read_table(out_dir: Path, name: str) -> pd.DataFrame:
    dataset_path = out_dir / name
    parquet_path = out_dir / f"{name}.parquet"
    if dataset_path.exists():
        return pd.read_parquet(dataset_path)
    return pd.read_parquet(parquet_path)


def _normalise(frame: pd.DataFrame) -> pd.DataFrame:
    out = frame.copy()
    for column in out.select_dtypes(include="category").columns:
        out[column] = out[column].astype(str)
    for column in ["service_date", "slot_date", "date"]:
        if column in out.columns:
            out[column] = out[column].astype(str)
    return out


def _sort_table(name: str, frame: pd.DataFrame) -> pd.DataFrame:
    keys = [column for column in SORT_KEYS[name] if column in frame.columns]
    if keys:
        return frame.sort_values(keys, kind="stable").reset_index(drop=True)
    return frame.reset_index(drop=True)


def test_stream_tail_matches_batch_outputs(tmp_path) -> None:
    module = _load_runner_module()
    batch_dir = tmp_path / "batch"
    stream_dir = tmp_path / "stream"
    batch_summary = _run_tail(module, batch_dir, stream=False)
    stream_summary = _run_tail(module, stream_dir, stream=True, date_chunk_size=1)

    assert stream_summary["n_bus_ev_state_records"] == batch_summary["n_bus_ev_state_records"]
    assert stream_summary["n_vehicle_day_events"] == batch_summary["n_vehicle_day_events"]

    for name in TABLES:
        expected = _sort_table(name, _normalise(_read_table(batch_dir, name)))
        actual = _sort_table(name, _normalise(_read_table(stream_dir, name)))
        actual = actual.loc[:, expected.columns]
        pdt.assert_frame_equal(actual, expected, check_dtype=False, rtol=1e-9, atol=1e-9)


def test_stream_tail_writes_hive_partitioned_large_tables(tmp_path) -> None:
    module = _load_runner_module()
    stream_dir = tmp_path / "stream"
    _run_tail(module, stream_dir, stream=True, date_chunk_size=2)

    assert (stream_dir / "bus_ev_state_records" / "service_date=2026-04-17" / "part.parquet").exists()
    assert (stream_dir / "vehicle_day_events" / "service_date=2026-04-18" / "part.parquet").exists()
    assert (stream_dir / "depot_load_15min" / "service_date=2026-04-17" / "part.parquet").exists()
    assert (stream_dir / "depot_daily_summary" / "service_date=2026-04-18" / "part.parquet").exists()
    assert not (stream_dir / "bus_ev_state_records.parquet").exists()


def _run_stream_tail(module, out_dir: Path, *, dates=None, resume=False, round_trip_templates=False, tmp_dir=None) -> dict[str, object]:
    assignments, block_instances, block_templates, ev_specs, depot_registry = _inputs()
    if dates is not None:
        assignments = assignments.loc[assignments["service_date"].isin(dates)].reset_index(drop=True)
    if round_trip_templates:
        path = tmp_dir / "templates_roundtrip.parquet"
        block_templates.to_parquet(path, index=False)
        block_templates = pd.read_parquet(path)
    args = _make_args(resume=resume)
    return module._run_streaming_pipeline_tail(
        args=args,
        out_dir=out_dir,
        start_iso="2026-04-17",
        end_iso="2026-04-18",
        preflight={"n_trip_rows": 2, "minibus_row_count": 0, "n_ev_specs_dropped_by_sanity": 0},
        block_templates_lsoa=block_templates,
        block_instances=block_instances,
        depot_registry=depot_registry,
        ev_specs=ev_specs,
        assignments=assignments,
    )


def _assert_matches_reference(ref_dir: Path, resumed_dir: Path) -> None:
    for name in TABLES:
        expected = _sort_table(name, _normalise(_read_table(ref_dir, name)))
        actual = _sort_table(name, _normalise(_read_table(resumed_dir, name)))
        actual = actual.loc[:, expected.columns]
        pdt.assert_frame_equal(actual, expected, check_dtype=False, rtol=1e-9, atol=1e-9)


def test_stream_resume_completes_interrupted_run(tmp_path) -> None:
    module = _load_runner_module()
    ref_dir = tmp_path / "ref"
    res_dir = tmp_path / "resume"
    ref_summary = _run_stream_tail(module, ref_dir)

    # Simulate a run killed after day 1: only day-1 partitions and stale combined outputs exist.
    _run_stream_tail(module, res_dir, dates=["2026-04-17"])
    assert not (res_dir / "vehicle_day_events" / "service_date=2026-04-18").exists()

    # Resume with templates round-tripped through parquet, as run_pipeline --resume loads them.
    resumed_summary = _run_stream_tail(module, res_dir, resume=True, round_trip_templates=True, tmp_dir=tmp_path)

    assert resumed_summary["n_vehicle_day_events"] == ref_summary["n_vehicle_day_events"]
    assert resumed_summary["n_bus_ev_state_records"] == ref_summary["n_bus_ev_state_records"]
    _assert_matches_reference(ref_dir, res_dir)


def test_stream_resume_redoes_unreadable_partition(tmp_path) -> None:
    module = _load_runner_module()
    ref_dir = tmp_path / "ref"
    res_dir = tmp_path / "resume"
    _run_stream_tail(module, ref_dir)
    _run_stream_tail(module, res_dir, dates=["2026-04-17"])

    # Truncate one day-1 partition, as a kill mid-write would.
    victim = res_dir / "bus_ev_state_records" / "service_date=2026-04-17" / "part.parquet"
    victim.write_bytes(victim.read_bytes()[:10])

    _run_stream_tail(module, res_dir, resume=True)
    _assert_matches_reference(ref_dir, res_dir)


def test_stream_resume_rebuilds_missing_depot_partitions(tmp_path) -> None:
    # Models runs from before depot partitions existed: event datasets complete, depot datasets absent.
    import shutil

    module = _load_runner_module()
    ref_dir = tmp_path / "ref"
    res_dir = tmp_path / "resume"
    _run_stream_tail(module, ref_dir)
    _run_stream_tail(module, res_dir, dates=["2026-04-17"])
    shutil.rmtree(res_dir / "depot_load_15min")
    shutil.rmtree(res_dir / "depot_daily_summary")

    _run_stream_tail(module, res_dir, resume=True)
    assert (res_dir / "depot_load_15min" / "service_date=2026-04-17" / "part.parquet").exists()
    _assert_matches_reference(ref_dir, res_dir)
