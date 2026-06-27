# Results Inventory — EV Mobility & Charging Simulation Outputs

> **生成日期 / Generated:** 2026-06-27 · **盘点机器 / Host:** the Modelling server (`~/Work/…`) · **盘点方式:** 只读盘点 (read-only); 没有任何结果文件被修改、移动或删除。
>
> **阅读指引 / How to read this document.** 本文档是「结果产出清单」(Results Inventory),用于在**没有这些数据**的本地机器上撰写实验设计方案。它是**自包含**的:仅凭它即可看清每个结果的结构 (schema) 与来源 (provenance / lineage),无需访问服务器。下游分析目标:基于建模出的 **EV 充电负荷 (expected demand)**,推导各区域**预期充电设施需求**,与 **现存真实充电设施 (actual supply)** 对比,识别 *infrastructure-weak* 区域。因此本文档的重点是 **lineage:数据源 → 处理步骤 (脚本路径) → 结果**,以及每个结果的**空间/时间分辨率**与**已确认状态**。
>
> 文档分四节,对应任务要求:**(A)** 顶层概览表 · **(B)** 逐结果明细 (schema + provenance) · **(C)** 数据源 → 结果 lineage 映射表 · **(D)** 缺口与待确认。
>
> **纪律 (data integrity):** 列名一律**原样照抄** (verbatim, 含 en-dash 与空格);只报告能从文件或代码核实的内容;无法确认来源者明确标 **Unknown / partial**;明确区分 *source-data / derived-intermediate / final / expected-demand / actual-supply / spatial-linking / web-export / diagnostic / report*。

---

## 0. Orientation — 一眼看清「用哪个结果做什么」

The repo `Modelling/` (`/home/mazhichuan/Projects/Modelling`) is the **code**; all **outputs/data are local and git-ignored** (`.gitignore`: `data`, `output`, `outputs`). Outputs live under `~/Work/` in two homes:

- **`~/Work/Nature_EV_2025/outputs/`** (≈65 GB) — the **bus & coach** annual depot-charging-load results (the original workspace `Modelling/` was extracted from).
- **`~/Work/Modelling/outputs/`** (≈1.1 TB) — the **private-car** full-year sharded charging simulation + an extra **bus** depot-only sample + national bus-annual aggregates.

Three transport modes were modelled, each producing a 15-minute **expected charging-demand** signal. These are compared against one **actual-supply** dataset and joined via shared **spatial-linking** lookups.

### 0.1 "What to use" cheat-sheet

| Need | Use this | Path |
|---|---|---|
| **Private-car** expected demand (per station, 15-min, full year 2025) | `station_charging_curve_15min_2025` — **SUM across the 16 `shards_16_v2` shards** on `(station_id, time_bin_start)`; no pre-merged national file exists | `~/Work/Modelling/outputs/privatecar_full_2025_shards_16_v2/shard_*/` |
| Private-car station identity/location (lat/lon, lsoa_code, region) | `station_metadata_2025.json` | same shards (replica per shard) |
| **Bus** expected demand (per depot, 15-min, ~9-month) — canonical | `bus_annual_depot_load_main/depot_load_15min` (or `…_carryover` for time-of-day realism, filter `is_warmup=False`) | `~/Work/Nature_EV_2025/outputs/` |
| **Coach** expected demand (per depot, 15-min, Apr–Dec 2026) | `coach_annual_depot_load/depot_load_15min` | `~/Work/Nature_EV_2025/outputs/` |
| **Actual supply** — real UK charging devices (lat/lon, power, band, LSOA/LAD/region) | `UK_OCM_connectors_expanded_with_bus_and_LAD_LSOA.csv` | `~/Work/UK-EV-Charging-Stations/` |
| Spatial linking: postcode/point → LSOA21 / LAD / region | `ONSPD_MAY_2025_UK.csv` | `~/Work/Data/Units/` |
| EV stock / penetration (per LSOA, the demand driver) | `EV_UK_LSOA_2025_with_energy.csv` | `~/Work/Modelling/data/` (source input, not an output) |

### 0.2 Critical caveats (read before using any result)

1. **Private-car national demand has NO merged parquet.** `privatecar_full_2025_merged/` holds **only logs**. You must aggregate the 16 `shards_16_v2` shards yourself (each shard = ~1/16 of national load; shards partition *vehicles*, so summing on `(station_id, time_bin_start)` is correct). The only completed national artifact is a per-day **JSON web export at `~/Projects/Web/public/data/results_with_connectors/`** (outside `~/Work`).
2. **Three private-car runs exist; only `shards_16_v2` is canonical/complete.** `shards_16` (v1) is complete but **superseded** (older code, fewer rows); `shards_32` is **incomplete/abandoned** (no parquet).
3. **Private-car station curves are PUBLIC charging only.** Home charging is a separate event table (`private_car_home_charging_events.parquet`, no `station_id`) — do not conflate with public-infrastructure demand.
4. **Bus/coach depots are INFERRED operational anchors, not verified physical garages** (`is_physical_depot=False` everywhere). For **bus**, ~33–34% of charge lands at `_missing`-LSOA depots (null lat/lon) and is **spatially unmappable**. Coach has 100% mappable but only 35.6% of charge on high-confidence depots.
5. **The mobility scenarios are current-EV-stock-scale SCENARIOS, not full electrification and not externally calibrated.** Bus ≈5,328 EV buses; coach serves only ~2.26% of all coach service. They are internally consistent, not validated absolutes.
6. **`region_key` is absent on the bus/coach `depot_load_15min`/`depot_daily_summary` load tables** — they carry `depot_lsoa`; you must join LSOA→region externally (via ONSPD). `region_key` lives on `vehicle_day_assignments` and block-template tables.
7. **Window/calendar mismatches:** bus runs span 9-month (276-day) or 12-month (365-day) windows — not directly comparable without normalization; GTFS/TxC **calendar-decay tail dates** (bus: 49 flagged; coach: 2026-12-14..25) reflect feed expiry, **not** real seasonality.
8. **`~/Work/NatureEV` is NOT an EV charging simulation** — it is "SearchRealCost", a DCOPF electricity-**price** calibration tool (generator marginal costs / LMP). Unrelated to charging demand; included only for completeness.
9. **Mixed small-area geography in actual-supply:** `UK_OCM_…_LSOA.csv` `lsoa_code` = LSOA21 for England/Wales, **Data Zone 2022** for Scotland, **Data Zone 2021** for NI (keyed by its `region` = EW/SC/NI). Not uniformly LSOA21.

---

## A. 顶层概览表 / Top-level overview

Roles: **ED** = expected-demand · **AS** = actual-supply · **SL** = spatial-linking · **SRC** = source-data · **INT** = derived-intermediate · **FIN** = final · **DIAG** = diagnostic · **RPT** = report · **WEB** = web-export · **OTHER** = unrelated/empty. Provenance: ✅ confirmed · ◐ partial · ✖ unknown.

### A.1 Expected demand — Private car (`~/Work/Modelling/outputs/`)

| Result | Format · rows · size | Role | Source(s) | Prov |
|---|---|---|---|---|
| `privatecar_full_2025_shards_16_v2/` **(canonical)** | 16 shard dirs · 685 G | ED | EV fleet + NTS trips + OCM stations + dest-choice | ✅ |
| └ `station_charging_curve_15min_2025.parquet` | parquet · 176.6M rows/shard · 3.77G/shard (+17.9G csv) | ED | per-shard sim | ✅ |
| └ `station_metadata_2025.json` | json · ~26,630 stations · 7.86M/shard | SL | OCM stations | ✅ |
| └ `station_summary_2025.csv` | csv · ~26,630 · 3.79M/shard | INT | curve agg | ✅ |
| └ `station_counts_2025.parquet` / `station_day_counts_2025.parquet` | parquet · 26,630 / 5.23M · 255K / 11M | INT | curve agg | ✅ |
| └ `private_car_charging_events.parquet` | parquet · 78.7M/shard · 3.85G | INT | sim sessions | ✅ |
| └ `private_car_home_charging_events.parquet` | parquet · 43.3M/shard · 1.97G | INT | sim (home) | ✅ |
| └ `private_car_failed_charging_events.parquet` | parquet · 17.0M/shard · 707M | DIAG | sim (unmet) | ✅ |
| └ `private_car_trip_records.parquet` | parquet · 62.2M/shard · 3.33G | INT | NTS trips | ✅ |
| └ `scotland_dz2011_to_dz2022_area_crosswalk_2025.csv` | csv · ~24,947 · 8.59M | SL | SG boundary shapefiles | ✅ |
| └ `data_quality_report.md` + `preflight_*_2025.{csv,json}` | md/csv/json · 32K report | RPT | run metadata | ✅ |
| `privatecar_full_2025_shards_16/` (v1) | 16 shard dirs · 392 G · ~165.9M rows/shard | FIN (superseded) | same | ✅ |
| `privatecar_full_2025_shards_32/` | 32 stub dirs · 28 G · **0 parquet** | DIAG (incomplete) | same | ✅ |
| `privatecar_full_2025_merged/` | 3 log files · 13 K · **no parquet** | RPT | merge logs | ✅ |
| `privatecar_server_smoke_5/` | dir · 5 vehicles · 13 M | DIAG | same (—limit 5) | ✅ |
| `privatecar_server_benchmark_500/` | dir · 499 veh · 273 M | DIAG | same (—limit 500) | ✅ |
| `privatecar_server_shard_benchmark_4/` | dir · 500 veh / 4 shards · 564 M | DIAG | same (4-shard) | ✅ |
| `privatecar_server_shard_benchmark_4_seedfix/` | dir · 500 veh / 4 shards · 563 M | DIAG | same (post seed-fix) | ✅ |

### A.2 Expected demand — Bus (`~/Work/Nature_EV_2025/outputs/` unless noted)

Each bus run dir contains the same dataset family; schema is shared. `depot_load_15min` is the demand product; the rest are intermediates/diagnostics.

| Run dir | Window | Method | total charge | Role | Prov |
|---|---|---|---|---|---|
| `bus_annual_depot_load_main/` **(MAIN / canonical)** | 9-mo (276 d) | home-depot `service_supply_weighted`, daily SOC reset | 134.6 GWh | ED | ✅ |
| `bus_annual_depot_load_carryover/` | 9-mo (276 d) | MAIN + multi-day SOC carryover (+`is_warmup`) | 141.0 / 133.9 GWh (incl/excl warmup) | ED | ✅ |
| `bus_annual_depot_load_pr15/` | 12-mo (365 d) | home-depot, full year (London-weighted) | 142.1 GWh | ED | ✅ |
| `bus_annual_depot_load_pr1_healthy/` | 9-mo (276 d) | PR1 no-home-depot (apples-to-apples vs MAIN) | 155.0 GWh | ED | ✅ |
| `bus_annual_depot_load/` | 12-mo (365 d) | earliest PR1, no home-depot | 172.6 GWh | ED (superseded) | ✅ |
| `bus_annual_depot_load_sens_population/` | 9-mo (276 d) | sensitivity: `source_lsoa_nearest` (urban-biased) | 143.0 GWh | ED (biased; not headline) | ✅ |
| `_batch_ref/` | 2 days | smoke/reference batch | 87.8 MWh | DIAG (not a demand result) | ✅ |

Per-run sub-datasets (MAIN row counts; schema in §B): `depot_load_15min` (10.99M), `depot_daily_summary` (753K), `depot_registry.parquet` (7,415, SL), `depot_supply_demand.parquet` (7,412, DIAG, home-depot runs only), `bus_charging_events` (1.35M, INT), `bus_ev_state_records` (164.7M, INT), `bus_trip_records` (9.74M, INT), `vehicle_day_soc_summary` (1.21M, INT), `vehicle_day_events` (22.97M, INT), `vehicle_day_assignments.parquet` (1.21M, INT — **carries `region_key`**); carryover adds `vehicle_soc_state` (1.47M, INT). **Note:** each partitioned dir has a coalesced single-file twin (same rows) — do not double-count.

Bus outputs under `~/Work/Modelling/outputs/` (distinct pipeline):

| Result | Format · rows · size | Role | Prov |
|---|---|---|---|
| `bus_depot_only_sample/depot_load_15min.parquet` | parquet · 50,675 · 440K | ED (sample scenario; carries `region_key`) | ✅ |
| `bus_depot_only_sample/operational_depot_registry.parquet` | parquet · 2,393 · 91K | SL | ✅ |
| `bus_depot_only_sample/case_soc_summary.parquet` | parquet · 5,328 · 651K | FIN | ✅ |
| `bus_depot_only_sample/` (12 more parquet + 3 reports) | parquet/md/json | INT / DIAG / SRC / RPT | ✅ |
| `bus_annual_load_profile.parquet` | parquet · 35,040 · 346K | FIN (national total, **no geography**) | ✅ |
| `bus_annual_per_block.parquet` | parquet · 214,915 · 16M | FIN (per-block annual ledger, **no LSOA**) | ✅ |

### A.3 Expected demand — Coach (`~/Work/Nature_EV_2025/outputs/coach_annual_depot_load/`)

| Result | Format · rows · size | Role | Prov |
|---|---|---|---|
| `depot_load_15min.parquet` (== dir) | parquet · 52,950 · 684K (dir 4.7M) | ED | ✅ |
| `depot_daily_summary.parquet` (== dir) | parquet · 4,142 · 60K | ED | ✅ |
| `depot_registry.parquet` | parquet · 134 · 15K | SL | ✅ |
| `depot_supply_demand.parquet` | parquet · 134 · 13K | DIAG | ✅ |
| `vehicle_day_assignments.parquet` (== dir) | parquet · 9,680 · 331K | INT (**`region_key`**) | ✅ |
| `bus_charging_events/` (bus_*-named; coach content) | parquet · 9,781 · 7.7M | INT | ✅ |
| `block_templates_lsoa.parquet` / `block_instances_annual.parquet` | parquet · 24,955 / 427,915 | INT | ✅ |
| `unmatched_sampled_blocks.parquet` | parquet · 39,238 · 959K | DIAG | ✅ |
| preflight + diagnostics bundle (12 files incl. `coach_chains_long.parquet` 1.53M) | parquet/json/md/log | DIAG / RPT | ✅ |
| `depot_load_map.html` | html · 64K | WEB | ✅ |

### A.4 Cross-run analysis, web exports, actual supply, source data, unrelated

| Result | Format · rows · size | Role | Prov |
|---|---|---|---|
| `_analysis/coach_phaseB_acceptance/REPORT.md` | md · 8.5K | RPT (coach acceptance) | ✅ |
| `_analysis/pr1_vs_main/` (REPORT + ~50 csv/png) | md+csv+png · 3.5M | RPT (**BUS**, not coach) | ◐ |
| `_analysis/pr2_carryover_vs_main/REPORT.md` | md · 3.7K | RPT (**BUS**) | ◐ |
| `web_export_depot_coach/` (index + 253 daily json + `Depots_coach.csv`) | json+csv · 256 files · ~0.1M | WEB | ✅ |
| `~/Work/UK-EV-Charging-Stations/UK_OCM_connectors_expanded_with_bus_and_LAD_LSOA.csv` | csv · 66,071 · 9.6M | **AS** (key) | ✅ |
| `…/UK_OCM_stations_labeled.csv` | csv · 26,959 · 3.4M | FIN (+land-use label) | ✅ |
| `…/UK_OCM_stations.csv` / `…_with_band_capacity.csv` | csv · 26,959 · 1.7M / 2.3M | INT | ✅ |
| `…/UK_OCM_connectors.csv` | csv · 46,498 · 2.9M | INT (**only table with ConnectionType/CurrentType**) | ✅ |
| `…/UK_OCM_connectors_expanded.csv` | csv · 65,512 · 5.8M | INT | ✅ |
| `…/UK_OCM_connectors_expanded_with_bus_and_LAD.csv` | csv · 66,071 · 8.7M | INT | ✅ |
| `…/UK_operating_connectors_minimal_with_level.csv` | csv · 46,522 · 3.9M | SRC (closest to OCM API) | ✅ |
| `…/Charging_stations.csv` | csv · 27,048 · 1.3M | INT (lineage not fully traced) | ◐ |
| `…/Buses.csv` | csv · 552 · 36K | SL (grid buses) | ◐ |
| `…/OSM_POI_Labeling/Local_Authority_Districts_May_2024_…BFE…geojson` | geojson · 250M | SL (LAD boundaries) | ✅ |
| `…/OSM_POI_Labeling/cache/` | tile cache · ~13G | (not a result) | ✅ |
| `~/Work/Data/Units/ONSPD_MAY_2025_UK.csv` | csv · 2,714,964 · 1.4G | **SL** (postcode→LSOA21/LAD/region) | ✅ |
| `~/Work/Data/Charging_stations/OSM_POI_Labeling/destination_choice_table.parquet` | parquet · 153,698,722 · 1.1G | INT/SRC (origin→dest LSOA prob) | ✅ |
| `~/Work/NatureEV/results_1day/` | json+csv+npy+png · 6 files · 720K | OTHER (**DCOPF generator costs, not EV**) | ✅ |
| `~/Work/NatureEV/results_30days/` | empty | OTHER (empty) | ✅ |
| `~/Work/Web/public/data/results/` | empty | WEB (empty) | ✅ |
| `~/Projects/Web/public/data/results_with_connectors/` (off-`~/Work`) | 367 json · 3.5G | WEB (private-car national, only completed merged artifact) | ✅ |

---

## B. 逐结果明细 / Per-result detail

> Column names are copied **verbatim** from the parquet/CSV schemas (incl. en-dash `–` and spaces). For large datasets, row counts come from parquet footer metadata only. Replica-per-shard datasets are described once with `n_files`.

### B1. Private-car full-year charging simulation (core expected demand)

**Producer (all per-shard outputs):** `scripts/run_privatecar_charging_curves.py` → `mobility/cars/station_curves.py:run_privatecar_station_curve_pipeline`. Orchestrated by `privatecar_full_2025_shards_16_v2/run_shards_controller.sh` (16-shard memory-aware controller) + `auto_finish.sh`. Vehicles split into 16 disjoint **vehicle** shards (`--vehicle-shard-index/--vehicle-shard-count`, ~92,090 veh/shard from a 1,478,272-EV national fleet). Charging is **uncontrolled** plug-in, 15-min resolution, `queue_model=not_considered`; public power = min(station capacity, vehicle AC power).

**Confirmed inputs** (`mobility/cars/data_loader.py`, `station_curves.py`, `data_quality_report.md`): `data/EV_UK_LSOA_2025_with_energy.csv` (EV fleet, country split E 1,330,701 / N 23,194 / S 88,550 / W 35,827); `data/person_fleet.parquet` (one NTS person per `EV_ID`); `data/trip_recent_filtered.csv` (NTS trip chains); `data/UK_OCM_stations_labeled.csv` (public station registry); `../Data/Charging_stations/OSM_POI_Labeling/destination_choice_table.parquet`; Scotland boundary shapefiles for the DZ2011→DZ2022 crosswalk.

#### `station_charging_curve_15min_2025.parquet` — **EXPECTED DEMAND (core)**
- **Path:** `~/Work/Modelling/outputs/privatecar_full_2025_shards_16_v2/shard_*/station_charging_curve_15min_2025.parquet` (+ identical `.csv` twin ~17.9 G/shard)
- **Format · rows · size:** parquet (16 shard replicas, **SUM across shards**) · 176,600,312 rows/shard · 3.77 G/shard
- **Columns (verbatim):** `station_id` (large_string, join key; synthetic sequential ids `'1','2',…`) · `time_bin_start` (timestamp[ms], 2025-01-01 00:00 → 2025-12-31 23:45) · `time_bin_end` (timestamp[ms]) · `date` (large_string, YYYY-MM-DD) · `energy_kwh` (double, **PARTIAL per shard** ~31.3–31.9M kWh/shard; sum across 16 for national) · `avg_power_kw` (double = energy_kwh×4, partial) · `active_vehicle_count` (int64) · `charging_session_count` (int64)
- **Spatiotemporal:** 15-min × 365 days, full-year 2025; spatial via `station_id`→`station_metadata` (lat/lon, lsoa_code, region). National (E/W/S/N) but each shard = 1/16 of vehicles' load.
- **Provenance:** ✅ confirmed — `station_curves.py:2669-2670` writes parquet/csv; `data_quality_report.md` scope `private_car_public_charging_only`. **Public charging only** (excludes home).
- **Generated:** 2026-05-19 .. 2026-05-24 (v2 shards).

#### `station_metadata_2025.json` — spatial-linking
- **Path:** same shards (replica). JSON object `{schema_version, scope, year, stations[]}`. ~26,630 stations · 7.86 MB/shard.
- **Columns (verbatim, per `stations[]` record):** `station_id` (string) · `station_name` (string, synthetic "Station N") · `latitude` (number) · `longitude` (number) · `station_type` (string, e.g. "Fast site") · `station_label` (string; destination class work/leisure/shopping/…) · `capacity_kw` (number) · `lsoa_code` (string; LSOA21 `E01…/W01…` or Scotland DZ `S01…`) · `region` (string; `EW`/`SC`)
- **Role:** SL — required to map curve `energy_kwh` to a region (via lat/lon or `lsoa_code`). **Provenance:** ✅ — `station_curves.py` reads `UK_OCM_stations_labeled.csv`, renames `StationID→station_id` etc.
- **Caveat:** `region` is only EW/SC (Scotland flag), not a fine admin region — use `lsoa_code` for region rollups.

#### `station_summary_2025.csv` — derived-intermediate
- ~26,630 rows · 3.79 MB/shard. **Columns:** `station_id`, `station_name`, `latitude`, `longitude`, `total_energy_kwh_2025` (PARTIAL per shard, additive), `peak_power_kw_2025` (**per-shard, NOT additive** — true national peak needs the 15-min curve), `peak_time_2025`, `active_days_2025`, `total_sessions_2025`, `unique_vehicles_2025`, `average_daily_energy_kwh`, `max_daily_energy_kwh`. **Provenance:** ✅ `station_curves.py` build_station_summary.

#### `station_counts_2025.parquet` / `station_day_counts_2025.parquet` — derived-intermediate
- `station_counts`: 26,630 rows · 255K. **Columns:** `station_id`, `unique_vehicles` (int64), `total_sessions` (int64). Additive across shards (vehicles disjoint).
- `station_day_counts`: 5,231,510 rows · 11 MB. **Columns:** `station_id`, `date` (large_string), `unique_vehicles`, `total_sessions`. Additive on `(station_id,date)`. **Provenance:** ✅ `combine_chunk_outputs`.

#### `private_car_charging_events.parquet` — derived-intermediate (unified sessions)
- 78,692,329 rows/shard · 3.85 G. **Columns (verbatim):** `ev_id`, `person_id`, `event_id`, `simulation_week` (int64), `date`, `charging_start_time` (timestamp[ns]), `charging_end_time`, `charging_lsoa`, `home_lsoa`, `charging_type`, `can_charge` (bool), `station_id`, `charging_power_kw` (double), `charged_energy_kwh` (double), `soc_before_charging`, `soc_after_charging`, `reason`, `holiday_week` (bool). Both home+public via `charging_type`/`station_id`. Useful for **LSOA-level** demand (`charging_lsoa`) beyond station lat/lon. **Provenance:** ✅. (Skipped by slim merge to limit RAM.)

#### `private_car_home_charging_events.parquet` — derived-intermediate (home only)
- 43,254,891 rows/shard · 1.97 G. Same column set as above, but `station_id` **EMPTY** and `charging_power_kw` = HOME_CHARGER_KW; `charging_lsoa` = `home_lsoa`. **Role:** the at-home baseline that does **not** compete for public stations. **Do not double-count with public.** **Provenance:** ✅.

#### `private_car_failed_charging_events.parquet` — diagnostic (unmet demand)
- 17,042,544 rows/shard · 707 M. Same event schema (with `reason` = why unmet). **Directly relevant** to infrastructure-weak detection: aggregate unmet attempts by `charging_lsoa` across 16 shards. **Provenance:** ✅.

#### `private_car_trip_records.parquet` — derived-intermediate (NTS trips → energy)
- 62,218,447 rows/shard · 3.33 G. **Columns (verbatim):** `ev_id`, `person_id`, `trip_id`, `trip_sequence_id` (int64), `simulation_week`, `date`, `day_of_week` (int64), `origin_lsoa`, `destination_lsoa`, `purpose_original`, `purpose_final`, `departure_time` (double), `arrival_time` (double), `distance_km`, `energy_consumed_kwh`, `soc_before_trip`, `soc_after_trip`, `holiday_week` (bool), `is_holiday_modified` (bool), `holiday_rule_applied` (bool). Used as the controller's **completion marker** (all 16 v2 shards have it → v2 complete). **Provenance:** ✅ (`data_loader.py:61` trip_recent_filtered.csv).

#### `scotland_dz2011_to_dz2022_area_crosswalk_2025.csv` — spatial-linking
- ~24,947 rows · 8.59 MB/shard. **Columns:** `dz2011` (string), `dz2022` (string), `area_weight` (float). Area-weighted Scotland Data Zone crosswalk; all 88,550 Scottish fleet rows reassigned, 0 unmapped. **Provenance:** ✅ — `mobility/cars/scotland_geography.py:153` from `SG_DataZone_Bdry_2011.shp` + `SG_DataZone_Bdry_2022.shp` (under `~/Projects/Data/Charging_stations/SG_DataZoneBdry_*`).

#### `data_quality_report.md` (+ `preflight_*_2025.{csv,json}`) — report
- ~32 KB/shard (v2; v1 ~14 KB). Confirms scope/physics, vehicle counts, input file list, Scotland crosswalk, ~302 vehicles/0.33% failed referential integrity → `failed_vehicles_2025.csv`. Sidecars: `preflight_geography_2025.json`, `preflight_referential_integrity_2025.json`, `preflight_missing_by_{LAD,nts_region,home_lsoa,Model,…}_2025.csv`. **Provenance:** ✅.

#### Run-version status (private car)
- **`shards_16_v2`** (685 G, 16 shards) — **CANONICAL/COMPLETE.** Early `controller.log` shows 14/16 shards once "failed" + an aborted auto_finish merge, but rerun3/rerun4 fixed all; every shard now carries the completion marker + current 32 K report.
- **`shards_16`** (392 G, v1) — complete but **SUPERSEDED** (older code, ~165.9M curve rows/shard, 14 K report, May 13).
- **`shards_32`** (28 G) — **INCOMPLETE/ABANDONED** (shard_0 stalled at chunk 128/461; 6 K stub report; **no parquet**). Do not use.
- **`privatecar_full_2025_merged`** — **logs only**, no merged parquet. `merge_privatecar_station_curve_shards_slim.py` (`--output-dir` required) reran but stopped at shard 7/16 and wrote nothing here. The connectors merge (`merge_privatecar_station_curve_shards.py`) DID complete → 365 daily JSONs at `~/Projects/Web/public/data/results_with_connectors/` (9,345,315 (date,station) pairs, 365 dates), using `~/Projects/Web/public/data/UK_OCM_connectors_expanded_with_bus_and_LAD_LSOA.csv`.

#### Private-car benchmark/smoke runs — diagnostic (NOT science)
All from `run_privatecar_charging_curves.py` at small `--limit`, same output families, **superseded by the full run**. Same `station_charging_curve_15min_2025` 8-column schema (`station_id`,`time_bin_start`,`time_bin_end`,`date`,`energy_kwh`,`avg_power_kw`,`active_vehicle_count`,`charging_session_count`).
- `privatecar_server_smoke_5` (13 M, 5 vehicles, curve 30,272 rows) — correctness smoke.
- `privatecar_server_benchmark_500` (273 M, 499 veh, curve 1,793,403 rows, 5,120 stations) — single-process performance benchmark.
- `privatecar_server_shard_benchmark_4` (564 M, 500 veh / 4 shards, merged 1,789,359 rows) — parallelism + merge benchmark (pre-seedfix), has `merged/merge_manifest_2025.json` (public_station_energy_kwh 129,543.73).
- `privatecar_server_shard_benchmark_4_seedfix` (563 M, merged 1,792,254 rows; 129,765.43 kWh) — re-run after commit `ac69646` "stable vehicle seed generation for shard-invariant schedules"; small row/energy diffs are the determinism fix, not new science.
- `station_metadata_2025.json` is **byte-identical (7,855,864 B)** across all four runs = full OCM station catalogue regardless of subsample.

### B2. Bus annual depot-load family (`~/Work/Nature_EV_2025/outputs/`)

**Producer:** `scripts/run_bus_annual_depot_load.py` → `mobility/bus/annual_*.py`. **Inputs:** block source `outputs/all_blocks.parquet` (DEFAULT_BLOCKS; GTFS-derived; 1,668,452 trip rows → 214,915 blocks — **note: not present at that default path in the current checkout**) → `block_templates*` → `block_instances_annual` (~14.4–14.6M); EV inventory `data/EV_UK_LSOA_2025_with_energy.csv` → `ev_bus_specs.parquet` (5,328 valid specs); `depot_registry.parquet` (7,415 inferred anchors); ONSPD LSOA21→region lookup. Pipeline: attach LSOA/region → `annual_vehicle_day_assignment` (`sample_then_feasible_match[_home_depot]`) → SOC sim (`annual_depot_soc`/`annual_soc_state`) → `annual_depot_events` → `aggregate_depot_load_15min` (asserts energy reconciles to charging-event ledger).

> Schema below is described from `bus_annual_depot_load_main`; **identical across all runs** except where noted. `depot_load_15min`/`depot_daily_summary` dirs are **hive-partitioned on `service_date`** (recoverable only from the partition path) and have a **coalesced single-file twin** (`…​.parquet`) with the same rows but **no `service_date` column** — do not double-count.

#### `depot_load_15min` (MAIN) — **EXPECTED DEMAND (canonical bus)**
- **Path:** `…/bus_annual_depot_load_main/depot_load_15min` (276 partitions) · 10,985,687 rows · 107 M (dir).
- **Columns (verbatim):** `depot_id` (large_string; form `opdepot_OP<n>_<LSOA|'missing'>`, `_missing` ⇒ unmappable) · `depot_lsoa` (large_string; primary spatial link, join externally to region/LAD) · `depot_lat` (double; null for `_missing`) · `depot_lon` (double; null for `_missing`) · `depot_confidence` (large_string; low/high/missing) · `slot_date` (large_string; calendar day load falls on, can differ from `service_date` partition for overnight) · `slot_index` (int64; 0–95) · `slot_start_datetime` (timestamp[ns]) · `slot_end_datetime` (timestamp[ns]) · `charge_kwh` (double; = `average_kw`×0.25) · `average_kw` (double; the per-depot demand signal) · `n_charging_vehicles` (int64) · `scenario_mode` (large_string; `ev_stock_scale`)
- **Spatiotemporal:** per-depot 15-min; `service_date` 2026-04-17 .. 2027-01-17 (276 d). To get a wall-clock depot profile, group by `(depot_id, slot_start_datetime)` and SUM over `service_dates`. Natural key `(depot_id, service_date, slot_date, slot_index)`. **~33% of charge at `_missing` depots (unmappable).** No `region_key`. **Generated:** 2026-06-04. **Provenance:** ✅.

#### `depot_daily_summary` (MAIN) — **EXPECTED DEMAND (per-depot daily peak/energy)**
- **Path:** `…/bus_annual_depot_load_main/depot_daily_summary` (276 partitions) · 753,116 rows · 33 M.
- **Columns (verbatim):** `depot_id`, `depot_lsoa`, `depot_lat`, `depot_lon`, `depot_confidence`, `slot_date`, `daily_charge_kwh` (double), `daily_peak_kw` (double; **infrastructure sizing signal**), `n_vehicle_days` (int64), `n_charging_vehicles` (int64), `n_infeasible_vehicle_days` (int64; 0 in full runs), `share_infeasible_vehicle_days` (double). Reconciles exactly (0.0 diff) to `depot_load_15min` on `(depot_id, service_date, slot_date)`. **Provenance:** ✅.

#### `depot_registry.parquet` (shared) — spatial-linking
- 7,415 rows · 278 K. **Columns (verbatim):** `depot_id`, `agency_id`, `depot_lat`, `depot_lon`, `depot_lsoa`, `depot_source`, `depot_confidence`, `is_physical_depot` (bool, **False everywhere**), `is_operational_anchor` (bool), `source_block_template_count` (int64), `source_block_instance_count` (int64), `manual_review_flag` (bool), `limitation_note`, `depot_coordinate_source`. The canonical `depot_id`→(lsoa,lat,lon,confidence) map; filter mappable vs `_missing`. **Provenance:** ✅ (`annual_depot_registry.py`).

#### `vehicle_day_assignments.parquet` (MAIN) — derived-intermediate (**carries `region_key`**)
- 1,212,046 rows · 109 M. **Columns (verbatim, 24 total; key ones):** `vehicle_day_id`, `vehicle_spec_id`, `block_instance_id`, `depot_id`, `home_depot_id` (home-depot runs), `region_key` (large_string; GOR `E12*` or country, from ONSPD `end_lsoa` lookup), `assignment_status`, `assignment_method` (`sample_then_feasible_match_home_depot` in MAIN), `sample_weight` (double), `required_kwh_est` (double), `deadhead_km_est` (double), `daily_soc_mode`, `is_warmup` (bool; carryover flags first 14 d). **`region_key` here is the bridge** to attribute load (via `depot_id`) to a region. **Caveat:** derived from block `end_lsoa`, not depot location. **Provenance:** ✅ (`annual_vehicle_day_assignment.py`).

#### Other MAIN sub-datasets (derived-intermediate unless noted)
- `depot_supply_demand.parquet` (DIAG, home-depot runs only) — 7,412 rows. Columns: `depot_id`, `n_block_instances`, `n_service_dates`, `mean_daily_blocks`, `n_home_vehicles`, `supply_demand_ratio`, `depot_lsoa`, `depot_lat`, `depot_lon`.
- `bus_charging_events` (276 part · 1,349,504 rows; 33 cols) — event ledger `depot_load_15min` aggregates from. Key cols: `ev_id`, `vehicle_day_id`, `vehicle_spec_id`, `block_instance_id`, `date`, `charging_start_time`, `charging_end_time`, `charging_lsoa`, `depot_lsoa`, `depot_id`, `charging_power_kw`, `charged_energy_kwh`, `soc_before_charging`, `soc_after_charging`, `depot_event_type`, `scenario_mode` (+ person_id, bus_id, event_id, event_seq, block_template_id, agency_id, block_id, simulation_week, charging_type, can_charge, station_id, soc_*_kwh, reason, holiday_week).
- `bus_ev_state_records` (276 part · 164,650,536 rows; 45 cols) — 15-min EV state stream. Key cols: `ev_id`, `vehicle_day_id`, `slot_date`, `slot_index`, `slot_start_datetime`, `current_lsoa`, `depot_id`, `depot_lsoa`, `is_charging`, `charge_kwh`, `soc_start`, `soc_end`, `scenario_mode` (+ lat/lon, origin/dest lsoa, location_status/confidence, is_moving, can_charge, charging_type, energy_consumed_kwh). **Largest table — never load fully.**
- `bus_trip_records` (276 part · 9,743,046 rows; 36 cols) — per-trip ledger. Key cols: `vehicle_day_id`, `trip_id`, `origin_lsoa`, `destination_lsoa`, `departure_datetime`, `arrival_datetime`, `distance_km`, `energy_consumed_kwh`, `soc_before_trip`, `soc_after_trip`, `scenario_mode`.
- `vehicle_day_soc_summary` (276 part · 1,212,046 rows; 22 cols) — per-vehicle-day feasibility/energy. Key cols: `vehicle_day_id`, `vehicle_spec_id`, `depot_id`, `depot_lsoa`, `battery_kwh`, `ac_charge_kw_max`, `depot_power_kw`, `total_deadhead_km`, `total_energy_kwh`, `total_charge_kwh`, `energy_shortfall_kwh`, `depot_only_feasible` (bool), `breaches_zero_soc` (bool), `infeasibility_reason`, `scenario_mode`.
- `vehicle_day_events` (276 part · 22,968,239 rows; 35 cols) — event-level vehicle-day timeline (`event_type`, datetimes, `distance_km`, `energy_kwh`, `charge_power_kw`, `charge_kwh_added`, `soc_*_kwh`, `battery_kwh`, `ac_charge_kw_max`, depot keys).
- `vehicle_soc_state` (**carryover only**, 276 part · 1,470,528 rows; 26 cols) — per-vehicle SOC carried across day boundaries (`soc_day_boundary_hour=6`). Key cols: `vehicle_spec_id`, `soc_kwh`, `last_event_end_ts`, `has_pending`, `pending_*` seam fields, `home_depot_id`, `home_depot_lsoa`, `battery_kwh`, `ac_charge_kw_max`, `valid_params`.

#### Per-run distinctions (demand tables; schema = MAIN unless noted)
- `bus_annual_depot_load_main` — **MAIN/canonical**, home-depot `service_supply_weighted`, daily reset, 134.6 GWh.
- `bus_annual_depot_load_carryover` — MAIN + multi-day carryover; **adds `is_warmup` (bool)** column to `depot_load_15min`; 141.0/133.9 GWh (incl/excl warmup); overnight charge shifts 00–06h→06–09h; adds `vehicle_soc_state` + `depot_load_map.html`. 49 calendar-decay suspect dates; `unknown_depot_charge_share=0.3381` (47.7 GWh unmappable). **Recommended for time-of-day realism (filter `is_warmup=False`).**
- `bus_annual_depot_load_pr15` — home-depot, **full 12-month** (365 part, 9,034,437 rows); London-weighted; 142.1 GWh.
- `bus_annual_depot_load_pr1_healthy` — PR1 no-home-depot, 9-month (276 part, 11,880,845 rows); 155.0 GWh; the PR1 comparator in `_analysis/pr1_vs_main`. No `depot_supply_demand`.
- `bus_annual_depot_load` — earliest PR1, **full 12-month** (365 part, 12,498,726 rows); 172.6 GWh; **superseded**.
- `bus_annual_depot_load_sens_population` — sensitivity `source_lsoa_nearest` (urban/London-biased); 276 part, 6,576,795 rows; 143.0 GWh. **Not a headline estimate** — bounds spatial uncertainty only.
- `_batch_ref` — 2-day reference batch (23 single-file parquet; `depot_load_15min.parquet` 6,342 rows; **uniquely retains a `service_date` column**); lsoa_attach 11.5%, 800 vehicle-days. **Not a demand result.**

### B2b. Bus outputs under `~/Work/Modelling/outputs/`

**(i) `bus_depot_only_sample/`** — depot-only EV-bus-stock SAMPLE scenario. **Producer:** `scripts/run_bus_depot_only_sample.py` → `mobility/bus/depot_only_*.py`, run 2026-06-04 18:35–18:45. **Inputs:** `--blocks=../Data/EV_behavior/Bus_Data/all_blocks.parquet`, `--ev-inventory=data/EV_UK_LSOA_2025_with_energy.csv`, `--onspd-path=/home/mazhichuan/Projects/Data/Units/ONSPD_MAY_2025_UK.csv`. 5,328 cases, 94.97% feasible, 589,097 kWh (single representative service day 2026-06-03 + overnight tail). **NOT a UK all-bus total.**

- `depot_load_15min.parquet` — **EXPECTED DEMAND.** 50,675 rows · 440 K. **Columns (verbatim):** `depot_id` (large_string; `opdepot_OP####_E0#######`, 2,393 depots) · `operational_depot_lsoa` (large_string; LSOA21) · `region_key` (large_string; 12 values incl. `unknown`) · `time_slot` (large_string) · `slot_start` (timestamp[ns]; 2026-06-03 00:15 → 2026-06-05 01:00) · `slot_end` (timestamp[ns]) · `charge_kwh` (double; Σ=589,097) · `average_kw` (double = charge_kwh/0.25) · `n_active_cases` (int64) · `sample_mode` (large_string; `full_ev_inventory`) · `weighting_mode` (large_string; `unweighted_ev_stock_scenario`). **This one carries `region_key`** (unlike the Nature_EV bus family). **Provenance:** ✅ (`depot_only_outputs.py`).
- `operational_depot_registry.parquet` — SL. 2,393 rows · 91 K. **Columns:** `depot_id`, `agency_id`, `operational_depot_lsoa`, `region_key`, `depot_lat`, `depot_lon`, `depot_confidence` (high 881/medium 1506/low 6), `is_operational_anchor` (bool), `source_block_template_count` (int64), `manual_review_flag` (bool), `depot_coordinate_source`, `anchor_limitation_note`.
- `case_soc_summary.parquet` — FIN. 5,328 rows · 651 K. Per-case SOC/energy/charge/feasibility (29 cols incl. `simulation_case_id`, `vehicle_model`, `depot_id`, `operational_depot_lsoa`, `region_key`, `battery_kwh`, `consumption_kwh_per_km`, `ac_charge_kw_max`, `depot_power_kw`, `total_passenger_km`, `total_deadhead_km`, `total_energy_kwh`, `total_charge_kwh`, `min_soc_kwh`, `energy_shortfall_kwh`, `depot_only_feasible` (bool, 94.97% True), `infeasibility_reason`).
- `simulation_cases.parquet` (INT, 5,328 · 3.8 M, ~63 cols incl. trip_* lists) · `sampled_blocks.parquet` (INT, 5,328 · 4.0 M) · `vehicle_day_events.parquet` (INT, 97,638 · 5.6 M, event ledger) · `block_templates.parquet` (**SRC**, 214,915 · 111 M — full national block pool the 5,328 sampled from) · `ev_bus_instances.parquet` (**SRC**, 5,328 · 93 K — valid EV bus specs; `source_lsoa` audit-only).
- Diagnostics: `depot_inference_diagnostics.parquet` (5,328), `lsoa_attach_diagnostics.parquet` (1-row summary; `polygon_endpoint_count=0` ⇒ all centroid-fallback; `base_region_lookup_path=…/ONSPD_MAY_2025_UK.csv`), `block_sample_diagnostics.parquet` (670 strata), `vehicle_assignment_diagnostics.parquet` (1-row), `invalid_vehicle_rows.parquet` (894 dropped, with `drop_reason`).
- Reports: `run_summary.md` (4.4 K, authoritative narrative), `preflight_summary.json`/`.md`.

**(ii) National bus-annual aggregates** — **Producer:** `scripts/run_bus_annual.py` → `mobility/bus/annual_simulation.simulate_fleet_year`, run 2026-05-11. Input `outputs/all_blocks.parquet`, warm-up 14 d, full GTFS feed-year 2026-04-17 .. 2027-04-16.
- `bus_annual_load_profile.parquet` — FIN. 35,040 rows (365 d × 96 steps) · 346 K. **Columns:** `date` (timestamp[ns]), `step` (int32; 0–95), `step_index` (int32; 0–35039), `hour` (double), `hour_of_day` (double), `load_kw` (double; **NATIONAL full-fleet** charging power, peak ~1.487 GW). **NO geography** — a single nationwide time series; cannot be regionalized.
- `bus_annual_per_block.parquet` — FIN. 214,915 rows · 16 M (~40 cols). Key cols: `agency_id`, `service_id`, `block_source`, `n_trips_template`, `active_days`, `n_active_dates`, `annual_distance_km`, `annual_energy_kwh` (fleet Σ ~1.522 TWh), `energy_charged_kwh`, `depot_kwh` (fleet Σ ~1.441 TWh), `layover_kwh`, `soc_end`, `soc_min`, `infeasible` (bool), `shortfall_kwh`, `infeasibility_reason`, `deadhead_total_km`, `deadhead_total_kwh`, `vehicle_make`, `vehicle_gen_model`, `vehicle_stock_2025_q2`, `run_scope` (`full_fleet`), `feed_year_start`, `feed_year_end`, `block_id`. **NO LSOA/region/depot key** — to regionalize, join `block_id`/template to a block→LSOA source.

### B3. Coach annual depot-load (`~/Work/Nature_EV_2025/outputs/coach_annual_depot_load/`)

**Producer:** `scripts/run_coach_annual_depot_load.py` (commit `76cf21d`) + `mobility/coach/*` + shared `mobility/{core,bus}/*`. **Inputs:** coach **TxC-2.4** source (`Data/EV_behavior/Coach_Data/TxC-2.4/`, inventory `TxCInventory17APR26.csv`, 337 XML → 8,550 journeys; via `all_coach_journeys.parquet`/`all_coach_stop_sequences.parquet`), EV coach specs (201 valid), `data/EV_UK_LSOA_2025_with_energy.csv`, ONSPD. Config: `soc_mode=carryover`, warmup 21 d (≤ 2026-05-08), radius 25 km, inter-trip relocation ON, DC 150 kW vehicle / effective 100 kW, seed 20260603. Window 2026-04-17 .. 2026-12-25 (253 service days). **Naming trap:** event files keep bus names (`bus_charging_events` etc.) — content is **coach**. Each major dataset exists as flat `.parquet` + `service_date`-partitioned dir (same rows).

#### `depot_load_15min.parquet` (== dir) — **EXPECTED DEMAND (coach)**
- 52,950 rows · flat 684 K / dir 4.7 M. **Columns (verbatim):** `depot_id` (large_string; e.g. `opdepot_NATX_E01004750`) · `depot_lsoa` (large_string; LSOA21) · `depot_lat` (double) · `depot_lon` (double) · `depot_confidence` (large_string; high/low/missing — only 35.6% of charge on high-conf) · `service_date` (large_string; partition key; recovered as column in flat file) · `slot_date` (large_string) · `slot_index` (int64) · `slot_start_datetime` (timestamp[ns]) · `slot_end_datetime` (timestamp[ns]) · `charge_kwh` (double = avg_kw×0.25) · `average_kw` (double) · `n_charging_vehicles` (int64) · `scenario_mode` (large_string) · `is_warmup` (bool; True iff `service_date ≤ 2026-05-08`). Σ charge = 2,653,208 kWh incl warmup / **2,442,096 kWh excl**. Evening-peaked (18–21h ≈45%). 134 depots, London-dominant (Victoria anchor 43%). **Provenance:** ✅.

#### `depot_daily_summary.parquet` (== dir) — EXPECTED DEMAND
- 4,142 rows. **Columns (verbatim):** `depot_id`, `depot_lsoa`, `depot_lat`, `depot_lon`, `depot_confidence`, `service_date`, `slot_date`, `daily_charge_kwh` (double — **coach energy col is `daily_charge_kwh`**, vs events' `charged_energy_kwh` — citation trap), `daily_peak_kw` (double), `n_vehicle_days` (int64), `n_charging_vehicles` (int64), `n_infeasible_vehicle_days` (int64; 0), `share_infeasible_vehicle_days` (double), `is_warmup` (bool). Reconciles 0.0 to 15-min on `(depot_id, service_date, slot_date)`.

#### Other coach datasets
- `depot_registry.parquet` (SL, 134 rows) — `depot_id`, `agency_id` (NATX 97.3%), `depot_lat`, `depot_lon`, `depot_lsoa`, `depot_source`, `depot_confidence` (high 57/low 74/missing 3), `is_physical_depot` (False), `is_operational_anchor` (bool; 131), `source_block_template_count`, `source_block_instance_count`, `manual_review_flag`, `limitation_note`, `depot_coordinate_source`.
- `depot_supply_demand.parquet` (DIAG, 134) · `vehicle_day_assignments.parquet` (INT, 9,680, **carries `region_key`**: London 5,914 / South West 1,891 / Wales 829 / …) · `bus_charging_events/` (INT, 9,781, 253 part; `charged_energy_kwh` energy col) · `block_templates_lsoa.parquet` (24,955) / `block_instances_annual.parquet` (427,915) — carry `region_key` + start/end LSOA · `unmatched_sampled_blocks.parquet` (DIAG, 39,238; `unmatched_reason` mostly long-haul range-infeasible) · preflight/diagnostics bundle (12 files incl. `coach_chains_long.parquet` 1,534,245 rows, `ev_bus_specs.parquet` 201, `coach_preflight_summary.{json,md}`).
- `depot_load_map.html` (WEB, 64 K; 61 mappable depots, 100% coverage).

### B4. Actual supply — UK existing charging infrastructure (`~/Work/UK-EV-Charging-Stations/`)

Standalone git repo (`github.com/zhichuanma/UK-EV-Charging-Stations`), separate from `Modelling`. **Source:** OpenChargeMap (OCM) GB REST API (`api.openchargemap.io/v3/poi/`, `countrycode=GB`, operational POIs only), DfT-July-2025 speed bands. **Snapshot date NOT recorded in code** — repo mtimes ~Apr 2025; treat as **~2025, unverified**. Producer notebooks: `charging_station.ipynb` (chain), `OSM_POI_Labeling/CS_OSM_POI_Labeling.ipynb` (Huff land-use label).

#### `UK_OCM_connectors_expanded_with_bus_and_LAD_LSOA.csv` — **ACTUAL SUPPLY (key deliverable)**
- 66,071 rows · 9.6 M. One row per physical connector/device (65,512 devices expanded by OCM `Quantity`; +559 from boundary joins). **Columns (verbatim):** `ConnectorID` (string; e.g. `conn_65502`) · `StationID` (int; OCM id, join key) · `Latitude` (float, WGS84) · `Longitude` (float) · `Power_kW` (float) · `DfT_Band` (string; `Below 3kW` / `Slow (3–8kW)` / `Fast (8–49kW)` / `Rapid (50–149kW)` / `Ultra-rapid (150kW+)`) · `Level` (string; OCM level title) · `BusID` (int; nearest GB grid bus) · `BusName` (string) · `Voltage_kV` (float) · `LAD24CD` (string; LAD 2024 code) · `LAD24NM` (string) · `lsoa_code` (string; **MIXED**: LSOA21CD if `region=EW`, Scotland DataZone2022 if `SC`, NI DataZone2021 if `NI`) · `region` (string; EW/SC/NI). Region split EW 58,586 / SC 6,116 / NI 1,369; UK-wide (lat 50.0–60.8, lon −7.9..+1.76). **Provenance:** ✅ (`charging_station.ipynb` cells 27–37; joins to `Buses.csv`, LAD-2024 geojson, concatenated EW LSOA21 + SC DZ2022 + NI DZ2021).
- **Limitations:** no connector-type/operator columns (those only in `UK_OCM_connectors.csv`); `lsoa_code` geography differs by nation.

#### Other actual-supply tables
- `UK_OCM_stations_labeled.csv` (FIN, 26,959 · 3.4 M; **2 identical copies**: repo root + `OSM_POI_Labeling/`, also symlinked into `~/Work/Modelling/data/`). Columns: `StationID`, `Latitude`, `Longitude`, `Title` (often blank), `TotalCapacity_kW` (float; 2.3–10,092 kW, total ~2.22 GW), `StationType` (Fast site 22,606 / Rapid 3,683 / Ultra-rapid 660 / Slow 10), `Bands` (string; `;`-joined), `label` (Huff dominant scene: leisure/shopping/work/education/holiday/personal_business/social/residential), `top_score`, `n_pois_in_radius`, `huff_score_{work,education,shopping,personal_business,social,leisure,holiday}`. `label` is a destination-attractiveness **covariate**, not supply quantity.
- `UK_OCM_stations.csv` (INT, 26,959) — clean station master: `StationID`, `Latitude`, `Longitude`, `Title`, `TotalCapacity_kW`, `StationType`, `Bands`.
- `UK_OCM_stations_with_band_capacity.csv` (INT, 26,959) — above + 5 band columns (verbatim, with en-dash): `Below 3kW`, `Fast (8–49kW)`, `Rapid (50–149kW)`, `Slow (3–8kW)`, `Ultra-rapid (150kW+)` (kW capacity per band).
- `UK_OCM_connectors.csv` (INT, 46,498) — **ONLY table with `ConnectionType` (plug) and `CurrentType` (AC/DC)**: `StationID`, `Power_kW`, `Quantity` (int), `Capacity_kW`, `DfT_Band`, `Level`, `ConnectionType`, `CurrentType`. No lat/lon (join via `StationID`). Σ`Quantity`=65,512 devices.
- `UK_OCM_connectors_expanded.csv` (INT, 65,512) — device-level with lat/lon **and** ConnectionType/CurrentType, no admin keys. DfT_Band split Slow 35,631 / Fast 13,806 / Rapid 11,846 / Ultra-rapid 4,205 / Below 3kW 24.
- `UK_OCM_connectors_expanded_with_bus_and_LAD.csv` (INT, 66,071) — device + grid bus + LAD24, before LSOA/region (superseded by `_LSOA` file).
- `UK_operating_connectors_minimal_with_level.csv` (**SRC**, 46,522) — closest to OCM API: `RowID`, `StationID`, `Latitude`, `Longitude`, `Capacity_kW`, `Quantity`, `Level_OCM`, `DfT_Level`.
- `Charging_stations.csv` (INT◐, 27,048) — station→grid-bus capacity (`CS_ID`, `Capacity_kW`, `Station_Type`, `Bus_ID`, `Bus_Name`, `Voltage_kV`); exact writing cell not located ⇒ **partial**.
- `Buses.csv` (SL◐, 552) — GB grid buses: `BusID`, `bus_name`, `voltage`, `x` (lon), `y` (lat), `RegionName` (often blank), `color`. External grid input.
- `OSM_POI_Labeling/Local_Authority_Districts_May_2024_…_BFE_…geojson` (SL, 250 M) — ONS LAD-2024 polygons (`LAD24CD`, `LAD24NM`, `geometry`).
- `OSM_POI_Labeling/cache/` (~13 G) — **Overpass tile cache, NOT a result** (dominates cluster size).

### B5. Source & spatial-linking data (`~/Work/Data/`)

- `Units/ONSPD_MAY_2025_UK.csv` — **SPATIAL-LINKING (master).** 2,714,964 rows · 1.4 G (53 cols; key cols verbatim): `pcd`, `pcds` (postcodes), `oslaua` (LAD), `osward` (ward), `ctry` (country), `rgn` (region), `lsoa11`, `msoa11`, `lsoa21` (**key linker**), `msoa21`, `lat`, `long`, `imd`, `oseast1m`, `osnrth1m`. ONS Postcode Directory May-2025, full UK. **Used across all mobility pipelines** as `DEFAULT_ONSPD_PATH` (`mobility/core/spatial.py:22`) for LSOA/region attach. The canonical point/postcode→region table for downstream aggregation. **Never load fully.** **Provenance:** ✅ (external ONS).
- `Charging_stations/OSM_POI_Labeling/destination_choice_table.parquet` — INT/SRC. 153,698,722 rows · 1.1 G. Columns: `origin_lsoa` (string), `purpose` (string), `dest_lsoa` (string), `prob` (float32, normalized per `(origin_lsoa,purpose)`). Origin→destination LSOA trip-choice probabilities. **Produced by** `mobility/cars/build_destination_choice_table.py` (from `lsoa_scene_attractiveness.parquet` + LSOA centroids); **consumed by** the private-car curve sim. A modelled intermediate, **not real infrastructure**. **Never load fully.** **Provenance:** ✅.

> EV-stock source `EV_UK_LSOA_2025_with_energy.csv` (174 MB; per-LSOA EV penetration with energy, columns `EV_ID, LSOA_code, LAD, Model, count, Energy_kWh, DC_Power_kW, AC_Power_kW, efficiency_wh_per_km, …`) lives at `~/Work/Modelling/data/` (symlinked into the repo `data/`). It is the **demand driver** input shared by all three modes — a source input, not an output.

### B6. Cross-run analysis & web exports (`~/Work/Nature_EV_2025/outputs/`)

- `_analysis/coach_phaseB_acceptance/REPORT.md` (RPT, 8.5 K) — coach run acceptance (2026-06-06): **ACCEPTED**, 45 checks 42P/3W/0F; headline 2,653,208 kWh incl / 2,442,096 excl warmup, 3.48 MW peak, Victoria 43.1%, 177/201 coaches used; 8 citation traps; **no external calibration baseline**. **Provenance:** ✅.
- `_analysis/pr1_vs_main/` (RPT◐, 56 files, 3.5 M) — **BUS** PR1.5-MAIN vs PR1 comparison (5,328 buses, 276 d), verdict ACCEPT. CSVs (verbatim header examples): `d1_depot_annual_comparison.csv` (`depot_id, annual_charge_kwh_main, peak_kw_main, …, annual_charge_kwh_pr1, delta_kwh`), `d5_region_table.csv` (`region, MAIN, PR1, MAIN_share, PR1_share`), `d5_unknown_and_geo_summary.csv` (`run, total_charge_kwh, unknown_depot_charge_share, london_charge_share, load_centroid_lat/lon, charge_conf_high/low/missing`), `d2_diurnal.csv` (`run, daytype, slot_index, hour, minute, mean_MW`), `d3_daily_match.csv`, `d6_pass_fail.csv` + PNGs (Lorenz, scatter, diurnal, monthly, load-duration, regional map). Producer = bus PR1-vs-MAIN analysis (named in REPORT) — exact script not pinned ⇒ **partial**.
- `_analysis/pr2_carryover_vs_main/REPORT.md` (RPT◐, 3.7 K) — **BUS** carryover vs daily-reset (depot load +4.73%, charge shifts 00–06h→06–09h, peaks ~unchanged). Do not transfer to coach (coach is evening-peaked).
- `web_export_depot_coach/` (WEB, 256 files, ~0.1 M + tar.gz) — public coach map bundle: `depot_coach_index.json` (48 steps/day, 30-min, dates_with_data 2025-04-17..2025-12-25), 253 daily fragment JSONs (each depot → 48 half-hourly avg-kW values), `Depots_coach.csv` (61 rows: `depot_id, lat, lon, lsoa, confidence, mappable, annual_charge_kwh, peak_kw, is_operational_anchor`), README, merge script. **Producer:** `scripts/export_bus_depot_web_json.py` + `build_bus_depot_load_map.py` + `merge_depot_bus_into_results.py` (parameterized for coach, commit `47804ff`). **Dates re-labelled to 2025 calendar** (source 2026); 112/365 days have no coach data. **Provenance:** ✅.
- `~/Projects/Web/public/data/results_with_connectors/` (WEB, off-`~/Work`, 367 json, 3.5 G) — private-car national per-day station load JSON (the **only completed national private-car merge artifact**); 365 daily files + 2 stray `…MacBook Pro (4).json` artifacts. **Producer:** `merge_privatecar_station_curve_shards.py` over the 16 v2 shards + `UK_OCM_connectors_expanded_with_bus_and_LAD_LSOA.csv`.

### B7. Unrelated / empty

- `~/Work/NatureEV/results_1day/` (OTHER, 6 files, 720 K) — **"SearchRealCost" DCOPF electricity-PRICE calibration** (NOT EV charging). `results.json` (optimisation record), `unit_costs.csv` (2,384 generator units; columns `UnitID, Latitude, Longitude, LAD_Code, Region, BusID, Capacity (MW), Technology, Type, SRMC_i, init_cost_EUR_MWh, cma_cost_EUR_MWh, calibrated_cost_EUR_MWh`), `calibrated_cost.npy`, `cma_cost.npy`, `loss_curve.png`, `price_comparison.png`. Producer `~/Work/NatureEV/run_1day.py` (CMA-ES). `LAD_Code/Region` = **power-plant** locations. Possible use to downstream only as an electricity-price reference. **Provenance:** ✅.
- `~/Work/NatureEV/results_30days/` — **EMPTY** (no files). `run_30days.py` exists but produced nothing retained.
- `~/Work/Web/public/data/results/` — **EMPTY** (no files), despite the directory existing.

---

## C. 数据源 → 结果 lineage 映射表 / Source → Result map

Each row connects an **upstream source** to the **processing step (script path)** and the **downstream result(s)**. Status: ✅ confirmed (code path verified) · ◐ partial · ✖ unknown.

### C.1 Inputs (sources) and where they live

| Upstream source | Path | Used as |
|---|---|---|
| EV stock/penetration (per LSOA, energy) | `~/Work/Modelling/data/EV_UK_LSOA_2025_with_energy.csv` (symlink ← repo `data/`) | demand driver — **all 3 modes** |
| NTS person/fleet binding | `~/Work/Modelling/data/person_fleet.parquet`, `person_week_library.parquet` | private-car fleet |
| NTS trip chains | `~/Work/Modelling/data/trip_recent_filtered.csv` | private-car trips |
| OCM public station registry (labeled) | `~/Work/Modelling/data/UK_OCM_stations_labeled.csv` (also in UK-EV-Charging-Stations repo) | private-car station universe |
| Destination-choice probabilities | `~/Work/Data/Charging_stations/OSM_POI_Labeling/destination_choice_table.parquet` | private-car trip destinations |
| Scotland DZ boundary shapefiles | `~/Projects/Data/Charging_stations/SG_DataZoneBdry_{2011,2022}/…shp` | private-car Scotland geography |
| GTFS-derived bus blocks | `outputs/all_blocks.parquet` (default; **absent in checkout**) and `~/Work/Data/EV_behavior/Bus_Data/all_blocks.parquet` (depot-only sample) | bus blocks |
| Coach TxC-2.4 XML + inventory | `Data/EV_behavior/Coach_Data/TxC-2.4/` + `TxCInventory17APR26.csv` | coach journeys |
| ONS Postcode Directory | `~/Work/Data/Units/ONSPD_MAY_2025_UK.csv` | LSOA/region attach — **all modes** |
| OpenChargeMap GB API | (live API, snapshot ~2025) | actual-supply registry |
| OSM Overpass POIs | (live API, cached) | station land-use labels |
| ONS LAD-2024 boundaries | `…/UK-EV-Charging-Stations/OSM_POI_Labeling/…LAD…BFE…geojson` | actual-supply LAD attach |

### C.2 Source → processing → result

| Upstream source(s) | Processing step (script/path) | Output result(s) (path) | Status |
|---|---|---|---|
| EV stock CSV + person_fleet + trip_recent_filtered + OCM stations + destination_choice_table + SG shapefiles | `scripts/run_privatecar_charging_curves.py` → `mobility/cars/station_curves.py` (16 vehicle shards) | `privatecar_full_2025_shards_16_v2/shard_*/` (`station_charging_curve_15min_2025`, `station_metadata/summary/counts`, `private_car_*_events`, `trip_records`, crosswalk, DQ report) | ✅ |
| 16 v2 shards' `station_charging_curve`/counts | `scripts/merge_privatecar_station_curve_shards_slim.py` (`--output-dir`) | **none materialized** in `privatecar_full_2025_merged/` (logs only; stopped at shard 7/16) | ✅ (incomplete) |
| 16 v2 shards + `UK_OCM_connectors_expanded_with_bus_and_LAD_LSOA.csv` | `scripts/merge_privatecar_station_curve_shards.py` | `~/Projects/Web/public/data/results_with_connectors/*.json` (365 daily) | ✅ |
| EV stock CSV + person_fleet + OCM stations (small `--limit`) | `run_privatecar_charging_curves.py` (+ `merge_privatecar_station_curve_shards.py`) | `privatecar_server_{smoke_5,benchmark_500,shard_benchmark_4,shard_benchmark_4_seedfix}/` | ✅ |
| `all_blocks.parquet` + EV stock CSV + ONSPD | `scripts/run_bus_annual_depot_load.py` → `mobility/bus/annual_*.py` | `bus_annual_depot_load{,_main,_pr15,_pr1_healthy,_carryover,_sens_population}/` (all sub-datasets), `_batch_ref/` | ✅ |
| `all_blocks.parquet` (warm-up 14d) | `scripts/run_bus_annual.py` → `annual_simulation.simulate_fleet_year` | `bus_annual_load_profile.parquet`, `bus_annual_per_block.parquet` | ✅ |
| `Bus_Data/all_blocks.parquet` + EV stock CSV + ONSPD | `scripts/run_bus_depot_only_sample.py` → `mobility/bus/depot_only_*.py` | `bus_depot_only_sample/*` (depot_load_15min, registry, case_soc_summary, …) | ✅ |
| Coach TxC-2.4 + EV stock CSV + ONSPD | `scripts/run_coach_annual_depot_load.py` → `mobility/coach/*` + shared bus engine | `coach_annual_depot_load/*` (depot_load_15min, daily_summary, registry, assignments, events, templates, diagnostics, map) | ✅ |
| coach `depot_load_15min`/`depot_daily_summary`/`depot_registry` | `scripts/export_bus_depot_web_json.py` + `build_bus_depot_load_map.py` + `merge_depot_bus_into_results.py` (coach-parameterized, `47804ff`) | `web_export_depot_coach/*`, `coach_annual_depot_load/depot_load_map.html` | ✅ |
| `bus_annual_depot_load_main` vs `…_pr1_healthy` | bus PR1-vs-MAIN analysis (script/notebook named in REPORT) | `_analysis/pr1_vs_main/*` | ◐ |
| `bus_annual_depot_load_carryover` vs `…_main` | bus PR2 carryover analysis | `_analysis/pr2_carryover_vs_main/REPORT.md` | ◐ |
| `coach_annual_depot_load/*` | coach Phase-B acceptance audit (read-only workflow) | `_analysis/coach_phaseB_acceptance/REPORT.md` | ✅ |
| OpenChargeMap GB API (operational) | `charging_station.ipynb` cell 2 | `UK_operating_connectors_minimal_with_level.csv` | ✅ |
| OCM stations objects | `charging_station.ipynb` cell 11–12 | `UK_OCM_stations.csv`, `UK_OCM_stations_with_band_capacity.csv`, `UK_OCM_connectors.csv`, `UK_OCM_connectors_expanded.csv` | ✅ |
| `UK_OCM_connectors_expanded.csv` + `Buses.csv` + LAD geojson + EW/SC/NI small-area layers | `charging_station.ipynb` cells 27–37 | `…_with_bus_and_LAD.csv` → `…_with_bus_and_LAD_LSOA.csv` (**actual-supply key**) | ✅ |
| `UK_OCM_stations.csv` + OSM Overpass POIs (Huff model) | `OSM_POI_Labeling/CS_OSM_POI_Labeling.ipynb` | `UK_OCM_stations_labeled.csv` | ✅ |
| `lsoa_scene_attractiveness.parquet` + LSOA centroids | `mobility/cars/build_destination_choice_table.py` | `destination_choice_table.parquet` | ✅ |
| ONS Postcode Directory (external) | (download) | `ONSPD_MAY_2025_UK.csv` | ✅ |
| Generators/Buses/Network/RealGenMix/wind.nc/solar.nc/… | `~/Work/NatureEV/run_1day.py` (DCOPF, CMA-ES) | `NatureEV/results_1day/*` (generator costs — **not EV**) | ✅ |

---

## D. 缺口与待确认 / Gaps & open items

### D.1 For the "expected vs real" analysis — what's missing or needs work

1. **No pre-merged national private-car demand.** The downstream must SUM the 16 `shards_16_v2` `station_charging_curve_15min_2025.parquet` files on `(station_id, time_bin_start)` (≈56 GB of parquet to scan). No single file exists; budget for the aggregation. The per-day JSON web export is national but a different (daily, connector-augmented) shape.
2. **Mode-to-mode geography keys are inconsistent.** Private-car demand keys on `station_id`→lat/lon/`lsoa_code` (EW=LSOA21, SC=DZ); bus/coach demand keys on `depot_id`→`depot_lsoa` (LSOA21, England GOR via ONSPD), with `region_key` only on assignment/template tables; actual-supply keys on `lsoa_code` (EW=LSOA21, SC=**DZ2022**, NI=**DZ2021**) + `LAD24`. **A single common geography (e.g. LSOA21 + a Scotland/NI Data-Zone bridge, or roll everything to LAD/region) must be chosen**, and the Scotland DZ vintages reconciled (private-car uses DZ2011→DZ2022 crosswalk; actual-supply uses DZ2022; ONSPD carries lsoa11/lsoa21 but Scottish "lsoa" = Data Zone).
3. **Unmappable bus load.** ~33–34% of bus charge sits at `_missing`-LSOA depots (null lat/lon) — must be **excluded** from any spatial map, which biases regional bus-demand totals. Coach is fully mappable but London-skewed.
4. **Scenario scale ≠ stock to compare against supply.** All three demand products are **current-EV-stock-scale** (bus ≈5,328 EVs; coach ~2.26% of service; private-car = the modelled EV fleet, not the full vehicle parc). To compare "expected infrastructure need" vs real supply you must decide a **penetration/scaling assumption** — the raw kWh are not a full-electrification demand.
5. **Actual-supply snapshot date is unverified (~2025)** and carries **no operator/network** and (in the geo-tagged file) **no connector-type** columns. If the analysis needs connector-type or operator splits, join `UK_OCM_connectors.csv` (has `ConnectionType`/`CurrentType`, no lat/lon) on `StationID`. Device counts use OCM `Quantity` (defaults to 1 when missing) → possibly conservative.
6. **Home vs public.** Private-car `station_charging_curve` is **public only**. Residential demand (`private_car_home_charging_events`, keyed `home_lsoa`) is separate and does not compete for public stations — keep them distinct when comparing to public infrastructure.
7. **Calendar/window normalization.** Bus runs mix 9-/12-month windows; GTFS/TxC tail dates (bus 49 flagged; coach Dec-14..25) are feed-expiry artifacts. Normalize to a common healthy window before annualizing.

### D.2 Provenance not fully confirmed (◐ / ✖)

- `_analysis/pr1_vs_main/` and `_analysis/pr2_carryover_vs_main/` — comparison REPORTs/CSVs; the exact producing script/notebook is named in the REPORT prose but not pinned to a repo path here (**◐**). Content/inputs (the bus runs compared) are confirmed.
- `UK-EV-Charging-Stations/Charging_stations.csv` — header confirmed but **no `to_csv` cell located** for this exact filename; lineage to the OCM family is inferred (**◐**).
- `UK-EV-Charging-Stations/Buses.csv` — used as input (confirmed read) but its **own origin is an external GB power-system dataset** outside the repo (**◐**).
- `station_metadata_2025.json` of benchmark runs — flagged **◐** only because it is byte-identical across runs (full catalogue, not per-run); the full-run copy's lineage is **confirmed**.
- `~/Work/Web/public/data/results/` producer — **✖ unknown** (directory empty; nothing produced).
- `all_blocks.parquet` — the upstream bus block source is **not present** at the bus-annual default path (`outputs/all_blocks.parquet`) in this checkout; the depot-only sample used a different copy (`Data/EV_behavior/Bus_Data/all_blocks.parquet`). The block universe is visible indirectly via `bus_depot_only_sample/block_templates.parquet` (214,915 rows) and `bus_annual_per_block.parquet`.

### D.3 Resolution / coverage limits (summary)

- **Spatial granularity:** private-car = station point (lat/lon) + LSOA21/DZ; bus/coach = inferred depot LSOA21 (+ England GOR region); actual-supply = device lat/lon + LSOA21/DZ + LAD24. Finest common admin unit ≈ LSOA21 (England/Wales) / Data Zone (Scotland) / LAD (UK-wide, cleanest).
- **Temporal:** private-car 15-min full-year **2025**; bus 15-min over GTFS feed-year **2026-04 → 2027-01/04**; coach 15-min **2026-04 → 2026-12**. The three demand series are on **different calendars** — align by time-of-day / month-of-year, not absolute date.
- **Geographic coverage:** private-car & actual-supply UK-wide (GB+NI for supply; E/W/S/N for demand). Bus/coach GB (England/Scotland/Wales; NI absent). Coach is London-dominant (Victoria 43%).
- **Empty/abandoned:** `privatecar_full_2025_shards_32` (no parquet), `privatecar_full_2025_merged` (logs only), `NatureEV/results_30days` (empty), `Work/Web/public/data/results` (empty).

---

*Inventory compiled read-only from `~/Work/` on 2026-06-27. Schemas/row counts via parquet footer metadata + CSV headers; provenance traced to `Modelling/scripts/` + `mobility/` and the standalone `UK-EV-Charging-Stations`/`NatureEV` repos. Column names are verbatim. Unconfirmed lineage is marked partial/unknown — not guessed.*
