from pathlib import Path
from textwrap import dedent

import nbformat as nbf


ROOT = Path(__file__).resolve().parents[2]
OUT_PATH = ROOT / "Modelling" / "notebooks" / "01_single_bus_simulation.ipynb"


def md(text: str):
    return nbf.v4.new_markdown_cell(dedent(text).strip())


def code(text: str):
    return nbf.v4.new_code_cell(dedent(text).strip())


cells = []

cells.append(
    md(
        """
        # 01 Single Bus Simulation

        This notebook follows one representative bus block from the frozen `all_blocks.parquet` table into a multi-day-safe SOC simulation. It is deliberately honest about what the GTFS-derived table can and cannot say: native blocks are cleaner than inferred blocks, shape distance is not always available, depot charging is still a terminus abstraction, and there is no calendar-aware service day yet.
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

        from mobility.core import DailySchedule, ParkingEvent, simulate_single_day
        from mobility.core.constants import CV_THRESHOLD, DEFAULT_CHEMISTRY
        from mobility.core.simulator import STEP_HOURS, STEPS_PER_DAY
        from mobility.bus.data_loader import load_all_blocks, summarize_block_quality
        from mobility.bus.selection import (
            render_block_identity_card,
            sample_contrast_block,
            sample_protagonist_block,
        )
        from mobility.bus.trip_chain_bus import block_to_daily_schedules
        from mobility.bus.sim_adapter import simulate_block

        BLOCKS_PATH = REPO_ROOT / "outputs" / "all_blocks.parquet"
        MAIN_BUS_SEED = 20260430
        ALT_BUS_SEED = MAIN_BUS_SEED + 1
        BUS_BATTERY_KWH = 300.0
        BUS_CONSUMPTION_KWH_PER_KM = 1.2
        DEPOT_CHARGE_KW = 100.0

        plt.rcParams["figure.figsize"] = (12, 4.5)
        plt.rcParams["figure.dpi"] = 110
        plt.rcParams["axes.spines.top"] = False
        plt.rcParams["axes.spines.right"] = False
        """
    )
)

cells.append(md("## 0. Units & time grid"))

cells.append(
    code(
        """
        stub_schedule = DailySchedule(
            ev_id="bus_stage0_stub",
            day=0,
            day_type="representative_service_day",
            parking_events=[
                ParkingEvent(
                    start_time=0.0,
                    end_time=18.0,
                    duration_hours=18.0,
                    location_purpose="depot_terminus",
                    can_charge=False,
                    charge_power_kw=0.0,
                ),
                ParkingEvent(
                    start_time=18.0,
                    end_time=24.0,
                    duration_hours=6.0,
                    location_purpose="depot_terminus",
                    can_charge=True,
                    charge_power_kw=100.0,
                ),
            ],
        )
        stub_soc, stub_load_kw, stub_soc_end = simulate_single_day(
            stub_schedule,
            battery_capacity_kwh=300.0,
            soc_start=0.20,
        )
        unit_check = pd.DataFrame(
            [
                ("SOC steps", len(stub_soc)),
                ("load steps", len(stub_load_kw)),
                ("STEP_HOURS", STEP_HOURS),
                ("load_kw x STEP_HOURS", float(stub_load_kw.sum() * STEP_HOURS)),
                ("session energy_kwh", sum(event.energy_charged_kwh for event in stub_schedule.parking_events)),
                ("soc_end", stub_soc_end),
            ],
            columns=["metric", "value"],
        )
        display(unit_check)

        hours = np.arange(STEPS_PER_DAY) * STEP_HOURS
        fig, ax = plt.subplots(figsize=(11, 3.4))
        ax.step(hours, stub_load_kw, where="post", color="tab:blue", lw=2)
        ax.set(xlabel="Hour", ylabel="Average charging power (kW)", title="Stub depot charge on the 15-minute grid")
        ax.set_xlim(0, 24)
        ax.grid(alpha=0.25)
        plt.tight_layout()
        plt.show()
        """
    )
)

cells.append(md("## A. What `all_blocks.parquet` is"))

cells.append(
    code(
        """
        all_blocks = load_all_blocks(BLOCKS_PATH)
        quality = summarize_block_quality(all_blocks)
        display(quality.T.rename(columns={0: "value"}))

        block_stats = all_blocks.groupby("block_id").agg(
            n_trips=("trip_id", "count"),
            total_km=("distance_km", "sum"),
            start_h=("start_h", "min"),
            end_h=("end_h", "max"),
            block_source=("block_source", "first"),
        )
        block_stats["span_h"] = block_stats["end_h"] - block_stats["start_h"]

        fig, axes = plt.subplots(1, 3, figsize=(13, 3.6))
        for ax, col, title in zip(
            axes,
            ["n_trips", "total_km", "span_h"],
            ["Trips per block", "Total km per block", "Block span (h)"],
        ):
            ax.hist(block_stats[col], bins=60, color="tab:blue", alpha=0.78)
            ax.set_yscale("log")
            ax.set_title(title)
            ax.grid(alpha=0.22)
        plt.tight_layout()
        plt.show()
        """
    )
)

cells.append(md("## A.5. Honest labels for the data"))

cells.append(
    code(
        """
        q = quality.iloc[0]
        honest_labels = pd.DataFrame(
            [
                ("Cross-midnight blocks", f"{q['pct_cross_midnight_blocks']:.1f}% of blocks have end_h >= 24", "Handled by splitting into day 0 and day 1 schedules."),
                ("Native vs inferred continuity", f"native {q['stop_continuity_native']:.1f}% / inferred {q['stop_continuity_inferred']:.1f}%", "Selection defaults to native; inferred quality is still reported."),
                ("Distance provenance", f"shape {q['pct_shape_distance']:.1f}% / stop_haversine {q['pct_stop_haversine_distance']:.1f}%", "Stop-haversine distance is a transparent fallback and likely underestimates by 15-25%."),
                ("Depot abstraction", "depot_terminus", "It marks first/last terminus dwell, not a real depot model."),
                ("Calendar missing", "service_id only", "The label is a representative service day, never a real date."),
            ],
            columns=["issue", "observed label", "notebook treatment"],
        )
        display(honest_labels)
        """
    )
)

cells.append(md("## B. Picking a protagonist"))

cells.append(
    code(
        """
        main_rng = np.random.default_rng(MAIN_BUS_SEED)
        alt_rng = np.random.default_rng(ALT_BUS_SEED)
        protagonist_id = sample_protagonist_block(all_blocks, main_rng)
        contrast_id = sample_contrast_block(all_blocks, alt_rng, protagonist_id)

        protagonist_card = render_block_identity_card(all_blocks, protagonist_id)
        contrast_card = render_block_identity_card(all_blocks, contrast_id)
        display(pd.concat([protagonist_card.assign(role="protagonist"), contrast_card.assign(role="contrast")], ignore_index=True))
        """
    )
)

cells.append(md("## C. Block -> schedule"))

cells.append(
    code(
        """
        protagonist_block = all_blocks[all_blocks["block_id"].astype(str) == protagonist_id].copy()
        schedules = block_to_daily_schedules(
            protagonist_block,
            ev_id=f"bus_{protagonist_id}",
            consumption_kwh_per_km=BUS_CONSUMPTION_KWH_PER_KM,
            depot_charge_kw=DEPOT_CHARGE_KW,
        )

        trip_rows = []
        parking_rows = []
        for schedule in schedules:
            day_offset = schedule.day * 24.0
            for trip in schedule.trips:
                trip_rows.append(
                    {
                        "day": schedule.day,
                        "trip_id": trip.trip_id,
                        "route_id": getattr(trip, "route_id", ""),
                        "departure_h": trip.departure_time,
                        "arrival_h": trip.arrival_time,
                        "distance_km": trip.distance_km,
                        "energy_kwh": trip.energy_consumed_kwh,
                    }
                )
            for event in schedule.parking_events:
                parking_rows.append(
                    {
                        "day": schedule.day,
                        "purpose": event.location_purpose,
                        "start_h": event.start_time,
                        "end_h": event.end_time,
                        "duration_h": event.duration_hours,
                        "can_charge": event.can_charge,
                        "charge_power_kw": event.charge_power_kw,
                    }
                )

        trips_table = pd.DataFrame(trip_rows)
        parking_table = pd.DataFrame(parking_rows)
        display(trips_table.head(20))
        display(parking_table.head(20))

        fig, ax = plt.subplots(figsize=(12, max(3.2, len(schedules) * 1.8)))
        colors = {"trip": "tab:orange", "depot_terminus": "tab:blue", "layover": "0.65"}
        y = 0
        for schedule in schedules:
            for event in schedule.parking_events:
                ax.broken_barh([(schedule.day * 24 + event.start_time, event.duration_hours)], (y - 0.35, 0.25), facecolors=colors[event.location_purpose])
            for trip in schedule.trips:
                ax.broken_barh([(schedule.day * 24 + trip.departure_time, trip.arrival_time - trip.departure_time)], (y, 0.42), facecolors=colors["trip"])
                ax.text(schedule.day * 24 + trip.departure_time, y + 0.48, str(getattr(trip, "route_id", "")), fontsize=7)
            y += 1
        ax.set_yticks(range(len(schedules)))
        ax.set_yticklabels([f"day {schedule.day}" for schedule in schedules])
        ax.set_xlabel("Hour from service-day start")
        ax.set_title("Trips, depot_terminus dwell, and layovers")
        ax.grid(axis="x", alpha=0.25)
        plt.tight_layout()
        plt.show()
        """
    )
)

cells.append(md("## D. Baseline SOC trajectory"))

cells.append(
    code(
        """
        baseline = simulate_block(
            protagonist_block,
            battery_kwh=BUS_BATTERY_KWH,
            consumption_kwh_per_km=BUS_CONSUMPTION_KWH_PER_KM,
            depot_charge_kw=DEPOT_CHARGE_KW,
            allow_layover_charging=False,
        )
        time_h = np.arange(len(baseline["soc"])) * STEP_HOURS

        fig, ax_soc = plt.subplots(figsize=(12, 4.2))
        ax_load = ax_soc.twinx()
        ax_soc.plot(time_h, baseline["soc"], color="tab:green", lw=2, label="SOC")
        ax_load.step(time_h, baseline["load_kw"], where="post", color="tab:blue", alpha=0.45, label="load_kw")
        for schedule in baseline["schedules"]:
            offset = schedule.day * 24.0
            for trip in schedule.trips:
                ax_soc.scatter(offset + trip.departure_time, max(0.0, trip.energy_consumed_kwh / BUS_BATTERY_KWH), marker="v", color="tab:red", s=30)
        ax_soc.axhline(CV_THRESHOLD[DEFAULT_CHEMISTRY], color="0.25", ls="--", lw=1, label=f"CV threshold {DEFAULT_CHEMISTRY}")
        ax_soc.set(xlabel="Hour from service-day start", ylabel="SOC", ylim=(0, 1.02))
        ax_load.set_ylabel("Charging power (kW)")
        ax_soc.set_title("Baseline: depot charging only")
        ax_soc.grid(alpha=0.25)
        plt.tight_layout()
        plt.show()

        display(pd.DataFrame([{key: baseline[key] for key in ["soc_end", "soc_min", "energy_charged_kwh", "depot_kwh", "layover_kwh", "total_consumed_kwh"]}]))
        """
    )
)

cells.append(md("## E. What if we charge during layovers"))

cells.append(
    code(
        """
        with_layover = simulate_block(
            protagonist_block,
            battery_kwh=BUS_BATTERY_KWH,
            consumption_kwh_per_km=BUS_CONSUMPTION_KWH_PER_KM,
            depot_charge_kw=DEPOT_CHARGE_KW,
            allow_layover_charging=True,
            layover_charge_kw=50.0,
            min_layover_for_charging_h=STEP_HOURS,
        )

        fig, ax = plt.subplots(figsize=(12, 4.0))
        ax.plot(np.arange(len(baseline["soc"])) * STEP_HOURS, baseline["soc"], lw=2, label="depot only")
        ax.plot(np.arange(len(with_layover["soc"])) * STEP_HOURS, with_layover["soc"], lw=2, label="depot + layover")
        ax.set(xlabel="Hour from service-day start", ylabel="SOC", ylim=(0, 1.02), title="Layover charging scenario")
        ax.grid(alpha=0.25)
        ax.legend()
        plt.tight_layout()
        plt.show()

        compare = pd.DataFrame(
            [
                {
                    "scenario": "depot_only",
                    "soc_end": baseline["soc_end"],
                    "soc_min": baseline["soc_min"],
                    "energy_charged_kwh": baseline["energy_charged_kwh"],
                    "depot_share": baseline["depot_kwh"] / max(baseline["energy_charged_kwh"], 1e-9),
                },
                {
                    "scenario": "depot_plus_layover",
                    "soc_end": with_layover["soc_end"],
                    "soc_min": with_layover["soc_min"],
                    "energy_charged_kwh": with_layover["energy_charged_kwh"],
                    "depot_share": with_layover["depot_kwh"] / max(with_layover["energy_charged_kwh"], 1e-9),
                },
            ]
        )
        display(compare)
        """
    )
)

cells.append(md("## F. Sensitivity grid"))

cells.append(
    code(
        """
        battery_grid = [200.0, 300.0, 400.0]
        consumption_grid = [0.9, 1.2, 1.5]
        rows = []
        for battery_kwh in battery_grid:
            for consumption_kwh_per_km in consumption_grid:
                result = simulate_block(
                    protagonist_block,
                    battery_kwh=battery_kwh,
                    consumption_kwh_per_km=consumption_kwh_per_km,
                    depot_charge_kw=DEPOT_CHARGE_KW,
                    allow_layover_charging=False,
                )
                rows.append(
                    {
                        "battery_kwh": battery_kwh,
                        "consumption_kwh_per_km": consumption_kwh_per_km,
                        "soc_end": result["soc_end"],
                        "soc_min": result["soc_min"],
                        "below_0_10": result["soc_min"] < 0.10,
                    }
                )
        sensitivity = pd.DataFrame(rows)
        display(sensitivity)

        heat = sensitivity.pivot(index="battery_kwh", columns="consumption_kwh_per_km", values="soc_min")
        fig, ax = plt.subplots(figsize=(6.6, 4.6))
        im = ax.imshow(heat.values, vmin=0.0, vmax=1.0, cmap="RdYlGn", aspect="auto")
        ax.set_xticks(range(len(heat.columns)), [str(col) for col in heat.columns])
        ax.set_yticks(range(len(heat.index)), [str(idx) for idx in heat.index])
        ax.set_xlabel("consumption_kwh_per_km")
        ax.set_ylabel("battery_kwh")
        ax.set_title("Minimum SOC across battery / consumption assumptions")
        for i, battery_kwh in enumerate(heat.index):
            for j, consumption in enumerate(heat.columns):
                value = heat.loc[battery_kwh, consumption]
                marker = "!" if value < 0.10 else ""
                ax.text(j, i, f"{value:.2f}{marker}", ha="center", va="center", color="black")
        fig.colorbar(im, ax=ax, label="soc_min")
        plt.tight_layout()
        plt.show()
        """
    )
)

cells.append(md("## H. Final identity card"))

cells.append(
    code(
        """
        dwell_by_purpose = parking_table.groupby("purpose")["duration_h"].sum()
        final_card = pd.DataFrame(
            [
                {
                    "block_id": protagonist_id,
                    "agency_id": str(protagonist_block["agency_id"].iloc[0]),
                    "n_trips_original": int(protagonist_block.shape[0]),
                    "n_schedule_days": len(baseline["schedules"]),
                    "total_km": round(baseline["total_km"], 2),
                    "span_h": round(float(protagonist_block["end_h"].max() - protagonist_block["start_h"].min()), 2),
                    "depot_dwell_h": round(float(dwell_by_purpose.get("depot_terminus", 0.0)), 2),
                    "layover_dwell_h": round(float(dwell_by_purpose.get("layover", 0.0)), 2),
                    "total_consumed_kwh": round(baseline["total_consumed_kwh"], 2),
                    "battery_kwh": BUS_BATTERY_KWH,
                    "consumption_kwh_per_km": BUS_CONSUMPTION_KWH_PER_KM,
                    "depot_charge_kw": DEPOT_CHARGE_KW,
                    "soc_end_baseline": round(baseline["soc_end"], 4),
                    "soc_min_baseline": round(baseline["soc_min"], 4),
                    "soc_end_with_layover": round(with_layover["soc_end"], 4),
                    "energy_charged_kwh_baseline": round(baseline["energy_charged_kwh"], 2),
                }
            ]
        ).T.rename(columns={0: "value"})
        display(final_card)

        print(f"Notebook runtime: {time.time() - NOTEBOOK_START:.1f}s")
        """
    )
)

nb = nbf.v4.new_notebook()
nb["cells"] = cells
nb["metadata"] = {
    "kernelspec": {
        "display_name": "Python 3",
        "language": "python",
        "name": "python3",
    },
    "language_info": {"name": "python", "pygments_lexer": "ipython3"},
}

OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
nbf.write(nb, OUT_PATH)
print(f"Wrote {OUT_PATH}")
