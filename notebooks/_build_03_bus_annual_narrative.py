from pathlib import Path
from textwrap import dedent

import nbformat as nbf


ROOT = Path(__file__).resolve().parents[2]
OUT_PATH = ROOT / "Modelling" / "notebooks" / "03_bus_annual_simulation.ipynb"


def md(text: str):
    return nbf.v4.new_markdown_cell(dedent(text).strip())


def code(text: str):
    return nbf.v4.new_code_cell(dedent(text).strip())


cells = []

cells.append(
    md(
        """
        # 03 Bus Annual Simulation

        This notebook moves the bus model from one representative block day to a dated feed-year simulation. The current GTFS timetable covers `2026-04-17` to `2027-04-16`, so this is a feed-year model, not a 2025 calendar-year model. SOC is carried continuously across days, and inactive days are represented as all-day `depot_terminus` dwell so charging can recover between service days.
        """
    )
)

cells.append(
    code(
        """
        import sys
        import time
        from pathlib import Path

        import numpy as np
        import pandas as pd
        import matplotlib.pyplot as plt
        from IPython.display import display

        NOTEBOOK_START = time.time()
        NOTEBOOK_DIR = Path.cwd()
        REPO_ROOT = NOTEBOOK_DIR.parent if (NOTEBOOK_DIR.parent / "mobility").exists() else NOTEBOOK_DIR
        if not (REPO_ROOT / "mobility").exists():
            REPO_ROOT = Path.cwd().parent
        sys.path.insert(0, str(REPO_ROOT))

        from mobility.core.simulator import STEP_HOURS, STEPS_PER_DAY
        from mobility.bus import (
            FEED_YEAR_END,
            FEED_YEAR_START,
            build_service_date_index,
            load_all_blocks,
            load_bus_vehicle_params,
            load_service_calendar,
            sample_bus_vehicle_specs,
            simulate_block_year,
            simulate_fleet_year,
            write_annual_results,
        )

        BLOCKS_PATH = REPO_ROOT / "outputs" / "all_blocks.parquet"
        VEHICLE_PARAMS_PATH = REPO_ROOT.parent / "Data" / "EV" / "EV_prepared" / "BEV_Bus_Coach_unique_with_params_with_AC.csv"
        BUS_ANNUAL_SMOKE_PER_BLOCK_PATH = REPO_ROOT / "outputs" / "bus_annual_smoke_per_block.parquet"
        BUS_ANNUAL_SMOKE_LOAD_PROFILE_PATH = REPO_ROOT / "outputs" / "bus_annual_smoke_load_profile.parquet"
        MAIN_BUS_ANNUAL_SEED = 20260505

        plt.rcParams["figure.figsize"] = (12, 4.5)
        plt.rcParams["figure.dpi"] = 110
        plt.rcParams["axes.spines.top"] = False
        plt.rcParams["axes.spines.right"] = False
        """
    )
)

cells.append(md("## A. Feed-Year Calendar"))

cells.append(
    code(
        """
        all_blocks = load_all_blocks(BLOCKS_PATH)
        service_calendar = load_service_calendar()
        service_date_index = build_service_date_index(
            all_blocks["service_id"].astype(str).unique(),
            FEED_YEAR_START,
            FEED_YEAR_END,
            service_calendar,
        )
        active_days_by_service = pd.Series(
            {service_id: len(dates) for service_id, dates in service_date_index.items()},
            name="active_days",
        )
        display(
            pd.DataFrame(
                [
                    ("feed_year_start", FEED_YEAR_START.isoformat()),
                    ("feed_year_end", FEED_YEAR_END.isoformat()),
                    ("gtfs_calendar_services", service_calendar.calendar["service_id"].nunique()),
                    ("gtfs_exception_services", service_calendar.calendar_dates["service_id"].nunique()),
                    ("block_services", all_blocks["service_id"].nunique()),
                    ("services_active_in_feed_year", int((active_days_by_service > 0).sum())),
                    ("services_active_in_2025", 0),
                ],
                columns=["metric", "value"],
            )
        )

        fig, ax = plt.subplots(figsize=(12, 4.5), dpi=110)
        ax.hist(active_days_by_service, bins=40, color="tab:blue", alpha=0.78)
        ax.set(title="GTFS active days per service_id in the feed year", xlabel="active days", ylabel="service_ids")
        ax.grid(alpha=0.25)
        plt.tight_layout()
        plt.show()
        """
    )
)

cells.append(md("## B. Pick a Protagonist Block"))

cells.append(
    code(
        """
        rng = np.random.default_rng(MAIN_BUS_ANNUAL_SEED)
        block_stats = all_blocks.groupby("block_id", sort=False).agg(
            agency_id=("agency_id", "first"),
            service_id=("service_id", "first"),
            block_source=("block_source", "first"),
            n_trips=("trip_id", "count"),
            total_km=("distance_km", "sum"),
            start_h=("start_h", "min"),
            end_h=("end_h", "max"),
        )
        block_stats["active_days"] = block_stats["service_id"].astype(str).map(active_days_by_service).fillna(0).astype(int)
        candidates = block_stats[
            block_stats["active_days"].gt(0)
            & block_stats["block_source"].eq("native")
            & block_stats["total_km"].between(40.0, 250.0)
        ].sort_index()
        protagonist_id = str(candidates.index[int(rng.integers(0, len(candidates)))])
        protagonist_block = all_blocks[all_blocks["block_id"].astype(str).eq(protagonist_id)].copy()
        protagonist_service_id = str(protagonist_block["service_id"].iloc[0])
        protagonist_active_dates = service_date_index[protagonist_service_id]

        vehicle_params = load_bus_vehicle_params(VEHICLE_PARAMS_PATH)
        protagonist_vehicle = sample_bus_vehicle_specs(vehicle_params, rng, n=1).iloc[0]
        display(block_stats.loc[[protagonist_id]])
        display(pd.DataFrame([protagonist_vehicle]))
        """
    )
)

cells.append(md("## C. Single Block Annual SOC"))

cells.append(
    code(
        """
        protagonist_result = simulate_block_year(
            protagonist_block,
            protagonist_active_dates,
            protagonist_vehicle,
            FEED_YEAR_START,
            FEED_YEAR_END,
            soc_init=1.0,
        )
        display(
            pd.DataFrame(
                [
                    ("active_days", protagonist_result["active_days"]),
                    ("annual_distance_km", round(protagonist_result["annual_distance_km"], 2)),
                    ("annual_energy_kwh", round(protagonist_result["annual_energy_kwh"], 2)),
                    ("energy_charged_kwh", round(protagonist_result["energy_charged_kwh"], 2)),
                    ("soc_min", round(protagonist_result["soc_min"], 4)),
                    ("soc_end", round(protagonist_result["soc_end"], 4)),
                ],
                columns=["metric", "value"],
            )
        )

        dates = pd.date_range(FEED_YEAR_START, FEED_YEAR_END, freq="D")
        soc_days = protagonist_result["soc"].reshape(len(dates), STEPS_PER_DAY)
        fig, ax = plt.subplots(figsize=(12, 4.5), dpi=110)
        ax.plot(dates, soc_days.min(axis=1), color="tab:red", lw=1.4, label="daily min SOC")
        ax.plot(dates, soc_days[:, -1], color="tab:green", lw=1.4, label="end-of-day SOC")
        ax.set(title="Protagonist block annual SOC continuity", ylabel="SOC")
        ax.legend()
        ax.grid(alpha=0.25)
        plt.tight_layout()
        plt.show()
        """
    )
)

cells.append(md("## D. Small Fleet Annual Load"))

cells.append(
    code(
        """
        sample_block_ids = candidates.sample(n=min(8, len(candidates)), random_state=MAIN_BUS_ANNUAL_SEED).index.astype(str)
        small_fleet_blocks = all_blocks[all_blocks["block_id"].astype(str).isin(sample_block_ids)].copy()
        fleet_per_block, fleet_load_kw = simulate_fleet_year(
            small_fleet_blocks,
            service_date_index,
            vehicle_params=vehicle_params,
            vehicle_rng=np.random.default_rng(MAIN_BUS_ANNUAL_SEED + 1),
            start_date=FEED_YEAR_START,
            end_date=FEED_YEAR_END,
            progress_interval=4,
        )
        display(fleet_per_block[[
            "agency_id",
            "service_id",
            "active_days",
            "annual_distance_km",
            "annual_energy_kwh",
            "soc_min",
            "vehicle_gen_model",
        ]])

        daily_energy = fleet_load_kw.sum(axis=1) * STEP_HOURS
        per_block_path, load_profile_path = write_annual_results(
            fleet_per_block,
            fleet_load_kw,
            start_date=FEED_YEAR_START,
            end_date=FEED_YEAR_END,
            per_block_path=BUS_ANNUAL_SMOKE_PER_BLOCK_PATH,
            load_profile_path=BUS_ANNUAL_SMOKE_LOAD_PROFILE_PATH,
        )
        display(
            pd.DataFrame(
                [
                    ("per_block_path", str(per_block_path)),
                    ("load_profile_path", str(load_profile_path)),
                    ("per_block_rows", len(fleet_per_block)),
                    ("load_profile_rows", int(fleet_load_kw.shape[0] * fleet_load_kw.shape[1])),
                    ("daily_energy_kwh_min", round(float(daily_energy.min()), 2)),
                    ("daily_energy_kwh_max", round(float(daily_energy.max()), 2)),
                ],
                columns=["metric", "value"],
            )
        )

        fig, ax = plt.subplots(figsize=(12, 4.5), dpi=110)
        ax.plot(dates, daily_energy, color="tab:blue", lw=1.4)
        ax.set(title="Small bus-fleet annual charging energy", ylabel="kWh/day")
        ax.grid(alpha=0.25)
        plt.tight_layout()
        plt.show()
        """
    )
)

cells.append(md("## E. Honest Labels"))

cells.append(
    code(
        """
        honest_labels = pd.DataFrame(
            [
                ("Calendar window", f"{FEED_YEAR_START} to {FEED_YEAR_END}", "This follows the current GTFS feed-year, not 2025."),
                ("SOC policy", "continuous", "SOC carries across active and inactive days."),
                ("Inactive days", "all-day depot_terminus", "They allow charging recovery between service days."),
                ("Fleet scale", f"{len(sample_block_ids)} sampled blocks", "The notebook is intentionally small; full fleet uses the same simulate_fleet_year API."),
                ("Smoke outputs", f"{BUS_ANNUAL_SMOKE_PER_BLOCK_PATH.name}; {BUS_ANNUAL_SMOKE_LOAD_PROFILE_PATH.name}", "These files are smoke artifacts, not full-fleet production outputs."),
                ("Depot model", "depot_terminus abstraction", "No real depot assignment has been added yet."),
                ("Vehicle assignment", "one sampled EV spec per block", "The sampled bus model remains fixed for that block's feed-year."),
            ],
            columns=["label", "value", "treatment"],
        )
        display(honest_labels)
        print(f"Notebook wall-clock seconds: {time.time() - NOTEBOOK_START:.2f}")
        """
    )
)


nb = nbf.v4.new_notebook()
nb["cells"] = cells
nb["metadata"] = {
    "kernelspec": {"display_name": "Python 3", "language": "python", "name": "python3"},
    "language_info": {"name": "python", "pygments_lexer": "ipython3"},
}

OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
nbf.write(nb, OUT_PATH)
print(f"Wrote {OUT_PATH}")
