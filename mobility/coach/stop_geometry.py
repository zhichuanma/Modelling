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
