"""Stage 2c helpers for freezing per-person week-pattern libraries."""

from __future__ import annotations

import datetime as dt
import json

import numpy as np
import pandas as pd

from mobility.cars import holiday_rules

LIBRARY_COLUMNS = ["person_id", "pattern_id", "day_of_week", "chain_json"]
BUILD_REQUIRED_COLUMNS = [
    "IndividualID",
    "DayID",
    "TravDay",
    "JourSeq",
    "departure_time",
    "arrival_time",
    "distance_km",
    "purpose_from",
    "purpose_to",
    "SurveyYear",
]
LEISURE_PURPOSES = frozenset({"leisure", "holiday"})
EMPTY_CHAIN_JSON = "[]"

ChainEntry = tuple[float, float, float, str, str]
ChainTuple = list[ChainEntry]
WeekPattern = list[ChainTuple]
LibraryIndex = dict[str, list[WeekPattern]]
LeisurePoolIndex = dict[str, list[ChainEntry]]


def _to_string_series(values: pd.Series) -> pd.Series:
    return pd.Series(values, copy=False).astype("string[python]")


def _map_travday_to_day_of_week(trav_day: pd.Series) -> pd.Series:
    mapped = pd.to_numeric(trav_day, errors="coerce")
    if mapped.isna().any():
        raise ValueError("TravDay contains missing or non-numeric values")
    invalid = mapped[(mapped < 1) | (mapped > 7)]
    if not invalid.empty:
        raise ValueError("TravDay must stay within 1..7")
    return (mapped.astype("int64") - 1).astype("int64")


def _normalise_trip_chain(day_trips: pd.DataFrame) -> ChainTuple:
    ordered = day_trips.sort_values("JourSeq", kind="stable")
    chain: ChainTuple = []
    for row in ordered.itertuples(index=False):
        chain.append(
            (
                float(row.departure_time),
                float(row.arrival_time),
                float(row.distance_km),
                str(row.purpose_from),
                str(row.purpose_to),
            )
        )
    return chain


def _serialise_chain(chain: ChainTuple) -> str:
    payload = [
        [dep, arr, distance_km, purpose_from, purpose_to]
        for dep, arr, distance_km, purpose_from, purpose_to in chain
    ]
    return json.dumps(payload, separators=(",", ":"), ensure_ascii=True)


def _deserialise_chain(chain_json: str) -> ChainTuple:
    decoded = json.loads(chain_json)
    if not isinstance(decoded, list):
        raise ValueError("chain_json must decode to a list")

    chain: ChainTuple = []
    for leg in decoded:
        if not isinstance(leg, list) or len(leg) != 5:
            raise ValueError("Each chain leg must be a list of length 5")
        dep, arr, distance_km, purpose_from, purpose_to = leg
        chain.append(
            (
                float(dep),
                float(arr),
                float(distance_km),
                str(purpose_from),
                str(purpose_to),
            )
        )
    return chain


def _prepare_build_input(nts_trips: pd.DataFrame) -> pd.DataFrame:
    missing_columns = sorted(set(BUILD_REQUIRED_COLUMNS).difference(nts_trips.columns))
    if missing_columns:
        raise ValueError(f"nts_trips missing required columns: {missing_columns}")

    trips = nts_trips.loc[:, BUILD_REQUIRED_COLUMNS].copy()
    if trips.empty:
        return pd.DataFrame(
            {
                "person_id": pd.Series(dtype="string[python]"),
                "survey_year": pd.Series(dtype="int64"),
                "day_of_week": pd.Series(dtype="int64"),
                "DayID": pd.Series(dtype="string[python]"),
                "JourSeq": pd.Series(dtype="int64"),
                "departure_time": pd.Series(dtype="float64"),
                "arrival_time": pd.Series(dtype="float64"),
                "distance_km": pd.Series(dtype="float64"),
                "purpose_from": pd.Series(dtype="string[python]"),
                "purpose_to": pd.Series(dtype="string[python]"),
            }
        )

    trips["person_id"] = _to_string_series(trips["IndividualID"])
    trips["DayID"] = _to_string_series(trips["DayID"])
    trips["survey_year"] = pd.to_numeric(trips["SurveyYear"], errors="coerce")
    trips["JourSeq"] = pd.to_numeric(trips["JourSeq"], errors="coerce")
    trips["departure_time"] = pd.to_numeric(trips["departure_time"], errors="coerce")
    trips["arrival_time"] = pd.to_numeric(trips["arrival_time"], errors="coerce")
    trips["distance_km"] = pd.to_numeric(trips["distance_km"], errors="coerce")
    trips["purpose_from"] = _to_string_series(trips["purpose_from"])
    trips["purpose_to"] = _to_string_series(trips["purpose_to"])
    trips["day_of_week"] = _map_travday_to_day_of_week(trips["TravDay"])

    required_notna = [
        "person_id",
        "DayID",
        "survey_year",
        "JourSeq",
        "departure_time",
        "arrival_time",
        "distance_km",
        "purpose_from",
        "purpose_to",
        "day_of_week",
    ]
    if trips[required_notna].isna().any().any():
        raise ValueError("nts_trips contains missing values in required Stage 2c fields")

    trips["survey_year"] = trips["survey_year"].astype("int64")
    trips["JourSeq"] = trips["JourSeq"].astype("int64")
    trips["day_of_week"] = trips["day_of_week"].astype("int64")
    return trips


def build_person_week_library(nts_trips: pd.DataFrame) -> pd.DataFrame:
    """Freeze NTS person-day chains into a 7-row-per-pattern library."""
    trips = _prepare_build_input(nts_trips)
    if trips.empty:
        return pd.DataFrame(columns=LIBRARY_COLUMNS)

    trips = trips.sort_values(
        ["person_id", "survey_year", "day_of_week", "DayID", "JourSeq"],
        kind="stable",
    ).reset_index(drop=True)

    day_rows: list[dict[str, object]] = []
    for (person_id, survey_year, day_of_week, day_id), day_df in trips.groupby(
        ["person_id", "survey_year", "day_of_week", "DayID"],
        sort=False,
        dropna=False,
    ):
        day_rows.append(
            {
                "person_id": str(person_id),
                "survey_year": int(survey_year),
                "day_of_week": int(day_of_week),
                "day_id": str(day_id),
                "chain_json": _serialise_chain(_normalise_trip_chain(day_df)),
            }
        )

    day_level = pd.DataFrame(day_rows)
    day_level = day_level.sort_values(
        ["person_id", "survey_year", "day_of_week", "day_id"],
        kind="stable",
    ).reset_index(drop=True)
    day_level["pattern_ordinal"] = (
        day_level.groupby(["person_id", "survey_year", "day_of_week"], sort=False).cumcount()
    )

    library_rows: list[dict[str, object]] = []
    for person_id, person_df in day_level.groupby("person_id", sort=False):
        pattern_specs = (
            person_df.loc[:, ["survey_year", "pattern_ordinal"]]
            .drop_duplicates()
            .sort_values(["survey_year", "pattern_ordinal"], kind="stable")
            .reset_index(drop=True)
        )
        chain_lookup = {
            (int(row.survey_year), int(row.pattern_ordinal), int(row.day_of_week)): str(
                row.chain_json
            )
            for row in person_df.itertuples(index=False)
        }

        for pattern_id, pattern_row in enumerate(pattern_specs.itertuples(index=False)):
            survey_year = int(pattern_row.survey_year)
            pattern_ordinal = int(pattern_row.pattern_ordinal)
            for day_of_week in range(7):
                library_rows.append(
                    {
                        "person_id": str(person_id),
                        "pattern_id": int(pattern_id),
                        "day_of_week": int(day_of_week),
                        "chain_json": chain_lookup.get(
                            (survey_year, pattern_ordinal, day_of_week),
                            EMPTY_CHAIN_JSON,
                        ),
                    }
                )

    library_df = pd.DataFrame(library_rows, columns=LIBRARY_COLUMNS)
    library_df["person_id"] = _to_string_series(library_df["person_id"])
    library_df["pattern_id"] = library_df["pattern_id"].astype("int64")
    library_df["day_of_week"] = library_df["day_of_week"].astype("int64")
    library_df["chain_json"] = _to_string_series(library_df["chain_json"])
    return library_df.loc[:, LIBRARY_COLUMNS]


def _validate_library_df(library_df: pd.DataFrame) -> pd.DataFrame:
    missing_columns = sorted(set(LIBRARY_COLUMNS).difference(library_df.columns))
    if missing_columns:
        raise ValueError(f"library_df missing required columns: {missing_columns}")

    ordered = library_df.loc[:, LIBRARY_COLUMNS].copy()
    ordered["person_id"] = _to_string_series(ordered["person_id"])
    ordered["pattern_id"] = pd.to_numeric(ordered["pattern_id"], errors="coerce")
    ordered["day_of_week"] = pd.to_numeric(ordered["day_of_week"], errors="coerce")
    ordered["chain_json"] = _to_string_series(ordered["chain_json"])

    if ordered[["person_id", "pattern_id", "day_of_week", "chain_json"]].isna().any().any():
        raise ValueError("library_df contains missing values in frozen Stage 2c columns")

    ordered["pattern_id"] = ordered["pattern_id"].astype("int64")
    ordered["day_of_week"] = ordered["day_of_week"].astype("int64")
    invalid_days = ordered.loc[~ordered["day_of_week"].isin(range(7)), "day_of_week"]
    if not invalid_days.empty:
        raise ValueError("day_of_week must stay within 0..6")

    return ordered.sort_values(
        ["person_id", "pattern_id", "day_of_week"],
        kind="stable",
    ).reset_index(drop=True)


def build_library_index(library_df: pd.DataFrame) -> LibraryIndex:
    """Return {person_id: list[list[ChainTuple]]} for fast runtime sampling."""
    ordered = _validate_library_df(library_df)
    if ordered.empty:
        return {}

    group_sizes = ordered.groupby(["person_id", "pattern_id"], sort=False).size()
    if not (group_sizes == 7).all():
        raise ValueError("Each (person_id, pattern_id) group must have exactly 7 rows")

    library_index: LibraryIndex = {}
    for person_id, person_df in ordered.groupby("person_id", sort=False):
        patterns: list[WeekPattern] = []
        for _pattern_id, pattern_df in person_df.groupby("pattern_id", sort=False):
            if pattern_df["day_of_week"].tolist() != list(range(7)):
                raise ValueError("Each pattern must contain day_of_week 0..6 exactly once")
            patterns.append(
                [_deserialise_chain(chain_json) for chain_json in pattern_df["chain_json"].tolist()]
            )
        library_index[str(person_id)] = patterns

    return library_index


def build_leisure_pool_index(
    library_df: pd.DataFrame,
    *,
    library_index: LibraryIndex | None = None,
) -> LeisurePoolIndex:
    """Return {person_id: leisure-like chain entries} for holiday transforms."""
    if library_index is None:
        library_index = build_library_index(library_df)
    leisure_pool_index: LeisurePoolIndex = {}

    for person_id, patterns in library_index.items():
        leisure_entries: list[ChainEntry] = []
        for pattern in patterns:
            for day_chain in pattern:
                for leg in day_chain:
                    if leg[4] in LEISURE_PURPOSES:
                        leisure_entries.append(leg)
        leisure_pool_index[person_id] = leisure_entries

    return leisure_pool_index


def _jitter_chain_distances(
    chain: ChainTuple,
    rng: np.random.Generator,
    distance_jitter_pct: float,
) -> ChainTuple:
    jittered_chain: ChainTuple = []
    for dep, arr, distance_km, purpose_from, purpose_to in chain:
        distance_scale = 1.0 + float(
            rng.uniform(-distance_jitter_pct, distance_jitter_pct)
        )
        jittered_chain.append(
            (
                float(dep),
                float(arr),
                max(0.1, float(distance_km) * distance_scale),
                str(purpose_from),
                str(purpose_to),
            )
        )
    return jittered_chain


def _ensure_holiday_leisure_signal(
    chain: ChainTuple,
    leisure_pool: list[ChainEntry],
    rng: np.random.Generator,
) -> ChainTuple:
    """Inject one leisure-like leg so holiday transforms are visible on work-only days."""
    if not leisure_pool:
        return list(chain)
    if any(purpose_to in LEISURE_PURPOSES for *_rest, purpose_to in chain):
        return list(chain)

    sampled_index = int(rng.choice(len(leisure_pool)))
    return list(chain) + [tuple(leisure_pool[sampled_index])]


def sample_person_week(
    person_id: str,
    week_start: dt.date,
    library_index: LibraryIndex,
    leisure_pool_index: LeisurePoolIndex,
    rng: np.random.Generator,
    *,
    is_holiday_week: bool = False,
    distance_jitter_pct: float = 0.10,
) -> list[ChainTuple]:
    """Sample one frozen weekly pattern for a person and apply runtime jitter.

    Holiday-week behaviour
    ----------------------
    When ``is_holiday_week=True`` and the sampled day chain contains no
    leisure/holiday legs, a single leisure-like leg is drawn from
    ``leisure_pool_index[person_id]`` and appended BEFORE calling
    ``holiday_rules.apply_holiday_chain_transform``. This keeps Stage 2a's
    pure contract (``n_extra = original leisure/holiday count``) intact while
    ensuring work-only days still surface plausible holiday-week mobility.
    Consumes one extra rng draw per affected day.
    """
    _ = week_start
    if distance_jitter_pct < 0.0:
        raise ValueError("distance_jitter_pct must be non-negative")
    if person_id not in library_index:
        raise KeyError(f"person_id not found in library_index: {person_id}")

    person_patterns = library_index[person_id]
    if not person_patterns:
        raise ValueError(f"No week patterns available for person_id={person_id}")

    selected_pattern = person_patterns[int(rng.choice(len(person_patterns)))]
    leisure_pool = list(leisure_pool_index.get(person_id, []))
    sampled_week: list[ChainTuple] = []

    for day_chain in selected_pattern:
        jittered_chain = _jitter_chain_distances(day_chain, rng, distance_jitter_pct)
        if is_holiday_week:
            holiday_chain = _ensure_holiday_leisure_signal(jittered_chain, leisure_pool, rng)
            jittered_chain = holiday_rules.apply_holiday_chain_transform(
                holiday_chain,
                leisure_pool,
                rng,
            )
        sampled_week.append(jittered_chain)

    return sampled_week


__all__ = [
    "BUILD_REQUIRED_COLUMNS",
    "LIBRARY_COLUMNS",
    "build_library_index",
    "build_leisure_pool_index",
    "build_person_week_library",
    "sample_person_week",
]
