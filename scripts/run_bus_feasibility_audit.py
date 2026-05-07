"""Run a bus fleet feasibility/deadhead audit outside the notebook."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from mobility.bus.sim_adapter import (
    DEFAULT_BATTERY_KWH,
    DEFAULT_CONSUMPTION_KWH_PER_KM,
    DEFAULT_DEPOT_CHARGE_KW,
    simulate_fleet_blocks,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--blocks", type=Path, default=Path("outputs/all_blocks.parquet"))
    parser.add_argument("--out", type=Path, default=Path("outputs/bus_feasibility_audit.parquet"))
    parser.add_argument("--sample-blocks", type=int, default=0, help="Deterministic block sample size; 0 means full fleet.")
    parser.add_argument("--seed", type=int, default=20260506)
    parser.add_argument("--battery-kwh", type=float, default=DEFAULT_BATTERY_KWH)
    parser.add_argument("--consumption-kwh-per-km", type=float, default=DEFAULT_CONSUMPTION_KWH_PER_KM)
    parser.add_argument("--depot-charge-kw", type=float, default=DEFAULT_DEPOT_CHARGE_KW)
    parser.add_argument("--allow-layover-charging", action="store_true")
    parser.add_argument("--layover-charge-kw", type=float, default=0.0)
    parser.add_argument("--min-layover-for-charging-h", type=float, default=0.0)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    blocks = pd.read_parquet(args.blocks)
    if args.sample_blocks > 0:
        block_ids = pd.Index(blocks["block_id"].drop_duplicates())
        rng = np.random.default_rng(args.seed)
        chosen = rng.choice(block_ids.to_numpy(dtype=object), size=min(args.sample_blocks, len(block_ids)), replace=False)
        blocks = blocks[blocks["block_id"].isin(chosen)].copy()

    per_block, _load_kw = simulate_fleet_blocks(
        blocks,
        battery_kwh=args.battery_kwh,
        consumption_kwh_per_km=args.consumption_kwh_per_km,
        depot_charge_kw=args.depot_charge_kw,
        allow_layover_charging=args.allow_layover_charging,
        layover_charge_kw=args.layover_charge_kw,
        min_layover_for_charging_h=args.min_layover_for_charging_h,
        progress_interval=1000,
    )
    args.out.parent.mkdir(parents=True, exist_ok=True)
    per_block.reset_index().to_parquet(args.out, index=False)
    print(f"Wrote {args.out} with {len(per_block):,} block rows")


if __name__ == "__main__":
    main()
