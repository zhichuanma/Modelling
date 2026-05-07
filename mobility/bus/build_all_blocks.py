"""Build the nationwide bus block parquet from the raw GTFS feed.

The module is intentionally self-contained: the old ``gtfs_parser`` helper was
removed, while the canonical rebuild still needs to call the current
``mobility.bus.block_inference.infer_blocks`` implementation.
"""

from __future__ import annotations

import argparse
import time
from pathlib import Path

import numpy as np
import pandas as pd

from .block_inference import BlockInferenceConfig, infer_blocks
from .distance import haversine_km


MODELLING_ROOT = Path(__file__).resolve().parents[2]
PROJECT_ROOT = MODELLING_ROOT.parent
DEFAULT_GTFS_DIR = PROJECT_ROOT / "Data" / "EV_behavior" / "Bus_Data" / "GTFS_timetable"
DEFAULT_OUT_DIR = MODELLING_ROOT / "outputs"
DEFAULT_OUTPUT = DEFAULT_OUT_DIR / "all_blocks.parquet"
CHUNK_ROWS = 3_000_000

OUTPUT_COLUMNS = [
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
]

INFERENCE_COLUMNS = [
    "trip_id",
    "agency_id",
    "service_id",
    "route_id",
    "start_h",
    "end_h",
    "start_lat",
    "start_lon",
    "end_lat",
    "end_lon",
    "start_stop",
    "end_stop",
]


def parse_gtfs_time(value) -> float:
    """Parse GTFS HH:MM:SS, allowing hours above 24."""
    if pd.isna(value):
        return float("nan")
    parts = str(value).split(":")
    if len(parts) != 3:
        return float("nan")
    try:
        hours, minutes, seconds = (float(part) for part in parts)
    except ValueError:
        return float("nan")
    return float(hours + minutes / 60.0 + seconds / 3600.0)


def _read_required_csv(path: Path, **kwargs) -> pd.DataFrame:
    if not path.exists():
        raise FileNotFoundError(f"Missing GTFS file: {path}")
    return pd.read_csv(path, **kwargs)


def _dedupe_first_last(parts: list[pd.DataFrame], keep: str) -> pd.DataFrame:
    if not parts:
        return pd.DataFrame()
    combined = pd.concat(parts, ignore_index=True)
    return (
        combined.sort_values(["trip_id", "stop_sequence"], kind="stable")
        .drop_duplicates("trip_id", keep=keep)
        .reset_index(drop=True)
    )


def stream_trip_summary(
    path: Path,
    stops_coord: pd.DataFrame,
    *,
    chunk_rows: int = CHUNK_ROWS,
    verbose: bool = True,
) -> pd.DataFrame:
    """Single-pass stop_times summary: first/last stop, times, and stop km."""
    cols = ["trip_id", "stop_id", "stop_sequence", "arrival_time", "departure_time"]
    first_parts: list[pd.DataFrame] = []
    last_parts: list[pd.DataFrame] = []
    dist_parts: list[pd.Series] = []
    t0 = time.time()
    rows_total = 0

    for index, chunk in enumerate(
        pd.read_csv(path, usecols=cols, chunksize=chunk_rows, low_memory=False, dtype={"trip_id": "string", "stop_id": "string"}),
        1,
    ):
        ordered = chunk.sort_values(["trip_id", "stop_sequence"], kind="stable")
        first_parts.append(
            ordered.drop_duplicates("trip_id", keep="first")[
                ["trip_id", "stop_id", "stop_sequence", "departure_time"]
            ]
        )
        last_parts.append(
            ordered.drop_duplicates("trip_id", keep="last")[
                ["trip_id", "stop_id", "stop_sequence", "arrival_time"]
            ]
        )

        merged = ordered.merge(stops_coord, left_on="stop_id", right_index=True, how="left")
        grouped = merged.groupby("trip_id", sort=False)
        merged["lat2"] = grouped["stop_lat"].shift(-1)
        merged["lon2"] = grouped["stop_lon"].shift(-1)
        valid = merged[["stop_lat", "stop_lon", "lat2", "lon2"]].notna().all(axis=1)
        seg = np.zeros(len(merged), dtype=np.float32)
        if valid.any():
            seg[valid.to_numpy()] = haversine_km(
                merged.loc[valid, "stop_lat"].to_numpy(dtype=float),
                merged.loc[valid, "stop_lon"].to_numpy(dtype=float),
                merged.loc[valid, "lat2"].to_numpy(dtype=float),
                merged.loc[valid, "lon2"].to_numpy(dtype=float),
            ).astype(np.float32)
        merged["seg_km"] = seg
        dist_parts.append(merged.groupby("trip_id", sort=False)["seg_km"].sum())

        rows_total += len(chunk)
        if verbose:
            elapsed = time.time() - t0
            print(f"    stop_times chunk {index:>2}: rows={len(chunk):>10,} cum={rows_total:>12,} elapsed={elapsed:>6.0f}s", flush=True)

    firsts = _dedupe_first_last(first_parts, "first")
    lasts = _dedupe_first_last(last_parts, "last")
    dist = pd.concat(dist_parts).groupby(level=0).sum() if dist_parts else pd.Series(dtype=np.float32)

    spans = firsts.rename(
        columns={"stop_id": "start_stop", "departure_time": "start_time"}
    )[["trip_id", "start_stop", "start_time"]]
    spans = spans.merge(
        lasts.rename(columns={"stop_id": "end_stop", "arrival_time": "end_time"})[
            ["trip_id", "end_stop", "end_time"]
        ],
        on="trip_id",
        how="inner",
    )
    spans["stop_based_km"] = spans["trip_id"].map(dist).astype(np.float32)
    return spans


def stream_shape_lengths(
    path: Path,
    *,
    chunk_rows: int = CHUNK_ROWS,
    verbose: bool = True,
) -> pd.Series:
    """Single-pass per-shape length calculation."""
    cols = ["shape_id", "shape_pt_lat", "shape_pt_lon", "shape_pt_sequence"]
    parts: list[pd.Series] = []
    t0 = time.time()
    rows_total = 0

    for index, chunk in enumerate(
        pd.read_csv(path, usecols=cols, chunksize=chunk_rows, low_memory=False, dtype={"shape_id": "string"}),
        1,
    ):
        ordered = chunk.sort_values(["shape_id", "shape_pt_sequence"], kind="stable")
        grouped = ordered.groupby("shape_id", sort=False)
        distance_rows = ordered.copy()
        distance_rows["lat2"] = grouped["shape_pt_lat"].shift(-1)
        distance_rows["lon2"] = grouped["shape_pt_lon"].shift(-1)
        valid = distance_rows[["shape_pt_lat", "shape_pt_lon", "lat2", "lon2"]].notna().all(axis=1)
        seg = np.zeros(len(distance_rows), dtype=np.float32)
        if valid.any():
            seg[valid.to_numpy()] = haversine_km(
                distance_rows.loc[valid, "shape_pt_lat"].to_numpy(dtype=float),
                distance_rows.loc[valid, "shape_pt_lon"].to_numpy(dtype=float),
                distance_rows.loc[valid, "lat2"].to_numpy(dtype=float),
                distance_rows.loc[valid, "lon2"].to_numpy(dtype=float),
            ).astype(np.float32)
        distance_rows["seg_km"] = seg
        parts.append(distance_rows.groupby("shape_id", sort=False)["seg_km"].sum())

        rows_total += len(chunk)
        if verbose:
            elapsed = time.time() - t0
            print(f"    shapes chunk     {index:>2}: rows={len(chunk):>10,} cum={rows_total:>12,} elapsed={elapsed:>6.0f}s", flush=True)

    return pd.concat(parts).groupby(level=0).sum() if parts else pd.Series(dtype=np.float32)


def _load_small_tables(gtfs_dir: Path) -> tuple[pd.DataFrame, pd.DataFrame]:
    trips = _read_required_csv(gtfs_dir / "trips.txt", low_memory=False, dtype={"trip_id": "string", "block_id": "string", "shape_id": "string"})
    routes = _read_required_csv(gtfs_dir / "routes.txt", usecols=["route_id", "agency_id"], dtype={"agency_id": "string"})
    stops = _read_required_csv(gtfs_dir / "stops.txt", usecols=["stop_id", "stop_lat", "stop_lon"], dtype={"stop_id": "string"})
    stops_coord = stops.set_index("stop_id")[["stop_lat", "stop_lon"]]
    return trips.merge(routes, on="route_id", how="left"), stops_coord


def build_all_blocks(
    *,
    gtfs_dir: Path = DEFAULT_GTFS_DIR,
    output: Path | None = DEFAULT_OUTPUT,
    out_dir: Path = DEFAULT_OUT_DIR,
    chunk_rows: int = CHUNK_ROWS,
    config: BlockInferenceConfig | None = None,
    write_checkpoints: bool = True,
    verbose: bool = True,
) -> pd.DataFrame:
    """Build all bus blocks and optionally write the canonical parquet."""
    gtfs_dir = Path(gtfs_dir)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    cfg = config or BlockInferenceConfig()
    start = time.time()

    if verbose:
        print("=== Building nationwide bus block table ===")
        print(f"GTFS: {gtfs_dir}")
        print(f"Inference config: {cfg}")

    trips, stops_coord = _load_small_tables(gtfs_dir)
    if verbose:
        print(f"  trips={len(trips):,} agencies={trips['agency_id'].nunique():,}")

    spans = stream_trip_summary(gtfs_dir / "stop_times.txt", stops_coord, chunk_rows=chunk_rows, verbose=verbose)
    shape_km = stream_shape_lengths(gtfs_dir / "shapes.txt", chunk_rows=chunk_rows, verbose=verbose)

    data = trips.merge(spans, on="trip_id", how="left")
    data["shape_km"] = data["shape_id"].map(shape_km).astype(np.float32)
    data["distance_km"] = data["shape_km"].fillna(data["stop_based_km"]).astype(np.float32)
    data["start_h"] = data["start_time"].map(parse_gtfs_time).astype(np.float32)
    data["end_h"] = data["end_time"].map(parse_gtfs_time).astype(np.float32)
    data = data.merge(
        stops_coord.rename(columns={"stop_lat": "start_lat", "stop_lon": "start_lon"}),
        left_on="start_stop",
        right_index=True,
        how="left",
    )
    data = data.merge(
        stops_coord.rename(columns={"stop_lat": "end_lat", "stop_lon": "end_lon"}),
        left_on="end_stop",
        right_index=True,
        how="left",
    )

    usable = data.dropna(
        subset=["start_h", "end_h", "start_lat", "start_lon", "end_lat", "end_lon", "start_stop", "end_stop", "distance_km"]
    ).copy()
    positive_duration = usable["end_h"].astype(float) > usable["start_h"].astype(float)
    dropped_non_positive = int((~positive_duration).sum())
    if dropped_non_positive:
        usable = usable.loc[positive_duration].copy()
    if verbose:
        print(f"  usable trips={len(usable):,} / {len(data):,}")
        if dropped_non_positive:
            print(f"  dropped non-positive-duration trips={dropped_non_positive:,}")

    if write_checkpoints:
        trip_ckpt = out_dir / "_trips_merged.ckpt.pkl"
        usable.to_pickle(trip_ckpt)
        if verbose:
            print(f"  checkpoint: {trip_ckpt} ({trip_ckpt.stat().st_size / 1e6:.1f} MB)")

    block_id_text = usable["block_id"].astype("string").str.strip()
    has_native = usable["block_id"].notna() & block_id_text.ne("")
    native = usable.loc[has_native].copy()
    native["block_id"] = block_id_text.loc[has_native].astype(object)
    native["block_source"] = "native"

    missing = usable.loc[~has_native].copy()
    if verbose:
        print(f"  native trips={len(native):,} blocks={native['block_id'].nunique():,}")
        print(f"  inferred input trips={len(missing):,}")

    if not missing.empty:
        t_inf = time.time()
        inferred_series = infer_blocks(missing[INFERENCE_COLUMNS], cfg)
        missing["block_id"] = inferred_series.reindex(missing.index).to_numpy(dtype=object)
        missing["block_source"] = "inferred"
        if verbose:
            print(f"  inference took {time.time() - t_inf:.0f}s")
            print(f"  inferred blocks={missing['block_id'].nunique():,}")
    else:
        missing["block_source"] = "inferred"

    all_blocks = pd.concat([native, missing], ignore_index=True)
    all_blocks = all_blocks.loc[:, OUTPUT_COLUMNS].copy()
    for col in ("trip_id", "agency_id", "block_id", "block_source", "start_stop", "end_stop", "shape_id"):
        all_blocks[col] = all_blocks[col].astype(object)
    all_blocks = all_blocks.sort_values(["agency_id", "block_id", "start_h"], kind="stable").reset_index(drop=True)

    if write_checkpoints:
        block_ckpt = out_dir / "_all_blocks.ckpt.pkl"
        all_blocks.to_pickle(block_ckpt)
        if verbose:
            print(f"  checkpoint: {block_ckpt} ({block_ckpt.stat().st_size / 1e6:.1f} MB)")

    if output is not None:
        output = Path(output)
        output.parent.mkdir(parents=True, exist_ok=True)
        all_blocks.to_parquet(output, index=False)
        if verbose:
            print(f"  saved: {output} ({output.stat().st_size / 1e6:.1f} MB, {len(all_blocks):,} rows)")

    if verbose:
        print("=== Summary ===")
        print(f"  total trips={len(all_blocks):,}")
        print(f"  total blocks={all_blocks['block_id'].nunique():,}")
        for source, group in all_blocks.groupby("block_source", sort=True):
            print(f"  {source:<8} trips={len(group):>10,} blocks={group['block_id'].nunique():>8,}")
        print(f"  wall time={time.time() - start:.0f}s")

    return all_blocks


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gtfs-dir", type=Path, default=DEFAULT_GTFS_DIR)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    parser.add_argument("--chunk-rows", type=int, default=CHUNK_ROWS)
    parser.add_argument("--no-checkpoints", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> None:
    args = _parser().parse_args(argv)
    build_all_blocks(
        gtfs_dir=args.gtfs_dir,
        output=args.output,
        out_dir=args.out_dir,
        chunk_rows=args.chunk_rows,
        write_checkpoints=not args.no_checkpoints,
        verbose=True,
    )


if __name__ == "__main__":
    main()
