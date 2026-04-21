"""Load and preprocess the three core datasets:
   - NTS trip diary  (trip_recent_filtered.csv)
   - EV fleet        (EV_UK_LSOA_2025.csv)
   - Charging stations (UK_OCM_stations_labeled.csv + LSOA mapping)
"""

from pathlib import Path
from typing import Tuple

import pandas as pd

# ---------------------------------------------------------------------------
# NTS Trip Purpose Code → readable label  (TripPurpFrom/To_B01ID)
# Source: 5340_nts_lookup_table_response_levels
# ---------------------------------------------------------------------------
NTS_PURPOSE_MAP = {
    1:  "work",                 # Work
    2:  "work",                 # In course of work
    3:  "education",            # Education
    4:  "shopping",             # Food shopping
    5:  "shopping",             # Non food shopping
    6:  "personal_business",    # Personal business medical
    7:  "personal_business",    # Personal business eat/drink
    8:  "personal_business",    # Personal business other
    9:  "social",               # Eat/drink with friends
    10: "social",               # Visit friends
    11: "social",               # Other social
    12: "leisure",              # Entertain / public activity
    13: "leisure",              # Sport: participate
    14: "holiday",              # Holiday: base
    15: "leisure",              # Day trip / just walk
    16: "leisure",              # Other non-escort
    17: "home",                 # Escort home
    18: "work",                 # Escort work
    19: "work",                 # Escort in course of work
    20: "education",            # Escort education
    21: "shopping",             # Escort shopping / personal business
    22: "social",               # Other escort
    23: "home",                 # Home
}

MILES_TO_KM = 1.609344


def _default_data_dir() -> Path:
    """Return the shared data directory at Modelling/data/."""
    return Path(__file__).resolve().parents[2] / "data"


# ---- NTS trips -----------------------------------------------------------

def load_nts_trips(data_dir: Path | None = None) -> pd.DataFrame:
    """Load and preprocess the NTS trip diary.

    Returns a DataFrame with columns:
        IndividualID, DayID, TravDay, JourSeq,
        departure_time, arrival_time, distance_km,
        purpose_from, purpose_to, day_type, W5, SurveyYear
    """
    data_dir = data_dir or _default_data_dir()
    df = pd.read_csv(data_dir / "trip_recent_filtered.csv")

    # Convert distance: miles → km
    df["distance_km"] = df["TripDisExSW"] * MILES_TO_KM

    # Convert time to decimal hours
    df["departure_time"] = df["TripStartHours"] + df["TripStartMinutes"] / 60.0
    df["arrival_time"] = df["TripEndHours"] + df["TripEndMinutes"] / 60.0

    # Map purpose codes to labels
    df["purpose_from"] = df["TripPurpFrom_B01ID"].map(NTS_PURPOSE_MAP).fillna("other")
    df["purpose_to"] = df["TripPurpTo_B01ID"].map(NTS_PURPOSE_MAP).fillna("other")

    # Day type: TravDay 1-5 = weekday, 6-7 = weekend
    df["day_type"] = df["TravDay"].apply(lambda x: "weekday" if x <= 5 else "weekend")

    # Drop rows with missing time
    df = df.dropna(subset=["departure_time", "arrival_time"])

    keep = [
        "IndividualID", "DayID", "TravDay", "JourSeq",
        "departure_time", "arrival_time", "distance_km",
        "purpose_from", "purpose_to", "day_type", "W5", "SurveyYear",
    ]
    return df[keep].sort_values(["IndividualID", "DayID", "JourSeq"]).reset_index(drop=True)


# ---- EV fleet ------------------------------------------------------------

def load_ev_fleet(data_dir: Path | None = None) -> pd.DataFrame:
    """Load EV fleet data.

    Prefers EV_UK_LSOA_2025_with_energy.csv (has per-model efficiency) and
    falls back to EV_UK_LSOA_2025.csv if not present.

    Returns a DataFrame with columns:
        EV_ID, LSOA_code, LAD, Model, count,
        battery_capacity_kwh, dc_power_kw, ac_power_kw,
        consumption_kwh_per_km, efficiency_source
    """
    data_dir = data_dir or _default_data_dir()
    enriched = data_dir / "EV_UK_LSOA_2025_with_energy.csv"
    path = enriched if enriched.exists() else data_dir / "EV_UK_LSOA_2025.csv"
    df = pd.read_csv(path)

    df = df.rename(columns={
        "Energy_kWh":  "battery_capacity_kwh",
        "DC_Power_kW": "dc_power_kw",
        "AC_Power_kW": "ac_power_kw",
    })

    if "efficiency_wh_per_km" in df.columns:
        df["consumption_kwh_per_km"] = df["efficiency_wh_per_km"] / 1000.0
    return df


# ---- Charging stations ---------------------------------------------------

def load_stations(
    data_dir: Path | None = None,
    lsoa_file: Path | None = None,
) -> pd.DataFrame:
    """Load labeled charging stations and attach LSOA codes.

    Parameters
    ----------
    data_dir : path to the ev_mobility directory (contains UK_OCM_stations_labeled.csv)
    lsoa_file : path to UK_OCM_connectors_expanded_with_bus_and_LAD_LSOA.csv
                (defaults to ../../Data/Charging_stations/...)

    Returns a DataFrame with columns:
        StationID, Latitude, Longitude, TotalCapacity_kW,
        StationType, label, lsoa_code
    """
    data_dir = data_dir or _default_data_dir()
    df = pd.read_csv(data_dir / "UK_OCM_stations_labeled.csv")

    # Attach LSOA codes from the connector-level file
    if lsoa_file is None:
        lsoa_file = data_dir.parent.parent / "Data" / "Charging_stations" / \
                     "UK_OCM_connectors_expanded_with_bus_and_LAD_LSOA.csv"
    if lsoa_file.exists():
        lsoa_map = (
            pd.read_csv(lsoa_file, usecols=["StationID", "lsoa_code"])
            .drop_duplicates("StationID")
        )
        df = df.merge(lsoa_map, on="StationID", how="left")
    else:
        df["lsoa_code"] = None

    keep = [
        "StationID", "Latitude", "Longitude", "TotalCapacity_kW",
        "StationType", "label", "lsoa_code",
    ]
    return df[keep]


# ---- Convenience ----------------------------------------------------------

def load_all(
    data_dir: Path | None = None,
) -> Tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Load all three datasets. Returns (trips, ev_fleet, stations)."""
    data_dir = data_dir or _default_data_dir()
    return (
        load_nts_trips(data_dir),
        load_ev_fleet(data_dir),
        load_stations(data_dir),
    )
