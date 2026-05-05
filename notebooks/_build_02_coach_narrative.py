from pathlib import Path
from textwrap import dedent

import nbformat as nbf


ROOT = Path(__file__).resolve().parents[2]
OUT_PATH = ROOT / "Modelling" / "notebooks" / "02_single_coach_simulation.ipynb"


def md(text: str):
    return nbf.v4.new_markdown_cell(dedent(text).strip())


def code(text: str):
    return nbf.v4.new_code_cell(dedent(text).strip())


cells = []

cells.append(
    md(
        """
        # 02 Single Coach Simulation

        This notebook follows one long-distance coach `vehicle_journey_code` from TransXChange timing data into a single-charge feasibility check and SOC simulation. Coach journeys are modelled at journey level, not block level: distance is estimated from stop coordinates with `haversine x 1.30`, and infeasible journeys are labelled before the simulator can clamp SOC to zero.
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
        from mobility.core.simulator import STEP_HOURS, STEPS_PER_DAY
        from mobility.coach import (
            journey_feasibility,
            journey_to_daily_schedules,
            load_all_coach_journeys,
            load_all_coach_stop_sequences,
            load_coach_fleet,
            render_journey_identity_card,
            sample_coach_ev,
            sample_contrast_journey,
            sample_protagonist_journey,
            simulate_coach_journey,
            summarize_journey_quality,
        )

        JOURNEYS_PATH = REPO_ROOT / "outputs" / "all_coach_journeys.parquet"
        STOP_SEQUENCES_PATH = REPO_ROOT / "outputs" / "all_coach_stop_sequences.parquet"
        MAIN_COACH_SEED = 20260501

        plt.rcParams["figure.figsize"] = (12, 4.5)
        plt.rcParams["figure.dpi"] = 110
        plt.rcParams["axes.spines.top"] = False
        plt.rcParams["axes.spines.right"] = False
        """
    )
)

cells.append(md("## Stage 0 - Units and Time Grid"))

cells.append(
    code(
        """
        stub_schedule = DailySchedule(
            ev_id="coach_stage0_stub",
            day=0,
            day_type="representative_service_day",
            parking_events=[
                ParkingEvent(0.0, 8.0, 8.0, "terminus_dwell", can_charge=True, charge_power_kw=50.0),
                ParkingEvent(12.0, 24.0, 12.0, "terminus_dwell", can_charge=True, charge_power_kw=50.0),
            ],
        )
        stub_soc, stub_load_kw, stub_soc_end = simulate_single_day(stub_schedule, 281.0, soc_start=0.40)
        display(
            pd.DataFrame(
                [
                    ("SOC steps", len(stub_soc)),
                    ("load steps", len(stub_load_kw)),
                    ("STEP_HOURS", STEP_HOURS),
                    ("coach schedule unit", "vehicle_journey_code"),
                    ("stub soc_end", stub_soc_end),
                ],
                columns=["metric", "value"],
            )
        )

        fig, ax = plt.subplots(figsize=(12, 4.5), dpi=110)
        hours = np.arange(STEPS_PER_DAY) * STEP_HOURS
        ax.step(hours, stub_load_kw, where="post", color="tab:blue", lw=2)
        ax.set(xlabel="Hour", ylabel="Average charging power (kW)", title="Terminus dwell on the 15-minute grid")
        ax.set_xlim(0, 24)
        ax.grid(alpha=0.25)
        plt.tight_layout()
        plt.show()
        """
    )
)

cells.append(md("## Stage A - Load All Coach Journeys"))

cells.append(
    code(
        """
        all_journeys = load_all_coach_journeys(JOURNEYS_PATH)
        all_stop_sequences = load_all_coach_stop_sequences(STOP_SEQUENCES_PATH)
        quality = summarize_journey_quality(all_journeys)
        display(quality.T.rename(columns={0: "value"}))

        known = all_journeys[all_journeys["distance_km"].notna()].copy()
        fig, axes = plt.subplots(1, 3, figsize=(12, 4.5), dpi=110)
        axes[0].hist(known["distance_km"], bins=50, color="tab:orange", alpha=0.82)
        axes[0].set(title="Known journey distance", xlabel="km", ylabel="journeys")
        axes[1].hist(all_journeys["runtime_min"], bins=50, color="tab:green", alpha=0.82)
        axes[1].set(title="Runtime distribution", xlabel="minutes")
        top_ops = all_journeys["operator_code"].value_counts().head(8)
        axes[2].bar(top_ops.index.astype(str), top_ops.values, color="tab:blue", alpha=0.82)
        axes[2].set(title="Operator distribution", xlabel="operator")
        axes[2].tick_params(axis="x", rotation=45)
        for ax in axes:
            ax.grid(alpha=0.2)
        plt.tight_layout()
        plt.show()
        """
    )
)

cells.append(md("## Stage A.5 - Honest Labels"))

cells.append(
    code(
        """
        q = quality.iloc[0]
        honest_labels = pd.DataFrame(
            [
                ("NaPTAN coordinate state", f"{q['known_distance_pct']:.1f}% known-distance journeys", "Missing any required stop coordinate makes the whole journey unknown."),
                ("Distance model", "haversine x 1.30", "This is not routed road distance; it is a transparent detour approximation."),
                ("Unknown-distance share", f"{q['unknown_distance_pct']:.1f}%", "Unknown-distance journeys are excluded from simulation selection."),
                ("Cross-midnight share", f"{q['cross_midnight_pct']:.1f}%", "The converter can split these, but random protagonist selection excludes them."),
                ("Calendar", "not expanded", "A row is a representative TxC vehicle journey, not a dated service."),
                ("Feasibility", "distributional", "One sampled EV spec is not a deterministic operator fleet assignment."),
            ],
            columns=["label", "observed value", "treatment"],
        )
        display(honest_labels)
        """
    )
)

cells.append(md("## Stage A.7 - Real Coach EV Fleet"))

cells.append(
    code(
        """
        coach_fleet = load_coach_fleet()
        display(
            coach_fleet[
                ["model", "count", "battery_kwh", "consumption_kwh_per_km", "range_km", "battery_source", "consumption_source", "is_simulatable"]
            ].sort_values("model")
        )

        fig, ax = plt.subplots(figsize=(12, 4.5), dpi=110)
        ordered_fleet = coach_fleet.sort_values("range_km")
        ax.barh(ordered_fleet["model"], ordered_fleet["range_km"], color="tab:green", alpha=0.78)
        ax.set(xlabel="Estimated range (km)", title="Coach EV specs from prepared vehicle table")
        ax.grid(axis="x", alpha=0.25)
        plt.tight_layout()
        plt.show()
        """
    )
)

cells.append(md("## Stage B - Random Protagonist and Contrast"))

cells.append(
    code(
        """
        rng = np.random.default_rng(MAIN_COACH_SEED)
        protagonist = sample_protagonist_journey(all_journeys, rng)
        contrast = sample_contrast_journey(all_journeys, rng, protagonist)
        ev_spec = sample_coach_ev(rng, weight_by_count=True, fleet=coach_fleet)

        protagonist_stops = all_stop_sequences[all_stop_sequences["journey_id"] == protagonist["journey_id"]].sort_values("stop_sequence")
        contrast_stops = all_stop_sequences[all_stop_sequences["journey_id"] == contrast["journey_id"]].sort_values("stop_sequence")

        display(pd.DataFrame([protagonist[["journey_id", "operator_code", "line_name", "departure_time", "arrival_time", "distance_km"]]]))
        display(pd.DataFrame([contrast[["journey_id", "operator_code", "line_name", "departure_time", "arrival_time", "distance_km"]]]))
        display(pd.DataFrame([ev_spec]))
        """
    )
)

cells.append(md("## Stage C - Daily Schedules and Gantt"))

cells.append(
    code(
        """
        schedules = journey_to_daily_schedules(
            protagonist,
            protagonist_stops,
            consumption_kwh_per_km=ev_spec["consumption_kwh_per_km"],
            terminus_charge_kw=50.0,
        )

        def plot_gantt(schedules, title):
            fig, ax = plt.subplots(figsize=(12, 4.5), dpi=110)
            for y, schedule in enumerate(schedules):
                for trip in schedule.trips:
                    ax.barh(y, trip.arrival_time - trip.departure_time, left=trip.departure_time, color="tab:orange", label="trip")
                for event in schedule.parking_events:
                    ax.barh(y, event.end_time - event.start_time, left=event.start_time, color="tab:blue", alpha=0.65, label="terminus_dwell")
            handles, labels = ax.get_legend_handles_labels()
            by_label = dict(zip(labels, handles))
            ax.legend(by_label.values(), by_label.keys(), loc="upper right")
            ax.set(yticks=range(len(schedules)), yticklabels=[f"day {s.day}" for s in schedules], xlabel="Hour", title=title)
            ax.set_xlim(0, 24)
            ax.grid(axis="x", alpha=0.25)
            plt.tight_layout()
            plt.show()

        display(pd.DataFrame([
            {
                "day": schedule.day,
                "n_trips": len(schedule.trips),
                "n_terminus_dwell": sum(event.location_purpose == "terminus_dwell" for event in schedule.parking_events),
                "trip_km": sum(trip.distance_km for trip in schedule.trips),
            }
            for schedule in schedules
        ]))
        plot_gantt(schedules, "Protagonist coach journey schedule")
        """
    )
)

cells.append(md("## Stage D - Feasibility Then Simulation"))

cells.append(
    code(
        """
        feasibility = journey_feasibility(
            protagonist["distance_km"],
            battery_kwh=ev_spec["battery_kwh"],
            consumption_kwh_per_km=ev_spec["consumption_kwh_per_km"],
        )
        result = simulate_coach_journey(protagonist, protagonist_stops, ev_spec, terminus_charge_kw=50.0, soc_init=1.0)
        display(pd.DataFrame([feasibility]))

        soc = result["soc"]
        hours = np.arange(1, len(soc) + 1) * STEP_HOURS
        fig, ax = plt.subplots(figsize=(12, 4.5), dpi=110)
        ax.plot(hours, soc, color="tab:purple", lw=2)
        ax.axhline(0.0, color="black", lw=1)
        if not feasibility["feasible_single_charge"]:
            for spine in ax.spines.values():
                spine.set_edgecolor("red")
                spine.set_linewidth(2.5)
        if result["soc_floor_hit_h"] is not None:
            ax.axvline(result["soc_floor_hit_h"], color="red", ls="--", lw=1.5)
        ax.set(xlabel="Simulation hour", ylabel="SOC", ylim=(-0.02, 1.02), title="SOC profile with explicit feasibility label")
        ax.grid(alpha=0.25)
        plt.tight_layout()
        plt.show()
        """
    )
)

cells.append(md("## Stage E - Feasibility Frontier"))

cells.append(
    code(
        """
        candidates = all_journeys[all_journeys["distance_km"].notna()].copy()
        simulatable_fleet = coach_fleet[coach_fleet["is_simulatable"]].copy()
        frontier_rows = []
        for _, ev in simulatable_fleet.iterrows():
            required = candidates["distance_km"] * ev["consumption_kwh_per_km"]
            feasible = required <= ev["battery_kwh"] * 0.95
            for operator, values in feasible.groupby(candidates["operator_code"]):
                frontier_rows.append({"EV model": ev["model"], "operator": operator, "pct_feasible": float(values.mean() * 100.0)})
        frontier = pd.DataFrame(frontier_rows)
        heat = frontier.pivot(index="operator", columns="EV model", values="pct_feasible").fillna(0.0)
        fig, ax = plt.subplots(figsize=(12, 4.5), dpi=110)
        im = ax.imshow(heat.values, aspect="auto", cmap="YlGn")
        ax.set_xticks(np.arange(heat.shape[1]), heat.columns, rotation=25, ha="right")
        ax.set_yticks(np.arange(heat.shape[0]), heat.index)
        ax.set(title="Known-distance journey feasibility by operator and EV model")
        fig.colorbar(im, ax=ax, label="% feasible")
        plt.tight_layout()
        plt.show()

        protagonist_percentile = (candidates["distance_km"] <= protagonist["distance_km"]).mean() * 100.0
        contrast_percentile = (candidates["distance_km"] <= contrast["distance_km"]).mean() * 100.0
        display(pd.DataFrame([{"journey": "protagonist", "distance_percentile": protagonist_percentile}, {"journey": "contrast", "distance_percentile": contrast_percentile}]))
        """
    )
)

cells.append(md("## Stage F - 3x3 Sensitivity"))

cells.append(
    code(
        """
        battery_values = [281.0, 400.0, 563.0]
        consumption_values = [0.81, 1.17, 1.5]
        sensitivity = pd.DataFrame(
            [
                {
                    "battery_kwh": battery,
                    "consumption_kwh_per_km": consumption,
                    "feasible_single_charge": journey_feasibility(
                        protagonist["distance_km"],
                        battery_kwh=battery,
                        consumption_kwh_per_km=consumption,
                    )["feasible_single_charge"],
                }
                for battery in battery_values
                for consumption in consumption_values
            ]
        )
        display(sensitivity.pivot(index="battery_kwh", columns="consumption_kwh_per_km", values="feasible_single_charge"))
        """
    )
)

cells.append(md("## Stage G - Optional Operator Comparison"))

cells.append(
    code(
        """
        operator_quality = (
            all_journeys.assign(known_distance=all_journeys["distance_km"].notna())
            .groupby("operator_code")
            .agg(n_journeys=("journey_id", "count"), pct_known_distance=("known_distance", lambda s: float(s.mean() * 100.0)))
            .sort_values("n_journeys", ascending=False)
        )
        included = operator_quality[operator_quality["pct_known_distance"] >= 50.0]
        skipped = operator_quality[operator_quality["pct_known_distance"] < 50.0]
        display(operator_quality)
        display(pd.DataFrame({"skipped_operator": skipped.index, "reason": "pct_known_distance < 50%"}))

        if not included.empty:
            fig, ax = plt.subplots(figsize=(12, 4.5), dpi=110)
            ax.bar(included.index.astype(str), included["pct_known_distance"], color="tab:cyan", alpha=0.8)
            ax.set(ylabel="% known distance", title="Operators retained for comparison")
            ax.tick_params(axis="x", rotation=45)
            ax.grid(axis="y", alpha=0.25)
            plt.tight_layout()
            plt.show()
        """
    )
)

cells.append(md("## Stage H - Identity Card"))

cells.append(
    code(
        """
        identity_card = render_journey_identity_card(
            protagonist,
            ev_spec,
            result["feasibility"],
            wall_clock_s=time.time() - NOTEBOOK_START,
        )
        display(identity_card.T.rename(columns={0: "value"}))
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
