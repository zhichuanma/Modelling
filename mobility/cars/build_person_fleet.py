"""CLI to freeze one EV-to-person binding table for Stage 2b."""

from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
import pandas as pd

from mobility.cars.person_fleet import (
    build_person_fleet,
    load_nts_persons,
    load_valid_individual_ids,
    write_person_fleet_parquet,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_EV_FLEET_PATH = REPO_ROOT / "Modelling" / "data" / "EV_UK_LSOA_2025_with_energy.csv"
DEFAULT_NTS_INDIVIDUAL_PATH = (
    REPO_ROOT
    / "Data"
    / "EV_behavior"
    / "UKDA-5340-stata"
    / "stata"
    / "stata13"
    / "individual_eul_2002-2024.dta"
)
DEFAULT_NTS_TRIPS_PATH = REPO_ROOT / "Modelling" / "data" / "trip_recent_filtered.csv"
DEFAULT_OUT_PATH = REPO_ROOT / "Modelling" / "data" / "person_fleet.parquet"


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ev-fleet", type=Path, default=DEFAULT_EV_FLEET_PATH)
    parser.add_argument("--nts-individual", type=Path, default=DEFAULT_NTS_INDIVIDUAL_PATH)
    parser.add_argument("--nts-trips", type=Path, default=DEFAULT_NTS_TRIPS_PATH)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT_PATH)
    parser.add_argument("--seed", type=int, default=42)
    return parser


def main() -> None:
    args = _build_parser().parse_args()
    rng = np.random.default_rng(args.seed)

    ev_fleet = pd.read_csv(args.ev_fleet)
    nts_persons = load_nts_persons(args.nts_individual, args.nts_trips)
    valid_individual_ids = load_valid_individual_ids(args.nts_trips)

    person_fleet = build_person_fleet(
        ev_fleet=ev_fleet,
        nts_persons=nts_persons,
        valid_individual_ids=valid_individual_ids,
        rng=rng,
    )
    write_person_fleet_parquet(person_fleet, args.out)

    print(f"Wrote {len(person_fleet):,} rows to {args.out}")
    region_share = (
        person_fleet["nts_region"]
        .fillna("<NA>")
        .value_counts(normalize=True, dropna=False)
        .sort_index()
    )
    print("Region share:")
    for region, share in region_share.items():
        print(f"  {region}: {share:.4%}")


if __name__ == "__main__":
    main()
