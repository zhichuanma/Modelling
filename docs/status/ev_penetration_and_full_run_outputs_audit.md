# EV Penetration And Full-Run Outputs Audit

Status date: 2026-06-23

This note corrects the provenance language for regional EV stock and records the
current bus/coach full-run artifacts available through the Web data bundle.

## Scope And Path Rules

- Modelling root was located with `git -C Modelling rev-parse --show-toplevel`.
- Shared EV penetration data was found at `../Data/EV_penetration/`.
- Full bus/coach visualization outputs were found under `../Web/public/data/`.
- This audit is read-only: no annual simulation was rerun and no Web data was
  regenerated.

## EV Penetration Data

### Files

| File | Rows | Fields | Time coverage | Spatial field |
|---|---:|---|---|---|
| `../Data/EV_penetration/df_VEH0125.csv` | 896,663 | `LSOA21CD`, `LSOA21NM`, `BodyType`, `Keepership`, `LicenceStatus`, quarterly count columns | `2009 Q4` to `2025 Q4` | `LSOA21CD` |
| `../Data/EV_penetration/df_VEH0135.csv` | 293,825 | `LSOA21CD`, `LSOA21NM`, `Fuel`, `Keepership`, quarterly count columns | `2011 Q4` to `2025 Q4` | `LSOA21CD` |

### Numerator And Denominator

- `df_VEH0125.csv` contains the denominator needed for car penetration. The
  recommended denominator is `BodyType == "Cars"`, `Keepership == "Total"`,
  `LicenceStatus == "Licensed"`.
- `df_VEH0135.csv` contains EV-style numerators by `Fuel`. Use explicit
  definitions:
  - BEV numerator: `Fuel == "BATTERY ELECTRIC"`, `Keepership == "Total"`.
  - Source total numerator: `Fuel == "Total"`, `Keepership == "Total"` in
    `df_VEH0135.csv`.
  - A custom plug-in numerator may sum BEV + PHEV + range-extended fuels, but
    should be labelled separately from the source `Fuel == "Total"` aggregate.
- There is no ready-made penetration column in the two files. Penetration is
  derived after joining by `LSOA21CD` and quarter:
  `ev_penetration = ev_count / total_vehicle_count`.

For `2025 Q4`, the lightweight check found:

| Metric | Value |
|---|---:|
| Licensed cars denominator, total keepership | 34,485,453 |
| BEV numerator | 1,869,434 |
| `df_VEH0135` source `Fuel == "Total"` numerator | 2,843,169 |
| Regions with denominator rows | 43,915 |
| Regions with BEV numerator rows | 41,130 |
| Regions with source total numerator rows | 42,970 |

### Spatial Version

- The EV penetration files use `LSOA21CD`.
- Code prefixes in `df_VEH0125.csv`: `E`, `W`, `S`, `N`, and one
  `Miscellaneous` code.
- Scotland rows use `S01013482` to `S01020873`, i.e. the Data Zone 2022 range.
  This is compatible with the current Scotland geography target used after the
  DZ2011 -> DZ2022 correction.
- Northern Ireland rows use `N210...`; treat them as NI small-area/DZ-style
  units and validate joins separately when combining with GB-only layers.
- Exclude or separately label `Miscellaneous` rows in regional analysis.

## Corrected EV Stock Provenance

`data/EV_UK_LSOA_2025_with_energy.csv` is not actual EV stock or actual EV
spatial distribution. It is a synthetic/allocation fleet generated from national
vehicle totals and population weighting, then enriched with model/energy fields.

Allowed labels:

| Source | Correct provenance label | Allowed use |
|---|---|---|
| `../Data/EV_penetration/df_VEH0125.csv` + `df_VEH0135.csv` | `actual` | Regional denominator, EV numerator, and derived penetration by quarter |
| `data/EV_UK_LSOA_2025_with_energy.csv` | `synthetic_allocated` | Model input fleet, allocated demand proxy, technology/spec proxy |
| Aggregates inferred from simulation demand only | `proxy` | Scenario demand proxy when actual numerator is unavailable or intentionally not used |

Do not describe `EV_UK_LSOA_2025_with_energy.csv` as actual EV stock, actual EV
distribution, or true EV penetration.

## `region_ev_stock_mvp` Schema

Recommended MVP table:

| Column | Meaning |
|---|---|
| `region_id` | Regional code, usually `LSOA21CD` for the current MVP |
| `region_name` | Region name from the source file |
| `region_code_version` | e.g. `LSOA21`, `Scotland_DZ2022`, `NI_N210` |
| `year` | Calendar year from the quarter label |
| `quarter` | Quarter label, e.g. `Q4` |
| `period` | Source period string, e.g. `2025 Q4` |
| `ev_count` | Numerator count |
| `ev_definition` | `bev`, `source_total`, `plugin_custom`, or `allocated_synthetic_fleet` |
| `total_vehicle_count` | Denominator count, preferably licensed cars total keepership |
| `denominator_definition` | e.g. `licensed_cars_total_keepership` |
| `ev_penetration` | `ev_count / total_vehicle_count` when denominator is available |
| `source_file` | Input file path relative to the Modelling root |
| `source_table` | Source table name, e.g. `VEH0125`, `VEH0135`, or synthetic fleet |
| `provenance_type` | `actual`, `synthetic_allocated`, or `proxy` |
| `confidence_level` | `high` for direct VEH-derived actual fields; lower for synthetic/proxy |
| `notes` | Caveats such as `Miscellaneous`, NI join caveat, or custom fuel definition |

MVP grain: `region_id` x `period` x `ev_definition`. For this cycle, prefer
LSOA21/DZ2022. LAD/city tables can be produced by a documented lookup/aggregation
step after the LSOA-level table is stable.

## Web Full-Run Outputs

### Bus Depot Load

| Artifact | Finding |
|---|---|
| `../Web/public/data/depot_bus_index.json` | Dataset `bus_depot_charging_load`; source run metadata points to `outputs/bus_annual_depot_load_carryover`; `soc_mode == carryover`; 48 steps/day; 30 minutes/step; unit `avg_kw_per_half_hour`. |
| `../Web/public/data/Depots.csv` | 2,324 depot rows; 2,019 mappable; fields include `depot_id`, `lat`, `lon`, `lsoa`, `confidence`, `mappable`, `annual_charge_kwh`, `peak_kw`, `is_operational_anchor`. |
| `../Web/public/data/results/*.json` | 367 files, 365 unique dates; duplicate local files exist for `2025-01-01` and `2025-01-02`; daily JSON includes `depots` and `depots_system`. |
| Date coverage | 278 dates with data. `2025-01-22..2025-04-16` is outside the GTFS feed window and has empty depot blocks. 15 warm-up dates are listed and should be excluded or labelled. |
| Load scale | Indexed depot annual charge sums to about 140,993,140 kWh; max recorded depot `peak_kw` is 2,068.6 kW. |

Daily bus depot entries are depot-id -> 48 half-hour average-kW values.
`depots_system` contains `load_kw`, `n_active_depots`, and `source_date`.

### Coach Depot Load

| Artifact | Finding |
|---|---|
| `../Web/public/data/depot_coach_index.json` | Dataset `coach_depot_charging_load`; source run metadata points to `outputs/coach_annual_depot_load`; `soc_mode == carryover`; 48 steps/day; 30 minutes/step; unit `avg_kw_per_half_hour`. |
| `../Web/public/data/Depots_coach.csv` | 61 depot rows; all mappable; same core fields as bus depot metadata. |
| `../Web/public/data/results/*.json` | Daily JSON includes `depots_coach` and `depots_coach_system`; many dates outside the source feed window have empty coach depot blocks. |
| Date coverage | 253 dates with data; 112 2025 dates outside the source feed window; 22 warm-up wall-clock dates. |
| Load scale | Indexed coach depot annual charge sums to about 2,653,208.5 kWh; max recorded depot `peak_kw` is 1,700.0 kW. |

Coach caveats embedded in the index are important: chains are first-fit
constructs from TxC journeys, not real coach rosters; the indexed 201-coach
scenario should be interpreted as a model-input/synthetic EV scenario serving
about 2.26% of active coach service, not actual EV coach stock and not a
full-electrification projection; and the mapped `2025-12-14..2025-12-25` period
is TxC calendar decay, not December seasonality.

### Connector And Route Context

- `../Web/public/data/results_with_connectors/*.json` adds a `connectors` block
  with connector-id -> 48 half-hour values.
- `../Web/public/data/UK_OCM_connectors_expanded_with_bus_and_LAD_LSOA.csv`
  has 65,916 connector rows with `Power_kW`, `LAD24CD`, `LAD24NM`,
  `lsoa_code`, `BusID`, and voltage fields. This can support connector-level
  utilization checks when joined to the connector load arrays.
- `../Web/public/data/bus_coach_routes.geojson` has 32,365 features, and
  `bus_coach_routes_full_raw.geojson` has 47,935 features. These are useful for
  route context but should be treated as planned/service-derived geometry, not
  observed trajectories.

## Suitability For Infrastructure Analysis

The Web full-run outputs are usable for:

- depot-level demand curves and peak demand by bus/coach depot;
- spatial mismatch between simulated depot charging demand and mappable depot
  locations;
- connector-level load overlays where `results_with_connectors` is joined to
  the connector metadata;
- exploratory infrastructure sufficiency metrics such as
  `simulated_peak_kw / assumed_or_observed_charger_capacity_kw`.

They are not sufficient by themselves for:

- true grid-capacity adequacy, because charger capacity and simulated load are
  not distribution-network headroom;
- causal adoption conclusions;
- event-level waiting/unmet charging analysis unless the underlying event
  outputs are brought in;
- claims that synthetic allocation fleet counts are actual EV penetration.

Keep the core caveat visible: charger capacity is not grid capacity. A depot may
have enough simulated charger power and still be grid-constrained, and a grid
node may have spare headroom even if local public charger capacity is low.
