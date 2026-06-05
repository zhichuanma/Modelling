"""Adapt TxC coach journeys + first-fit chains to the bus depot-load schemas.

The bus annual depot-load machinery (assignment, events, SOC carry-over, load
aggregation — plan v2 / commit 0b41737) is frame-generic: it only consumes the
``block_templates`` / ``block_instances`` / ``ev_specs`` schemas. This module
maps coach first-fit chains onto those schemas so the entire downstream runs
unchanged:

- one chain template (= journey-set hash from ``chain_builder``) becomes one
  block template whose ``trip_*`` arrays are the ordered journeys;
- ``chains_long`` (journey x date x template) IS the calendar: instances are
  emitted per (template, date) directly — no GTFS service calendar involved.

Energy convention (decided 2026-06-05): chains may relocate up to
``max_relocation_km`` between journeys; that repositioning is explicit energy
in this port. ``passenger_distance_km`` on templates/instances therefore holds
the ENERGY-RELEVANT on-vehicle km (journey km + relocation km) so the
feasibility screen budgets relocations with zero downstream edits, while the
honest decomposition is kept in ``coach_passenger_km`` and
``relocation_km_total`` (audit columns). Relocation distances use the bus
events haversine and the same >MIDDAY_DEPOT_DISTANCE_THRESHOLD_KM qualifying
rule, so the screen total equals the event-ledger relocation sum exactly.

Chains are first-fit constructs, NOT real operator rosters — every consumer
must carry that caveat.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import numpy as np
import pandas as pd

from mobility.bus.annual_block_instances import coerce_date, datetime_from_service_hour
from mobility.bus.annual_block_templates import hours_to_time_label
from mobility.bus.annual_depot_events import MIDDAY_DEPOT_DISTANCE_THRESHOLD_KM, haversine_km


COACH_BLOCK_SOURCE = "txc_first_fit_chain"


def attach_journey_endpoints(journeys: pd.DataFrame, stop_sequences: pd.DataFrame) -> pd.DataFrame:
    """Attach first/last stop coordinates and stop refs to journeys (idempotent)."""
    out = journeys.copy()
    need_coords = not {"start_lat", "start_lon", "end_lat", "end_lon"}.issubset(out.columns)
    need_stops = not {"start_stop", "end_stop"}.issubset(out.columns)
    if not (need_coords or need_stops):
        return out
    if stop_sequences.empty or not {"journey_id", "stop_sequence"}.issubset(stop_sequences.columns):
        raise ValueError("stop_sequences with journey_id/stop_sequence are required to attach journey endpoints.")
    records: list[dict[str, Any]] = []
    has_coords = {"lat", "lon"}.issubset(stop_sequences.columns)
    for journey_id, group in stop_sequences.groupby("journey_id", sort=False):
        ordered = group.sort_values("stop_sequence", kind="stable")
        first = ordered.iloc[0]
        last = ordered.iloc[-1]
        record: dict[str, Any] = {
            "journey_id": str(journey_id),
            "start_stop": str(first.get("stop_point_ref", "") or ""),
            "end_stop": str(last.get("stop_point_ref", "") or ""),
        }
        if has_coords:
            record.update(
                {
                    "start_lat": first.get("lat"),
                    "start_lon": first.get("lon"),
                    "end_lat": last.get("lat"),
                    "end_lon": last.get("lon"),
                }
            )
        records.append(record)
    endpoints = pd.DataFrame.from_records(records)
    merge_cols = [col for col in endpoints.columns if col == "journey_id" or col not in out.columns]
    return out.merge(endpoints.loc[:, merge_cols], on="journey_id", how="left")


def _as_float_list(values: pd.Series) -> list[float]:
    return [float(value) if pd.notna(value) else np.nan for value in values.to_numpy()]


def _as_str_list(values: pd.Series) -> list[str]:
    return ["" if pd.isna(value) else str(value) for value in values.to_numpy()]


def _relocation_km(ordered: pd.DataFrame) -> float:
    """Sum of qualifying inter-journey repositioning distances for one chain.

    Must mirror the event-stage rule exactly (same haversine, same threshold,
    NaN coords contribute nothing) so the feasibility screen and the SOC walk
    account identical relocation energy.
    """
    total = 0.0
    end_lat = pd.to_numeric(ordered["end_lat"], errors="coerce").to_numpy(dtype=float)
    end_lon = pd.to_numeric(ordered["end_lon"], errors="coerce").to_numpy(dtype=float)
    start_lat = pd.to_numeric(ordered["start_lat"], errors="coerce").to_numpy(dtype=float)
    start_lon = pd.to_numeric(ordered["start_lon"], errors="coerce").to_numpy(dtype=float)
    for index in range(len(ordered) - 1):
        km = haversine_km(end_lat[index], end_lon[index], start_lat[index + 1], start_lon[index + 1])
        if np.isfinite(km) and km > MIDDAY_DEPOT_DISTANCE_THRESHOLD_KM:
            total += float(km)
    return total


def build_coach_block_templates(journeys: pd.DataFrame, chains_long: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """One chain template -> one bus-schema block template (ordered journeys = trips)."""
    required_chain = {"journey_id", "date", "coach_chain_id", "position_in_chain", "coach_chain_template_id", "operator_code"}
    missing = sorted(required_chain - set(chains_long.columns))
    if missing:
        raise ValueError(f"chains_long is missing required columns: {missing}")
    required_journeys = {"journey_id", "operator_code", "start_h", "end_h", "distance_km", "start_lat", "start_lon", "end_lat", "end_lon"}
    missing_journeys = sorted(required_journeys - set(journeys.columns))
    if missing_journeys:
        raise ValueError(f"journeys is missing required columns: {missing_journeys}")
    if chains_long.empty:
        return pd.DataFrame(), pd.DataFrame([{"n_chain_rows": 0, "n_block_templates": 0}])

    unique_journeys = journeys.drop_duplicates("journey_id").copy()
    unique_journeys["journey_id"] = unique_journeys["journey_id"].astype(str)
    journey_lookup = unique_journeys.set_index("journey_id", drop=False)

    records: list[dict[str, Any]] = []
    invariant_violations: list[str] = []
    for template_id, template_rows in chains_long.groupby("coach_chain_template_id", sort=True):
        # Invariant (chain_builder fix-1): every chain instance of a template
        # carries the identical ordered journey tuple. Fail loudly otherwise —
        # downstream timing/energy would silently diverge between dates.
        ordered_sets = {
            chain_id: tuple(group.sort_values("position_in_chain")["journey_id"].astype(str))
            for chain_id, group in template_rows.groupby("coach_chain_id", sort=True)
        }
        unique_orderings = set(ordered_sets.values())
        if len(unique_orderings) != 1:
            invariant_violations.append(str(template_id))
            continue
        journey_ids = list(next(iter(unique_orderings)))
        missing_ids = [jid for jid in journey_ids if jid not in journey_lookup.index]
        if missing_ids:
            raise KeyError(f"chains_long references journeys missing from the journeys table: {missing_ids[:5]}")
        ordered = journey_lookup.loc[journey_ids].reset_index(drop=True)
        for column in ("start_h", "end_h", "distance_km", "start_lat", "start_lon", "end_lat", "end_lon"):
            ordered[column] = pd.to_numeric(ordered[column], errors="coerce")

        first_start = float(ordered["start_h"].iloc[0])
        last_end = float(ordered["end_h"].iloc[-1])
        passenger_km = float(ordered["distance_km"].fillna(0.0).sum())
        relocation_km = _relocation_km(ordered)
        operator = str(template_rows["operator_code"].iloc[0])
        block_template_id = f"ct_{template_id}"
        records.append(
            {
                "block_template_id": block_template_id,
                "agency_id": operator,
                "service_id": block_template_id,
                "block_id": str(template_id),
                "block_source": COACH_BLOCK_SOURCE,
                "n_trips": int(len(ordered)),
                "start_h": first_start,
                "end_h": last_end,
                "start_time": hours_to_time_label(first_start),
                "end_time": hours_to_time_label(last_end),
                "duration_h": float(last_end - first_start),
                # ENERGY-RELEVANT on-vehicle km (journeys + relocations) so the
                # feasibility screen budgets repositioning automatically.
                "passenger_distance_km": passenger_km + relocation_km,
                "coach_passenger_km": passenger_km,
                "relocation_km_total": relocation_km,
                "start_stop": str(ordered["start_stop"].iloc[0]) if "start_stop" in ordered.columns else "",
                "end_stop": str(ordered["end_stop"].iloc[-1]) if "end_stop" in ordered.columns else "",
                "start_lat": float(ordered["start_lat"].iloc[0]),
                "start_lon": float(ordered["start_lon"].iloc[0]),
                "end_lat": float(ordered["end_lat"].iloc[-1]),
                "end_lon": float(ordered["end_lon"].iloc[-1]),
                "trip_ids": _as_str_list(ordered["journey_id"]),
                "trip_start_times": _as_float_list(ordered["start_h"]),
                "trip_end_times": _as_float_list(ordered["end_h"]),
                "trip_start_lats": _as_float_list(ordered["start_lat"]),
                "trip_start_lons": _as_float_list(ordered["start_lon"]),
                "trip_end_lats": _as_float_list(ordered["end_lat"]),
                "trip_end_lons": _as_float_list(ordered["end_lon"]),
                "trip_distances_km": _as_float_list(ordered["distance_km"]),
                "trip_start_stops": _as_str_list(ordered["start_stop"]) if "start_stop" in ordered.columns else [""] * len(ordered),
                "trip_end_stops": _as_str_list(ordered["end_stop"]) if "end_stop" in ordered.columns else [""] * len(ordered),
            }
        )

    if invariant_violations:
        raise RuntimeError(
            "coach chain template invariant violated (same template hash, different journey ordering) for: "
            + ", ".join(invariant_violations[:10])
            + (" ..." if len(invariant_violations) > 10 else "")
        )

    templates = pd.DataFrame.from_records(records)
    if not templates.empty:
        templates = templates.sort_values(["agency_id", "block_template_id"], kind="stable").reset_index(drop=True)
    diagnostics = pd.DataFrame(
        [
            {
                "n_chain_rows": int(len(chains_long)),
                "n_block_templates": int(len(templates)),
                "n_cross_midnight_templates": int((templates["end_h"] >= 24.0).sum()) if not templates.empty else 0,
                "min_trips_per_template": int(templates["n_trips"].min()) if not templates.empty else 0,
                "max_trips_per_template": int(templates["n_trips"].max()) if not templates.empty else 0,
                "total_relocation_km": float(templates["relocation_km_total"].sum()) if not templates.empty else 0.0,
                "n_templates_with_relocation": int((templates["relocation_km_total"] > 0).sum()) if not templates.empty else 0,
                "block_source": COACH_BLOCK_SOURCE,
            }
        ]
    )
    return templates, diagnostics


def expand_coach_block_instances(
    block_templates_lsoa: pd.DataFrame,
    chains_long: pd.DataFrame,
    *,
    start_date: str | dt.date | pd.Timestamp,
    end_date: str | dt.date | pd.Timestamp,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Emit one instance per (template, active date): chains_long IS the calendar."""
    start = coerce_date(start_date)
    end = coerce_date(end_date)
    if block_templates_lsoa.empty or chains_long.empty:
        diagnostics = pd.DataFrame(
            [
                {
                    "feed_year_start": start.isoformat(),
                    "feed_year_end": end.isoformat(),
                    "n_block_templates": int(len(block_templates_lsoa)),
                    "n_active_block_templates": 0,
                    "n_block_instances_annual": 0,
                    "block_instance_ids_unique": True,
                }
            ]
        )
        return pd.DataFrame(), diagnostics

    active = chains_long.loc[:, ["coach_chain_template_id", "date"]].drop_duplicates().copy()
    active["date"] = active["date"].map(coerce_date)
    active = active.loc[(active["date"] >= start) & (active["date"] <= end)]
    active["block_template_id"] = "ct_" + active["coach_chain_template_id"].astype(str)

    pass_columns = [
        "block_template_id",
        "agency_id",
        "service_id",
        "block_id",
        "block_source",
        "start_h",
        "end_h",
        "duration_h",
        "passenger_distance_km",
        "coach_passenger_km",
        "relocation_km_total",
        "start_stop",
        "end_stop",
        "start_lat",
        "start_lon",
        "end_lat",
        "end_lon",
        "start_lsoa",
        "end_lsoa",
        "region_key",
    ]
    optional = [col for col in pass_columns if col in block_templates_lsoa.columns]
    template_lookup = block_templates_lsoa.drop_duplicates("block_template_id").set_index("block_template_id")

    records: list[dict[str, Any]] = []
    for row in active.itertuples(index=False):
        template_id = str(row.block_template_id)
        if template_id not in template_lookup.index:
            continue
        template = template_lookup.loc[template_id]
        record = {col: template.get(col) for col in optional if col != "block_template_id"}
        record["block_template_id"] = template_id
        record["service_date"] = row.date.isoformat()
        record["start_datetime"] = datetime_from_service_hour(row.date, float(template["start_h"]))
        record["end_datetime"] = datetime_from_service_hour(row.date, float(template["end_h"]))
        records.append(record)

    instances = pd.DataFrame.from_records(records)
    if not instances.empty:
        instances = instances.sort_values(["service_date", "block_template_id", "start_datetime"], kind="stable").reset_index(drop=True)
        seq = instances.groupby(["service_date", "block_template_id"], sort=False).cumcount()
        instances.insert(
            1,
            "block_instance_id",
            [
                f"{service_date}_{template_id}_{index:02d}"
                for service_date, template_id, index in zip(instances["service_date"], instances["block_template_id"], seq)
            ],
        )
        ordered_cols = [
            "service_date",
            "block_instance_id",
            "block_template_id",
            "agency_id",
            "service_id",
            "block_id",
            "block_source",
            "start_datetime",
            "end_datetime",
            "start_h",
            "end_h",
            "duration_h",
            "passenger_distance_km",
            "coach_passenger_km",
            "relocation_km_total",
            "start_stop",
            "end_stop",
            "start_lat",
            "start_lon",
            "end_lat",
            "end_lon",
            "start_lsoa",
            "end_lsoa",
            "region_key",
        ]
        instances = instances.loc[:, [col for col in ordered_cols if col in instances.columns]]

    active_templates = int(instances["block_template_id"].nunique()) if not instances.empty else 0
    diagnostics = pd.DataFrame(
        [
            {
                "feed_year_start": start.isoformat(),
                "feed_year_end": end.isoformat(),
                "n_block_templates": int(len(block_templates_lsoa)),
                "n_active_block_templates": active_templates,
                "n_block_instances_annual": int(len(instances)),
                "n_templates_with_no_active_dates": int(len(block_templates_lsoa) - active_templates),
                "block_instance_ids_unique": bool(instances["block_instance_id"].is_unique) if not instances.empty else True,
            }
        ]
    )
    return instances, diagnostics


__all__ = [
    "COACH_BLOCK_SOURCE",
    "attach_journey_endpoints",
    "build_coach_block_templates",
    "expand_coach_block_instances",
]
