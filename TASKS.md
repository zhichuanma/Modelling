# Coach Module Code-Review Response — Task Spec

Unattended task. Execute the 7 tasks below in order. After each task: run `pytest tests/coach/ -x -q`, fix any red, then commit. Only then move to the next task.

## Global constraints

1. No new external dependencies.
2. Do not modify any bus module code. You may read bus code for reference only.
3. After each task: `pytest tests/coach/ -x -q` must pass before moving on. Fix red immediately; do not skip.
4. One git commit per task. English commit message, format: `coach: <task n> <short description>`.
5. No force push. No rebase of existing commits. No `git push`.
6. For cross-midnight "dead code", repair it — do not delete.
7. Any "I think this design is wrong but the review didn't mention it" thoughts → write to `CODE_REVIEW_RESPONSE.md` under "Out of scope observations". Do not act on them.
8. Running under Auto mode. The server-side classifier may block dangerous ops. If blocked: do NOT retry the same command — switch to a safe equivalent. After 2 consecutive blocks, stop, log "blocked action + intent + reason" to `CODE_REVIEW_RESPONSE.md` under "Blocked by classifier", skip the current task, continue with the next.
9. No `git push`. No `curl | sh` of external scripts. Do not touch `~/.ssh` or `.env`. Out of scope even if "convenient".

## Task 1 — Unify cross-midnight entry semantics

**Decision: `start_h >= 24` is forbidden; `end_h > 24` is allowed (true cross-midnight).**

- `mobility/coach/trip_chain_coach.py:46-49` — keep the `raise ValueError(...)`, rewrite the message to: `"start_h must be in [0, 24); cross-midnight journeys are encoded as end_h > 24 with start_h < 24"`.
- `mobility/coach/trip_chain_coach.py:79-80` — delete the dead `else: add(1, start_h - HOURS_PER_DAY, ...)` branch.
- `mobility/coach/data_loader.py:119` — change to `has_cross_midnight = (end_h > 24.0)`. Assert `(start_h < 24.0).all()` before the assignment so dirty input fails at build time.
- Add one line to the `trip_chain_coach.py` module docstring documenting this convention.

## Task 2 — Cross-midnight end-to-end integration test

Goal: prove the `_split_trip` multi-day branch + `sim_adapter` day_offset work end-to-end.

- New file: `tests/coach/test_simulate_cross_midnight_e2e.py`.
- Construct a synthetic journey with `start_h=22.0, end_h=26.0`. Do NOT go through `selection.py` — feed it directly to `simulate_coach_journey`.
- Assert: `len(load_kw) == 2 * STEPS_PER_DAY`; both the day=0 tail and the day=1 head have non-zero driving load; the SoC trajectory is continuous (no jumps).
- If `simulate_coach_journey`'s current API does not allow bypassing `selection.py`, add a thin wrapper `simulate_coach_journey_from_dict(journey_dict, ev_dict)` in `sim_adapter.py`. Do NOT change the original entry point signature.

## Task 3 — Infeasible EV regression test

Goal: cover `simulate_single_ev` failure semantics when the EV cannot complete the journey.

- New file: `tests/coach/test_simulate_infeasible.py`.
- Use a synthetic EV with deliberately small `Energy_kWh` against a long journey.
- Assert: `soc_clamped_to_zero == True`; `soc_floor_hit_h` is a finite positive number and `< journey end_h`; `min_soc_required > 0`.
- Also assert `feasibility.py` reports `feasible == False` for the same input.

## Task 4 — `pre_journey_dwell_h` vs `soc_init` default conflict

**Decision: default `soc_init` becomes `None`, meaning "auto-derive starting SoC from `pre_journey_dwell_h` charging".**

- `mobility/coach/sim_adapter.py` — change `soc_init: float = 1.0` to `soc_init: Optional[float] = None`.
- At the top of the function body: `if soc_init is None: soc_init = max(0.0, 1.0 - (pre_journey_dwell_h * charger_kw / battery_kwh))`. Clip to `[0.0, 1.0]`. This makes the 6h pre-dwell an actual charging window.
- Existing tests that pass `soc_init=1.0` explicitly must keep their current behaviour. Do NOT modify those tests.
- Add 2 lines to the `sim_adapter.py` docstring describing the coupling between these two parameters.
- Run the full test suite to confirm no regression.

## Task 5 — Remove redundant `.copy()` in `coach_fleet.py`

- `coach_fleet.py` lines 54 and 63 — keep the first `.copy()` (after the loc slice). After the filter, replace the second `.copy()` with in-place assignment.
- `sample_coach_ev`: collapse its two `.copy()` calls down to 1 or 0 (if the code only reads, no copy is needed).
- Run `pytest tests/coach/test_coach_fleet.py` to confirm behaviour is unchanged.

## Task 6 — Make `selection.py` runtime fallback observable

- Change `_runtime_h()` to return a `(value, source_col_name)` tuple.
- All callers must accept the tuple and write `source_col_name` into a new column `runtime_source` on the selection output dataframe.
- Update the corresponding unit tests to assert the new column exists and that values are within the legal enum.

## Task 7 — Scaffold `scripts/run_coach_pipeline.py`

Goal: add the fleet-level simulation entry-point v1. **Only** batch single-journey simulation. **Not** vehicle assignment. **Not** year-long scheduling.

- New file: `scripts/run_coach_pipeline.py`.
- `main()`: read the journey parquet output by `build_all_journeys` → for each journey, sample one coach EV → run `simulate_coach_journey` → write `(journey_id, ev_id, feasible, total_kwh, soc_floor_hit_h, soc_clamped_to_zero)` to the output parquet.
- argparse: `--journeys-parquet`, `--output-parquet`, `--seed`, `--limit` (for smoke tests), `--n-workers` (default 1, serial).
- Top-level try/except + logging, in the style of bus's `build_all_blocks.py` for robustness. Do NOT import any bus module.
- Top of script docstring must state: `"Scope: batch single-journey simulation only. Does NOT do vehicle-to-journey assignment or year-long scheduling."`
- New file: `tests/coach/test_run_coach_pipeline.py`. Run a `--limit 3` smoke test. Assert the output parquet exists and has exactly 3 rows.

## Deliverable

At the repo root, write `CODE_REVIEW_RESPONSE.md` containing:

- For each task: commit hash + one-line change summary.
- The test commands run and their results.
- "Out of scope observations" section — issues you noticed but did not touch.
- "Known limitations" section — what pipeline v1 explicitly does not do.
- "Blocked by classifier" section — actions blocked by Auto mode, if any.

## Failure handling

If a task's preconditions turn out to be wrong (file path missing, API mismatch, etc.), stop, write the blocker into the "Blocked" section of `CODE_REVIEW_RESPONSE.md`, skip that task, and continue with the next. Do not guess.
