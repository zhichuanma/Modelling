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
from mobility.bus.annual_soc_state import (  # noqa: E402
    IDLE_EVENT_TYPE,
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
from mobility.bus.annual_vehicle_day_assignment import (  # noqa: E402
    assign_vehicle_days_for_date,
    assignment_frames_from_records,
    build_matching_context,
    merge_depot_coords,
)
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
# Carryover mode (plan v2 §8): assignments are produced inside the day loop and
# the per-vehicle SOC state is checkpointed per service_date for exact resume.
CARRYOVER_ASSIGNMENT_DATASET_NAMES = [
    "vehicle_day_assignments",
    "vehicle_day_assignment_diagnostics",
    "unmatched_sampled_blocks",
]
SOC_STATE_DATASET_NAME = "vehicle_soc_state"
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
    parser.add_argument(
        "--soc-mode",
        default="daily_reset",
        choices=["daily_reset", "carryover"],
        help=(
            "daily_reset: every vehicle-day starts at usable_soc_max (legacy, default). "
            "carryover (plan v2 §8): SOC carries across service days via per-vehicle ledger "
            "stitching; assignment moves inside the chronological day loop; idle vehicles "
            "charge at their home depot; requires feasibility-aware assignment, home depots, "
            "and --stream true."
        ),
    )
    parser.add_argument(
        "--warmup-days",
        type=int,
        default=14,
        help="Carryover only (§14.5): first N service dates are flagged is_warmup and excluded from headline summary metrics (rows stay in all tables).",
    )
    parser.add_argument(
        "--idle-vehicle-charging-policy",
        default="home_depot",
        choices=["home_depot", "none"],
        help="Carryover only (§14.4): home_depot charges idle vehicles to usable_soc_max at their home depot (explicit events); none disables idle charging.",
    )
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
    soc_mode = str(getattr(args, "soc_mode", "daily_reset"))
    if soc_mode == "carryover":
        if str(getattr(args, "assignment_mode", "sample_then_feasible_match")) == "legacy_random_zip":
            raise ValueError("--soc-mode carryover requires feasibility-aware assignment (--assignment-mode sample_then_feasible_match).")
        if str(getattr(args, "home_depot_method", "none")) == "none":
            raise ValueError("--soc-mode carryover requires a fixed home depot (--home-depot-method != none): overnight stitching and idle charging anchor to it.")
        if not bool(getattr(args, "stream", True)):
            raise ValueError("--soc-mode carryover requires --stream true (the day loop is inherently streaming).")
        if int(getattr(args, "max_vehicle_days", 0)) > 0:
            raise ValueError("--max-vehicle-days is incompatible with --soc-mode carryover (state must thread through complete days); use --max-days.")

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
        # Carryover produces assignments inside the day loop (per-date
        # partitions); the upfront full-year assignment parquets do not exist.
        artifact_files = dict(RESUME_ARTIFACT_FILES)
        if soc_mode == "carryover":
            artifact_files.pop("assignments")
            artifact_files.pop("assignment_diagnostics")
        artifact_paths = {key: out_dir / name for key, name in artifact_files.items()}
        missing = [str(path) for path in artifact_paths.values() if not path.exists()]
        if missing:
            raise RuntimeError(f"--resume requires persisted upstream artifacts in --out-dir; missing: {missing}")
        block_templates_lsoa = pd.read_parquet(artifact_paths["block_templates_lsoa"])
        block_instances = pd.read_parquet(artifact_paths["block_instances"])
        depot_registry = pd.read_parquet(artifact_paths["depot_registry"])
        ev_specs = pd.read_parquet(artifact_paths["ev_specs"])
        if soc_mode == "carryover":
            assignments = None
            assignment_diagnostics = None
        else:
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

        if soc_mode == "carryover":
            # Carryover (plan v2 §8.2): assignment depends on each day's carried
            # SOC state, so it happens inside the chronological day loop in
            # _run_carryover_streaming_tail — nothing to precompute here.
            if home_depot_radius_km is None:
                raise ValueError("--soc-mode carryover requires home depots with a radius (--home-depot-method != none).")
            return _run_carryover_streaming_tail(
                args,
                out_dir=out_dir,
                start_iso=start_iso,
                end_iso=end_iso,
                preflight=preflight,
                block_templates_lsoa=block_templates_lsoa,
                block_instances=block_instances,
                depot_registry=depot_registry,
                ev_specs=ev_specs,
                home_depot_radius_km=home_depot_radius_km,
                block_sampling=block_sampling,
            )

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

    if soc_mode == "carryover":
        # Resume path: upstream artifacts loaded from disk; assignment happens in-loop.
        return _run_carryover_streaming_tail(
            args,
            out_dir=out_dir,
            start_iso=start_iso,
            end_iso=end_iso,
            preflight=preflight,
            block_templates_lsoa=block_templates_lsoa,
            block_instances=block_instances,
            depot_registry=depot_registry,
            ev_specs=ev_specs,
            home_depot_radius_km=float(getattr(args, "home_depot_radius_km", 10.0)),
            block_sampling=str(getattr(args, "block_sampling", "supply_weighted")),
        )

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


def _run_carryover_streaming_tail(
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
    home_depot_radius_km: float,
    block_sampling: str,
) -> dict[str, object]:
    """Chronological day loop with SOC carry-over (plan v2 §8) and lag-one finalize.

    Day D's overnight/idle windows end where day D+1's first events begin
    (§3.3 stitching), so day D's partitions are finalized and written at the
    START of iteration D+1; the per-spec ``vehicle_soc_state`` checkpoint is
    written LAST and doubles as the day's completion marker for --resume.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    resume = bool(getattr(args, "resume", False))
    depot_power_kw = float(args.depot_power_kw)
    overnight_end_hour = float(args.default_overnight_end_hour)
    warmup_days = max(0, int(getattr(args, "warmup_days", 14)))
    idle_policy = str(getattr(args, "idle_vehicle_charging_policy", "home_depot"))
    seed = int(args.seed)

    instances = merge_depot_coords(block_instances, depot_registry)
    instance_dates = instances["service_date"].astype(str)
    service_dates = sorted(date for date in instance_dates.unique() if start_iso <= date <= end_iso)
    if not service_dates:
        raise RuntimeError("carryover: no active service dates in the requested window.")
    warmup_set = set(service_dates[:warmup_days])
    counts_by_date = instance_dates.value_counts()
    median_active = float(counts_by_date.reindex(service_dates).fillna(0).median())
    decay_floor = 0.5 * median_active
    decay_suspect_dates = sorted(date for date in service_dates if float(counts_by_date.get(date, 0)) < decay_floor)

    context = build_matching_context(ev_specs, float(home_depot_radius_km))
    stats = _new_carryover_stream_stats()
    depot_load_frames: list[pd.DataFrame] = []
    depot_daily_frames: list[pd.DataFrame] = []

    if resume:
        if not (out_dir / SOC_STATE_DATASET_NAME).is_dir() and (out_dir / "vehicle_day_events").is_dir():
            raise RuntimeError(
                "--resume: --out-dir holds stream partitions but no vehicle_soc_state checkpoints; "
                "it looks like a daily_reset run tree, which cannot be resumed under --soc-mode carryover."
            )
        done_dates, redo_dates = _scan_resume_dates_carryover(out_dir, service_dates)
        _clear_partition_dirs_carryover(out_dir, redo_dates)
        print(
            f"[annual_depot] carryover resume: {len(done_dates):,} complete day(s), "
            f"{len(redo_dates):,} partial day(s) cleared, {len(service_dates) - len(done_dates):,} to run",
            flush=True,
        )
        if done_dates:
            state_frame = _read_partition(out_dir, SOC_STATE_DATASET_NAME, done_dates[-1])
            expected_specs = set(ev_specs["vehicle_spec_id"].astype(str))
            checkpoint_specs = set(state_frame["vehicle_spec_id"].astype(str))
            if checkpoint_specs != expected_specs:
                raise RuntimeError(
                    f"carryover resume: vehicle_soc_state checkpoint for {done_dates[-1]} covers "
                    f"{len(checkpoint_specs):,} specs, expected {len(expected_specs):,}; state is broken (§8.5)."
                )
            state = soc_state_from_frame(state_frame)
            _replay_completed_dates_carryover(out_dir, done_dates, stats, depot_load_frames, depot_daily_frames, warmup_set)
        else:
            state = initialize_soc_state(ev_specs, start_ts=pd.Timestamp(start_iso))
        remaining_dates = service_dates[len(done_dates) :]
    else:
        _prepare_carryover_output_dirs(out_dir)
        state = initialize_soc_state(ev_specs, start_ts=pd.Timestamp(start_iso))
        remaining_dates = service_dates

    print(
        f"[annual_depot] carryover stream: {len(remaining_dates):,} service_date(s), "
        f"warmup_days={warmup_days}, idle_policy={idle_policy}",
        flush=True,
    )
    prev_buffer: dict[str, Any] | None = None
    for day_index, service_date in enumerate(remaining_dates, start=1):
        print(f"[annual_depot] carryover day {day_index:,}/{len(remaining_dates):,}: {service_date}", flush=True)
        day_blocks = instances.loc[instance_dates.eq(service_date)].copy()

        # 1. Assign with state-projected screening + temporal guard (§8.2/§8.4).
        assignment_records, diagnostic_records, unmatched_records = assign_vehicle_days_for_date(
            day_blocks,
            context,
            service_date=service_date,
            seed=seed,
            scenario_mode=str(args.scenario_mode),
            sample_block_multiplier=float(getattr(args, "sample_block_multiplier", 1.0)),
            block_sampling=block_sampling,
            soc_mode="carryover",
            available_kwh_by_spec=project_available_kwh(state),
            available_from_by_spec=available_from_by_spec(state),
        )
        assignments, diagnostics, unmatched = assignment_frames_from_records(
            assignment_records, diagnostic_records, unmatched_records
        )

        # 2. Build the day's events from each spec's stitch seam (§3.3: no
        #    midnight pre window; pre opens at the previous pending's seam).
        stitch_start_map = {
            spec_id: (spec.pending.seam_end if spec.pending is not None else spec.last_event_end_ts)
            for spec_id, spec in state.items()
        }
        events = build_vehicle_day_events(
            assignments,
            day_blocks,
            block_templates_lsoa,
            ev_specs,
            depot_registry,
            depot_power_kw=depot_power_kw,
            default_overnight_end_hour=overnight_end_hour,
            use_trip_level_events=bool(args.use_trip_level_events),
            stitch_start_by_spec=stitch_start_map,
            inter_trip_relocation=bool(getattr(args, "inter_trip_relocation", False)),
            relocation_speed_kmh=float(getattr(args, "relocation_speed_kmh", 50.0)),
        )

        # 3. Stitch yesterday's pending windows against today's BUILT first
        #    events (exact, no estimate drift), then finalize & write yesterday.
        first_starts = first_event_start_by_spec(events)
        stitch_results = stitch_pendings(state, first_starts)
        if prev_buffer is not None:
            day_load, day_daily = _write_carryover_day(
                prev_buffer, stitch_results, state=state, out_dir=out_dir, depot_registry=depot_registry, start_iso=start_iso, stats=stats
            )
            depot_load_frames.append(day_load)
            depot_daily_frames.append(day_daily)

        # 4. SOC walk from the exact stitch SOC (missing state fatal, §8.5).
        soc_init = soc_init_by_vehicle_day(assignments, stitch_results)
        events, soc_summary = apply_depot_only_soc(
            events, depot_power_kw=depot_power_kw, soc_mode="carryover", soc_init_by_vehicle_day=soc_init
        )

        # 5. Advance state: every spec opens a new pending (duty overnight or idle).
        advance_state_after_walk(
            state,
            stitch_results,
            events,
            assignments,
            service_date=service_date,
            depot_power_kw=depot_power_kw,
            default_overnight_end_hour=overnight_end_hour,
            idle_vehicle_charging_policy=idle_policy,
        )
        prev_buffer = {
            "service_date": service_date,
            "events": events,
            "soc_summary": soc_summary,
            "assignments": assignments,
            "diagnostics": diagnostics,
            "unmatched": unmatched,
            "state_frame": soc_state_to_frame(state, service_date),
            "is_warmup": service_date in warmup_set,
        }
        gc.collect()

    if prev_buffer is not None:
        # End of range: every pending window ends at its natural seam.
        final_stitch = stitch_pendings(state, {})
        day_load, day_daily = _write_carryover_day(
            prev_buffer, final_stitch, state=state, out_dir=out_dir, depot_registry=depot_registry, start_iso=start_iso, stats=stats
        )
        depot_load_frames.append(day_load)
        depot_daily_frames.append(day_daily)

    depot_load, depot_daily = _combine_stream_load_outputs(depot_load_frames, depot_daily_frames, depot_registry)
    _assert_stream_energy_close(stats)
    depot_load.to_parquet(out_dir / "depot_load_15min.parquet", index=False)
    depot_daily.to_parquet(out_dir / "depot_daily_summary.parquet", index=False)
    _write_empty_stream_datasets_if_needed(out_dir, stats)

    # Combined assignment artifacts for parity with the daily_reset layout.
    assignments_all = _concat_partitions(out_dir, "vehicle_day_assignments", service_dates)
    diagnostics_all = _concat_partitions(out_dir, "vehicle_day_assignment_diagnostics", service_dates)
    unmatched_all = _concat_partitions(out_dir, "unmatched_sampled_blocks", service_dates)
    assignments_all.to_parquet(out_dir / "vehicle_day_assignments.parquet", index=False)
    diagnostics_all.to_parquet(out_dir / "vehicle_day_assignment_diagnostics.parquet", index=False)
    unmatched_all.to_parquet(out_dir / "unmatched_sampled_blocks.parquet", index=False)

    summary_stats = _finalize_carryover_stream_stats(stats)
    summary_stats.update(
        {
            "soc_day_boundary_hour": overnight_end_hour,
            "warmup_days": warmup_days,
            "warmup_start_date": service_dates[0] if warmup_set else "",
            "warmup_end_date": service_dates[: warmup_days][-1] if warmup_set else "",
            "idle_vehicle_charging_policy": idle_policy,
            "n_temporal_overlap_exclusions": int(
                pd.to_numeric(diagnostics_all.get("n_unmatched_vehicle_busy_overnight", pd.Series(dtype=float)), errors="coerce").fillna(0).sum()
            ),
            "calendar_decay_median_active_blocks": median_active,
            "calendar_decay_floor": decay_floor,
            "calendar_decay_suspect_dates": decay_suspect_dates,
        }
    )
    summary_md = build_run_summary_markdown(
        preflight_summary=preflight,
        block_templates=block_templates_lsoa,
        block_instances=block_instances,
        depot_registry=depot_registry,
        ev_bus_specs=ev_specs,
        vehicle_day_assignments=assignments_all,
        assignment_diagnostics=diagnostics_all,
        vehicle_day_soc_summary=pd.DataFrame(),
        depot_load_15min=depot_load,
        depot_daily_summary=depot_daily,
        feed_year_start=start_iso,
        feed_year_end=end_iso,
        scenario_mode=args.scenario_mode,
        preaggregated_stats=summary_stats,
        soc_mode="carryover",
    )
    write_run_summary(summary_md, out_dir)
    print(f"[annual_depot] wrote carryover stream outputs to {out_dir}", flush=True)
    return _result_dict(
        out_dir=out_dir,
        stream=True,
        block_templates_lsoa=block_templates_lsoa,
        block_instances=block_instances,
        depot_registry=depot_registry,
        assignments=assignments_all,
        depot_load=depot_load,
        artifact_counts=summary_stats,
    )


def _write_carryover_day(
    buffer: dict[str, Any],
    stitch_results: dict[str, Any],
    *,
    state: dict[str, Any],
    out_dir: Path,
    depot_registry: pd.DataFrame,
    start_iso: str,
    stats: dict[str, Any],
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Finalize one buffered day and write all its partitions (checkpoint last)."""
    service_date = str(buffer["service_date"])
    is_warmup = bool(buffer["is_warmup"])
    events, soc_summary, finalize_stats = finalize_day_frames(buffer["events"], buffer["soc_summary"], stitch_results, state)
    assignments = buffer["assignments"].copy()
    for frame in (events, soc_summary, assignments):
        frame["is_warmup"] = is_warmup

    bus_trip_records = build_bus_trip_records(events, feed_year_start=start_iso)
    bus_charging_events = build_bus_charging_event_records(events, feed_year_start=start_iso)
    bus_ev_state_records = build_bus_ev_state_records(events, feed_year_start=start_iso)

    depot_load, depot_daily = aggregate_depot_load_15min(events, depot_registry, soc_summary)
    if not depot_load_energy_matches_events(depot_load, events):
        raise RuntimeError(f"Daily depot load energy mismatch for service_date={service_date}.")
    depot_load["is_warmup"] = is_warmup
    depot_daily["is_warmup"] = is_warmup

    # Data tables: empty days simply have no partition (schema-less empty
    # parquet would poison whole-dataset reads with null-typed columns).
    _write_partitioned_by_service_date(events, out_dir / "vehicle_day_events")
    _write_partitioned_by_service_date(soc_summary, out_dir / "vehicle_day_soc_summary")
    _write_partitioned_by_service_date(bus_trip_records, out_dir / "bus_trip_records")
    _write_partitioned_by_service_date(bus_charging_events, out_dir / "bus_charging_events")
    _write_partitioned_by_service_date(bus_ev_state_records, out_dir / "bus_ev_state_records")
    _write_partitioned_by_service_date(assignments, out_dir / "vehicle_day_assignments")
    _write_partitioned_by_service_date(buffer["diagnostics"], out_dir / "vehicle_day_assignment_diagnostics")
    _write_partitioned_by_service_date(buffer["unmatched"], out_dir / "unmatched_sampled_blocks")
    _write_partitioned_by_service_date(depot_load, out_dir / "depot_load_15min")
    _write_partitioned_by_service_date(depot_daily, out_dir / "depot_daily_summary")
    # The state checkpoint (always one row per spec) is written LAST: because
    # every other write for the day strictly precedes it, its readability alone
    # marks the day complete for --resume.
    _write_partitioned_by_service_date(buffer["state_frame"], out_dir / SOC_STATE_DATASET_NAME)

    _add_carryover_stream_stats(
        stats,
        events,
        soc_summary,
        len(bus_trip_records),
        len(bus_charging_events),
        len(bus_ev_state_records),
        depot_load,
        is_warmup=is_warmup,
        finalize_stats=finalize_stats,
    )
    return depot_load, depot_daily


def _read_partition_or_empty(out_dir: Path, name: str, service_date: str) -> pd.DataFrame:
    if _partition_row_count(out_dir, name, service_date) is None:
        return pd.DataFrame()
    return _read_partition(out_dir, name, service_date)


def _carryover_required_datasets() -> list[str]:
    return STREAM_DATASET_NAMES + CARRYOVER_ASSIGNMENT_DATASET_NAMES + STREAM_LOAD_DATASET_NAMES + [SOC_STATE_DATASET_NAME]


def _prepare_carryover_output_dirs(out_dir: Path) -> None:
    for name in _carryover_required_datasets():
        _clear_output_path(out_dir / name)
        _clear_output_path(out_dir / f"{name}.parquet")


def _scan_resume_dates_carryover(out_dir: Path, service_dates: list[str]) -> tuple[list[str], list[str]]:
    """Carryover completeness is the CONTIGUOUS prefix of fully-written days.

    State threads linearly, so a hole invalidates everything after it (unlike
    daily_reset, which tolerates arbitrary holes). The vehicle_soc_state
    checkpoint is written strictly LAST within a day, so its readability alone
    marks the day complete; data tables may legitimately lack a partition on a
    zero-row day.
    """
    required = _carryover_required_datasets()
    done: list[str] = []
    for service_date in service_dates:
        if _partition_row_count(out_dir, SOC_STATE_DATASET_NAME, service_date) is not None:
            done.append(service_date)
        else:
            break
    redo = [
        service_date
        for service_date in service_dates[len(done) :]
        if any(_partition_file(out_dir, name, service_date).parent.exists() for name in required)
    ]
    if done:
        # The last complete day may have been finalized by the end-of-range
        # flush (pendings closed at their seam) instead of being stitched
        # against its successor. Redo it unconditionally: the redo is
        # deterministic, and its windows then stitch correctly against the
        # day that follows in this run.
        redo.insert(0, done.pop())
    return done, redo


def _clear_partition_dirs_carryover(out_dir: Path, service_dates: list[str]) -> None:
    for service_date in service_dates:
        for name in _carryover_required_datasets():
            _clear_output_path(_partition_file(out_dir, name, service_date).parent)


def _replay_completed_dates_carryover(
    out_dir: Path,
    done_dates: list[str],
    stats: dict[str, Any],
    depot_load_frames: list[pd.DataFrame],
    depot_daily_frames: list[pd.DataFrame],
    warmup_set: set[str],
) -> None:
    for index, service_date in enumerate(done_dates, start=1):
        print(f"[annual_depot] carryover resume replay {index:,}/{len(done_dates):,}: {service_date}", flush=True)
        events = _read_partition_or_empty(out_dir, "vehicle_day_events", service_date)
        soc_summary = _read_partition_or_empty(out_dir, "vehicle_day_soc_summary", service_date)
        depot_load = _read_partition_or_empty(out_dir, "depot_load_15min", service_date)
        depot_daily = _read_partition_or_empty(out_dir, "depot_daily_summary", service_date)
        if not depot_load_energy_matches_events(depot_load, events):
            raise RuntimeError(f"Carryover resume replay: depot load energy mismatch for service_date={service_date}.")
        _add_carryover_stream_stats(
            stats,
            events,
            soc_summary,
            _partition_row_count(out_dir, "bus_trip_records", service_date) or 0,
            _partition_row_count(out_dir, "bus_charging_events", service_date) or 0,
            _partition_row_count(out_dir, "bus_ev_state_records", service_date) or 0,
            depot_load,
            is_warmup=service_date in warmup_set,
            finalize_stats=None,
        )
        depot_load_frames.append(depot_load)
        depot_daily_frames.append(depot_daily)
        del events, soc_summary, depot_load, depot_daily
    if done_dates:
        gc.collect()


def _concat_partitions(out_dir: Path, name: str, service_dates: list[str]) -> pd.DataFrame:
    frames = [
        _read_partition(out_dir, name, service_date)
        for service_date in service_dates
        if _partition_row_count(out_dir, name, service_date) is not None
    ]
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True)


def _new_carryover_stream_stats() -> dict[str, Any]:
    stats = _new_stream_stats()
    stats.update(
        {
            "n_idle_charging_events": 0,
            "idle_charge_kwh": 0.0,
            "n_pre_block_windows_carryover": 0,
            "pre_block_charge_kwh": 0.0,
            "overnight_truncation_kwh": 0.0,
            "n_soc_rows_excl_warmup": 0,
            "n_soc_feasible_excl_warmup": 0,
            "total_energy_kwh_excl_warmup": 0.0,
            "total_charge_kwh_excl_warmup": 0.0,
        }
    )
    return stats


def _add_carryover_stream_stats(
    stats: dict[str, Any],
    events: pd.DataFrame,
    soc_summary: pd.DataFrame,
    n_bus_trip_records: int,
    n_bus_charging_events: int,
    n_bus_ev_state_records: int,
    depot_load: pd.DataFrame,
    *,
    is_warmup: bool,
    finalize_stats: dict[str, Any] | None = None,
) -> None:
    _add_stream_stats(stats, events, soc_summary, n_bus_trip_records, n_bus_charging_events, n_bus_ev_state_records, depot_load)
    if not events.empty and {"event_type", "charge_kwh_added"}.issubset(events.columns):
        idle_mask = events["event_type"].eq(IDLE_EVENT_TYPE)
        stats["n_idle_charging_events"] += int(idle_mask.sum())
        stats["idle_charge_kwh"] += float(pd.to_numeric(events.loc[idle_mask, "charge_kwh_added"], errors="coerce").fillna(0.0).sum())
        pre_mask = events["event_type"].eq("depot_parking_pre")
        stats["n_pre_block_windows_carryover"] += int(pre_mask.sum())
        stats["pre_block_charge_kwh"] += float(pd.to_numeric(events.loc[pre_mask, "charge_kwh_added"], errors="coerce").fillna(0.0).sum())
    if finalize_stats:
        stats["overnight_truncation_kwh"] += float(finalize_stats.get("overnight_truncation_kwh", 0.0))
    if not is_warmup:
        stats["total_energy_kwh_excl_warmup"] += _sum_numeric(soc_summary, "total_energy_kwh")
        stats["total_charge_kwh_excl_warmup"] += _sum_numeric(depot_load, "charge_kwh")
        if not soc_summary.empty and "depot_only_feasible" in soc_summary.columns:
            stats["n_soc_rows_excl_warmup"] += int(len(soc_summary))
            stats["n_soc_feasible_excl_warmup"] += int(soc_summary["depot_only_feasible"].astype(bool).sum())


def _finalize_carryover_stream_stats(stats: dict[str, Any]) -> dict[str, Any]:
    out = _finalize_stream_stats(stats)
    n_rows_excl = int(stats.get("n_soc_rows_excl_warmup", 0))
    out["matched_vehicle_day_feasible_share_excl_warmup"] = (
        float(stats["n_soc_feasible_excl_warmup"] / n_rows_excl) if n_rows_excl else float("nan")
    )
    out["n_matched_but_walk_infeasible"] = int(stats.get("n_soc_rows", 0)) - int(stats.get("n_soc_feasible", 0))
    load_charge = float(stats.get("load_charge_kwh", 0.0))
    out["idle_charge_share"] = float(stats.get("idle_charge_kwh", 0.0) / load_charge) if load_charge else float("nan")
    return out


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
        inter_trip_relocation=bool(getattr(args, "inter_trip_relocation", False)),
        relocation_speed_kmh=float(getattr(args, "relocation_speed_kmh", 50.0)),
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
