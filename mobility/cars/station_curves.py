"""Private-car station-level charging curve export helpers.

This module is intentionally an export layer around the existing passenger-car
model. It reuses the schedule assembly, station matching, and uncontrolled
charging simulation already used by ``notebooks/00_single_car_simulation.ipynb``
and only adds the station-level bin attribution, aggregation, and web JSON
serialization needed by the private-car curve project.
"""

from __future__ import annotations

import datetime as dt
import errno
import json
import math
import os
import re
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Mapping

import numpy as np
import pandas as pd

from mobility.cars.data_loader import load_ev_fleet
from mobility.cars.station_matcher import (
    _build_lsoa_indices,
    match_stations_for_schedule,
)
from mobility.cars.trip_chain import assign_year_schedules
from mobility.cars.week_pattern import build_leisure_pool_index, build_library_index
from mobility.core.constants import DEFAULT_CHEMISTRY, WARMUP_DAYS
from mobility.core.data_structures import DailySchedule, ParkingEvent
from mobility.core.simulator import STEP_HOURS, STEPS_PER_DAY, simulate_single_ev
from mobility.core.spatial import load_lsoa_centroids

SCHEMA_VERSION = "1.0"
SCOPE = "private_car_public_charging_only"
CHARGING_STRATEGY = "uncontrolled"
QUEUE_MODEL = "not_considered"
TIME_RESOLUTION_MINUTES = 15
WEB_JSON_WRITE_ATTEMPTS = 5
WEB_JSON_WRITE_RETRY_SECONDS = 0.5

MAIN_CAR_SEED = 20260422
ALT_CAR_SEED = MAIN_CAR_SEED + 1

_STEP_STARTS_H = np.arange(STEPS_PER_DAY, dtype=float) * STEP_HOURS
_STEP_ENDS_H = _STEP_STARTS_H + STEP_HOURS
_VALID_HOLIDAY_REGIONS = {"england", "wales", "scotland", "ni"}


@dataclass
class CurveRunMetrics:
    """Counters and checks accumulated while building station curves."""

    study_year: int
    timezone: str = "not_specified_in_existing_model"
    private_vehicle_count_available: int = 0
    private_vehicle_count_run: int = 0
    failed_vehicle_count: int = 0
    vehicle_limit: int | None = None
    chunk_size: int = 0
    station_metadata_count: int = 0
    station_metadata_missing_name_count: int = 0
    station_metadata_missing_latitude_count: int = 0
    station_metadata_missing_longitude_count: int = 0
    session_count: int = 0
    bin_row_count: int = 0
    station_curve_row_count: int = 0
    station_summary_row_count: int = 0
    public_session_energy_kwh: float = 0.0
    public_bin_energy_kwh: float = 0.0
    station_curve_energy_kwh: float = 0.0
    invalid_session_time_count: int = 0
    negative_session_energy_count: int = 0
    negative_power_count: int = 0
    missing_station_id_count: int = 0
    unmatched_station_metadata_count: int = 0
    station_date_count: int = 0
    json_file_count: int = 0
    json_parse_failures: int = 0
    station_dates_with_96_points: int = 0
    station_dates_without_96_points: int = 0
    notes: list[str] = field(default_factory=list)

    @property
    def is_sample(self) -> bool:
        return self.vehicle_limit is not None


class LazyDestinationSampler:
    """Read-only destination sampler matching the notebook's lazy wrapper.

    The package-level ``DestinationSampler`` eagerly indexes the full
    destination parquet. The 00 notebook deliberately used this lazy variant for
    the 1.1 GB table, so the export pipeline does the same while keeping the
    ``sample_destination_lsoa`` and ``distance_km`` interface unchanged.
    """

    def __init__(
        self,
        table_path: Path,
        centroids: pd.DataFrame | None = None,
        *,
        cache_mode: str = "origin",
    ):
        import pyarrow.dataset as ds

        if cache_mode not in {"key", "origin"}:
            raise ValueError("cache_mode must be 'key' or 'origin'")

        self._table_path = Path(table_path)
        self._dataset = ds.dataset(self._table_path, format="parquet")
        self._cache_mode = cache_mode
        centroid_frame = load_lsoa_centroids() if centroids is None else centroids.copy()
        if "lsoa_code" in centroid_frame.columns:
            centroid_frame = centroid_frame.set_index("lsoa_code", drop=True)
        self._centroids = centroid_frame.loc[:, ["easting_m", "northing_m"]]
        self._index: dict[tuple[str, str], tuple[np.ndarray, np.ndarray] | None] = {}
        self._loaded_origins: set[str] = set()
        self._warned_missing_keys: set[tuple[str, str]] = set()
        self._sample_call_count = 0
        self._home_purpose_call_count = 0
        self._cache_hit_count = 0
        self._cache_miss_count = 0
        self._fallback_count = 0
        self._query_count = 0
        self._query_seconds = 0.0
        self._query_row_count = 0
        self._distance_call_count = 0
        self._distance_seconds = 0.0
        self._key_request_counts: Counter[tuple[str, str]] = Counter()
        self._key_query_counts: Counter[tuple[str, str]] = Counter()
        self._key_query_seconds: defaultdict[tuple[str, str], float] = defaultdict(float)
        self._key_query_rows: Counter[tuple[str, str]] = Counter()
        self._origin_query_counts: Counter[str] = Counter()
        self._origin_query_seconds: defaultdict[str, float] = defaultdict(float)
        self._origin_query_rows: Counter[str] = Counter()
        self._fallback_key_counts: Counter[tuple[str, str]] = Counter()

    def _load_key(self, origin_lsoa: str, purpose: str):
        import pyarrow.dataset as ds

        key = (str(origin_lsoa), str(purpose))
        if key in self._index:
            return self._index[key]

        if self._cache_mode == "origin":
            self._load_origin(key[0])
            if key not in self._index:
                self._index[key] = None
            return self._index[key]

        start = time.perf_counter()
        table = self._dataset.to_table(
            columns=["origin_lsoa", "purpose", "dest_lsoa", "prob"],
            filter=(ds.field("origin_lsoa") == key[0]) & (ds.field("purpose") == key[1]),
        )
        elapsed = time.perf_counter() - start
        self._query_count += 1
        self._query_seconds += elapsed
        self._query_row_count += int(table.num_rows)
        self._key_query_counts[key] += 1
        self._key_query_seconds[key] += elapsed
        self._key_query_rows[key] += int(table.num_rows)
        if table.num_rows == 0:
            self._index[key] = None
            return None

        group = table.to_pandas()
        dest_lsoas = group["dest_lsoa"].astype(str).to_numpy(dtype=object)
        probs = group["prob"].to_numpy(dtype=np.float64)
        prob_sum = float(probs.sum())
        if prob_sum <= 0.0:
            self._index[key] = None
            return None

        probs = probs / prob_sum
        self._index[key] = (dest_lsoas, probs)
        return self._index[key]

    def _load_origin(self, origin_lsoa: str) -> None:
        import pyarrow.dataset as ds

        origin = str(origin_lsoa)
        if origin in self._loaded_origins:
            return

        start = time.perf_counter()
        table = self._dataset.to_table(
            columns=["origin_lsoa", "purpose", "dest_lsoa", "prob"],
            filter=ds.field("origin_lsoa") == origin,
        )
        elapsed = time.perf_counter() - start
        self._loaded_origins.add(origin)
        self._query_count += 1
        self._query_seconds += elapsed
        self._query_row_count += int(table.num_rows)
        self._origin_query_counts[origin] += 1
        self._origin_query_seconds[origin] += elapsed
        self._origin_query_rows[origin] += int(table.num_rows)

        if table.num_rows == 0:
            return

        group = table.to_pandas()
        for purpose, purpose_group in group.groupby("purpose", sort=False):
            key = (origin, str(purpose))
            dest_lsoas = purpose_group["dest_lsoa"].astype(str).to_numpy(dtype=object)
            probs = purpose_group["prob"].to_numpy(dtype=np.float64)
            prob_sum = float(probs.sum())
            if prob_sum <= 0.0:
                self._index[key] = None
                continue
            self._index[key] = (dest_lsoas, probs / prob_sum)
            self._key_query_counts[key] += 1
            self._key_query_seconds[key] += elapsed
            self._key_query_rows[key] += int(len(purpose_group))

    def preload_origins(self, origin_lsoas: Iterable[object]) -> None:
        for origin_lsoa in sorted({str(value) for value in origin_lsoas if pd.notna(value) and str(value)}):
            self._load_origin(origin_lsoa)

    def sample_destination_lsoa(
        self,
        origin_lsoa: str,
        purpose: str,
        rng: np.random.Generator,
        home_lsoa: str,
    ) -> str:
        self._sample_call_count += 1
        if purpose == "home":
            self._home_purpose_call_count += 1
            return str(home_lsoa)

        key = (str(origin_lsoa), str(purpose))
        self._key_request_counts[key] += 1
        had_key = key in self._index or (
            self._cache_mode == "origin" and str(origin_lsoa) in self._loaded_origins
        )
        hit = self._load_key(*key)
        if had_key:
            self._cache_hit_count += 1
        else:
            self._cache_miss_count += 1
        if hit is None:
            self._warned_missing_keys.add(key)
            self._fallback_count += 1
            self._fallback_key_counts[key] += 1
            return str(home_lsoa)

        dest_lsoas, probs = hit
        return str(rng.choice(dest_lsoas, p=probs))

    def distance_km(self, a: str, b: str) -> float:
        from mobility.core.spatial import od_distance_km

        start = time.perf_counter()
        result = float(od_distance_km(str(a), str(b), self._centroids, intra_km=0.5))
        self._distance_call_count += 1
        self._distance_seconds += time.perf_counter() - start
        return result

    def stats_snapshot(self) -> dict:
        cached_distribution_count = 0
        cached_destination_count = 0
        cached_array_bytes = 0
        cached_string_bytes = 0
        for key, value in self._index.items():
            cached_distribution_count += 1
            cached_string_bytes += sum(len(part.encode("utf-8")) for part in key)
            if value is None:
                continue
            dest_lsoas, probs = value
            cached_destination_count += int(len(dest_lsoas))
            cached_array_bytes += int(dest_lsoas.nbytes) + int(probs.nbytes)
            cached_string_bytes += sum(len(str(dest).encode("utf-8")) for dest in dest_lsoas)
        cache_estimated_bytes = cached_array_bytes + cached_string_bytes
        return {
            "cache_mode": self._cache_mode,
            "sample_call_count": self._sample_call_count,
            "home_purpose_call_count": self._home_purpose_call_count,
            "non_home_sample_call_count": self._sample_call_count - self._home_purpose_call_count,
            "cache_hit_count": self._cache_hit_count,
            "cache_miss_count": self._cache_miss_count,
            "fallback_count": self._fallback_count,
            "unique_requested_keys": len(self._key_request_counts),
            "cached_keys": len(self._index),
            "loaded_origins": len(self._loaded_origins),
            "query_count": self._query_count,
            "query_seconds": self._query_seconds,
            "query_row_count": self._query_row_count,
            "distance_call_count": self._distance_call_count,
            "distance_seconds": self._distance_seconds,
            "cached_distribution_count": cached_distribution_count,
            "cached_destination_count": cached_destination_count,
            "cache_estimated_bytes": cache_estimated_bytes,
            "cache_estimated_mb": cache_estimated_bytes / (1024 * 1024),
        }

    @staticmethod
    def diff_stats(after: Mapping[str, object], before: Mapping[str, object]) -> dict:
        result = {}
        for key, after_value in after.items():
            before_value = before.get(key)
            if isinstance(after_value, (int, float)) and isinstance(before_value, (int, float)):
                value = after_value - before_value
                result[key] = round(float(value), 6) if isinstance(value, float) else int(value)
        result["cache_mode"] = after.get("cache_mode")
        return result

    def key_stats_frame(self) -> pd.DataFrame:
        rows = []
        keys = set(self._key_request_counts) | set(self._key_query_counts) | set(self._fallback_key_counts)
        for origin_lsoa, purpose in sorted(keys):
            key = (origin_lsoa, purpose)
            rows.append(
                {
                    "origin_lsoa": origin_lsoa,
                    "purpose": purpose,
                    "request_count": int(self._key_request_counts[key]),
                    "query_count": int(self._key_query_counts[key]),
                    "query_seconds": round(float(self._key_query_seconds[key]), 6),
                    "query_rows": int(self._key_query_rows[key]),
                    "fallback_count": int(self._fallback_key_counts[key]),
                }
            )
        return pd.DataFrame(
            rows,
            columns=[
                "origin_lsoa",
                "purpose",
                "request_count",
                "query_count",
                "query_seconds",
                "query_rows",
                "fallback_count",
            ],
        )

    def origin_stats_frame(self) -> pd.DataFrame:
        rows = []
        for origin_lsoa in sorted(self._origin_query_counts):
            rows.append(
                {
                    "origin_lsoa": origin_lsoa,
                    "query_count": int(self._origin_query_counts[origin_lsoa]),
                    "query_seconds": round(float(self._origin_query_seconds[origin_lsoa]), 6),
                    "query_rows": int(self._origin_query_rows[origin_lsoa]),
                }
            )
        return pd.DataFrame(rows, columns=["origin_lsoa", "query_count", "query_seconds", "query_rows"])


def inspect_existing_notebook_assets(
    notebook_path: Path | str = Path("notebooks/00_single_car_simulation.ipynb"),
) -> pd.DataFrame:
    """Return a static inventory of 00-notebook assets relevant to this export."""

    path = Path(notebook_path)
    source = path.read_text(encoding="utf-8") if path.exists() else ""

    def contains(*needles: str) -> bool:
        return all(needle in source for needle in needles)

    rows = [
        {
            "item": "private car data",
            "exists": True,
            "variable_file_function": "data/person_fleet.parquet; data/EV_UK_LSOA_2025_with_energy.csv; load_ev_fleet()",
            "reuse": True,
            "notes": "person_fleet binds one NTS person to each EV_ID; EV fleet has vehicle_subtype=cars.",
        },
        {
            "item": "charging window",
            "exists": contains("DailySchedule", "parking_events"),
            "variable_file_function": "DailySchedule.parking_events from assign_year_schedules()",
            "reuse": True,
            "notes": "ParkingEvent is the existing charging-window carrier.",
        },
        {
            "item": "charging demand",
            "exists": contains("energy_charged_kwh", "simulate_single_ev"),
            "variable_file_function": "ParkingEvent.energy_charged_kwh after simulate_single_ev()",
            "reuse": True,
            "notes": "No separate requested_energy_kwh field was found; delivered energy is backfilled by the existing simulator.",
        },
        {
            "item": "charging power",
            "exists": contains("charge_power_kw", "match_stations_for_schedule"),
            "variable_file_function": "ParkingEvent.charge_power_kw from match_stations_for_schedule()",
            "reuse": True,
            "notes": "Station matching sets public power to min(station capacity, vehicle AC power); home uses HOME_CHARGER_KW.",
        },
        {
            "item": "uncontrolled session",
            "exists": True,
            "variable_file_function": "simulate_single_day(); simulate_single_ev()",
            "reuse": True,
            "notes": "Existing core simulator is documented as uncontrolled plug-in charging with AC taper.",
        },
        {
            "item": "session-to-time-bin mapping",
            "exists": contains("load_profile", "STEPS_PER_DAY"),
            "variable_file_function": "vehicle/day load_profile[96]",
            "reuse": False,
            "notes": "Existing mapping is vehicle-level load. This module adds station/session attribution without changing simulation physics.",
        },
        {
            "item": "station metadata",
            "exists": True,
            "variable_file_function": "data/UK_OCM_stations_labeled.csv; STATIONS_PATH",
            "reuse": True,
            "notes": "StationID, Latitude, Longitude, TotalCapacity_kW, StationType, label, lsoa_code, region.",
        },
    ]
    return pd.DataFrame(rows)


_PRIVATE_CAR_SUBTYPES = {"car", "cars", "private_car", "private cars", "privatecar"}
_MISSING_ID_STRINGS = {"", "nan", "none", "null", "na", "<na>", "nat"}


def _normalise_identifier_series(
    series: pd.Series,
    *,
    strip_decimal_zero: bool = False,
) -> pd.Series:
    """Return a nullable string identifier series with common dtype artifacts removed."""

    result = series.astype("string").str.strip()
    lower = result.str.lower()
    result = result.mask(result.isna() | lower.isin(_MISSING_ID_STRINGS), pd.NA)
    if strip_decimal_zero:
        result = result.str.replace(r"\.0$", "", regex=True)
    return result


def _normalise_person_id_series(series: pd.Series) -> pd.Series:
    return _normalise_identifier_series(series, strip_decimal_zero=True)


def _normalise_ev_id_series(series: pd.Series) -> pd.Series:
    return _normalise_identifier_series(series, strip_decimal_zero=False)


def _normalise_private_car_ev_fleet(ev_fleet: pd.DataFrame) -> tuple[pd.DataFrame, list[str]]:
    """Normalize EV identifiers and apply the same private-car subtype filter as the export pipeline."""

    result = ev_fleet.copy()
    result["EV_ID"] = _normalise_ev_id_series(result["EV_ID"]).fillna("")

    subtype_values: list[str] = []
    if "vehicle_subtype" in result.columns:
        subtype_values = sorted(result["vehicle_subtype"].dropna().astype(str).unique().tolist())
        result = result.loc[
            result["vehicle_subtype"].astype(str).str.lower().isin(_PRIVATE_CAR_SUBTYPES)
        ].copy()

    return result, subtype_values


def load_existing_private_car_data(
    data_dir: Path | str = Path("data"),
    *,
    max_vehicles: int | None = None,
) -> tuple[pd.DataFrame, pd.DataFrame, dict]:
    """Load existing private-car binding and EV fleet inputs."""

    data_path = Path(data_dir)
    person_fleet = pd.read_parquet(data_path / "person_fleet.parquet").copy()
    ev_fleet, subtype_values = _normalise_private_car_ev_fleet(load_ev_fleet(data_path))

    if "home_lsoa" not in ev_fleet.columns and "LSOA_code" in ev_fleet.columns:
        ev_fleet["home_lsoa"] = ev_fleet["LSOA_code"]
    person_fleet["ev_id"] = _normalise_ev_id_series(person_fleet["ev_id"]).fillna("")
    person_fleet["person_id"] = _normalise_person_id_series(person_fleet["person_id"]).fillna("")

    valid_ev_ids = set(ev_fleet["EV_ID"].astype(str))
    person_fleet = person_fleet.loc[person_fleet["ev_id"].isin(valid_ev_ids)].copy()
    available_count = int(len(person_fleet))

    if max_vehicles is not None:
        person_fleet = person_fleet.head(int(max_vehicles)).copy()

    run_ev_ids = person_fleet["ev_id"].astype(str).tolist()
    ev_fleet = ev_fleet.loc[ev_fleet["EV_ID"].isin(run_ev_ids)].copy()
    ev_fleet = ev_fleet.set_index("EV_ID", drop=False).loc[run_ev_ids].reset_index(drop=True)

    metadata = {
        "private_vehicle_count_available": available_count,
        "private_vehicle_count_run": int(len(person_fleet)),
        "vehicle_subtype_values": subtype_values,
    }
    return person_fleet, ev_fleet, metadata


def load_existing_charging_windows_or_sessions() -> dict:
    """Document the existing charging-window/session source.

    Charging windows are not stored as a standalone local table in this repo;
    they are generated as ``ParkingEvent`` objects by the existing car pipeline.
    """

    return {
        "charging_window_source": "DailySchedule.parking_events",
        "charging_session_source": "simulate_single_ev mutates ParkingEvent.energy_charged_kwh",
        "standalone_session_file_found": False,
    }


def load_existing_station_metadata(
    data_dir: Path | str = Path("data"),
) -> pd.DataFrame:
    """Load and normalize station metadata while preserving StationID."""

    stations = pd.read_csv(Path(data_dir) / "UK_OCM_stations_labeled.csv").copy()
    stations["station_id"] = stations["StationID"].astype(str)

    title = stations["Title"] if "Title" in stations.columns else pd.Series(pd.NA, index=stations.index)
    title_text = title.where(title.notna(), "").astype(str).str.strip()
    fallback_name = "Station " + stations["station_id"].astype(str)
    stations["station_name"] = title_text.where(title_text != "", fallback_name)
    stations["station_name_source"] = np.where(title_text != "", "Title", "generated_from_station_id")

    rename_map = {
        "Latitude": "latitude",
        "Longitude": "longitude",
        "TotalCapacity_kW": "total_capacity_kw",
        "StationType": "station_type",
        "label": "station_label",
        "lsoa_code": "lsoa_code",
        "region": "region",
    }
    stations = stations.rename(columns={k: v for k, v in rename_map.items() if k in stations.columns})

    keep = [
        "station_id",
        "station_name",
        "station_name_source",
        "latitude",
        "longitude",
        "station_type",
        "station_label",
        "total_capacity_kw",
        "lsoa_code",
        "region",
    ]
    for column in keep:
        if column not in stations.columns:
            stations[column] = pd.NA
    return stations[keep].drop_duplicates("station_id").reset_index(drop=True)


def select_uncontrolled_charging_results() -> dict:
    """Return the charging strategy selection used by this project."""

    return {
        "charging_strategy": CHARGING_STRATEGY,
        "queue_model": QUEUE_MODEL,
        "source": "mobility.core.simulator.simulate_single_ev",
        "notes": "The existing simulator starts charging immediately whenever a ParkingEvent can charge.",
    }


def _normalise_holiday_region(value: object) -> str:
    text = str(value).strip().lower().replace("-", "_").replace(" ", "_")
    text = {"northern_ireland": "ni"}.get(text, text)
    return text if text in _VALID_HOLIDAY_REGIONS else "england"


def _chunk_frame(frame: pd.DataFrame, chunk_size: int) -> Iterable[pd.DataFrame]:
    for start in range(0, len(frame), chunk_size):
        yield frame.iloc[start : start + chunk_size].copy()


def _home_lsoa_map(ev_fleet: pd.DataFrame) -> dict[str, str]:
    if "home_lsoa" in ev_fleet.columns:
        values = ev_fleet["home_lsoa"].fillna("").astype(str)
        if "LSOA_code" in ev_fleet.columns:
            fallback = ev_fleet["LSOA_code"].fillna("").astype(str)
            values = values.where(values != "", fallback)
        return dict(zip(ev_fleet["EV_ID"].astype(str), values))
    if "LSOA_code" in ev_fleet.columns:
        return dict(zip(ev_fleet["EV_ID"].astype(str), ev_fleet["LSOA_code"].fillna("").astype(str)))
    return dict(zip(ev_fleet["EV_ID"].astype(str), np.full(len(ev_fleet), "", dtype=object)))


def _positive_float(value: object, fallback: float) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return float(fallback)
    if not np.isfinite(result) or result <= 0.0:
        return float(fallback)
    return result


def build_year_schedules_with_warmup(
    person_fleet: pd.DataFrame,
    ev_fleet: pd.DataFrame,
    library_df: pd.DataFrame,
    *,
    year: int,
    sampler,
    main_seed: int = MAIN_CAR_SEED,
    warmup_seed: int = ALT_CAR_SEED,
    warmup_days: int = WARMUP_DAYS,
    progress_interval: int = 0,
    schedule_profile_callback=None,
    library_index_cache=None,
    leisure_pool_index_cache=None,
) -> dict[str, list[DailySchedule]]:
    """Build the 14-day warm-up prefix plus the calendar-year schedules."""

    if person_fleet.empty:
        return {}

    jan_1 = dt.date(year, 1, 1)
    warmup_start = jan_1 - dt.timedelta(days=warmup_days)
    warmup_end = jan_1 - dt.timedelta(days=1)

    result: dict[str, list[DailySchedule]] = {}
    person_runtime = person_fleet.copy()
    person_runtime["_holiday_region"] = person_runtime["nts_region"].map(_normalise_holiday_region)

    for region, group in person_runtime.groupby("_holiday_region", sort=False):
        group = group.drop(columns=["_holiday_region"])
        group_ev_ids = group["ev_id"].astype(str).tolist()
        ev_group = ev_fleet.set_index("EV_ID", drop=False).loc[group_ev_ids].reset_index(drop=True)

        schedules_2025 = assign_year_schedules(
            group,
            ev_group,
            library_df,
            year=year,
            n_weeks=53,
            rng=np.random.default_rng(main_seed),
            sampler=sampler,
            region=region,
            progress_interval=progress_interval,
            library_index_cache=library_index_cache,
            leisure_pool_index_cache=leisure_pool_index_cache,
            profile_callback=(
                lambda row, region=region: schedule_profile_callback(
                    {**row, "schedule_scope": "study_year", "holiday_region": region}
                )
                if schedule_profile_callback is not None
                else None
            ),
        )
        schedules_prev = assign_year_schedules(
            group,
            ev_group,
            library_df,
            year=year - 1,
            n_weeks=53,
            rng=np.random.default_rng(warmup_seed),
            sampler=sampler,
            region=region,
            progress_interval=progress_interval,
            library_index_cache=library_index_cache,
            leisure_pool_index_cache=leisure_pool_index_cache,
            profile_callback=(
                lambda row, region=region: schedule_profile_callback(
                    {**row, "schedule_scope": "warmup_year", "holiday_region": region}
                )
                if schedule_profile_callback is not None
                else None
            ),
        )

        for ev_id in group_ev_ids:
            warmup = [
                schedule
                for schedule in schedules_prev[str(ev_id)]
                if schedule.date is not None and warmup_start <= schedule.date <= warmup_end
            ]
            retained = [
                schedule
                for schedule in schedules_2025[str(ev_id)]
                if schedule.date is not None and schedule.date.year == year
            ]
            result[str(ev_id)] = warmup + retained

    return result


def match_schedules_to_stations(
    fleet_schedules: Mapping[str, list[DailySchedule]],
    ev_fleet: pd.DataFrame,
    stations_df: pd.DataFrame,
    *,
    centroids: pd.DataFrame,
) -> None:
    """Annotate schedules in-place with existing station matching logic."""

    indices = _build_lsoa_indices(stations_df)
    centroids_indexed = centroids.set_index("lsoa_code") if "lsoa_code" in centroids.columns else centroids
    home_map = _home_lsoa_map(ev_fleet)
    ac_map = dict(zip(ev_fleet["EV_ID"].astype(str), ev_fleet["ac_power_kw"]))

    for ev_id, schedules in fleet_schedules.items():
        ev_home_lsoa = home_map.get(str(ev_id), "")
        ev_ac_power_kw = _positive_float(ac_map.get(str(ev_id)), 7.0)
        for schedule in schedules:
            date_iso = schedule.date.isoformat() if schedule.date is not None else f"day{schedule.day:03d}"
            match_stations_for_schedule(
                schedule=schedule,
                ev_home_lsoa=ev_home_lsoa,
                ev_ac_power_kw=ev_ac_power_kw,
                stations_df=stations_df,
                rng=np.random.default_rng(0),
                centroids=centroids_indexed,
                _indices=indices,
                date_iso=date_iso,
            )


def _event_overlap_fraction(parking_event: ParkingEvent) -> np.ndarray:
    overlap_h = np.maximum(
        0.0,
        np.minimum(_STEP_ENDS_H, parking_event.end_time)
        - np.maximum(_STEP_STARTS_H, parking_event.start_time),
    )
    return overlap_h / STEP_HOURS


def _timestamp_at_hour(date_value: dt.date, hour_value: float) -> pd.Timestamp:
    return pd.Timestamp(date_value) + pd.to_timedelta(float(hour_value), unit="h")


def _public_station_id(parking_event: ParkingEvent) -> str | None:
    if parking_event.matched_station_id is None:
        return None
    if isinstance(parking_event.matched_station_id, float) and math.isnan(parking_event.matched_station_id):
        return None
    return str(parking_event.matched_station_id)


def build_session_time_bins_for_ev(
    ev_id: str,
    schedules_2025: list[DailySchedule],
    load_profile_2025: np.ndarray,
    *,
    energy_epsilon: float = 1e-10,
) -> tuple[pd.DataFrame, pd.DataFrame, dict]:
    """Attribute one EV's actual load profile to public station time bins."""

    expected_steps = len(schedules_2025) * STEPS_PER_DAY
    if len(load_profile_2025) != expected_steps:
        raise ValueError(
            f"load_profile length {len(load_profile_2025)} does not match "
            f"{len(schedules_2025)} schedules x {STEPS_PER_DAY}."
        )

    bin_rows: list[dict] = []
    session_rows: list[dict] = []
    metrics = {
        "invalid_session_time_count": 0,
        "negative_session_energy_count": 0,
        "negative_power_count": 0,
        "missing_station_id_count": 0,
    }

    for day_index, schedule in enumerate(schedules_2025):
        if schedule.date is None:
            continue
        start = day_index * STEPS_PER_DAY
        end = start + STEPS_PER_DAY
        day_load_kw = np.asarray(load_profile_2025[start:end], dtype=float)
        step_energy_kwh = day_load_kw * STEP_HOURS

        charging_events = [
            (event_index, parking_event)
            for event_index, parking_event in enumerate(schedule.parking_events)
            if parking_event.can_charge and parking_event.charge_power_kw > 0.0
        ]

        denominator = np.zeros(STEPS_PER_DAY, dtype=float)
        event_weights: dict[int, np.ndarray] = {}
        for event_index, parking_event in charging_events:
            if parking_event.end_time < parking_event.start_time:
                metrics["invalid_session_time_count"] += 1
                continue
            if parking_event.charge_power_kw < 0.0:
                metrics["negative_power_count"] += 1
                continue
            weight = parking_event.charge_power_kw * _event_overlap_fraction(parking_event)
            event_weights[event_index] = weight
            denominator += weight

        for event_index, parking_event in charging_events:
            station_id = _public_station_id(parking_event)
            if station_id is None:
                metrics["missing_station_id_count"] += 1
                continue

            weight = event_weights.get(event_index)
            if weight is None:
                continue
            share = np.divide(
                weight,
                denominator,
                out=np.zeros_like(weight),
                where=denominator > 0.0,
            )
            event_energy_by_step = step_energy_kwh * share
            if (event_energy_by_step < -energy_epsilon).any():
                metrics["negative_session_energy_count"] += 1

            active_steps = np.flatnonzero(event_energy_by_step > energy_epsilon)
            if len(active_steps) == 0:
                continue

            session_id = f"{ev_id}_{schedule.date.isoformat()}_pe{event_index:02d}"
            session_energy = float(event_energy_by_step[active_steps].sum())
            session_rows.append(
                {
                    "session_id": session_id,
                    "vehicle_id": str(ev_id),
                    "station_id": station_id,
                    "date": schedule.date.isoformat(),
                    "window_start_time": _timestamp_at_hour(schedule.date, parking_event.start_time),
                    "window_end_time": _timestamp_at_hour(schedule.date, parking_event.end_time),
                    "delivered_energy_kwh": session_energy,
                    "charging_power_kw": float(parking_event.charge_power_kw),
                    "location_purpose": parking_event.location_purpose,
                    "location_lsoa": parking_event.location_lsoa,
                }
            )

            day_start = pd.Timestamp(schedule.date)
            for step in active_steps:
                bin_start = day_start + pd.to_timedelta(int(step) * TIME_RESOLUTION_MINUTES, unit="min")
                bin_rows.append(
                    {
                        "station_id": station_id,
                        "time_bin_start": bin_start,
                        "time_bin_end": bin_start + pd.to_timedelta(TIME_RESOLUTION_MINUTES, unit="min"),
                        "date": schedule.date.isoformat(),
                        "step_index": int(step),
                        "energy_kwh": float(event_energy_by_step[step]),
                        "vehicle_id": str(ev_id),
                        "session_id": session_id,
                    }
                )

    bin_columns = [
        "station_id",
        "time_bin_start",
        "time_bin_end",
        "date",
        "step_index",
        "energy_kwh",
        "vehicle_id",
        "session_id",
    ]
    session_columns = [
        "session_id",
        "vehicle_id",
        "station_id",
        "date",
        "window_start_time",
        "window_end_time",
        "delivered_energy_kwh",
        "charging_power_kw",
        "location_purpose",
        "location_lsoa",
    ]
    return (
        pd.DataFrame(bin_rows, columns=bin_columns),
        pd.DataFrame(session_rows, columns=session_columns),
        metrics,
    )


def load_or_build_session_time_bins_15min(
    fleet_schedules: Mapping[str, list[DailySchedule]],
    ev_fleet: pd.DataFrame,
    *,
    year: int,
    warmup_days: int = WARMUP_DAYS,
) -> tuple[pd.DataFrame, pd.DataFrame, CurveRunMetrics]:
    """Simulate existing uncontrolled charging and build public station bins."""

    ev_lookup = ev_fleet.set_index("EV_ID", drop=False)
    chemistry_available = "chemistry" in ev_fleet.columns

    bin_frames: list[pd.DataFrame] = []
    session_frames: list[pd.DataFrame] = []
    metrics = CurveRunMetrics(study_year=year)

    for ev_id, schedules in fleet_schedules.items():
        if str(ev_id) not in ev_lookup.index:
            continue
        ev_row = ev_lookup.loc[str(ev_id)]
        battery_kwh = _positive_float(ev_row.get("battery_capacity_kwh"), 60.0)
        chemistry = DEFAULT_CHEMISTRY
        if chemistry_available and pd.notna(ev_row.get("chemistry")):
            chemistry = str(ev_row.get("chemistry"))

        _soc, load_profile, _soc_after_warmup = simulate_single_ev(
            schedules,
            battery_kwh,
            warm_up_days=warmup_days,
            chemistry=chemistry,
        )
        schedules_2025 = [
            schedule
            for schedule in schedules
            if schedule.date is not None and schedule.date.year == year
        ]
        bin_df, session_df, ev_metrics = build_session_time_bins_for_ev(
            str(ev_id),
            schedules_2025,
            load_profile,
        )
        if not bin_df.empty:
            bin_frames.append(bin_df)
        if not session_df.empty:
            session_frames.append(session_df)

        for key in [
            "invalid_session_time_count",
            "negative_session_energy_count",
            "negative_power_count",
            "missing_station_id_count",
        ]:
            setattr(metrics, key, getattr(metrics, key) + int(ev_metrics[key]))

    bin_columns = [
        "station_id",
        "time_bin_start",
        "time_bin_end",
        "date",
        "step_index",
        "energy_kwh",
        "vehicle_id",
        "session_id",
    ]
    session_columns = [
        "session_id",
        "vehicle_id",
        "station_id",
        "date",
        "window_start_time",
        "window_end_time",
        "delivered_energy_kwh",
        "charging_power_kw",
        "location_purpose",
        "location_lsoa",
    ]
    bin_result = pd.concat(bin_frames, ignore_index=True) if bin_frames else pd.DataFrame(columns=bin_columns)
    session_result = (
        pd.concat(session_frames, ignore_index=True)
        if session_frames
        else pd.DataFrame(columns=session_columns)
    )
    metrics.bin_row_count = int(len(bin_result))
    metrics.session_count = int(len(session_result))
    metrics.public_bin_energy_kwh = float(bin_result["energy_kwh"].sum()) if not bin_result.empty else 0.0
    metrics.public_session_energy_kwh = (
        float(session_result["delivered_energy_kwh"].sum()) if not session_result.empty else 0.0
    )
    return bin_result, session_result, metrics


def aggregate_station_curves_15min(session_bin_df: pd.DataFrame) -> pd.DataFrame:
    """Aggregate session-bin rows to station-level 15-minute curves."""

    columns = [
        "station_id",
        "time_bin_start",
        "time_bin_end",
        "date",
        "energy_kwh",
        "avg_power_kw",
        "active_vehicle_count",
        "charging_session_count",
    ]
    if session_bin_df.empty:
        return pd.DataFrame(columns=columns)

    grouped = (
        session_bin_df.groupby(["station_id", "time_bin_start", "time_bin_end"], as_index=False)
        .agg(
            energy_kwh=("energy_kwh", "sum"),
            active_vehicle_count=("vehicle_id", "nunique"),
            charging_session_count=("session_id", "nunique"),
        )
        .sort_values(["station_id", "time_bin_start"])
        .reset_index(drop=True)
    )
    grouped["date"] = pd.to_datetime(grouped["time_bin_start"]).dt.strftime("%Y-%m-%d")
    grouped["avg_power_kw"] = grouped["energy_kwh"] / STEP_HOURS
    return grouped[columns]


def _combine_station_curves(curve_frames: list[pd.DataFrame]) -> pd.DataFrame:
    if not curve_frames:
        return aggregate_station_curves_15min(pd.DataFrame())
    combined = pd.concat(curve_frames, ignore_index=True)
    if combined.empty:
        return combined
    combined = (
        combined.groupby(["station_id", "time_bin_start", "time_bin_end"], as_index=False)
        .agg(
            energy_kwh=("energy_kwh", "sum"),
            active_vehicle_count=("active_vehicle_count", "sum"),
            charging_session_count=("charging_session_count", "sum"),
        )
        .sort_values(["station_id", "time_bin_start"])
        .reset_index(drop=True)
    )
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


def _combine_count_frames(frames: list[pd.DataFrame], keys: list[str]) -> pd.DataFrame:
    if not frames:
        return pd.DataFrame(columns=keys + ["unique_vehicles", "total_sessions"])
    combined = pd.concat(frames, ignore_index=True)
    if combined.empty:
        return pd.DataFrame(columns=keys + ["unique_vehicles", "total_sessions"])
    return (
        combined.groupby(keys, as_index=False)
        .agg(unique_vehicles=("unique_vehicles", "sum"), total_sessions=("total_sessions", "sum"))
        .reset_index(drop=True)
    )


def _chunk_paths(output_dir: Path, chunk_number: int) -> dict[str, Path]:
    chunk_dir = output_dir / "chunks"
    prefix = f"chunk_{chunk_number:06d}"
    return {
        "dir": chunk_dir,
        "curve": chunk_dir / f"{prefix}_station_curve.parquet",
        "station_counts": chunk_dir / f"{prefix}_station_counts.parquet",
        "station_day_counts": chunk_dir / f"{prefix}_station_day_counts.parquet",
        "metrics": chunk_dir / f"{prefix}_metrics.json",
    }


def _chunk_checkpoint_exists(output_dir: Path, chunk_number: int) -> bool:
    paths = _chunk_paths(output_dir, chunk_number)
    return all(paths[key].exists() for key in ["curve", "station_counts", "station_day_counts", "metrics"])


def _chunk_count_frame(session_df: pd.DataFrame) -> pd.DataFrame:
    if session_df.empty:
        return pd.DataFrame(columns=["station_id", "unique_vehicles", "total_sessions"])
    return session_df.groupby("station_id", as_index=False).agg(
        unique_vehicles=("vehicle_id", "nunique"),
        total_sessions=("session_id", "nunique"),
    )


def _chunk_day_count_frame(session_df: pd.DataFrame) -> pd.DataFrame:
    if session_df.empty:
        return pd.DataFrame(columns=["station_id", "date", "unique_vehicles", "total_sessions"])
    return session_df.groupby(["station_id", "date"], as_index=False).agg(
        unique_vehicles=("vehicle_id", "nunique"),
        total_sessions=("session_id", "nunique"),
    )


def _chunk_metrics_payload(
    *,
    chunk_number: int,
    total_chunks: int,
    ev_ids: list[str],
    chunk_metrics: CurveRunMetrics,
    station_curve: pd.DataFrame,
    station_counts: pd.DataFrame,
    station_day_counts: pd.DataFrame,
) -> dict:
    return {
        "chunk_number": int(chunk_number),
        "total_chunks": int(total_chunks),
        "vehicle_count": int(len(ev_ids)),
        "vehicle_ids": [str(ev_id) for ev_id in ev_ids],
        "failed_vehicle_count": int(chunk_metrics.failed_vehicle_count),
        "invalid_session_time_count": int(chunk_metrics.invalid_session_time_count),
        "negative_session_energy_count": int(chunk_metrics.negative_session_energy_count),
        "negative_power_count": int(chunk_metrics.negative_power_count),
        "missing_station_id_count": int(chunk_metrics.missing_station_id_count),
        "bin_row_count": int(chunk_metrics.bin_row_count),
        "session_count": int(chunk_metrics.session_count),
        "public_bin_energy_kwh": float(chunk_metrics.public_bin_energy_kwh),
        "public_session_energy_kwh": float(chunk_metrics.public_session_energy_kwh),
        "station_curve_row_count": int(len(station_curve)),
        "station_curve_energy_kwh": float(station_curve["energy_kwh"].sum()) if not station_curve.empty else 0.0,
        "station_count_rows": int(len(station_counts)),
        "station_day_count_rows": int(len(station_day_counts)),
    }


def _write_chunk_checkpoint(
    output_dir: Path,
    *,
    chunk_number: int,
    total_chunks: int,
    ev_ids: list[str],
    chunk_metrics: CurveRunMetrics,
    station_curve: pd.DataFrame,
    station_counts: pd.DataFrame,
    station_day_counts: pd.DataFrame,
) -> None:
    paths = _chunk_paths(output_dir, chunk_number)
    paths["dir"].mkdir(parents=True, exist_ok=True)
    station_curve.to_parquet(paths["curve"], index=False)
    station_counts.to_parquet(paths["station_counts"], index=False)
    station_day_counts.to_parquet(paths["station_day_counts"], index=False)
    payload = _chunk_metrics_payload(
        chunk_number=chunk_number,
        total_chunks=total_chunks,
        ev_ids=ev_ids,
        chunk_metrics=chunk_metrics,
        station_curve=station_curve,
        station_counts=station_counts,
        station_day_counts=station_day_counts,
    )
    paths["metrics"].write_text(json.dumps(payload, ensure_ascii=True, indent=2), encoding="utf-8")


def _read_chunk_checkpoint(output_dir: Path, chunk_number: int) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, dict]:
    paths = _chunk_paths(output_dir, chunk_number)
    payload = json.loads(paths["metrics"].read_text(encoding="utf-8"))
    return (
        pd.read_parquet(paths["curve"]),
        pd.read_parquet(paths["station_counts"]),
        pd.read_parquet(paths["station_day_counts"]),
        payload,
    )


def _accumulate_chunk_metrics(metrics: CurveRunMetrics, payload: Mapping[str, object]) -> None:
    for key in [
        "failed_vehicle_count",
        "invalid_session_time_count",
        "negative_session_energy_count",
        "negative_power_count",
        "missing_station_id_count",
        "bin_row_count",
        "session_count",
    ]:
        setattr(metrics, key, getattr(metrics, key) + int(payload.get(key, 0) or 0))
    metrics.public_bin_energy_kwh += float(payload.get("public_bin_energy_kwh", 0.0) or 0.0)
    metrics.public_session_energy_kwh += float(payload.get("public_session_energy_kwh", 0.0) or 0.0)


def build_station_summary_2025(
    station_curve: pd.DataFrame,
    station_metadata: pd.DataFrame,
    station_counts: pd.DataFrame,
    *,
    year: int,
) -> pd.DataFrame:
    """Build annual station summary fields for the web index and CSV."""

    summary_columns = [
        "station_id",
        "station_name",
        "latitude",
        "longitude",
        f"total_energy_kwh_{year}",
        f"peak_power_kw_{year}",
        f"peak_time_{year}",
        f"active_days_{year}",
        f"total_sessions_{year}",
        f"unique_vehicles_{year}",
        "average_daily_energy_kwh",
        "max_daily_energy_kwh",
    ]
    if station_curve.empty:
        return pd.DataFrame(columns=summary_columns)

    energy_summary = (
        station_curve.groupby("station_id", as_index=False)
        .agg(
            total_energy=("energy_kwh", "sum"),
            peak_power=("avg_power_kw", "max"),
            active_days=("date", "nunique"),
        )
        .rename(
            columns={
                "total_energy": f"total_energy_kwh_{year}",
                "peak_power": f"peak_power_kw_{year}",
                "active_days": f"active_days_{year}",
            }
        )
    )

    peak_rows = station_curve.loc[station_curve.groupby("station_id")["avg_power_kw"].idxmax()]
    peak_time = peak_rows.loc[:, ["station_id", "time_bin_start"]].rename(
        columns={"time_bin_start": f"peak_time_{year}"}
    )
    peak_time[f"peak_time_{year}"] = pd.to_datetime(peak_time[f"peak_time_{year}"]).dt.strftime(
        "%Y-%m-%dT%H:%M:%S"
    )

    daily_energy = (
        station_curve.groupby(["station_id", "date"], as_index=False)["energy_kwh"]
        .sum()
        .rename(columns={"energy_kwh": "daily_energy_kwh"})
    )
    max_daily = (
        daily_energy.groupby("station_id", as_index=False)["daily_energy_kwh"]
        .max()
        .rename(columns={"daily_energy_kwh": "max_daily_energy_kwh"})
    )

    result = energy_summary.merge(peak_time, on="station_id", how="left").merge(
        max_daily, on="station_id", how="left"
    )
    result["average_daily_energy_kwh"] = result[f"total_energy_kwh_{year}"] / (
        366 if dt.date(year, 12, 31).timetuple().tm_yday == 366 else 365
    )

    if not station_counts.empty:
        counts = station_counts.rename(
            columns={
                "unique_vehicles": f"unique_vehicles_{year}",
                "total_sessions": f"total_sessions_{year}",
            }
        )
        result = result.merge(counts, on="station_id", how="left")
    else:
        result[f"unique_vehicles_{year}"] = 0
        result[f"total_sessions_{year}"] = 0

    metadata_cols = ["station_id", "station_name", "latitude", "longitude"]
    result = result.merge(station_metadata[metadata_cols], on="station_id", how="left")
    for column in [f"unique_vehicles_{year}", f"total_sessions_{year}"]:
        result[column] = result[column].fillna(0).astype(int)

    return result[summary_columns].sort_values("station_id").reset_index(drop=True)


def _json_clean(value):
    if pd.isna(value):
        return None
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating,)):
        return float(value)
    return value


def _station_metadata_payload(station_row: Mapping) -> dict:
    return {
        "station_id": str(station_row.get("station_id")),
        "station_name": _json_clean(station_row.get("station_name")),
        "latitude": _json_clean(station_row.get("latitude")),
        "longitude": _json_clean(station_row.get("longitude")),
        "station_type": _json_clean(station_row.get("station_type")),
        "station_label": _json_clean(station_row.get("station_label")),
        "capacity_kw": _json_clean(station_row.get("total_capacity_kw")),
        "lsoa_code": _json_clean(station_row.get("lsoa_code")),
        "region": _json_clean(station_row.get("region")),
    }


def _safe_path_fragment(value: object) -> str:
    text = str(value)
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", text)


def _write_text_atomic_with_retries(
    path: Path,
    text: str,
    *,
    encoding: str = "utf-8",
    attempts: int = WEB_JSON_WRITE_ATTEMPTS,
    retry_delay_seconds: float = WEB_JSON_WRITE_RETRY_SECONDS,
) -> int:
    """Write text through a sibling temp file and retry transient filesystem timeouts."""

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    retries = 0
    last_error: OSError | None = None
    for attempt in range(1, attempts + 1):
        try:
            tmp.write_text(text, encoding=encoding)
            tmp.replace(path)
            return retries
        except OSError as exc:
            if not isinstance(exc, TimeoutError) and getattr(exc, "errno", None) != errno.ETIMEDOUT:
                raise
            last_error = exc
            retries += 1
            try:
                tmp.unlink()
            except FileNotFoundError:
                pass
            except OSError:
                pass
            if attempt < attempts:
                time.sleep(retry_delay_seconds * attempt)

    if last_error is not None:
        raise last_error
    return retries


def _record_profile(
    profile_rows: list[dict],
    *,
    run_start_perf: float,
    phase_start_perf: float,
    phase: str,
    chunk_number: int | None = None,
    total_chunks: int | None = None,
    vehicle_count: int | None = None,
    details: Mapping[str, object] | None = None,
) -> None:
    """Append and print one coarse profiling event without changing model logic."""

    elapsed_seconds = time.perf_counter() - phase_start_perf
    total_elapsed_seconds = time.perf_counter() - run_start_perf
    clean_details = {
        str(key): _json_clean(value)
        for key, value in (details or {}).items()
    }
    row = {
        "timestamp": pd.Timestamp.now().isoformat(timespec="seconds"),
        "phase": phase,
        "chunk_number": chunk_number,
        "total_chunks": total_chunks,
        "vehicle_count": vehicle_count,
        "elapsed_seconds": round(float(elapsed_seconds), 3),
        "total_elapsed_seconds": round(float(total_elapsed_seconds), 3),
        "details_json": json.dumps(clean_details, ensure_ascii=True, default=str),
    }
    profile_rows.append(row)

    chunk_label = f" chunk {chunk_number}/{total_chunks}" if chunk_number is not None else ""
    vehicle_label = f", vehicles={vehicle_count:,}" if vehicle_count is not None else ""
    detail_label = f", details={clean_details}" if clean_details else ""
    print(
        f"[privatecar-profile]{chunk_label} {phase}: "
        f"{elapsed_seconds:.2f}s elapsed, {total_elapsed_seconds:.2f}s total"
        f"{vehicle_label}{detail_label}",
        flush=True,
    )


def _record_profile_elapsed(
    profile_rows: list[dict],
    *,
    run_start_perf: float,
    elapsed_seconds: float,
    phase: str,
    chunk_number: int | None = None,
    total_chunks: int | None = None,
    vehicle_count: int | None = None,
    details: Mapping[str, object] | None = None,
) -> None:
    phase_start = time.perf_counter() - float(elapsed_seconds)
    _record_profile(
        profile_rows,
        run_start_perf=run_start_perf,
        phase_start_perf=phase_start,
        phase=phase,
        chunk_number=chunk_number,
        total_chunks=total_chunks,
        vehicle_count=vehicle_count,
        details=details,
    )


def _write_profile_log(output_dir: Path, profile_rows: list[dict], *, year: int) -> None:
    if profile_rows:
        pd.DataFrame(profile_rows).to_csv(output_dir / f"profiling_log_{year}.csv", index=False)


def _write_optional_frame(frame: pd.DataFrame, path: Path) -> None:
    if not frame.empty:
        frame.to_csv(path, index=False)


def _value_type_counts(series: pd.Series) -> dict[str, int]:
    counts = series.dropna().map(lambda value: type(value).__name__).value_counts()
    return {str(key): int(value) for key, value in counts.head(20).items()}


def _frame_records(frame: pd.DataFrame, limit: int) -> list[dict]:
    if frame.empty:
        return []
    records = []
    for row in frame.head(limit).to_dict(orient="records"):
        records.append({str(key): _json_clean(value) for key, value in row.items()})
    return records


def _group_missing_concentration(
    frame: pd.DataFrame,
    column: str,
    *,
    top_n: int,
) -> pd.DataFrame:
    if column not in frame.columns or "_is_missing_person_id" not in frame.columns:
        return pd.DataFrame(
            columns=[
                column,
                "vehicle_rows",
                "missing_vehicle_rows",
                "missing_rate",
                "unique_missing_person_ids",
            ]
        )

    group_values = frame[column].astype("string").fillna("missing")
    work = pd.DataFrame(
        {
            column: group_values,
            "_is_missing_person_id": frame["_is_missing_person_id"].astype(bool).to_numpy(),
            "_person_id_norm": frame["_person_id_norm"].astype("string").to_numpy(),
        }
    )
    totals = work.groupby(column, dropna=False).size().rename("vehicle_rows")
    missing = (
        work.loc[work["_is_missing_person_id"]]
        .groupby(column, dropna=False)
        .agg(
            missing_vehicle_rows=("_is_missing_person_id", "size"),
            unique_missing_person_ids=("_person_id_norm", "nunique"),
        )
    )
    result = totals.to_frame().join(missing, how="left").fillna(
        {"missing_vehicle_rows": 0, "unique_missing_person_ids": 0}
    )
    result["missing_vehicle_rows"] = result["missing_vehicle_rows"].astype(int)
    result["unique_missing_person_ids"] = result["unique_missing_person_ids"].astype(int)
    result["missing_rate"] = np.where(
        result["vehicle_rows"] > 0,
        result["missing_vehicle_rows"] / result["vehicle_rows"],
        0.0,
    )
    result = (
        result.loc[result["missing_vehicle_rows"] > 0]
        .sort_values(["missing_vehicle_rows", "missing_rate", "vehicle_rows"], ascending=[False, False, False])
        .head(top_n)
        .reset_index()
    )
    return result


def _add_population_segment_columns(frame: pd.DataFrame) -> pd.DataFrame:
    result = frame.copy()
    if "home_lsoa" not in result.columns:
        if "LSOA_code" in result.columns:
            result["home_lsoa"] = result["LSOA_code"]
        else:
            result["home_lsoa"] = pd.NA

    if "source_file" not in result.columns:
        result["source_file"] = "EV_UK_LSOA_2025_with_energy.csv"

    if "allocation_method" in result.columns and "nts_region" in result.columns:
        result["population_segment"] = (
            result["nts_region"].astype("string").fillna("missing")
            + "|"
            + result["allocation_method"].astype("string").fillna("missing")
        )
    elif "nts_region" in result.columns:
        result["population_segment"] = result["nts_region"].astype("string").fillna("missing")
    else:
        result["population_segment"] = "missing"

    row_number = pd.Series(np.nan, index=result.index, dtype="float64")
    if "ev_id" in result.columns:
        row_number = pd.to_numeric(
            result["ev_id"].astype("string").str.extract(r"(\d+)$", expand=False),
            errors="coerce",
        )
    if row_number.notna().sum() == 0 and "EV_ID" in result.columns:
        row_number = pd.to_numeric(
            result["EV_ID"].astype("string").str.extract(r"(\d+)$", expand=False),
            errors="coerce",
        )
    if row_number.notna().sum() == 0 and "EV_ID_in_row" in result.columns:
        row_number = pd.to_numeric(result["EV_ID_in_row"], errors="coerce")

    if row_number.notna().sum() > 0:
        bucket = pd.Series("missing", index=result.index, dtype="object")
        valid = row_number.notna()
        if valid.any():
            bucket_start = (((row_number.loc[valid].astype(int) - 1) // 100000) * 100000) + 1
            bucket_end = bucket_start + 99999
            bucket.loc[valid] = bucket_start.astype(str) + "-" + bucket_end.astype(str)
        result["ev_id_row_bucket"] = bucket
    else:
        result["ev_id_row_bucket"] = "missing"

    return result


def _missing_person_id_frame(missing_vehicle_rows: pd.DataFrame) -> pd.DataFrame:
    columns = [
        "person_id",
        "affected_vehicle_count",
        "sample_ev_id",
        "sample_home_lsoa",
        "sample_nts_region",
        "sample_vehicle_subtype",
        "sample_allocation_method",
        "sample_model",
    ]
    if missing_vehicle_rows.empty:
        return pd.DataFrame(columns=columns)

    rows = []
    for person_id, group in missing_vehicle_rows.groupby("_person_id_norm", sort=True):
        rows.append(
            {
                "person_id": str(person_id),
                "affected_vehicle_count": int(len(group)),
                "sample_ev_id": _json_clean(group["ev_id"].iloc[0]) if "ev_id" in group.columns else None,
                "sample_home_lsoa": _json_clean(group["home_lsoa"].iloc[0]) if "home_lsoa" in group.columns else None,
                "sample_nts_region": _json_clean(group["nts_region"].iloc[0]) if "nts_region" in group.columns else None,
                "sample_vehicle_subtype": _json_clean(group["vehicle_subtype"].iloc[0])
                if "vehicle_subtype" in group.columns
                else None,
                "sample_allocation_method": _json_clean(group["allocation_method"].iloc[0])
                if "allocation_method" in group.columns
                else None,
                "sample_model": _json_clean(group["Model"].iloc[0]) if "Model" in group.columns else None,
            }
        )
    return pd.DataFrame(rows, columns=columns)


def build_privatecar_person_week_integrity_report(
    person_fleet: pd.DataFrame,
    library_df: pd.DataFrame,
    ev_fleet: pd.DataFrame,
    *,
    scope: str,
    sample_size: int = 20,
    top_n: int = 20,
) -> dict:
    """Compare private-car person IDs against the person-week library IDs."""

    person_raw = person_fleet.copy()
    library_raw = library_df.copy()
    ev_private, subtype_values = _normalise_private_car_ev_fleet(ev_fleet)

    person_raw["_ev_id_norm"] = _normalise_ev_id_series(person_raw["ev_id"]).fillna("")
    person_raw["_person_id_norm"] = _normalise_person_id_series(person_raw["person_id"])
    library_raw["_person_id_norm"] = _normalise_person_id_series(library_raw["person_id"])

    valid_private_ev_ids = set(ev_private["EV_ID"].astype(str))
    cars = person_raw.loc[person_raw["_ev_id_norm"].astype(str).isin(valid_private_ev_ids)].copy()

    ev_columns = [
        column
        for column in [
            "EV_ID",
            "LSOA_code",
            "home_lsoa",
            "LAD",
            "Model",
            "vehicle_subtype",
            "allocation_method",
            "legacy_bus_combined",
            "EV_ID_in_row",
        ]
        if column in ev_private.columns
    ]
    ev_meta = ev_private.loc[:, ev_columns].copy()
    if "EV_ID" in ev_meta.columns:
        ev_meta["_ev_id_norm"] = _normalise_ev_id_series(ev_meta["EV_ID"]).fillna("")
    cars = cars.merge(ev_meta, on="_ev_id_norm", how="left", suffixes=("", "_ev"))
    cars = _add_population_segment_columns(cars)

    raw_private_values = cars["person_id"].dropna().tolist()
    raw_library_values = library_raw["person_id"].dropna().tolist()
    private_person_ids = set(cars["_person_id_norm"].dropna().astype(str))
    library_person_ids = set(library_raw["_person_id_norm"].dropna().astype(str))
    missing_person_ids = sorted(private_person_ids.difference(library_person_ids))
    missing_person_id_set = set(missing_person_ids)
    cars["_is_missing_person_id"] = cars["_person_id_norm"].astype("string").isin(missing_person_id_set)
    missing_vehicle_rows = cars.loc[cars["_is_missing_person_id"]].copy()
    missing_person_ids_df = _missing_person_id_frame(missing_vehicle_rows)

    concentration_columns = [
        "home_lsoa",
        "nts_region",
        "vehicle_subtype",
        "allocation_method",
        "source_file",
        "population_segment",
        "ev_id_row_bucket",
        "LAD",
        "Model",
    ]
    concentration = {
        column: _group_missing_concentration(cars, column, top_n=top_n)
        for column in concentration_columns
    }

    private_person_id_rows = int(cars["_person_id_norm"].notna().sum())
    private_vehicle_rows = int(len(cars))
    library_person_id_rows = int(library_raw["_person_id_norm"].notna().sum())
    missing_vehicle_row_count = int(len(missing_vehicle_rows))
    missing_before_type_normalization = len(set(raw_private_values).difference(set(raw_library_values)))
    missing_after_type_normalization = len(missing_person_ids)
    summary = {
        "scope": scope,
        "private_car_vehicle_rows": private_vehicle_rows,
        "private_car_person_id_rows": private_person_id_rows,
        "private_car_unique_person_ids": int(len(private_person_ids)),
        "person_week_library_person_id_rows": library_person_id_rows,
        "person_week_library_unique_person_ids": int(len(library_person_ids)),
        "missing_person_id_count": int(len(missing_person_ids)),
        "missing_person_id_rate": (
            float(len(missing_person_ids) / len(private_person_ids)) if private_person_ids else 0.0
        ),
        "missing_vehicle_row_count": missing_vehicle_row_count,
        "missing_vehicle_row_rate": (
            float(missing_vehicle_row_count / private_vehicle_rows) if private_vehicle_rows else 0.0
        ),
        "missing_person_id_count_before_type_normalization": int(missing_before_type_normalization),
        "missing_person_id_count_after_type_normalization": int(missing_after_type_normalization),
        "dtype_mismatch_resolved_by_normalization": bool(
            missing_before_type_normalization > 0 and missing_after_type_normalization == 0
        ),
        "dtype_mismatch_reduced_by_normalization": bool(
            missing_before_type_normalization > missing_after_type_normalization
        ),
        "vehicle_subtype_values": subtype_values,
    }
    dtype_checks = {
        "private_car_person_id_raw_dtype": str(person_raw["person_id"].dtype),
        "private_car_person_id_normalized_dtype": str(cars["_person_id_norm"].dtype),
        "person_week_library_person_id_raw_dtype": str(library_raw["person_id"].dtype),
        "person_week_library_person_id_normalized_dtype": str(library_raw["_person_id_norm"].dtype),
        "private_car_person_id_raw_type_counts": _value_type_counts(person_raw["person_id"]),
        "person_week_library_person_id_raw_type_counts": _value_type_counts(library_raw["person_id"]),
    }

    sample_columns = [
        column
        for column in [
            "ev_id",
            "_person_id_norm",
            "nts_household_id",
            "nts_region",
            "home_lsoa",
            "LAD",
            "vehicle_subtype",
            "allocation_method",
            "source_file",
            "population_segment",
            "ev_id_row_bucket",
            "Model",
        ]
        if column in missing_vehicle_rows.columns
    ]
    samples = {
        "missing_person_id_sample": missing_person_ids[:sample_size],
        "missing_vehicle_sample": _frame_records(missing_vehicle_rows.loc[:, sample_columns], sample_size),
    }

    return {
        "summary": summary,
        "dtype_checks": dtype_checks,
        "samples": samples,
        "missing_person_ids": missing_person_ids_df,
        "missing_vehicle_rows": missing_vehicle_rows,
        "concentration": concentration,
    }


def _preflight_json_payload(report: Mapping) -> dict:
    concentration = {
        name: _frame_records(frame, 20)
        for name, frame in report.get("concentration", {}).items()
        if isinstance(frame, pd.DataFrame)
    }
    return {
        "summary": report.get("summary", {}),
        "dtype_checks": report.get("dtype_checks", {}),
        "samples": report.get("samples", {}),
        "concentration_top": concentration,
    }


def _preflight_markdown_lines(report: Mapping, *, year: int, standalone: bool = False) -> list[str]:
    summary = dict(report.get("summary", {}))
    dtype_checks = dict(report.get("dtype_checks", {}))
    samples = dict(report.get("samples", {}))
    concentration = report.get("concentration", {})

    lines = []
    if standalone:
        lines.extend(
            [
                f"# Private Car Station Charging Curves {year} Data Quality Report",
                "",
            ]
        )
    lines.extend(
        [
            "## Referential Integrity Preflight",
            "",
            f"- scope: `{summary.get('scope', 'unknown')}`",
            f"- private car vehicle rows: `{summary.get('private_car_vehicle_rows', 0)}`",
            f"- private car person_id rows: `{summary.get('private_car_person_id_rows', 0)}`",
            f"- private car unique person_id count: `{summary.get('private_car_unique_person_ids', 0)}`",
            f"- person_week_library person_id rows: `{summary.get('person_week_library_person_id_rows', 0)}`",
            f"- person_week_library unique person_id count: `{summary.get('person_week_library_unique_person_ids', 0)}`",
            f"- missing person_id count: `{summary.get('missing_person_id_count', 0)}`",
            f"- missing person_id rate: `{summary.get('missing_person_id_rate', 0.0):.9f}`",
            f"- affected vehicle rows: `{summary.get('missing_vehicle_row_count', 0)}`",
            f"- affected vehicle row rate: `{summary.get('missing_vehicle_row_rate', 0.0):.9f}`",
            "- dtype mismatch resolved by normalization: "
            f"`{_markdown_bool(bool(summary.get('dtype_mismatch_resolved_by_normalization', False)))}`",
            "- dtype mismatch reduced by normalization: "
            f"`{_markdown_bool(bool(summary.get('dtype_mismatch_reduced_by_normalization', False)))}`",
            f"- private car person_id dtype: `{dtype_checks.get('private_car_person_id_raw_dtype', 'unknown')}`",
            f"- person_week_library person_id dtype: `{dtype_checks.get('person_week_library_person_id_raw_dtype', 'unknown')}`",
            f"- missing person_id sample: `{samples.get('missing_person_id_sample', [])}`",
            "",
            "If missing IDs remain after normalization, they are treated as true referential-integrity misses. "
            "The export pipeline records the affected vehicles as failed preflight vehicles and continues.",
        ]
    )

    for name in ["home_lsoa", "nts_region", "vehicle_subtype", "allocation_method", "population_segment", "ev_id_row_bucket"]:
        frame = concentration.get(name) if isinstance(concentration, Mapping) else None
        if isinstance(frame, pd.DataFrame) and not frame.empty:
            lines.extend(
                [
                    "",
                    f"### Missing Concentration By {name}",
                    "",
                    "```text",
                    frame.head(10).to_string(index=False),
                    "```",
                ]
            )
    return lines


def write_preflight_referential_integrity_outputs(
    report: Mapping,
    output_dir: Path | str,
    *,
    year: int,
    max_missing_vehicle_rows_to_write: int = 100000,
) -> None:
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)

    (out / f"preflight_referential_integrity_{year}.json").write_text(
        json.dumps(_preflight_json_payload(report), ensure_ascii=True, indent=2, default=str),
        encoding="utf-8",
    )
    _write_optional_frame(report["missing_person_ids"], out / f"preflight_missing_person_ids_{year}.csv")

    missing_vehicle_rows = report["missing_vehicle_rows"]
    if not missing_vehicle_rows.empty:
        selected_columns = [
            column
            for column in [
                "ev_id",
                "_person_id_norm",
                "nts_household_id",
                "nts_region",
                "home_lsoa",
                "LAD",
                "vehicle_subtype",
                "allocation_method",
                "source_file",
                "population_segment",
                "ev_id_row_bucket",
                "Model",
            ]
            if column in missing_vehicle_rows.columns
        ]
        to_write = missing_vehicle_rows.loc[:, selected_columns].head(max_missing_vehicle_rows_to_write).rename(
            columns={"_person_id_norm": "person_id"}
        )
        to_write.to_csv(out / f"preflight_missing_vehicle_rows_{year}.csv", index=False)

    for name, frame in report.get("concentration", {}).items():
        if isinstance(frame, pd.DataFrame):
            _write_optional_frame(frame, out / f"preflight_missing_by_{name}_{year}.csv")

    markdown = _preflight_markdown_lines(report, year=year, standalone=True)
    (out / "data_quality_report.md").write_text("\n".join(markdown), encoding="utf-8")


def run_preflight_referential_integrity_check(
    *,
    data_dir: Path | str = Path("data"),
    output_dir: Path | str = Path("outputs/privatecar_charging_curves_2025_preflight"),
    year: int = 2025,
    sample_size: int = 20,
    top_n: int = 20,
) -> dict:
    """Run the full private-car person_id vs person_week_library integrity check."""

    data_path = Path(data_dir)
    person_fleet = pd.read_parquet(data_path / "person_fleet.parquet")
    library_df = pd.read_parquet(data_path / "person_week_library.parquet", columns=["person_id"])
    ev_fleet = load_ev_fleet(data_path)
    report = build_privatecar_person_week_integrity_report(
        person_fleet,
        library_df,
        ev_fleet,
        scope="full_private_car_population",
        sample_size=sample_size,
        top_n=top_n,
    )
    write_preflight_referential_integrity_outputs(report, output_dir, year=year)
    return report


def build_station_day_json(
    station_id: str,
    date: str,
    station_curve: pd.DataFrame,
    station_metadata_lookup: Mapping[str, Mapping],
    station_day_counts: pd.DataFrame,
    *,
    year: int,
    timezone: str = "not_specified_in_existing_model",
) -> dict:
    """Build one station-date JSON payload with exactly 96 curve points."""

    full_starts = pd.date_range(date, periods=STEPS_PER_DAY, freq=f"{TIME_RESOLUTION_MINUTES}min")
    full = pd.DataFrame({"time_bin_start": full_starts})
    full["time_bin_end"] = full["time_bin_start"] + pd.to_timedelta(TIME_RESOLUTION_MINUTES, unit="min")

    day_curve = station_curve.loc[
        (station_curve["station_id"].astype(str) == str(station_id)) & (station_curve["date"] == date)
    ].copy()
    if not day_curve.empty:
        day_curve["time_bin_start"] = pd.to_datetime(day_curve["time_bin_start"])
        merged = full.merge(
            day_curve[
                [
                    "time_bin_start",
                    "energy_kwh",
                    "avg_power_kw",
                    "active_vehicle_count",
                    "charging_session_count",
                ]
            ],
            on="time_bin_start",
            how="left",
        )
    else:
        merged = full
        merged["energy_kwh"] = 0.0
        merged["avg_power_kw"] = 0.0
        merged["active_vehicle_count"] = 0
        merged["charging_session_count"] = 0

    for column in ["energy_kwh", "avg_power_kw"]:
        merged[column] = merged[column].fillna(0.0).astype(float)
    for column in ["active_vehicle_count", "charging_session_count"]:
        merged[column] = merged[column].fillna(0).astype(int)

    curve = [
        {
            "time_bin_start": row.time_bin_start.strftime("%Y-%m-%dT%H:%M:%S"),
            "time_bin_end": row.time_bin_end.strftime("%Y-%m-%dT%H:%M:%S"),
            "time": row.time_bin_start.strftime("%H:%M"),
            "time_label": row.time_bin_start.strftime("%H:%M"),
            "energy_kwh": round(float(row.energy_kwh), 6),
            "avg_power_kw": round(float(row.avg_power_kw), 6),
            "active_vehicle_count": int(row.active_vehicle_count),
            "charging_session_count": int(row.charging_session_count),
        }
        for row in merged.itertuples(index=False)
    ]

    day_count_hit = station_day_counts.loc[
        (station_day_counts["station_id"].astype(str) == str(station_id))
        & (station_day_counts["date"] == date)
    ]
    unique_vehicles = int(day_count_hit["unique_vehicles"].iloc[0]) if not day_count_hit.empty else 0
    total_sessions = int(day_count_hit["total_sessions"].iloc[0]) if not day_count_hit.empty else 0
    peak_idx = int(merged["avg_power_kw"].idxmax()) if len(merged) else 0
    peak_power = float(merged.loc[peak_idx, "avg_power_kw"]) if len(merged) else 0.0
    peak_time = (
        merged.loc[peak_idx, "time_bin_start"].strftime("%Y-%m-%dT%H:%M:%S")
        if peak_power > 0.0
        else None
    )

    station_row = station_metadata_lookup.get(str(station_id), {"station_id": str(station_id)})
    station_payload = _station_metadata_payload(station_row)
    return {
        "schema_version": SCHEMA_VERSION,
        "scope": SCOPE,
        "year": year,
        "study_year": year,
        "date": date,
        "timezone": timezone,
        "time_resolution_minutes": TIME_RESOLUTION_MINUTES,
        "charging_strategy": CHARGING_STRATEGY,
        "queue_model": QUEUE_MODEL,
        "station_id": station_payload["station_id"],
        "station_name": station_payload["station_name"],
        "latitude": station_payload["latitude"],
        "longitude": station_payload["longitude"],
        "station": station_payload,
        "curve": curve,
        "summary": {
            "daily_energy_kwh": round(float(merged["energy_kwh"].sum()), 6),
            "daily_peak_power_kw": round(peak_power, 6),
            "daily_peak_time": peak_time,
            "daily_active_vehicle_count": unique_vehicles,
            "daily_session_count": total_sessions,
        },
    }


def build_station_index_json(
    station_curve: pd.DataFrame,
    station_summary: pd.DataFrame,
    station_metadata_lookup: Mapping[str, Mapping],
    *,
    year: int,
    timezone: str = "not_specified_in_existing_model",
) -> dict:
    """Build the station index JSON payload."""

    stations = []
    if station_curve.empty:
        available_dates_by_station: dict[str, list[str]] = {}
    else:
        available_dates_by_station = (
            station_curve.groupby("station_id")["date"]
            .apply(lambda values: sorted(values.astype(str).unique().tolist()))
            .to_dict()
        )

    summary_lookup = (
        station_summary.set_index("station_id", drop=False).to_dict("index")
        if not station_summary.empty
        else {}
    )
    for station_id, available_dates in sorted(available_dates_by_station.items(), key=lambda item: item[0]):
        metadata = station_metadata_lookup.get(str(station_id), {"station_id": str(station_id)})
        summary = summary_lookup.get(str(station_id), {})
        total_energy_kwh = round(float(summary.get(f"total_energy_kwh_{year}", 0.0) or 0.0), 6)
        peak_power_kw = round(float(summary.get(f"peak_power_kw_{year}", 0.0) or 0.0), 6)
        peak_time = _json_clean(summary.get(f"peak_time_{year}"))
        stations.append(
            {
                **_station_metadata_payload(metadata),
                "available_dates": available_dates,
                "total_energy_kwh": total_energy_kwh,
                "peak_power_kw": peak_power_kw,
                "peak_time": peak_time,
                f"total_energy_kwh_{year}": total_energy_kwh,
                f"peak_power_kw_{year}": peak_power_kw,
                f"peak_time_{year}": peak_time,
            }
        )

    return {
        "schema_version": SCHEMA_VERSION,
        "scope": SCOPE,
        "year": year,
        "study_year": year,
        "timezone": timezone,
        "time_resolution_minutes": TIME_RESOLUTION_MINUTES,
        "charging_strategy": CHARGING_STRATEGY,
        "queue_model": QUEUE_MODEL,
        "stations": stations,
    }


def export_web_json_files(
    station_curve: pd.DataFrame,
    station_summary: pd.DataFrame,
    station_metadata: pd.DataFrame,
    station_day_counts: pd.DataFrame,
    output_dir: Path | str,
    *,
    year: int,
    timezone: str = "not_specified_in_existing_model",
    station_ids: Iterable[str] | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    json_indent: int | None = 2,
    validate_written_json: bool = True,
) -> dict:
    """Write station index and per-station daily curve JSON files."""

    out = Path(output_dir)
    web_dir = out / "web"
    daily_root = web_dir / "daily_curves"
    daily_root.mkdir(parents=True, exist_ok=True)
    metadata_lookup = station_metadata.set_index("station_id", drop=False).to_dict("index")

    export_curve = station_curve.copy()
    export_day_counts = station_day_counts.copy()
    if station_ids is not None:
        requested_station_ids = {str(station_id) for station_id in station_ids}
        export_curve = export_curve.loc[export_curve["station_id"].astype(str).isin(requested_station_ids)].copy()
        if not export_day_counts.empty:
            export_day_counts = export_day_counts.loc[
                export_day_counts["station_id"].astype(str).isin(requested_station_ids)
            ].copy()
    if date_from is not None:
        export_curve = export_curve.loc[export_curve["date"].astype(str) >= str(date_from)].copy()
        if not export_day_counts.empty:
            export_day_counts = export_day_counts.loc[export_day_counts["date"].astype(str) >= str(date_from)].copy()
    if date_to is not None:
        export_curve = export_curve.loc[export_curve["date"].astype(str) <= str(date_to)].copy()
        if not export_day_counts.empty:
            export_day_counts = export_day_counts.loc[export_day_counts["date"].astype(str) <= str(date_to)].copy()

    json_count = 0
    parse_failures = 0
    write_retries = 0
    station_dates_with_96 = 0
    station_dates_without_96 = 0

    if not export_curve.empty:
        station_dates = (
            export_curve.loc[:, ["station_id", "date"]]
            .drop_duplicates()
            .sort_values(["station_id", "date"])
        )
        day_curve_lookup = {
            (str(station_id), str(date)): day_curve
            for (station_id, date), day_curve in export_curve.groupby(["station_id", "date"], sort=False)
        }
        for row in station_dates.itertuples(index=False):
            station_id = str(row.station_id)
            date = str(row.date)
            day_curve = day_curve_lookup[(station_id, date)]
            payload = build_station_day_json(
                station_id,
                date,
                day_curve,
                metadata_lookup,
                export_day_counts,
                year=year,
                timezone=timezone,
            )
            if len(payload["curve"]) == STEPS_PER_DAY:
                station_dates_with_96 += 1
            else:
                station_dates_without_96 += 1
            station_dir = daily_root / _safe_path_fragment(station_id)
            target = station_dir / f"{date}.json"
            write_retries += _write_text_atomic_with_retries(
                target,
                json.dumps(payload, ensure_ascii=True, indent=json_indent),
                encoding="utf-8",
            )
            json_count += 1
            if validate_written_json:
                try:
                    json.loads(target.read_text(encoding="utf-8"))
                except json.JSONDecodeError:
                    parse_failures += 1

    station_index = build_station_index_json(
        export_curve,
        station_summary,
        metadata_lookup,
        year=year,
        timezone=timezone,
    )
    index_path = web_dir / "station_index.json"
    write_retries += _write_text_atomic_with_retries(
        index_path,
        json.dumps(station_index, ensure_ascii=True, indent=json_indent),
        encoding="utf-8",
    )
    json_count += 1
    if validate_written_json:
        try:
            json.loads(index_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            parse_failures += 1

    return {
        "json_file_count": json_count,
        "json_parse_failures": parse_failures,
        "station_dates_with_96_points": station_dates_with_96,
        "station_dates_without_96_points": station_dates_without_96,
        "json_write_retry_count": write_retries,
        "web_json_station_filter_count": 0 if station_ids is None else len(set(map(str, station_ids))),
        "web_json_date_from": date_from,
        "web_json_date_to": date_to,
    }


def export_analysis_files(
    station_curve: pd.DataFrame,
    station_summary: pd.DataFrame,
    station_metadata: pd.DataFrame,
    output_dir: Path | str,
    *,
    year: int,
    station_counts: pd.DataFrame | None = None,
    station_day_counts: pd.DataFrame | None = None,
) -> None:
    """Write parquet/csv analysis outputs and station metadata JSON."""

    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    curve_for_write = station_curve.copy()
    if not curve_for_write.empty:
        curve_for_write["time_bin_start"] = pd.to_datetime(curve_for_write["time_bin_start"])
        curve_for_write["time_bin_end"] = pd.to_datetime(curve_for_write["time_bin_end"])
    curve_for_write.to_parquet(out / f"station_charging_curve_15min_{year}.parquet", index=False)
    curve_for_write.to_csv(out / f"station_charging_curve_15min_{year}.csv", index=False)
    station_summary.to_csv(out / f"station_summary_{year}.csv", index=False)
    if station_counts is not None:
        station_counts.to_parquet(out / f"station_counts_{year}.parquet", index=False)
    if station_day_counts is not None:
        station_day_counts.to_parquet(out / f"station_day_counts_{year}.parquet", index=False)

    payload = {
        "schema_version": SCHEMA_VERSION,
        "scope": SCOPE,
        "year": year,
        "study_year": year,
        "stations": [
            _station_metadata_payload(row._asdict())
            for row in station_metadata.itertuples(index=False)
        ],
    }
    (out / f"station_metadata_{year}.json").write_text(
        json.dumps(payload, ensure_ascii=True, indent=2),
        encoding="utf-8",
    )


def _markdown_bool(value: bool) -> str:
    return "yes" if value else "no"


def write_data_quality_report(
    output_dir: Path | str,
    *,
    inventory: pd.DataFrame,
    metrics: CurveRunMetrics,
    station_curve: pd.DataFrame,
    station_metadata: pd.DataFrame,
    station_summary: pd.DataFrame,
    year: int,
    preflight_report: Mapping | None = None,
) -> None:
    """Write a markdown data quality and reuse report."""

    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)

    energy_diff_session_bin = abs(metrics.public_session_energy_kwh - metrics.public_bin_energy_kwh)
    energy_diff_bin_station = abs(metrics.public_bin_energy_kwh - metrics.station_curve_energy_kwh)
    station_ids = set(station_curve["station_id"].astype(str)) if not station_curve.empty else set()
    metadata_ids = set(station_metadata["station_id"].astype(str))
    unmatched_ids = sorted(station_ids.difference(metadata_ids))
    severe_errors: list[str] = []
    if metrics.failed_vehicle_count:
        severe_errors.append(f"failed vehicles: {metrics.failed_vehicle_count}")
    if metrics.invalid_session_time_count:
        severe_errors.append(f"invalid session times: {metrics.invalid_session_time_count}")
    if metrics.negative_session_energy_count:
        severe_errors.append(f"negative session energies: {metrics.negative_session_energy_count}")
    if metrics.negative_power_count:
        severe_errors.append(f"negative charging powers: {metrics.negative_power_count}")
    if metrics.json_parse_failures:
        severe_errors.append(f"JSON parse failures: {metrics.json_parse_failures}")
    if metrics.station_dates_without_96_points:
        severe_errors.append(f"station-date JSON files without 96 points: {metrics.station_dates_without_96_points}")
    if unmatched_ids:
        severe_errors.append(f"station IDs missing from metadata: {len(unmatched_ids)}")

    lines = [
        f"# Private Car Station Charging Curves {year} Data Quality Report",
        "",
        "## Run Scope",
        "",
        f"- scope: `{SCOPE}`",
        f"- study_year: `{year}`",
        f"- time_resolution_minutes: `{TIME_RESOLUTION_MINUTES}`",
        f"- charging_strategy: `{CHARGING_STRATEGY}`",
        f"- queue_model: `{QUEUE_MODEL}`",
            f"- timezone: `{metrics.timezone}`",
            f"- private vehicles available: `{metrics.private_vehicle_count_available}`",
            f"- private vehicles processed in this run: `{metrics.private_vehicle_count_run}`",
            f"- failed vehicles: `{metrics.failed_vehicle_count}`",
            f"- sample run: `{_markdown_bool(metrics.is_sample)}`",
        "",
        "## Inventory Summary",
        "",
        "| Item | Exists | Variable / File / Function | Reuse | Notes |",
        "|---|---:|---|---:|---|",
    ]
    for row in inventory.itertuples(index=False):
        lines.append(
            f"| {row.item} | {_markdown_bool(bool(row.exists))} | `{row.variable_file_function}` | "
            f"{_markdown_bool(bool(row.reuse))} | {row.notes} |"
        )

    lines.extend(
        [
            "",
            "## Reuse And New Code",
            "",
            "| Module | Reuse Existing Result | Source | New Code | Notes |",
            "|---|---:|---|---:|---|",
            "| private car data | yes | `person_fleet.parquet`, `EV_UK_LSOA_2025_with_energy.csv` | no | Filtered to existing car subtype where available. |",
            "| charging demand | yes | `simulate_single_ev` backfilled `ParkingEvent.energy_charged_kwh` | no | No separate requested-energy table found. |",
            "| charging power | yes | `match_stations_for_schedule` sets `ParkingEvent.charge_power_kw` | no | Existing station and vehicle AC constraints reused. |",
            "| uncontrolled sessions | yes | `simulate_single_ev` and `ParkingEvent` | no | Existing plug-in-immediately simulator reused. |",
            "| session-to-bin mapping | partial | existing vehicle `load_profile[96]` | yes | New attribution from vehicle bins to public station IDs. |",
            "| station aggregation | no | n/a | yes | New station-level group-by. |",
            "| web JSON export | no | n/a | yes | New station index and station-date JSON files. |",
            "",
            "## Private Car Checks",
            "",
            f"- vehicle_id missing in processed person_fleet: checked during load; processed count `{metrics.private_vehicle_count_run}`.",
            "- non-private vehicle exclusion: EV fleet `vehicle_subtype` is filtered to car/private-car values when present.",
            "- home charging events have no public `station_id` in the existing model and are not exported as station curves.",
        "",
        ]
    )

    if preflight_report is not None:
        lines.extend(_preflight_markdown_lines(preflight_report, year=year))
        lines.append("")

    lines.extend(
        [
            "## Charging Session Checks",
            "",
            f"- sessions with station load: `{metrics.session_count}`",
            f"- invalid session time count: `{metrics.invalid_session_time_count}`",
            f"- negative session energy count: `{metrics.negative_session_energy_count}`",
            f"- negative charging power count: `{metrics.negative_power_count}`",
            f"- charging events without a public station_id count: `{metrics.missing_station_id_count}`",
            f"- skipped sessions not exported to public station curves: `{metrics.missing_station_id_count}`",
            "- uncontrolled charging confirmed: existing simulator module docstring and function name are used.",
            "",
            "## Session-To-Bin Checks",
            "",
            f"- time resolution: `{TIME_RESOLUTION_MINUTES}` minutes",
            f"- study year: `{year}`",
            f"- session-bin rows: `{metrics.bin_row_count}`",
            f"- session energy kWh: `{metrics.public_session_energy_kwh:.9f}`",
            f"- bin energy kWh: `{metrics.public_bin_energy_kwh:.9f}`",
            f"- absolute session/bin energy difference kWh: `{energy_diff_session_bin:.9f}`",
            "- cross-bin sessions are split by 15-minute overlap/share; rows are not assigned only to session start.",
            "",
            "## Station Aggregation Checks",
            "",
            f"- station curve rows: `{metrics.station_curve_row_count}`",
            f"- station summary rows: `{metrics.station_summary_row_count}`",
            f"- station curve energy kWh: `{metrics.station_curve_energy_kwh:.9f}`",
            f"- absolute bin/station energy difference kWh: `{energy_diff_bin_station:.9f}`",
            f"- station-date pairs exported: `{metrics.station_date_count}`",
            f"- station IDs missing from metadata: `{len(unmatched_ids)}`",
            f"- station metadata rows: `{metrics.station_metadata_count}`",
            f"- station metadata missing generated/display station_name: `{metrics.station_metadata_missing_name_count}`",
            f"- station metadata missing latitude: `{metrics.station_metadata_missing_latitude_count}`",
            f"- station metadata missing longitude: `{metrics.station_metadata_missing_longitude_count}`",
            "",
            "## Web JSON Checks",
            "",
            f"- JSON files written: `{metrics.json_file_count}`",
            f"- JSON parse failures: `{metrics.json_parse_failures}`",
            f"- station-date JSON files with 96 points: `{metrics.station_dates_with_96_points}`",
            f"- station-date JSON files without 96 points: `{metrics.station_dates_without_96_points}`",
            "- `avg_power_kw` is present in every curve point generated by `build_station_day_json`.",
            "- time labels are generated from `00:00` through `23:45` for each 96-point payload.",
            "",
            "## Severe Error Checks",
            "",
            f"- severe error count: `{len(severe_errors)}`",
            f"- severe errors: `{'; '.join(severe_errors) if severe_errors else 'none'}`",
            "",
            "## Remaining Confirmations",
            "",
            "- The existing private-car model does not specify a timezone in persisted inputs; JSON metadata records `not_specified_in_existing_model`.",
            "- Home charging is excluded from station-level public charger JSON because home events have `matched_station_id = None` in the existing model.",
            "- Full-fleet execution is large: the local private-car binding has more than one million EV rows, so smoke runs should be labelled with `--max-vehicles`.",
        ]
    )

    if metrics.notes:
        lines.extend(["", "## Notes", ""])
        for note in metrics.notes:
            lines.append(f"- {note}")

    if not station_summary.empty:
        snapshot = station_summary.sort_values(f"peak_power_kw_{year}", ascending=False).head(10)
        lines.extend(
            [
                "",
                "## Peak Power Sanity Snapshot",
                "",
                "```text",
                snapshot.to_string(index=False),
                "```",
            ]
        )

    (out / "data_quality_report.md").write_text("\n".join(lines), encoding="utf-8")


def run_privatecar_station_curve_pipeline(
    *,
    data_dir: Path | str = Path("data"),
    output_dir: Path | str = Path("outputs/privatecar_charging_curves_2025"),
    destination_table_path: Path | str | None = None,
    destination_cache_mode: str = "origin",
    year: int = 2025,
    max_vehicles: int | None = None,
    chunk_size: int = 100,
    main_seed: int = MAIN_CAR_SEED,
    warmup_seed: int = ALT_CAR_SEED,
    write_web_json: bool = True,
    web_station_ids: Iterable[str] | None = None,
    web_date_from: str | None = None,
    web_date_to: str | None = None,
    web_json_indent: int | None = 2,
    validate_written_json: bool = True,
    checkpoint_chunks: bool = True,
    resume: bool = False,
    progress_interval: int = 0,
) -> dict:
    """Run the complete private-car station curve export pipeline."""

    data_path = Path(data_dir)
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    if chunk_size <= 0:
        raise ValueError("chunk_size must be positive")
    profile_rows: list[dict] = []
    vehicle_schedule_profile_rows: list[dict] = []
    run_start_perf = time.perf_counter()

    destination_path = (
        Path(destination_table_path)
        if destination_table_path is not None
        else Path(__file__).resolve().parents[3]
        / "Data"
        / "Charging_stations"
        / "OSM_POI_Labeling"
        / "destination_choice_table.parquet"
    )

    phase_start = time.perf_counter()
    inventory = inspect_existing_notebook_assets()
    _record_profile(
        profile_rows,
        run_start_perf=run_start_perf,
        phase_start_perf=phase_start,
        phase="inspect_existing_notebook_assets",
    )

    phase_start = time.perf_counter()
    person_fleet, ev_fleet, private_meta = load_existing_private_car_data(
        data_path,
        max_vehicles=max_vehicles,
    )
    _record_profile(
        profile_rows,
        run_start_perf=run_start_perf,
        phase_start_perf=phase_start,
        phase="load_private_car_data",
        vehicle_count=int(private_meta["private_vehicle_count_run"]),
        details={
            "private_vehicle_count_available": int(private_meta["private_vehicle_count_available"]),
            "max_vehicles": max_vehicles,
        },
    )

    phase_start = time.perf_counter()
    library_df_raw = pd.read_parquet(data_path / "person_week_library.parquet")
    library_df = library_df_raw.copy()
    library_df["person_id"] = _normalise_person_id_series(library_df["person_id"]).fillna("")
    stations_raw = pd.read_csv(data_path / "UK_OCM_stations_labeled.csv")
    station_metadata = load_existing_station_metadata(data_path)
    centroids = load_lsoa_centroids()
    sampler = LazyDestinationSampler(
        destination_path,
        centroids=centroids,
        cache_mode=destination_cache_mode,
    )
    _record_profile(
        profile_rows,
        run_start_perf=run_start_perf,
        phase_start_perf=phase_start,
        phase="load_reference_data",
        details={
            "library_rows": len(library_df),
            "station_rows": len(stations_raw),
            "station_metadata_rows": len(station_metadata),
            "centroid_rows": len(centroids),
            "destination_cache_mode": destination_cache_mode,
        },
    )

    phase_start = time.perf_counter()
    preflight_report = build_privatecar_person_week_integrity_report(
        person_fleet,
        library_df_raw,
        ev_fleet,
        scope="pipeline_run",
    )
    write_preflight_referential_integrity_outputs(preflight_report, out, year=year)
    _record_profile(
        profile_rows,
        run_start_perf=run_start_perf,
        phase_start_perf=phase_start,
        phase="preflight_referential_integrity_check",
        vehicle_count=int(len(person_fleet)),
        details=preflight_report["summary"],
    )

    phase_start = time.perf_counter()
    library_index = build_library_index(library_df)
    leisure_pool_index = build_leisure_pool_index(library_df, library_index=library_index)
    _record_profile(
        profile_rows,
        run_start_perf=run_start_perf,
        phase_start_perf=phase_start,
        phase="build_week_pattern_indices",
        details={
            "library_index_persons": len(library_index),
            "leisure_pool_persons": len(leisure_pool_index),
        },
    )

    failed_vehicle_rows: list[dict] = []
    valid_person_ids = set(library_index)
    missing_person_mask = ~person_fleet["person_id"].astype(str).isin(valid_person_ids)
    if missing_person_mask.any():
        missing_person_rows = person_fleet.loc[missing_person_mask].copy()
        missing_details = preflight_report.get("missing_vehicle_rows", pd.DataFrame())
        if isinstance(missing_details, pd.DataFrame) and not missing_details.empty and "ev_id" in missing_details.columns:
            missing_details_by_ev = missing_details.set_index("ev_id", drop=False).to_dict("index")
        else:
            missing_details_by_ev = {}
        for row in missing_person_rows.itertuples(index=False):
            detail = missing_details_by_ev.get(str(row.ev_id), {})
            failed_vehicle_rows.append(
                {
                    "ev_id": str(row.ev_id),
                    "person_id": str(row.person_id),
                    "home_lsoa": _json_clean(detail.get("home_lsoa")),
                    "nts_region": _json_clean(getattr(row, "nts_region", detail.get("nts_region", None))),
                    "vehicle_subtype": _json_clean(detail.get("vehicle_subtype")),
                    "allocation_method": _json_clean(detail.get("allocation_method")),
                    "population_segment": _json_clean(detail.get("population_segment")),
                    "source_file": _json_clean(detail.get("source_file")),
                    "failure_stage": "preflight_person_week_library",
                    "failure_reason": "person_id not found in person_week_library",
                }
            )
        person_fleet = person_fleet.loc[~missing_person_mask].copy()
        run_ev_ids = person_fleet["ev_id"].astype(str).tolist()
        ev_fleet = ev_fleet.loc[ev_fleet["EV_ID"].astype(str).isin(run_ev_ids)].copy()
        if run_ev_ids:
            ev_fleet = ev_fleet.set_index("EV_ID", drop=False).loc[run_ev_ids].reset_index(drop=True)
        _record_profile(
            profile_rows,
            run_start_perf=run_start_perf,
            phase_start_perf=time.perf_counter(),
            phase="preflight_failed_vehicle_filter",
            vehicle_count=len(failed_vehicle_rows),
            details={
                "failed_vehicle_count": len(failed_vehicle_rows),
                "remaining_vehicle_count": len(person_fleet),
            },
        )
    if failed_vehicle_rows:
        pd.DataFrame(failed_vehicle_rows).to_csv(out / f"failed_vehicles_{year}.csv", index=False)

    metrics = CurveRunMetrics(
        study_year=year,
        private_vehicle_count_available=int(private_meta["private_vehicle_count_available"]),
        private_vehicle_count_run=int(len(person_fleet)),
        failed_vehicle_count=len(failed_vehicle_rows),
        vehicle_limit=max_vehicles,
        chunk_size=chunk_size,
        station_metadata_count=int(len(station_metadata)),
        station_metadata_missing_name_count=int(station_metadata["station_name"].isna().sum()),
        station_metadata_missing_latitude_count=int(station_metadata["latitude"].isna().sum()),
        station_metadata_missing_longitude_count=int(station_metadata["longitude"].isna().sum()),
    )

    curve_frames: list[pd.DataFrame] = []
    station_count_frames: list[pd.DataFrame] = []
    station_day_count_frames: list[pd.DataFrame] = []

    total_chunks = math.ceil(len(person_fleet) / chunk_size)
    for chunk_number, person_chunk in enumerate(_chunk_frame(person_fleet, chunk_size), start=1):
        ev_ids = person_chunk["ev_id"].astype(str).tolist()
        ev_chunk = ev_fleet.set_index("EV_ID", drop=False).loc[ev_ids].reset_index(drop=True)
        print(
            f"[privatecar] chunk {chunk_number}/{total_chunks}: {len(person_chunk):,} vehicles",
            flush=True,
        )

        chunk_start = time.perf_counter()
        if resume and checkpoint_chunks and _chunk_checkpoint_exists(out, chunk_number):
            phase_start = time.perf_counter()
            curve, station_counts_chunk, station_day_counts_chunk, payload = _read_chunk_checkpoint(out, chunk_number)
            if not curve.empty:
                curve_frames.append(curve)
            if not station_counts_chunk.empty:
                station_count_frames.append(station_counts_chunk)
            if not station_day_counts_chunk.empty:
                station_day_count_frames.append(station_day_counts_chunk)
            _accumulate_chunk_metrics(metrics, payload)
            _record_profile(
                profile_rows,
                run_start_perf=run_start_perf,
                phase_start_perf=phase_start,
                phase="load_chunk_checkpoint",
                chunk_number=chunk_number,
                total_chunks=total_chunks,
                vehicle_count=len(person_chunk),
                details={
                    "station_curve_rows": len(curve),
                    "station_count_rows": len(station_counts_chunk),
                    "station_day_count_rows": len(station_day_counts_chunk),
                },
            )
            _record_profile(
                profile_rows,
                run_start_perf=run_start_perf,
                phase_start_perf=chunk_start,
                phase="chunk_total",
                chunk_number=chunk_number,
                total_chunks=total_chunks,
                vehicle_count=len(person_chunk),
                details={"resumed_from_checkpoint": True},
            )
            continue

        phase_start = time.perf_counter()
        home_map = _home_lsoa_map(ev_chunk)
        sampler_before_preload = sampler.stats_snapshot()
        if hasattr(sampler, "preload_origins"):
            sampler.preload_origins(home_map.values())
        preload_delta = sampler.diff_stats(sampler.stats_snapshot(), sampler_before_preload)
        _record_profile(
            profile_rows,
            run_start_perf=run_start_perf,
            phase_start_perf=phase_start,
            phase="preload_destination_origins",
            chunk_number=chunk_number,
            total_chunks=total_chunks,
            vehicle_count=len(person_chunk),
            details=preload_delta,
        )

        phase_start = time.perf_counter()
        sampler_before_schedule = sampler.stats_snapshot()
        schedules = build_year_schedules_with_warmup(
            person_chunk,
            ev_chunk,
            library_df,
            year=year,
            sampler=sampler,
            main_seed=main_seed,
            warmup_seed=warmup_seed,
            progress_interval=progress_interval,
            library_index_cache=library_index,
            leisure_pool_index_cache=leisure_pool_index,
            schedule_profile_callback=lambda row, chunk_number=chunk_number: vehicle_schedule_profile_rows.append(
                {
                    **row,
                    "chunk_number": chunk_number,
                    "total_chunks": total_chunks,
                }
            ),
        )
        schedule_sampler_delta = sampler.diff_stats(sampler.stats_snapshot(), sampler_before_schedule)
        _record_profile(
            profile_rows,
            run_start_perf=run_start_perf,
            phase_start_perf=phase_start,
            phase="build_year_schedules_with_warmup",
            chunk_number=chunk_number,
            total_chunks=total_chunks,
            vehicle_count=len(person_chunk),
            details={
                "schedule_days_including_warmup": sum(len(value) for value in schedules.values()),
                **{f"destination_{key}": value for key, value in schedule_sampler_delta.items()},
            },
        )
        _record_profile_elapsed(
            profile_rows,
            run_start_perf=run_start_perf,
            elapsed_seconds=float(schedule_sampler_delta.get("query_seconds", 0.0) or 0.0),
            phase="destination_lookup_inside_schedule",
            chunk_number=chunk_number,
            total_chunks=total_chunks,
            vehicle_count=len(person_chunk),
            details=schedule_sampler_delta,
        )

        phase_start = time.perf_counter()
        match_schedules_to_stations(
            schedules,
            ev_chunk,
            stations_raw,
            centroids=centroids,
        )
        _record_profile(
            profile_rows,
            run_start_perf=run_start_perf,
            phase_start_perf=phase_start,
            phase="match_schedules_to_stations",
            chunk_number=chunk_number,
            total_chunks=total_chunks,
            vehicle_count=len(person_chunk),
        )

        phase_start = time.perf_counter()
        bin_df, session_df, chunk_metrics = load_or_build_session_time_bins_15min(
            schedules,
            ev_chunk,
            year=year,
        )
        _record_profile(
            profile_rows,
            run_start_perf=run_start_perf,
            phase_start_perf=phase_start,
            phase="simulate_single_ev_and_build_session_bins",
            chunk_number=chunk_number,
            total_chunks=total_chunks,
            vehicle_count=len(person_chunk),
            details={
                "session_bin_rows": len(bin_df),
                "session_rows": len(session_df),
                "public_bin_energy_kwh": round(chunk_metrics.public_bin_energy_kwh, 6),
            },
        )

        phase_start = time.perf_counter()
        curve = aggregate_station_curves_15min(bin_df)
        station_counts_chunk = _chunk_count_frame(session_df)
        station_day_counts_chunk = _chunk_day_count_frame(session_df)
        if not curve.empty:
            curve_frames.append(curve)
        if not station_counts_chunk.empty:
            station_count_frames.append(station_counts_chunk)
        if not station_day_counts_chunk.empty:
            station_day_count_frames.append(station_day_counts_chunk)
        payload = _chunk_metrics_payload(
            chunk_number=chunk_number,
            total_chunks=total_chunks,
            ev_ids=ev_ids,
            chunk_metrics=chunk_metrics,
            station_curve=curve,
            station_counts=station_counts_chunk,
            station_day_counts=station_day_counts_chunk,
        )
        _accumulate_chunk_metrics(metrics, payload)
        _record_profile(
            profile_rows,
            run_start_perf=run_start_perf,
            phase_start_perf=phase_start,
            phase="aggregate_chunk_station_curves",
            chunk_number=chunk_number,
            total_chunks=total_chunks,
            vehicle_count=len(person_chunk),
            details={
                "station_curve_rows": len(curve),
                "active_stations": curve["station_id"].nunique() if not curve.empty else 0,
            },
        )
        if checkpoint_chunks:
            phase_start = time.perf_counter()
            _write_chunk_checkpoint(
                out,
                chunk_number=chunk_number,
                total_chunks=total_chunks,
                ev_ids=ev_ids,
                chunk_metrics=chunk_metrics,
                station_curve=curve,
                station_counts=station_counts_chunk,
                station_day_counts=station_day_counts_chunk,
            )
            _record_profile(
                profile_rows,
                run_start_perf=run_start_perf,
                phase_start_perf=phase_start,
                phase="write_chunk_checkpoint",
                chunk_number=chunk_number,
                total_chunks=total_chunks,
                vehicle_count=len(person_chunk),
                details={
                    "station_curve_rows": len(curve),
                    "station_count_rows": len(station_counts_chunk),
                    "station_day_count_rows": len(station_day_counts_chunk),
                },
            )
        _record_profile(
            profile_rows,
            run_start_perf=run_start_perf,
            phase_start_perf=chunk_start,
            phase="chunk_total",
            chunk_number=chunk_number,
            total_chunks=total_chunks,
            vehicle_count=len(person_chunk),
        )

    phase_start = time.perf_counter()
    station_curve = _combine_station_curves(curve_frames)
    station_counts = _combine_count_frames(station_count_frames, ["station_id"])
    station_day_counts = _combine_count_frames(station_day_count_frames, ["station_id", "date"])
    _record_profile(
        profile_rows,
        run_start_perf=run_start_perf,
        phase_start_perf=phase_start,
        phase="combine_chunk_outputs",
        details={
            "station_curve_rows": len(station_curve),
            "station_count_rows": len(station_counts),
            "station_day_count_rows": len(station_day_counts),
        },
    )

    phase_start = time.perf_counter()
    station_summary = build_station_summary_2025(
        station_curve,
        station_metadata,
        station_counts,
        year=year,
    )
    _record_profile(
        profile_rows,
        run_start_perf=run_start_perf,
        phase_start_perf=phase_start,
        phase="build_station_summary",
        details={"station_summary_rows": len(station_summary)},
    )

    metrics.station_curve_row_count = int(len(station_curve))
    metrics.station_summary_row_count = int(len(station_summary))
    metrics.station_curve_energy_kwh = float(station_curve["energy_kwh"].sum()) if not station_curve.empty else 0.0
    metrics.station_date_count = (
        int(station_curve.loc[:, ["station_id", "date"]].drop_duplicates().shape[0])
        if not station_curve.empty
        else 0
    )
    if not station_curve.empty:
        metadata_ids = set(station_metadata["station_id"].astype(str))
        metrics.unmatched_station_metadata_count = int(
            (~station_curve["station_id"].astype(str).isin(metadata_ids)).sum()
        )

    phase_start = time.perf_counter()
    export_analysis_files(
        station_curve,
        station_summary,
        station_metadata,
        out,
        year=year,
        station_counts=station_counts,
        station_day_counts=station_day_counts,
    )
    _record_profile(
        profile_rows,
        run_start_perf=run_start_perf,
        phase_start_perf=phase_start,
        phase="export_analysis_files",
        details={"station_curve_rows": len(station_curve), "station_summary_rows": len(station_summary)},
    )
    if write_web_json:
        phase_start = time.perf_counter()
        web_metrics = export_web_json_files(
            station_curve,
            station_summary,
            station_metadata,
            station_day_counts,
            out,
            year=year,
            timezone=metrics.timezone,
            station_ids=web_station_ids,
            date_from=web_date_from,
            date_to=web_date_to,
            json_indent=web_json_indent,
            validate_written_json=validate_written_json,
        )
        for key in [
            "json_file_count",
            "json_parse_failures",
            "station_dates_with_96_points",
            "station_dates_without_96_points",
        ]:
            setattr(metrics, key, int(web_metrics.get(key, 0) or 0))
        _record_profile(
            profile_rows,
            run_start_perf=run_start_perf,
            phase_start_perf=phase_start,
            phase="export_web_json_files",
            details=web_metrics,
        )

    if metrics.is_sample:
        metrics.notes.append(
            f"This run used --max-vehicles={max_vehicles}; outputs are a smoke/sample export, not the full private-car fleet."
        )
    if failed_vehicle_rows:
        metrics.notes.append(
            f"{len(failed_vehicle_rows)} vehicle(s) failed preflight validation and were not simulated; see failed_vehicles_{year}.csv."
        )

    phase_start = time.perf_counter()
    write_data_quality_report(
        out,
        inventory=inventory,
        metrics=metrics,
        station_curve=station_curve,
        station_metadata=station_metadata,
        station_summary=station_summary,
        year=year,
        preflight_report=preflight_report,
    )
    _record_profile(
        profile_rows,
        run_start_perf=run_start_perf,
        phase_start_perf=phase_start,
        phase="write_data_quality_report",
    )
    _write_profile_log(out, profile_rows, year=year)
    if vehicle_schedule_profile_rows:
        pd.DataFrame(vehicle_schedule_profile_rows).to_csv(
            out / f"schedule_vehicle_profile_{year}.csv",
            index=False,
        )
    if failed_vehicle_rows:
        pd.DataFrame(failed_vehicle_rows).to_csv(out / f"failed_vehicles_{year}.csv", index=False)
    pd.DataFrame([sampler.stats_snapshot()]).to_csv(out / f"destination_lookup_summary_{year}.csv", index=False)
    _write_optional_frame(sampler.key_stats_frame(), out / f"destination_lookup_keys_{year}.csv")
    _write_optional_frame(sampler.origin_stats_frame(), out / f"destination_lookup_origins_{year}.csv")

    return {
        "station_curve": station_curve,
        "station_summary": station_summary,
        "station_metadata": station_metadata,
        "station_day_counts": station_day_counts,
        "metrics": metrics,
        "profile": pd.DataFrame(profile_rows),
        "schedule_profile": pd.DataFrame(vehicle_schedule_profile_rows),
        "destination_lookup_summary": sampler.stats_snapshot(),
        "output_dir": out,
    }
