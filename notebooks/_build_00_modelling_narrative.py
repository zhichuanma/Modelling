from pathlib import Path
from textwrap import dedent

import nbformat as nbf


ROOT = Path(__file__).resolve().parents[2]
OUT_PATH = ROOT / "Modelling" / "notebooks" / "00_single_car_simulation.ipynb"


def md(text: str):
    return nbf.v4.new_markdown_cell(dedent(text).strip())


def code(text: str):
    return nbf.v4.new_code_cell(dedent(text).strip())


cells = []

cells.append(
    md(
        """
        # 00 Modelling Narrative

        This notebook is the prologue to the mobility notebooks. It follows one sampled EV across the full modelling stack and asks the same three questions at every stage: why we made the choice, how the model encodes it, and what changed for this car because of it.
        """
    )
)

cells.append(
    code(
        """
        import sys
        import time
        import json
        import warnings
        import datetime as dt
        from copy import deepcopy
        from pathlib import Path

        import numpy as np
        import pandas as pd
        import matplotlib.pyplot as plt
        import pyarrow.dataset as ds
        from IPython.display import Markdown, display

        sys.path.insert(0, str(Path.cwd().parent))

        import mobility as em
        from mobility.core import simulate_single_day, simulate_single_ev, STEP_HOURS, STEPS_PER_DAY
        from mobility.core.constants import (
            STEPS_PER_DAY_DECISION,
            STEPS_PER_DAY_PROFILE,
            STEP_HOURS_DECISION,
            STEP_HOURS_PROFILE,
            SCENE_CATEGORIES,
            SEASONAL_CONSUMPTION_FACTOR,
            CV_THRESHOLD,
            DEFAULT_CHEMISTRY,
            WARMUP_DAYS,
            HOME_CHARGER_KW,
        )
        from mobility.core.spatial import load_lsoa_centroids, od_distance_km
        from mobility.core.seasonal import get_seasonal_factor
        from mobility.cars import assign_year_schedules
        from mobility.cars.holiday_rules import is_holiday_week
        from mobility.cars.station_matcher import match_stations_for_schedule
        from mobility.core.data_structures import DailySchedule, ParkingEvent

        import mobility.cars.trip_chain as trip_chain_module
        import mobility.cars.station_matcher as station_matcher_module
        import mobility.cars.week_pattern as week_pattern_module

        %matplotlib inline

        NOTEBOOK_START = time.time()
        MAIN_CAR_SEED = 20260422
        ALT_CAR_SEED = MAIN_CAR_SEED + 1

        NOTEBOOK_DIR = Path.cwd()
        REPO_ROOT = NOTEBOOK_DIR.parent.parent
        if not (REPO_ROOT / "AGENTS.md").exists():
            REPO_ROOT = NOTEBOOK_DIR

        DATA_ROOT = REPO_ROOT / "Data"
        DESTINATION_TABLE_PATH = DATA_ROOT / "Charging_stations" / "OSM_POI_Labeling" / "destination_choice_table.parquet"
        STATIONS_PATH = DATA_ROOT / "Charging_stations" / "OSM_POI_Labeling" / "UK_OCM_stations_labeled.csv"
        PERSON_FLEET_PATH = REPO_ROOT / "Modelling" / "data" / "person_fleet.parquet"
        PERSON_WEEK_LIBRARY_PATH = REPO_ROOT / "Modelling" / "data" / "person_week_library.parquet"

        plt.rcParams["figure.figsize"] = (12, 4.5)
        plt.rcParams["figure.dpi"] = 110
        plt.rcParams["axes.spines.top"] = False
        plt.rcParams["axes.spines.right"] = False
        """
    )
)

cells.append(
    md(
        """
        ## 0. Opening

        The downstream optimisation problem wants year-long, EV-level inputs: when the car is parked, where it is parked, how much charging power it can draw, and what SOC trajectory emerges under those constraints. National Travel Survey data does not give that object directly. It gives single-day fragments for people, while the optimiser needs consistent, date-stamped schedules for vehicles.

        That is why this project is a simulation pipeline rather than a statistical summary. We need to turn partial diaries into reproducible, physically coherent trajectories that can later feed LSOA-level V2G optimisation.
        """
    )
)

cells.append(
    code(
        """
        stage_map_df = pd.DataFrame(
            [
                ("Stage 0", "Interface and units", "Foundation", "Freeze units, time steps, and RNG semantics before anything else."),
                ("Stage 7", "Station attractiveness kernel", "Space", "Make Layer 2 prefer capable stations without letting mega-sites dominate linearly."),
                ("Stage 1", "Two-level Huff", "Space", "Infer destination LSOA first, then station choice inside that LSOA."),
                ("Stage 2a", "Holiday rules", "Time", "Encode bank and school holidays deterministically without extra dependencies."),
                ("Stage 2b", "Person <-> EV binding", "Time", "Keep one respondent attached to one EV for the whole simulation horizon."),
                ("Stage 2c", "Week pattern library", "Time", "Freeze finite NTS behaviour into reusable weekly patterns."),
                ("Stage 2d + 8", "Year assembly + jitter clamp", "Time", "Assemble dated schedules and perturb times without breaking the day boundary."),
                ("Stage 6", "Home charging short-circuit", "Charging mechanics", "Skip public-station search when the car is parked at home."),
                ("Stage 4", "AC charging curve heterogeneity", "Charging mechanics", "Taper NMC and LFP differently near high SOC."),
                ("Stage 3", "SOC warm-up", "Long-horizon dynamics", "Burn in the arbitrary starting SOC before reading the trace seriously."),
                ("Stage 5", "Seasonal consumption correction", "Long-horizon dynamics", "Scale trip energy by exogenous winter and summer factors."),
            ],
            columns=["Stage", "Name", "Logical chapter", "Why it matters"],
        )
        display(stage_map_df)
        print("This notebook follows the logical order Space -> Time -> Charging mechanics -> Long-horizon dynamics -> Integration, not the execution order listed in AGENTS.md section 8.")
        """
    )
)

cells.append(
    md(
        """
        ## 1. Stage 0 - Interface and unit conventions

        **Motivation.** Units and step sizes have to stop moving before the rest of the model grows around them. Otherwise a later "small" change silently changes every exported profile, every charging-energy calculation, and every test baseline.

        **Decision.** The simulator carries decision-layer state on 96 fifteen-minute steps, while exported charging load is interpreted as average power over each step. That is why `energy_kwh_step = load_profile[step] * STEP_HOURS`, why power columns end in `_kw`, energy columns in `_kwh`, SOC columns in `_soc`, and why BNG Euclidean distance is fixed project-wide.

        **Protagonist demo.** Before telling the car's story, use its battery size in a one-day sandbox to show the basic contract: the simulator returns 96-step arrays, and a power value becomes energy only after multiplying by the step duration.
        """
    )
)

cells.append(
    code(
        """
        stage0_demo_schedule = DailySchedule(
            ev_id="stage0_demo",
            day=0,
            day_type="weekday",
            parking_events=[
                ParkingEvent(
                    start_time=0.0,
                    end_time=18.0,
                    duration_hours=18.0,
                    location_purpose="home",
                    can_charge=False,
                    charge_power_kw=0.0,
                ),
                ParkingEvent(
                    start_time=18.0,
                    end_time=24.0,
                    duration_hours=6.0,
                    location_purpose="home",
                    can_charge=True,
                    charge_power_kw=HOME_CHARGER_KW,
                ),
            ],
        )
        stage0_soc, stage0_load_kw, stage0_soc_end = simulate_single_day(
            stage0_demo_schedule,
            battery_capacity_kwh=52.0,
            soc_start=0.30,
        )
        nonzero_step = int(np.flatnonzero(stage0_load_kw > 0)[0])
        stage0_unit_df = pd.DataFrame(
            {
                "metric": ["load_profile shape", "STEP_HOURS", "example step", "power_kw at step", "energy_kwh at step"],
                "value": [
                    str(stage0_load_kw.shape),
                    STEP_HOURS,
                    nonzero_step,
                    round(float(stage0_load_kw[nonzero_step]), 3),
                    round(float(stage0_load_kw[nonzero_step] * STEP_HOURS), 3),
                ],
            }
        )
        display(stage0_unit_df)

        hours_15min = np.arange(STEPS_PER_DAY) * STEP_HOURS
        fig, ax = plt.subplots(figsize=(12, 3.8))
        ax.step(hours_15min, stage0_load_kw, where="post", color="tab:blue", lw=1.8)
        ax.set_xlabel("Hour of day")
        ax.set_ylabel("Average power in step (kW)")
        ax.set_title("Stage 0 sandbox: one day of home charging under the fixed unit convention")
        ax.set_xlim(0, 24)
        ax.grid(alpha=0.25)
        plt.tight_layout()
        plt.show()
        """
    )
)

cells.append(
    md(
        """
        *Caption.* The y-axis is power, not energy. A flat 7 kW segment over one 15-minute step contributes `7 x 0.25 = 1.75 kWh`, which is exactly the convention every later stage inherits.
        """
    )
)

cells.append(
    md(
        """
        ## 2. Protagonist sampling and identity card

        One EV is drawn with `MAIN_CAR_SEED = 20260422`. The notebook keeps drawing from `person_fleet.parquet` until the row passes three filters: `nts_region` is present, the bound `person_id` has at least one frozen weekly pattern, and the EV exists in the runtime fleet table. If the first draw fails, the notebook keeps advancing the same RNG rather than re-seeding.
        """
    )
)

cells.append(
    code(
        """
        try:
            trips_df, fleet_df, stations_df = em.load_all()
        except (AttributeError, KeyError):
            trips_df = em.load_nts_trips()
            fleet_df = em.load_ev_fleet()
            stations_df = pd.read_csv(STATIONS_PATH)

        fleet_df = fleet_df.copy()
        fleet_df["EV_ID"] = fleet_df["EV_ID"].astype(str)
        if "home_lsoa" not in fleet_df.columns:
            fleet_df["home_lsoa"] = fleet_df["LSOA_code"].astype(str)
        else:
            fleet_df["home_lsoa"] = fleet_df["home_lsoa"].fillna(fleet_df["LSOA_code"]).astype(str)

        stations_df = stations_df.copy()
        stations_df["lsoa_code"] = stations_df["lsoa_code"].astype(str)
        person_fleet_df = pd.read_parquet(PERSON_FLEET_PATH).copy()
        person_fleet_df["ev_id"] = person_fleet_df["ev_id"].astype(str)
        person_fleet_df["person_id"] = person_fleet_df["person_id"].astype(str)
        library_df = pd.read_parquet(PERSON_WEEK_LIBRARY_PATH).copy()
        centroids_df = load_lsoa_centroids()

        valid_person_ids = set(library_df["person_id"].astype(str))
        valid_ev_ids = set(fleet_df["EV_ID"].astype(str))

        rng = np.random.default_rng(MAIN_CAR_SEED)
        protagonist_attempts = 0
        while True:
            protagonist_idx = int(rng.integers(0, len(person_fleet_df)))
            protagonist_attempts += 1
            candidate = person_fleet_df.iloc[protagonist_idx]
            if pd.isna(candidate["nts_region"]) or str(candidate["nts_region"]).strip() == "":
                continue
            if candidate["person_id"] not in valid_person_ids:
                continue
            if candidate["ev_id"] not in valid_ev_ids:
                continue
            protagonist_row = candidate.copy()
            protagonist_fleet_row = fleet_df.loc[fleet_df["EV_ID"] == protagonist_row["ev_id"]].iloc[0].copy()
            break

        rng_alt = np.random.default_rng(ALT_CAR_SEED)
        while True:
            contrast_idx = int(rng_alt.integers(0, len(person_fleet_df)))
            candidate = person_fleet_df.iloc[contrast_idx]
            if pd.isna(candidate["nts_region"]) or str(candidate["nts_region"]).strip() == "":
                continue
            if candidate["person_id"] not in valid_person_ids:
                continue
            if candidate["ev_id"] not in valid_ev_ids:
                continue
            if candidate["ev_id"] == protagonist_row["ev_id"]:
                continue
            contrast_row = candidate.copy()
            contrast_fleet_row = fleet_df.loc[fleet_df["EV_ID"] == contrast_row["ev_id"]].iloc[0].copy()
            if str(contrast_row["nts_region"]) != str(protagonist_row["nts_region"]) or str(contrast_fleet_row["Model"]) != str(protagonist_fleet_row["Model"]):
                break

        protagonist_person_df = person_fleet_df.loc[person_fleet_df["ev_id"] == protagonist_row["ev_id"]].iloc[[0]].copy()
        protagonist_fleet_df = fleet_df.loc[fleet_df["EV_ID"] == protagonist_row["ev_id"]].iloc[[0]].copy()
        protagonist_library_df = library_df.loc[library_df["person_id"].astype(str) == protagonist_row["person_id"]].copy()

        if "chemistry" in protagonist_fleet_row.index and pd.notna(protagonist_fleet_row["chemistry"]):
            protagonist_chemistry = str(protagonist_fleet_row["chemistry"])
            protagonist_chemistry_note = "from fleet row"
        else:
            protagonist_chemistry = DEFAULT_CHEMISTRY
            protagonist_chemistry_note = "runtime fallback because the current EV CSV has no chemistry column"
        contrast_chemistry = "LFP"

        region_text = str(protagonist_row["nts_region"]).strip().lower().replace("-", "_").replace(" ", "_")
        protagonist_holiday_region = {"northern_ireland": "ni"}.get(region_text, region_text)
        if protagonist_holiday_region not in {"england", "wales", "scotland", "ni"}:
            protagonist_holiday_region = "england"

        def render_identity_card(rows):
            lines = ["| Field | Value |", "|---|---|"]
            for field, value in rows:
                safe_field = str(field).replace("|", "\\|")
                safe_value = str(value).replace("|", "\\|")
                lines.append(f"| {safe_field} | {safe_value} |")
            display(Markdown("\\n".join(lines)))
            return pd.DataFrame(rows, columns=["Field", "Value"])

        identity_card_stage2 = render_identity_card(
            [
                ("sampling_seed", MAIN_CAR_SEED),
                ("sampling_attempts", protagonist_attempts),
                ("ev_id", protagonist_row["ev_id"]),
                ("person_id", protagonist_row["person_id"]),
                ("home_lsoa", protagonist_fleet_row["home_lsoa"]),
                ("nts_region", protagonist_row["nts_region"]),
                ("holiday_region", protagonist_holiday_region),
                ("model", protagonist_fleet_row["Model"]),
                ("battery_capacity_kwh", float(protagonist_fleet_row["battery_capacity_kwh"])),
                ("consumption_kwh_per_km", float(protagonist_fleet_row["consumption_kwh_per_km"])),
                ("chemistry", protagonist_chemistry),
                ("chemistry_note", protagonist_chemistry_note),
                ("contrast_car_ev_id", contrast_row["ev_id"]),
                ("contrast_car_model", contrast_fleet_row["Model"]),
                ("contrast_car_region", contrast_row["nts_region"]),
            ]
        )

        comparison_df = pd.DataFrame(
            {
                "car": ["Protagonist", "Contrast"],
                "battery_capacity_kwh": [float(protagonist_fleet_row["battery_capacity_kwh"]), float(contrast_fleet_row["battery_capacity_kwh"])],
                "consumption_kwh_per_km": [float(protagonist_fleet_row["consumption_kwh_per_km"]), float(contrast_fleet_row["consumption_kwh_per_km"])],
            }
        )
        fig, axes = plt.subplots(1, 2, figsize=(10, 3.8))
        axes[0].bar(comparison_df["car"], comparison_df["battery_capacity_kwh"], color=["tab:blue", "tab:orange"])
        axes[0].set_ylabel("Battery capacity (kWh)")
        axes[0].set_title("Identity card anchor: battery size")
        axes[1].bar(comparison_df["car"], comparison_df["consumption_kwh_per_km"], color=["tab:blue", "tab:orange"])
        axes[1].set_ylabel("Consumption (kWh/km)")
        axes[1].set_title("Identity card anchor: efficiency")
        for ax in axes:
            ax.grid(axis="y", alpha=0.25)
        plt.tight_layout()
        plt.show()
        """
    )
)

cells.append(
    md(
        """
        *Caption.* The contrast car is not a second protagonist. It is only there to make later heterogeneity comparisons legible.
        """
    )
)

cells.append(
    md(
        """
        ## 3. Space

        ### 3.1 Stage 7 - Station attractiveness kernel

        **Motivation.** Layer 2 needs a station score, but raw capacity is too aggressive. A 350 kW site should be more attractive than an 11 kW post, just not 31.8 times more attractive in a model that already includes distance decay.

        **Decision.** Stage 7 freezes `station_attractiveness = log(1 + TotalCapacity_kW)`. The logarithm keeps the ordering while compressing the tail, so capacity matters without letting a few mega-sites swallow the whole choice set.

        **Protagonist demo.** Look near the protagonist's home LSOA, then rank the local candidates by the frozen attractiveness score.
        """
    )
)

cells.append(
    code(
        """
        centroids_indexed = centroids_df.set_index("lsoa_code")
        nearby_station_df = stations_df.loc[:, ["StationID", "Title", "label", "lsoa_code", "TotalCapacity_kW", "station_attractiveness"]].copy()
        nearby_station_df["lsoa_code"] = nearby_station_df["lsoa_code"].astype(str)
        centroid_lsoas = set(centroids_indexed.index.astype(str))
        dropped_station_count = int((~nearby_station_df["lsoa_code"].isin(centroid_lsoas)).sum())
        nearby_station_df = nearby_station_df.loc[nearby_station_df["lsoa_code"].isin(centroid_lsoas)].copy()
        nearby_station_df["distance_to_home_km"] = nearby_station_df["lsoa_code"].map(
            lambda lsoa_code: od_distance_km(str(protagonist_fleet_row["home_lsoa"]), str(lsoa_code), centroids_indexed, intra_km=0.5)
        )
        nearby_station_top10 = (
            nearby_station_df
            .sort_values(["distance_to_home_km", "station_attractiveness"], ascending=[True, False])
            .head(20)
            .sort_values("station_attractiveness", ascending=False)
            .head(10)
            .reset_index(drop=True)
        )
        display(Markdown(
            f"_Stage 7 note._ The candidate list drops {dropped_station_count} stations whose labelled LSOA is absent from the centroid table. "
            "That is a data-coverage boundary, not a modelling change."
        ))
        display(nearby_station_top10[["StationID", "label", "lsoa_code", "TotalCapacity_kW", "station_attractiveness", "distance_to_home_km"]])

        fig, ax = plt.subplots(figsize=(12, 4.2))
        bar_labels = nearby_station_top10.apply(lambda row: f"{int(row['StationID'])}\\n{row['label']}", axis=1)
        ax.bar(bar_labels, nearby_station_top10["station_attractiveness"], color="tab:blue")
        ax.set_ylabel("station_attractiveness = log(1 + capacity_kW)")
        ax.set_title("Nearest candidate stations ranked by Stage 7 attractiveness")
        ax.grid(axis="y", alpha=0.25)
        plt.tight_layout()
        plt.show()
        """
    )
)

cells.append(
    md(
        """
        *Caption.* The closest candidates are not all equal. A 378 kW shopping site scores higher than a 14 kW shopping site, but the log transform keeps the gap interpretable instead of letting capacity dominate everything else.
        """
    )
)

cells.append(
    md(
        """
        ### 3.2 Stage 1 - Two-level Huff

        **Motivation.** NTS gives trip distance and purpose, not destination LSOA. The simulator therefore has to infer where the trip ends before it can reason about where the car might charge.

        **Decision.** Stage 1 splits the problem in two. Layer 1 samples a destination LSOA from frozen purpose-specific attractiveness, while Layer 2 samples a charging station inside that destination from `station_attractiveness` and distance decay. Keeping the layers separate avoids mixing POI-area magnitudes with station-capacity magnitudes. Home never enters Layer 1: it goes straight back to `home_lsoa`.

        **Notebook note.** The official `mobility.cars.destination.DestinationSampler` eagerly loads the full destination parquet at construction. In this repository that frozen table has 153,698,722 rows and is about 1.1 GB on disk, so a narrative notebook that only follows one EV uses a read-only lazy wrapper instead. It exposes the same two methods the notebook needs, `sample_destination_lsoa(...)` and `distance_km(...)`, but only reads the `(origin_lsoa, purpose)` slice required for the current story. The runtime modules are unchanged.

        **Protagonist demo.** First read the protagonist's frozen shopping destinations from Layer 1. Then send one synthetic shopping parking event into Layer 2 and inspect the chosen station's distance and Huff weight.
        """
    )
)

cells.append(
    code(
        """
        class LazyDestinationSampler:
            def __init__(self, table_path: Path, centroids: pd.DataFrame | None = None):
                self._table_path = Path(table_path)
                self._dataset = ds.dataset(self._table_path, format="parquet")
                centroid_frame = load_lsoa_centroids() if centroids is None else centroids.copy()
                if "lsoa_code" in centroid_frame.columns:
                    centroid_frame = centroid_frame.set_index("lsoa_code", drop=True)
                self._centroids = centroid_frame.loc[:, ["easting_m", "northing_m"]]
                self._index = {}
                self._warned_missing_keys = set()

            def _load_key(self, origin_lsoa: str, purpose: str):
                key = (str(origin_lsoa), str(purpose))
                if key in self._index:
                    return self._index[key]
                table = self._dataset.to_table(
                    columns=["origin_lsoa", "purpose", "dest_lsoa", "prob"],
                    filter=(ds.field("origin_lsoa") == key[0]) & (ds.field("purpose") == key[1]),
                )
                if table.num_rows == 0:
                    self._index[key] = None
                    return None
                group = table.to_pandas()
                dest_lsoas = group["dest_lsoa"].astype(str).to_numpy(dtype=object)
                probs = group["prob"].to_numpy(dtype=np.float64)
                prob_sum = probs.sum()
                if prob_sum <= 0.0:
                    self._index[key] = None
                    return None
                probs = probs / prob_sum
                self._index[key] = (dest_lsoas, probs)
                return self._index[key]

            def sample_destination_lsoa(self, origin_lsoa: str, purpose: str, rng: np.random.Generator, home_lsoa: str) -> str:
                if purpose == "home":
                    return str(home_lsoa)
                key = (str(origin_lsoa), str(purpose))
                hit = self._load_key(*key)
                if hit is None:
                    if key not in self._warned_missing_keys:
                        warnings.warn(
                            f"Missing Layer-1 destination probabilities for origin={origin_lsoa!r}, purpose={purpose!r}; falling back to home_lsoa={home_lsoa!r}",
                            RuntimeWarning,
                            stacklevel=2,
                        )
                        self._warned_missing_keys.add(key)
                    return str(home_lsoa)
                dest_lsoas, probs = hit
                return str(rng.choice(dest_lsoas, p=probs))

            def distance_km(self, a: str, b: str) -> float:
                return float(od_distance_km(a, b, self._centroids, intra_km=0.5))


        destination_dataset = ds.dataset(DESTINATION_TABLE_PATH, format="parquet")
        destination_sampler = LazyDestinationSampler(DESTINATION_TABLE_PATH, centroids=centroids_df)
        """
    )
)

cells.append(
    code(
        """
        shopping_top20 = (
            destination_dataset.to_table(
                columns=["origin_lsoa", "purpose", "dest_lsoa", "prob"],
                filter=(ds.field("origin_lsoa") == str(protagonist_fleet_row["home_lsoa"])) & (ds.field("purpose") == "shopping"),
            )
            .to_pandas()
            .sort_values("prob", ascending=False)
            .reset_index(drop=True)
        )
        shopping_top5 = shopping_top20.head(5).copy()
        display(shopping_top5)

        fig, ax = plt.subplots(figsize=(10, 4.0))
        ax.bar(shopping_top5["dest_lsoa"], shopping_top5["prob"], color="tab:green")
        ax.set_ylabel("Layer 1 probability")
        ax.set_title("Stage 1 Layer 1: shopping destination LSOAs from the frozen choice table")
        ax.tick_params(axis="x", rotation=45)
        ax.grid(axis="y", alpha=0.25)
        plt.tight_layout()
        plt.show()
        """
    )
)

cells.append(
    code(
        """
        station_indices = station_matcher_module._build_lsoa_indices(stations_df)
        layer2_dest_lsoa = None
        for dest_lsoa in shopping_top20["dest_lsoa"].astype(str):
            pool = station_indices["by_lsoa_label"].get((dest_lsoa, "shopping"))
            if pool is not None and len(pool) > 1:
                layer2_dest_lsoa = dest_lsoa
                break

        layer2_schedule = DailySchedule(
            ev_id=str(protagonist_row["ev_id"]),
            day=0,
            day_type="weekday",
            parking_events=[
                ParkingEvent(
                    start_time=10.0,
                    end_time=12.0,
                    duration_hours=2.0,
                    location_purpose="shopping",
                    location_lsoa=layer2_dest_lsoa,
                )
            ],
        )
        match_stations_for_schedule(
            schedule=layer2_schedule,
            ev_home_lsoa=str(protagonist_fleet_row["home_lsoa"]),
            ev_ac_power_kw=float(protagonist_fleet_row["ac_power_kw"]),
            stations_df=stations_df,
            rng=np.random.default_rng(MAIN_CAR_SEED),
            centroids=centroids_indexed,
            _indices=station_indices,
            date_iso="2025-05-06",
        )
        selected_event = layer2_schedule.parking_events[0]
        pool_rows = station_indices["by_lsoa_label"][(layer2_dest_lsoa, "shopping")]
        pool_distances_m = np.fromiter(
            (station_matcher_module._distance_m(layer2_dest_lsoa, station_indices["lsoa"][row_idx], centroids_indexed) for row_idx in pool_rows),
            dtype=np.float64,
            count=len(pool_rows),
        )
        pool_weights = station_matcher_module._huff_weights(station_indices["attr"][pool_rows], pool_distances_m)
        pool_probs = pool_weights / pool_weights.sum()
        layer2_pool_df = pd.DataFrame(
            {
                "StationID": station_indices["sid"][pool_rows].astype(int),
                "capacity_kw": np.round(station_indices["cap"][pool_rows], 2),
                "station_attractiveness": np.round(station_indices["attr"][pool_rows], 3),
                "distance_m": np.round(pool_distances_m, 1),
                "huff_weight": np.round(pool_weights, 9),
                "probability": np.round(pool_probs, 6),
            }
        )
        layer2_pool_df["chosen"] = layer2_pool_df["StationID"] == int(selected_event.matched_station_id)
        print(f"Sampled destination LSOA for the Layer 2 demo: {layer2_dest_lsoa}")
        display(layer2_pool_df.sort_values("probability", ascending=False).reset_index(drop=True))
        print("Selected station:", int(selected_event.matched_station_id), "| charge_power_kw =", float(selected_event.charge_power_kw))
        """
    )
)

cells.append(
    md(
        """
        *Caption.* The Layer 1 draw picks the destination LSOA; the Layer 2 draw then works inside that LSOA. In this case the protagonist sees two shopping stations, and the higher-capacity one wins because it combines the same 500 m intra-LSOA distance with a larger log-capacity score.
        """
    )
)

cells.append(
    md(
        """
        ## 4. Time

        ### 4.1 Stage 2a - Hard-coded holiday rules

        **Motivation.** Holiday logic needs to be deterministic and dependency-light. Pulling runtime calendars from external packages would weaken reproducibility for a stage that is supposed to be frozen.

        **Decision.** The project hard-codes UK bank holidays and representative school-holiday windows for 2025 and 2026 in `holiday_rules.py`. That keeps the runtime pure: the same input date always produces the same answer.

        **Protagonist demo.** Check the Christmas 2025 school break and the week that contains New Year 2026.
        """
    )
)

cells.append(
    code(
        """
        holiday_demo_df = pd.DataFrame(
            {
                "week_start": [dt.date(2025, 12, 22), dt.date(2025, 12, 29), dt.date(2026, 1, 5)],
                "holiday_region": ["england", "england", "england"],
            }
        )
        holiday_demo_df["is_holiday_week"] = holiday_demo_df.apply(
            lambda row: is_holiday_week(row["week_start"], row["holiday_region"]),
            axis=1,
        )
        display(holiday_demo_df)

        fig, ax = plt.subplots(figsize=(9, 3.2))
        ax.bar(
            holiday_demo_df["week_start"].astype(str),
            holiday_demo_df["is_holiday_week"].astype(int),
            color=["tab:red" if flag else "tab:gray" for flag in holiday_demo_df["is_holiday_week"]],
        )
        ax.set_ylabel("holiday flag")
        ax.set_ylim(0, 1.2)
        ax.set_title("Stage 2a around Christmas 2025 and New Year 2026")
        ax.grid(axis="y", alpha=0.25)
        plt.tight_layout()
        plt.show()
        """
    )
)

cells.append(
    md(
        """
        *Caption.* `is_holiday_week(dt.date(2025, 12, 22), "england")` is `True`, and the Monday December 29, 2025 week is also flagged because it contains New Year's Day on January 1, 2026.
        """
    )
)

cells.append(
    md(
        """
        ### 4.2 Stage 2b - Person <-> EV binding

        **Motivation.** If the model re-sampled a person every day, it would destroy within-week correlation. Commute structure, weekend style, and leisure habits would all decohere into a bag of independent days.

        **Decision.** Stage 2b freezes one `person_id` per `ev_id` for the full simulation horizon. The person-side fields live in `person_fleet.parquet`; the vehicle-side fields come from the EV fleet table.

        **Protagonist demo.** The identity card already shows the fixed pair. The plot below makes the same point in timeline form: once the protagonist is bound, that binding does not change within the year.
        """
    )
)

cells.append(
    code(
        """
        binding_timeline_df = pd.DataFrame(
            {
                "date": pd.date_range("2025-01-01", "2025-12-01", freq="MS"),
                "distinct_person_ids_for_ev": 1,
            }
        )
        fig, ax = plt.subplots(figsize=(11, 2.8))
        ax.step(binding_timeline_df["date"], binding_timeline_df["distinct_person_ids_for_ev"], where="mid", color="tab:purple", lw=2)
        ax.scatter(binding_timeline_df["date"], binding_timeline_df["distinct_person_ids_for_ev"], color="tab:purple", s=30)
        ax.set_ylim(0.8, 1.2)
        ax.set_yticks([1])
        ax.set_ylabel("distinct person_id")
        ax.set_title("Stage 2b: the protagonist EV keeps the same bound person through 2025")
        ax.grid(axis="x", alpha=0.15)
        plt.tight_layout()
        plt.show()
        """
    )
)

cells.append(
    md(
        """
        *Caption.* The chart is intentionally boring. That is the point of Stage 2b: binding should be stable enough that there is no daily rebinding story to tell.
        """
    )
)

cells.append(
    md(
        """
        ### 4.3 Stage 2c - Week pattern library

        **Motivation.** The NTS sample is finite. Runtime cannot invent new weekly behaviour if the goal is reproducible diary-based schedules rather than a generative behaviour model.

        **Decision.** Stage 2c freezes a per-person week library as seven `chain_json` rows per pattern. The runtime only samples from this frozen set, then jitters distances slightly. In the current artefact every person has one stored pattern, so holiday variation comes from Stage 2a's transform rather than from multiple base patterns.

        **Protagonist demo.** Read the protagonist's frozen week, then compare a non-holiday sample week against a Christmas-holiday sample week for the same person.
        """
    )
)

cells.append(
    code(
        """
        def summarise_week_for_display(week_chain):
            day_names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
            summary_rows = []
            for day_name, day_chain in zip(day_names, week_chain, strict=True):
                purposes = " | ".join(f"{leg[3]}->{leg[4]}" for leg in day_chain) if day_chain else "No trips"
                distance_km = float(sum(leg[2] for leg in day_chain))
                summary_rows.append({"day": day_name, "purposes": purposes, "distance_km": round(distance_km, 2)})
            return pd.DataFrame(summary_rows)


        protagonist_library_display = protagonist_library_df.sort_values(["pattern_id", "day_of_week"]).reset_index(drop=True)
        display(protagonist_library_display)

        library_index = week_pattern_module.build_library_index(protagonist_library_df)
        leisure_pool_index = week_pattern_module.build_leisure_pool_index(protagonist_library_df)
        worklike_week = week_pattern_module.sample_person_week(
            str(protagonist_row["person_id"]),
            dt.date(2025, 5, 12),
            library_index,
            leisure_pool_index,
            np.random.default_rng(MAIN_CAR_SEED),
            is_holiday_week=False,
        )
        holiday_week = week_pattern_module.sample_person_week(
            str(protagonist_row["person_id"]),
            dt.date(2025, 12, 22),
            library_index,
            leisure_pool_index,
            np.random.default_rng(MAIN_CAR_SEED),
            is_holiday_week=True,
        )
        worklike_week_df = summarise_week_for_display(worklike_week).rename(columns={"purposes": "work_week_purposes", "distance_km": "work_week_distance_km"})
        holiday_week_df = summarise_week_for_display(holiday_week).rename(columns={"purposes": "holiday_week_purposes", "distance_km": "holiday_week_distance_km"})
        week_comparison_df = worklike_week_df.merge(holiday_week_df, on="day")
        display(week_comparison_df)

        fig, ax = plt.subplots(figsize=(11, 4.0))
        x = np.arange(len(week_comparison_df))
        width = 0.38
        ax.bar(x - width / 2, week_comparison_df["work_week_distance_km"], width=width, color="tab:blue", label="Non-holiday sampled week")
        ax.bar(x + width / 2, week_comparison_df["holiday_week_distance_km"], width=width, color="tab:orange", label="Holiday sampled week")
        ax.set_xticks(x)
        ax.set_xticklabels(week_comparison_df["day"])
        ax.set_ylabel("Total distance per day (km)")
        ax.set_title("Stage 2c in practice: the frozen week plus the holiday transform")
        ax.legend(frameon=False)
        ax.grid(axis="y", alpha=0.25)
        plt.tight_layout()
        plt.show()
        """
    )
)

cells.append(
    md(
        """
        *Caption.* The library itself is frozen, but the holiday transform can still bend the sampled week away from ordinary-term behaviour by dropping work-like legs, shifting times, and injecting leisure signal.
        """
    )
)

cells.append(
    md(
        """
        ### 4.4 Stage 2d - Year schedule assembly + Stage 8 jitter clamp

        **Motivation.** The optimiser wants dated schedules, not anonymous week fragments. At the same time, repeating the exact same observed departure times all year would create visible artefacts at the step boundaries.

        **Decision.** `assign_year_schedules` stitches the frozen person-week patterns into dated `DailySchedule` objects. Because January 1, 2025 does not align with the first Monday used by the scheduler, the notebook requests 53 weeks and then slices the schedules whose `date.year == 2025`. Stage 8 adds bounded time jitter and clamps the result to `[0.0, 23.75]` so the diary remains inside a single day.

        **Protagonist demo.** Build the protagonist's 2025 calendar schedules, then compare Monday trip start times before and after jitter using the same RNG seed.
        """
    )
)

cells.append(
    code(
        """
        calendar_2025_all = assign_year_schedules(
            protagonist_person_df,
            protagonist_fleet_df,
            protagonist_library_df,
            year=2025,
            n_weeks=53,
            rng=np.random.default_rng(MAIN_CAR_SEED),
            sampler=destination_sampler,
            region=protagonist_holiday_region,
        )[str(protagonist_row["ev_id"])]
        calendar_2025_schedules = [schedule for schedule in calendar_2025_all if schedule.date is not None and schedule.date.year == 2025]
        print(
            f"Calendar-year schedules: {len(calendar_2025_schedules)} days "
            f"from {calendar_2025_schedules[0].date} to {calendar_2025_schedules[-1].date}."
        )

        monday_chain_json = protagonist_library_df.loc[protagonist_library_df["day_of_week"] == 0, "chain_json"].iloc[0]
        monday_chain = [tuple(leg) for leg in json.loads(monday_chain_json)]
        monday_base_schedule = trip_chain_module.chain_to_daily_schedule(
            monday_chain,
            str(protagonist_row["ev_id"]),
            0,
            "weekday",
            float(protagonist_fleet_row["consumption_kwh_per_km"]),
            jitter_minutes=0.0,
            rng=np.random.default_rng(MAIN_CAR_SEED),
        )
        monday_jittered_schedule = trip_chain_module.chain_to_daily_schedule(
            monday_chain,
            str(protagonist_row["ev_id"]),
            0,
            "weekday",
            float(protagonist_fleet_row["consumption_kwh_per_km"]),
            jitter_minutes=10.0,
            rng=np.random.default_rng(MAIN_CAR_SEED),
        )
        jitter_compare_df = pd.DataFrame(
            {
                "leg": np.arange(1, len(monday_chain) + 1),
                "before_jitter_h": [trip.departure_time for trip in monday_base_schedule.trips],
                "after_jitter_h": [trip.departure_time for trip in monday_jittered_schedule.trips],
            }
        )
        jitter_compare_df["within_bounds"] = jitter_compare_df["after_jitter_h"].between(0.0, 23.75)
        display(jitter_compare_df)

        fig, ax = plt.subplots(figsize=(10, 4.0))
        ax.scatter(jitter_compare_df["before_jitter_h"], jitter_compare_df["leg"], color="tab:gray", s=50, label="Before jitter")
        ax.scatter(jitter_compare_df["after_jitter_h"], jitter_compare_df["leg"], color="tab:red", s=50, label="After jitter")
        for _, row in jitter_compare_df.iterrows():
            ax.plot([row["before_jitter_h"], row["after_jitter_h"]], [row["leg"], row["leg"]], color="tab:red", alpha=0.35)
        ax.set_xlabel("Departure time (hour of day)")
        ax.set_ylabel("Trip leg")
        ax.set_title("Stage 8: Monday trip starts move, but never leave the day")
        ax.legend(frameon=False)
        ax.grid(alpha=0.25)
        plt.tight_layout()
        plt.show()
        """
    )
)

cells.append(
    md(
        """
        *Caption.* The perturbation is deliberately small. It stops visible spike-alignment at quarter-hour boundaries without letting trips spill past the 23.75-hour ceiling.
        """
    )
)

cells.append(
    md(
        """
        ## 5. Charging mechanics

        ### 5.1 Stage 6 - Home-charging short-circuit

        **Motivation.** Home charging should not depend on whether a public-station dataset happens to contain a nearby connector. Parking at home is a different mechanism from hunting for public infrastructure.

        **Decision.** Stage 6 bypasses Layer 2 for `location_purpose == "home"`. The parking event becomes chargeable immediately and uses `HOME_CHARGER_KW = 7.0`, regardless of the car's AC intake ceiling or the density of public stations around the home LSOA.

        **Protagonist demo.** Match the protagonist's 2025 schedules to charging opportunities, then inspect one day that contains both home and non-home parking events.
        """
    )
)

cells.append(
    code(
        """
        def match_schedule_window(schedules):
            matched = deepcopy(schedules)
            indices = station_matcher_module._build_lsoa_indices(stations_df)
            for schedule in matched:
                match_stations_for_schedule(
                    schedule=schedule,
                    ev_home_lsoa=str(protagonist_fleet_row["home_lsoa"]),
                    ev_ac_power_kw=float(protagonist_fleet_row["ac_power_kw"]),
                    stations_df=stations_df,
                    rng=np.random.default_rng(MAIN_CAR_SEED),
                    centroids=centroids_indexed,
                    _indices=indices,
                    date_iso=schedule.date.isoformat() if schedule.date is not None else f"day{schedule.day:03d}",
                )
            return matched


        matched_calendar_2025_schedules = match_schedule_window(calendar_2025_schedules)
        selected_charge_day = next(
            schedule
            for schedule in matched_calendar_2025_schedules
            if any(pe.location_purpose == "home" and pe.can_charge for pe in schedule.parking_events)
            and any(pe.location_purpose != "home" and pe.can_charge for pe in schedule.parking_events)
        )
        selected_charge_day_df = pd.DataFrame(
            [
                {
                    "purpose": pe.location_purpose,
                    "start_h": round(pe.start_time, 2),
                    "end_h": round(pe.end_time, 2),
                    "can_charge": pe.can_charge,
                    "power_kw": round(pe.charge_power_kw, 2),
                }
                for pe in selected_charge_day.parking_events
            ]
        )
        display(selected_charge_day_df)

        fig, ax = plt.subplots(figsize=(11, 3.8))
        color_map = selected_charge_day_df["purpose"].map(lambda purpose: "tab:blue" if purpose == "home" else "tab:orange")
        widths = selected_charge_day_df["end_h"] - selected_charge_day_df["start_h"]
        ax.barh(
            y=np.arange(len(selected_charge_day_df)),
            width=widths,
            left=selected_charge_day_df["start_h"],
            color=color_map,
            alpha=0.8,
        )
        for row_idx, row in selected_charge_day_df.iterrows():
            ax.text(row["start_h"] + 0.05, row_idx, f"{row['power_kw']} kW", va="center", ha="left", fontsize=9)
        ax.set_yticks(np.arange(len(selected_charge_day_df)))
        ax.set_yticklabels(selected_charge_day_df["purpose"])
        ax.set_xlabel("Hour of day")
        ax.set_title(f"Stage 6 on {selected_charge_day.date}: home charging bypasses station matching")
        ax.set_xlim(0, 24)
        ax.grid(axis="x", alpha=0.25)
        plt.tight_layout()
        plt.show()
        """
    )
)

cells.append(
    md(
        """
        *Caption.* The home events are fixed at 7 kW even though this Renault Zoe can accept much more AC power. That is deliberate: Stage 6 models a household wallbox, not the vehicle's maximum onboard charger.
        """
    )
)

cells.append(
    md(
        """
        ### 5.2 Stage 4 - AC charging curve heterogeneity

        **Motivation.** The last few percent of charging are not chemistry-neutral. NMC starts tapering earlier than LFP, so a model that uses one universal CV threshold misstates usable energy near high SOC.

        **Decision.** Stage 4 freezes `CV_THRESHOLD = {"NMC": 0.80, "LFP": 0.88}`. The charging curve stays flat below the chemistry-specific threshold, then tapers linearly toward full SOC.

        **Protagonist demo.** Use the same evening charging window for the protagonist and a contrast LFP branch, starting from the same SOC.
        """
    )
)

cells.append(
    code(
        """
        stage4_charge_power_kw = float(min(HOME_CHARGER_KW, protagonist_fleet_row["ac_power_kw"], contrast_fleet_row["ac_power_kw"]))
        stage4_schedule = [
            DailySchedule(
                ev_id="stage4_window",
                day=0,
                day_type="weekday",
                parking_events=[
                    ParkingEvent(
                        start_time=0.0,
                        end_time=18.0,
                        duration_hours=18.0,
                        location_purpose="home",
                        can_charge=False,
                        charge_power_kw=0.0,
                    ),
                    ParkingEvent(
                        start_time=18.0,
                        end_time=24.0,
                        duration_hours=6.0,
                        location_purpose="home",
                        can_charge=True,
                        charge_power_kw=stage4_charge_power_kw,
                    ),
                ],
            )
        ]
        soc_nmc, load_nmc, _ = simulate_single_ev(
            deepcopy(stage4_schedule),
            battery_capacity_kwh=float(protagonist_fleet_row["battery_capacity_kwh"]),
            soc_init=0.30,
            warm_up_days=0,
            chemistry="NMC",
        )
        soc_lfp, load_lfp, _ = simulate_single_ev(
            deepcopy(stage4_schedule),
            battery_capacity_kwh=float(contrast_fleet_row["battery_capacity_kwh"]),
            soc_init=0.30,
            warm_up_days=0,
            chemistry="LFP",
        )
        stage4_time_h = np.arange(len(soc_nmc)) * STEP_HOURS
        fig, ax = plt.subplots(figsize=(11, 4.0))
        ax.plot(stage4_time_h, soc_nmc, color="tab:blue", lw=2, label=f"Protagonist as NMC (CV {CV_THRESHOLD['NMC']:.2f})")
        ax.plot(stage4_time_h, soc_lfp, color="tab:orange", lw=2, label=f"Contrast branch as LFP (CV {CV_THRESHOLD['LFP']:.2f})")
        ax.axhline(CV_THRESHOLD["NMC"], color="tab:blue", ls=":", alpha=0.6)
        ax.axhline(CV_THRESHOLD["LFP"], color="tab:orange", ls=":", alpha=0.6)
        ax.set_xlabel("Hour of day")
        ax.set_ylabel("SOC")
        ax.set_title("Stage 4: later taper lets the LFP branch keep charging harder for longer")
        ax.set_xlim(0, 24)
        ax.legend(frameon=False)
        ax.grid(alpha=0.25)
        plt.tight_layout()
        plt.show()
        """
    )
)

cells.append(
    md(
        """
        *Caption.* The source EV CSV does not carry battery chemistry, so the notebook uses the runtime default `NMC` for the protagonist and an explicit `LFP` contrast branch to show exactly what Stage 4 changed in the simulator.
        """
    )
)

cells.append(
    md(
        """
        ## 6. Long-horizon dynamics

        ### 6.1 Stage 3 - SOC warm-up

        **Motivation.** A simulation has to start somewhere, but the initial SOC is arbitrary unless the model has already been running for long enough to wash that assumption out.

        **Decision.** Stage 3 adds `warm_up_days = 14`. The model still simulates those days, but it discards them from the returned arrays and keeps only the SOC that emerges after the burn-in.

        **Protagonist demo.** Compare the first 30 matched days of 2025 with and without the 14-day warm-up strip.
        """
    )
)

cells.append(
    code(
        """
        warmup_demo_schedules = matched_calendar_2025_schedules[:30]
        soc_no_warmup, load_no_warmup, _ = simulate_single_ev(
            deepcopy(warmup_demo_schedules),
            battery_capacity_kwh=float(protagonist_fleet_row["battery_capacity_kwh"]),
            warm_up_days=0,
            chemistry=protagonist_chemistry,
        )
        soc_with_warmup, load_with_warmup, soc_after_warmup_demo = simulate_single_ev(
            deepcopy(warmup_demo_schedules),
            battery_capacity_kwh=float(protagonist_fleet_row["battery_capacity_kwh"]),
            warm_up_days=WARMUP_DAYS,
            chemistry=protagonist_chemistry,
        )
        warmup_dates_full = pd.date_range(warmup_demo_schedules[0].date, periods=len(soc_no_warmup), freq="15min")
        warmup_dates_post = pd.date_range(warmup_demo_schedules[WARMUP_DAYS].date, periods=len(soc_with_warmup), freq="15min")
        cutoff_date = warmup_demo_schedules[WARMUP_DAYS].date

        fig, ax = plt.subplots(figsize=(12, 4.2))
        ax.plot(warmup_dates_full, soc_no_warmup, color="tab:gray", lw=1.3, label="warm_up_days = 0")
        ax.plot(warmup_dates_post, soc_with_warmup, color="tab:green", lw=1.8, label=f"warm_up_days = {WARMUP_DAYS}")
        ax.axvline(pd.Timestamp(cutoff_date), color="tab:red", ls="--", label=f"cut-off: {cutoff_date}")
        ax.set_ylabel("SOC")
        ax.set_title("Stage 3: the retained SOC trace begins only after the burn-in window")
        ax.legend(frameon=False)
        ax.grid(alpha=0.25)
        plt.tight_layout()
        plt.show()
        """
    )
)

cells.append(
    md(
        """
        *Caption.* The warm-up run does not change the charging physics. It changes which part of the trajectory we trust as a meaningful starting state.
        """
    )
)

cells.append(
    md(
        """
        ### 6.2 Stage 5 - Seasonal consumption correction

        **Motivation.** Winter energy demand is not something the route model can infer from distances alone. Heating and cold-weather losses are exogenous facts that need to be imposed explicitly.

        **Decision.** Stage 5 applies a season-specific scalar to each trip's `energy_consumed_kwh`. Winter is `1.35`, summer is `1.10`, and spring and autumn remain `1.00`.

        **Protagonist demo.** Compare the same weekday template in January and May, then sum the resulting trip energy month by month across the full calendar year.
        """
    )
)

cells.append(
    code(
        """
        def first_non_holiday_weekday(schedules, month: int, weekday: int):
            for schedule in schedules:
                if schedule.date is None:
                    continue
                if schedule.date.month != month or schedule.date.weekday() != weekday:
                    continue
                if is_holiday_week(schedule.date, protagonist_holiday_region):
                    continue
                if schedule.trips:
                    return schedule
            raise ValueError(f"No non-holiday weekday found for month={month}, weekday={weekday}.")


        january_weekday = first_non_holiday_weekday(calendar_2025_schedules, month=1, weekday=0)
        may_weekday = first_non_holiday_weekday(calendar_2025_schedules, month=5, weekday=0)
        seasonal_compare_df = pd.DataFrame(
            [
                {
                    "date": january_weekday.date,
                    "seasonal_factor": get_seasonal_factor(january_weekday.date.month),
                    "distance_km": round(sum(trip.distance_km for trip in january_weekday.trips), 2),
                    "energy_consumed_kwh": round(sum(trip.energy_consumed_kwh for trip in january_weekday.trips), 2),
                },
                {
                    "date": may_weekday.date,
                    "seasonal_factor": get_seasonal_factor(may_weekday.date.month),
                    "distance_km": round(sum(trip.distance_km for trip in may_weekday.trips), 2),
                    "energy_consumed_kwh": round(sum(trip.energy_consumed_kwh for trip in may_weekday.trips), 2),
                },
            ]
        )
        display(seasonal_compare_df)

        monthly_consumption_df = (
            pd.DataFrame(
                {
                    "month": [schedule.date.strftime("%b") for schedule in calendar_2025_schedules],
                    "month_num": [schedule.date.month for schedule in calendar_2025_schedules],
                    "energy_consumed_kwh": [sum(trip.energy_consumed_kwh for trip in schedule.trips) for schedule in calendar_2025_schedules],
                }
            )
            .groupby(["month_num", "month"], as_index=False)["energy_consumed_kwh"]
            .sum()
            .sort_values("month_num")
        )

        fig, ax = plt.subplots(figsize=(11, 4.0))
        ax.bar(monthly_consumption_df["month"], monthly_consumption_df["energy_consumed_kwh"], color="tab:cyan")
        ax.set_ylabel("Trip energy consumed (kWh)")
        ax.set_title("Stage 5: seasonal correction lifts winter energy above spring and autumn")
        ax.grid(axis="y", alpha=0.25)
        plt.tight_layout()
        plt.show()
        """
    )
)

cells.append(
    md(
        """
        *Caption.* The winter bars are high not because the protagonist suddenly drives farther, but because the same movement pattern gets multiplied by a larger exogenous energy factor.
        """
    )
)

cells.append(
    md(
        """
        ## 7. Integration: full-year simulation

        The final step is to put the pieces together without changing their meaning: a calendar-year schedule window, a 14-day burn-in prefix, station matching, and then one full `simulate_single_ev(..., warm_up_days=WARMUP_DAYS)` call whose retained trace runs from January 1, 2025 to December 31, 2025.
        """
    )
)

cells.append(
    code(
        """
        warmup_2024_all = assign_year_schedules(
            protagonist_person_df,
            protagonist_fleet_df,
            protagonist_library_df,
            year=2024,
            n_weeks=53,
            rng=np.random.default_rng(ALT_CAR_SEED),
            sampler=destination_sampler,
            region=protagonist_holiday_region,
        )[str(protagonist_row["ev_id"])]
        warmup_prefix_2024 = [
            schedule
            for schedule in warmup_2024_all
            if schedule.date is not None and dt.date(2024, 12, 18) <= schedule.date <= dt.date(2024, 12, 31)
        ]

        steady_state_input_schedules = warmup_prefix_2024 + calendar_2025_schedules
        matched_steady_state_input_schedules = match_schedule_window(steady_state_input_schedules)
        soc_year_2025, load_year_2025, soc_after_warmup = simulate_single_ev(
            matched_steady_state_input_schedules,
            battery_capacity_kwh=float(protagonist_fleet_row["battery_capacity_kwh"]),
            warm_up_days=WARMUP_DAYS,
            chemistry=protagonist_chemistry,
        )
        steady_state_calendar_2025_schedules = [
            schedule for schedule in matched_steady_state_input_schedules if schedule.date is not None and schedule.date.year == 2025
        ]
        dates_2025 = pd.date_range("2025-01-01", "2025-12-31", freq="D")
        soc_day_matrix = soc_year_2025.reshape(len(dates_2025), STEPS_PER_DAY)
        load_day_matrix = load_year_2025.reshape(len(dates_2025), STEPS_PER_DAY)
        daily_soc_df = pd.DataFrame(
            {
                "date": dates_2025,
                "soc_min": soc_day_matrix.min(axis=1),
                "soc_mean": soc_day_matrix.mean(axis=1),
                "soc_max": soc_day_matrix.max(axis=1),
            }
        )
            """
    )
)

cells.append(
    code(
        """
        fig, ax = plt.subplots(figsize=(12, 4.2))
        ax.fill_between(daily_soc_df["date"], daily_soc_df["soc_min"], daily_soc_df["soc_max"], color="tab:blue", alpha=0.15)
        ax.plot(daily_soc_df["date"], daily_soc_df["soc_min"], color="tab:blue", lw=1.1, label="daily min")
        ax.plot(daily_soc_df["date"], daily_soc_df["soc_mean"], color="tab:green", lw=1.5, label="daily mean")
        ax.plot(daily_soc_df["date"], daily_soc_df["soc_max"], color="tab:orange", lw=1.1, label="daily max")
        ax.set_ylabel("SOC")
        ax.set_title("2025 steady-state daily SOC envelope for the protagonist car")
        ax.legend(frameon=False, ncol=3)
        ax.grid(alpha=0.25)
        plt.tight_layout()
        plt.show()
        """
    )
)

cells.append(
    md(
        """
        *Caption.* Warm-up has already happened before January 1, 2025 in this retained trace, so the envelope can be read as a full-year state trajectory rather than as a transient from an arbitrary SOC guess.
        """
    )
)

cells.append(
    code(
        """
        load_day_matrix_30min = load_day_matrix.reshape(len(dates_2025), STEPS_PER_DAY_PROFILE, 2).mean(axis=2)
        weekday_codes = np.array([date.weekday() for date in dates_2025])
        weekday_labels = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
        heatmap_matrix = np.vstack([load_day_matrix_30min[weekday_codes == weekday_code].mean(axis=0) for weekday_code in range(7)])

        fig, ax = plt.subplots(figsize=(12, 4.4))
        im = ax.imshow(heatmap_matrix, aspect="auto", cmap="YlGnBu")
        ax.set_yticks(np.arange(7))
        ax.set_yticklabels(weekday_labels)
        ax.set_xticks(np.arange(0, STEPS_PER_DAY_PROFILE, 4))
        ax.set_xticklabels([f"{int(hour):02d}:00" for hour in np.arange(0, 24, 2)])
        ax.set_title("Average 7x48 half-hour load profile across the steady-state 2025 trace")
        cbar = plt.colorbar(im, ax=ax)
        cbar.set_label("Average power (kW)")
        plt.tight_layout()
        plt.show()
        """
    )
)

cells.append(
    md(
        """
        *Caption.* Aggregating to the 30-minute profile layer makes the weekly charging rhythm easier to read while keeping the semantics intact: each cell still represents average power over that half-hour slot.
        """
    )
)

cells.append(
    code(
        """
        home_charge_energy_kwh = sum(
            pe.energy_charged_kwh
            for schedule in steady_state_calendar_2025_schedules
            for pe in schedule.parking_events
            if pe.location_purpose == "home"
        )
        public_charge_energy_kwh = sum(
            pe.energy_charged_kwh
            for schedule in steady_state_calendar_2025_schedules
            for pe in schedule.parking_events
            if pe.location_purpose != "home"
        )
        fig, ax = plt.subplots(figsize=(6, 4.4))
        ax.pie(
            [home_charge_energy_kwh, public_charge_energy_kwh],
            labels=["Home charging", "Public charging"],
            autopct=lambda pct: f"{pct:.1f}%",
            colors=["tab:blue", "tab:orange"],
            startangle=90,
        )
        ax.set_title("Charged energy split across the protagonist's 2025 year")
        plt.tight_layout()
        plt.show()
        """
    )
)

cells.append(
    md(
        """
        *Caption.* The energy pie is the final modelling consequence of everything above it: trip generation, destination choice, station matching, home-charging bypass, taper physics, seasonality, and the warm-up state that determines how often the car actually needs to plug in.
        """
    )
)

cells.append(
    code(
        """
        total_trip_distance_km = sum(trip.distance_km for schedule in steady_state_calendar_2025_schedules for trip in schedule.trips)
        total_trip_energy_kwh = sum(trip.energy_consumed_kwh for schedule in steady_state_calendar_2025_schedules for trip in schedule.trips)
        home_charge_sessions = sum(
            1
            for schedule in steady_state_calendar_2025_schedules
            for pe in schedule.parking_events
            if pe.location_purpose == "home" and pe.energy_charged_kwh > 0
        )
        public_charge_sessions = sum(
            1
            for schedule in steady_state_calendar_2025_schedules
            for pe in schedule.parking_events
            if pe.location_purpose != "home" and pe.energy_charged_kwh > 0
        )

        FINAL_IDENTITY_CARD_DF = render_identity_card(
            [
                ("ev_id", protagonist_row["ev_id"]),
                ("person_id", protagonist_row["person_id"]),
                ("home_lsoa", protagonist_fleet_row["home_lsoa"]),
                ("nts_region", protagonist_row["nts_region"]),
                ("holiday_region", protagonist_holiday_region),
                ("model", protagonist_fleet_row["Model"]),
                ("battery_capacity_kwh", round(float(protagonist_fleet_row["battery_capacity_kwh"]), 2)),
                ("consumption_kwh_per_km", round(float(protagonist_fleet_row["consumption_kwh_per_km"]), 3)),
                ("chemistry", protagonist_chemistry),
                ("calendar_schedule_days", len(calendar_2025_schedules)),
                ("warm_up_prefix_days", len(warmup_prefix_2024)),
                ("warm_up_days", WARMUP_DAYS),
                ("retained_soc_days", len(daily_soc_df)),
                ("soc_after_warmup", round(float(soc_after_warmup), 6)),
                ("year_trip_distance_km", round(float(total_trip_distance_km), 2)),
                ("year_trip_energy_kwh", round(float(total_trip_energy_kwh), 2)),
                ("home_charge_energy_kwh", round(float(home_charge_energy_kwh), 2)),
                ("public_charge_energy_kwh", round(float(public_charge_energy_kwh), 2)),
                ("home_charge_sessions", home_charge_sessions),
                ("public_charge_sessions", public_charge_sessions),
                ("daily_soc_min_2025", round(float(daily_soc_df["soc_min"].min()), 4)),
                ("daily_soc_mean_2025", round(float(daily_soc_df["soc_mean"].mean()), 4)),
                ("daily_soc_max_2025", round(float(daily_soc_df["soc_max"].max()), 4)),
            ]
        )

        NOTEBOOK_WALL_CLOCK_S = time.time() - NOTEBOOK_START
        print(f"Notebook wall-clock time: {NOTEBOOK_WALL_CLOCK_S:.2f} s")
        """
    )
)


nb = nbf.v4.new_notebook()
nb["cells"] = cells
nb["metadata"]["kernelspec"] = {
    "display_name": "Python 3",
    "language": "python",
    "name": "python3",
}
nb["metadata"]["language_info"] = {"name": "python", "version": "3.11"}

OUT_PATH.write_text(nbf.writes(nb), encoding="utf-8")
print(f"Wrote {OUT_PATH}")
