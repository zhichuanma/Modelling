"""Result analysis and export utilities.

Unit conventions
----------------
- load_profile[step] is the AVERAGE POWER (kW) over that step,
  NOT energy. The step duration is controlled by STEP_HOURS.
- energy_kwh_step = load_profile[step] * STEP_HOURS
- All exported DataFrame columns carrying a physical quantity
  must use an explicit unit suffix:
    power   -> _kw
    energy  -> _kwh
    SOC     -> _soc (dimensionless, 0..1)
    distance-> _km
    time    -> _h or _min
"""

from pathlib import Path
from typing import Dict, Optional

import numpy as np
import pandas as pd

from .simulator import STEP_HOURS, STEPS_PER_DAY


def aggregate_load_profile(
    results: Dict[str, dict],
    num_days: Optional[int] = None,
) -> np.ndarray:
    """Sum per-EV load profiles into a fleet-level load curve.

    Returns
    -------
    total_load : np.ndarray of shape (num_steps,) - total kW at each 15-min step
    """
    total = None
    for result in results.values():
        load = result["load"]
        if total is None:
            total = load.copy()
        else:
            total += load
    return total if total is not None else np.array([])


def average_daily_load_profile(
    results: Dict[str, dict],
    num_days: int,
) -> np.ndarray:
    """Average the fleet load profile across days -> shape (96,).

    Useful for plotting a representative daily load curve.
    """
    total = aggregate_load_profile(results)
    if len(total) == 0:
        return np.zeros(STEPS_PER_DAY)
    daily = total.reshape(num_days, STEPS_PER_DAY)
    return daily.mean(axis=0)


def single_ev_soc_series(
    results: Dict[str, dict],
    ev_id: str,
) -> pd.Series:
    """Return the SOC time-series for a single EV as a pandas Series."""
    soc = results[ev_id]["soc"]
    hours = np.arange(len(soc)) * STEP_HOURS
    return pd.Series(soc, index=hours, name=f"SOC_{ev_id}")


def compute_statistics(
    results: Dict[str, dict],
    ev_fleet: pd.DataFrame,
    num_days: int,
) -> pd.DataFrame:
    """Compute per-EV summary statistics."""
    battery_map = ev_fleet.set_index("EV_ID")["battery_capacity_kwh"].to_dict()
    rows = []
    for ev_id, result in results.items():
        soc = result["soc"]
        load = result["load"]
        cap = battery_map.get(ev_id, 60.0)
        if pd.isna(cap):
            cap = 60.0

        total_charge = load.sum() * STEP_HOURS
        mean_daily_charge = total_charge / num_days if num_days > 0 else 0

        rows.append(
            {
                "ev_id": ev_id,
                "mean_daily_charge_kwh": mean_daily_charge,
                "peak_load_kw": load.max(),
                "min_soc": soc.min(),
                "mean_soc": soc.mean(),
                "max_soc": soc.max(),
                "battery_capacity_kwh": cap,
            }
        )

    return pd.DataFrame(rows)


def fleet_summary(
    results: Dict[str, dict],
    ev_fleet: pd.DataFrame,
    num_days: int,
) -> dict:
    """Compute fleet-level aggregate statistics."""
    total_load = aggregate_load_profile(results)
    stats_df = compute_statistics(results, ev_fleet, num_days)

    return {
        "num_evs": len(results),
        "num_days": num_days,
        "peak_fleet_load_kw": float(total_load.max()) if len(total_load) else 0,
        "mean_daily_fleet_charge_kwh": float(stats_df["mean_daily_charge_kwh"].sum()),
        "fleet_min_soc": float(stats_df["min_soc"].min()),
        "fleet_mean_soc": float(stats_df["mean_soc"].mean()),
    }


def export_results(
    results: Dict[str, dict],
    ev_fleet: pd.DataFrame,
    num_days: int,
    output_dir: Path,
) -> None:
    """Export simulation results to CSV and numpy files."""
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    total_load = aggregate_load_profile(results)
    hours = np.arange(len(total_load)) * STEP_HOURS
    load_df = pd.DataFrame({"hour": hours, "total_kw": total_load})
    load_df.to_csv(output_dir / "fleet_load_profile.csv", index=False)

    stats_df = compute_statistics(results, ev_fleet, num_days)
    stats_df.to_csv(output_dir / "ev_statistics.csv", index=False)

    np.save(output_dir / "fleet_load.npy", total_load)

    ev_ids = sorted(results.keys())
    n_steps = len(total_load) if len(total_load) else 0
    if n_steps > 0:
        soc_mmap = np.lib.format.open_memmap(
            str(output_dir / "fleet_soc.npy"),
            mode="w+",
            dtype=np.float32,
            shape=(len(ev_ids), n_steps),
        )
        for index, ev_id in enumerate(ev_ids):
            soc_mmap[index] = results[ev_id]["soc"]
        del soc_mmap

    pd.Series(ev_ids, name="ev_id").to_csv(output_dir / "ev_id_order.csv", index=False)
