"""Annual bus-block simulation orchestration."""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from mobility.core.constants import DEFAULT_CHEMISTRY, STEP_HOURS_DECISION, STEPS_PER_DAY_DECISION
from mobility.core.simulator import simulate_single_ev

from .calendar import FEED_YEAR_END, FEED_YEAR_START
from .vehicle_sampling import sample_bus_vehicle_specs
from .year_schedule import block_to_year_schedules


DEFAULT_PER_BLOCK_PATH = Path(__file__).resolve().parents[2] / "outputs" / "bus_annual_per_block.parquet"
DEFAULT_LOAD_PROFILE_PATH = Path(__file__).resolve().parents[2] / "outputs" / "bus_annual_load_profile.parquet"


def _coerce_date(value: str | dt.date | pd.Timestamp) -> dt.date:
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    if isinstance(value, pd.Timestamp):
        return value.date()
    return dt.date.fromisoformat(str(value))


def annual_dates(
    start_date: str | dt.date | pd.Timestamp = FEED_YEAR_START,
    end_date: str | dt.date | pd.Timestamp = FEED_YEAR_END,
) -> list[dt.date]:
    start = _coerce_date(start_date)
    end = _coerce_date(end_date)
    if end < start:
        raise ValueError("end_date must be on or after start_date.")
    return [start + dt.timedelta(days=offset) for offset in range((end - start).days + 1)]


def _spec_value(spec: pd.Series | dict[str, Any], key: str, default: Any = None) -> Any:
    if isinstance(spec, pd.Series):
        return spec.get(key, default)
    return spec.get(key, default)


def _parking_energy_kwh(schedules, purpose: str) -> float:
    return float(
        sum(
            event.energy_charged_kwh
            for schedule in schedules
            for event in schedule.parking_events
            if event.location_purpose == purpose
        )
    )


def _load_matrix(load_kw: np.ndarray, n_days: int) -> np.ndarray:
    expected = int(n_days) * STEPS_PER_DAY_DECISION
    if load_kw.shape[0] != expected:
        raise ValueError(f"load_kw length {load_kw.shape[0]} does not match {n_days} days.")
    return load_kw.reshape(n_days, STEPS_PER_DAY_DECISION)


def simulate_block_year(
    block_df: pd.DataFrame,
    active_dates,
    vehicle_spec: pd.Series | dict[str, Any],
    start_date: str | dt.date | pd.Timestamp = FEED_YEAR_START,
    end_date: str | dt.date | pd.Timestamp = FEED_YEAR_END,
    *,
    soc_init: float = 1.0,
    allow_layover_charging: bool = False,
    layover_charge_kw: float = 0.0,
    min_layover_for_charging_h: float = 0.0,
    chemistry: str = DEFAULT_CHEMISTRY,
) -> dict:
    """Simulate one bus block across a dated feed-year window."""
    battery_kwh = float(_spec_value(vehicle_spec, "battery_kwh"))
    consumption_kwh_per_km = float(_spec_value(vehicle_spec, "consumption_kwh_per_km"))
    depot_charge_kw = float(_spec_value(vehicle_spec, "depot_charge_kw"))
    block_id = str(block_df["block_id"].iloc[0]) if "block_id" in block_df else "bus_block"
    schedules = block_to_year_schedules(
        block_df,
        active_dates,
        start_date=start_date,
        end_date=end_date,
        ev_id=f"bus_{block_id}",
        consumption_kwh_per_km=consumption_kwh_per_km,
        depot_charge_kw=depot_charge_kw,
        allow_layover_charging=allow_layover_charging,
        layover_charge_kw=layover_charge_kw,
        min_layover_for_charging_h=min_layover_for_charging_h,
    )
    soc, load_kw, soc_after_warmup = simulate_single_ev(
        schedules,
        battery_kwh,
        soc_init=soc_init,
        warm_up_days=0,
        chemistry=chemistry,
    )
    trip_distance_km = float(sum(trip.distance_km for schedule in schedules for trip in schedule.trips))
    trip_energy_kwh = float(sum(trip.energy_consumed_kwh for schedule in schedules for trip in schedule.trips))
    energy_charged_kwh = float(np.sum(load_kw) * STEP_HOURS_DECISION)
    return {
        "schedules": schedules,
        "soc": soc,
        "load_kw": load_kw,
        "load_matrix_kw": _load_matrix(load_kw, len(schedules)),
        "soc_after_warmup": float(soc_after_warmup),
        "soc_end": float(soc[-1]),
        "soc_min": float(soc.min()),
        "annual_distance_km": trip_distance_km,
        "annual_energy_kwh": trip_energy_kwh,
        "energy_charged_kwh": energy_charged_kwh,
        "depot_kwh": _parking_energy_kwh(schedules, "depot_terminus"),
        "layover_kwh": _parking_energy_kwh(schedules, "layover"),
        "active_days": int(len(set(active_dates))),
        "n_schedule_days": int(len(schedules)),
        "battery_kwh": battery_kwh,
        "consumption_kwh_per_km": consumption_kwh_per_km,
        "depot_charge_kw": depot_charge_kw,
    }


def simulate_fleet_year(
    blocks_df: pd.DataFrame,
    service_date_index: dict[str, tuple[dt.date, ...] | list[dt.date]],
    *,
    vehicle_params: pd.DataFrame | None = None,
    vehicle_rng: np.random.Generator | None = None,
    battery_kwh: float | None = None,
    consumption_kwh_per_km: float | None = None,
    depot_charge_kw: float | None = None,
    start_date: str | dt.date | pd.Timestamp = FEED_YEAR_START,
    end_date: str | dt.date | pd.Timestamp = FEED_YEAR_END,
    max_blocks: int | None = None,
    progress_interval: int = 0,
    **kwargs,
) -> tuple[pd.DataFrame, np.ndarray]:
    """Simulate a bus block fleet over the feed-year and aggregate annual load."""
    groups = blocks_df.groupby("block_id", sort=False)
    block_ids = list(groups.groups)
    if max_blocks is not None:
        block_ids = block_ids[: int(max_blocks)]
    if vehicle_params is not None:
        if vehicle_rng is None:
            raise ValueError("vehicle_rng must be provided when vehicle_params is used.")
        sampled = sample_bus_vehicle_specs(vehicle_params, vehicle_rng, n=len(block_ids))
        sampled.insert(0, "block_id", block_ids)
        sampled_specs = {row["block_id"]: row for _, row in sampled.iterrows()}
    elif battery_kwh is None or consumption_kwh_per_km is None or depot_charge_kw is None:
        raise ValueError(
            "Provide either vehicle_params + vehicle_rng or fixed battery_kwh, "
            "consumption_kwh_per_km, and depot_charge_kw."
        )
    else:
        sampled_specs = {}

    dates = annual_dates(start_date, end_date)
    fleet_load_kw = np.zeros((len(dates), STEPS_PER_DAY_DECISION), dtype=float)
    records: list[dict[str, Any]] = []
    total = len(block_ids)

    for index, block_id in enumerate(block_ids, 1):
        block_df = groups.get_group(block_id)
        vehicle_spec = sampled_specs.get(block_id)
        if vehicle_spec is None:
            vehicle_spec = {
                "battery_kwh": battery_kwh,
                "consumption_kwh_per_km": consumption_kwh_per_km,
                "depot_charge_kw": depot_charge_kw,
            }
        service_id = str(block_df["service_id"].iloc[0])
        result = simulate_block_year(
            block_df,
            service_date_index.get(service_id, ()),
            vehicle_spec,
            start_date=start_date,
            end_date=end_date,
            **kwargs,
        )
        fleet_load_kw += result["load_matrix_kw"]
        record = {
            "block_id": block_id,
            "agency_id": block_df["agency_id"].iloc[0],
            "service_id": service_id,
            "block_source": block_df["block_source"].iloc[0],
            "n_trips_template": int(len(block_df)),
            "active_days": result["active_days"],
            "n_schedule_days": result["n_schedule_days"],
            "annual_distance_km": result["annual_distance_km"],
            "annual_energy_kwh": result["annual_energy_kwh"],
            "energy_charged_kwh": result["energy_charged_kwh"],
            "depot_kwh": result["depot_kwh"],
            "layover_kwh": result["layover_kwh"],
            "soc_end": result["soc_end"],
            "soc_min": result["soc_min"],
            "battery_kwh": result["battery_kwh"],
            "consumption_kwh_per_km": result["consumption_kwh_per_km"],
            "depot_charge_kw": result["depot_charge_kw"],
        }
        if vehicle_params is not None:
            record.update(
                {
                    "vehicle_make": str(vehicle_spec["make"]),
                    "vehicle_gen_model": str(vehicle_spec["gen_model"]),
                    "vehicle_stock_2025_q2": float(vehicle_spec["stock_2025_q2"]),
                }
            )
        records.append(record)
        if progress_interval > 0 and (index % progress_interval == 0 or index == total):
            print(f"  Simulated annual bus blocks: {index:,}/{total:,}", flush=True)

    per_block = pd.DataFrame.from_records(records)
    if not per_block.empty:
        per_block = per_block.set_index("block_id")
    per_block.attrs["start_date"] = _coerce_date(start_date).isoformat()
    per_block.attrs["end_date"] = _coerce_date(end_date).isoformat()
    return per_block, fleet_load_kw


def annual_load_matrix_to_frame(
    load_matrix_kw: np.ndarray,
    start_date: str | dt.date | pd.Timestamp = FEED_YEAR_START,
    end_date: str | dt.date | pd.Timestamp = FEED_YEAR_END,
) -> pd.DataFrame:
    """Convert a days x 96 annual load matrix to a tidy DataFrame."""
    dates = annual_dates(start_date, end_date)
    if load_matrix_kw.shape != (len(dates), STEPS_PER_DAY_DECISION):
        raise ValueError(
            f"load_matrix_kw must have shape {(len(dates), STEPS_PER_DAY_DECISION)}, "
            f"got {load_matrix_kw.shape}."
        )
    records = []
    for day_index, date_value in enumerate(dates):
        for step in range(STEPS_PER_DAY_DECISION):
            records.append(
                {
                    "date": date_value,
                    "step": step,
                    "hour": float(step * STEP_HOURS_DECISION),
                    "load_kw": float(load_matrix_kw[day_index, step]),
                }
            )
    return pd.DataFrame.from_records(records)


def write_annual_results(
    per_block: pd.DataFrame,
    load_matrix_kw: np.ndarray,
    *,
    start_date: str | dt.date | pd.Timestamp = FEED_YEAR_START,
    end_date: str | dt.date | pd.Timestamp = FEED_YEAR_END,
    per_block_path: str | Path = DEFAULT_PER_BLOCK_PATH,
    load_profile_path: str | Path = DEFAULT_LOAD_PROFILE_PATH,
) -> tuple[Path, Path]:
    """Write annual per-block metrics and load profile parquet outputs."""
    per_block_out = Path(per_block_path)
    load_out = Path(load_profile_path)
    per_block_out.parent.mkdir(parents=True, exist_ok=True)
    load_out.parent.mkdir(parents=True, exist_ok=True)
    per_block.to_parquet(per_block_out)
    annual_load_matrix_to_frame(load_matrix_kw, start_date, end_date).to_parquet(load_out, index=False)
    return per_block_out, load_out
