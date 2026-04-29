"""Spatial helpers for LSOA centroids and OD distances.

Units:
- `_m` suffix means meter in British National Grid (EPSG:27700).
- `_km` suffix means kilometer derived from Euclidean BNG distances.
"""

from __future__ import annotations

from pathlib import Path
from typing import Sequence

import numpy as np
import pandas as pd

DEFAULT_ONSPD_PATH = (
    Path(__file__).resolve().parents[3] / "Data" / "Units" / "ONSPD_MAY_2025_UK.csv"
)


def load_lsoa_centroids(onspd_path: Path | None = None) -> pd.DataFrame:
    """Load LSOA centroids from ONSPD postcode points."""
    source_path = DEFAULT_ONSPD_PATH if onspd_path is None else Path(onspd_path)
    raw = pd.read_csv(
        source_path,
        usecols=["lsoa21", "oseast1m", "osnrth1m"],
        dtype={"lsoa21": "string"},
    )

    centroids = raw.rename(
        columns={
            "lsoa21": "lsoa_code",
            "oseast1m": "easting_m",
            "osnrth1m": "northing_m",
        }
    ).copy()
    centroids["lsoa_code"] = centroids["lsoa_code"].str.strip()
    centroids["easting_m"] = pd.to_numeric(centroids["easting_m"], errors="coerce")
    centroids["northing_m"] = pd.to_numeric(centroids["northing_m"], errors="coerce")

    valid_rows = (
        centroids["lsoa_code"].notna()
        & centroids["lsoa_code"].ne("")
        & centroids["easting_m"].notna()
        & centroids["northing_m"].notna()
        & centroids["easting_m"].ne(0.0)
    )
    grouped = (
        centroids.loc[valid_rows, ["lsoa_code", "easting_m", "northing_m"]]
        .groupby("lsoa_code", as_index=False, sort=True)[["easting_m", "northing_m"]]
        .mean()
        .sort_values("lsoa_code", kind="stable")
        .reset_index(drop=True)
    )

    grouped["lsoa_code"] = grouped["lsoa_code"].astype(object)
    grouped["easting_m"] = grouped["easting_m"].astype(float)
    grouped["northing_m"] = grouped["northing_m"].astype(float)
    return grouped


def od_distance_km(
    a_lsoa: str,
    b_lsoa: str,
    centroids: pd.DataFrame,
    intra_km: float = 0.5,
) -> float:
    """Return the BNG Euclidean centroid distance in kilometers."""
    if a_lsoa == b_lsoa:
        return float(intra_km)

    indexed = _indexed_centroids(centroids)
    pair = indexed.loc[[a_lsoa, b_lsoa], ["easting_m", "northing_m"]].to_numpy(dtype=float)
    delta_m = pair[0] - pair[1]
    return float(np.sqrt(np.square(delta_m).sum()) / 1000.0)


def od_distance_matrix(
    codes: Sequence[str],
    centroids: pd.DataFrame,
    intra_km: float = 0.5,
) -> np.ndarray:
    """Return a vectorized `(N, N)` OD distance matrix in kilometers."""
    code_array = np.asarray(codes, dtype=object)
    indexed = _indexed_centroids(centroids)
    points = indexed.loc[list(code_array), ["easting_m", "northing_m"]].to_numpy(dtype=float)
    easting_m = points[:, 0]
    northing_m = points[:, 1]

    delta_e_m = np.subtract.outer(easting_m, easting_m)
    delta_n_m = np.subtract.outer(northing_m, northing_m)
    distance_km = np.sqrt(np.square(delta_e_m) + np.square(delta_n_m)) / 1000.0
    distance_km[np.equal.outer(code_array, code_array)] = float(intra_km)
    return distance_km


def _indexed_centroids(centroids: pd.DataFrame) -> pd.DataFrame:
    if "lsoa_code" in centroids.columns:
        indexed = centroids.set_index("lsoa_code", drop=True)
    else:
        indexed = centroids

    missing_columns = {"easting_m", "northing_m"} - set(indexed.columns)
    if missing_columns:
        raise KeyError(f"centroids is missing required columns: {sorted(missing_columns)}")

    if indexed.index.has_duplicates:
        raise ValueError("centroids must have unique lsoa_code values")

    return indexed.loc[:, ["easting_m", "northing_m"]]
