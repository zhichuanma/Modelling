"""Compare legacy and regenerated bus block parquet files."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from mobility.bus.distance import haversine_km
from mobility.bus.trip_chain_bus import DEADHEAD_MIN_DWELL_H, DEADHEAD_NOISE_KM, DEADHEAD_SPEED_KMH


OLD_PATH = Path("outputs/all_blocks.parquet.legacy.bak")
NEW_PATH = Path("outputs/all_blocks.parquet")
OUT_PATH = Path("outputs/inference_comparison.csv")


def _adjacent_pairs(df: pd.DataFrame) -> pd.DataFrame:
    ordered = df.sort_values(["block_id", "start_h", "end_h", "trip_id"]).copy()
    grouped = ordered.groupby("block_id", sort=False)
    pairs = ordered.loc[grouped.cumcount() > 0, ["block_id", "block_source", "start_h", "start_stop", "start_lat", "start_lon"]].copy()
    pairs["prev_end_h"] = grouped["end_h"].shift().loc[pairs.index].to_numpy(dtype=float)
    pairs["prev_end_stop"] = grouped["end_stop"].shift().loc[pairs.index].astype(object).to_numpy()
    pairs["prev_end_lat"] = grouped["end_lat"].shift().loc[pairs.index].to_numpy(dtype=float)
    pairs["prev_end_lon"] = grouped["end_lon"].shift().loc[pairs.index].to_numpy(dtype=float)
    valid = np.isfinite(pairs[["prev_end_lat", "prev_end_lon", "start_lat", "start_lon"]].to_numpy(dtype=float)).all(axis=1)
    pairs["deadhead_km"] = np.nan
    pairs.loc[valid, "deadhead_km"] = haversine_km(
        pairs.loc[valid, "prev_end_lat"].to_numpy(dtype=float),
        pairs.loc[valid, "prev_end_lon"].to_numpy(dtype=float),
        pairs.loc[valid, "start_lat"].to_numpy(dtype=float),
        pairs.loc[valid, "start_lon"].to_numpy(dtype=float),
    )
    pairs["gap_h"] = pairs["start_h"].astype(float) - pairs["prev_end_h"].astype(float)
    return pairs


def _metrics(label: str, df: pd.DataFrame) -> dict[str, float | str]:
    pairs = _adjacent_pairs(df)
    discontinuous = pairs["prev_end_stop"].astype(str) != pairs["start_stop"].astype(str)
    time_infeasible = (
        discontinuous
        & pairs["deadhead_km"].notna()
        & (pairs["deadhead_km"] >= DEADHEAD_NOISE_KM)
        & (pairs["gap_h"] < pairs["deadhead_km"] / DEADHEAD_SPEED_KMH + DEADHEAD_MIN_DWELL_H)
    )
    return {
        "version": label,
        "total_rows": int(len(df)),
        "block_count": int(df["block_id"].nunique()),
        "inferred_share_pct": float((df.groupby("block_id")["block_source"].first() == "inferred").mean() * 100.0),
        "inferred_time_infeasible_share_pct": float(
            time_infeasible[pairs["block_source"].eq("inferred")].mean() * 100.0
        ),
        "discontinuous_adjacent_pairs": int(discontinuous.sum()),
        "deadhead_injectable_pairs": int((discontinuous & pairs["deadhead_km"].ge(DEADHEAD_NOISE_KM)).sum()),
        "deadhead_skipped_time_pairs": int(time_infeasible.sum()),
    }


def _native_rows_unchanged(old: pd.DataFrame, new: pd.DataFrame) -> bool:
    old_native = old.loc[old["block_source"].eq("native")].sort_values(["trip_id", "start_h", "end_h"]).reset_index(drop=True)
    new_native = new.loc[new["block_source"].eq("native")].sort_values(["trip_id", "start_h", "end_h"]).reset_index(drop=True)
    if old_native.shape != new_native.shape or list(old_native.columns) != list(new_native.columns):
        return False
    for col in old_native.columns:
        if pd.api.types.is_numeric_dtype(old_native[col]) and pd.api.types.is_numeric_dtype(new_native[col]):
            if not np.allclose(
                old_native[col].to_numpy(dtype=float),
                new_native[col].to_numpy(dtype=float),
                rtol=0.0,
                atol=1e-9,
                equal_nan=True,
            ):
                return False
        elif not old_native[col].astype("string").fillna("<NA>").equals(new_native[col].astype("string").fillna("<NA>")):
            return False
    return True


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--legacy", type=Path, default=OLD_PATH)
    parser.add_argument("--current", type=Path, default=NEW_PATH)
    parser.add_argument("--output", type=Path, default=OUT_PATH)
    return parser


def main(argv: list[str] | None = None) -> None:
    args = _parser().parse_args(argv)
    if not args.legacy.exists() or not args.current.exists():
        missing = [str(path) for path in (args.legacy, args.current) if not path.exists()]
        raise SystemExit(f"Missing required parquet(s): {missing}")

    old = pd.read_parquet(args.legacy)
    new = pd.read_parquet(args.current)
    rows = [_metrics("legacy", old), _metrics("new", new)]

    native_unchanged = _native_rows_unchanged(old, new)
    for row in rows:
        row["native_rows_unchanged"] = bool(native_unchanged)

    args.output.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows).to_csv(args.output, index=False)
    print(f"Wrote {args.output}")


if __name__ == "__main__":
    main()
