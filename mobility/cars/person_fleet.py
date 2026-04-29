"""Stage 2b helpers for freezing EV-to-person bindings."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Iterable

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_ONSPD_PATH = REPO_ROOT / "Data" / "Units" / "ONSPD_MAY_2025_UK.csv"
DEFAULT_HOUSEHOLD_DTA_NAME = "household_eul_2002-2024.dta"

NTS_GOR_CODE_TO_REGION = {
    1: "north_east",
    2: "north_west",
    3: "yorkshire_and_the_humber",
    4: "east_midlands",
    5: "west_midlands",
    6: "east_of_england",
    7: "london",
    8: "south_east",
    9: "south_west",
    10: "wales",
    11: "scotland",
}

ONSPD_RGN_CODE_TO_REGION = {
    "E12000001": "north_east",
    "E12000002": "north_west",
    "E12000003": "yorkshire_and_the_humber",
    "E12000004": "east_midlands",
    "E12000005": "west_midlands",
    "E12000006": "east_of_england",
    "E12000007": "london",
    "E12000008": "south_east",
    "E12000009": "south_west",
}

ONSPD_CTRY_CODE_TO_REGION = {
    "S92000003": "scotland",
    "W92000004": "wales",
    "N92000002": "ni",
}

REGION_NAME_ALIASES = {
    "north east": "north_east",
    "north_east": "north_east",
    "north-east": "north_east",
    "north west": "north_west",
    "north_west": "north_west",
    "north-west": "north_west",
    "yorkshire and the humber": "yorkshire_and_the_humber",
    "yorkshire_and_the_humber": "yorkshire_and_the_humber",
    "east midlands": "east_midlands",
    "east_midlands": "east_midlands",
    "west midlands": "west_midlands",
    "west_midlands": "west_midlands",
    "east of england": "east_of_england",
    "east_of_england": "east_of_england",
    "london": "london",
    "south east": "south_east",
    "south_east": "south_east",
    "south-east": "south_east",
    "south west": "south_west",
    "south_west": "south_west",
    "south-west": "south_west",
    "wales": "wales",
    "scotland": "scotland",
    "ni": "ni",
    "northern ireland": "ni",
    "northern_ireland": "ni",
    "northern-ireland": "ni",
}

PERSON_FLEET_COLUMNS = ["ev_id", "person_id", "nts_household_id", "nts_region"]


def _to_string_series(values: Iterable[object]) -> pd.Series:
    return pd.Series(values, copy=False).astype("string[python]")


def _normalise_region_name(value: object) -> str | None:
    if pd.isna(value):
        return None

    if isinstance(value, (np.integer, int)):
        return NTS_GOR_CODE_TO_REGION.get(int(value))

    if isinstance(value, (np.floating, float)):
        if np.isnan(value):
            return None
        if float(value).is_integer():
            return NTS_GOR_CODE_TO_REGION.get(int(value))

    text = str(value).strip().lower()
    if text in {"", "nan", "<na>", "none"}:
        return None
    if text.isdigit():
        return NTS_GOR_CODE_TO_REGION.get(int(text))
    return REGION_NAME_ALIASES.get(text, text.replace(" ", "_"))


def _first_non_null_string(values: pd.Series) -> str | pd.NA:
    for value in values:
        normalised = _normalise_region_name(value)
        if normalised is not None:
            return normalised
    return pd.NA


def _prepare_person_pool(
    nts_persons: pd.DataFrame,
    valid_individual_ids: set[str],
) -> pd.DataFrame:
    required_cols = {"IndividualID", "HouseholdID", "W5", "nts_region"}
    missing = required_cols.difference(nts_persons.columns)
    if missing:
        raise ValueError(f"nts_persons missing required columns: {sorted(missing)}")

    persons = nts_persons.loc[:, ["IndividualID", "HouseholdID", "W5", "nts_region"]].copy()
    persons["IndividualID"] = _to_string_series(persons["IndividualID"])
    persons["HouseholdID"] = _to_string_series(persons["HouseholdID"])
    persons["W5"] = pd.to_numeric(persons["W5"], errors="coerce")
    persons["nts_region"] = _to_string_series(
        [_normalise_region_name(value) for value in persons["nts_region"]]
    )

    persons = persons[persons["IndividualID"].isin(valid_individual_ids)]
    persons = persons[persons["W5"].notna() & (persons["W5"] > 0.0)]
    if persons.empty:
        raise ValueError("No eligible NTS persons remain after trip/W5 filtering")

    persons = (
        persons.groupby("IndividualID", as_index=False, sort=False)
        .agg(
            HouseholdID=("HouseholdID", "first"),
            W5=("W5", "mean"),
            nts_region=("nts_region", _first_non_null_string),
        )
    )
    persons["IndividualID"] = persons["IndividualID"].astype("string[python]")
    persons["HouseholdID"] = persons["HouseholdID"].astype("string[python]")
    persons["nts_region"] = persons["nts_region"].astype("string[python]")
    return persons


@lru_cache(maxsize=1)
def _load_lsoa_region_lookup(onspd_path: str | Path = DEFAULT_ONSPD_PATH) -> dict[str, str]:
    lookup: dict[str, str] = {}
    for chunk in pd.read_csv(
        Path(onspd_path),
        usecols=["lsoa21", "rgn", "ctry"],
        dtype="string",
        chunksize=200_000,
    ):
        region = chunk["rgn"].map(ONSPD_RGN_CODE_TO_REGION)
        country_region = chunk["ctry"].map(ONSPD_CTRY_CODE_TO_REGION)
        resolved = region.where(region.notna(), country_region)
        chunk = chunk.assign(resolved_region=resolved)
        chunk = chunk.dropna(subset=["lsoa21", "resolved_region"]).drop_duplicates("lsoa21")
        lookup.update(
            dict(
                zip(
                    chunk["lsoa21"].astype(str),
                    chunk["resolved_region"].astype(str),
                )
            )
        )
    return lookup


def _resolve_ev_regions(ev_fleet: pd.DataFrame) -> np.ndarray:
    if "LSOA_code" not in ev_fleet.columns:
        raise ValueError("ev_fleet must include LSOA_code")

    lsoa_region_lookup = _load_lsoa_region_lookup()
    ev_lsoas = ev_fleet["LSOA_code"].fillna("").astype(str).to_numpy(dtype=object)
    return np.array(
        [lsoa_region_lookup.get(lsoa_code, None) for lsoa_code in ev_lsoas],
        dtype=object,
    )


def _sample_from_pool(
    person_ids: np.ndarray,
    household_ids: np.ndarray,
    nts_regions: np.ndarray,
    probabilities: np.ndarray,
    sample_size: int,
    rng: np.random.Generator,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    draws = rng.choice(len(person_ids), size=sample_size, replace=True, p=probabilities)
    return person_ids[draws], household_ids[draws], nts_regions[draws]


def build_person_fleet(
    ev_fleet: pd.DataFrame,
    nts_persons: pd.DataFrame,
    valid_individual_ids: set[str],
    rng: np.random.Generator,
) -> pd.DataFrame:
    """Sample one NTS person for each EV, region-aligned where possible."""
    required_ev_cols = {"EV_ID", "LSOA_code"}
    missing_ev_cols = required_ev_cols.difference(ev_fleet.columns)
    if missing_ev_cols:
        raise ValueError(f"ev_fleet missing required columns: {sorted(missing_ev_cols)}")

    prepared_valid_ids = {str(individual_id) for individual_id in valid_individual_ids}
    persons = _prepare_person_pool(nts_persons, prepared_valid_ids)
    ev_regions = _resolve_ev_regions(ev_fleet)

    global_person_ids = persons["IndividualID"].to_numpy(dtype=object)
    global_household_ids = persons["HouseholdID"].to_numpy(dtype=object)
    global_nts_regions = persons["nts_region"].to_numpy(dtype=object)
    global_weights = persons["W5"].to_numpy(dtype=float)
    global_probabilities = global_weights / global_weights.sum()

    sampled_person_ids = np.empty(len(ev_fleet), dtype=object)
    sampled_household_ids = np.empty(len(ev_fleet), dtype=object)
    sampled_nts_regions = np.empty(len(ev_fleet), dtype=object)
    assigned_mask = np.zeros(len(ev_fleet), dtype=bool)

    grouped_persons = {
        region: region_df.reset_index(drop=True)
        for region, region_df in persons.dropna(subset=["nts_region"]).groupby("nts_region", sort=False)
    }

    for region, region_df in grouped_persons.items():
        ev_indices = np.flatnonzero(ev_regions == region)
        if len(ev_indices) == 0:
            continue

        region_person_ids = region_df["IndividualID"].to_numpy(dtype=object)
        region_household_ids = region_df["HouseholdID"].to_numpy(dtype=object)
        region_nts_regions = region_df["nts_region"].to_numpy(dtype=object)
        region_weights = region_df["W5"].to_numpy(dtype=float)
        region_probabilities = region_weights / region_weights.sum()

        (
            sampled_person_ids[ev_indices],
            sampled_household_ids[ev_indices],
            sampled_nts_regions[ev_indices],
        ) = _sample_from_pool(
            region_person_ids,
            region_household_ids,
            region_nts_regions,
            region_probabilities,
            len(ev_indices),
            rng,
        )
        assigned_mask[ev_indices] = True

    fallback_indices = np.flatnonzero(~assigned_mask)
    if len(fallback_indices) > 0:
        (
            sampled_person_ids[fallback_indices],
            sampled_household_ids[fallback_indices],
            sampled_nts_regions[fallback_indices],
        ) = _sample_from_pool(
            global_person_ids,
            global_household_ids,
            global_nts_regions,
            global_probabilities,
            len(fallback_indices),
            rng,
        )

    result = pd.DataFrame(
        {
            "ev_id": _to_string_series(ev_fleet["EV_ID"]),
            "person_id": _to_string_series(sampled_person_ids),
            "nts_household_id": _to_string_series(sampled_household_ids),
            "nts_region": _to_string_series(sampled_nts_regions),
        }
    )
    return result.loc[:, PERSON_FLEET_COLUMNS]


def load_valid_individual_ids(nts_trips_path: Path | str) -> set[str]:
    trip_ids = pd.read_csv(
        nts_trips_path,
        usecols=["IndividualID"],
        dtype={"IndividualID": "string"},
    )
    return set(trip_ids["IndividualID"].dropna().astype(str))


def _load_trip_level_person_weights(nts_trips_path: Path | str) -> pd.DataFrame:
    trips = pd.read_csv(
        nts_trips_path,
        usecols=["IndividualID", "W5"],
        dtype={"IndividualID": "string"},
    )
    trips["W5"] = pd.to_numeric(trips["W5"], errors="coerce")
    trips = trips[trips["W5"].notna() & (trips["W5"] > 0.0)]
    return trips.groupby("IndividualID", as_index=False, sort=False)["W5"].mean()


def _load_household_regions(household_dta_path: Path) -> pd.DataFrame:
    households = pd.read_stata(
        household_dta_path,
        convert_categoricals=False,
        columns=["HouseholdID", "HHoldGOR_B02ID"],
    )
    households["HouseholdID"] = _to_string_series(households["HouseholdID"])
    households["nts_region"] = _to_string_series(
        [_normalise_region_name(value) for value in households["HHoldGOR_B02ID"]]
    )
    return households.loc[:, ["HouseholdID", "nts_region"]].drop_duplicates("HouseholdID")


def load_nts_persons(
    nts_individual_path: Path | str,
    nts_trips_path: Path | str,
) -> pd.DataFrame:
    """Load a person pool for binding.

    The Stage-2b prompt asks for ``W5`` and ``GORAlt_B02ID`` from the raw
    individual file, but the current local extract only exposes
    ``IndividualID``, ``HouseholdID`` and ``IndWkGOR_B02ID``. We therefore:

    1. read the raw individual file for stable person/household IDs;
    2. use raw ``W5`` / ``GORAlt_B02ID`` when present;
    3. otherwise fall back to trip-level mean ``W5`` and household-region
       ``HHoldGOR_B02ID`` from the sibling household file.
    """
    nts_individual_path = Path(nts_individual_path)
    reader = pd.io.stata.StataReader(nts_individual_path, convert_categoricals=False)
    available_columns = set(reader.varlist)

    selected_columns = ["IndividualID", "HouseholdID"]
    for optional_column in ["W5", "GORAlt_B02ID", "IndWkGOR_B02ID"]:
        if optional_column in available_columns:
            selected_columns.append(optional_column)

    persons = pd.read_stata(
        nts_individual_path,
        convert_categoricals=False,
        columns=selected_columns,
    )
    persons["IndividualID"] = _to_string_series(persons["IndividualID"])
    persons["HouseholdID"] = _to_string_series(persons["HouseholdID"])

    if "W5" in persons.columns:
        persons["W5"] = pd.to_numeric(persons["W5"], errors="coerce")
    else:
        persons = persons.merge(
            _load_trip_level_person_weights(nts_trips_path),
            on="IndividualID",
            how="left",
        )

    if "GORAlt_B02ID" in persons.columns:
        persons["nts_region"] = _to_string_series(
            [_normalise_region_name(value) for value in persons["GORAlt_B02ID"]]
        )
    else:
        household_path = nts_individual_path.with_name(DEFAULT_HOUSEHOLD_DTA_NAME)
        if household_path.exists():
            persons = persons.merge(
                _load_household_regions(household_path),
                on="HouseholdID",
                how="left",
            )
        else:
            persons["nts_region"] = pd.Series(pd.NA, index=persons.index, dtype="string[python]")

        if "IndWkGOR_B02ID" in persons.columns:
            work_region = _to_string_series(
                [_normalise_region_name(value) for value in persons["IndWkGOR_B02ID"]]
            )
            persons["nts_region"] = persons["nts_region"].fillna(work_region)

    return persons.loc[:, ["IndividualID", "HouseholdID", "W5", "nts_region"]]


def write_person_fleet_parquet(person_fleet: pd.DataFrame, out_path: Path | str) -> None:
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    ordered = person_fleet.loc[:, PERSON_FLEET_COLUMNS].copy()
    for column in PERSON_FLEET_COLUMNS:
        ordered[column] = ordered[column].astype("string[python]")
    table = pa.Table.from_pandas(ordered, preserve_index=False)
    pq.write_table(table, out_path, compression="zstd")


__all__ = [
    "PERSON_FLEET_COLUMNS",
    "build_person_fleet",
    "load_nts_persons",
    "load_valid_individual_ids",
    "write_person_fleet_parquet",
]
