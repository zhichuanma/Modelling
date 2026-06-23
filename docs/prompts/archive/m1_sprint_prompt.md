# EV Bus Annual Simulation — M1 Sprint Implementation Prompt (Simulation Mode)

> Version 2 — supersedes the planning-oriented draft.
> Project Owner clarification: this is a **simulation pipeline**, not an
> infrastructure planning pipeline. Existing chargers and existing
> EV inventory are inputs, not decision variables. The simulator MUST
> produce a fully-resolved schedule for every active block instance —
> **infeasibility is not an acceptable terminal state**.

## Role

You are an expert EV infrastructure modeller, transport data engineer, and
Python developer working on the **Nature EV** project. You are implementing
**Milestone 1 (M1)** of a multi-milestone refactor of the bus annual
simulation pipeline.

Your work must be **operationally interpretable**, **reproducible**, and
**honest about data quality**. Every depot, vehicle, and charging-station
record must carry source + confidence metadata. Where the existing data does
not support a high-confidence answer, the output must say so rather than
producing misleadingly precise results — but the simulation itself must
still complete for every block instance.

---

## Context Updates From Project Owner

1. **Use case is simulation, not planning.** The downstream consumer is a
   grid-load model that asks "given today's bus operations, what charging
   load lands on which station / LSOA / 15-minute slot?" — not "how many
   buses or chargers should we add?"
2. **Charging infrastructure is fixed.** The simulator MUST NOT invent new
   charger sites. The charger universe is the union of:
   - one synthetic depot charger per depot (AC, sized from EV inventory), and
   - real public chargers from `data/UK_OCM_stations_labeled.csv` (27,006 sites).
3. **Vehicle inventory is fixed.** The fleet is the bus rows of
   `data/EV_UK_LSOA_2025_with_energy.csv` (~6,222 buses). Synthetic overflow
   vehicles may be drawn from the same distribution if the real pool is
   exhausted, but every spawned vehicle must be flagged with
   `vehicle_provenance ∈ {ev_uk_lsoa_real, synthetic_overflow}`.
4. **No chain may end the day infeasible.** The simulator runs a deterministic
   resolution cascade (L0 → L4) until every chain is feasible. If L4 cannot
   resolve a chain, that is a **hard error** — investigate, do not silently
   carry forward.
5. **TransXChange XML is available** at
   `../Data/EV_behavior/Bus_Data/<Operator Name>_<id>/*.xml`. Walk
   recursively across operator subdirectories. Real path is **NOT**
   `bus_data/transxchange/`.
6. **LSOA assignment correctness must be validated** before depot/terminal
   coordinates are stamped with LSOA codes. Mis-attribution by 1 km moves
   the entire load to the wrong LSOA; the centroid-fallback radius must be
   tightened from 1000 m to 250 m for high-load locations.

---

## M1 Scope — What This Sprint Delivers

### In scope

1. **Depot registry** with explicit confidence tiers (TXC `<Garage>` where
   matchable; virtual operator-centroid depots elsewhere — never via
   stop clustering).
2. **Vehicle inventory** bridged from `EV_UK_LSOA_2025_with_energy.csv`,
   anchored to depots; AC/DC charging powers preserved separately.
3. **Charger registry** unifying depot chargers (synthetic, one per depot)
   and public chargers (from `UK_OCM_stations_labeled.csv`).
4. **Block templates → block instances** via GTFS service-calendar expansion.
5. **Greedy vehicle assignment** based on **time-space feasibility only**
   (no SOC). Synthetic overflow vehicles spawn when the real pool is
   exhausted; every block instance is assigned.
6. **Chain-level SOC walk** as a standalone audit function (NOT a
   simulator rewrite).
7. **Resolution cascade L0 → L4** that mutates the chain (charger
   eligibility, vehicle upgrade, mid-day depot return) until SOC walks
   non-negative.
8. **Vehicle-day event ledger** as a required canonical debugging output.
9. **Reconciliation report** against the existing per-block pipeline.

### Out of scope (deferred)

| Item | Defer to |
|---|---|
| LSOA / station 15-min load aggregation | M2 |
| Explicit `charging_windows.parquet` as a first-class object | M2 |
| Temperature / winter consumption multipliers | M3 |
| Stochastic delay propagation, Monte Carlo, P05/P50/P95 | M4 |
| Min-cost flow vehicle assignment | M5 (only if greedy proves insufficient) |
| Holiday alternative scenarios | Not done. GTFS `calendar_dates.txt` is the sole source of truth; document this assumption explicitly in code. |
| Road-network detour factor / OSRM | Deferred indefinitely. Use haversine × 1.0 in M1. |

---

## Critical Methodological Rules

These rules override any conflicting instructions in older planning documents.

### Rule 1 — `block_id` is NOT a vehicle

`block_id` is a **timetable duty template**. The hierarchy is:

```
BlockTemplate     : block_id (e.g. B100)               -- timetable template
BlockInstance     : (service_date, block_id, seq)      -- one running instance
VehicleAssignment : block_instance_id -> vehicle_id    -- physical assignment
Chain             : ordered block_instance_ids assigned to one vehicle on one date
```

`block_instance_id` MUST include a `seq` suffix because the same `block_id`
can run twice on the same date. Format:
`f"{service_date}_{block_id}_{seq:02d}"`.

`chain_id` is `f"{vehicle_id}_{service_date}_{chain_seq:02d}"`. Default
`chain_seq=00`; reserved for split-shift in M5+.

### Rule 2 — Greedy assignment is TIME-SPACE ONLY

`assign_vehicles_greedy()` MUST NOT take `battery_kwh`,
`consumption_kwh_per_km`, or any SOC-related parameter. The only
spawn / overflow trigger is **time-space infeasibility** (cannot connect
two blocks given deadhead time + min turnaround).

**Synthetic overflow**: when no real vehicle in the depot pool is eligible
for a block instance, spawn a synthetic vehicle drawn from the depot's
existing distribution. Mark `vehicle_provenance="synthetic_overflow"`.
Do not raise. Every block instance must be assigned.

SOC feasibility is a **separate, downstream** concern (Day 4) handled by
the resolution cascade. It does NOT influence Day 2 assignment.

**Rationale**: mixing SOC into greedy creates a chicken-and-egg problem
(charging windows are defined by assignment) and forces two inconsistent
SOC models.

### Rule 3 — Chain SOC walk is required, but does NOT replace the simulator

Implement `chain_soc_walk(events_df, vehicle, charger_eligibility,
initial_soc_kwh) -> events_with_soc_df`. It:

- Takes a chain's full event sequence as a DataFrame.
- Walks SOC from morning depot start through each event in order.
- Subtracts `distance_km * consumption_kwh_per_km` for movement events.
- Adds energy at events whose location has an eligible charger, capped by
  `min(vehicle.ac_charge_kw_max, charger.power_kw)` for depot events and
  `min(vehicle.dc_charge_kw_max, charger.power_kw)` for opportunity events.
- **Does NOT clamp SOC at zero** — let it go negative so true shortfall is
  measurable (the resolution cascade reads this signal).
- Returns the events with `soc_start_kwh`, `soc_end_kwh`, `charge_kwh_added`
  columns added.

This function is ~80–120 lines. It does **NOT** replace `simulate_single_ev`.

### Rule 4 — Resolution cascade: simulator MUST NOT return `infeasible`

For each chain, run levels in order; stop at the first that produces
non-negative SOC throughout:

| Level | Modification | Rationale |
|---|---|---|
| **L0** | Initial vehicle, depot-only charging | Cheapest path |
| **L1** | + opportunity charging at any real public charger within 200 m of an event location with `duration_min ≥ 10` and station `TotalCapacity_kW ≥ 50` | Use what's already there |
| **L2** | + upgrade vehicle to the highest-`battery_kwh` member of the same depot's pool not already assigned to another chain on that date | Existing fleet, no new vehicle |
| **L3** | + insert one mid-day depot return between the two blocks straddling the first SOC floor crossing; depot return adds 2× depot-deadhead and ≥ 30 min depot charging time | Always feasible because the depot has unlimited dwell capacity |
| **L4** | L3 + L2 combined | Last resort |
| **L5** | Hard error | Should not occur under realistic UK bus parameters; investigate immediately |

Record `resolution_level: int` (0–4) plus the cumulative modifications on
every chain. Re-running with identical inputs MUST yield identical
resolution levels (deterministic tie-break).

If `resolution_level == 5` is ever produced, the script MUST raise
`SimulationError`, write the offending chain's full event ledger to
`outputs/diagnostics/unresolvable_chains/`, and abort the run.

### Rule 4a — Charger eligibility is a SPATIAL JOIN against fixed registries

The simulator does NOT invent chargers. The eligibility check at every
event is:

```
event.lat, event.lon
    --> nearest_charger(charger_registry, radius_m=200)
    --> if found and station.TotalCapacity_kW >= 50: eligible
```

The charger registry has two strata:

- `depot_chargers`: one per depot, `lat/lon = depot.lat/lon`,
  `power_kw = depot_ac_charge_kw` (sized from EV inventory’s AC_Power_kW
  median for that depot's vehicles), `station_id = f"depot_{depot_id}"`,
  always eligible at events of type `depot_parking`.
- `public_chargers`: rows from `data/UK_OCM_stations_labeled.csv`,
  filtered to `TotalCapacity_kW >= 50` and `Bands` matching one of
  {`Fast site`, `Rapid`, `Ultra-rapid`}; eligible at events of type
  `terminal_layover` and `mid_block_layover` only when the spatial query
  succeeds.

### Rule 4b — DO NOT cluster stops to find depots

The DBSCAN-over-stops approach is **rejected**. A garage serves many
terminals; a terminal serves many garages. Clustering terminals does not
recover garages. **M1 uses TXC `<Garage>` where present and a virtual
operator-centroid depot (geometric median of the operator's first-stop
coordinates, via Weiszfeld) elsewhere. No stop clustering.**

### Rule 5 — LSOA assignment must be validated, not assumed

Before stamping LSOA codes on depot/terminal/charger coordinates:

1. Run `tests/validation/test_lsoa_known_anchors.py` on at least 20
   ground-truth UK addresses (provide the anchor list under
   `tests/validation/lsoa_anchors.csv` — see Day 0 below).
2. Confirm all polygon datasets are in WGS84 EPSG:4326 (the existing
   `mobility/core/spatial.py` already validates this — assert it does
   before reuse).
3. Confirm the LSOA polygon vintage matches the LSOA vintage in
   `EV_UK_LSOA_2025_with_energy.csv` and `UK_OCM_stations_labeled.csv`
   (LSOA 2021 for E&W; DZ 2022 for Scotland; DZ 2021 for NI).
4. For coordinates that fall outside any polygon, the centroid-fallback
   distance threshold MUST be tightened from 1000 m to **250 m** for
   depots, terminals, and chargers. Beyond 250 m,
   `manual_review_flag=True` and `lsoa_code=NaN`.

If validation fails, fix the loader. Do not paper over with high
fallback distances.

### Rule 6 — Holiday handling

GTFS `calendar.txt` + `calendar_dates.txt` is the sole source of truth.
Code MUST contain this comment near the calendar-expansion call:

```python
# ASSUMPTION: GTFS calendar_dates.txt correctly reflects bank-holiday
# service. This holds for UK BODS feeds. For non-UK data an explicit
# holiday scenario module would be required. See M4 backlog.
```

### Rule 7 — Honour the existing AGENTS.md constraints

- No `geopandas / pyproj / pyshp / shapely / fiona / holidays` in
  `Modelling` runtime dependencies.
- No `mobility.coach.*` or `mobility.cars.*` imports from `mobility.bus`.
- No `pip install`, `df.to_csv`, or `df.to_parquet` inside notebooks.
- No `--no-verify` to bypass hooks.
- All randomness must be deterministically seeded.
- Schema additions are append-only with defaults; never remove columns.
- Do NOT modify `mobility/core/simulator._soc_walk`.

---

## Day-by-Day Plan

### Day 0 — Pre-flight (≤ 4 h)

These prerequisites unblock Day 1 and must complete before any module is
written.

#### 0.1 Shared TXC helpers refactor

`mobility/coach/txc_parser.py` already implements `TXC_NS`,
`_findtext`, `parse_clock_to_seconds`, `_local_name`. Cross-package import
is forbidden. Extract these helpers into `mobility/core/txc_parser.py`
(thin module, ~60 lines), then make `mobility/coach/txc_parser.py`
re-export from core. Bus parser will import only from
`mobility.core.txc_parser`.

#### 0.2 ONS Postcode Directory loader

Implement `mobility/core/postcode_geocoder.py`:

```python
def load_onspd(path: Path) -> dict[str, tuple[float, float]]: ...
def geocode_postcode(postcode: str, index: dict) -> tuple[float, float] | None: ...
```

Source data: ONS Postcode Directory CSV from
`https://geoportal.statistics.gov.uk/`, placed at
`../Data/Loads/ONSPD_latest.csv`. Loader normalises postcodes (strip
whitespace, uppercase, no internal space variants) before lookup.

If ONSPD is not available at runtime, the loader must return an empty
dict and emit a `RuntimeWarning`. TXC garages without coordinates and
without an ONSPD-resolvable postcode fall through to L4 — **never**
silently dropped.

3 unit tests on known postcodes (e.g. `SW1A 1AA`, `EH1 1YZ`, `BT1 5GS`).

#### 0.3 LSOA validation anchor set

Create `tests/validation/lsoa_anchors.csv` with at least 20 rows:
`name, lat, lon, expected_lsoa_or_dz_code, source`. Use major UK bus
stations and named TfL garages with public coordinates. Pin the test:
`tests/validation/test_lsoa_known_anchors.py` — fail the run if any
mismatch.

---

### Day 1 — TransXChange Parser, Depot & Charger Registries, Vehicle Inventory

**New modules**: `mobility/bus/txc_parser.py`,
`mobility/bus/depot_registry.py`, `mobility/bus/charger_registry.py`,
`mobility/bus/vehicle_inventory.py`.

#### 1.1 TransXChange parser

```python
DEFAULT_TXC_DIR = (
    Path(__file__).resolve().parents[2].parent
    / "Data" / "EV_behavior" / "Bus_Data"
)


def parse_txc_garages(txc_dir: Path = DEFAULT_TXC_DIR) -> pd.DataFrame:
    """Recursively walk operator subdirectories under ``txc_dir`` and
    parse every TransXChange XML.

    Extract from each file:
        - <Garages>/<Garage>: garage code, garage name, postcode if present.
        - <Operators>/<Operator>: NOC and operator name.
        - <DeadRun> / <PositioningLink>: linked stop_id and garage ref
          (used in M2 for charging-window association).

    Returns a DataFrame with columns:
        garage_id, garage_code, operator_noc, operator_name, postcode,
        approx_lat, approx_lon, source_file, parse_warnings.

    If postcode is present but coordinates are not, geocode via
    mobility.core.postcode_geocoder. If geocoding fails, leave coordinates
    NaN and emit a warning — caller decides fallback.

    Returns an EMPTY DataFrame (correct schema) if txc_dir does not exist,
    contains no XML, or every parse fails.
    """
```

**Validation requirement**: after parsing, log
`n_garages_parsed`, `n_with_coords`, `n_without_coords`. If
`n_with_coords / max(1, n_garages_parsed) < 0.5`, emit
`DataQualityWarning` — typically a sign of a broken postcode loader.

**Operator code reality check**: TXC `OperatorCode` carries the real NOC
(e.g. `ANEA`, `SLBS`) even when GTFS `agency_id` is anonymised. Match by
operator name when available; otherwise by NOC against any
`agency_noc` column in GTFS `agency.txt`. **Garages whose operator
cannot be matched to any GTFS `agency_id` MUST be dropped from the
registry and logged to `outputs/diagnostics/depot_registry_dropped.parquet`**
— retaining unmatched garages clutters downstream joins to no benefit.

#### 1.2 Depot registry builder

```python
def build_depot_registry(
    blocks_df: pd.DataFrame,
    agency_df: pd.DataFrame,           # GTFS agency.txt
    stops_df: pd.DataFrame,
    lsoa_index: dict,                  # mobility.core.spatial output
    txc_garages_df: pd.DataFrame | None = None,
    external_depots: pd.DataFrame | None = None,  # placeholder for L2/L3
) -> pd.DataFrame:
    """Build the depot registry by combining tiered sources.

    Algorithm:
        1. Initialise empty registry.
        2. If ``txc_garages_df`` is non-empty: add L1 entries.
              confidence = 'high' if coords present, 'medium' if address-only.
        3. If ``external_depots`` is non-empty: merge L2/L3 entries.
              On conflict with L1, prefer L1 unless external explicitly
              carries a higher confidence (e.g. a curated TfL list);
              record override_reason.
        4. For every agency_id NOT covered by L1/L2/L3, create one virtual
           L4 depot:
              depot_id = f"virtual_{agency_id}"
              lat, lon = geometric median (Weiszfeld algorithm) of the
                        agency's blocks' first_stop coordinates.
                        Use scipy.optimize.minimize as a fallback, and pin
                        random_state for determinism.
              confidence = 'low'
              depot_source = 'virtual_operator_centroid'
        5. Assign LSOA via point-in-polygon on ``lsoa_index``. Centroid
           fallback only when outside any polygon, with max distance
           250 m. Beyond that: lsoa_code=NaN, manual_review_flag=True.

    Returns columns:
        depot_id, agency_id, operator_noc, lat, lon, lsoa_code, lsoa_method,
        depot_source, depot_confidence, depot_assignment_method,
        override_reason, manual_review_flag, n_candidate_vehicles
    """
```

Function MUST handle `txc_garages_df=None` (no TXC files) without
crashing — fall through to L4 for every agency.

#### 1.3 Charger registry builder

```python
def build_charger_registry(
    depot_registry: pd.DataFrame,
    ocm_csv_path: Path = DEFAULT_OCM_PATH,
    min_power_kw: float = 50.0,
    allowed_bands: tuple[str, ...] = ("Fast site", "Rapid", "Ultra-rapid"),
) -> pd.DataFrame:
    """Combine synthetic depot chargers and filtered public chargers.

    Returns columns:
        station_id, station_kind, lat, lon, lsoa_code, power_kw,
        attached_depot_id, source.

    station_kind in {'depot', 'public'}.
    For depot rows:
        station_id = f"depot_{depot_id}"
        power_kw   = median(AC_Power_kW) of vehicles attached to that
                     depot in vehicles.parquet (computed in Day 1.4).
                     Fallback to 100 kW if no attached vehicles.
    For public rows:
        station_id = OCM StationID (string-cast for safety).
        power_kw   = TotalCapacity_kW.
        Filter rows where TotalCapacity_kW < min_power_kw or Bands not in
        allowed_bands; log dropped count.
    """
```

#### 1.4 Vehicle inventory bridging

```python
def bridge_ev_lsoa_to_fleet(
    ev_lsoa_df: pd.DataFrame,
    depot_registry: pd.DataFrame,
    nearest_depot_max_km: float = 30.0,
) -> pd.DataFrame:
    """Bridge EV_UK_LSOA bus rows to depots.

    Filter to ``vehicle_subtype in {'bus', 'minibus'}``.
    For each bus:
        1. Candidate depots within ``nearest_depot_max_km`` of the bus's
           LSOA centroid.
        2. Prefer same-operator depots (when an operator hint exists);
           otherwise skip operator filter.
        3. Assign the nearest qualifying depot.
        4. If none in radius: depot_id=NaN, depot_match_method='unmatched'.
           These vehicles are EXCLUDED from Day 2 assignment but retained
           in vehicles.parquet for audit.

    Output columns:
        vehicle_id, depot_id, source_lsoa, battery_kwh,
        consumption_kwh_per_km, ac_charge_kw_max, dc_charge_kw_max,
        usable_soc_min, usable_soc_max, depot_match_distance_km,
        depot_match_method, operator_match (bool), vehicle_provenance,
        source_row_id, source_csv_md5.

    Constants:
        usable_soc_min = 0.10
        usable_soc_max = 0.95
    Sourced from existing mobility/bus/vehicle_sampling.py defaults; M3
    may diversify per subtype.
    """
```

**Edge case — unit confusion**: `EV_UK_LSOA` stores efficiency as
`efficiency_wh_per_km`. Convert to kWh/km via `/ 1000.0`, NOT via
`energy_kWh_per_100km / 100` (the latter rounds and loses precision).
A silent inversion would cause Day 4 SOC walks to be wildly wrong while
looking plausible. Add a sanity test: all consumption values must fall
within `[0.7, 3.0]` kWh/km for UK buses. The known dataset minimum
(BYD ENVIRO at 0.81 kWh/km) sits on the lower edge — keep the floor at
0.7 to allow it without flagging.

**Charging power split**: store `ac_charge_kw_max = AC_Power_kW` and
`dc_charge_kw_max = DC_Power_kW` as separate columns. Depot charging
uses AC, opportunity charging uses DC. Collapsing them would overstate
depot charge speed by ~2×.

#### 1.5 Day 1 unit tests

```
tests/test_txc_parser.py
    - test_empty_dir_returns_empty_df
    - test_garage_extraction_from_sample_xml
    - test_geocoding_warning_when_below_50pct
    - test_unmatched_operator_dropped_and_logged

tests/test_depot_registry.py
    - test_l4_only_when_no_txc
    - test_l1_overrides_l4
    - test_geometric_median_robust_to_outliers
    - test_lsoa_assigned_for_each_depot
    - test_manual_review_flag_when_outside_polygon
    - test_no_geopandas_imported (introspection)

tests/test_charger_registry.py
    - test_filters_low_power_public_chargers
    - test_synthetic_depot_charger_per_depot
    - test_ocm_lsoa_preserved

tests/test_vehicle_inventory.py
    - test_unmatched_vehicles_flagged
    - test_consumption_unit_sanity
    - test_no_silent_inversion (explicit unit interpretation)
    - test_ac_dc_powers_distinct_columns
```

---

### Day 2 — Block Instance Expansion + Greedy Vehicle Assignment

**Modules**: `mobility/bus/block_instances.py`,
`mobility/bus/vehicle_assignment.py`.

#### 2.1 Block instance expansion

```python
def expand_block_instances(
    blocks_df: pd.DataFrame,
    calendar_df: pd.DataFrame,
    calendar_dates_df: pd.DataFrame,
    date_range: tuple[date, date],
) -> pd.DataFrame:
    """Expand block templates into (service_date, block_id, seq) instances.

    Determine active dates from calendar plus calendar_dates
    (exception_type=1 adds, exception_type=2 removes).

    For each active date, emit one BlockInstance with
        block_instance_id = f"{service_date}_{block_id}_{seq:02d}"
    seq=00 by default; on (date, block_id) collision, increment and warn.

    GTFS time handling: '25:30:00' is next-day 01:30. Compute
        actual_datetime = service_date + timedelta(seconds=parsed_seconds)
    where parsed_seconds may exceed 86400.
    """
```

#### 2.2 Greedy vehicle assignment (TIME-SPACE ONLY)

```python
def assign_vehicles_greedy(
    block_instances: pd.DataFrame,
    vehicles: pd.DataFrame,
    depot_registry: pd.DataFrame,
    deadhead_speed_kmh: float = 30.0,
    min_turnaround_min: float = 10.0,
    rng_seed: int = 20260508,
) -> pd.DataFrame:
    """Greedy earliest-available assignment. NO SOC PARAMETERS.

    Algorithm (per service_date, per depot):
        1. Sort block instances by start_time ascending.
        2. Pool = vehicles assigned to that depot, each carrying:
              (vehicle_id, current_loc, free_after_time).
           Initialise: all at home depot, free from 00:00.
        3. For each block_instance in order:
              a. Eligible vehicles satisfy:
                     free_after_time
                     + haversine(current_loc, block.start_loc) / speed
                     + min_turnaround
                     <= block.start_time
              b. Tie-break:
                     1) MIN deadhead distance to start_loc
                     2) MIN vehicle_id (string sort)
              c. If no eligible vehicle: spawn a synthetic overflow vehicle
                 by sampling spec from the depot's existing distribution
                 (deterministic via rng_seed + depot_id + service_date).
                 vehicle_provenance = 'synthetic_overflow'.
                 Append to vehicles registry in-memory.
        4. Update assigned vehicle's state.

    NO depot-return events are constructed here — the event ledger
    (Day 3) is the sole place those are created. Day 2 only records
    inter-block connection_deadhead_km / connection_deadhead_min on each
    assignment row.

    Output: vehicle_assignments.parquet with columns:
        service_date, block_id, block_instance_id, vehicle_id, chain_id,
        prev_block_instance_id, next_block_instance_id,
        connection_deadhead_km, connection_deadhead_min,
        assignment_status ('assigned' for every row in M1),
        assignment_method ('greedy'), tiebreak_reason,
        vehicle_provenance
    """
```

`assignment_method` is recorded per row to allow future swap-in of better
solvers without schema changes.

#### 2.3 Day 2 unit tests

```
tests/test_block_instances.py
    - test_gtfs_time_over_24h
    - test_calendar_dates_exception_1_adds
    - test_calendar_dates_exception_2_removes
    - test_duplicate_block_same_date_gets_seq

tests/test_vehicle_assignment.py
    - test_greedy_no_soc_params (signature introspection)
    - test_tiebreak_minimum_deadhead
    - test_tiebreak_secondary_lowest_id
    - test_overflow_spawn_when_pool_exhausted
    - test_overflow_provenance_tagged
    - test_no_uncovered_status_in_output
    - test_no_event_construction_in_assignment_output
```

---

### Day 3 — Vehicle-Day Event Ledger + Chain SOC Walk

**Modules**: `mobility/bus/event_ledger.py`, `mobility/bus/chain_soc.py`.

#### 3.1 Event ledger

```python
def build_event_ledger(
    vehicle_assignments: pd.DataFrame,
    block_instances: pd.DataFrame,
    depot_registry: pd.DataFrame,
    stops_df: pd.DataFrame,
) -> pd.DataFrame:
    """Reconstruct the canonical per-vehicle-per-day event sequence.

    For each chain, emit events in time order:
        depot_parking      (00:00 -> first deadhead start)
        depot_deadhead     (depot -> first block start_loc)
        passenger_block    (one event per assigned block_instance)
        inter_block_deadhead  (between consecutive blocks if locations differ)
        terminal_layover   (at block boundaries with shared end/start stop)
        return_deadhead    (last block end_loc -> depot)
        depot_parking      (return time -> 23:59)

    Required columns (M1 minimum, ordered):
        vehicle_id, chain_id, service_date, event_seq, event_type,
        block_instance_id, start_time, end_time, duration_min,
        start_lat, start_lon, end_lat, end_lon,
        distance_km, distance_method ('haversine_x_1.0' in M1),
        energy_kwh_proxy

    energy_kwh_proxy = distance_km * vehicle.consumption_kwh_per_km
        (movement events; 0 for parking and layover events).

    Status: REQUIRED. This is the canonical debugging output.
    """
```

#### 3.2 Chain SOC walk

```python
def chain_soc_walk(
    chain_events: pd.DataFrame,
    vehicle: pd.Series,
    charger_eligibility: pd.DataFrame,
    initial_soc_kwh: float | None = None,
) -> pd.DataFrame:
    """Walk SOC through one chain's events given a charger-eligibility table.

    ``charger_eligibility`` is a per-event-row DataFrame with columns
    ``event_seq, eligible (bool), power_kw, station_id`` produced by the
    resolution cascade for this iteration. The SOC walk does NOT decide
    eligibility itself — that is the resolution layer's job.

    initial_soc_kwh defaults to vehicle.battery_kwh * vehicle.usable_soc_max.

    For each event:
        movement (passenger_block, *_deadhead):
            soc_end = soc_start - energy_kwh_proxy
        parking / layover with eligible == True:
            charge = min(power_kw, soc_cap) * (duration_h)
            where soc_cap_power is vehicle.ac_charge_kw_max for depot events
                                  vehicle.dc_charge_kw_max otherwise
            soc_end = min(soc_start + charge,
                          battery_kwh * usable_soc_max)
        otherwise: soc_end = soc_start

    DO NOT clamp SOC at zero. Negative SOC is the signal the resolution
    cascade reads.

    Adds columns: soc_start_kwh, soc_end_kwh, charge_kwh_added,
                  station_id (where charging occurred).
    """
```

#### 3.3 Day 3 unit tests

```
tests/test_event_ledger.py
    - test_chain_starts_and_ends_at_depot
    - test_event_seq_is_strictly_increasing
    - test_no_time_overlaps_within_chain
    - test_passenger_blocks_match_assignments_one_to_one
    - test_distance_method_constant_in_m1

tests/test_chain_soc.py
    - test_soc_does_not_clamp_at_zero
    - test_full_charge_at_depot_overnight
    - test_ac_used_for_depot_dc_for_opportunity
    - test_charging_excluded_when_eligibility_false
    - test_known_shortfall_computed_correctly
```

---

### Day 4 — Resolution Cascade + Diagnostics

**Module**: `mobility/bus/chain_resolver.py`.

#### 4.1 Charger spatial query

```python
def query_charger_eligibility(
    chain_events: pd.DataFrame,
    charger_registry: pd.DataFrame,
    radius_m: float = 200.0,
    min_dwell_min: float = 10.0,
    levels_enabled: set[str] = frozenset({"L1"}),
) -> pd.DataFrame:
    """Per-event eligibility lookup against the fixed charger registry.

    For each event:
        - depot_parking events: always eligible at the home depot's
          synthetic charger.
        - terminal_layover or mid_block_layover events with
          duration >= min_dwell_min: eligible if any public charger from
          ``charger_registry`` falls within ``radius_m`` of (lat, lon).
          When multiple, pick the highest-power station; deterministic
          tie-break by ``station_id`` ascending.
        - Otherwise: ineligible.

    levels_enabled controls whether L1 (opportunity at public chargers)
    is on. L0 passes ``levels_enabled = frozenset()``; L1+ pass
    ``frozenset({'L1'})``.

    Returns: per-event-row DataFrame with eligibility columns.
    """
```

#### 4.2 Resolution cascade

```python
def resolve_chain(
    chain_events: pd.DataFrame,
    chain_vehicle: pd.Series,
    depot_pool: pd.DataFrame,
    charger_registry: pd.DataFrame,
    depot_id: str,
    soc_floor_kwh: float = 0.0,
) -> dict:
    """Run levels L0 -> L4 in order, return at the first feasible result.

    Returns:
        {
          'resolution_level': int,                   # 0..4
          'final_vehicle_id': str,
          'final_chain_events': pd.DataFrame,        # mutated for L3/L4
          'modifications': list[str],                 # ['vehicle_upgrade',
                                                      #  'mid_day_return',
                                                      #  'opportunity_charging']
          'min_soc_kwh_per_level': dict[int, float], # diagnostic
          'charge_kwh_per_level': dict[int, float],
          'station_ids_used': set[str],
        }

    L0: vanilla, depot-only.
    L1: depot + opportunity charging at public chargers in radius.
    L2: L1 + upgrade vehicle to highest-battery member of depot_pool not
        already used by another chain on this date. If pool has no spare
        vehicle with strictly larger battery, skip to L3.
    L3: L1 with original vehicle + insert one mid-day depot return:
        - locate event_seq where soc first goes below soc_floor_kwh
        - identify the inter-block layover preceding that event_seq
        - if the layover is shorter than (2 x depot_deadhead_min + 30 min),
          escalate to L4 directly
        - otherwise insert: return_deadhead -> depot_parking (charge) ->
          out_deadhead, replacing the original layover; recompute SOC
    L4: L3 + L2 (mid-day return AND vehicle upgrade).
    L5: raise SimulationError; write the chain ledger to
        outputs/diagnostics/unresolvable_chains/<chain_id>.parquet.
    """
```

**Determinism is non-negotiable**: vehicle-upgrade selection,
mid-day-return insertion point, and station selection MUST be stable
across re-runs. The function MUST be unit-tested for repeatability.

#### 4.3 Resolution summary aggregator

```python
def build_resolution_summary(
    resolutions: list[dict],
    vehicle_assignments: pd.DataFrame,
) -> pd.DataFrame:
    """Per-chain resolution summary.

    Output: resolution_summary.parquet with columns:
        service_date, chain_id, depot_id, original_vehicle_id,
        final_vehicle_id, vehicle_upgraded (bool),
        resolution_level (int 0..4), modifications_str,
        min_soc_kwh_l0, min_soc_kwh_l1, min_soc_kwh_l2,
        min_soc_kwh_l3, min_soc_kwh_l4,
        opportunity_charge_kwh, mid_day_return_kwh,
        n_stations_used, station_ids_used_csv,
        had_synthetic_overflow_vehicle (bool)
    """
```

This output is **diagnostic, not normative**. It does NOT report
"infeasibility" — every row has a finite `resolution_level`. The
distribution of `resolution_level` is the headline diagnostic ("how much
work did the cascade have to do?").

#### 4.4 Day 4 unit tests

```
tests/test_chain_resolver.py
    - test_l0_when_chain_already_feasible
    - test_l1_finds_public_charger_within_radius
    - test_l1_skipped_when_no_charger_in_radius
    - test_l2_picks_highest_battery_spare
    - test_l2_skipped_when_no_spare_with_larger_battery
    - test_l3_inserts_mid_day_return_at_first_floor_event
    - test_l3_escalates_to_l4_when_layover_too_short
    - test_l4_combines_upgrade_and_return
    - test_l5_raises_simulation_error_and_writes_diagnostic
    - test_resolution_is_deterministic_under_rerun
    - test_no_invented_chargers (introspection)

tests/test_resolution_summary.py
    - test_every_chain_has_finite_resolution_level
    - test_modifications_string_format_stable
    - test_synthetic_overflow_flag_propagates
```

---

### Day 5 — End-to-End Integration + Reconciliation

#### 5.1 Pipeline runner

```
scripts/run_bus_pipeline.py
    Stage 1: parse TXC (if present) + build depot_registry
    Stage 2: bridge ev_lsoa -> vehicles
    Stage 3: build charger_registry
    Stage 4: expand block_instances
    Stage 5: greedy assignment (time-space only, may spawn overflow)
    Stage 6: build event_ledger
    Stage 7: chain_resolver per chain (cascade L0 -> L4)
    Stage 8: write resolution_summary
    Stage 9: reconciliation report (see 5.2) -> m1_reconciliation_report.md
```

The script writes `m1_reconciliation_report.md` automatically via a
template; no manual editing.

#### 5.2 Reconciliation against the existing per-block pipeline

The old `bus_annual_per_block.parquet` does NOT depot-anchor blocks: it
has only intra-block deadhead km. The new chain-mode output **adds**
depot-to-first-stop and last-stop-to-depot legs that did not exist
before. New deadhead km will therefore be **higher**, not lower.

Reconciliation criteria:

| Check | Threshold | Rationale |
|---|---:|---|
| Passenger trip total km | exact match | Block contents are identical between modes |
| Passenger trip total km per agency_id | exact match | Detects per-operator processing bugs |
| Intra-chain deadhead km (new vs old) | within ±5% | Same logical content; tiny differences from chain stitching |
| Depot-anchored deadhead km (new only) | reported, no threshold | New attribute; sanity-check against typical UK garage-to-route-end distances (median 3–10 km) |
| Total energy (new vs old) | within +0% to +30% | New adds depot-anchored deadhead and possibly mid-day returns |
| Distinct chain count per (service_date, depot_id) | ≤ vehicles in pool + n_synthetic_overflow | Else assignment bug |
| `resolution_level` distribution | L0+L1 share ≥ 70% expected | Lower share suggests SOC walk over-discharging or charger filter too strict |
| Synthetic overflow rate | ≤ 5% expected | Higher suggests EV_UK_LSOA-to-depot bridging too tight; widen radius or reduce filter |
| Hard L5 errors | exactly 0 | If non-zero: stop and investigate |
| Distinct LSOAs receiving charge | sanity-checked vs. depot count | Each depot LSOA + a few public-charger LSOAs |

If passenger trip km don't match exactly, **stop and find the bug**
before continuing.

The reconciliation script splits the new pipeline's deadhead km into
`deadhead_intra_chain_km` (comparable to old) and
`deadhead_depot_anchored_km` (new attribute) so the comparison is
meaningful.

#### 5.3 Notebook

Update `notebooks/01_single_bus_simulation.ipynb` with a Stage J
section that loads the new outputs and shows:

- Depot count by confidence tier (bar chart).
- Fleet size by depot, with overflow share stacked (bar chart).
- Daily chain count by depot (line chart).
- Resolution-level distribution (stacked bar by depot or by agency).
- Top-10 public-charger stations by total charge_kwh in a sample week.
- A small map of depots with confidence as marker shape (high=circle,
  medium=square, low=triangle); colour by `n_candidate_vehicles`. **No
  high-resolution heatmap for L4-confidence depots** — caption must read:
  "Virtual operator-centroid depots are illustrative only; LSOA
  attribution for these depots is low-confidence."

Notebook must NOT write parquet/csv — read pre-built outputs only.

#### 5.4 Integration test

```
tests/test_pipeline_integration.py
    - test_full_pipeline_on_one_week_runs_without_error
    - test_passenger_km_reconcile_exactly
    - test_no_chain_uses_more_than_one_depot
    - test_every_block_instance_assigned (no 'uncovered')
    - test_every_chain_resolved (no L5)
    - test_resolution_levels_are_bounded_0_to_4
```

---

## Required Outputs (M1)

| File | Producer | Required |
|---|---|---|
| `depot_registry.parquet` | Day 1 | yes |
| `charger_registry.parquet` | Day 1 | yes |
| `vehicles.parquet` | Day 1 | yes |
| `block_instances.parquet` | Day 2 | yes |
| `vehicle_assignments.parquet` | Day 2 | yes |
| `vehicle_day_events.parquet` | Day 3 | yes (NOT optional) |
| `resolution_summary.parquet` | Day 4 | yes |
| `m1_reconciliation_report.md` | Day 5 | yes |
| `outputs/diagnostics/depot_registry_dropped.parquet` | Day 1 | only when applicable |
| `outputs/diagnostics/unresolvable_chains/*.parquet` | Day 4 | only on L5 (which must be 0) |

Deferred to M2: `charging_windows.parquet`, `load_by_lsoa_15min.parquet`,
`load_by_station_15min.parquet`, `fleet_load_15min.parquet`.

---

## Acceptance Criteria

The M1 implementation is complete when all of the following hold:

1. `block_id == vehicle_id` does not appear anywhere in the codebase.
2. Every active `(service_date, block_id, seq)` is in
   `block_instances.parquet`.
3. Every block instance is assigned (`assignment_status='assigned'` for
   every row); `'uncovered'` does not appear.
4. Synthetic overflow vehicles (when present) are tagged with
   `vehicle_provenance='synthetic_overflow'` and are < 5% of total.
5. Every chain begins and ends at its vehicle's home depot in the event
   ledger.
6. Inter-block deadhead km, time, and energy proxy are recorded for every
   connection; depot-anchored deadhead is recorded as separate columns.
7. `assign_vehicles_greedy` has no SOC-related parameters (verified by
   introspection test).
8. `chain_soc_walk` does not clamp SOC at zero (verified by unit test).
9. `chain_soc_walk` uses AC power for depot events and DC power for
   opportunity events.
10. Every chain has a finite `resolution_level ∈ {0, 1, 2, 3, 4}`. Zero
    chains reach L5.
11. The cascade is deterministic across re-runs.
12. Depot records carry `depot_source`, `depot_confidence`,
    `depot_assignment_method`. TXC L1 depots are used where present;
    virtual L4 fills the rest. No stop-clustering depots exist.
13. Charger registry is the union of synthetic depot chargers and
    filtered OCM public chargers (`TotalCapacity_kW >= 50` and
    `Bands ∈ {Fast site, Rapid, Ultra-rapid}`). The simulator does not
    create chargers anywhere else.
14. LSOA assignment passes `tests/validation/test_lsoa_known_anchors.py`
    on the 20-anchor reference set; centroid fallback radius is 250 m
    for high-load locations.
15. `vehicle_day_events.parquet` covers all assigned chains.
16. Reconciliation: passenger km exact match per agency; intra-chain
    deadhead within ±5%; total energy +0% to +30%; resolution_level
    L0+L1 share ≥ 70%; synthetic overflow share ≤ 5%; zero L5.
17. Notebook visualisations honour `depot_confidence`: no precise
    heatmaps for L4 depots.
18. All unit tests pass; integration test passes on one full week of
    data.
19. No `geopandas / pyproj / shapely / fiona / holidays` import in
    `mobility/*` (introspection test).
20. No `mobility.coach.*` or `mobility.cars.*` import in `mobility.bus`
    (introspection test).

---

## Reference: What is NOT in M1 (do not implement)

- `charging_windows.parquet` as a first-class object (M2).
- LSOA / station 15-min aggregation (M2).
- Temperature / winter consumption multipliers (M3).
- Stochastic delay propagation (M4).
- Monte Carlo and percentile bands (M4).
- Min-cost flow / matching solvers (M5).
- Holiday alternative scenarios (not planned; GTFS calendar is the
  source of truth).
- OSRM / road-network detour factors (not planned in current roadmap).
- New charger sites of any kind (not planned at any milestone — the
  charger universe is fixed by `UK_OCM_stations_labeled.csv` and one
  synthetic charger per depot).
- Fleet sizing recommendations or `vehicle_requirement_summary` (this
  pipeline does not advise on fleet composition — it consumes the
  EV inventory as given).

If you find yourself building any of the above, stop and confirm scope.

---

## Non-Negotiable Reminders

- **Existing infrastructure is the universe.** Never invent a charger
  site or relocate one. The simulator consumes the depot registry +
  OCM CSV as fixed inputs.
- **Infeasibility is not an output.** The cascade L0 → L4 must always
  resolve. L5 is a hard error to investigate, not a status to report.
- **Confidence over precision.** Low-confidence depots and L4 depots
  must be visibly labelled in every output that reaches a human reader.
- **No silent SOC clamping.** Negative SOC values during cascade
  iteration are valid — they are how the resolver detects shortfall.
- **No depot-from-stop clustering.** Use TXC `<Garage>` or virtual
  operator-centroid (geometric median).
- **Greedy is time-space only.** SOC enters the cascade, not the
  assignment.
- **Reconciliation passes by passenger km, not by total energy.** Total
  energy is expected to rise by 0–30% versus the old per-block pipeline.
- **Validate LSOA assignment** before depending on it for high-load
  locations (depots, terminals, public chargers).
- **AGENTS.md constraints are non-negotiable.** No `geopandas`, no
  cross-package imports, no notebook IO, no `--no-verify`.
