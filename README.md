# Modelling

`Modelling` is the standalone mobility simulation package extracted from the
larger `Nature_EV_2025` workspace. It contains the code used to build and run
EV mobility and charging simulations across multiple transport modes.

The repository is intentionally code-first: source files, package metadata, and
exploratory notebooks are versioned here, while large input datasets and
generated outputs stay local and are not tracked by git.

## What Is Included

- `mobility/core/`: shared data structures, SOC simulation, and analysis tools
- `mobility/cars/`: passenger-car workflows based on NTS-style trip chains
- `mobility/bus/`: bus-oriented preprocessing and fleet simulation helpers
- `mobility/coach/`: coach parsing utilities
- `notebooks/`: exploratory and workflow notebooks
- `pyproject.toml`: package metadata for editable installs

## Repository Layout

```text
Modelling/
|-- mobility/
|   |-- core/
|   |-- cars/
|   |-- bus/
|   `-- coach/
|-- notebooks/
|-- data/        # local only, not versioned
|-- output/      # local only, not versioned
|-- outputs/     # local only, not versioned
|-- pyproject.toml
`-- README.md
```

## Install

Create an environment and install the package in editable mode:

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -e .
```

Current package metadata declares:

- Python `>=3.10`
- `numpy`
- `pandas`

## Data And Outputs

Large files are intentionally excluded from the GitHub repository.

Expected local directories:

- `data/`: raw inputs and intermediate datasets
- `output/`: ad hoc local artifacts
- `outputs/`: generated simulation outputs

This keeps the repository lightweight and avoids pushing large CSV, parquet,
pickle, or numpy output files into git history.

## Conventions

The codebase follows a few important simulation conventions:

- `load_profile[step]` means average power in `kW` over the step, not energy
- energy per step is `load_profile[step] * STEP_HOURS`
- exported physical quantities use explicit unit suffixes such as `_kw`,
  `_kwh`, `_soc`, `_km`, `_h`, and `_min`

These conventions are documented in the package docstrings and should be
preserved when extending the model.

## Quick Start

Once installed, the top-level package re-exports the most common core and
passenger-car helpers:

```python
import mobility as em

print(em.STEPS_PER_DAY)
print(em.STEP_HOURS)
```

For workflow examples, start from the notebooks:

- `notebooks/01_data_exploration.ipynb`
- `notebooks/02_fleet_simulation.ipynb`
- `notebooks/03_bus_mobility.ipynb`
- `notebooks/04_coach_txc_to_trip_table.ipynb`

## Notes

- This repository is meant to stay maintainable as a standalone code package.
- If you need reproducible runs, keep local data preparation and output paths
  stable across machines.
- If you want to publish sample data later, prefer a small documented example
  dataset rather than committing full production-scale files.
