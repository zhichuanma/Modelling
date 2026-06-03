"""Run the annual depot-only bus charging load pipeline.

This runner is deliberately separate from ``scripts/run_bus_annual.py``. It
builds event-ledger outputs and depot-level 15-minute load curves without
public charging, OCM matching, opportunity charging, or M1 L0-L4 resolution.
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys
from pathlib import Path

import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from mobility.bus.annual_block_instances import coerce_date, expand_block_instances  # noqa: E402
from mobility.bus.annual_block_templates import build_block_templates  # noqa: E402
from mobility.bus.annual_depot_artifacts import (  # noqa: E402
    build_bus_charging_event_records,
    build_bus_ev_state_records,
    build_bus_trip_records,
)
from mobility.bus.annual_depot_events import build_vehicle_day_events  # noqa: E402
from mobility.bus.annual_depot_load import aggregate_depot_load_15min, depot_load_energy_matches_events  # noqa: E402
from mobility.bus.annual_depot_outputs import build_run_summary_markdown, write_run_summary  # noqa: E402
from mobility.bus.annual_depot_preflight import run_preflight, write_preflight_summary  # noqa: E402
from mobility.bus.annual_depot_registry import attach_depots_to_instances, build_operational_depot_registry  # noqa: E402
from mobility.bus.annual_depot_soc import apply_depot_only_soc  # noqa: E402
from mobility.bus.annual_ev_specs import build_ev_bus_specs  # noqa: E402
from mobility.bus.annual_lsoa_region import attach_lsoa_and_region  # noqa: E402
from mobility.bus.calendar import FEED_YEAR_END, FEED_YEAR_START, load_service_calendar  # noqa: E402
from mobility.core.spatial import DEFAULT_ONSPD_PATH, load_lsoa_centroids  # noqa: E402


DEFAULT_BLOCKS = REPO_ROOT / "outputs" / "all_blocks.parquet"
DEFAULT_EV_INVENTORY = REPO_ROOT / "data" / "EV_UK_LSOA_2025_with_energy.csv"
DEFAULT_OUT_DIR = REPO_ROOT / "outputs" / "bus_annual_depot_load"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--blocks", type=Path, default=DEFAULT_BLOCKS)
    parser.add_argument("--ev-inventory", type=Path, default=DEFAULT_EV_INVENTORY)
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    parser.add_argument("--scenario-mode", default="ev_stock_scale")
    parser.add_argument("--charging-mode", default="depot_only")
    parser.add_argument("--depot-power-kw", type=float, default=100.0)
    parser.add_argument("--default-overnight-end-hour", type=float, default=6.0)
    parser.add_argument("--seed", type=int, default=20260603)
    parser.add_argument("--start-date", default=FEED_YEAR_START.isoformat())
    parser.add_argument("--end-date", default=FEED_YEAR_END.isoformat())
    parser.add_argument("--max-days", type=int, default=0, help="Smoke/debug only; 0 means the full requested date range.")
    parser.add_argument("--max-vehicle-days", type=int, default=0, help="Smoke/debug only; 0 means no cap.")
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

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    start_date = coerce_date(args.start_date)
    end_date = coerce_date(args.end_date)
    if int(args.max_days) > 0:
        end_date = min(end_date, start_date + dt.timedelta(days=int(args.max_days) - 1))
    start_iso = start_date.isoformat()
    end_iso = end_date.isoformat()

    print(f"[annual_depot] reading blocks: {args.blocks}", flush=True)
    blocks = pd.read_parquet(args.blocks)
    print(f"[annual_depot] reading EV inventory: {args.ev_inventory}", flush=True)
    ev_inventory = pd.read_csv(args.ev_inventory)

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
    ev_specs.to_parquet(out_dir / "ev_bus_specs.parquet", index=False)
    ev_diag.to_parquet(out_dir / "ev_bus_spec_diagnostics.parquet", index=False)

    print("[annual_depot] assigning vehicle-days", flush=True)
    from mobility.bus.annual_vehicle_day_assignment import build_vehicle_day_assignments

    assignments = build_vehicle_day_assignments(
        block_instances,
        ev_specs,
        seed=int(args.seed),
        scenario_mode=args.scenario_mode,
        max_vehicle_days=int(args.max_vehicle_days) if int(args.max_vehicle_days) > 0 else None,
    )
    assignments.to_parquet(out_dir / "vehicle_day_assignments.parquet", index=False)
    _assignment_diagnostics(assignments).to_parquet(out_dir / "vehicle_day_assignment_diagnostics.parquet", index=False)

    print("[annual_depot] building event ledger", flush=True)
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

    print("[annual_depot] walking SOC", flush=True)
    events, soc_summary = apply_depot_only_soc(events, depot_power_kw=float(args.depot_power_kw))
    events.to_parquet(out_dir / "vehicle_day_events.parquet", index=False)
    soc_summary.to_parquet(out_dir / "vehicle_day_soc_summary.parquet", index=False)

    print("[annual_depot] building bus observability artifacts", flush=True)
    bus_trip_records = build_bus_trip_records(events, feed_year_start=start_iso)
    bus_charging_events = build_bus_charging_event_records(events, feed_year_start=start_iso)
    bus_ev_state_records = build_bus_ev_state_records(events, feed_year_start=start_iso)
    bus_trip_records.to_parquet(out_dir / "bus_trip_records.parquet", index=False)
    bus_charging_events.to_parquet(out_dir / "bus_charging_events.parquet", index=False)
    bus_ev_state_records.to_parquet(out_dir / "bus_ev_state_records.parquet", index=False)

    print("[annual_depot] aggregating depot load", flush=True)
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
    print(f"[annual_depot] wrote outputs to {out_dir}", flush=True)
    return {
        "out_dir": str(out_dir),
        "n_block_templates": len(block_templates),
        "n_block_instances": len(block_instances),
        "n_vehicle_day_assignments": len(assignments),
        "n_depots": len(depot_registry),
        "n_load_rows": len(depot_load),
        "n_bus_trip_records": len(bus_trip_records),
        "n_bus_charging_events": len(bus_charging_events),
        "n_bus_ev_state_records": len(bus_ev_state_records),
    }


def _try_load_centroids() -> pd.DataFrame | None:
    try:
        return load_lsoa_centroids()
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
