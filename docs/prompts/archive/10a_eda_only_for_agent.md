# Task: Bus Depot EDA (PR-1 of `10_bus_depot_curves_plan.md`)

You are working on a self-contained slice of a larger plan
(`docs/prompts/archive/10_bus_depot_curves_plan.md`). **Only do PR-1**:
the exploratory analysis script that selects the per-depot block-count
threshold. Do **not** implement PR-2 onwards.

## Working directory

```
/Users/zm348/Library/CloudStorage/OneDrive-UniversityofExeter/Projects/Nature_EV_2025/Modelling
```

## What you are building

One new script:

```
scripts/explore_bus_depots.py
```

It reads `outputs/all_blocks.parquet`, derives a per-LSOA depot-candidate
table from each block's morning/night endpoints, and writes the artefacts
listed under "Outputs" below. It does **not** run the bus simulator and does
**not** depend on any other PR.

## Required reading (for context)

| File | Why |
|---|---|
| `docs/prompts/archive/10_bus_depot_curves_plan.md` | The parent plan. Read §1.3, §3 in full. §1.3 lists the locked design decisions you must honour. |
| `mobility/core/spatial.py` | Source of `query_lsoa_polygons`, `nearest_lsoa_for_points`, `load_lsoa_centroids`. Use these to map `(lat, lon) → LSOA`. |
| `mobility/bus/depot_registry.py` (function `_assign_lsoa`) | Existing example of LSOA resolution with polygon-then-centroid fallback. Mirror this pattern; do **not** invent a new one. |
| `outputs/all_blocks.parquet` | Inputs. Columns include `trip_id, agency_id, route_id, service_id, block_id, block_source, start_h, end_h, distance_km, start_stop, end_stop, start_lat, start_lon, end_lat, end_lon, shape_id`. ~1.67M rows over ~215k blocks. |

If a helper for LSOA→LAD lookup exists in `mobility/core/spatial.py` or
elsewhere in `mobility/`, reuse it. If none exists, fall back to writing
the `lad` column as empty and note it in `eda_summary.md`.

## Algorithm (precise)

For each `block_id` in `outputs/all_blocks.parquet`:

1. Sort that block's rows by `start_h`. The first row gives
   `(start_lat, start_lon)` → `morning_lsoa`. The last row gives
   `(end_lat, end_lon)` → `night_lsoa`. Use polygon query first, centroid
   fallback ≤ 0.25 km (mirror `mobility/bus/depot_registry.py::_assign_lsoa`).
2. Drop blocks where **either** endpoint coord is NaN. Record the count
   of such drops in `eda_summary.md`.

Aggregate at the LSOA level (no filtering — write the raw table):

| Column | Definition |
|---|---|
| `lsoa_code` | E01xxxxxxx |
| `n_blocks_morning` | # blocks whose `morning_lsoa` == this |
| `n_blocks_night` | # blocks whose `night_lsoa` == this |
| `n_blocks_total` | # distinct blocks where this LSOA is morning **or** night |
| `n_round_trip_blocks` | # blocks where `morning_lsoa == night_lsoa == this` |
| `round_trip_share` | `n_round_trip_blocks / n_blocks_total` |
| `n_agencies` | distinct `agency_id` count across associated blocks |
| `primary_agency_id` | mode of `agency_id` |
| `agencies_top3` | top-3 `agency_id` by frequency, joined with `\|` |
| `lad` | LAD reverse lookup (empty if no helper found) |
| `country` | 'E' / 'S' / 'W' / 'N' inferred from LSOA prefix |

## Outputs (write to `outputs/diagnostics/bus_depot_eda/`)

| Path | Format | Content |
|---|---|---|
| `lsoa_block_count.parquet` | parquet | The raw per-LSOA table above, **unfiltered**. |
| `lsoa_block_count_cdf.png` | matplotlib PNG | x = `n_blocks_total` (log scale), left y = number of LSOAs at-or-above x, right y = cumulative share of all blocks covered by LSOAs at-or-above x. Vertical reference lines at x ∈ {1, 3, 5, 10}. |
| `n_depots_by_lad.csv` | csv | One row per LAD, columns = `lad, n_lsoa_at_thresh_1, n_lsoa_at_thresh_3, n_lsoa_at_thresh_5, n_lsoa_at_thresh_10, n_blocks_total`. Sort descending by `n_blocks_total`. |
| `morning_eq_night_share.csv` | csv | Two sections: (a) one global row with `share = sum(morning_lsoa==night_lsoa) / n_blocks_with_both_lsoa`; (b) per-`agency_id` rows with `agency_id, n_blocks, share`. |
| `unmatched_blocks_summary.csv` | csv | For each candidate threshold T ∈ {1, 3, 5, 10}: one row with `min_blocks_threshold = T`, `n_blocks_unmatched` (both morning and night LSOA are sub-threshold), `n_agencies_unmatched`. |
| `eda_summary.md` | markdown | Your written summary — see "Summary file" below. |

## `eda_summary.md` content (≤ 400 words)

Write 6 short sections:

1. **Inputs** — # blocks read, # blocks dropped for NaN coords, total blocks aggregated
2. **LSOA distribution** — total candidate LSOAs (i.e. unique union of morning + night), # of LSOAs at each threshold T ∈ {1, 3, 5, 10}, % of blocks covered at each T
3. **Geographic spread** — top 10 LADs by `n_blocks_total` and their `n_lsoa_at_thresh_5`; check whether London (any LAD starting with `E090000` or with `London` in name) dominates. Quantify "London share = % of all candidate depot LSOAs in Greater London at T = 5".
4. **Round-trip share** — global `morning_eq_night` share, and agency-level outliers (agencies where share < 0.5)
5. **Recommended `MIN_BLOCKS_PER_DEPOT`** — one number with one-sentence justification anchored to the CDF
6. **Caveats / follow-ups** — anything you noticed that the parent plan should address (e.g. agencies with no morning-LSOA matches at all)

## Constraints

- **Do not** touch any file under `mobility/bus/` except for read-only reference.
- **Do not** create `mobility/bus/depot_lsoa_registry.py` — that is PR-2.
- **Do not** run the bus simulator or import from `mobility.bus.annual_simulation`.
- **Do not** add new dependencies; use what's already in `pyproject.toml`.
- Keep the script under ~300 lines. Helper functions live in the same file.
- Add a `__main__` block with `argparse`:
  - `--blocks` (default `outputs/all_blocks.parquet`)
  - `--output-dir` (default `outputs/diagnostics/bus_depot_eda`)
- Use `tqdm` or simple print progress when iterating ~215k blocks (LSOA resolution is the slow step).
- Vectorise LSOA resolution (one call to `query_lsoa_polygons` on the full coord arrays), don't loop per block.

## Verification before reporting done

Run:

```bash
python scripts/explore_bus_depots.py
```

Confirm all 6 output files exist and `lsoa_block_count.parquet` has > 0 rows.
Open `eda_summary.md` and verify the 6 sections are populated with real numbers.

## What to report back

A short message (≤ 250 words) containing:

- Total candidate LSOAs and counts at thresholds {1, 3, 5, 10}
- Whether London-dominance is a real issue (your call, with the % number)
- Your recommended `MIN_BLOCKS_PER_DEPOT`
- Anything in the plan (§3 or §4) that needs revising based on what you saw

Stop after this report. **Do not** start PR-2.
