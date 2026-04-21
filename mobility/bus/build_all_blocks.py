"""Nationwide block table — one unified pipeline over all UK GTFS agencies.

Pipeline
--------
  1. Load trips / routes / stops (small, fast)
  2. Stream stop_times.txt (65M rows) ONCE  → per-trip first/last stop + stop-based km
  3. Stream shapes.txt (47M rows)     ONCE  → per-shape total km
  4. Merge everything onto the trip table (distance_km, start/end coords + times)
  5. Per-agency block assignment:
        - trip has native block_id → keep it,       source = 'native'
        - trip has no  block_id    → run infer_blocks, source = 'inferred'
  6. Save Modelling/outputs/all_blocks.parquet

Expected runtime: ~20-30 min. Progress is printed per chunk.
"""

from __future__ import annotations

import time
from pathlib import Path

import numpy as np
import pandas as pd

from mobility.bus.gtfs_parser import (
    parse_gtfs_time, haversine_km, shape_length_km, infer_blocks,
)

HERE = Path(__file__).resolve().parent
GTFS = HERE.parents[2] / "Data" / "EV_behavior" / "Bus_Data" / "GTFS_timetable"
OUT = HERE.parents[1] / "outputs"
OUT.mkdir(exist_ok=True)

CHUNK_ROWS = 3_000_000


# ---------- streaming helpers ----------
def stream_trip_summary(path: Path, stops_coord: pd.DataFrame,
                        chunk_rows: int = CHUNK_ROWS) -> pd.DataFrame:
    """Single pass over stop_times.txt.

    Returns per-trip: start_stop, end_stop, start_time, end_time, stop_based_km.
    Handles chunk boundaries (trips split across chunks) correctly.
    """
    cols = ["trip_id", "stop_id", "stop_sequence", "arrival_time", "departure_time"]
    first_parts, last_parts, dist_parts = [], [], []
    t0 = time.time()
    rows_total = 0

    for i, chunk in enumerate(pd.read_csv(path, usecols=cols, chunksize=chunk_rows, low_memory=False), 1):
        ordered = chunk.sort_values(["trip_id", "stop_sequence"])
        # first/last stop within this chunk per trip
        first = ordered.drop_duplicates("trip_id", keep="first")[
            ["trip_id", "stop_id", "stop_sequence", "departure_time"]
        ]
        last = ordered.drop_duplicates("trip_id", keep="last")[
            ["trip_id", "stop_id", "stop_sequence", "arrival_time"]
        ]
        first_parts.append(first)
        last_parts.append(last)

        # stop-based segment distance
        merged = ordered.merge(stops_coord, left_on="stop_id", right_index=True, how="left")
        merged["lat2"] = merged.groupby("trip_id", sort=False)["stop_lat"].shift(-1)
        merged["lon2"] = merged.groupby("trip_id", sort=False)["stop_lon"].shift(-1)
        mask = merged["lat2"].notna() & merged["stop_lat"].notna()
        seg = np.zeros(len(merged), dtype=np.float32)
        if mask.any():
            seg[mask] = haversine_km(
                merged.loc[mask, "stop_lat"].values,
                merged.loc[mask, "stop_lon"].values,
                merged.loc[mask, "lat2"].values,
                merged.loc[mask, "lon2"].values,
            ).astype(np.float32)
        merged["seg_km"] = seg
        dist_parts.append(merged.groupby("trip_id", sort=False)["seg_km"].sum())

        rows_total += len(chunk)
        print(f"    chunk {i:>2}: rows={len(chunk):>10,}  cum={rows_total:>12,}  "
              f"elapsed={time.time()-t0:>5.0f}s", flush=True)

    # Cross-chunk combine
    firsts = pd.concat(first_parts, ignore_index=True)
    lasts = pd.concat(last_parts, ignore_index=True)
    firsts = firsts.sort_values(["trip_id", "stop_sequence"]).drop_duplicates("trip_id", keep="first")
    lasts = lasts.sort_values(["trip_id", "stop_sequence"]).drop_duplicates("trip_id", keep="last")
    dist = pd.concat(dist_parts).groupby(level=0).sum()

    spans = firsts.rename(columns={
        "stop_id": "start_stop", "stop_sequence": "start_seq", "departure_time": "start_time",
    })[["trip_id", "start_stop", "start_time"]]
    spans = spans.merge(
        lasts.rename(columns={
            "stop_id": "end_stop", "stop_sequence": "end_seq", "arrival_time": "end_time",
        })[["trip_id", "end_stop", "end_time"]],
        on="trip_id",
    )
    spans["stop_based_km"] = spans["trip_id"].map(dist).astype(np.float32)
    return spans


def stream_shape_lengths(path: Path, chunk_rows: int = CHUNK_ROWS) -> pd.Series:
    """Chunked compute of per-shape total length. Segments at chunk boundaries
    are lost (~1 segment per split shape), negligible for long polylines."""
    parts = []
    t0 = time.time()
    rows_total = 0
    for i, chunk in enumerate(pd.read_csv(
        path, usecols=["shape_id", "shape_pt_lat", "shape_pt_lon", "shape_pt_sequence"],
        chunksize=chunk_rows, low_memory=False), 1
    ):
        s = chunk.sort_values(["shape_id", "shape_pt_sequence"])
        s["lat2"] = s.groupby("shape_id", sort=False)["shape_pt_lat"].shift(-1)
        s["lon2"] = s.groupby("shape_id", sort=False)["shape_pt_lon"].shift(-1)
        mask = s["lat2"].notna()
        seg = np.zeros(len(s), dtype=np.float32)
        if mask.any():
            seg[mask] = haversine_km(
                s.loc[mask, "shape_pt_lat"].values,
                s.loc[mask, "shape_pt_lon"].values,
                s.loc[mask, "lat2"].values,
                s.loc[mask, "lon2"].values,
            ).astype(np.float32)
        s["seg_km"] = seg
        parts.append(s.groupby("shape_id", sort=False)["seg_km"].sum())
        rows_total += len(chunk)
        print(f"    chunk {i:>2}: rows={len(chunk):>10,}  cum={rows_total:>12,}  "
              f"elapsed={time.time()-t0:>5.0f}s", flush=True)
    return pd.concat(parts).groupby(level=0).sum()


# ---------- main ----------
def main():
    T = time.time()
    print("=== Building nationwide block table ===\n")

    # ---- Small tables
    print("  Loading trips / routes / stops / agency...")
    trips = pd.read_csv(GTFS / "trips.txt", low_memory=False)
    routes = pd.read_csv(GTFS / "routes.txt", usecols=["route_id", "agency_id"])
    stops = pd.read_csv(GTFS / "stops.txt", usecols=["stop_id", "stop_lat", "stop_lon"])
    stops_coord = stops.set_index("stop_id")[["stop_lat", "stop_lon"]]

    trips = trips.merge(routes, on="route_id")
    print(f"    trips: {len(trips):,}  agencies: {trips['agency_id'].nunique():,}")

    # ---- Stream stop_times
    print("\n  Streaming stop_times.txt (expect ~15 chunks, ~10 min)...")
    spans = stream_trip_summary(GTFS / "stop_times.txt", stops_coord)
    print(f"    got trip_spans for {len(spans):,} trips")

    # ---- Stream shapes
    print("\n  Streaming shapes.txt (expect ~10 chunks, ~5 min)...")
    shape_km = stream_shape_lengths(GTFS / "shapes.txt")
    print(f"    got shape lengths for {len(shape_km):,} shapes")

    # ---- Merge onto trips
    print("\n  Merging trip table...")
    df = trips.merge(spans, on="trip_id", how="left")
    df["shape_km"] = df["shape_id"].map(shape_km).astype(np.float32)
    df["distance_km"] = df["shape_km"].fillna(df["stop_based_km"]).astype(np.float32)

    df["start_h"] = df["start_time"].map(parse_gtfs_time).astype(np.float32)
    df["end_h"] = df["end_time"].map(parse_gtfs_time).astype(np.float32)

    df = df.merge(
        stops_coord.rename(columns={"stop_lat": "start_lat", "stop_lon": "start_lon"}),
        left_on="start_stop", right_index=True, how="left",
    )
    df = df.merge(
        stops_coord.rename(columns={"stop_lat": "end_lat", "stop_lon": "end_lon"}),
        left_on="end_stop", right_index=True, how="left",
    )

    usable = df.dropna(subset=["start_h", "end_h", "start_lat", "end_lat", "start_stop", "end_stop"])
    print(f"    usable trips (have time+coords): {len(usable):,} / {len(df):,}")

    # Checkpoint: the merged trip table before expensive inference.
    ckpt = OUT / "_trips_merged.ckpt.pkl"
    usable.to_pickle(ckpt)
    print(f"    checkpoint: {ckpt} ({ckpt.stat().st_size/1e6:.1f} MB)")

    # ---- Per-agency block assignment
    print("\n  Assigning blocks per agency...")
    has_native = usable["block_id"].notna()
    native = usable[has_native].copy()
    native["block_source"] = "native"
    # keep block_id as-is for native
    print(f"    native block_id: {len(native):,} trips across {native['block_id'].nunique():,} blocks")

    missing = usable[~has_native].copy()
    print(f"    need inference:  {len(missing):,} trips")

    # infer_blocks expects these columns
    need = ["trip_id", "agency_id", "service_id", "route_id", "start_h", "end_h",
            "start_lat", "start_lon", "end_lat", "end_lon", "start_stop", "end_stop"]
    t_inf = time.time()
    inferred_series = infer_blocks(
        missing[need],
        max_layover_h=1.0, max_deadhead_km=1.0,
        same_stop_bonus_h=1.0, route_continuity_bonus_h=0.5, max_shift_h=10.0,
    )
    print(f"    inference took {time.time()-t_inf:.0f}s")
    missing["block_id"] = inferred_series.values
    missing["block_source"] = "inferred"
    print(f"    inferred blocks: {missing['block_id'].nunique():,} "
          f"(avg {len(missing)/missing['block_id'].nunique():.1f} trips/block)")

    # ---- Combine + save
    all_blocks = pd.concat([native, missing], ignore_index=True)
    keep_cols = [
        "trip_id", "agency_id", "route_id", "service_id", "direction_id",
        "block_id", "block_source",
        "start_h", "end_h", "distance_km",
        "start_stop", "end_stop",
        "start_lat", "start_lon", "end_lat", "end_lon",
        "shape_id",
    ]
    all_blocks = all_blocks[keep_cols].sort_values(["agency_id", "block_id", "start_h"]).reset_index(drop=True)

    # Save pickle first (cheap + guaranteed to work) so inference result is safe
    ckpt2 = OUT / "_all_blocks.ckpt.pkl"
    all_blocks.to_pickle(ckpt2)
    print(f"\n  checkpoint: {ckpt2} ({ckpt2.stat().st_size/1e6:.1f} MB)")

    # Then try parquet, fall back to csv.gz
    try:
        out_path = OUT / "all_blocks.parquet"
        all_blocks.to_parquet(out_path, index=False)
        print(f"  Saved: {out_path}  ({out_path.stat().st_size/1e6:.1f} MB, {len(all_blocks):,} rows)")
    except ImportError as e:
        print(f"  parquet unavailable ({e}) — falling back to csv.gz")
        out_path = OUT / "all_blocks.csv.gz"
        all_blocks.to_csv(out_path, index=False, compression="gzip")
        print(f"  Saved: {out_path}  ({out_path.stat().st_size/1e6:.1f} MB, {len(all_blocks):,} rows)")

    # ---- Summary
    print("\n=== Summary ===")
    print(f"  Total trips:        {len(all_blocks):,}")
    print(f"  Total blocks:       {all_blocks['block_id'].nunique():,}")
    for src, g in all_blocks.groupby("block_source"):
        print(f"    {src:<10} trips={len(g):>10,}   blocks={g['block_id'].nunique():>8,}   "
              f"avg_trips/block={len(g)/g['block_id'].nunique():.1f}")

    bs = all_blocks.groupby("block_id").agg(
        n_trips=("trip_id", "count"),
        total_km=("distance_km", "sum"),
        span_h=("end_h", "max"),
    )
    bs["span_h"] = bs["span_h"] - all_blocks.groupby("block_id")["start_h"].min()
    print(f"\n  Per-block:")
    print(f"    trips:   mean {bs['n_trips'].mean():.1f}  median {bs['n_trips'].median():.0f}  max {bs['n_trips'].max()}")
    print(f"    km:      mean {bs['total_km'].mean():.1f}  median {bs['total_km'].median():.1f}  max {bs['total_km'].max():.0f}")
    print(f"    span_h:  mean {bs['span_h'].mean():.1f}  median {bs['span_h'].median():.1f}  max {bs['span_h'].max():.1f}")

    print(f"\n  Total wall time: {time.time()-T:.0f}s")


if __name__ == "__main__":
    main()
