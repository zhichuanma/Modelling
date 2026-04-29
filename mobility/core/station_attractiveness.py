"""
Stage 7 · Station-level global attractiveness.

Unit conventions (see AGENTS.md §3.1)
------------------------------------
- TotalCapacity_kW: installed charging power in kW
- station_attractiveness: dimensionless; log(1 + TotalCapacity_kW)

Usage
-----
- compute_station_attractiveness(df): adds 'station_attractiveness' column to a
  station DataFrame; returns the same DataFrame (in-place for the new column).
- CLI entrypoint at __main__: reads a canonical station table, writes it back
  with the new column, and can sync one or more additional copies after
  creating .pre_stage7.bak idempotent backups.
"""
from __future__ import annotations

import argparse
from pathlib import Path
from typing import Callable

import numpy as np
import pandas as pd

DEFAULT_STATION_PATH = (
    Path(__file__).resolve().parents[3]
    / "Data"
    / "Charging_stations"
    / "OSM_POI_Labeling"
    / "UK_OCM_stations_labeled.csv"
)


def compute_station_attractiveness(
    df: pd.DataFrame,
    capacity_col: str = "TotalCapacity_kW",
    out_col: str = "station_attractiveness",
) -> pd.DataFrame:
    """
    Add a dimensionless station_attractiveness = log(1 + TotalCapacity_kW) column.

    Raises
    ------
    KeyError
        If `capacity_col` is not present.
    ValueError
        If `capacity_col` contains NaN values.
    ValueError
        If `capacity_col` contains negative values.
    """
    if capacity_col not in df.columns:
        raise KeyError(f"Missing required column: {capacity_col!r}")

    cap = df[capacity_col]
    if cap.isna().any():
        n_bad = int(cap.isna().sum())
        raise ValueError(
            f"{capacity_col!r} contains {n_bad} NaN values. "
            f"Stage 7 requires a clean capacity column - fix upstream."
        )
    if (cap < 0).any():
        n_bad = int((cap < 0).sum())
        raise ValueError(
            f"{capacity_col!r} contains {n_bad} negative values. Investigate upstream."
        )

    df[out_col] = np.log1p(cap.to_numpy(dtype=float))
    return df


def _backup_station_table(station_path: Path) -> None:
    import shutil

    backup = station_path.with_suffix(station_path.suffix + ".pre_stage7.bak")
    if not backup.exists():
        shutil.copy2(station_path, backup)
        print(f"[backup] {backup}")
    else:
        print(f"[backup] already exists, skipping: {backup}")


def _read_station_table(
    station_path: Path,
) -> tuple[pd.DataFrame, Callable[[pd.DataFrame], None]]:
    suffix = station_path.suffix.lower()
    if suffix == ".parquet":
        df = pd.read_parquet(station_path)
        write = lambda data_frame: data_frame.to_parquet(station_path, index=False)
    elif suffix == ".csv":
        df = pd.read_csv(station_path)
        write = lambda data_frame: data_frame.to_csv(station_path, index=False)
    else:
        raise ValueError(f"Unsupported station table format: {station_path.suffix}")
    return df, write


def _write_updated_station_table(station_path: Path) -> pd.DataFrame:
    _backup_station_table(station_path)
    df, write = _read_station_table(station_path)

    cols_before = set(df.columns)
    df = compute_station_attractiveness(df)
    cols_after = set(df.columns)

    expected_added = {"station_attractiveness"} if "station_attractiveness" not in cols_before else set()
    added = cols_after - cols_before
    removed = cols_before - cols_after
    assert added == expected_added, f"Unexpected added cols: {added}"
    assert removed == set(), f"Unexpected removed cols: {removed}"

    write(df)
    print(
        f"[write] {station_path} with {len(df)} rows, "
        "new col: station_attractiveness"
    )
    return df


def _sync_station_table_copy(station_path: Path, canonical_df: pd.DataFrame) -> None:
    _backup_station_table(station_path)
    _, write = _read_station_table(station_path)
    write(canonical_df)
    print(
        f"[write] {station_path} with {len(canonical_df)} rows, "
        "synced to canonical station table"
    )


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Add station_attractiveness to a canonical station table and "
            "optionally sync additional copies to the same post-update contents."
        )
    )
    parser.add_argument(
        "--path",
        dest="paths",
        action="append",
        help=(
            "Station table path. Repeat to sync multiple copies. The first path "
            "is treated as canonical; defaults to the Data-side OSM POI table."
        ),
    )
    return parser.parse_args()


def _main() -> None:
    """
    CLI entrypoint: update the canonical station table, then sync optional copies.
    Creates idempotent .pre_stage7.bak backups before each write.
    """
    args = _parse_args()
    paths = [Path(path) for path in args.paths] if args.paths else [DEFAULT_STATION_PATH]

    canonical_df = _write_updated_station_table(paths[0])
    for station_path in paths[1:]:
        _sync_station_table_copy(station_path, canonical_df)


if __name__ == "__main__":
    _main()
