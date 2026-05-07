"""Canonical full-fleet annual bus simulation runner.

Reads ``outputs/all_blocks.parquet``, attaches LSOA codes, expands every block
across the GTFS feed-year using ``simulate_fleet_year``, and writes the
per-block + load-profile parquets. Supports a deterministic dry-run via
``--limit-blocks``.

Typical usage:

    # 1000-block dry-run
    python scripts/run_bus_annual.py \\
      --blocks outputs/all_blocks.parquet \\
      --warm-up-days 14 \\
      --limit-blocks 1000 \\
      --per-block-out outputs/bus_annual_per_block.dryrun.parquet \\
      --load-profile-out outputs/bus_annual_load_profile.dryrun.parquet \\
      --progress-interval 100

    # Full fleet
    python scripts/run_bus_annual.py \\
      --blocks outputs/all_blocks.parquet \\
      --warm-up-days 14 \\
      --per-block-out outputs/bus_annual_per_block.parquet \\
      --load-profile-out outputs/bus_annual_load_profile.parquet \\
      --progress-interval 1000
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from mobility.bus import (  # noqa: E402  (sys.path mutation above is intentional)
    FEED_YEAR_END,
    FEED_YEAR_START,
    attach_lsoa,
    build_service_date_index,
    load_all_blocks,
    load_bus_vehicle_params,
    load_service_calendar,
    simulate_fleet_year,
    write_annual_results,
)


DEFAULT_BLOCKS_PATH = REPO_ROOT / "outputs" / "all_blocks.parquet"
DEFAULT_PER_BLOCK_PATH = REPO_ROOT / "outputs" / "bus_annual_per_block.parquet"
DEFAULT_LOAD_PROFILE_PATH = REPO_ROOT / "outputs" / "bus_annual_load_profile.parquet"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--blocks", type=Path, default=DEFAULT_BLOCKS_PATH,
                        help="Path to all_blocks.parquet")
    parser.add_argument("--vehicle-params", type=Path, default=None,
                        help="Optional override for the bus vehicle params CSV")
    parser.add_argument("--warm-up-days", type=int, default=14,
                        help="Annual warm-up days; 0 disables warm-up (faster but biased first day).")
    parser.add_argument("--limit-blocks", type=int, default=0,
                        help="Cap fleet to first N blocks (deterministic block_id order). 0 means no cap.")
    parser.add_argument("--seed", type=int, default=42,
                        help="Vehicle sampling RNG seed.")
    parser.add_argument("--run-scope", default="full_fleet",
                        help="Tag stored on per-block output (e.g. 'full_fleet', 'dryrun_1000').")
    parser.add_argument("--per-block-out", type=Path, default=DEFAULT_PER_BLOCK_PATH,
                        help="Output parquet path for per-block annual metrics.")
    parser.add_argument("--load-profile-out", type=Path, default=DEFAULT_LOAD_PROFILE_PATH,
                        help="Output parquet path for fleet-aggregate load profile.")
    parser.add_argument("--progress-interval", type=int, default=1000,
                        help="Print progress every N blocks (0 to silence).")
    parser.add_argument("--allow-layover-charging", action="store_true",
                        help="Permit charging during layovers (requires --layover-charge-kw > 0).")
    parser.add_argument("--layover-charge-kw", type=float, default=0.0,
                        help="Charger power applied during eligible layovers.")
    parser.add_argument("--min-layover-for-charging-h", type=float, default=0.0,
                        help="Minimum layover duration in hours that qualifies for charging.")
    parser.add_argument("--soc-init", type=float, default=1.0,
                        help="Starting state-of-charge fraction.")
    parser.add_argument("--start-date", default=FEED_YEAR_START.isoformat(),
                        help="Feed-year window start (default: GTFS feed-year start).")
    parser.add_argument("--end-date", default=FEED_YEAR_END.isoformat(),
                        help="Feed-year window end (default: GTFS feed-year end).")
    return parser.parse_args()


def _attach_run_metadata(
    per_block: pd.DataFrame,
    *,
    args: argparse.Namespace,
) -> pd.DataFrame:
    """Add provenance columns to the per-block DataFrame.

    These mirror the legacy parquet's ``run_scope`` / ``run_seed`` columns so
    downstream consumers that filter by run can keep working without code
    changes.
    """
    if per_block.empty:
        return per_block
    out = per_block.copy()
    out["run_scope"] = str(args.run_scope)
    out["run_seed"] = int(args.seed)
    out["blocks_path"] = str(args.blocks)
    out["warm_up_days"] = int(args.warm_up_days)
    out["feed_year_start"] = str(args.start_date)
    out["feed_year_end"] = str(args.end_date)
    out["selection"] = "all" if args.limit_blocks <= 0 else f"first_{int(args.limit_blocks)}"
    return out


def _warn_if_overwriting(path: Path, label: str) -> None:
    if path.exists():
        print(f"  [warn] {label} already exists at {path}; will be overwritten.", flush=True)


def _summary(per_block: pd.DataFrame, load_kw: np.ndarray, *, elapsed_s: float) -> dict[str, Any]:
    if per_block.empty:
        return {
            "blocks": 0,
            "runtime_s": elapsed_s,
            "deadhead_total_km": 0.0,
            "deadhead_short_count": 0,
            "deadhead_long_count": 0,
            "deadhead_skipped_time_count": 0,
            "infeasible_share": 0.0,
            "simulation_error_count": 0,
            "infeasible_count": 0,
            "infeasibility_reason_breakdown": {},
            "blocks_with_overlap_warnings": 0,
            "total_overlap_warnings": 0,
            "block_source_breakdown": {},
            "infeasible_share_native": 0.0,
            "infeasible_share_inferred": 0.0,
            "load_profile_rows": int(load_kw.size),
        }
    native_mask = per_block["block_source"].eq("native")
    inferred_mask = per_block["block_source"].eq("inferred")
    simulation_errors = per_block["simulation_error"].fillna("").astype(str).ne("")
    return {
        "blocks": int(len(per_block)),
        "runtime_s": float(elapsed_s),
        "deadhead_total_km": float(per_block["deadhead_total_km"].sum()),
        "deadhead_short_count": int(per_block["deadhead_short_count"].sum()),
        "deadhead_long_count": int(per_block["deadhead_long_count"].sum()),
        "deadhead_skipped_time_count": int(per_block["deadhead_skipped_time_count"].sum()),
        "infeasible_share": float(per_block["infeasible"].mean()),
        "simulation_error_count": int(simulation_errors.sum()),
        "infeasible_count": int(per_block["infeasible"].sum()),
        "infeasibility_reason_breakdown": per_block["infeasibility_reason"].value_counts(dropna=False).to_dict(),
        "blocks_with_overlap_warnings": int((per_block["n_overlap_warnings"] > 0).sum()),
        "total_overlap_warnings": int(per_block["n_overlap_warnings"].sum()),
        "block_source_breakdown": per_block["block_source"].value_counts().to_dict(),
        "infeasible_share_native": float(per_block.loc[native_mask, "infeasible"].mean()) if native_mask.any() else 0.0,
        "infeasible_share_inferred": float(per_block.loc[inferred_mask, "infeasible"].mean()) if inferred_mask.any() else 0.0,
        "load_profile_rows": int(load_kw.size),
    }


def run_annual(args: argparse.Namespace) -> dict[str, Any]:
    """Programmatic entry point used by both CLI and tests."""
    t0 = time.time()
    print(f"[run_bus_annual] reading blocks from {args.blocks}", flush=True)
    all_blocks = attach_lsoa(load_all_blocks(args.blocks))

    if args.limit_blocks > 0:
        unique_ids = list(dict.fromkeys(all_blocks["block_id"].astype(str).tolist()))
        keep = set(unique_ids[: int(args.limit_blocks)])
        all_blocks = all_blocks[all_blocks["block_id"].astype(str).isin(keep)].copy()
        print(f"[run_bus_annual] limited to first {len(keep):,} blocks", flush=True)

    print(f"[run_bus_annual] loading service calendar / building service-date index", flush=True)
    service_calendar = load_service_calendar()
    service_date_index = build_service_date_index(
        all_blocks["service_id"].astype(str).unique(),
        args.start_date,
        args.end_date,
        service_calendar,
    )

    if args.vehicle_params is not None:
        vehicle_params = load_bus_vehicle_params(args.vehicle_params)
    else:
        vehicle_params = load_bus_vehicle_params()
    vehicle_rng = np.random.default_rng(int(args.seed))

    _warn_if_overwriting(args.per_block_out, "per-block output")
    _warn_if_overwriting(args.load_profile_out, "load-profile output")

    print(
        f"[run_bus_annual] simulating fleet "
        f"(warm_up_days={args.warm_up_days}, soc_init={args.soc_init}, "
        f"allow_layover_charging={args.allow_layover_charging})",
        flush=True,
    )
    per_block, fleet_load_kw = simulate_fleet_year(
        all_blocks,
        service_date_index,
        vehicle_params=vehicle_params,
        vehicle_rng=vehicle_rng,
        start_date=args.start_date,
        end_date=args.end_date,
        soc_init=float(args.soc_init),
        warm_up_days=int(args.warm_up_days),
        progress_interval=int(args.progress_interval),
        allow_layover_charging=bool(args.allow_layover_charging),
        layover_charge_kw=float(args.layover_charge_kw),
        min_layover_for_charging_h=float(args.min_layover_for_charging_h),
    )

    per_block = _attach_run_metadata(per_block, args=args)

    args.per_block_out.parent.mkdir(parents=True, exist_ok=True)
    args.load_profile_out.parent.mkdir(parents=True, exist_ok=True)
    write_annual_results(
        per_block,
        fleet_load_kw,
        start_date=args.start_date,
        end_date=args.end_date,
        per_block_path=args.per_block_out,
        load_profile_path=args.load_profile_out,
    )
    elapsed_s = time.time() - t0
    return _summary(per_block, fleet_load_kw, elapsed_s=elapsed_s) | {
        "per_block_path": str(args.per_block_out),
        "load_profile_path": str(args.load_profile_out),
    }


def main() -> None:
    args = parse_args()
    summary = run_annual(args)
    print()
    print("=== Summary ===")
    for key, value in summary.items():
        if isinstance(value, float):
            print(f"  {key:<28} {value:,.4f}")
        elif isinstance(value, int):
            print(f"  {key:<28} {value:,}")
        elif isinstance(value, dict):
            print(f"  {key:<28} {value!r}")
        else:
            print(f"  {key:<28} {value}")


if __name__ == "__main__":
    main()
