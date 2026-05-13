"""Stop-coordinate loading for TransXChange coach journeys."""

from __future__ import annotations

from pathlib import Path

import pandas as pd


ROOT = Path(__file__).resolve().parents[2]
PROJECT_ROOT = Path(__file__).resolve().parents[3]

DEFAULT_NAPTAN_PATH = ROOT / "data" / "Stops.csv"
DEFAULT_CUSTOM_STOPS_PATH = (
    PROJECT_ROOT
    / "Data"
    / "EV_behavior"
    / "Coach_Data"
    / "TxC-2.4"
    / "CustomStopsList17APR26.csv"
)

OUTPUT_COLUMNS = ("stop_point_ref", "lat", "lon", "source")


def _empty() -> pd.DataFrame:
    return pd.DataFrame(columns=OUTPUT_COLUMNS)


def _normalise_stop_frame(
    raw: pd.DataFrame,
    *,
    ref_col: str,
    lat_col: str,
    lon_col: str,
    source: str,
) -> pd.DataFrame:
    if raw.empty or ref_col not in raw.columns:
        return _empty()
    out = pd.DataFrame(
        {
            "stop_point_ref": raw[ref_col].astype(str).str.strip(),
            "lat": pd.to_numeric(raw.get(lat_col), errors="coerce"),
            "lon": pd.to_numeric(raw.get(lon_col), errors="coerce"),
            "source": source,
        }
    )
    out = out[out["stop_point_ref"].ne("")]
    return out.loc[:, OUTPUT_COLUMNS]


def _read_csv_if_present(path: Path) -> pd.DataFrame:
    if path is None or not Path(path).exists():
        return pd.DataFrame()
    return pd.read_csv(path, low_memory=False)


def load_unified_stops(
    naptan_path: str | Path | None = DEFAULT_NAPTAN_PATH,
    custom_stops_path: str | Path | None = DEFAULT_CUSTOM_STOPS_PATH,
) -> pd.DataFrame:
    """Load NaPTAN plus project custom stops into one coordinate table.

    Missing source files are tolerated because coach distance estimation can
    fall back to ``distance_source='unknown'`` at journey level.
    """
    frames: list[pd.DataFrame] = []

    if naptan_path is not None:
        naptan = _read_csv_if_present(Path(naptan_path))
        frames.append(
            _normalise_stop_frame(
                naptan,
                ref_col="ATCOCode",
                lat_col="Latitude",
                lon_col="Longitude",
                source="naptan",
            )
        )

    if custom_stops_path is not None:
        custom = _read_csv_if_present(Path(custom_stops_path))
        frames.append(
            _normalise_stop_frame(
                custom,
                ref_col="AtcoCode",
                lat_col="Latitude",
                lon_col="Longitude",
                source="custom",
            )
        )

    frames = [frame for frame in frames if not frame.empty]
    if not frames:
        return _empty()

    unified = pd.concat(frames, ignore_index=True)
    if unified.empty:
        return _empty()

    unified = unified.drop_duplicates("stop_point_ref", keep="last")
    return unified.sort_values("stop_point_ref").reset_index(drop=True).loc[:, OUTPUT_COLUMNS]


def attach_lsoa_to_journeys(
    journeys: pd.DataFrame,
    *,
    centroids: pd.DataFrame | None = None,
    onspd_path: str | Path | None = None,
    max_distance_km: float | None = 5.0,
) -> pd.DataFrame:
    """Attach nearest LSOA centroid codes to coach journey endpoints.

    This is a coach-local, nearest-centroid fallback for annual attribution. It
    deliberately does not import the bus spatial join or polygon boundary path.
    """
    required = ("start_lat", "start_lon", "end_lat", "end_lon")
    missing = [column for column in required if column not in journeys.columns]
    if missing:
        raise ValueError(f"journeys is missing required coordinate columns: {missing}")

    from mobility.core.spatial import load_lsoa_centroids, nearest_lsoa_for_points

    out = journeys.copy()
    centroid_frame = load_lsoa_centroids(Path(onspd_path) if onspd_path is not None else None) if centroids is None else centroids.copy()
    start_codes, start_distances = nearest_lsoa_for_points(
        pd.to_numeric(out["start_lat"], errors="coerce").to_numpy(dtype=float),
        pd.to_numeric(out["start_lon"], errors="coerce").to_numpy(dtype=float),
        centroid_frame,
        max_distance_km=max_distance_km,
    )
    end_codes, end_distances = nearest_lsoa_for_points(
        pd.to_numeric(out["end_lat"], errors="coerce").to_numpy(dtype=float),
        pd.to_numeric(out["end_lon"], errors="coerce").to_numpy(dtype=float),
        centroid_frame,
        max_distance_km=max_distance_km,
    )
    out["start_lsoa"] = start_codes
    out["end_lsoa"] = end_codes
    out["start_lsoa_distance_km"] = start_distances
    out["end_lsoa_distance_km"] = end_distances
    out["start_lsoa_match_method"] = pd.Series(start_codes).replace("", pd.NA).notna().map(
        {True: "centroid_nearest", False: "no_match"}
    ).to_numpy()
    out["end_lsoa_match_method"] = pd.Series(end_codes).replace("", pd.NA).notna().map(
        {True: "centroid_nearest", False: "no_match"}
    ).to_numpy()
    return out
