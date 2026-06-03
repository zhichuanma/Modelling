# EV Bus Annual Depot Simulation Refactor Plan v2

> Scope: revise the vehicle-day assignment and SOC simulation pipeline for the annual EV bus depot-load model.
>
> This v2 plan replaces the earlier idealized proposal. It explicitly accounts for the existing pipeline structure in:
>
> - `annual_vehicle_day_assignment.py`
> - `annual_depot_events.py`
> - `annual_depot_soc.py`
> - `run_bus_annual_depot_load.py`
> - `annual_depot_outputs.py`
>
> Main changes from v1:
>
> 1. Do **not** implement SOC carry-over before resolving wall-clock charging-window ownership.
> 2. Do **not** compute feasibility from a nonexistent `block_energy_kwh`; compute required energy per EV-block pair.
> 3. Do **not** add a parallel SOC parameter system; use `usable_soc_min` / `usable_soc_max` already present on specs.
> 4. Do **not** build feasible pairs with `iterrows` nested loops; use vectorized feasibility and sparse bipartite matching.
> 5. Split the work into two PRs:
>    - PR 1: feasibility-aware assignment under the existing daily-reset SOC mode.
>    - PR 2: SOC carry-over with streaming-loop integration, day-boundary ownership, home depot, idle charging, warm-up, and output-contract updates.

---

## 1. Current model behavior

The current assignment logic is approximately:

```python
n_assign = min(len(specs), len(day_blocks))

block_positions = rng.choice(
    len(day_blocks),
    size=n_assign,
    replace=False,
)

spec_positions = rng.permutation(len(specs))[:n_assign]

for block_pos, spec_pos in zip(block_positions, spec_positions):
    block = day_blocks.iloc[block_pos]
    spec = specs.iloc[spec_pos]
    assignments.append(...)
```

Within each `service_date`:

- blocks are sampled without replacement;
- EV specs are sampled without replacement;
- the sampled blocks and EVs are randomly zipped;
- infeasibility is discovered later during SOC / event simulation.

This creates "fake infeasible" cases:

```text
A block may be technically runnable by some EV in the fleet,
but it is randomly paired with an EV that cannot run it.
```

The proposed assignment refactor should eliminate this avoidable source of infeasibility.

---

## 2. Target assignment semantics

For each `service_date`:

```text
sample representative active blocks
→ compute EV-block feasibility by pair
→ solve one-to-one feasible matching
→ simulate only matched feasible assignments
→ record unmatched sampled blocks with reasons
```

Daily assignment constraints:

```text
each EV bus can be assigned to at most one sampled block per service_date
each sampled block can be assigned to at most one EV bus
only feasible EV-block pairs may be assigned
```

Randomness should be used for representative block sampling and tie-breaking only.

It should **not** be used to decide whether a randomly selected EV is forced onto a block it cannot operate.

---

## 3. Critical design issue before SOC carry-over: wall-clock ownership

### 3.1 Existing event-window behavior

The current vehicle-day event ledger uses a daily window like:

```text
service_date 00:00 → next day 06:00
```

In particular, `depot_parking_overnight` for Day N may cover:

```text
Day N+1 00:00 → Day N+1 06:00
```

Meanwhile Day N+1 also has its own `depot_parking_pre` window:

```text
Day N+1 00:00 → pull-out time
```

This is acceptable while each day is SOC-independent.

It becomes invalid under SOC carry-over, because the same physical EV and the same wall-clock interval can be charged twice:

```text
Day N overnight charging:       Day N+1 00:00–06:00
Day N+1 pre-block charging:     Day N+1 00:00–pull-out
```

That would double-count both:

- SOC gain;
- depot load energy.

### 3.2 Required decision

Before implementing SOC carry-over, define one and only one owner for every wall-clock interval.

A fixed clock boundary (for example 06:00) is **not sufficient**. Early bus duties
commonly pull out at 04:30–05:30 (`deadhead_start = start_dt - to_block_h` can fall
before any fixed boundary), so a fixed-boundary rule merely moves the overlap from
00:00–06:00 onto early pull-outs. Night blocks returning after the boundary have the
symmetric problem.

### 3.3 Recommendation for v2: per-vehicle ledger stitching

Adopt a per-vehicle stitching rule instead of a fixed ownership boundary:

```text
Day N overnight charging window for a vehicle ends at:
    min(default overnight end, start of that vehicle's first Day N+1 event)

Day N+1 does NOT generate a depot_parking_pre event
(midnight → pull-out); that interval is already owned by
Day N's overnight window.
```

Under this rule:

- Consecutive vehicle-day ledgers are stitched per vehicle and never overlap.
- `soc_day_boundary_hour = 6` is retained, but demoted to the *default overnight
  end when the vehicle has no next-day assignment*, not a hard ownership boundary.
- Night blocks ending after the default boundary truncate the following day's
  windows by the same min() rule.
- End-of-day SOC is the SOC at the stitch point.

This is natural in carry-over mode because the chronological day loop can look
ahead to the same vehicle's Day N+1 assignment when closing Day N's overnight
window (or equivalently, truncate Day N's overnight retroactively when Day N+1's
events are generated).

This should be implemented before enabling SOC carry-over.

Until this is done, SOC carry-over must remain disabled.

---

## 4. Critical design issue before SOC carry-over: vehicle home depot

### 4.1 Problem

Under the current random daily assignment, the same `vehicle_spec_id` can be assigned to blocks from different regions or depots on different days.

That is acceptable for independent representative-day sampling.

It is not physically consistent once SOC is carried over across days:

```text
Day N: vehicle charges at Glasgow depot
Day N+1: same vehicle starts at London depot
```

This creates a continuous SOC trajectory attached to a vehicle that teleports geographically.

### 4.2 Required decision

Before SOC carry-over, assign every EV bus a stable home depot or home region.

Candidate approaches:

1. Use a depot field already available on the source EV inventory, if present.
2. Derive a home depot from `source_lsoa` or other location fields.
3. Assign home depot by first matched block in a warm-up / initialization pass.
4. Use agency / operator / region-level home assignment if exact depot is unavailable.

### 4.3 Minimum v2 requirement

For PR 2, each `vehicle_spec_id` must have:

```text
home_depot_id
home_depot_lat
home_depot_lon
```

or an equivalent stable home-location representation.

Matching should include at least one of:

```text
block depot == vehicle home depot
block region compatible with vehicle home region
deadhead distance from home depot within threshold
```

Without this, SOC carry-over should be considered only a semi-physical modeling mode and must be documented as such.

---

## 5. Idle vehicles under SOC carry-over

### 5.1 Problem

If an EV is not assigned to a block on a given day, it still has a physical location and SOC.

Under carry-over, the model must decide what happens to idle EVs.

The v1 idea of:

```python
update_idle_vehicle_soc(...)
```

is not sufficient if it silently increases SOC without producing charging events.

That would create energy outside the depot event ledger and would undermine the interpretation of depot-load energy checks.

### 5.2 Required behavior

For idle vehicles, choose one explicit policy.

Recommended first implementation:

```text
Idle EVs remain at their home depot.
If soc < usable_soc_max, they may charge during the depot-owned idle window.
This charging must generate normal depot charging events and appear in depot_load_15min.
```

Alternative conservative policy:

```text
Idle EVs do not charge.
Their SOC is simply carried forward unchanged.
```

This avoids hidden energy but may understate depot load and overstate future infeasibility.

### 5.3 v2 recommendation

Use explicit idle charging events in PR 2:

```text
idle_vehicle_at_home_depot charging event
```

Minimum event fields:

```text
service_date
vehicle_spec_id
event_type = "idle_home_depot_charging"
depot_id = home_depot_id
event_start_ts
event_end_ts
energy_kwh
soc_start_kwh
soc_end_kwh
```

Do not mutate SOC upward without writing a corresponding energy event.

---

## 6. Feasibility calculation must be pair-specific

### 6.1 `block_energy_kwh` should not be assumed

The block instance table does not provide a universal `block_energy_kwh`.

Energy depends on the EV spec:

```text
required_kwh = distance_km × spec.consumption_kwh_per_km
```

It may also depend on deadhead distance:

```text
required_kwh =
    (passenger_distance_km + deadhead_km_est) × spec.consumption_kwh_per_km
```

Therefore, feasibility must be computed for each EV-block pair.

### 6.2 Required energy

Recommended first-pass formula:

```python
required_kwh = (
    block.passenger_distance_km + deadhead_km_est
) * spec.consumption_kwh_per_km
```

Where:

```text
deadhead_km_est = depot_to_block_start_km + block_end_to_depot_km
```

In PR 1 (no home depot), the depot in this formula is the **block's attached
depot**, so `deadhead_km_est` depends only on the block, not on the vehicle —
it is a length-B vector, not an (S, B) matrix. An (S, B) deadhead matrix only
appears in PR 2 if matching allows vehicles to serve blocks away from their
home depot; if matching constrains `block depot == home depot`, it stays
per-block.

The deadhead formula must match the event stage exactly (`haversine_km × 1.0`,
as in `annual_depot_events.py`), otherwise the strong validation in §6.5 will
produce false positives from accounting mismatch alone. If coordinates are
missing, follow the event-stage behavior: deadhead contributes 0 km and the
row is flagged, keeping both stages consistent.

### 6.3 Available energy

Do not introduce new generic parameters such as:

```text
initial_soc = 1.0
usable_battery_fraction = 0.90
min_end_soc = 0.10
```

The existing spec columns already define usable SOC bounds:

```text
usable_soc_min
usable_soc_max
```

The SOC walk is in kWh.

Therefore:

```python
soc_floor_kwh = spec.battery_kwh * spec.usable_soc_min
soc_ceiling_kwh = spec.battery_kwh * spec.usable_soc_max
available_kwh = current_soc_kwh - soc_floor_kwh
```

Under daily-reset mode:

```python
current_soc_kwh = soc_ceiling_kwh
```

Under carry-over mode:

```python
current_soc_kwh = vehicle_soc_state[vehicle_spec_id]
```

Missing SOC state in carry-over mode should be a hard error, not defaulted to full battery.

### 6.4 Feasibility rule

First-pass conservative rule:

```python
is_feasible = required_kwh <= available_kwh
```

This intentionally ignores mid-day charging opportunities.

Interpretation:

```text
The feasibility screen is conservative.
It guarantees that a matched EV should be able to complete the block without relying on mid-day depot or layover charging.
```

If mid-day charging is later modeled as part of feasibility, then the rule should be expanded carefully and the SOC simulation should remain the source of truth.

### 6.5 Strong validation

After assignment, the SOC simulator should treat the feasibility screen as a strong pre-check.

If `end_soc_below_min_flag` still occurs for a matched pair, that indicates one of:

- feasibility and SOC simulation use inconsistent energy accounting;
- deadhead was omitted or underestimated;
- charging-window ownership is wrong;
- SOC state was not initialized or carried correctly.

---

## 7. PR 1: feasibility-aware matching under daily-reset SOC

PR 1 should avoid changing cross-day SOC semantics.

This makes the assignment refactor independently testable and lowers risk.

### 7.1 PR 1 behavior

For each `service_date`:

```text
1. Set each EV's start SOC to usable_soc_max.
2. Sample representative active blocks without replacement.
3. Compute vectorized EV-block feasibility using pair-specific energy.
4. Run maximum-cardinality bipartite matching.
5. Emit only matched feasible vehicle-day assignments.
6. Record sampled-but-unmatched blocks with reason.
7. Pass matched assignments into the existing SOC/event pipeline.
```

### 7.2 Sampling

Default:

```python
n_sampled_blocks = min(len(day_blocks), len(specs))
```

Optional:

```python
n_sampled_blocks = min(
    len(day_blocks),
    ceil(len(specs) * sample_block_multiplier),
)
```

A multiplier above 1.0 gives the matching step more candidate blocks while still limiting computational cost.

### 7.3 Vectorized feasibility matrix

Avoid Python nested `iterrows`.

Example shape:

```text
n_specs × n_sampled_blocks
```

Data dependency: feasibility needs `depot_lat/lon` and block `start_lat/lon`,
`end_lat/lon`. These are **not** on `block_instances` today — they are merged in
at the event stage (`annual_depot_events.py:70-74`, joining
`block_templates_lsoa` and `depot_registry`). The new assignment function must
take these tables as inputs (or require pre-merged instances) and perform the
same merge before computing deadhead.

Pseudo-code:

```python
spec_battery_kwh = specs["battery_kwh"].to_numpy()                    # shape (S,)
spec_usable_soc_min = specs["usable_soc_min"].to_numpy()              # shape (S,)
spec_usable_soc_max = specs["usable_soc_max"].to_numpy()              # shape (S,)
spec_consumption = specs["consumption_kwh_per_km"].to_numpy()         # shape (S,)

block_passenger_km = sampled_blocks["passenger_distance_km"].to_numpy() # shape (B,)
block_deadhead_km = compute_block_deadhead(sampled_blocks)               # shape (B,)
# per-block: attached depot ↔ block start/end, haversine × 1.0

current_soc_kwh = spec_battery_kwh * spec_usable_soc_max                # daily-reset mode
soc_floor_kwh = spec_battery_kwh * spec_usable_soc_min
available_kwh = current_soc_kwh - soc_floor_kwh                         # shape (S,)

required_kwh = (
    block_passenger_km + block_deadhead_km
)[None, :] * spec_consumption[:, None]                                   # shape (S, B)

feasible = required_kwh <= available_kwh[:, None]
```

### 7.4 Matching: exact greedy on the nested threshold structure

**Implementation note (2026-06-03): general bipartite matching is not needed in
PR 1, and scipy is not available in the target environment.**

Under daily-reset SOC, feasibility is a pure threshold rule:

```text
feasible(s, b)  ⟺  total_km[b] ≤ range_km[s]
range_km[s] = battery_kwh[s] × (usable_soc_max[s] − usable_soc_min[s]) / consumption_kwh_per_km[s]
```

So each block's feasible spec-set is `{s : range_km[s] ≥ total_km[b]}`, and these
sets are **nested by inclusion** (a longer block's feasible set is a subset of a
shorter block's). On a nested set system, the following greedy is an **exact
maximum-cardinality matching** (standard exchange argument), with unbiased
random tie-breaking:

```text
sort blocks by total_km descending
sort specs by range_km descending
pool = []   # feasible, still-unused specs
for each block (largest first):
    admit into pool all not-yet-admitted specs with range_km ≥ total_km[block]
    n_feasible_vehicles[block] = number of specs admitted so far
    if pool non-empty:
        match block to a uniformly random spec from pool (swap-pop)
    else:
        unmatched (reason: no_feasible_vehicle if n_feasible_vehicles == 0,
                   else lost_matching_competition)
```

Why any in-pool choice preserves maximality: all later blocks need ≤ the current
block's km, so every spec in the pool remains feasible for every later block —
no choice can strand a later block that an alternative choice would have saved.

Complexity: `O((S + B) log(S + B))` per day. No scipy / networkx dependency.

**Caveat for PR 2:** this exactness depends on the nested structure. If PR 2
adds non-nested constraints (home depot, vehicle type, agency), replace this
with general bipartite matching per constraint group (scipy
`maximum_bipartite_matching` if available, else networkx
`hopcroft_karp_matching`; networkx is present in the current environment).
Grouping by depot keeps the nested greedy exact within each group.

This optimizes:

```text
maximum number of matched feasible EV-block pairs
```

### 7.5 Determinism

Use the existing `stable_daily_seed` pattern.

Randomness should control:

- sampled block order;
- permuted spec order;
- permuted block order for matching tie-breaking.

Given the same inputs and seed, output should be reproducible.

### 7.6 Unmatched reasons

Record unmatched sampled blocks in a separate diagnostics table.

At minimum:

```text
no_feasible_vehicle
lost_matching_competition
```

Definitions:

```text
no_feasible_vehicle:
    the sampled block has zero feasible EVs before matching.

lost_matching_competition:
    the sampled block has at least one feasible EV,
    but all feasible EVs were matched to other blocks.
```

This preserves diagnostic value that would otherwise disappear after feasible matching.

### 7.7 PR 1 output tables

Recommended split:

1. `vehicle_day_assignments`
2. `assignment_diagnostics_by_service_date`
3. `unmatched_sampled_blocks`

Avoid repeating daily diagnostics on every assignment row.

#### `vehicle_day_assignments`

Suggested fields:

```text
service_date
vehicle_spec_id
block_id
assignment_status = "matched_feasible"
assignment_method = "sample_then_vectorized_feasible_matching"
required_kwh_est
available_kwh_at_assignment
deadhead_km_est
daily_soc_mode = "daily_reset"
```

#### `assignment_diagnostics_by_service_date`

Suggested fields:

```text
service_date
n_ev_specs
n_active_block_instances_for_service_date
n_sampled_block_instances_for_service_date
n_feasible_edges
n_blocks_with_any_feasible_vehicle
n_matched_feasible_block_instances_for_service_date
n_unmatched_sampled_block_instances_for_service_date
n_unmatched_no_feasible_vehicle
n_unmatched_lost_matching_competition
sampled_block_coverage_share
matched_sample_share
matched_active_block_share
assignment_method
daily_soc_mode
```

#### `unmatched_sampled_blocks`

Suggested fields:

```text
service_date
block_id
unmatched_reason
n_feasible_vehicles
assignment_method
daily_soc_mode
```

---

## 8. PR 2: SOC carry-over mode

PR 2 should be implemented only after PR 1 is stable.

It changes the simulation semantics and must be integrated into the streaming pipeline.

### 8.1 Required new mode

Add explicit SOC mode:

```text
soc_mode = "daily_reset" | "carryover"
```

Default should remain:

```text
daily_reset
```

until carry-over is fully validated.

### 8.2 Why assignment must move into the streaming loop

With SOC carry-over:

```text
Day N assignment depends on Day N start SOC.
Day N start SOC depends on Day N−1 charging and driving.
```

Therefore, assignment can no longer be generated as a fully independent upstream annual parquet before SOC simulation.

The current shape:

```text
build full-year assignments
→ write vehicle_day_assignments.parquet
→ stream dates through events/SOC
```

must become:

```text
initialize vehicle_soc_state
for each service_date in chronological order:
    load/generate day blocks
    assign vehicles using current vehicle_soc_state
    write day assignment partition
    generate day events
    run day SOC
    write day outputs
    update vehicle_soc_state for next service_date
```

This change belongs in the streaming tail of `run_bus_annual_depot_load.py`.

### 8.3 Avoid in-memory annual accumulation

Do not accumulate:

```text
assignment_rows
soc_state_rows
event_rows
load_rows
```

for the full year.

Use existing streaming patterns:

```text
_write_partitioned_by_service_date
_add_stream_stats
```

or their equivalent.

This matters because:

```text
vehicle_soc_states can be O(n_specs × n_days × states_per_day)
```

and annual event/load tables can be large.

### 8.4 Carry-over algorithm sketch

```python
vehicle_soc_state = initialize_vehicle_soc_state(
    specs,
    mode="usable_soc_max",
)

for service_date in service_dates:
    day_blocks = load_blocks_for_service_date(service_date)

    assignments, assignment_diag, unmatched = assign_for_service_date(
        specs=specs,
        day_blocks=day_blocks,
        vehicle_soc_state=vehicle_soc_state,
        soc_mode="carryover",
        rng=stable_daily_rng(service_date),
    )

    write_partitioned_by_service_date(assignments, "vehicle_day_assignments")
    write_partitioned_by_service_date(assignment_diag, "assignment_diagnostics")
    write_partitioned_by_service_date(unmatched, "unmatched_sampled_blocks")

    events = build_depot_events_for_service_date(
        assignments=assignments,
        vehicle_soc_state=vehicle_soc_state,
        day_boundary_hour=6,
        home_depots=home_depots,
    )

    idle_events = build_idle_home_depot_charging_events(
        specs=specs,
        assignments=assignments,
        vehicle_soc_state=vehicle_soc_state,
        service_date=service_date,
        day_boundary_hour=6,
        home_depots=home_depots,
    )

    events = concat(events, idle_events)

    soc_results = apply_depot_soc_for_service_date(
        events=events,
        vehicle_soc_state=vehicle_soc_state,
        soc_mode="carryover",
    )

    # Load aggregation stays in the existing separate function
    # (aggregate_depot_load_15min) so the streaming tail's
    # energy-conservation check structure is unchanged.
    day_load, day_daily = aggregate_depot_load_15min(events, depot_registry, soc_results.summary)

    write_partitioned_by_service_date(events, "depot_events")
    write_partitioned_by_service_date(soc_results.states, "vehicle_soc_states")

    vehicle_soc_state = soc_results.end_of_day_soc_by_vehicle
```

### 8.5 Missing SOC state should be fatal

Under carry-over:

```python
vehicle_soc_state[vehicle_spec_id]
```

should raise if missing.

Do not use:

```python
vehicle_soc_state.get(vehicle_spec_id, 1.0)
```

or any default full battery fallback.

A missing SOC state means the chronological simulation state is broken.

### 8.6 Start and end SOC

Initialize at the start of the simulation window:

```python
soc_start_kwh = battery_kwh * usable_soc_max
```

Clamp upper bound:

```python
soc_max_kwh = battery_kwh * usable_soc_max
```

Clamp / flag lower bound:

```python
soc_min_kwh = battery_kwh * usable_soc_min
```

The model should never initialize at `battery_kwh * 1.0` if `usable_soc_max = 0.95`.

---

## 9. Warm-up and partial-date runs

With carry-over, results for a date depend on prior history.

Therefore:

```text
--max-days smoke tests are not directly comparable with full-year results
unless the same prior SOC history is included.
```

Add a warm-up concept:

```text
warmup_days = 14
```

or reuse the existing `WARMUP_DAYS = 14` pattern if available.

Recommended output behavior:

- include warm-up days in SOC evolution;
- optionally exclude warm-up days from reported annual summary metrics;
- record warm-up settings in `run_summary`.

Suggested run summary fields:

```text
soc_mode
soc_day_boundary_hour
warmup_days
assignment_mode
sample_block_multiplier
home_depot_assignment_method
idle_vehicle_charging_policy
```

---

## 10. Output contract and limitations updates

### 10.1 `annual_depot_outputs.py`

The current required limitation:

```text
does not model multi-day SOC carry-over
```

should become conditional.

If:

```text
soc_mode = "daily_reset"
```

then keep the existing limitation.

If:

```text
soc_mode = "carryover"
```

replace it with limitations such as:

```text
models multi-day SOC carry-over using a fixed operational day boundary;
SOC continuity is subject to the assumed home-depot assignment and idle charging policy;
assignment feasibility uses a conservative pre-block energy screen unless otherwise specified.
```

### 10.2 `annual_depot_soc.py`

Update comments around the old overlap assumption.

Current comment meaning:

```text
overlapping previous-day overnight and next-day pre-block windows are acceptable
because days are independent
```

Under carry-over this is no longer true.

Add explicit branch:

```text
daily_reset mode:
    legacy overlapping windows are allowed.

carryover mode:
    event windows must be non-overlapping in wall-clock time per vehicle.
```

### 10.3 Tests

Update or add tests for:

```text
legacy_random_zip mode reproduces current outputs exactly (regression anchor)
daily-reset legacy compatibility
feasibility-aware matching never emits infeasible static pairs
unmatched reasons are correct
matching is deterministic under fixed seed
carry-over uses previous day end SOC
missing carry-over SOC state raises
no overlapping charging events for same vehicle under carryover
idle charging creates energy events if idle charging policy is enabled
run summary records soc_mode and warmup_days
limitations switch by soc_mode
```

Affected existing tests include:

```text
tests/mobility/bus/test_vehicle_day_assignment.py
```

---

## 11. Revised implementation order

### Phase 0: design decisions before code

Resolve and document:

```text
soc_day_boundary_hour = 6
home depot assignment method
idle vehicle charging policy
deadhead estimation method
whether PR 1 keeps daily_reset as the only enabled SOC mode
```

### Phase 1 / PR 1: assignment refactor under daily-reset SOC

Implement:

```text
sample blocks
vectorized pair-specific feasibility
exact greedy maximum matching on the nested threshold structure (§7.4)
unmatched reasons
separate assignment diagnostics table
legacy assignment mode switch
A/B comparison against random zip
```

Do not implement SOC carry-over in this PR.

Expected benefit:

```text
removes fake infeasible caused by random EV-block zip
keeps current day-independent SOC semantics
does not require changing event-window ownership yet
```

### Phase 2 / PR 2: carry-over SOC

Implement:

```text
soc_mode = carryover
day boundary ownership
home depot assignment
streaming assignment inside chronological day loop
idle vehicle charging events
vehicle_soc_state update across days
warm-up support
output contract / limitations update
tests
```

Expected benefit:

```text
physically meaningful vehicle-level SOC trajectories across days
no duplicate charging in overlapping wall-clock windows
depot load includes both assigned vehicle charging and explicit idle charging
```

---

## 12. Recommended CLI / config additions

Suggested parameters:

```text
--assignment-mode legacy_random_zip | sample_then_feasible_match
--soc-mode daily_reset | carryover
--sample-block-multiplier 1.0
--soc-day-boundary-hour 6      # default overnight end only; ownership is per-vehicle stitching (§3.3)
--warmup-days 14
--idle-vehicle-charging-policy none | home_depot
--home-depot-method source_inventory | source_lsoa_nearest | first_assignment | region
```

Default safe settings:

```text
assignment_mode = sample_then_feasible_match
soc_mode = daily_reset
sample_block_multiplier = 1.0
```

Carry-over should remain opt-in until validated.

---

## 13. Short model description for README / PR

### English

This simulation assigns EV buses to representative daily bus duties using a feasibility-aware matching step. For each service date, active blocks are sampled without replacement, EV-block energy feasibility is computed at the pair level using each vehicle's battery, usable SOC bounds, consumption rate, and estimated deadhead distance, and a maximum-cardinality bipartite matching assigns at most one EV to each sampled block and at most one sampled block to each EV. Only matched feasible assignments are passed into depot event and SOC simulation. Sampled blocks that cannot be matched are retained in diagnostics with unmatched reasons.

In `daily_reset` SOC mode, each EV starts each service date at `usable_soc_max`, preserving the existing representative-day semantics. In `carryover` SOC mode, assignment must be performed inside the chronological streaming loop because each day's feasible assignments depend on the prior day's end SOC. Carry-over mode also requires non-overlapping wall-clock event windows, a stable vehicle home-depot assignment, explicit idle-vehicle charging events if idle charging is enabled, and warm-up handling for partial-year runs.

Note on sampling semantics: feasibility-aware matching changes the composition of simulated duties relative to pure random zip. Matched blocks are no longer an unbiased sample of the day's active blocks — the feasibility screen and matching competition systematically favor lower-energy blocks, and this effect grows with `sample_block_multiplier > 1.0`. The unmatched-blocks diagnostics table preserves the excluded tail so that this selection effect remains observable and reportable.

### 中文

本仿真不再采用“随机抽 block、随机抽 EV、直接 zip 后再发现 infeasible”的分配方式。新的分配逻辑是在每个 `service_date` 上先从当天 active blocks 中不放回抽取 representative sample blocks，然后基于每辆 EV 的电池容量、`usable_soc_min` / `usable_soc_max`、单位里程能耗和估计 deadhead 距离，逐 EV-block pair 计算能量可行性，再用最大基数二分匹配求解一对一 assignment。日内每辆 EV 最多匹配一个 sampled block，每个 sampled block 最多被一辆 EV 覆盖。只有成功匹配的 feasible assignments 会进入 depot event 和 SOC 仿真；抽中但未匹配的 blocks 会保留在诊断表中，并区分 `no_feasible_vehicle` 和 `lost_matching_competition`。

在 `daily_reset` SOC 模式下，每辆 EV 每天从 `usable_soc_max` 开始，保留现有“独立代表日”的语义。在 `carryover` SOC 模式下，Day N 的 assignment 依赖 Day N-1 的 end-of-day SOC，因此 assignment 必须移入逐日 streaming 主循环。carry-over 模式还必须先解决墙钟时间窗口归属，避免 Day N overnight 和 Day N+1 pre-block 在 00:00–06:00 期间重复充电；同时需要给每辆车稳定的 home depot，并且如果 idle 车辆允许补电，必须产生显式 depot charging events，不能只在 SOC 字典里静默增加电量。

---

## 14. Open decisions — RESOLVED (2026-06-03)

1. **Day boundary**: per-vehicle ledger stitching (§3.3), not a fixed clock boundary.
   `soc_day_boundary_hour = 6` is only the default overnight end when the vehicle
   has no next-day assignment.
2. **Home depot source**: `source_lsoa_nearest` — derive from the EV inventory's
   `source_lsoa` and the depot registry's LSOA/coordinates. Deterministic, no
   simulation bootstrap. (`first_assignment` is rejected: matching constraints
   depend on home depot, which would itself come from matching — circular.)
3. **Deadhead with missing coordinates**: follow the event-stage behavior —
   contribute 0 km and flag the row, keeping assignment and SOC stages consistent.
4. **Idle charging target**: charge to `usable_soc_max`, consistent with overnight
   behavior; no extra threshold parameter.
5. **Warm-up**: include warm-up days in output tables with an `is_warmup` flag;
   exclude them from summary metrics. Keeps SOC convergence trajectories inspectable.
6. **`sample_block_multiplier`**: production default 1.0; values > 1.0 reserved for
   sensitivity analysis and always recorded in `run_summary` (they shift the matched
   sample toward lower-energy blocks — see §13 note).

---

## 15. Phase split update (2026-06-03): PR 1 / PR 1.5 / PR 2

Supersedes the two-phase order in §11. The home-depot work originally bundled into
PR 2 is pulled forward into its own PR so that the *spatial* constraint change and
the *temporal* SOC change land and validate independently.

### PR 1 — feasible matching under daily_reset (DONE, full-year run in progress)

As implemented: `--assignment-mode sample_then_feasible_match`, pair-specific
feasibility, exact nested-threshold greedy max matching, unmatched reasons,
diagnostics, `--resume` support for the streaming runner. Validated on real 3-day
data (95.4–98.3% EV match share, 0 fake-infeasible, depot_only_feasible = 100%).
Note: PR 1 matching is spatially unconstrained — any EV may serve any block
nationwide; deadhead uses the block-attached depot, not a vehicle home depot.

### PR 1.5 — fixed home depot + depot/radius-constrained assignment, still daily_reset

```text
home depot:   source_lsoa_nearest (§14.2) — each EV is deterministically pinned to
              the depot nearest its inventory source_lsoa; persisted to
              ev_bus_specs (home_depot_id, home_depot_lsoa, home_depot_distance_km).
constraint:   an EV-block pair is admissible only if the block's attached depot is
              the EV's home depot, or within --home-depot-radius-km of it.
deadhead:     computed home-depot <-> block start/end (replaces block-depot proxy),
              missing coordinates contribute 0 km and flag the row (§14.3).
soc:          daily_reset unchanged; assignment stays ahead of the streaming loop,
              so --resume keeps working as built.
diagnostics:  new unmatched reason no_feasible_vehicle_in_radius; per-depot fleet
              supply vs block demand table; A/B vs PR 1 unconstrained matching to
              quantify the match-share cost of spatial realism.
new CLI:      --home-depot-method source_lsoa_nearest (default) | region
              --home-depot-radius-km <float, default TBD from A/B>
```

Rationale: PR 2's in-loop assignment *requires* a stable home depot (§14.2 rejects
first_assignment as circular). Validating home-depot pinning and the radius
constraint under daily_reset isolates the match-share / coverage impact from SOC
carry-over effects, and PR 2 then only adds temporal state on top of an already
validated spatial assignment.

#### PR 1.5 draft results (2026-06-03, real data, service_date 2026-04-17)

Implementation: `annual_home_depot.assign_home_depots` + constrained
Hopcroft-Karp matching (scipy, seeded relabelling, cross-checked against
networkx) in `build_feasible_vehicle_day_assignments(home_depot_radius_km=...)`.

```text
home depots:    4,444 / 5,328 EVs assigned (757 distinct depots);
                median source_lsoa->depot distance 0.21 km, p90 1.08 km
missing:        884 EVs (16.6%) have no centroid for source_lsoa —
                677 Scotland DZ2022 (S...) + 207 NI DZ2021 (N20...) codes are
                not in ONSPD lsoa21; needs the Scotland/NI geojson centroid
                sources already referenced in mobility/core/spatial.py
match share:    PR1 unconstrained 98.3%  | radius 0   11.8%
                radius 10 km      43.9%  | radius 25  54.6%
runtime:        0.5-4.6 s/day (vs 0.5 s unconstrained)
```

Open issues — RESOLVED 2026-06-03 (second draft iteration):

1. **Centroid coverage — FIXED, no geojson needed.** The missing codes are
   legacy geographies that ONSPD itself carries in other columns: Scotland
   DZ2011 in `lsoa11` (275 distinct codes) and NI DZ2021 in `oa21` (207).
   `mobility.core.spatial.load_extended_lsoa_centroids()` appends
   postcode-mean centroids for codes absent from `lsoa21` (priority
   lsoa21 > lsoa11 > oa21, tagged `centroid_source`). Result: 5,328/5,328 EVs
   assigned a home depot (996 distinct depots).
2. **Sampling design — DECISION (b): supply-weighted block sampling.**
   `block_sampling="supply_weighted"` weights each active block by the number
   of home-depot vehicles admitting its depot under the radius
   (Efraimidis-Spirakis without replacement, seeded). Blocks with no reachable
   fleet are never sampled (`no_vehicle_in_radius` vanishes by construction);
   per-block weight recorded as `block_sampling_weight`. Uniform sampling kept
   for A/B (`--block-sampling uniform`).

#### Updated real-data A/B (2026-04-17, full coverage + supply weighting)

```text
                          mult=1.0   mult=1.5   mult=2.0   mult=3.0
radius 0   (strict)         43.7%
radius 10                   61.6%      68.1%      76.6%      87.0%
radius 25                   59.7%      64.9%      71.1%      86.0%
(% of 5,328-EV fleet matched; runtime 2-6 s/day)
```

Production defaults DECIDED 2026-06-03: `--home-depot-radius-km 10.0` +
`--sample-block-multiplier 3.0` + `--block-sampling supply_weighted`
(87.0% fleet utilisation, closest comparability with PR 1 depot loads). Note
§13/§14.6 — multiplier > 1 strengthens the low-energy selection effect, which
`unmatched_sampled_blocks` + `block_sampling_weight` keep observable. PR 1
parity invocation: `--home-depot-method none --block-sampling uniform
--sample-block-multiplier 1.0`.

### PR 2 — SOC carryover + idle charging + stitched event windows (FINAL DELIVERABLE)

Scope as §8–§10 plus §14 decisions: `--soc-mode carryover`, per-vehicle ledger
stitching of wall-clock event windows (§3.3), assignment moved inside the
chronological day loop, explicit idle charging events to usable_soc_max at the home
depot (§14.4), `is_warmup` flag (§14.5), missing SOC state fatal (§8.5).

Resume note: under carryover, the existing --resume contract is insufficient —
day N depends on day N-1 end SOC, so the runner must checkpoint
`vehicle_soc_state` as a per-service_date partition and resume from the last
complete day's state (re-deriving it from event partitions is the fallback).

**Final deliverable is PR 2 output**: full-year depot-level 15-min charging load
curves, `depot_load_15min` with at least
`depot_id / depot_lsoa / depot_lat / depot_lon / service_date / slot_start_datetime
/ slot_index / charge_kwh (+ average_kw, n_charging_vehicles)` — the existing
output contract (`annual_depot_load._load_columns()`) already carries all required
fields; PR 2 changes the semantics (carry-over SOC, idle charging included in
load), not the schema.
