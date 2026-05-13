"""Annual synthetic-chain simulation for coach journeys."""

from __future__ import annotations

import datetime as dt
from typing import Any

import numpy as np
import pandas as pd

from mobility.core.constants import DEFAULT_CHEMISTRY, STEP_HOURS_DECISION, STEPS_PER_DAY_DECISION
from mobility.core.simulator import simulate_single_ev

from .feasibility import journey_feasibility
from .sim_adapter import DEFAULT_TERMINUS_CHARGE_KW
from .year_schedule import annual_dates, chain_to_year_schedules
from .coach_fleet import sample_coach_ev


def _spec_value(spec: pd.Series | dict[str, Any], keys: tuple[str, ...], default: Any = None) -> Any:
    for key in keys:
        if isinstance(spec, pd.Series):
            if key in spec.index and pd.notna(spec[key]):
                return spec[key]
        elif key in spec and pd.notna(spec[key]):
            return spec[key]
    return default


def _battery_kwh(ev_spec: pd.Series | dict[str, Any]) -> float:
    value = _spec_value(ev_spec, ("Energy_kWh", "battery_kwh", "battery_capacity_kwh"))
    if value is None:
        raise ValueError("ev_spec must include Energy_kWh or battery_kwh.")
    value = float(value)
    if value <= 0.0:
        raise ValueError("battery_kwh must be positive.")
    return value


def _consumption_kwh_per_km(ev_spec: pd.Series | dict[str, Any]) -> float:
    value = _spec_value(ev_spec, ("consumption_kwh_per_km",))
    if value is None:
        raise ValueError("ev_spec must include consumption_kwh_per_km.")
    value = float(value)
    if value <= 0.0:
        raise ValueError("consumption_kwh_per_km must be positive.")
    return value


def _ev_id(ev_spec: pd.Series | dict[str, Any]) -> str:
    return str(_spec_value(ev_spec, ("EV_ID", "ev_id", "Model", "model"), ""))


def _derive_soc_init(
    soc_init: float | None,
    *,
    pre_journey_dwell_h: float,
    terminus_charge_kw: float,
    battery_kwh: float,
) -> float:
    if soc_init is not None:
        return float(np.clip(float(soc_init), 0.0, 1.0))
    derived = 1.0 - (float(pre_journey_dwell_h) * float(terminus_charge_kw) / float(battery_kwh))
    return float(np.clip(derived, 0.0, 1.0))


def _first_floor_hit_h(soc: np.ndarray) -> float | None:
    hits = np.flatnonzero(np.asarray(soc, dtype=float) <= 1e-12)
    if hits.size == 0:
        return None
    return float((int(hits[0]) + 1) * STEP_HOURS_DECISION)


def _coerce_date(value: Any) -> dt.date:
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    if isinstance(value, pd.Timestamp):
        return value.date()
    return pd.Timestamp(value).date()


def _simulate_with_annual_warmup(
    schedules,
    battery_kwh: float,
    *,
    soc_init: float,
    warm_up_days: int,
    chemistry: str,
) -> tuple[np.ndarray, np.ndarray, float]:
    """Run the bus-style calendar-day warm-up window.

    Note: Coach chain templates are sparse compared to bus blocks--a template
    may be inactive in the first few calendar days of the feed year. As a
    result the warm_up_days window often contains many inactive (24h depot
    dwell) days that do not drive realistic SoC burn-in. Recommend
    warm_up_days >= 21 for production runs to raise the probability that the
    first real journey lands inside the warm-up window.
    """
    if warm_up_days < 0:
        raise ValueError("warm_up_days must be non-negative.")
    if warm_up_days == 0:
        return simulate_single_ev(
            schedules,
            battery_kwh,
            soc_init=soc_init,
            warm_up_days=0,
            chemistry=chemistry,
        )
    if warm_up_days >= len(schedules):
        raise ValueError("warm_up_days must be smaller than the annual schedule length.")

    _, _, soc_after_warmup = simulate_single_ev(
        schedules[: warm_up_days + 1],
        battery_kwh,
        soc_init=soc_init,
        warm_up_days=warm_up_days,
        chemistry=chemistry,
    )
    soc, load_kw, _ = simulate_single_ev(
        schedules,
        battery_kwh,
        soc_init=soc_after_warmup,
        warm_up_days=0,
        chemistry=chemistry,
    )
    return soc, load_kw, float(soc_after_warmup)


def _infeasibility_reasons(
    chain_journeys: pd.DataFrame,
    *,
    battery_kwh: float,
    consumption_kwh_per_km: float,
    soc: np.ndarray,
) -> list[str]:
    reasons: set[str] = set()
    distances = pd.to_numeric(chain_journeys.get("distance_km"), errors="coerce")
    if distances.isna().any():
        reasons.add("missing_distance")
    for distance_km in distances.dropna():
        feasibility = journey_feasibility(
            float(distance_km),
            battery_kwh=battery_kwh,
            consumption_kwh_per_km=consumption_kwh_per_km,
        )
        if not bool(feasibility["feasible_single_charge"]):
            reasons.add("single_charge_shortfall")
            break
    if _first_floor_hit_h(soc) is not None:
        reasons.add("soc_floor_hit")
    return sorted(reasons)


def simulate_coach_chain_year(
    chain_id: str,
    chain_journeys: pd.DataFrame,
    ev_spec,
    active_dates,
    *,
    warm_up_days: int = 0,
    soc_init: float | None = None,
    terminus_charge_kw: float = DEFAULT_TERMINUS_CHARGE_KW,
    chemistry: str = DEFAULT_CHEMISTRY,
    pre_journey_dwell_h: float = 6.0,
) -> dict:
    """Simulate one synthetic coach chain across the coach feed year."""
    if chain_journeys.empty:
        raise ValueError("chain_journeys must contain at least one journey.")
    active_date_tuple = tuple(_coerce_date(value) for value in active_dates)
    battery_kwh = _battery_kwh(ev_spec)
    consumption_kwh_per_km = _consumption_kwh_per_km(ev_spec)
    start_soc = _derive_soc_init(
        soc_init,
        pre_journey_dwell_h=pre_journey_dwell_h,
        terminus_charge_kw=terminus_charge_kw,
        battery_kwh=battery_kwh,
    )
    template = chain_journeys.copy()
    template["consumption_kwh_per_km"] = consumption_kwh_per_km
    schedules = chain_to_year_schedules(
        template,
        active_date_tuple,
        pre_journey_dwell_h=pre_journey_dwell_h,
        consumption_kwh_per_km=consumption_kwh_per_km,
        terminus_charge_kw=terminus_charge_kw,
    )
    for schedule in schedules:
        schedule.ev_id = str(chain_id)
        metadata = getattr(schedule, "metadata", {})
        metadata["chain_id"] = str(chain_id)
        schedule.metadata = metadata

    soc, load_kw, soc_after_warmup = _simulate_with_annual_warmup(
        schedules,
        battery_kwh,
        soc_init=start_soc,
        warm_up_days=int(warm_up_days),
        chemistry=chemistry,
    )
    total_kwh = float(sum(trip.energy_consumed_kwh for schedule in schedules for trip in schedule.trips))
    annual_distance_km = float(sum(trip.distance_km for schedule in schedules for trip in schedule.trips))
    energy_charged_kwh = float(np.sum(load_kw) * STEP_HOURS_DECISION)
    reasons = _infeasibility_reasons(
        template,
        battery_kwh=battery_kwh,
        consumption_kwh_per_km=consumption_kwh_per_km,
        soc=soc,
    )
    active_date_set = set(active_date_tuple)
    return {
        "chain_id": str(chain_id),
        "ev_id": _ev_id(ev_spec),
        "schedules": schedules,
        "soc": soc,
        "load_kw": load_kw,
        "soc_after_warmup": float(soc_after_warmup),
        "soc_end": float(soc[-1]),
        "soc_min": float(soc.min()),
        "soc_floor_hit_h_min": _first_floor_hit_h(soc),
        "total_kwh": total_kwh,
        "total_consumed_kwh": total_kwh,
        "annual_distance_km": annual_distance_km,
        "energy_charged_kwh": energy_charged_kwh,
        "battery_kwh": battery_kwh,
        "consumption_kwh_per_km": consumption_kwh_per_km,
        "terminus_charge_kw": float(terminus_charge_kw),
        "n_active_days": int(len(active_date_set)),
        "feasible": bool(not reasons),
        "infeasibility_reasons": reasons,
    }


def _chain_group_column(chains_df: pd.DataFrame) -> str:
    for column in ("coach_chain_template_id", "coach_chain_id", "chain_id"):
        if column in chains_df.columns:
            return column
    raise ValueError("chains_df must include coach_chain_id or chain_id.")


def _chain_template(group: pd.DataFrame, journeys_df: pd.DataFrame, chain_id: str = "unknown") -> pd.DataFrame:
    if {"date", "journey_id"}.issubset(group.columns):
        per_date_sets = group.groupby("date")["journey_id"].agg(lambda s: tuple(sorted(s.astype(str))))
        if per_date_sets.nunique() > 1:
            raise AssertionError(
                f"chain template {chain_id} has inconsistent journey sets across dates: {per_date_sets.unique()}"
            )
    ordered_cols = [col for col in ("date", "position_in_chain", "journey_id") if col in group.columns]
    ordered = group.sort_values(ordered_cols, kind="stable") if ordered_cols else group
    first_rows = ordered.drop_duplicates("journey_id", keep="first")
    template = first_rows.merge(journeys_df, on="journey_id", how="left", validate="many_to_one")
    if "position_in_chain" in template.columns:
        template = template.sort_values(["position_in_chain", "journey_id"], kind="stable")
    return template


def _load_profile_frame(chain_id: str, load_kw: np.ndarray) -> pd.DataFrame:
    dates = annual_dates()
    expected = len(dates) * STEPS_PER_DAY_DECISION
    if load_kw.shape[0] != expected:
        raise ValueError(f"load_kw length {load_kw.shape[0]} does not match {expected}.")
    steps = np.tile(np.arange(STEPS_PER_DAY_DECISION), len(dates))
    return pd.DataFrame(
        {
            "chain_id": str(chain_id),
            "date": np.repeat(np.array(dates, dtype="datetime64[ns]"), STEPS_PER_DAY_DECISION),
            "step": steps,
            "hour": steps.astype(float) * STEP_HOURS_DECISION,
            "load_kw": load_kw.astype(float),
        }
    )


def simulate_coach_fleet_year(
    chains_df,
    fleet_df,
    journeys_df,
    *,
    seed: int,
    **kw,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Simulate a synthetic coach chain fleet and return metrics plus load rows."""
    if chains_df.empty:
        return pd.DataFrame(
            columns=[
                "chain_id",
                "ev_id",
                "total_kwh",
                "energy_charged_kwh",
                "terminus_charge_kw",
                "n_active_days",
                "soc_floor_hit_h_min",
                "feasible",
                "infeasibility_reasons",
            ]
        ), pd.DataFrame(columns=["chain_id", "date", "step", "hour", "load_kw"])

    group_col = _chain_group_column(chains_df)
    rng = np.random.default_rng(int(seed))
    records: list[dict[str, Any]] = []
    load_frames: list[pd.DataFrame] = []
    for chain_id, group in chains_df.groupby(group_col, sort=False):
        ev_spec = sample_coach_ev(fleet_df, rng)
        template = _chain_template(group, journeys_df, chain_id=str(chain_id))
        active_dates = sorted({_coerce_date(value) for value in group["date"]})
        try:
            result = simulate_coach_chain_year(
                str(chain_id),
                template,
                ev_spec,
                active_dates,
                **kw,
            )
            load_frames.append(_load_profile_frame(str(chain_id), result["load_kw"]))
            record = {
                "chain_id": str(chain_id),
                "ev_id": result["ev_id"],
                "total_kwh": result["total_kwh"],
                "energy_charged_kwh": result["energy_charged_kwh"],
                "terminus_charge_kw": result["terminus_charge_kw"],
                "n_active_days": result["n_active_days"],
                "soc_floor_hit_h_min": (
                    float(result["soc_floor_hit_h_min"])
                    if result["soc_floor_hit_h_min"] is not None
                    else np.nan
                ),
                "feasible": result["feasible"],
                "infeasibility_reasons": ",".join(result["infeasibility_reasons"]),
                "battery_kwh": result["battery_kwh"],
                "consumption_kwh_per_km": result["consumption_kwh_per_km"],
                "simulation_error": "",
            }
        except Exception as exc:  # noqa: BLE001 - annual smoke should keep moving
            record = {
                "chain_id": str(chain_id),
                "ev_id": _ev_id(ev_spec),
                "total_kwh": np.nan,
                "energy_charged_kwh": np.nan,
                "terminus_charge_kw": float(kw.get("terminus_charge_kw", DEFAULT_TERMINUS_CHARGE_KW)),
                "n_active_days": int(group["date"].nunique()),
                "soc_floor_hit_h_min": np.nan,
                "feasible": False,
                "infeasibility_reasons": "simulation_error",
                "battery_kwh": _battery_kwh(ev_spec),
                "consumption_kwh_per_km": _consumption_kwh_per_km(ev_spec),
                "simulation_error": str(exc),
            }
        records.append(record)

    per_chain = pd.DataFrame.from_records(records)
    load_profile = (
        pd.concat(load_frames, ignore_index=True)
        if load_frames
        else pd.DataFrame(columns=["chain_id", "date", "step", "hour", "load_kw"])
    )
    return per_chain, load_profile


__all__ = ["simulate_coach_chain_year", "simulate_coach_fleet_year"]
