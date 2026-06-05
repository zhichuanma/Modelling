"""Per-vehicle SOC state threading for multi-day carry-over (plan v2 §3.3/§8).

Carry-over mode stitches each vehicle's wall-clock ledger across service days:
every processed day leaves exactly one open "pending" depot window per valid
spec (the duty overnight window, or a full-day idle window at the home depot).
The pending window's end is only known once the NEXT day's assignment fixes the
vehicle's first event start, so windows are finalized one day late (lag-one
finalize) and a day's partitions are written during the following iteration.

Seam rule (decided 2026-06-05): the previous day's window ends at
``min(seam_end, next-day first event start)`` where ``seam_end`` is the
event-stage overnight end (next-day ``default_overnight_end_hour``, or the
late-return fallback ``return_dt + default_overnight_end_hour``). When the next
pull-out is after the seam, the next day owns a ``depot_parking_pre`` window
``[seam_end, deadhead_start]`` — every wall-clock instant has exactly one
owning event, and charge opportunity matches the daily-reset model.

SOC is never silently mutated: idle recovery happens only through explicit
``idle_home_depot_charging`` events (emitted only when they add energy), and a
day ending below ``usable_soc_min`` carries its unclamped SOC forward (§5.2).
Missing SOC state for a spec is fatal (§8.5) — never defaulted.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

import numpy as np
import pandas as pd

from .annual_depot_events import _effective_charge_kw, _event_columns


IDLE_EVENT_TYPE = "idle_home_depot_charging"
OVERNIGHT_EVENT_TYPES = {"depot_parking_overnight", "depot_parking_post"}


@dataclass(frozen=True)
class PendingWindow:
    """An open depot parking window awaiting next-day stitch finalization."""

    vehicle_day_id: str
    service_date: str
    is_idle: bool
    depot_id: str
    depot_lsoa: str
    depot_lat: float
    depot_lon: float
    window_start: pd.Timestamp
    seam_end: pd.Timestamp
    charge_power_kw: float
    soc_at_open_kwh: float


@dataclass
class SpecSocState:
    vehicle_spec_id: str
    battery_kwh: float
    consumption_kwh_per_km: float
    ac_charge_kw_max: float
    usable_soc_min: float
    usable_soc_max: float
    home_depot_id: str
    home_depot_lsoa: str
    home_depot_lat: float
    home_depot_lon: float
    valid_params: bool
    soc_kwh: float
    last_event_end_ts: pd.Timestamp
    pending: PendingWindow | None = None

    @property
    def soc_max_kwh(self) -> float:
        return self.battery_kwh * self.usable_soc_max

    @property
    def soc_min_kwh(self) -> float:
        return self.battery_kwh * self.usable_soc_min


@dataclass(frozen=True)
class StitchResult:
    """SOC at the stitch point of a spec's pending window (exact, not screened)."""

    stitch_ts: pd.Timestamp
    soc_at_stitch_kwh: float
    charge_kwh: float
    pending: PendingWindow | None


def initialize_soc_state(ev_specs: pd.DataFrame, *, start_ts: pd.Timestamp) -> dict[str, SpecSocState]:
    """Initial state at the simulation window start: usable_soc_max, no pending (§8.6)."""
    state: dict[str, SpecSocState] = {}
    for row in ev_specs.itertuples(index=False):
        spec_id = str(getattr(row, "vehicle_spec_id"))
        battery = float(getattr(row, "battery_kwh", np.nan))
        consumption = float(getattr(row, "consumption_kwh_per_km", np.nan))
        ac_kw = float(getattr(row, "ac_charge_kw_max", np.nan))
        usable_min = float(getattr(row, "usable_soc_min", 0.10))
        usable_max = float(getattr(row, "usable_soc_max", 0.95))
        home_status = str(getattr(row, "home_depot_status", ""))
        home_id = str(getattr(row, "home_depot_id", "") or "")
        valid = (
            all(np.isfinite(value) and value > 0 for value in (battery, consumption, ac_kw))
            and usable_min < usable_max
            and home_status == "assigned"
            and home_id != ""
        )
        state[spec_id] = SpecSocState(
            vehicle_spec_id=spec_id,
            battery_kwh=battery,
            consumption_kwh_per_km=consumption,
            ac_charge_kw_max=ac_kw,
            usable_soc_min=usable_min,
            usable_soc_max=usable_max,
            home_depot_id=home_id,
            home_depot_lsoa=str(getattr(row, "home_depot_lsoa", "") or ""),
            home_depot_lat=float(getattr(row, "home_depot_lat", np.nan)),
            home_depot_lon=float(getattr(row, "home_depot_lon", np.nan)),
            valid_params=valid,
            soc_kwh=battery * usable_max if valid else np.nan,
            last_event_end_ts=pd.Timestamp(start_ts),
        )
    return state


def _window_charge_kwh(spec: SpecSocState, pending: PendingWindow, end_ts: pd.Timestamp) -> float:
    """Constant-power charge over [window_start, end_ts], clamped to usable_soc_max."""
    hours = max(0.0, (pd.Timestamp(end_ts) - pending.window_start).total_seconds() / 3600.0)
    headroom = max(0.0, spec.soc_max_kwh - pending.soc_at_open_kwh)
    return float(min(pending.charge_power_kw * hours, headroom))


def project_available_kwh(state: Mapping[str, SpecSocState]) -> dict[str, float]:
    """Screening energy per spec for assignment: SOC projected to the pending seam.

    Conservative pre-block screen (plan v2 §10.1): projects charging to the
    pending window's natural seam end; an earlier-than-seam pull-out truncates
    the window in the exact walk, so a screened-feasible match can still end
    walk-infeasible (recorded, not prevented).
    """
    out: dict[str, float] = {}
    for spec_id, spec in state.items():
        if not spec.valid_params:
            out[spec_id] = np.nan
            continue
        if spec.pending is None:
            soc = spec.soc_kwh
        else:
            soc = min(spec.soc_max_kwh, spec.pending.soc_at_open_kwh + _window_charge_kwh(spec, spec.pending, spec.pending.seam_end))
        out[spec_id] = float(soc - spec.soc_min_kwh)
    return out


def available_from_by_spec(state: Mapping[str, SpecSocState]) -> dict[str, pd.Timestamp]:
    """Earliest instant each spec is physically free at its depot (temporal guard)."""
    return {
        spec_id: (spec.pending.window_start if spec.pending is not None else spec.last_event_end_ts)
        for spec_id, spec in state.items()
    }


def stitch_pendings(
    state: Mapping[str, SpecSocState],
    first_event_start_by_spec: Mapping[str, pd.Timestamp],
) -> dict[str, StitchResult]:
    """Exact stitch of every pending window against the next day's first events.

    A spec matched on the next day stitches at ``min(seam_end, first event
    start)``; an unmatched spec's window runs to its seam end. Specs without a
    pending (day 1) stitch trivially at their current state.
    """
    results: dict[str, StitchResult] = {}
    for spec_id, spec in state.items():
        if not spec.valid_params:
            results[spec_id] = StitchResult(spec.last_event_end_ts, np.nan, 0.0, None)
            continue
        pending = spec.pending
        if pending is None:
            results[spec_id] = StitchResult(spec.last_event_end_ts, spec.soc_kwh, 0.0, None)
            continue
        first_start = first_event_start_by_spec.get(spec_id)
        stitch_ts = pending.seam_end if first_start is None else min(pending.seam_end, pd.Timestamp(first_start))
        stitch_ts = max(stitch_ts, pending.window_start)
        charge = _window_charge_kwh(spec, pending, stitch_ts)
        results[spec_id] = StitchResult(stitch_ts, float(pending.soc_at_open_kwh + charge), charge, pending)
    return results


def soc_init_by_vehicle_day(
    assignments: pd.DataFrame,
    stitch_results: Mapping[str, StitchResult],
) -> dict[str, float]:
    """Map each matched vehicle_day_id to the exact stitch SOC of its spec (§8.5: fatal if missing)."""
    out: dict[str, float] = {}
    if assignments.empty:
        return out
    for row in assignments.loc[:, ["vehicle_day_id", "vehicle_spec_id"]].itertuples(index=False):
        spec_id = str(row.vehicle_spec_id)
        if spec_id not in stitch_results:
            raise KeyError(f"carryover: missing SOC state for vehicle_spec_id={spec_id}")
        out[str(row.vehicle_day_id)] = stitch_results[spec_id].soc_at_stitch_kwh
    return out


def first_event_start_by_spec(events: pd.DataFrame) -> dict[str, pd.Timestamp]:
    """First wall-clock event start per spec from the BUILT next-day events.

    Using the built events (not an assignment-stage estimate) guarantees the
    stitch end equals the next day's actual first event start, so per-vehicle
    windows tile the wall clock exactly.
    """
    if events.empty:
        return {}
    starts = events.groupby("vehicle_spec_id", sort=False)["start_datetime"].min()
    return {str(spec_id): pd.Timestamp(value) for spec_id, value in starts.items()}


def finalize_day_frames(
    events: pd.DataFrame,
    soc_summary: pd.DataFrame,
    stitch_results: Mapping[str, StitchResult],
    state: Mapping[str, SpecSocState],
) -> tuple[pd.DataFrame, pd.DataFrame, dict[str, float]]:
    """Finalize a buffered day: truncate duty overnight windows, emit idle events.

    Returns ``(events, soc_summary, finalize_stats)`` with the day's pending
    windows resolved against ``stitch_results`` (which must come from
    :func:`stitch_pendings` over the state as of the END of that day). Idle
    events are appended only when they add energy; every mutation keeps the
    event-vs-load energy identity intact because the load aggregation runs on
    the finalized frame.
    """
    stats = {"idle_charge_kwh": 0.0, "n_idle_charging_events": 0, "overnight_truncation_kwh": 0.0}
    idle_records: list[dict[str, Any]] = []
    events = events.copy()
    soc_summary = soc_summary.copy()
    if not soc_summary.empty:
        summary_index = pd.Index(soc_summary["vehicle_day_id"].astype(str))
    else:
        summary_index = pd.Index([])

    for spec_id, result in stitch_results.items():
        pending = result.pending
        if pending is None:
            continue
        if pending.is_idle:
            if result.charge_kwh > 0.0:
                idle_records.append(_idle_event_record(spec_id, pending, result, state[spec_id]))
                stats["idle_charge_kwh"] += result.charge_kwh
                stats["n_idle_charging_events"] += 1
            continue
        # Duty overnight window: locate the placeholder row (last event of the
        # vehicle-day, walked to seam_end) and re-fit it to the stitch end.
        mask = (events["vehicle_day_id"].astype(str) == pending.vehicle_day_id) & events["event_type"].isin(OVERNIGHT_EVENT_TYPES)
        positions = np.flatnonzero(mask.to_numpy())
        if positions.size != 1:
            raise RuntimeError(
                f"carryover finalize: expected exactly one overnight window for {pending.vehicle_day_id}, found {positions.size}"
            )
        position = int(positions[0])
        old_charge = float(events.iat[position, events.columns.get_loc("charge_kwh_added")] or 0.0)
        new_charge = result.charge_kwh
        stitch_ts = result.stitch_ts
        duration_min = max(0.0, (stitch_ts - pending.window_start).total_seconds() / 60.0)
        events.iat[position, events.columns.get_loc("end_datetime")] = stitch_ts
        events.iat[position, events.columns.get_loc("duration_min")] = duration_min
        events.iat[position, events.columns.get_loc("charge_kwh_added")] = new_charge
        events.iat[position, events.columns.get_loc("soc_end_kwh")] = result.soc_at_stitch_kwh
        if new_charge > 0.0 and pending.charge_power_kw > 0.0:
            charging_end = pending.window_start + pd.to_timedelta(new_charge / pending.charge_power_kw * 60.0, unit="m")
        else:
            charging_end = pd.NaT
        events.iat[position, events.columns.get_loc("charging_end_datetime")] = charging_end
        events.iat[position, events.columns.get_loc("event_type")] = (
            "depot_parking_overnight" if stitch_ts.date() != pending.window_start.date() else "depot_parking_post"
        )
        stats["overnight_truncation_kwh"] += old_charge - new_charge
        if not soc_summary.empty:
            summary_positions = np.flatnonzero(summary_index == pending.vehicle_day_id)
            if summary_positions.size != 1:
                raise RuntimeError(f"carryover finalize: missing soc summary row for {pending.vehicle_day_id}")
            summary_position = int(summary_positions[0])
            total_col = soc_summary.columns.get_loc("total_charge_kwh")
            soc_summary.iat[summary_position, total_col] = float(soc_summary.iat[summary_position, total_col]) - old_charge + new_charge
            soc_summary.iat[summary_position, soc_summary.columns.get_loc("end_soc_kwh")] = result.soc_at_stitch_kwh
            soc_summary.iat[summary_position, soc_summary.columns.get_loc("end_soc_ts")] = stitch_ts

    if idle_records:
        idle_frame = pd.DataFrame.from_records(idle_records, columns=_event_columns())
        events = pd.concat([events, idle_frame], ignore_index=True) if not events.empty else idle_frame
        events = events.sort_values(["vehicle_day_id", "start_datetime", "event_seq"], kind="stable").reset_index(drop=True)
    return events, soc_summary, stats


def _idle_event_record(spec_id: str, pending: PendingWindow, result: StitchResult, spec: SpecSocState) -> dict[str, Any]:
    duration_min = max(0.0, (result.stitch_ts - pending.window_start).total_seconds() / 60.0)
    charging_end = (
        pending.window_start + pd.to_timedelta(result.charge_kwh / pending.charge_power_kw * 60.0, unit="m")
        if result.charge_kwh > 0.0 and pending.charge_power_kw > 0.0
        else pd.NaT
    )
    record = {column: np.nan for column in _event_columns()}
    record.update(
        {
            "service_date": pending.service_date,
            "vehicle_day_id": pending.vehicle_day_id,
            "vehicle_spec_id": spec_id,
            "block_instance_id": "",
            "block_template_id": "",
            "agency_id": "",
            "block_id": "",
            "depot_id": pending.depot_id,
            "depot_lsoa": pending.depot_lsoa,
            "event_seq": 0,
            "event_type": IDLE_EVENT_TYPE,
            "trip_id": "",
            "start_datetime": pending.window_start,
            "end_datetime": result.stitch_ts,
            "duration_min": duration_min,
            "start_lat": pending.depot_lat,
            "start_lon": pending.depot_lon,
            "end_lat": pending.depot_lat,
            "end_lon": pending.depot_lon,
            "start_lsoa": pending.depot_lsoa,
            "end_lsoa": pending.depot_lsoa,
            "distance_km": 0.0,
            "distance_method": "none",
            "energy_kwh": 0.0,
            "can_charge": True,
            "charge_power_kw": pending.charge_power_kw,
            "charge_kwh_added": result.charge_kwh,
            "soc_start_kwh": pending.soc_at_open_kwh,
            "soc_end_kwh": result.soc_at_stitch_kwh,
            "charging_end_datetime": charging_end,
            "scenario_mode": "ev_stock_scale",
            "battery_kwh": spec.battery_kwh,
            "consumption_kwh_per_km": spec.consumption_kwh_per_km,
            "ac_charge_kw_max": spec.ac_charge_kw_max,
            "usable_soc_min": spec.usable_soc_min,
            "usable_soc_max": spec.usable_soc_max,
        }
    )
    return record


def advance_state_after_walk(
    state: dict[str, SpecSocState],
    stitch_results: Mapping[str, StitchResult],
    walked_events: pd.DataFrame,
    assignments: pd.DataFrame,
    *,
    service_date: str,
    depot_power_kw: float,
    default_overnight_end_hour: float,
    idle_vehicle_charging_policy: str = "home_depot",
) -> None:
    """Advance every spec's state past ``service_date`` (matched AND idle specs).

    Matched specs open a duty overnight pending taken from the walked overnight
    placeholder event; all other valid specs open an idle pending at their home
    depot from the stitch point to the day's seam. State ticks for every spec
    every day — "no event" never means "no state".

    ``idle_vehicle_charging_policy="none"`` keeps the idle pending window (it is
    still needed for ledger stitching) but with zero charge power, so idle days
    never add energy and never emit events.
    """
    matched_specs: dict[str, str] = {}
    if not assignments.empty:
        matched_specs = {
            str(row.vehicle_spec_id): str(row.vehicle_day_id)
            for row in assignments.loc[:, ["vehicle_spec_id", "vehicle_day_id"]].itertuples(index=False)
        }
    overnight_by_day: dict[str, Any] = {}
    if not walked_events.empty:
        overnight_rows = walked_events.loc[walked_events["event_type"].isin(OVERNIGHT_EVENT_TYPES)]
        for row in overnight_rows.itertuples(index=False):
            overnight_by_day[str(row.vehicle_day_id)] = row
    idle_seam_end = pd.Timestamp(service_date) + pd.Timedelta(days=1) + pd.to_timedelta(float(default_overnight_end_hour), unit="h")

    for spec_id, spec in state.items():
        if not spec.valid_params:
            continue
        result = stitch_results.get(spec_id)
        if result is None:
            raise KeyError(f"carryover: missing stitch result for vehicle_spec_id={spec_id}")
        vehicle_day_id = matched_specs.get(spec_id)
        if vehicle_day_id is not None:
            row = overnight_by_day.get(vehicle_day_id)
            if row is None:
                raise RuntimeError(f"carryover: matched vehicle-day {vehicle_day_id} has no overnight window event")
            soc_at_open = float(getattr(row, "soc_start_kwh"))
            if not np.isfinite(soc_at_open):
                raise RuntimeError(f"carryover: overnight window for {vehicle_day_id} has non-finite SOC")
            spec.pending = PendingWindow(
                vehicle_day_id=vehicle_day_id,
                service_date=str(service_date),
                is_idle=False,
                depot_id=str(getattr(row, "depot_id")),
                depot_lsoa=str(getattr(row, "depot_lsoa", "") or ""),
                depot_lat=float(getattr(row, "start_lat", np.nan)),
                depot_lon=float(getattr(row, "start_lon", np.nan)),
                window_start=pd.Timestamp(getattr(row, "start_datetime")),
                seam_end=pd.Timestamp(getattr(row, "end_datetime")),
                charge_power_kw=float(getattr(row, "charge_power_kw", 0.0) or 0.0),
                soc_at_open_kwh=soc_at_open,
            )
            spec.soc_kwh = soc_at_open
            spec.last_event_end_ts = spec.pending.window_start
        else:
            window_start = max(result.stitch_ts, spec.last_event_end_ts) if spec.pending is None else result.stitch_ts
            spec.pending = PendingWindow(
                vehicle_day_id=f"idle_{service_date}_{spec_id}",
                service_date=str(service_date),
                is_idle=True,
                depot_id=spec.home_depot_id,
                depot_lsoa=spec.home_depot_lsoa,
                depot_lat=spec.home_depot_lat,
                depot_lon=spec.home_depot_lon,
                window_start=window_start,
                seam_end=max(idle_seam_end, window_start),
                charge_power_kw=(
                    _effective_charge_kw(spec.ac_charge_kw_max, depot_power_kw)
                    if idle_vehicle_charging_policy == "home_depot"
                    else 0.0
                ),
                soc_at_open_kwh=result.soc_at_stitch_kwh,
            )
            spec.soc_kwh = result.soc_at_stitch_kwh
            spec.last_event_end_ts = window_start


_STATE_COLUMNS = [
    "vehicle_spec_id",
    "service_date",
    "soc_kwh",
    "last_event_end_ts",
    "has_pending",
    "pending_is_idle",
    "pending_vehicle_day_id",
    "pending_service_date",
    "pending_depot_id",
    "pending_depot_lsoa",
    "pending_depot_lat",
    "pending_depot_lon",
    "pending_window_start",
    "pending_seam_end",
    "pending_charge_power_kw",
    "pending_soc_at_open_kwh",
    "battery_kwh",
    "consumption_kwh_per_km",
    "ac_charge_kw_max",
    "usable_soc_min",
    "usable_soc_max",
    "home_depot_id",
    "home_depot_lsoa",
    "home_depot_lat",
    "home_depot_lon",
    "valid_params",
]


def soc_state_to_frame(state: Mapping[str, SpecSocState], service_date: str) -> pd.DataFrame:
    """Serialize the full per-spec state (checkpoint partition for ``service_date``)."""
    records: list[dict[str, Any]] = []
    for spec_id in sorted(state):
        spec = state[spec_id]
        pending = spec.pending
        records.append(
            {
                "vehicle_spec_id": spec_id,
                "service_date": str(service_date),
                "soc_kwh": spec.soc_kwh,
                "last_event_end_ts": spec.last_event_end_ts,
                "has_pending": pending is not None,
                "pending_is_idle": bool(pending.is_idle) if pending else False,
                "pending_vehicle_day_id": pending.vehicle_day_id if pending else "",
                "pending_service_date": pending.service_date if pending else "",
                "pending_depot_id": pending.depot_id if pending else "",
                "pending_depot_lsoa": pending.depot_lsoa if pending else "",
                "pending_depot_lat": pending.depot_lat if pending else np.nan,
                "pending_depot_lon": pending.depot_lon if pending else np.nan,
                "pending_window_start": pending.window_start if pending else pd.NaT,
                "pending_seam_end": pending.seam_end if pending else pd.NaT,
                "pending_charge_power_kw": pending.charge_power_kw if pending else np.nan,
                "pending_soc_at_open_kwh": pending.soc_at_open_kwh if pending else np.nan,
                "battery_kwh": spec.battery_kwh,
                "consumption_kwh_per_km": spec.consumption_kwh_per_km,
                "ac_charge_kw_max": spec.ac_charge_kw_max,
                "usable_soc_min": spec.usable_soc_min,
                "usable_soc_max": spec.usable_soc_max,
                "home_depot_id": spec.home_depot_id,
                "home_depot_lsoa": spec.home_depot_lsoa,
                "home_depot_lat": spec.home_depot_lat,
                "home_depot_lon": spec.home_depot_lon,
                "valid_params": bool(spec.valid_params),
            }
        )
    return pd.DataFrame.from_records(records, columns=_STATE_COLUMNS)


def soc_state_from_frame(frame: pd.DataFrame) -> dict[str, SpecSocState]:
    """Rebuild the state dict from a checkpoint partition (exact resume)."""
    state: dict[str, SpecSocState] = {}
    for row in frame.itertuples(index=False):
        pending = None
        if bool(getattr(row, "has_pending")):
            pending = PendingWindow(
                vehicle_day_id=str(getattr(row, "pending_vehicle_day_id")),
                service_date=str(getattr(row, "pending_service_date")),
                is_idle=bool(getattr(row, "pending_is_idle")),
                depot_id=str(getattr(row, "pending_depot_id")),
                depot_lsoa=str(getattr(row, "pending_depot_lsoa")),
                depot_lat=float(getattr(row, "pending_depot_lat")),
                depot_lon=float(getattr(row, "pending_depot_lon")),
                window_start=pd.Timestamp(getattr(row, "pending_window_start")),
                seam_end=pd.Timestamp(getattr(row, "pending_seam_end")),
                charge_power_kw=float(getattr(row, "pending_charge_power_kw")),
                soc_at_open_kwh=float(getattr(row, "pending_soc_at_open_kwh")),
            )
        spec_id = str(getattr(row, "vehicle_spec_id"))
        state[spec_id] = SpecSocState(
            vehicle_spec_id=spec_id,
            battery_kwh=float(getattr(row, "battery_kwh")),
            consumption_kwh_per_km=float(getattr(row, "consumption_kwh_per_km")),
            ac_charge_kw_max=float(getattr(row, "ac_charge_kw_max")),
            usable_soc_min=float(getattr(row, "usable_soc_min")),
            usable_soc_max=float(getattr(row, "usable_soc_max")),
            home_depot_id=str(getattr(row, "home_depot_id")),
            home_depot_lsoa=str(getattr(row, "home_depot_lsoa")),
            home_depot_lat=float(getattr(row, "home_depot_lat")),
            home_depot_lon=float(getattr(row, "home_depot_lon")),
            valid_params=bool(getattr(row, "valid_params")),
            soc_kwh=float(getattr(row, "soc_kwh")),
            last_event_end_ts=pd.Timestamp(getattr(row, "last_event_end_ts")),
            pending=pending,
        )
    return state


__all__ = [
    "IDLE_EVENT_TYPE",
    "OVERNIGHT_EVENT_TYPES",
    "PendingWindow",
    "SpecSocState",
    "StitchResult",
    "advance_state_after_walk",
    "available_from_by_spec",
    "finalize_day_frames",
    "first_event_start_by_spec",
    "initialize_soc_state",
    "project_available_kwh",
    "soc_init_by_vehicle_day",
    "soc_state_to_frame",
    "soc_state_from_frame",
    "stitch_pendings",
]
