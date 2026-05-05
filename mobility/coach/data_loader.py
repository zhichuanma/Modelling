"""Data loading and quality summaries for coach vehicle journeys."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from .distance import build_coords_lookup, vehicle_journey_distance_km
from .stop_geometry import load_unified_stops
from .txc_parser import (
    build_trip_table_from_xml,
    build_vehicle_journey_stop_times,
    expand_vehicle_journeys_to_timing_rows,
    load_txc_components,
    parse_clock_to_seconds,
)


ROOT = Path(__file__).resolve().parents[2]
PROJECT_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_COACH_ROOT = PROJECT_ROOT / "Data" / "EV_behavior" / "Coach_Data" / "TxC-2.4"
DEFAULT_INVENTORY_PATH = DEFAULT_COACH_ROOT / "TxCInventory17APR26.csv"
DEFAULT_JOURNEYS_PATH = ROOT / "outputs" / "all_coach_journeys.parquet"
DEFAULT_STOP_SEQUENCES_PATH = ROOT / "outputs" / "all_coach_stop_sequences.parquet"

JOURNEY_REQUIRED_COLUMNS = (
    "journey_id",
    "vehicle_journey_code",
    "operator_code",
    "operator_name",
    "line_name",
    "departure_time",
    "arrival_time",
    "start_h",
    "end_h",
    "duration_h",
    "distance_km",
    "distance_source",
    "road_detour_factor",
    "has_cross_midnight",
)
STOP_SEQUENCE_REQUIRED_COLUMNS = (
    "journey_id",
    "vehicle_journey_code",
    "stop_sequence",
    "stop_point_ref",
)


def _clock_to_hours(value: Any) -> float:
    seconds = parse_clock_to_seconds(str(value)) if pd.notna(value) else None
    return float(seconds) / 3600.0 if seconds is not None else float("nan")


def _journey_key(file_name: str, vehicle_journey_code: str) -> str:
    return f"{file_name}::{vehicle_journey_code}"


def _pct(numerator: float, denominator: float) -> float:
    return float(numerator) / float(denominator) * 100.0 if denominator else float("nan")


def _inventory_value(row: pd.Series, name: str, default: Any = "") -> Any:
    return row[name] if name in row.index and pd.notna(row[name]) else default


def build_all_coach_tables(
    inventory_path: str | Path = DEFAULT_INVENTORY_PATH,
    coach_root: str | Path = DEFAULT_COACH_ROOT,
    *,
    stops_geom: pd.DataFrame | None = None,
    road_detour_factor: float = 1.30,
    limit: int | None = None,
    progress_interval: int = 0,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Parse TxC XML into journey and stop-sequence tables."""
    inventory_path = Path(inventory_path)
    coach_root = Path(coach_root)
    inventory = pd.read_csv(inventory_path)
    stops = load_unified_stops() if stops_geom is None else stops_geom.copy()
    coords = build_coords_lookup(stops)
    stop_tables: list[pd.DataFrame] = []
    journey_tables: list[pd.DataFrame] = []

    if limit is not None:
        inventory = inventory.head(int(limit))

    total_rows = len(inventory)
    for index, (_, inventory_row) in enumerate(inventory.iterrows(), 1):
        xml_path = coach_root / str(inventory_row["FilePath"])
        if not xml_path.exists():
            continue

        components = load_txc_components(xml_path)
        timing_rows = expand_vehicle_journeys_to_timing_rows(components)
        stop_times = build_vehicle_journey_stop_times(timing_rows, components["stop_points"])
        trip_table = build_trip_table_from_xml(xml_path)
        if trip_table.empty or stop_times.empty:
            continue

        file_name = str(xml_path.name)
        trip_table = trip_table.copy()
        trip_table["journey_id"] = [
            _journey_key(file_name, str(code))
            for code in trip_table["vehicle_journey_code"]
        ]
        trip_table["start_h"] = trip_table["departure_time"].map(_clock_to_hours)
        trip_table["end_h"] = trip_table["arrival_time"].map(_clock_to_hours)
        has_runtime = pd.to_numeric(trip_table["runtime_min"], errors="coerce").notna()
        bad_end = trip_table["end_h"].isna() | (trip_table["end_h"] <= trip_table["start_h"])
        trip_table.loc[bad_end & has_runtime, "end_h"] = (
            trip_table.loc[bad_end & has_runtime, "start_h"]
            + pd.to_numeric(trip_table.loc[bad_end & has_runtime, "runtime_min"], errors="coerce") / 60.0
        )
        trip_table["duration_h"] = trip_table["end_h"] - trip_table["start_h"]
        trip_table["has_cross_midnight"] = (trip_table["start_h"] >= 24.0) | (trip_table["end_h"] > 24.0)
        for col in inventory.columns:
            target = col if col not in trip_table.columns else f"inventory_{col}"
            trip_table[target] = _inventory_value(inventory_row, col)

        stop_times = stop_times.copy()
        stop_times["file_name"] = file_name
        stop_times["xml_path"] = str(xml_path)
        stop_times["journey_id"] = [
            _journey_key(file_name, str(code))
            for code in stop_times["vehicle_journey_code"]
        ]
        geom = stops.rename(columns={"source": "stop_source"})
        stop_times = stop_times.merge(geom, on="stop_point_ref", how="left")

        distances: dict[str, tuple[float | None, str]] = {}
        for journey_id, stop_group in stop_times.groupby("journey_id", sort=False):
            distances[journey_id] = vehicle_journey_distance_km(
                stop_group,
                stops,
                road_detour_factor=road_detour_factor,
                coords=coords,
            )
        distance_frame = pd.DataFrame(
            [
                {
                    "journey_id": journey_id,
                    "distance_km": distance,
                    "distance_source": source,
                    "road_detour_factor": float(road_detour_factor),
                }
                for journey_id, (distance, source) in distances.items()
            ]
        )
        trip_table = trip_table.merge(distance_frame, on="journey_id", how="left")

        journey_tables.append(trip_table)
        stop_tables.append(stop_times)
        if progress_interval > 0 and (index % progress_interval == 0 or index == total_rows):
            print(f"  Parsed {index:,}/{total_rows:,} coach TxC inventory rows", flush=True)

    if not journey_tables:
        return pd.DataFrame(columns=JOURNEY_REQUIRED_COLUMNS), pd.DataFrame(columns=STOP_SEQUENCE_REQUIRED_COLUMNS)

    journey_tables = [table for table in journey_tables if not table.empty]
    stop_tables = [table for table in stop_tables if not table.empty]
    journeys = pd.concat(journey_tables, ignore_index=True)
    stop_sequences = pd.concat(stop_tables, ignore_index=True)
    return journeys, stop_sequences


def write_all_coach_tables(
    journeys_path: str | Path = DEFAULT_JOURNEYS_PATH,
    stop_sequences_path: str | Path = DEFAULT_STOP_SEQUENCES_PATH,
    **kwargs: Any,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Build and write coach parquet outputs."""
    journeys, stop_sequences = build_all_coach_tables(**kwargs)
    journeys_path = Path(journeys_path)
    stop_sequences_path = Path(stop_sequences_path)
    journeys_path.parent.mkdir(parents=True, exist_ok=True)
    stop_sequences_path.parent.mkdir(parents=True, exist_ok=True)
    journeys.to_parquet(journeys_path, index=False)
    stop_sequences.to_parquet(stop_sequences_path, index=False)
    return journeys, stop_sequences


def _validate_columns(df: pd.DataFrame, required: tuple[str, ...], label: str) -> pd.DataFrame:
    missing = [col for col in required if col not in df.columns]
    if missing:
        raise ValueError(f"{label} is missing required columns: {missing}")
    return df


def load_all_coach_journeys(path: str | Path = DEFAULT_JOURNEYS_PATH) -> pd.DataFrame:
    """Read ``all_coach_journeys.parquet`` and validate the public columns."""
    df = pd.read_parquet(path)
    return _validate_columns(df, JOURNEY_REQUIRED_COLUMNS, "all_coach_journeys")


def load_all_coach_stop_sequences(path: str | Path = DEFAULT_STOP_SEQUENCES_PATH) -> pd.DataFrame:
    """Read ``all_coach_stop_sequences.parquet`` and validate the public columns."""
    df = pd.read_parquet(path)
    return _validate_columns(df, STOP_SEQUENCE_REQUIRED_COLUMNS, "all_coach_stop_sequences")


def summarize_journey_quality(journeys: pd.DataFrame) -> pd.DataFrame:
    """Return one-row journey quality metrics for notebook Stage A."""
    data = journeys.copy()
    total = int(len(data))
    known = int(pd.to_numeric(data.get("distance_km"), errors="coerce").notna().sum())
    unknown = total - known
    cross_midnight = int(data.get("has_cross_midnight", pd.Series(False, index=data.index)).astype(bool).sum())
    operator_present = data.get("operator_code", pd.Series("", index=data.index)).fillna("").astype(str).str.strip().ne("")
    runtime_present = pd.to_numeric(data.get("runtime_min", pd.Series(np.nan, index=data.index)), errors="coerce").notna()

    record = {
        "total_journeys": total,
        "known_distance_journeys": known,
        "unknown_distance_journeys": unknown,
        "known_distance_pct": _pct(known, total),
        "unknown_distance_pct": _pct(unknown, total),
        "cross_midnight_pct": _pct(cross_midnight, total),
        "operator_coverage_pct": _pct(int(operator_present.sum()), total),
        "runtime_coverage_pct": _pct(int(runtime_present.sum()), total),
        "n_operators": int(data.loc[operator_present, "operator_code"].nunique()) if "operator_code" in data.columns else 0,
        "simulatable_known_non_cross_midnight_journeys": int(
            (
                pd.to_numeric(data.get("distance_km"), errors="coerce").notna()
                & ~data.get("has_cross_midnight", pd.Series(False, index=data.index)).astype(bool)
            ).sum()
        ),
    }
    return pd.DataFrame([record])
