"""Lightweight ONS Postcode Directory lookup helpers."""

from __future__ import annotations

from pathlib import Path
import warnings

import pandas as pd


PROJECT_ROOT = Path(__file__).resolve().parents[2].parent
DEFAULT_ONSPD_LATEST_PATH = PROJECT_ROOT / "Data" / "Loads" / "ONSPD_latest.csv"


def normalise_postcode(postcode: str | object) -> str:
    """Return an uppercase postcode key with all whitespace removed."""
    if postcode is None or pd.isna(postcode):
        return ""
    return "".join(str(postcode).upper().split())


def load_onspd(path: Path = DEFAULT_ONSPD_LATEST_PATH) -> dict[str, tuple[float, float]]:
    """Load ONSPD postcode coordinates as ``postcode -> (lat, lon)``.

    Missing ONSPD is a data-quality issue, not a parser failure: callers still
    need to continue and fall back to lower-confidence depot locations.
    """
    source_path = Path(path)
    if not source_path.exists():
        warnings.warn(
            f"ONSPD postcode directory not found: {source_path}",
            RuntimeWarning,
            stacklevel=2,
        )
        return {}

    header = pd.read_csv(source_path, nrows=0)
    columns = set(header.columns)
    postcode_col = next((col for col in ("pcds", "pcd", "pcd2", "postcode") if col in columns), None)
    lat_col = next((col for col in ("lat", "latitude") if col in columns), None)
    lon_col = next((col for col in ("long", "lon", "longitude") if col in columns), None)
    if postcode_col is None or lat_col is None or lon_col is None:
        raise ValueError(
            "ONSPD CSV must contain postcode plus lat/lon columns; "
            f"got {sorted(columns)}"
        )

    raw = pd.read_csv(
        source_path,
        usecols=[postcode_col, lat_col, lon_col],
        dtype={postcode_col: "string"},
    )
    raw["_postcode_key"] = raw[postcode_col].map(normalise_postcode)
    raw["_lat"] = pd.to_numeric(raw[lat_col], errors="coerce")
    raw["_lon"] = pd.to_numeric(raw[lon_col], errors="coerce")
    valid = (
        raw["_postcode_key"].ne("")
        & raw["_lat"].between(48.0, 62.5)
        & raw["_lon"].between(-12.0, 4.0)
    )
    deduped = raw.loc[valid, ["_postcode_key", "_lat", "_lon"]].drop_duplicates(
        "_postcode_key",
        keep="first",
    )
    return {
        str(row["_postcode_key"]): (float(row["_lat"]), float(row["_lon"]))
        for _, row in deduped.iterrows()
    }


def geocode_postcode(
    postcode: str,
    index: dict[str, tuple[float, float]],
) -> tuple[float, float] | None:
    """Look up one postcode in a ``load_onspd`` index."""
    key = normalise_postcode(postcode)
    if not key:
        return None
    return index.get(key)
