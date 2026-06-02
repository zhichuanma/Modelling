"""Slim merge for private-car shard outputs.

Loads only the tables required to drive the per-station daily web JSON export
(station_curve, station_counts, station_day_counts) and reuses the existing
merge/aggregation helpers. Skips private_car_trip_records.parquet and
private_car_charging_events.parquet to keep peak RAM under control on a
machine with ~250 GB but where loading every event/trip frame would push
past available memory.

station_curve is folded incrementally (concat + groupby across pairs) rather
than concatenating all 16 shards at once, so the working set stays close to
two shards' worth instead of all sixteen.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import pandas as pd

from mobility.cars.station_curves import (
    STEP_HOURS,
    _combine_count_frames,
    _combine_station_curves,
    build_station_summary_2025,
    export_analysis_files,
    load_existing_station_metadata,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=REPO_ROOT / "data")
    parser.add_argument(
        "--shard-root",
        type=Path,
        required=True,
        help="Directory containing shard_* subdirectories.",
    )
    parser.add_argument(
        "--shard-glob",
        default="shard_*",
        help="Glob used under --shard-root to find shard output directories.",
    )
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--year", type=int, default=2025)
    return parser.parse_args()


def _resolve_shard_dirs(shard_root: Path, glob: str) -> list[Path]:
    if not shard_root.exists():
        raise FileNotFoundError(shard_root)
    shard_dirs = sorted(p for p in shard_root.glob(glob) if p.is_dir())
    if not shard_dirs:
        raise FileNotFoundError(f"No shard dirs in {shard_root} matching {glob!r}")
    return shard_dirs


def _aggregate_curve_pair(left: pd.DataFrame, right: pd.DataFrame) -> pd.DataFrame:
    """Return left ⊕ right under the same group/agg rules as
    _combine_station_curves so it can be applied associatively."""

    if left.empty:
        base = right
    elif right.empty:
        base = left
    else:
        base = pd.concat([left, right], ignore_index=True, copy=False)
    if base.empty:
        return base
    agg_cols = {
        "energy_kwh": ("energy_kwh", "sum"),
        "active_vehicle_count": ("active_vehicle_count", "sum"),
        "charging_session_count": ("charging_session_count", "sum"),
    }
    grouped = (
        base.groupby(
            ["station_id", "time_bin_start", "time_bin_end"], as_index=False, sort=False
        )
        .agg(**agg_cols)
    )
    return grouped


def _finalize_curve(combined: pd.DataFrame) -> pd.DataFrame:
    if combined.empty:
        return _combine_station_curves([])
    combined = combined.sort_values(["station_id", "time_bin_start"]).reset_index(drop=True)
    combined["date"] = pd.to_datetime(combined["time_bin_start"]).dt.strftime("%Y-%m-%d")
    combined["avg_power_kw"] = combined["energy_kwh"] / STEP_HOURS
    return combined[
        [
            "station_id",
            "time_bin_start",
            "time_bin_end",
            "date",
            "energy_kwh",
            "avg_power_kw",
            "active_vehicle_count",
            "charging_session_count",
        ]
    ]


def main() -> None:
    args = parse_args()
    out = args.output_dir
    out.mkdir(parents=True, exist_ok=True)

    curve_name = f"station_charging_curve_15min_{args.year}.parquet"
    station_counts_name = f"station_counts_{args.year}.parquet"
    station_day_counts_name = f"station_day_counts_{args.year}.parquet"
    failed_name = f"failed_vehicles_{args.year}.csv"
    profile_name = f"profiling_log_{args.year}.csv"

    shard_dirs = _resolve_shard_dirs(args.shard_root, args.shard_glob)
    print(f"[slim-merge] resolved {len(shard_dirs)} shard dirs under {args.shard_root}")

    station_count_frames: list[pd.DataFrame] = []
    station_day_count_frames: list[pd.DataFrame] = []
    failed_frames: list[pd.DataFrame] = []
    profile_frames: list[pd.DataFrame] = []
    manifest_shards: list[dict] = []

    folded_curve: pd.DataFrame | None = None
    folded_energy_running = 0.0

    t_start = time.time()
    for idx, shard_dir in enumerate(shard_dirs):
        t_shard = time.time()
        curve_path = shard_dir / curve_name
        sc_path = shard_dir / station_counts_name
        sdc_path = shard_dir / station_day_counts_name
        for path in (curve_path, sc_path, sdc_path):
            if not path.exists():
                raise FileNotFoundError(f"Missing required shard output: {path}")

        shard_curve = pd.read_parquet(curve_path)
        shard_curve = shard_curve[
            [
                "station_id",
                "time_bin_start",
                "time_bin_end",
                "energy_kwh",
                "active_vehicle_count",
                "charging_session_count",
            ]
        ].copy()
        shard_curve_rows = int(len(shard_curve))
        shard_energy = float(shard_curve["energy_kwh"].sum())
        folded_energy_running += shard_energy

        sc_frame = pd.read_parquet(sc_path)
        sdc_frame = pd.read_parquet(sdc_path)
        station_count_frames.append(sc_frame)
        station_day_count_frames.append(sdc_frame)

        failed_path = shard_dir / failed_name
        if failed_path.exists():
            failed = pd.read_csv(failed_path)
            failed["source_shard_dir"] = str(shard_dir)
            failed_frames.append(failed)

        profile_path = shard_dir / profile_name
        if profile_path.exists():
            profile = pd.read_csv(profile_path)
            profile["source_shard_dir"] = str(shard_dir)
            profile_frames.append(profile)

        manifest_shards.append(
            {
                "shard_dir": str(shard_dir),
                "station_curve_rows": shard_curve_rows,
                "station_count_rows": int(len(sc_frame)),
                "station_day_count_rows": int(len(sdc_frame)),
                "energy_kwh": shard_energy,
            }
        )

        if folded_curve is None:
            folded_curve = _aggregate_curve_pair(pd.DataFrame(), shard_curve)
        else:
            folded_curve = _aggregate_curve_pair(folded_curve, shard_curve)
        del shard_curve

        print(
            f"[slim-merge] folded shard {idx + 1}/{len(shard_dirs)} "
            f"({shard_dir.name}): +{shard_curve_rows:,} rows, "
            f"folded rows={len(folded_curve):,}, "
            f"shard energy={shard_energy:,.1f} kWh, "
            f"shard secs={time.time() - t_shard:,.1f}, "
            f"elapsed={time.time() - t_start:,.1f}",
            flush=True,
        )

    print("[slim-merge] finalizing station_curve …", flush=True)
    station_curve = _finalize_curve(folded_curve if folded_curve is not None else pd.DataFrame())
    del folded_curve

    station_counts = _combine_count_frames(station_count_frames, ["station_id"])
    station_day_counts = _combine_count_frames(
        station_day_count_frames, ["station_id", "date"]
    )
    del station_count_frames
    del station_day_count_frames

    station_metadata = load_existing_station_metadata(args.data_dir)
    station_summary = build_station_summary_2025(
        station_curve, station_metadata, station_counts, year=args.year
    )

    print("[slim-merge] exporting analysis files …", flush=True)
    export_analysis_files(
        station_curve,
        station_summary,
        station_metadata,
        out,
        year=args.year,
        station_counts=station_counts,
        station_day_counts=station_day_counts,
    )

    if failed_frames:
        pd.concat(failed_frames, ignore_index=True).to_csv(out / failed_name, index=False)
    if profile_frames:
        pd.concat(profile_frames, ignore_index=True).to_csv(
            out / f"profiling_log_merged_{args.year}.csv",
            index=False,
        )

    manifest = {
        "schema_version": "1.0-slim",
        "year": args.year,
        "source_shard_count": len(shard_dirs),
        "source_shards": manifest_shards,
        "station_curve_rows": int(len(station_curve)),
        "station_summary_rows": int(len(station_summary)),
        "station_count_rows": int(len(station_counts)),
        "station_day_count_rows": int(len(station_day_counts)),
        "public_station_energy_kwh": float(station_curve["energy_kwh"].sum())
        if not station_curve.empty
        else 0.0,
        "source_shard_energy_kwh_sum": folded_energy_running,
        "notes": "trip_records and charging_events skipped; web-JSON path only.",
    }
    (out / f"merge_manifest_{args.year}.json").write_text(
        json.dumps(manifest, ensure_ascii=True, indent=2),
        encoding="utf-8",
    )

    print("\n=== Private-car slim merge outputs ===")
    print(f"output_dir: {out}")
    print(f"source_shards: {len(shard_dirs):,}")
    print(f"station_curve_rows: {len(station_curve):,}")
    print(f"station_summary_rows: {len(station_summary):,}")
    print(f"station_day_count_rows: {len(station_day_counts):,}")
    print(f"public_station_energy_kwh: {manifest['public_station_energy_kwh']:,.3f}")
    print(f"source_shard_energy_kwh_sum: {folded_energy_running:,.3f}")
    print(f"manifest: {out / f'merge_manifest_{args.year}.json'}")


if __name__ == "__main__":
    main()
