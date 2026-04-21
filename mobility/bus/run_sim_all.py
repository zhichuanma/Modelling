"""Run single-day CC-CV SOC simulation on every block in all_blocks.parquet."""

from __future__ import annotations

import time
from pathlib import Path

import numpy as np
import pandas as pd

from mobility.bus.sim_adapter import simulate_bus_fleet, DEFAULT_BATTERY_KWH, load_profile_times


HERE = Path(__file__).resolve().parent
OUT = HERE.parents[1] / "outputs"


def main():
    all_blocks = pd.read_parquet(OUT / "all_blocks.parquet")
    print(f"Loaded {len(all_blocks):,} trips, {all_blocks.block_id.nunique():,} blocks")

    # Filter extreme outliers from source-data bugs
    blk_km = all_blocks.groupby("block_id")["distance_km"].sum()
    bad = blk_km[blk_km > 1000].index
    if len(bad):
        all_blocks = all_blocks[~all_blocks.block_id.isin(bad)]
        print(f"  dropped {len(bad)} blocks with >1000 km/day → {all_blocks.block_id.nunique():,} blocks remain")

    t0 = time.time()
    per_bus, fleet_load = simulate_bus_fleet(
        all_blocks,
        battery_kwh=DEFAULT_BATTERY_KWH,
        progress_interval=25_000,
    )
    print(f"Sim time: {time.time()-t0:.1f}s")

    per_bus.to_parquet(OUT / "sim_per_bus.parquet")
    np.save(OUT / "sim_fleet_load_kw.npy", fleet_load)
    pd.DataFrame({
        "time_h": load_profile_times(),
        "fleet_load_kw": fleet_load,
    }).to_csv(OUT / "sim_fleet_load.csv", index=False)

    print("\n=== UK e-bus fleet — single-day CC-CV sim ===")
    print(f"  buses:            {len(per_bus):>10,}")
    print(f"  km/day:           {per_bus.total_km.sum()/1e6:>10.2f} million km")
    print(f"  energy demand:    {per_bus.energy_demand_kwh.sum()/1e6:>10.2f} GWh/day")
    print(f"  energy charged:   {per_bus.energy_charged_kwh.sum()/1e6:>10.2f} GWh/day")
    print(f"  mean SOC_end:     {per_bus.soc_end.mean():>10.3f}")
    print(f"  buses SOC_end<0.2:{(per_bus.soc_end<0.2).sum():>10,}")
    peak_i = int(fleet_load.argmax())
    print(f"  peak fleet load:  {fleet_load.max()/1e3:>10.1f} MW at {peak_i*0.25:.2f}h")
    off_peak = fleet_load[:16].mean()  # 00:00-04:00
    print(f"  00-04h avg:       {off_peak/1e3:>10.2f} MW")


if __name__ == "__main__":
    main()
