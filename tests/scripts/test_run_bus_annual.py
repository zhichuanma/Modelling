"""Tests for scripts/run_bus_annual.py runner helpers + dry-run smoke."""

from __future__ import annotations

import argparse
import datetime as dt
import importlib.util
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
RUNNER_PATH = REPO_ROOT / "scripts" / "run_bus_annual.py"


def _load_runner_module():
    spec = importlib.util.spec_from_file_location("run_bus_annual", RUNNER_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules["run_bus_annual"] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def runner_module():
    return _load_runner_module()


def _make_args(**overrides) -> argparse.Namespace:
    base = dict(
        blocks=Path("outputs/all_blocks.parquet"),
        warm_up_days=14,
        limit_blocks=0,
        seed=42,
        run_scope="full_fleet",
        per_block_out=Path("outputs/bus_annual_per_block.parquet"),
        load_profile_out=Path("outputs/bus_annual_load_profile.parquet"),
        progress_interval=1000,
        allow_layover_charging=False,
        layover_charge_kw=0.0,
        min_layover_for_charging_h=0.0,
        soc_init=1.0,
        start_date="2026-04-17",
        end_date="2027-04-16",
        vehicle_params=None,
    )
    base.update(overrides)
    return argparse.Namespace(**base)


def test_attach_run_metadata_columns(runner_module) -> None:
    df = pd.DataFrame({"block_id": ["B1", "B2"], "annual_distance_km": [10.0, 20.0]})
    args = _make_args(run_scope="dryrun_1000", limit_blocks=1000, seed=99)
    out = runner_module._attach_run_metadata(df, args=args)

    assert (out["run_scope"] == "dryrun_1000").all()
    assert (out["run_seed"] == 99).all()
    assert (out["warm_up_days"] == 14).all()
    assert (out["selection"] == "first_1000").all()
    assert (out["feed_year_start"] == "2026-04-17").all()
    assert (out["feed_year_end"] == "2027-04-16").all()
    assert "blocks_path" in out.columns


def test_attach_run_metadata_handles_empty_frame(runner_module) -> None:
    df = pd.DataFrame()
    out = runner_module._attach_run_metadata(df, args=_make_args())
    assert out.empty


def test_summary_with_audit_columns(runner_module) -> None:
    per_block = pd.DataFrame(
        {
            "deadhead_short_count": [1, 0, 2],
            "deadhead_long_count": [0, 0, 1],
            "deadhead_total_km": [2.0, 0.0, 7.5],
            "deadhead_skipped_time_count": [0, 1, 0],
            "infeasible": [False, True, False],
            "infeasibility_reason": [None, "midday_depletion", None],
            "n_overlap_warnings": [0, 2, 0],
            "block_source": ["native", "native", "inferred"],
            "simulation_error": ["", "failed", ""],
        }
    )
    load_kw = np.zeros((2, 96))
    summary = runner_module._summary(per_block, load_kw, elapsed_s=1.5)

    assert summary["blocks"] == 3
    assert summary["deadhead_short_count"] == 3
    assert summary["deadhead_long_count"] == 1
    assert summary["deadhead_total_km"] == pytest.approx(9.5)
    assert summary["deadhead_skipped_time_count"] == 1
    assert summary["infeasible_share"] == pytest.approx(1.0 / 3.0)
    assert summary["simulation_error_count"] == 1
    assert summary["infeasible_count"] == 1
    assert summary["infeasibility_reason_breakdown"][None] == 2
    assert summary["infeasibility_reason_breakdown"]["midday_depletion"] == 1
    assert summary["blocks_with_overlap_warnings"] == 1
    assert summary["total_overlap_warnings"] == 2
    assert summary["block_source_breakdown"] == {"native": 2, "inferred": 1}
    assert summary["infeasible_share_native"] == pytest.approx(0.5)
    assert summary["infeasible_share_inferred"] == pytest.approx(0.0)
    assert summary["load_profile_rows"] == 192
    assert summary["runtime_s"] == pytest.approx(1.5)


def test_run_annual_dryrun_smoke(monkeypatch, tmp_path, runner_module) -> None:
    """End-to-end dry-run smoke that monkeypatches data-loading boundaries.

    Verifies that the runner wires simulate_fleet_year + write_annual_results
    correctly, the resulting per-block parquet contains the required audit
    columns, and that deadhead injection actually fires through this path.
    """
    blocks = pd.DataFrame(
        [
            ("B1_t0", "OP", "R1", "S1", 0, "B1", "native", 8.0, 9.0, 10.0, "A", "B", 51.0, -1.0, 51.0, -1.0, "shape"),
            ("B1_t1", "OP", "R1", "S1", 0, "B1", "native", 10.0, 11.0, 10.0, "C", "D", 51.018, -1.0, 51.05, -1.0, "shape"),
            ("B2_t0", "OP", "R1", "S2", 0, "B2", "native", 8.0, 9.0, 10.0, "A", "B", 51.0, -1.0, 51.05, -1.0, "shape"),
            ("B2_t1", "OP", "R1", "S2", 0, "B2", "native", 10.0, 11.0, 10.0, "B", "D", 51.05, -1.0, 51.1, -1.0, "shape"),
        ],
        columns=[
            "trip_id",
            "agency_id",
            "route_id",
            "service_id",
            "direction_id",
            "block_id",
            "block_source",
            "start_h",
            "end_h",
            "distance_km",
            "start_stop",
            "end_stop",
            "start_lat",
            "start_lon",
            "end_lat",
            "end_lon",
            "shape_id",
        ],
    )
    blocks_path = tmp_path / "blocks.parquet"
    blocks.to_parquet(blocks_path, index=False)

    fake_service_calendar = object()
    fake_service_dates = {
        "S1": (dt.date(2026, 4, 17),),
        "S2": (dt.date(2026, 4, 17),),
    }
    fake_vehicle_params = pd.DataFrame(
        {
            "make": ["A"],
            "gen_model": ["Alpha"],
            "stock_2025_q2": [100.0],
            "battery_kwh": [200.0],
            "consumption_kwh_per_km": [1.5],
            "depot_charge_kw": [80.0],
        }
    )

    monkeypatch.setattr(runner_module, "attach_lsoa", lambda df, **_: df, raising=True)
    monkeypatch.setattr(runner_module, "load_service_calendar", lambda *a, **k: fake_service_calendar, raising=True)
    monkeypatch.setattr(
        runner_module,
        "build_service_date_index",
        lambda service_ids, start, end, calendar: fake_service_dates,
        raising=True,
    )
    monkeypatch.setattr(runner_module, "load_bus_vehicle_params", lambda *a, **k: fake_vehicle_params, raising=True)

    per_out = tmp_path / "per_block.parquet"
    load_out = tmp_path / "load_profile.parquet"
    args = _make_args(
        blocks=blocks_path,
        warm_up_days=0,
        limit_blocks=0,
        per_block_out=per_out,
        load_profile_out=load_out,
        start_date="2026-04-17",
        end_date="2026-04-17",
        progress_interval=0,
    )

    summary = runner_module.run_annual(args)

    assert per_out.exists()
    assert load_out.exists()
    assert summary["blocks"] == 2
    assert summary["deadhead_short_count"] >= 1
    assert "simulation_error_count" in summary
    assert "total_overlap_warnings" in summary
    assert "block_source_breakdown" in summary

    per_block = pd.read_parquet(per_out)
    expected_audit = {
        "n_overlap_warnings",
        "deadhead_short_count",
        "deadhead_long_count",
        "deadhead_total_km",
        "deadhead_total_kwh",
        "deadhead_skipped_time_count",
        "deadhead_skipped_time_km",
        "infeasible",
        "first_floor_hit_h",
        "first_floor_trip_id",
        "shortfall_kwh",
        "infeasibility_reason",
        "simulation_error",
        "run_scope",
        "run_seed",
        "warm_up_days",
        "selection",
    }
    missing = expected_audit - set(per_block.columns)
    assert not missing, f"missing columns in dry-run output: {missing}"
