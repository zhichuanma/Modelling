# AGENTS.md

Local instructions for the `Modelling/` mobility simulation package. Also read
the workspace-level `../AGENTS.md`.

## Scope

`Modelling/` contains the active Python package for EV mobility and charging
simulation:

- `mobility/core/`: shared data structures, SOC simulation, spatial helpers,
  seasonal factors, and analysis utilities.
- `mobility/cars/`: private-car NTS schedule, destination, station, person-fleet,
  and year-schedule workflows.
- `mobility/bus/`: GTFS-derived bus block, depot, annual simulation, and depot
  load workflows.
- `mobility/coach/`: TransXChange-derived coach journey, annual schedule, and
  charging workflows.
- `scripts/`: command-line pipeline entrypoints.
- `tests/`: unit, stage, validation, script, bus, and coach tests.
- `notebooks/`: narrative and exploratory notebooks.
- `docs/`: current notes, archived prompts, review records, and next steps.

## Hard Modelling Rules

- `load_profile[step]` means average power in kW over the step, not energy.
- Energy per step is `load_profile[step] * STEP_HOURS`.
- Exported physical columns must carry units: `_kw`, `_kwh`, `_soc`, `_km`,
  `_h`, or `_min`.
- Keep random sampling deterministic. Prefer explicit `numpy.random.Generator`
  objects and stable seeds; do not use Python `hash()` for persistent sampling.
- Avoid non-deterministic dates in core simulation logic.
- Do not add `holidays`, `geopandas`, `shapely`, `pyproj`, `fiona`, or `pytz`
  to runtime dependencies without a reviewed dependency-change note.
- Large tabular outputs should be parquet unless they are small human-facing
  reports.

## Changes Requiring Confirmation

Ask before changing:

- SOC equations, charging semantics, warm-up behaviour, station matching,
  destination choice, sampling rules, or vehicle assignment logic.
- Bus/coach blocking, calendar interpretation, depot inference, deadhead
  assumptions, or output contracts.
- Existing constants in `mobility/core/constants.py`.
- Any script that overwrites `outputs/` or `data/`.
- Any notebook cell that changes the scientific interpretation rather than only
  improving narration or paths.

## Local Code Change Scope

Keep code edits inside `Modelling/` unless the user confirms a cross-subproject
change. Safe maintenance changes are limited to non-behavioural path/import
fixes, references to moved docs/configs/prompts, and smoke test fixes that do
not change simulator semantics. List every code change in the final summary.

Do not edit `Data/`, `Web/`, `SearchRealCost/`, or DCOPF code from a modelling
task unless the cross-subproject impact has been explained and confirmed.

## Commands

Install from `Modelling/`:

```bash
pip install -e .
```

Focused checks:

```bash
python -m pytest tests/mobility/stage_0 -q
python -m pytest tests/mobility/cars -q
python -m pytest tests/mobility/bus -q
python -m pytest tests/coach -q
```

Full tests may touch real local data and can be slow. Use targeted tests unless
the user requests a full validation pass.

## Outputs And Data

`data/`, `output/`, and `outputs/` are local/generated and ignored by git. Do not
delete or overwrite them during documentation cleanup. If a task needs a
regenerated parquet, record the exact command and compare against the previous
artifact when possible.

## Documentation And Prompts

- Current mobility docs live in `docs/`.
- Historical implementation prompts live in `docs/prompts/archive/`.
- Historical review/task records live in `docs/archive/reviews/`.
- Do not put new prompts under `notebooks/_prompts/`; use `docs/prompts/`.
