# Run the annual depot-only bus charging load pipeline.
#
# This runner is deliberately separate from scripts/run_bus_annual.py. It builds
# event-ledger outputs and depot-level 15-minute load curves without public
# charging, OCM matching, opportunity charging, or M1 L0-L4 resolution.

from __future__ import annotations

import argparse
import datetime as dt
import gc
import os
import shutil
import sys
from pathlib import Path
from typing import Any

import pandas as pd
import pyarrow.parquet as pq

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from mobility.bus.annual_block_instances import coerce_date, expand_block_instances  # noqa: E402
from mobility.bus.annual_block_templates import build_block_templates  # noqa: E402
from mobility.bus.annual_depot_artifacts import (  # noqa: E402
    BUS_CHARGING_EVENT_COLUMNS,
    BUS_EV_STATE_RECORD_COLUMNS,
    BUS_TRIP_RECORD_COLUMNS,
    build_bus_charging_event_records,
    build_bus_ev_state_records,
    build_bus_trip_records,
)
from mobility.bus.annual_depot_events import DEPOT_CHARGING_EVENT_TYPES, build_vehicle_day_events  # noqa: E402
from mobility.bus.annual_depot_load import aggregate_depot_load_15min, depot_load_energy_matches_events  # noqa: E402
from mobility.bus.annual_depot_outputs import build_run_summary_markdown, write_run_summary  # noqa: E402
from mobility.bus.annual_depot_preflight import run_preflight, write_preflight_summary  # noqa: E402
from mobility.bus.annual_depot_registry import attach_depots_to_instances, build_operational_depot_registry  # noqa: E402
from mobility.bus.annual_depot_soc import apply_depot_only_soc  # noqa: E402
from mobility.bus.annual_ev_specs import build_ev_bus_specs  # noqa: E402
from mobility.bus.annual_home_depot import assign_home_depots, build_depot_supply_demand  # noqa: E402
from mobility.bus.annual_lsoa_region import attach_lsoa_and_region  # noqa: E402
from mobility.bus.calendar import FEED_YEAR_END, FEED_YEAR_START, load_service_calendar  # noqa: E402
from mobility.core.spatial import DEFAULT_ONSPD_PATH, load_extended_lsoa_centroids, load_lsoa_centroids  # noqa: E402


DEFAULT_BLOCKS = REPO_ROOT / "outputs" / "all_blocks.parquet"
DEFAULT_EV_INVENTORY = REPO_ROOT / "data" / "EV_UK_LSOA_2025_with_energy.csv"
DEFAULT_OUT_DIR = Path("~/Work/Nature_EV_2025/outputs/bus_annual_depot_load")
STREAM_DATASET_NAMES = [
    "vehicle_day_events",
    "vehicle_day_soc_summary",
    "bus_trip_records",
    "bus_charging_events",
    "bus_ev_state_records",
]
STREAM_LOAD_DATASET_NAMES = [
    "depot_load_15min",
    "depot_daily_summary",
]
RESUME_ARTIFACT_FILES = {
    "block_templates_lsoa": "block_templates_lsoa.parquet",
    "block_instances": "block_instances_annual.parquet",
    "depot_registry": "depot_registry.parquet",
    "ev_specs": "ev_bus_specs.parquet",
    "assignments": "vehicle_day_assignments.parquet",
    "assignment_diagnostics": "vehicle_day_assignment_diagnostics.parquet",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run the annual depot-only bus charging load pipeline.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--blocks", type=Path, default=DEFAULT_BLOCKS)
    parser.add_argument("--ev-inventory", type=Path, default=DEFAULT_EV_INVENTORY)
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    parser.add_argument("--scenario-mode", default="ev_stock_scale")
    parser.add_argument("--charging-mode", default="depot_only")
    parser.add_argument(
        "--assignment-mode",
        default="sample_then_feasible_match",
        choices=["sample_then_feasible_match", "legacy_random_zip"],
        help="sample_then_feasible_match: feasibility-aware matching. legacy_random_zip: pre-refactor random pairing.",
    )
    parser.add_argument(
        "--sample-block-multiplier",
        type=float,
        default=3.0,
        help="Sampled blocks per day = ceil(n_ev_specs * multiplier), capped by active blocks (and by positive-weight blocks under supply_weighted). PR 1 parity: 1.0.",
    )
    parser.add_argument(
        "--home-depot-method",
        default="service_supply_weighted",
        choices=["service_supply_weighted", "source_lsoa_nearest", "none"],
        help=(
            "service_supply_weighted (main scenario): apportion the fleet to depots proportional to "
            "service supply (largest-remainder seats, seeded fill). "
            "source_lsoa_nearest (sensitivity): population-weighted synthetic EV siting via the inventory "
            "source_lsoa; over-represents dense urban fleets. "
            "none: PR 1 spatially unconstrained matching (A/B baseline)."
        ),
    )
    parser.add_argument(
        "--home-depot-supply-weight",
        default="block_instances",
        choices=["block_instances", "bus_km"],
        help="Supply weight for service_supply_weighted: per-depot block-instance count or summed passenger km.",
    )
    parser.add_argument(
        "--home-depot-radius-km",
        type=float,
        default=10.0,
        help="Admissible block depots lie within this distance of the EV's home depot; 0 = strict same-depot. Ignored with --home-depot-method none.",
    )
    parser.add_argument(
        "--block-sampling",
        default="supply_weighted",
        choices=["supply_weighted", "uniform"],
        help="supply_weighted: daily blocks sampled proportional to in-radius home fleet (plan v2 §15 (b)); requires home depots. uniform: PR 1 sampling.",
    )
    parser.add_argument("--depot-power-kw", type=float, default=100.0)
    parser.add_argument("--default-overnight-end-hour", type=float, default=6.0)
    parser.add_argument("--seed", type=int, default=20260603)
    parser.add_argument("--start-date", default=FEED_YEAR_START.isoformat())
    parser.add_argument("--end-date", default=FEED_YEAR_END.isoformat())
    parser.add_argument("--max-days", type=int, default=0, help="Smoke/debug only; 0 means the full requested date range.")
    parser.add_argument("--max-vehicle-days", type=int, default=0, help="Smoke/debug only; 0 means no cap.")
    parser.add_argument("--stream", type=_parse_bool, default=True, help="Write event/artifact tables by service_date. Use false for batch parity checks.")
    parser.add_argument(
        "--resume",
        type=_parse_bool,
        default=False,
        help="Resume an interrupted streaming run: reuse persisted upstream artifacts in --out-dir and skip service_date partitions already complete on disk.",
    )
    parser.add_argument("--date-chunk-size", type=int, default=1, help="Number of service_date values to process per stream chunk.")
    parser.add_argument("--use-trip-level-events", type=_parse_bool, default=True)
    parser.add_argument("--enable-physical-depot-sources", type=_parse_bool, default=False)
    return parser.parse_args()


def run_pipeline(args: argparse.Namespace) -> dict[str, object]:
    if args.charging_mode != "depot_only":
        raise ValueError("Only --charging-mode depot_only is supported.")
    if args.scenario_mode != "ev_stock_scale":
        raise NotImplementedError("Only --scenario-mode ev_stock_scale is implemented.")
    if args.enable_physical_depot_sources:
        raise NotImplementedError("Physical depot source ingestion is not implemented in this pipeline version.")
    if int(getattr(args, "date_chunk_size", 1)) <= 0:
        raise ValueError("--date-chunk-size must be a positive integer.")
    resume = bool(getattr(args, "resume", False))
    if resume and not bool(getattr(args, "stream", True)):
        raise ValueError("--resume requires --stream true.")

    out_dir = _resolve_path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    start_date = coerce_date(args.start_date)
    end_date = coerce_date(args.end_date)
    if int(args.max_days) > 0:
        end_date = min(end_date, start_date + dt.timedelta(days=int(args.max_days) - 1))
    start_iso = start_date.isoformat()
    end_iso = end_date.isoformat()

    print(f"[annual_depot] reading blocks: {args.blocks}", flush=True)
    blocks = pd.read_parquet(_resolve_path(args.blocks))
    print(f"[annual_depot] reading EV inventory: {args.ev_inventory}", flush=True)
    ev_inventory = pd.read_csv(_resolve_path(args.ev_inventory))

    print("[annual_depot] preflight", flush=True)
    try:
        service_calendar = load_service_calendar()
    except FileNotFoundError as exc:
        preflight, _ = run_preflight(
            blocks,
            ev_inventory,
            calendar_available=False,
            lsoa_attach_available=Path(DEFAULT_ONSPD_PATH).exists(),
        )
        write_preflight_summary(preflight, out_dir)
        raise RuntimeError("Preflight failed: GTFS service calendar is unavailable.") from exc
    preflight, _ = run_preflight(
        blocks,
        ev_inventory,
        calendar_available=True,
        lsoa_attach_available=Path(DEFAULT_ONSPD_PATH).exists(),
    )
    write_preflight_summary(preflight, out_dir)
    if not bool(preflight.get("preflight_ok", False)):
        raise RuntimeError(f"Preflight failed; see {out_dir / 'preflight_summary.md'}")

    if resume:
        print("[annual_depot] resume: loading persisted upstream artifacts", flush=True)
        artifact_paths = {key: out_dir / name for key, name in RESUME_ARTIFACT_FILES.items()}
        missing = [str(path) for path in artifact_paths.values() if not path.exists()]
        if missing:
            raise RuntimeError(f"--resume requires persisted upstream artifacts in --out-dir; missing: {missing}")
        block_templates_lsoa = pd.read_parquet(artifact_paths["block_templates_lsoa"])
        block_instances = pd.read_parquet(artifact_paths["block_instances"])
        depot_registry = pd.read_parquet(artifact_paths["depot_registry"])
        ev_specs = pd.read_parquet(artifact_paths["ev_specs"])
        assignments = pd.read_parquet(artifact_paths["assignments"])
        assignment_diagnostics = pd.read_parquet(artifact_paths["assignment_diagnostics"])
    else:
        print("[annual_depot] building block templates", flush=True)
        block_templates, block_template_diag = build_block_templates(blocks)
        block_templates.to_parquet(out_dir / "block_templates.parquet", index=False)
        block_template_diag.to_parquet(out_dir / "block_template_build_diagnostics.parquet", index=False)

        print("[annual_depot] attaching LSOA and region", flush=True)
        centroids = _try_load_centroids()
        block_templates_lsoa, lsoa_region_diag = attach_lsoa_and_region(
            block_templates,
            centroids=centroids,
            max_distance_km=0.25,
        )
        block_templates_lsoa.to_parquet(out_dir / "block_templates_lsoa.parquet", index=False)
        lsoa_region_diag.to_parquet(out_dir / "lsoa_region_diagnostics.parquet", index=False)

        print("[annual_depot] expanding annual block instances", flush=True)
        block_instances, calendar_diag = expand_block_instances(
            block_templates_lsoa,
            start_date=start_iso,
            end_date=end_iso,
            calendar=service_calendar,
        )
        calendar_diag.to_parquet(out_dir / "calendar_expansion_diagnostics.parquet", index=False)

        print("[annual_depot] building depot registry", flush=True)
        depot_registry, depot_diag, block_depot = build_operational_depot_registry(
            block_templates_lsoa,
            block_instances,
            centroids=centroids,
        )
        block_instances = attach_depots_to_instances(block_instances, block_depot)
        block_instances.to_parquet(out_dir / "block_instances_annual.parquet", index=False)
        depot_registry.to_parquet(out_dir / "depot_registry.parquet", index=False)
        depot_diag.to_parquet(out_dir / "depot_inference_diagnostics.parquet", index=False)

        print("[annual_depot] building EV bus specs", flush=True)
        ev_specs, ev_diag = build_ev_bus_specs(ev_inventory)

        assignment_mode = str(getattr(args, "assignment_mode", "sample_then_feasible_match"))
        home_depot_method = str(getattr(args, "home_depot_method", "none"))
        home_depot_radius_km: float | None = None
        if assignment_mode != "legacy_random_zip" and home_depot_method != "none":
            home_centroids = None
            if home_depot_method == "source_lsoa_nearest":
                home_centroids = _try_load_extended_centroids()
                if home_centroids is None:
                    home_centroids = centroids
                if home_centroids is None:
                    raise RuntimeError("--home-depot-method source_lsoa_nearest requires ONSPD centroids, which could not be loaded.")
            print(f"[annual_depot] assigning home depots (method={home_depot_method})", flush=True)
            ev_specs, home_depot_diag = assign_home_depots(
                ev_specs,
                depot_registry,
                home_centroids,
                method=home_depot_method,
                block_instances=block_instances,
                supply_weight=str(getattr(args, "home_depot_supply_weight", "block_instances")),
                seed=int(args.seed),
            )
            home_depot_diag.to_parquet(out_dir / "home_depot_assignment_diagnostics.parquet", index=False)
            supply_demand = build_depot_supply_demand(ev_specs, block_instances, depot_registry)
            supply_demand.to_parquet(out_dir / "depot_supply_demand.parquet", index=False)
            home_depot_radius_km = float(getattr(args, "home_depot_radius_km", 0.0))
        ev_specs.to_parquet(out_dir / "ev_bus_specs.parquet", index=False)
        ev_diag.to_parquet(out_dir / "ev_bus_spec_diagnostics.parquet", index=False)

        block_sampling = str(getattr(args, "block_sampling", "uniform"))
        if home_depot_radius_km is None and block_sampling == "supply_weighted":
            print("[annual_depot] note: --block-sampling supply_weighted requires home depots; falling back to uniform", flush=True)
            block_sampling = "uniform"

        max_vehicle_days = int(args.max_vehicle_days) if int(args.max_vehicle_days) > 0 else None
        print(f"[annual_depot] assigning vehicle-days (mode={assignment_mode})", flush=True)
        if assignment_mode == "legacy_random_zip":
            from mobility.bus.annual_vehicle_day_assignment import build_vehicle_day_assignments

            assignments = build_vehicle_day_assignments(
                block_instances,
                ev_specs,
                seed=int(args.seed),
                scenario_mode=args.scenario_mode,
                max_vehicle_days=max_vehicle_days,
            )
            assignment_diagnostics = _assignment_diagnostics(assignments)
            unmatched_blocks = pd.DataFrame()
        else:
            from mobility.bus.annual_vehicle_day_assignment import build_feasible_vehicle_day_assignments

            assignments, assignment_diagnostics, unmatched_blocks = build_feasible_vehicle_day_assignments(
                block_instances,
                ev_specs,
                depot_registry,
                seed=int(args.seed),
                scenario_mode=args.scenario_mode,
                sample_block_multiplier=float(getattr(args, "sample_block_multiplier", 1.0)),
                max_vehicle_days=max_vehicle_days,
                home_depot_radius_km=home_depot_radius_km,
                block_sampling=block_sampling,
            )
        assignments.to_parquet(out_dir / "vehicle_day_assignments.parquet", index=False)
        assignment_diagnostics.to_parquet(out_dir / "vehicle_day_assignment_diagnostics.parquet", index=False)
        unmatched_blocks.to_parquet(out_dir / "unmatched_sampled_blocks.parquet", index=False)

    if bool(getattr(args, "stream", True)):
        return _run_streaming_pipeline_tail(
            args,
            out_dir=out_dir,
            start_iso=start_iso,
            end_iso=end_iso,
            preflight=preflight,
            block_templates_lsoa=block_templates_lsoa,
            block_instances=block_instances,
            depot_registry=depot_registry,
            ev_specs=ev_specs,
            assignments=assignments,
            assignment_diagnostics=assignment_diagnostics,
        )
    return _run_batch_pipeline_tail(
        args,
        out_dir=out_dir,
        start_iso=start_iso,
        end_iso=end_iso,
        preflight=preflight,
        block_templates_lsoa=block_templates_lsoa,
        block_instances=block_instances,
        depot_registry=depot_registry,
        ev_specs=ev_specs,
        assignments=assignments,
        assignment_diagnostics=assignment_diagnostics,
    )


def _run_batch_pipeline_tail(
    args: argparse.Namespace,
    *,
    out_dir: Path,
    start_iso: str,
    end_iso: str,
    preflight: dict[str, Any],
    block_templates_lsoa: pd.DataFrame,
    block_instances: pd.DataFrame,
    depot_registry: pd.DataFrame,
    ev_specs: pd.DataFrame,
    assignments: pd.DataFrame,
    assignment_diagnostics: pd.DataFrame | None = None,
) -> dict[str, object]:
    out_dir.mkdir(parents=True, exist_ok=True)
    _prepare_batch_output_paths(out_dir)
    print("[annual_depot] batch: building event ledger", flush=True)
    events, soc_summary = _build_events_and_soc(
        args,
        assignments=assignments,
        block_instances=block_instances,
        block_templates_lsoa=block_templates_lsoa,
        ev_specs=ev_specs,
        depot_registry=depot_registry,
    )
    events.to_parquet(out_dir / "vehicle_day_events.parquet", index=False)
    soc_summary.to_parquet(out_dir / "vehicle_day_soc_summary.parquet", index=False)

    print("[annual_depot] batch: building bus observability artifacts", flush=True)
    bus_trip_records = build_bus_trip_records(events, feed_year_start=start_iso)
    bus_charging_events = build_bus_charging_event_records(events, feed_year_start=start_iso)
    bus_ev_state_records = build_bus_ev_state_records(events, feed_year_start=start_iso)
    bus_trip_records.to_parquet(out_dir / "bus_trip_records.parquet", index=False)
    bus_charging_events.to_parquet(out_dir / "bus_charging_events.parquet", index=False)
    bus_ev_state_records.to_parquet(out_dir / "bus_ev_state_records.parquet", index=False)

    print("[annual_depot] batch: aggregating depot load", flush=True)
    depot_load, depot_daily = aggregate_depot_load_15min(events, depot_registry, soc_summary)
    if not depot_load_energy_matches_events(depot_load, events):
        raise RuntimeError("depot_load_15min energy does not match depot charging event ledger energy.")
    depot_load.to_parquet(out_dir / "depot_load_15min.parquet", index=False)
    depot_daily.to_parquet(out_dir / "depot_daily_summary.parquet", index=False)

    summary_md = build_run_summary_markdown(
        preflight_summary=preflight,
        block_templates=block_templates_lsoa,
        block_instances=block_instances,
        depot_registry=depot_registry,
        ev_bus_specs=ev_specs,
        vehicle_day_assignments=assignments,
        assignment_diagnostics=assignment_diagnostics,
        vehicle_day_soc_summary=soc_summary,
        depot_load_15min=depot_load,
        depot_daily_summary=depot_daily,
        bus_trip_records=bus_trip_records,
        bus_charging_events=bus_charging_events,
        bus_ev_state_records=bus_ev_state_records,
        feed_year_start=start_iso,
        feed_year_end=end_iso,
        scenario_mode=args.scenario_mode,
    )
    write_run_summary(summary_md, out_dir)
    print(f"[annual_depot] wrote batch outputs to {out_dir}", flush=True)
    return _result_dict(
        out_dir=out_dir,
        stream=False,
        block_templates_lsoa=block_templates_lsoa,
        block_instances=block_instances,
        depot_registry=depot_registry,
        assignments=assignments,
        depot_load=depot_load,
        artifact_counts={
            "n_vehicle_day_events": len(events),
            "n_vehicle_day_soc_summary": len(soc_summary),
            "n_bus_trip_records": len(bus_trip_records),
            "n_bus_charging_events": len(bus_charging_events),
            "n_bus_ev_state_records": len(bus_ev_state_records),
        },
    )


def _run_streaming_pipeline_tail(
    args: argparse.Namespace,
    *,
    out_dir: Path,
    start_iso: str,
    end_iso: str,
    preflight: dict[str, Any],
    block_templates_lsoa: pd.DataFrame,
    block_instances: pd.DataFrame,
    depot_registry: pd.DataFrame,
    ev_specs: pd.DataFrame,
    assignments: pd.DataFrame,
    assignment_diagnostics: pd.DataFrame | None = None,
) -> dict[str, object]:
    out_dir.mkdir(parents=True, exist_ok=True)
    resume = bool(getattr(args, "resume", False))
    service_dates = _service_dates(assignments)
    depot_load_frames: list[pd.DataFrame] = []
    depot_daily_frames: list[pd.DataFrame] = []
    stats = _new_stream_stats()

    if resume:
        done_dates, redo_dates = _scan_resume_dates(out_dir, service_dates)
        _clear_partition_dirs(out_dir, redo_dates)
        print(
            f"[annual_depot] resume: {len(done_dates):,} day(s) already complete on disk, "
            f"{len(redo_dates):,} incomplete day(s) cleared for redo, "
            f"{len(service_dates) - len(done_dates):,} day(s) to run",
            flush=True,
        )
        _replay_completed_dates(out_dir, done_dates, depot_registry, stats, depot_load_frames, depot_daily_frames)
        remaining_dates = [date for date in service_dates if date not in set(done_dates)]
    else:
        _prepare_stream_output_dirs(out_dir)
        remaining_dates = service_dates

    date_chunks = list(_chunked(remaining_dates, int(args.date_chunk_size)))
    assignment_dates = assignments["service_date"].astype(str) if "service_date" in assignments.columns else pd.Series(dtype=str)
    block_dates = block_instances["service_date"].astype(str) if "service_date" in block_instances.columns else pd.Series(dtype=str)

    print(
        f"[annual_depot] stream: processing {len(remaining_dates):,} service_date(s) in {len(date_chunks):,} chunk(s)",
        flush=True,
    )
    for chunk_index, dates in enumerate(date_chunks, start=1):
        date_set = set(dates)
        label = dates[0] if len(dates) == 1 else f"{dates[0]}..{dates[-1]}"
        print(f"[annual_depot] stream chunk {chunk_index:,}/{len(date_chunks):,}: {label}", flush=True)
        chunk_assignments = assignments.loc[assignment_dates.isin(date_set)].copy()
        chunk_blocks = block_instances.loc[block_dates.isin(date_set)].copy()
        if chunk_assignments.empty:
            continue

        events, soc_summary = _build_events_and_soc(
            args,
            assignments=chunk_assignments,
            block_instances=chunk_blocks,
            block_templates_lsoa=block_templates_lsoa,
            ev_specs=ev_specs,
            depot_registry=depot_registry,
        )
        _write_partitioned_by_service_date(events, out_dir / "vehicle_day_events")
        _write_partitioned_by_service_date(soc_summary, out_dir / "vehicle_day_soc_summary")

        bus_trip_records = build_bus_trip_records(events, feed_year_start=start_iso)
        bus_charging_events = build_bus_charging_event_records(events, feed_year_start=start_iso)
        bus_ev_state_records = build_bus_ev_state_records(events, feed_year_start=start_iso)
        _write_partitioned_by_service_date(bus_trip_records, out_dir / "bus_trip_records")
        _write_partitioned_by_service_date(bus_charging_events, out_dir / "bus_charging_events")
        _write_partitioned_by_service_date(bus_ev_state_records, out_dir / "bus_ev_state_records")

        depot_load, depot_daily = aggregate_depot_load_15min(events, depot_registry, soc_summary)
        for service_date in dates:
            day_events = events.loc[events["service_date"].astype(str).eq(service_date)]
            day_load = depot_load.loc[depot_load["service_date"].astype(str).eq(service_date)] if "service_date" in depot_load.columns else depot_load
            if not depot_load_energy_matches_events(day_load, day_events):
                raise RuntimeError(f"Daily depot load energy mismatch for service_date={service_date}.")
        _write_partitioned_by_service_date(depot_load, out_dir / "depot_load_15min")
        _write_partitioned_by_service_date(depot_daily, out_dir / "depot_daily_summary")
        depot_load_frames.append(depot_load)
        depot_daily_frames.append(depot_daily)
        _add_stream_stats(stats, events, soc_summary, len(bus_trip_records), len(bus_charging_events), len(bus_ev_state_records), depot_load)

        del events, soc_summary, bus_trip_records, bus_charging_events, bus_ev_state_records, depot_load, depot_daily
        gc.collect()

    depot_load, depot_daily = _combine_stream_load_outputs(depot_load_frames, depot_daily_frames, depot_registry)
    _assert_stream_energy_close(stats)
    depot_load.to_parquet(out_dir / "depot_load_15min.parquet", index=False)
    depot_daily.to_parquet(out_dir / "depot_daily_summary.parquet", index=False)
    _write_empty_stream_datasets_if_needed(out_dir, stats)

    summary_stats = _finalize_stream_stats(stats)
    summary_md = build_run_summary_markdown(
        preflight_summary=preflight,
        block_templates=block_templates_lsoa,
        block_instances=block_instances,
        depot_registry=depot_registry,
        ev_bus_specs=ev_specs,
        vehicle_day_assignments=assignments,
        assignment_diagnostics=assignment_diagnostics,
        vehicle_day_soc_summary=pd.DataFrame(),
        depot_load_15min=depot_load,
        depot_daily_summary=depot_daily,
        feed_year_start=start_iso,
        feed_year_end=end_iso,
        scenario_mode=args.scenario_mode,
        preaggregated_stats=summary_stats,
    )
    write_run_summary(summary_md, out_dir)
    print(f"[annual_depot] wrote stream outputs to {out_dir}", flush=True)
    return _result_dict(
        out_dir=out_dir,
        stream=True,
        block_templates_lsoa=block_templates_lsoa,
        block_instances=block_instances,
        depot_registry=depot_registry,
        assignments=assignments,
        depot_load=depot_load,
        artifact_counts=summary_stats,
    )


def _build_events_and_soc(
    args: argparse.Namespace,
    *,
    assignments: pd.DataFrame,
    block_instances: pd.DataFrame,
    block_templates_lsoa: pd.DataFrame,
    ev_specs: pd.DataFrame,
    depot_registry: pd.DataFrame,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    events = build_vehicle_day_events(
        assignments,
        block_instances,
        block_templates_lsoa,
        ev_specs,
        depot_registry,
        depot_power_kw=float(args.depot_power_kw),
        default_overnight_end_hour=float(args.default_overnight_end_hour),
        use_trip_level_events=bool(args.use_trip_level_events),
    )
    return apply_depot_only_soc(events, depot_power_kw=float(args.depot_power_kw))


def _prepare_batch_output_paths(out_dir: Path) -> None:
    for name in STREAM_DATASET_NAMES:
        _clear_output_path(out_dir / name)


def _prepare_stream_output_dirs(out_dir: Path) -> None:
    for name in STREAM_DATASET_NAMES + STREAM_LOAD_DATASET_NAMES:
        _clear_output_path(out_dir / name)
        if name not in STREAM_LOAD_DATASET_NAMES:
            _clear_output_path(out_dir / f"{name}.parquet")


def _clear_output_path(path: Path) -> None:
    if path.is_dir():
        shutil.rmtree(path)
    elif path.exists():
        path.unlink()


def _write_partitioned_by_service_date(frame: pd.DataFrame, dataset_dir: Path) -> None:
    if frame.empty:
        return
    if "service_date" not in frame.columns:
        raise ValueError(f"Cannot partition {dataset_dir.name}: missing service_date column.")
    dataset_dir.mkdir(parents=True, exist_ok=True)
    for service_date, group in frame.groupby("service_date", sort=True, dropna=False):
        service_date_text = str(service_date)
        part_dir = dataset_dir / f"service_date={service_date_text}"
        part_dir.mkdir(parents=True, exist_ok=True)
        payload = group.drop(columns=["service_date"]).reset_index(drop=True)
        payload.to_parquet(part_dir / "part.parquet", index=False)


def _partition_file(out_dir: Path, name: str, service_date: str) -> Path:
    return out_dir / name / f"service_date={service_date}" / "part.parquet"


def _partition_row_count(out_dir: Path, name: str, service_date: str) -> int | None:
    # Returns None when the partition is missing or unreadable (e.g. truncated by a kill mid-write).
    path = _partition_file(out_dir, name, service_date)
    if not path.is_file():
        return None
    try:
        return int(pq.read_metadata(path).num_rows)
    except Exception:
        return None


def _read_partition(out_dir: Path, name: str, service_date: str) -> pd.DataFrame:
    frame = pd.read_parquet(_partition_file(out_dir, name, service_date))
    frame["service_date"] = str(service_date)
    return frame


def _scan_resume_dates(out_dir: Path, service_dates: list[str]) -> tuple[list[str], list[str]]:
    # A service_date is complete when every event/artifact dataset has a readable partition.
    # Depot load partitions are derived and rebuilt from events/soc when absent.
    done: list[str] = []
    redo: list[str] = []
    for service_date in service_dates:
        counts = [_partition_row_count(out_dir, name, service_date) for name in STREAM_DATASET_NAMES]
        if all(count is not None for count in counts):
            done.append(service_date)
        elif any(_partition_file(out_dir, name, service_date).parent.exists() for name in STREAM_DATASET_NAMES + STREAM_LOAD_DATASET_NAMES):
            redo.append(service_date)
    return done, redo


def _clear_partition_dirs(out_dir: Path, service_dates: list[str]) -> None:
    for service_date in service_dates:
        for name in STREAM_DATASET_NAMES + STREAM_LOAD_DATASET_NAMES:
            _clear_output_path(_partition_file(out_dir, name, service_date).parent)


def _replay_completed_dates(
    out_dir: Path,
    done_dates: list[str],
    depot_registry: pd.DataFrame,
    stats: dict[str, Any],
    depot_load_frames: list[pd.DataFrame],
    depot_daily_frames: list[pd.DataFrame],
) -> None:
    for index, service_date in enumerate(done_dates, start=1):
        print(f"[annual_depot] resume replay {index:,}/{len(done_dates):,}: {service_date}", flush=True)
        events = _read_partition(out_dir, "vehicle_day_events", service_date)
        soc_summary = _read_partition(out_dir, "vehicle_day_soc_summary", service_date)
        load_ok = _partition_row_count(out_dir, "depot_load_15min", service_date) is not None
        daily_ok = _partition_row_count(out_dir, "depot_daily_summary", service_date) is not None
        if load_ok and daily_ok:
            depot_load = _read_partition(out_dir, "depot_load_15min", service_date)
            depot_daily = _read_partition(out_dir, "depot_daily_summary", service_date)
        else:
            depot_load, depot_daily = aggregate_depot_load_15min(events, depot_registry, soc_summary)
            _write_partitioned_by_service_date(depot_load, out_dir / "depot_load_15min")
            _write_partitioned_by_service_date(depot_daily, out_dir / "depot_daily_summary")
        if not depot_load_energy_matches_events(depot_load, events):
            raise RuntimeError(f"Resume replay: depot load energy mismatch for service_date={service_date}.")
        _add_stream_stats(
            stats,
            events,
            soc_summary,
            _partition_row_count(out_dir, "bus_trip_records", service_date) or 0,
            _partition_row_count(out_dir, "bus_charging_events", service_date) or 0,
            _partition_row_count(out_dir, "bus_ev_state_records", service_date) or 0,
            depot_load,
        )
        depot_load_frames.append(depot_load)
        depot_daily_frames.append(depot_daily)
        del events, soc_summary, depot_load, depot_daily
    if done_dates:
        gc.collect()


def _write_empty_stream_datasets_if_needed(out_dir: Path, stats: dict[str, Any]) -> None:
    empty_specs = {
        "vehicle_day_events": ("n_vehicle_day_events", _empty_event_columns()),
        "vehicle_day_soc_summary": ("n_vehicle_day_soc_summary", _empty_soc_summary_columns()),
        "bus_trip_records": ("n_bus_trip_records", BUS_TRIP_RECORD_COLUMNS),
        "bus_charging_events": ("n_bus_charging_events", BUS_CHARGING_EVENT_COLUMNS),
        "bus_ev_state_records": ("n_bus_ev_state_records", BUS_EV_STATE_RECORD_COLUMNS),
    }
    for name, (count_key, columns) in empty_specs.items():
        if int(stats.get(count_key, 0)) == 0:
            dataset_dir = out_dir / name
            dataset_dir.mkdir(parents=True, exist_ok=True)
            pd.DataFrame(columns=list(columns)).to_parquet(dataset_dir / "_empty.parquet", index=False)


def _empty_event_columns() -> list[str]:
    return list(build_vehicle_day_events(pd.DataFrame(), pd.DataFrame(), pd.DataFrame(), pd.DataFrame(), pd.DataFrame()).columns)


def _empty_soc_summary_columns() -> list[str]:
    _, summary = apply_depot_only_soc(pd.DataFrame(columns=_empty_event_columns()))
    return list(summary.columns)


def _combine_stream_load_outputs(
    depot_load_frames: list[pd.DataFrame],
    depot_daily_frames: list[pd.DataFrame],
    depot_registry: pd.DataFrame,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    if depot_load_frames:
        depot_load = pd.concat(depot_load_frames, ignore_index=True)
    else:
        depot_load, _ = aggregate_depot_load_15min(pd.DataFrame(), depot_registry, pd.DataFrame())
    if depot_daily_frames:
        depot_daily = pd.concat(depot_daily_frames, ignore_index=True)
    else:
        _, depot_daily = aggregate_depot_load_15min(pd.DataFrame(), depot_registry, pd.DataFrame())
    depot_load = _sort_if_columns(depot_load, ["depot_id", "service_date", "slot_start_datetime", "slot_index"])
    depot_daily = _sort_if_columns(depot_daily, ["depot_id", "service_date", "slot_date"])
    return depot_load.reset_index(drop=True), depot_daily.reset_index(drop=True)


def _new_stream_stats() -> dict[str, Any]:
    return {
        "n_vehicle_day_events": 0,
        "n_vehicle_day_soc_summary": 0,
        "n_bus_trip_records": 0,
        "n_bus_charging_events": 0,
        "n_bus_ev_state_records": 0,
        "total_energy_kwh": 0.0,
        "total_deadhead_km": 0.0,
        "n_soc_rows": 0,
        "n_soc_feasible": 0,
        "event_charge_kwh": 0.0,
        "load_charge_kwh": 0.0,
        "top_shortfall_frames": [],
    }


def _add_stream_stats(
    stats: dict[str, Any],
    events: pd.DataFrame,
    soc_summary: pd.DataFrame,
    n_bus_trip_records: int,
    n_bus_charging_events: int,
    n_bus_ev_state_records: int,
    depot_load: pd.DataFrame,
) -> None:
    stats["n_vehicle_day_events"] += int(len(events))
    stats["n_vehicle_day_soc_summary"] += int(len(soc_summary))
    stats["n_bus_trip_records"] += int(n_bus_trip_records)
    stats["n_bus_charging_events"] += int(n_bus_charging_events)
    stats["n_bus_ev_state_records"] += int(n_bus_ev_state_records)
    stats["total_energy_kwh"] += _sum_numeric(soc_summary, "total_energy_kwh")
    stats["total_deadhead_km"] += _sum_numeric(soc_summary, "total_deadhead_km")
    if not soc_summary.empty and "depot_only_feasible" in soc_summary.columns:
        stats["n_soc_rows"] += int(len(soc_summary))
        stats["n_soc_feasible"] += int(soc_summary["depot_only_feasible"].astype(bool).sum())
    if not events.empty and {"event_type", "charge_kwh_added"}.issubset(events.columns):
        depot_events = events["event_type"].isin(DEPOT_CHARGING_EVENT_TYPES)
        stats["event_charge_kwh"] += float(pd.to_numeric(events.loc[depot_events, "charge_kwh_added"], errors="coerce").fillna(0.0).sum())
    stats["load_charge_kwh"] += _sum_numeric(depot_load, "charge_kwh")
    if not soc_summary.empty and {"block_instance_id", "energy_shortfall_kwh"}.issubset(soc_summary.columns):
        top = soc_summary.loc[:, ["block_instance_id", "energy_shortfall_kwh"]].copy()
        top["energy_shortfall_kwh"] = pd.to_numeric(top["energy_shortfall_kwh"], errors="coerce").fillna(0.0)
        stats["top_shortfall_frames"].append(top.sort_values("energy_shortfall_kwh", ascending=False).head(10))


def _finalize_stream_stats(stats: dict[str, Any]) -> dict[str, Any]:
    out = {key: value for key, value in stats.items() if key != "top_shortfall_frames"}
    n_soc_rows = int(stats.get("n_soc_rows", 0))
    out["matched_vehicle_day_feasible_share"] = float(stats["n_soc_feasible"] / n_soc_rows) if n_soc_rows else float("nan")
    frames = stats.get("top_shortfall_frames", [])
    if frames:
        top = pd.concat(frames, ignore_index=True).sort_values("energy_shortfall_kwh", ascending=False).head(10)
        out["top_blocks_by_shortfall"] = [
            {"block_instance_id": str(row.block_instance_id), "energy_shortfall_kwh": float(row.energy_shortfall_kwh)}
            for row in top.itertuples(index=False)
        ]
    else:
        out["top_blocks_by_shortfall"] = []
    return out


def _assert_stream_energy_close(stats: dict[str, Any]) -> None:
    event_energy = float(stats.get("event_charge_kwh", 0.0))
    load_energy = float(stats.get("load_charge_kwh", 0.0))
    if abs(load_energy - event_energy) > 1e-6 + 1e-9 * abs(event_energy):
        raise RuntimeError(
            "Annual depot load energy mismatch: "
            f"load_charge_kwh={load_energy:.12f}, event_charge_kwh={event_energy:.12f}."
        )


def _sum_numeric(frame: pd.DataFrame, column: str) -> float:
    if frame.empty or column not in frame.columns:
        return 0.0
    return float(pd.to_numeric(frame[column], errors="coerce").fillna(0.0).sum())


def _sort_if_columns(frame: pd.DataFrame, columns: list[str]) -> pd.DataFrame:
    present = [column for column in columns if column in frame.columns]
    if not present or frame.empty:
        return frame
    return frame.sort_values(present, kind="stable")


def _service_dates(assignments: pd.DataFrame) -> list[str]:
    if assignments.empty or "service_date" not in assignments.columns:
        return []
    return sorted(assignments["service_date"].dropna().astype(str).unique().tolist())


def _chunked(values: list[str], size: int):
    chunk_size = max(1, int(size))
    for start in range(0, len(values), chunk_size):
        yield values[start : start + chunk_size]


def _resolve_path(value: str | os.PathLike[str]) -> Path:
    return Path(os.path.expandvars(os.path.expanduser(str(value)))).resolve()


def _result_dict(
    *,
    out_dir: Path,
    stream: bool,
    block_templates_lsoa: pd.DataFrame,
    block_instances: pd.DataFrame,
    depot_registry: pd.DataFrame,
    assignments: pd.DataFrame,
    depot_load: pd.DataFrame,
    artifact_counts: dict[str, Any],
) -> dict[str, object]:
    return {
        "out_dir": str(out_dir),
        "stream": bool(stream),
        "n_block_templates": len(block_templates_lsoa),
        "n_block_instances": len(block_instances),
        "n_vehicle_day_assignments": len(assignments),
        "n_depots": len(depot_registry),
        "n_load_rows": len(depot_load),
        "n_vehicle_day_events": int(artifact_counts.get("n_vehicle_day_events", 0)),
        "n_vehicle_day_soc_summary": int(artifact_counts.get("n_vehicle_day_soc_summary", 0)),
        "n_bus_trip_records": int(artifact_counts.get("n_bus_trip_records", 0)),
        "n_bus_charging_events": int(artifact_counts.get("n_bus_charging_events", 0)),
        "n_bus_ev_state_records": int(artifact_counts.get("n_bus_ev_state_records", 0)),
    }


def _try_load_centroids() -> pd.DataFrame | None:
    try:
        return load_lsoa_centroids()
    except (FileNotFoundError, KeyError, ValueError, pd.errors.EmptyDataError):
        return None


def _try_load_extended_centroids() -> pd.DataFrame | None:
    try:
        return load_extended_lsoa_centroids()
    except (FileNotFoundError, KeyError, ValueError, pd.errors.EmptyDataError):
        return None


def _assignment_diagnostics(assignments: pd.DataFrame) -> pd.DataFrame:
    columns = [
        "service_date",
        "n_available_block_instances_for_service_date",
        "n_assigned_block_instances_for_service_date",
        "n_unassigned_block_instances_for_service_date",
        "daily_assignment_coverage_share",
    ]
    if assignments.empty or not set(columns).issubset(assignments.columns):
        return pd.DataFrame(columns=columns)
    return assignments.loc[:, columns].drop_duplicates("service_date").sort_values("service_date", kind="stable")


def _parse_bool(value: str | bool) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"true", "1", "yes", "y"}:
        return True
    if text in {"false", "0", "no", "n"}:
        return False
    raise argparse.ArgumentTypeError(f"Expected true/false, got {value!r}")


def main() -> None:
    summary = run_pipeline(parse_args())
    for key, value in summary.items():
        print(f"{key}: {value}")


if __name__ == "__main__":
    main()
