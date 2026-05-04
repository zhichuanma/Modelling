# Changelog

All notable changes to the Modelling package.

## Passenger-car cross-day continuity and Layer 2 label removal (2026-05-04)
- Reversed passenger-car cross-day smoothing so `day N+1` remains the NTS source
  of truth and only the overnight parking event on `day N` is aligned to the next
  day's declared origin `purpose` and `lsoa`.
- Added upstream `start_lsoa` injection to passenger-car day assembly so days
  that explicitly begin away from home now seed Layer-1 sampling, distances,
  and morning parking from the prior overnight LSOA instead of hard-coding
  `home_lsoa`.
- Tightened the overnight update to only touch parking events whose `end_time >= 24.0`,
  skipping late-arrival edge cases where no overnight parking event exists.
- Removed Layer-2 station matching's implicit dependency on station `label` for
  non-home parking, so same-LSOA and neighbor-LSOA fallback now sample from all
  stations in the candidate LSOAs while preserving existing Huff weighting and
  home-charging short-circuit behaviour.
- Added regression tests for true-overstay, declared-return-home, and naturally
  consistent cross-day boundaries, plus Layer-2 tests covering label-mismatched
  same-LSOA and neighbor fallback and missing-label station rows.
- Added upstream-injection tests for true overnight threading, silent-return
  suppression, and day-0 home fallback; no Stage-2d numeric baseline refresh
  was needed because the existing suite has no fixed-value snapshots here.

## Bus module redesign - single-bus narrative (2026-04-30)
- Rebuilt `mobility/bus/` around `DailySchedule` semantics consistent with `mobility/cars/`.
- `trip_chain_bus.block_to_daily_schedules` correctly handles the 9.5% of blocks
  that span midnight, returning a 2-day list instead of silently truncating.
- Added `data_loader.summarize_block_quality` to surface native-vs-inferred
  continuity, distance provenance, and cross-midnight prevalence as first-class
  metrics rather than caveats.
- `block_inference.infer_blocks` is a bit-exact port of the legacy greedy
  algorithm; preserved by a full-inferred-subset regression test.
- Added `notebooks/01_single_bus_simulation.ipynb` with explicit Stage A.5
  data-quality disclosure and a final identity-card summary.
- Removed the legacy single-day `mobility/bus/sim_adapter.py` and the stale
  `outputs/sim_per_bus.parquet` / `sim_fleet_load_kw.npy` / `sim_fleet_load.csv`
  artifacts - they were built before the cross-midnight fix.

## Bus vehicle-parameter sampling (2026-04-30)
- Added weighted sampling from `BEV_Bus_Coach_unique_with_params_with_AC.csv`
  so simulated bus blocks can use heterogeneous battery, consumption, and
  depot charging parameters instead of a single fixed vehicle.
- Updated `notebooks/01_single_bus_simulation.ipynb` to sample the protagonist
  bus model from the prepared vehicle table using `2025 Q2` stock weights.
