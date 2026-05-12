"""Random coach journey selection helpers for the narrative notebook."""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from ._compat import field as _field
from .feasibility import journey_feasibility


RUNTIME_SOURCE_VALUES = (
    "runtime_h",
    "duration_h",
    "runtime_min",
    "start_end_h_diff",
    "none",
)


def _runtime_h(data: pd.DataFrame) -> tuple[pd.Series, str]:
    if "runtime_h" in data.columns:
        return pd.to_numeric(data["runtime_h"], errors="coerce"), "runtime_h"
    if "duration_h" in data.columns:
        return pd.to_numeric(data["duration_h"], errors="coerce"), "duration_h"
    if "runtime_min" in data.columns:
        return pd.to_numeric(data["runtime_min"], errors="coerce") / 60.0, "runtime_min"
    if {"start_h", "end_h"}.issubset(data.columns):
        value = pd.to_numeric(data["end_h"], errors="coerce") - pd.to_numeric(
            data["start_h"],
            errors="coerce",
        )
        return value, "start_end_h_diff"
    return pd.Series(np.nan, index=data.index, dtype=float), "none"


def _filter_journeys(
    df: pd.DataFrame,
    *,
    runtime_h_range: tuple[float, float] | None = (1.0, 8.0),
    require_no_cross_midnight: bool = True,
    require_known_distance: bool = True,
) -> pd.DataFrame:
    data = df.copy()
    mask = pd.Series(True, index=data.index)
    if "has_cross_midnight" in data.columns:
        cross_midnight = data["has_cross_midnight"].astype(bool)
    elif {"start_h", "end_h"}.issubset(data.columns):
        cross_midnight = (pd.to_numeric(data["start_h"], errors="coerce") >= 24.0) | (
            pd.to_numeric(data["end_h"], errors="coerce") > 24.0
        )
    else:
        cross_midnight = pd.Series(False, index=data.index)
    if require_no_cross_midnight:
        mask &= ~cross_midnight

    if require_known_distance:
        known_distance = pd.to_numeric(data["distance_km"], errors="coerce").notna()
        if "distance_source" in data.columns:
            known_distance &= data["distance_source"].astype(str).ne("unknown")
        mask &= known_distance

    runtimes, runtime_source = _runtime_h(data)
    data["runtime_source"] = runtime_source
    if runtime_h_range is not None:
        lo, hi = runtime_h_range
        mask &= runtimes.ge(float(lo)) & runtimes.le(float(hi))

    return data.loc[mask].copy()


def _choose_row(candidates: pd.DataFrame, rng: np.random.Generator) -> pd.Series:
    if candidates.empty:
        raise ValueError("No coach journeys satisfy the requested selection filters.")
    pos = int(rng.integers(0, len(candidates)))
    return candidates.iloc[pos].copy()


def sample_protagonist_journey(
    journeys: pd.DataFrame,
    rng: np.random.Generator,
    *,
    runtime_h_range: tuple[float, float] = (1.0, 8.0),
    require_no_cross_midnight: bool = True,
    require_known_distance: bool = True,
) -> pd.Series:
    """Randomly sample one coach journey satisfying the basic narrative filters."""
    candidates = _filter_journeys(
        journeys,
        runtime_h_range=runtime_h_range,
        require_no_cross_midnight=require_no_cross_midnight,
        require_known_distance=require_known_distance,
    )
    return _choose_row(candidates, rng)


def sample_contrast_journey(
    journeys: pd.DataFrame,
    rng: np.random.Generator,
    protagonist: pd.Series | str | None = None,
    *,
    require_distance_gap: float = 0.5,
    runtime_h_range: tuple[float, float] = (1.0, 8.0),
    require_no_cross_midnight: bool = True,
    require_known_distance: bool = True,
) -> pd.Series:
    """Randomly sample a contrast journey with a materially different distance."""
    candidates = _filter_journeys(
        journeys,
        runtime_h_range=runtime_h_range,
        require_no_cross_midnight=require_no_cross_midnight,
        require_known_distance=require_known_distance,
    )
    key_col = "journey_id" if "journey_id" in candidates.columns else "vehicle_journey_code"
    if protagonist is not None and key_col in candidates.columns:
        protagonist_code = (
            str(protagonist.get(key_col))
            if isinstance(protagonist, pd.Series)
            else str(protagonist)
        )
        candidates = candidates[candidates[key_col].astype(str).ne(protagonist_code)]
    if protagonist is not None and require_distance_gap is not None:
        if isinstance(protagonist, pd.Series):
            protagonist_distance = float(protagonist.get("distance_km"))
        else:
            source = journeys
            if key_col not in source.columns:
                raise ValueError(f"journeys must contain {key_col} to find protagonist distance.")
            match = source[source[key_col].astype(str).eq(str(protagonist))]
            if match.empty:
                raise ValueError(f"protagonist {protagonist!r} was not found in journeys.")
            protagonist_distance = float(match.iloc[0]["distance_km"])
        candidate_distance = pd.to_numeric(candidates["distance_km"], errors="coerce")
        gap = (candidate_distance - protagonist_distance).abs() / max(protagonist_distance, 1.0)
        candidates = candidates.loc[gap.ge(float(require_distance_gap))].copy()
    return _choose_row(candidates, rng)


def _float_or_nan(value: Any) -> float:
    if value == "":
        return float("nan")
    return float(value) if pd.notna(value) else float("nan")


def render_journey_identity_card(
    journey_row: pd.Series | dict,
    ev_spec: pd.Series | dict | None = None,
    feasibility: dict | None = None,
    *,
    wall_clock_s: float | None = None,
) -> pd.DataFrame:
    """Return a compact 13+ field identity card for one journey and EV."""
    ev_spec_data: pd.Series | dict = {} if ev_spec is None else ev_spec
    if feasibility is None and ev_spec is not None:
        feasibility = journey_feasibility(
            _float_or_nan(_field(journey_row, "distance_km")),
            battery_kwh=_float_or_nan(_field(ev_spec_data, "Energy_kWh", _field(ev_spec_data, "battery_kwh"))),
            consumption_kwh_per_km=_float_or_nan(_field(ev_spec_data, "consumption_kwh_per_km")),
        )
    feasibility = feasibility or {}

    route = _field(journey_row, "line_name", "") or _field(journey_row, "service_code", "")
    duration_h = _field(journey_row, "duration_h", None)
    if duration_h is None or pd.isna(duration_h):
        runtime_min = _field(journey_row, "runtime_min", None)
        duration_h = float(runtime_min) / 60.0 if pd.notna(runtime_min) else float("nan")

    record = {
        "operator": _field(journey_row, "operator_name", _field(journey_row, "operator_code", "")),
        "operator_code": _field(journey_row, "operator_code", ""),
        "vehicle_journey_code": _field(journey_row, "vehicle_journey_code", ""),
        "route": route,
        "service_code": _field(journey_row, "service_code", ""),
        "start_time": _field(journey_row, "departure_time", _field(journey_row, "start_h", "")),
        "end_time": _field(journey_row, "arrival_time", _field(journey_row, "end_h", "")),
        "duration_h": float(duration_h),
        "distance_km": _float_or_nan(_field(journey_row, "distance_km")),
        "distance_source": _field(journey_row, "distance_source", ""),
        "EV model": _field(ev_spec_data, "Model", _field(ev_spec_data, "model", _field(ev_spec_data, "gen_model", ""))),
        "battery_kwh": _float_or_nan(_field(ev_spec_data, "Energy_kWh", _field(ev_spec_data, "battery_kwh", float("nan")))),
        "consumption_kwh_per_km": _float_or_nan(_field(ev_spec_data, "consumption_kwh_per_km", float("nan"))),
        "feasible_single_charge": feasibility.get("feasible_single_charge", ""),
        "shortfall_kwh": feasibility.get("shortfall_kwh", float("nan")),
        "wall-clock time": float(wall_clock_s) if wall_clock_s is not None else float("nan"),
    }
    return pd.DataFrame([record])
