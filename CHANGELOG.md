# Changelog

All notable changes to the Modelling package.

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
