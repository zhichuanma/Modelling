# Private-Car Scotland Geography Status

Status date: 2026-06-23

## Current Status

The Scotland DZ2011/DZ2022 mismatch is treated as resolved for the current
private-car station-curve workflow.

Current evidence in the codebase:

- `mobility/cars/scotland_geography.py` contains Scotland Data Zone version
  detection and area-weighted DZ2011 -> DZ2022 unification helpers.
- `mobility/cars/station_curves.py` calls
  `unify_scotland_ev_home_lsoa_to_dz2022()` before the geography preflight and
  records the geography context in the output report.
- `mobility/cars/geography_preflight.py` reports the final Scotland geography
  version as `Data Zone 2022` when the crosswalk has been applied and no
  blocking mismatch remains.
- `tests/mobility/cars/test_geography_preflight.py` covers the raw mismatch,
  the corrected overlap case, and DZ2011 -> DZ2022 unification.
- `mobility/core/spatial.py::load_extended_lsoa_centroids()` keeps an extended
  centroid fallback for Scotland DZ2011 codes, which means bus
  `source_lsoa_nearest` paths are no longer represented by the old failed
  attach diagnosis when the extended centroid input is available.

## Interpretation Rules

- Archived prompts and reviews may still describe the old Scotland mismatch;
  they are historical context, not active acceptance criteria.
- Existing generated outputs should only be interpreted as current if they were
  produced after the Scotland unification code was present in the relevant
  pipeline.
- New reports that use Scotland LSOA/Data Zone fields should record whether the
  source geography is DZ2011, DZ2022, or unified to DZ2022.
- For bus and coach outputs, the remaining work is provenance and targeted
  validation when those paths depend on exact regional codes. It is no longer a
  blanket blocker for spatial charging experiments.

## Follow-Up Checks

- Keep the focused geography tests in `tests/mobility/cars/` and
  `tests/mobility/core/` in the lightweight validation set.
- When refreshing private-car station-curve outputs, include the geography
  report alongside the output summary.
- If coach layover charging or a new exact-code bus/coach join is enabled,
  add a path-specific assertion that Scotland codes have been unified or
  matched through the extended centroid route.
