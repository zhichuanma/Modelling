# Follow-up: Bus feasibility review fixes + full parquet regeneration gate

## Purpose

This is a merge-blocking follow-up for the bus duty-cycle PR. The main implementation has passed functional review and the new bus/core tests are green, but four small code fixes plus the full `outputs/all_blocks.parquet` regeneration gate still need to be handled before final merge.

The coding agent should apply the code fixes first, then attempt the full parquet regeneration workflow. If the canonical build command cannot be found, do **not** fake regeneration; stop and document exactly what is missing.

---

## Scope

### Must fix before merge

1. Fix `starts_below_min_required` false positives by including pre-first-trip charging.
2. Make `attach_lsoa` robust when centroid fallback data is empty or unavailable.
3. Add explanatory comments for two intentional deviations.
4. Attempt full `outputs/all_blocks.parquet` regeneration and legacy comparison.
5. Update PR description to state exactly which P5 steps were completed or deferred.

### Do not change

- Do not modify `mobility/core/simulator._soc_walk`.
- Do not introduce `geopandas`, `shapely`, `pyproj`, `pyshp`, `fiona`, or other GIS dependencies in `Modelling/`.
- Do not import from `mobility.coach.*` or `mobility.cars.*`.
- Do not write parquet/csv from notebooks.
- Do not use `--no-verify`.

---

## 1. Fix `starts_below_min_required` pre-charge handling

### File

`mobility/bus/feasibility.py`

### Problem

The current `starts_below_min_required` check only compares the first trip energy against initial SOC:

```python
soc_init * battery_kwh < first_trip.energy_consumed_kwh + reserve_kwh
```

This can falsely mark a block infeasible when there is chargeable depot parking before the first service trip.

Example false positive:

- `soc_init = 0.05`
- `battery_kwh = 300`
- initial energy = 15 kWh
- first trip requires 60 kWh
- vehicle has 2 hours depot charging at 50 kW before first trip, giving up to 100 kWh

This block should not be classified as `starts_below_min_required`.

### Required change

Before applying the `starts_below_min_required` reason, include charge accumulated before the first trip departure from chargeable `ParkingEvent`s.

Suggested logic:

```python
pre_first_trip_charge_kwh = sum(
    parking.duration_hours * parking.charge_power_kw
    for parking in first_schedule.parking_events
    if (
        parking.can_charge
        and parking.end_time <= first_trip.departure_time
        and parking.charge_power_kw > 0.0
    )
)

available_before_first_trip_kwh = min(
    battery_kwh,
    soc_init * battery_kwh + pre_first_trip_charge_kwh,
)

if available_before_first_trip_kwh < first_trip.energy_consumed_kwh + reserve_kwh:
    return starts_below_min_required
```

Preserve the existing infeasibility reason priority order.

### Required tests

Update or add tests in `tests/mobility/bus/test_feasibility.py`:

1. Low `soc_init`, first trip energy exceeds initial energy, but pre-first-trip charging is sufficient.
   - Expected: not `starts_below_min_required`.
2. Same setup, but pre-first-trip charging is insufficient.
   - Expected: `starts_below_min_required`.

---

## 2. Make `attach_lsoa` robust when centroid fallback is unavailable

### File

`mobility/bus/data_loader.py`

### Problem

In an extreme case, polygon matching may leave some points unresolved and centroid fallback data may be empty or unavailable. The current implementation can raise through `_centroids_for_nearest`.

Production data probably avoids this, but test fixtures and partial data environments should not crash.

### Required behavior

If polygon matching returns `no_match` for some points and centroid fallback data is empty or unavailable:

- do not raise;
- keep unresolved points as `no_match`;
- preserve empty or missing LSOA code values for those rows;
- update `attrs["lsoa_join"]["no_match_pct"]` correctly.

Only attempt centroid fallback when there are unresolved points. If centroid data is empty, skip fallback gracefully.

### Required test

Add or update a test in `tests/mobility/bus/test_attach_lsoa_polygon.py`:

- polygon fixture does not cover at least one stop;
- centroid fallback fixture/data is empty or unavailable;
- `attach_lsoa` returns successfully;
- unresolved row has `*_lsoa_match_method == "no_match"`;
- `attrs["lsoa_join"]["no_match_pct"] > 0`.

---

## 3. Add comments for intentional deviations

### File 1

`mobility/bus/block_inference.py`

Add a short comment near the deterministic tie-break explaining why `pool_bid` is used instead of `trip_id`:

```python
# Candidates represent existing inferred pool blocks, not only individual trips;
# using pool_bid gives a stable deterministic tie-break at the block level.
```

### File 2

`mobility/bus/feasibility.py`

Add a short comment near the shadow SOC walk explaining why it relies on `ParkingEvent` fields rather than accepting separate charging toggles:

```python
# Charging policy is already encoded in ParkingEvent.can_charge and
# ParkingEvent.charge_power_kw by the schedule builder/sim adapter, so the
# shadow walk intentionally does not re-interpret allow_layover_charging.
```

---

## 4. Full `outputs/all_blocks.parquet` regeneration gate

This is part of P5 and should be attempted after the code fixes and tests pass.

### 4.1 Find the canonical build command

From the `Modelling/` repo root, run:

```bash
git log --all --diff-filter=A -- '**/build_all_blocks*'
find /Users/zm348/Library/CloudStorage/OneDrive-UniversityofExeter -maxdepth 6 \
  \( -name 'build_all_blocks*' -o -name 'build_blocks*' \) -not -path '*/.*' 2>/dev/null
```

Also inspect likely locations if needed:

```bash
find . -maxdepth 4 \
  \( -name '*build*blocks*.py' -o -name '*all_blocks*.py' \) -not -path '*/.*'
ls -la scripts mobility/bus outputs 2>/dev/null
```

### 4.2 If no canonical build command is found

Stop the P5 regeneration step and report this explicitly.

Do **not**:

- fabricate a new build script;
- overwrite `outputs/all_blocks.parquet`;
- update bit-exact baseline as if regeneration happened;
- claim `compare_legacy_blocks.py` has validated the new parquet.

PR description must say:

```markdown
P5 parquet regeneration was not completed because the canonical all-blocks build command could not be located.
Current bit-exact coverage is deterministic self-consistency only.
outputs/all_blocks.parquet remains the pre-existing file.
```

### 4.3 If the canonical build command is found

Run the full regeneration workflow.

```bash
cp outputs/all_blocks.parquet outputs/all_blocks.parquet.legacy.bak
```

Then run the canonical build command. Use the discovered command, not a guessed one. Examples only:

```bash
python -m mobility.bus.build_all_blocks
# or
python scripts/build_all_blocks.py
# or whatever the discovered canonical command is
```

After the build completes, verify the file changed and is readable:

```bash
ls -lh outputs/all_blocks.parquet outputs/all_blocks.parquet.legacy.bak
python - <<'PY'
import pandas as pd
for path in ["outputs/all_blocks.parquet", "outputs/all_blocks.parquet.legacy.bak"]:
    df = pd.read_parquet(path)
    print(path, df.shape, df.columns[:10].tolist())
PY
```

Then run the legacy comparison script:

```bash
python scripts/compare_legacy_blocks.py \
  --legacy outputs/all_blocks.parquet.legacy.bak \
  --current outputs/all_blocks.parquet \
  --output outputs/inference_comparison.csv
```

If the script has a different CLI, use its actual CLI and document the command used.

Check output:

```bash
ls -lh outputs/inference_comparison.csv
python - <<'PY'
import pandas as pd
p = "outputs/inference_comparison.csv"
df = pd.read_csv(p)
print(df.head(20).to_string(index=False))
print("rows", len(df))
PY
```

### 4.4 Update bit-exact/baseline tests only after regeneration

If and only if `outputs/all_blocks.parquet` was actually regenerated:

- update `tests/mobility/bus/test_block_inference_bitexact.py` so it compares against the regenerated parquet baseline, not only deterministic self-consistency;
- explicitly pin the default `BlockInferenceConfig` values in the test to avoid default drift;
- keep a deterministic self-consistency test as an additional test if useful.

If regeneration was not completed, keep the deterministic self-consistency test and clearly mark true baseline comparison as deferred.

---

## 5. Verification commands

Run after code fixes:

```bash
pytest tests/mobility/bus/ tests/mobility/core/ -v
```

If parquet regeneration succeeded, also run:

```bash
python scripts/compare_legacy_blocks.py --legacy outputs/all_blocks.parquet.legacy.bak --current outputs/all_blocks.parquet --output outputs/inference_comparison.csv
python notebooks/_build_01_bus_narrative.py
jupyter nbconvert --to notebook --execute --inplace \
  notebooks/01_single_bus_simulation.ipynb \
  --ExecutePreprocessor.timeout=180
```

If the annual narrative builder was touched, also run its relevant builder or smoke test:

```bash
python notebooks/_build_03_bus_annual_narrative.py
```

---

## 6. PR description requirements

### If parquet regeneration succeeded

Include:

```markdown
## P5 parquet regeneration
- Backed up legacy parquet to `outputs/all_blocks.parquet.legacy.bak`.
- Regenerated `outputs/all_blocks.parquet` using: `<paste exact command>`.
- Ran `scripts/compare_legacy_blocks.py` and wrote `outputs/inference_comparison.csv`.
- Updated bit-exact baseline test against regenerated parquet.

Key comparison results:
- legacy rows: `<N>`
- current rows: `<N>`
- inferred block share: `<old>` -> `<new>`
- inferred time-infeasible rate: `<old>` -> `<new>`
- deadhead injected count/km: `<value>`
- fleet infeasible share, if audit run: `<value>`
```

### If parquet regeneration did not succeed

Include:

```markdown
## P5 parquet regeneration
Deferred. The canonical all-blocks build command could not be located during pre-flight.

Completed in this PR:
- P1 infer_blocks rewrite
- P2 deadhead injection
- P3 polygon LSOA attribution
- P4 feasibility audit
- P6 notebook Stage I integration
- deterministic self-consistency coverage for block inference

Not completed in this PR:
- `outputs/all_blocks.parquet` regeneration
- legacy vs regenerated comparison via `outputs/inference_comparison.csv`
- true regenerated-parquet bit-exact baseline

`outputs/all_blocks.parquet` remains the pre-existing file.
```

---

## 7. Acceptance criteria

The follow-up is complete when one of the two states below is reached.

### Preferred complete state

- `starts_below_min_required` includes pre-first-trip charging.
- empty centroid fallback is handled without raising.
- comments for `pool_bid` tie-break and shadow SOC charging policy are added.
- `pytest tests/mobility/bus/ tests/mobility/core/ -v` passes.
- canonical build command is found.
- `outputs/all_blocks.parquet` is backed up and regenerated.
- `outputs/inference_comparison.csv` is generated.
- bit-exact baseline is updated against regenerated parquet.
- PR description includes exact commands and comparison metrics.

### Acceptable deferred-P5 state

- all code fixes pass tests;
- canonical build command cannot be found after the specified search;
- no parquet overwrite is performed;
- PR description explicitly marks P5 regeneration as deferred;
- deterministic self-consistency test remains in place until the build command is confirmed.
