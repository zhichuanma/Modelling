# Bus charging modelling - next steps

Status: 2026-05-13
Owner: zhichuanma

This list scopes the assumptions deliberately simplified in
`notebooks/03_bus_annual_walkthrough.ipynb` E.5. Each item is a follow-up PR.

## 1. Public charger eligibility for bus

Current: only synthetic per-block depots are considered. Public OCM chargers
are excluded because they are not spec'd for bus dwell patterns / power.
Next: define an eligibility rule (e.g. >=150 kW DC + accessible site type)
and split public stations into "bus-eligible" vs "car-only".

## 2. Utilization & queueing

Current: ceiling computed as `depot_total_kw x 8760` (theoretical max).
Next: introduce per-station-kind utilization or full queueing
(M/G/c or discrete-event) so kWh demand competes for finite charge slots
across overlapping block end times.

## 3. Per-event station matching (cars-style Huff)

Current: charging is attributed at the LSOA level post-hoc, with one
synthetic depot per block.
Next: attach `location_lsoa` to each `depot_terminus` parking event and
extend `mobility.cars.station_matcher` style Huff to bus depots,
producing per-event `matched_station_id` and contention with car demand.

## 4. Real depot inventory

Current: depot map is synthesized - one depot per block at the block's
home_lsoa, capacity = the vehicle's depot_charge_kw. This means the depot
map is a reflection of the timetable, not of real infrastructure.
Next: replace synthetic depots with operator-reported depot inventory
(`outputs/depot_registry.parquet` provides one depot per agency from TxC
garages + virtual operator-centroid fallback; CPT / DfT / OS open data can
extend coverage), allowing one physical depot to serve multiple blocks,
capturing real geographic clustering, and exposing under-served operators
whose actual depot capacity is below the synthesized ceiling.

## 5. Cross-modal contention

Current: cars and bus consume separate registries.
Next: unified national charger registry, with bus, coach, car, LGV demands
all running through the same station capacity in time.
