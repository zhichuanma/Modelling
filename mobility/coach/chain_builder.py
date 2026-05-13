"""First-fit coach journey chaining for annual simulation.

The v1 algorithm is deliberately simple: within each ``operator_code`` and
active date, journeys are sorted by ``start_h`` and assigned to the first
existing chain whose final journey ends before the candidate starts (including
``transit_buffer_h``) and whose end point is within ``max_relocation_km`` of
the candidate start point. If no existing chain qualifies, a new chain is
opened.

This is not vehicle-blocking optimisation, not a reconstruction of real
operator rosters, and does not consider state-of-charge constraints while
building chains.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import pandas as pd

from .distance import haversine_km


REQUIRED_JOURNEY_COLUMNS = {
    "journey_id",
    "operator_code",
    "start_h",
    "end_h",
    "start_lat",
    "start_lon",
    "end_lat",
    "end_lon",
}
REQUIRED_DATE_INDEX_COLUMNS = {"journey_id", "date"}


def _coerce_date(value: Any) -> dt.date:
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    if isinstance(value, pd.Timestamp):
        return value.date()
    return dt.date.fromisoformat(str(value))


def _validate_inputs(journeys: pd.DataFrame, date_index: pd.DataFrame) -> None:
    missing_journeys = REQUIRED_JOURNEY_COLUMNS - set(journeys.columns)
    if missing_journeys:
        raise ValueError(f"journeys is missing required columns: {sorted(missing_journeys)}")
    missing_dates = REQUIRED_DATE_INDEX_COLUMNS - set(date_index.columns)
    if missing_dates:
        raise ValueError(f"date_index is missing required columns: {sorted(missing_dates)}")


def _can_append(
    last: pd.Series,
    candidate: pd.Series,
    *,
    transit_buffer_h: float,
    max_relocation_km: float,
) -> bool:
    if float(last["end_h"]) + float(transit_buffer_h) > float(candidate["start_h"]):
        return False
    relocation_km = haversine_km(
        float(last["end_lat"]),
        float(last["end_lon"]),
        float(candidate["start_lat"]),
        float(candidate["start_lon"]),
    )
    return relocation_km <= float(max_relocation_km)


def build_coach_chains(
    journeys: pd.DataFrame,
    date_index: pd.DataFrame,
    *,
    transit_buffer_h: float = 0.5,
    max_relocation_km: float = 50.0,
) -> pd.DataFrame:
    """Assign active coach journeys to date-specific first-fit chains."""
    _validate_inputs(journeys, date_index)
    if transit_buffer_h < 0.0:
        raise ValueError("transit_buffer_h must be non-negative.")
    if max_relocation_km < 0.0:
        raise ValueError("max_relocation_km must be non-negative.")
    if journeys.empty or date_index.empty:
        return pd.DataFrame(
            columns=[
                "journey_id",
                "date",
                "coach_chain_id",
                "position_in_chain",
                "coach_chain_template_id",
                "operator_code",
            ]
        )

    active = date_index.loc[:, ["journey_id", "date"]].drop_duplicates().copy()
    active["date"] = active["date"].map(_coerce_date)
    merged = active.merge(journeys, on="journey_id", how="inner", validate="many_to_one")
    if merged.empty:
        return pd.DataFrame(
            columns=[
                "journey_id",
                "date",
                "coach_chain_id",
                "position_in_chain",
                "coach_chain_template_id",
                "operator_code",
            ]
        )
    for column in ("start_h", "end_h", "start_lat", "start_lon", "end_lat", "end_lon"):
        merged[column] = pd.to_numeric(merged[column], errors="coerce")
    merged = merged.dropna(subset=["start_h", "end_h", "start_lat", "start_lon", "end_lat", "end_lon"])

    records: list[dict[str, object]] = []
    group_cols = ["operator_code", "date"]
    for (operator_code, active_date), group in merged.groupby(group_cols, sort=True):
        ordered = group.sort_values(["start_h", "end_h", "journey_id"], kind="stable")
        chains: list[list[pd.Series]] = []
        for _, row in ordered.iterrows():
            placed = False
            for chain_index, chain in enumerate(chains, start=1):
                if _can_append(
                    chain[-1],
                    row,
                    transit_buffer_h=transit_buffer_h,
                    max_relocation_km=max_relocation_km,
                ):
                    chain.append(row)
                    records.append(
                        {
                            "journey_id": str(row["journey_id"]),
                            "date": active_date,
                            "coach_chain_id": f"{operator_code}_{active_date.isoformat()}_{chain_index:03d}",
                            "position_in_chain": len(chain),
                            "coach_chain_template_id": f"{operator_code}_{chain_index:03d}",
                            "operator_code": str(operator_code),
                        }
                    )
                    placed = True
                    break
            if placed:
                continue
            chains.append([row])
            chain_index = len(chains)
            records.append(
                {
                    "journey_id": str(row["journey_id"]),
                    "date": active_date,
                    "coach_chain_id": f"{operator_code}_{active_date.isoformat()}_{chain_index:03d}",
                    "position_in_chain": 1,
                    "coach_chain_template_id": f"{operator_code}_{chain_index:03d}",
                    "operator_code": str(operator_code),
                }
            )

    out = pd.DataFrame.from_records(
        records,
        columns=[
            "journey_id",
            "date",
            "coach_chain_id",
            "position_in_chain",
            "coach_chain_template_id",
            "operator_code",
        ],
    )
    return out.sort_values(["date", "operator_code", "coach_chain_id", "position_in_chain"], kind="stable").reset_index(drop=True)


__all__ = ["build_coach_chains"]
