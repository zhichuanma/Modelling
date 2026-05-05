"""Random coach journey selection helpers for the narrative notebook."""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from .feasibility import journey_feasibility


def _known_non_cross_midnight(df: pd.DataFrame) -> pd.DataFrame:
    data = df.copy()
    if "has_cross_midnight" in data.columns:
        cross_midnight = data["has_cross_midnight"].astype(bool)
    elif {"start_h", "end_h"}.issubset(data.columns):
        cross_midnight = (pd.to_numeric(data["start_h"], errors="coerce") >= 24.0) | (
            pd.to_numeric(data["end_h"], errors="coerce") > 24.0
        )
    else:
        cross_midnight = pd.Series(False, index=data.index)
    known_distance = pd.to_numeric(data["distance_km"], errors="coerce").notna()
    if "distance_source" in data.columns:
        known_distance &= data["distance_source"].astype(str).ne("unknown")
    return data.loc[known_distance & ~cross_midnight].copy()


def _choose_row(candidates: pd.DataFrame, rng: np.random.Generator) -> pd.Series:
    if candidates.empty:
        raise ValueError("No coach journeys satisfy known-distance, non-cross-midnight selection.")
    pos = int(rng.integers(0, len(candidates)))
    return candidates.iloc[pos].copy()


def sample_protagonist_journey(
    journeys: pd.DataFrame,
    rng: np.random.Generator,
) -> pd.Series:
    """Randomly sample one known-distance, non-cross-midnight journey."""
    return _choose_row(_known_non_cross_midnight(journeys), rng)


def sample_contrast_journey(
    journeys: pd.DataFrame,
    rng: np.random.Generator,
    protagonist: pd.Series | str | None = None,
) -> pd.Series:
    """Randomly sample a contrast journey using only the same base filters."""
    candidates = _known_non_cross_midnight(journeys)
    key_col = "journey_id" if "journey_id" in candidates.columns else "vehicle_journey_code"
    if protagonist is not None and key_col in candidates.columns:
        protagonist_code = (
            str(protagonist.get(key_col))
            if isinstance(protagonist, pd.Series)
            else str(protagonist)
        )
        candidates = candidates[candidates[key_col].astype(str).ne(protagonist_code)]
    return _choose_row(candidates, rng)


def _field(row: Any, key: str, default: Any = "") -> Any:
    if isinstance(row, pd.Series):
        return row.get(key, default)
    if isinstance(row, dict):
        return row.get(key, default)
    return getattr(row, key, default)


def _float_or_nan(value: Any) -> float:
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
            battery_kwh=_float_or_nan(_field(ev_spec_data, "battery_kwh")),
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
        "EV model": _field(ev_spec_data, "model", _field(ev_spec_data, "gen_model", "")),
        "battery_kwh": _float_or_nan(_field(ev_spec_data, "battery_kwh", float("nan"))),
        "consumption_kwh_per_km": _float_or_nan(_field(ev_spec_data, "consumption_kwh_per_km", float("nan"))),
        "feasible_single_charge": feasibility.get("feasible_single_charge", ""),
        "shortfall_kwh": feasibility.get("shortfall_kwh", float("nan")),
        "wall-clock time": float(wall_clock_s) if wall_clock_s is not None else float("nan"),
    }
    return pd.DataFrame([record])
