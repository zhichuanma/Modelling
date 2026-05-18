# Coach Annual Simulation Next Steps

1. **Operator-real vehicle blocking** — replace the first-fit synthetic chain heuristic with real coach operator vehicle rosters if CPT, DfT, or operator-reported blocking data becomes available.

2. **Per-event terminus matching** — attach `location_lsoa` to every `depot_terminus` event and assign each event to a plausible terminus or charger using a Huff-style allocation similar to the private-car `station_matcher`. V1 of this is implemented as retry-with-eligible-LSOA-layover, gated by `--enable-eligible-layover-retry`; full Huff allocation across journeys remains future work.

3. **Public charger eligibility for coach** — define which OCM chargers are coach-usable, for example high-power DC chargers with suitable bay geometry and long-dwell access, then include eligible public supply in the capacity side. Initial coach-eligibility filtering (`COACH_ELIGIBLE_OCM_BANDS`) is in `mobility/coach/charging_supply.py`; refinements such as operator-specific access rules, bay geometry, and dwell-friendly siting remain future work.

4. **Real coach depot inventory** — replace the synthetic terminus map with operator-owned depot or yard locations if an auditable national depot inventory can be obtained.

5. **Cross-modal contention** — put bus, coach, and car demand onto a shared national charger registry so capacity, utilization, and queueing pressure can be studied across modes rather than in separate silos.
