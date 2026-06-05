# Run the annual depot-only COACH charging load pipeline (bus methodology port).
#
# Stage 0 swaps the GTFS upstream for TxC journeys + first-fit chains
# (mobility/coach/coach_block_templates.py adapts them to the bus schemas);
# everything downstream — feasibility matching, per-vehicle ledger stitching,
# SOC carry-over, depot_load_15min, resume checkpoints — REUSES the bus runner
# verbatim (imported, not forked). See plan 2026-06-05 + plan v2 PR 2.
#
# Honest labels carried throughout: coach chains are first-fit constructs, not
# real operator rosters; inter-journey relocation energy is explicit (default
# ON for coach); passenger_distance_km on coach instances is energy-relevant
# on-vehicle km (journeys + relocations) with the honest decomposition kept in
# coach_passenger_km / relocation_km_total.

from __future__ import annotations

import argparse
import datetime as dt
import importlib.util
import sys
from pathlib import Path

import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from mobility.bus.annual_block_instances import coerce_date  # noqa: E402
from mobility.bus.annual_depot_registry import attach_depots_to_instances, build_operational_depot_registry  # noqa: E402
from mobility.bus.annual_home_depot import assign_home_depots, build_depot_supply_demand  # noqa: E402
from mobility.bus.annual_lsoa_region import attach_lsoa_and_region  # noqa: E402
from mobility.bus.calendar import FEED_YEAR_END as BUS_FEED_YEAR_END  # noqa: E402
from mobility.bus.calendar import FEED_YEAR_START as BUS_FEED_YEAR_START  # noqa: E402
from mobility.coach.calendar import COACH_FEED_YEAR_END, COACH_FEED_YEAR_START, build_journey_date_index  # noqa: E402
from mobility.coach.chain_builder import build_coach_chains  # noqa: E402
from mobility.coach.coach_block_templates import (  # noqa: E402
    attach_journey_endpoints,
    build_coach_block_templates,
    expand_coach_block_instances,
)
from mobility.coach.coach_depot_preflight import run_coach_preflight, write_coach_preflight_summary  # noqa: E402
from mobility.coach.coach_ev_specs import build_ev_coach_specs, coach_ev_specs_summary  # noqa: E402
from mobility.coach.data_loader import (  # noqa: E402
    DEFAULT_COACH_ROOT,
    DEFAULT_INVENTORY_PATH,
    DEFAULT_JOURNEYS_PATH,
    DEFAULT_STOP_SEQUENCES_PATH,
    load_all_coach_journeys,
    load_all_coach_stop_sequences,
    write_all_coach_tables,
)
from mobility.coach.stop_geometry import attach_lsoa_to_journeys  # noqa: E402
from mobility.core.spatial import DEFAULT_ONSPD_PATH  # noqa: E402


def _load_bus_runner():
    spec = importlib.util.spec_from_file_location("run_bus_annual_depot_load", REPO_ROOT / "scripts" / "run_bus_annual_depot_load.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("run_bus_annual_depot_load", module)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


busrun = _load_bus_runner()

DEFAULT_OUT_DIR = Path("~/Work/Nature_EV_2025/outputs/coach_annual_depot_load")
DEFAULT_EV_INVENTORY = REPO_ROOT / "data" / "EV_UK_LSOA_2025_with_energy.csv"
DEFAULT_START = max(coerce_date(BUS_FEED_YEAR_START), coerce_date(COACH_FEED_YEAR_START))
DEFAULT_END = min(coerce_date(BUS_FEED_YEAR_END), coerce_date(COACH_FEED_YEAR_END))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run the annual depot-only coach charging load pipeline (bus methodology port).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--coach-root", type=Path, default=DEFAULT_COACH_ROOT)
    parser.add_argument("--txc-inventory", type=Path, default=DEFAULT_INVENTORY_PATH)
    parser.add_argument("--journeys-parquet", type=Path, default=DEFAULT_JOURNEYS_PATH)
    parser.add_argument("--stop-sequences-parquet", type=Path, default=DEFAULT_STOP_SEQUENCES_PATH)
    parser.add_argument("--rebuild-journeys", type=busrun._parse_bool, default=False, help="Re-parse the TxC XML tree even if the journeys parquet exists.")
    parser.add_argument("--ev-inventory", type=Path, default=DEFAULT_EV_INVENTORY)
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    parser.add_argument("--scenario-mode", default="ev_stock_scale")
    parser.add_argument("--charging-mode", default="depot_only")
    parser.add_argument("--assignment-mode", default="sample_then_feasible_match", choices=["sample_then_feasible_match"])
    parser.add_argument("--sample-block-multiplier", type=float, default=1.0, help="Sampled chains per day = ceil(201 * multiplier); recalibrate from preflight chains/day.")
    parser.add_argument("--home-depot-method", default="service_supply_weighted", choices=["service_supply_weighted", "source_lsoa_nearest", "none"])
    parser.add_argument("--home-depot-supply-weight", default="block_instances", choices=["block_instances", "bus_km"])
    parser.add_argument("--home-depot-radius-km", type=float, default=25.0, help="Coach bases are sparse; default wider than bus (sensitivity {10,25,50}).")
    parser.add_argument("--block-sampling", default="supply_weighted", choices=["supply_weighted", "uniform"])
    parser.add_argument("--depot-power-kw", type=float, default=100.0, help="Depot-side power; vehicle-side cap comes from --charge-side (DC 150 default).")
    parser.add_argument("--charge-side", default="dc", choices=["dc", "ac"], help="Vehicle-side charging cap channel: dc=150 kW (default), ac=22 kW conservative sensitivity.")
    parser.add_argument("--max-relocation-km", type=float, default=50.0)
    parser.add_argument("--transit-buffer-h", type=float, default=0.5)
    parser.add_argument("--inter-trip-relocation", type=busrun._parse_bool, default=True, help="Explicit inter-journey repositioning energy (coach default ON).")
    parser.add_argument("--relocation-speed-kmh", type=float, default=50.0)
    parser.add_argument("--default-overnight-end-hour", type=float, default=6.0)
    parser.add_argument("--seed", type=int, default=20260603)
    parser.add_argument("--start-date", default=DEFAULT_START.isoformat())
    parser.add_argument("--end-date", default=DEFAULT_END.isoformat())
    parser.add_argument("--max-days", type=int, default=0)
    parser.add_argument("--max-vehicle-days", type=int, default=0)
    parser.add_argument("--stream", type=busrun._parse_bool, default=True)
    parser.add_argument("--resume", type=busrun._parse_bool, default=False)
    parser.add_argument("--date-chunk-size", type=int, default=1)
    parser.add_argument("--use-trip-level-events", type=busrun._parse_bool, default=True)
    parser.add_argument("--soc-mode", default="carryover", choices=["daily_reset", "carryover"], help="Coach default: carryover (the methodology port target).")
    parser.add_argument("--warmup-days", type=int, default=21, help="Coach chains are sparse; >=21 recommended (prompt 09 fix-2).")
    parser.add_argument("--idle-vehicle-charging-policy", default="home_depot", choices=["home_depot", "none"])
    return parser.parse_args()


def run_pipeline(args: argparse.Namespace) -> dict[str, object]:
    if args.charging_mode != "depot_only":
        raise ValueError("Only --charging-mode depot_only is supported.")
    if args.scenario_mode != "ev_stock_scale":
        raise NotImplementedError("Only --scenario-mode ev_stock_scale is implemented.")
    soc_mode = str(getattr(args, "soc_mode", "carryover"))
    if soc_mode == "carryover":
        if str(args.home_depot_method) == "none":
            raise ValueError("--soc-mode carryover requires a fixed home depot (--home-depot-method != none).")
        if not bool(args.stream):
            raise ValueError("--soc-mode carryover requires --stream true.")
        if int(args.max_vehicle_days) > 0:
            raise ValueError("--max-vehicle-days is incompatible with --soc-mode carryover; use --max-days.")
    resume = bool(args.resume)

    out_dir = busrun._resolve_path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    start_date = coerce_date(args.start_date)
    end_date = coerce_date(args.end_date)
    if int(args.max_days) > 0:
        end_date = min(end_date, start_date + dt.timedelta(days=int(args.max_days) - 1))
    start_iso, end_iso = start_date.isoformat(), end_date.isoformat()

    if resume:
        print("[coach_depot] resume: loading persisted upstream artifacts", flush=True)
        names = {
            "block_templates_lsoa": "block_templates_lsoa.parquet",
            "block_instances": "block_instances_annual.parquet",
            "depot_registry": "depot_registry.parquet",
            "ev_specs": "ev_bus_specs.parquet",
        }
        paths = {key: out_dir / name for key, name in names.items()}
        missing = [str(path) for path in paths.values() if not path.exists()]
        if missing:
            raise RuntimeError(f"--resume requires persisted upstream artifacts in --out-dir; missing: {missing}")
        import json

        preflight_path = out_dir / "coach_preflight_summary.json"
        preflight = json.loads(preflight_path.read_text(encoding="utf-8")) if preflight_path.exists() else {"n_trip_rows": 0}
        block_templates_lsoa = pd.read_parquet(paths["block_templates_lsoa"])
        block_instances = pd.read_parquet(paths["block_instances"])
        depot_registry = pd.read_parquet(paths["depot_registry"])
        ev_specs = pd.read_parquet(paths["ev_specs"])
        return _dispatch(args, out_dir, start_iso, end_iso, preflight, block_templates_lsoa, block_instances, depot_registry, ev_specs, soc_mode)

    coach_root = busrun._resolve_path(args.coach_root)
    inventory_path = busrun._resolve_path(args.txc_inventory)
    journeys_path = busrun._resolve_path(args.journeys_parquet)
    stops_path = busrun._resolve_path(args.stop_sequences_parquet)

    if bool(args.rebuild_journeys) or not journeys_path.exists():
        print(f"[coach_depot] building coach journeys from TxC tree: {coach_root}", flush=True)
        journeys, stop_sequences = write_all_coach_tables(
            journeys_path, stops_path, inventory_path=inventory_path, coach_root=coach_root
        )
    else:
        print(f"[coach_depot] reading journeys: {journeys_path}", flush=True)
        journeys = load_all_coach_journeys(journeys_path)
        stop_sequences = load_all_coach_stop_sequences(stops_path) if stops_path.exists() else pd.DataFrame()

    print("[coach_depot] attaching endpoints + LSOA", flush=True)
    journeys = attach_journey_endpoints(journeys, stop_sequences)
    distance = pd.to_numeric(journeys.get("distance_km"), errors="coerce")
    n_no_distance = int(distance.isna().sum())
    journeys = journeys.loc[distance.notna()].copy()
    if not {"start_lsoa", "end_lsoa"}.issubset(journeys.columns):
        try:
            journeys = attach_lsoa_to_journeys(journeys)
        except Exception as exc:  # noqa: BLE001 - LSOA attach is attribution, depot inference degrades without it
            print(f"[coach_depot] WARNING: journey LSOA attach failed: {exc}", flush=True)

    print("[coach_depot] building journey date index (TxC operating profiles)", flush=True)
    date_index = build_journey_date_index(journeys, coach_root)
    print("[coach_depot] building first-fit chains", flush=True)
    chains_long = build_coach_chains(
        journeys, date_index, transit_buffer_h=float(args.transit_buffer_h), max_relocation_km=float(args.max_relocation_km)
    )

    print("[coach_depot] building coach EV specs", flush=True)
    ev_inventory = pd.read_csv(busrun._resolve_path(args.ev_inventory), low_memory=False)
    ev_specs, ev_diag = build_ev_coach_specs(ev_inventory, charge_side=str(args.charge_side))
    specs_summary = coach_ev_specs_summary(ev_inventory, ev_specs, ev_diag)

    print("[coach_depot] preflight", flush=True)
    preflight, problems = run_coach_preflight(
        coach_root=coach_root,
        inventory_path=inventory_path,
        journeys=journeys,
        date_index=date_index,
        chains_long=chains_long,
        coach_specs=ev_specs,
        coach_specs_summary=specs_summary,
        max_relocation_km=float(args.max_relocation_km),
        lsoa_attach_available=Path(DEFAULT_ONSPD_PATH).exists(),
    )
    preflight["n_trip_rows"] = int(len(journeys))
    preflight["n_journeys_dropped_no_distance"] = n_no_distance
    preflight["minibus_row_count"] = 0
    preflight["n_ev_specs_dropped_by_sanity"] = int(specs_summary.get("n_coach_specs_dropped_by_sanity", 0))
    write_coach_preflight_summary(preflight, out_dir)
    if problems:
        raise RuntimeError(f"Coach preflight failed; see {out_dir / 'coach_preflight_summary.md'}")

    print("[coach_depot] chains -> block templates", flush=True)
    block_templates, template_diag = build_coach_block_templates(journeys, chains_long)
    centroids = busrun._try_load_centroids()
    block_templates_lsoa, lsoa_diag = attach_lsoa_and_region(block_templates, centroids=centroids, max_distance_km=5.0)
    block_templates_lsoa.to_parquet(out_dir / "block_templates_lsoa.parquet", index=False)
    template_diag.to_parquet(out_dir / "coach_template_build_diagnostics.parquet", index=False)
    lsoa_diag.to_parquet(out_dir / "lsoa_region_diagnostics.parquet", index=False)
    chains_long.to_parquet(out_dir / "coach_chains_long.parquet", index=False)

    print("[coach_depot] expanding chain instances", flush=True)
    block_instances, calendar_diag = expand_coach_block_instances(block_templates_lsoa, chains_long, start_date=start_iso, end_date=end_iso)
    calendar_diag.to_parquet(out_dir / "calendar_expansion_diagnostics.parquet", index=False)

    print("[coach_depot] building depot registry", flush=True)
    depot_registry, depot_diag, block_depot = build_operational_depot_registry(block_templates_lsoa, block_instances, centroids=centroids)
    block_instances = attach_depots_to_instances(block_instances, block_depot)
    block_instances.to_parquet(out_dir / "block_instances_annual.parquet", index=False)
    depot_registry.to_parquet(out_dir / "depot_registry.parquet", index=False)
    depot_diag.to_parquet(out_dir / "depot_inference_diagnostics.parquet", index=False)

    if str(args.home_depot_method) != "none":
        print(f"[coach_depot] assigning home depots (method={args.home_depot_method})", flush=True)
        ev_specs, home_diag = assign_home_depots(
            ev_specs,
            depot_registry,
            centroids,
            method=str(args.home_depot_method),
            block_instances=block_instances,
            supply_weight=str(args.home_depot_supply_weight),
            seed=int(args.seed),
        )
        home_diag.to_parquet(out_dir / "home_depot_assignment_diagnostics.parquet", index=False)
        supply_demand = build_depot_supply_demand(ev_specs, block_instances, depot_registry)
        supply_demand.to_parquet(out_dir / "depot_supply_demand.parquet", index=False)
    ev_specs.to_parquet(out_dir / "ev_bus_specs.parquet", index=False)
    ev_diag.to_parquet(out_dir / "ev_bus_spec_diagnostics.parquet", index=False)

    return _dispatch(args, out_dir, start_iso, end_iso, preflight, block_templates_lsoa, block_instances, depot_registry, ev_specs, soc_mode)


def _dispatch(args, out_dir, start_iso, end_iso, preflight, block_templates_lsoa, block_instances, depot_registry, ev_specs, soc_mode):
    if soc_mode == "carryover":
        return busrun._run_carryover_streaming_tail(
            args,
            out_dir=out_dir,
            start_iso=start_iso,
            end_iso=end_iso,
            preflight=preflight,
            block_templates_lsoa=block_templates_lsoa,
            block_instances=block_instances,
            depot_registry=depot_registry,
            ev_specs=ev_specs,
            home_depot_radius_km=float(args.home_depot_radius_km),
            block_sampling=str(args.block_sampling),
        )
    # daily_reset: precompute assignments like the bus runner, then stream.
    from mobility.bus.annual_vehicle_day_assignment import build_feasible_vehicle_day_assignments

    radius = None if str(args.home_depot_method) == "none" else float(args.home_depot_radius_km)
    block_sampling = str(args.block_sampling)
    if radius is None and block_sampling == "supply_weighted":
        block_sampling = "uniform"
    assignments, assignment_diagnostics, unmatched = build_feasible_vehicle_day_assignments(
        block_instances,
        ev_specs,
        depot_registry,
        seed=int(args.seed),
        scenario_mode=str(args.scenario_mode),
        sample_block_multiplier=float(args.sample_block_multiplier),
        max_vehicle_days=int(args.max_vehicle_days) if int(args.max_vehicle_days) > 0 else None,
        home_depot_radius_km=radius,
        block_sampling=block_sampling,
    )
    assignments.to_parquet(out_dir / "vehicle_day_assignments.parquet", index=False)
    assignment_diagnostics.to_parquet(out_dir / "vehicle_day_assignment_diagnostics.parquet", index=False)
    unmatched.to_parquet(out_dir / "unmatched_sampled_blocks.parquet", index=False)
    return busrun._run_streaming_pipeline_tail(
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


def main() -> None:
    summary = run_pipeline(parse_args())
    for key, value in summary.items():
        print(f"{key}: {value}")


if __name__ == "__main__":
    main()
