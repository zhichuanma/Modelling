# Coach Annual Simulation Response

All 8 tasks from the coach annual simulation prompt were executed in order. Each task was followed by `pytest tests/coach/ -x -q`; Task 4 also ran the full suite as requested.

## Commits

| Task | Commit | Summary |
| --- | --- | --- |
| 1 | `eb56941` | Added `mobility/coach/calendar.py` for TxC operating-profile parsing and per-journey feed-year date indexes with `profile_source` auditing. |
| 2 | `4cfd684` | Added first-fit date/operator coach chain construction in `mobility/coach/chain_builder.py`. |
| 3 | `c8271e8` | Added feed-year chain schedule expansion with inactive 24h `terminus_dwell` days in `mobility/coach/year_schedule.py`. |
| 4 | `fccc293` | Added chain-year and fleet-year coach SOC/load simulation in `mobility/coach/annual_simulation.py`. |
| 5 | `7cad9b8` | Added LSOA attribution helpers plus a coach-local nearest-centroid endpoint LSOA attach helper. |
| 6 | `bf0e642` | Added `scripts/run_coach_annual_pipeline.py` for annual smoke/full coach chain runs. |
| 7 | `c08e02f` | Added builder and generated notebook `notebooks/04_coach_annual_simulation.ipynb`. |
| 8 | this commit | Added `docs/coach_annual_next_steps.md` and this response file. |

## Test commands and results

- After Task 1: `pytest tests/coach/ -x -q` -> `17 passed in 2.79s`
- After Task 2: `pytest tests/coach/ -x -q` -> `20 passed in 2.21s`
- After Task 3: `pytest tests/coach/ -x -q` -> `21 passed in 3.81s`
- After Task 4: `pytest tests/coach/ -x -q` -> `23 passed in 3.96s`
- After Task 4 full suite: `pytest tests/ -x -q` -> `1 failed, 242 passed, 2 warnings in 48.84s`
- After Task 5: `pytest tests/coach/ -x -q` -> `25 passed in 2.23s`
- After Task 6: `pytest tests/coach/ -x -q` -> `26 passed in 2.65s`
- After Task 7: `pytest tests/coach/ -x -q` -> `26 passed in 2.54s`
- After Task 8: `pytest tests/coach/ -x -q` -> `26 passed in 2.56s`

The full-suite failure is the pre-existing, non-coach failure:

```text
tests/mobility/stage_6_8/test_home_charging.py::test_home_events_short_circuit_to_home_charger_and_keep_non_home_matching
assert 3.6 == 11.0 +/- 1.1e-05
```

This failure does not import coach modules and matches the earlier known failure recorded in `CODE_REVIEW_RESPONSE.md`.

## Notebook and public API checks

- `python notebooks/_build_04_coach_annual_narrative.py` -> wrote `notebooks/04_coach_annual_simulation.ipynb` with `22` cells.
- `jupyter nbconvert --to notebook --execute --inplace notebooks/04_coach_annual_simulation.ipynb` -> passed in about `13.3s` after allowing Jupyter to start a local kernel.
- Public API grep checks:

```text
mobility/coach/sim_adapter.py:69:def simulate_coach_journey(
mobility/coach/trip_chain_coach.py:105:def journey_to_daily_schedules(
mobility/coach/feasibility.py:6:def journey_feasibility(
mobility/coach/coach_fleet.py:31:def load_coach_fleet(path: str | Path = COACH_FLEET_PATH) -> pd.DataFrame:
mobility/coach/coach_fleet.py:72:def sample_coach_ev(
```

## Out of scope observations

1. `coach_chain_template_id` is a simulation convenience layered over date-stamped first-fit chains; it is not a real recurring vehicle duty identifier.
2. TxC operating profiles have richer holiday semantics than this v1 parser models. The parser covers weekday tags, date ranges, holidays-only cases, and simple bank-holiday add/remove behavior, but not every TransXChange edge case.
3. Nearest-centroid LSOA assignment is lower confidence than polygon-first matching. It is adequate for smoke-level post-hoc attribution but should not be treated as authoritative infrastructure siting.
4. The notebook smoke path intentionally samples a small prepared subset to keep execution below 120 seconds; formal national outputs still require an explicit production run.

## Known limitations

- No operator-real vehicle blocking. Chain assignment is first-fit by time and relocation distance, not a reconstruction of real coach rosters.
- SoC is carried continuously within each synthetic chain-year, but not across separate chain templates that might correspond to the same real-world vehicle under unknown operator scheduling.
- No public charger eligibility or OCM supply is included in E.5; terminus capacity is synthesized only from simulated chains.
- No utilization, queueing, connector occupancy, or capacity contention model is applied. `ceiling_kwh_year = terminus_total_kw x 8760`.
- No en-route fast-charging or per-event charger matching is modelled. `terminus_charge_kw` remains a single slow-charging endpoint abstraction.
- `--n-workers` is accepted by the annual CLI but values other than `1` only warn and still run serially.

## Blocked by classifier

None.
