"""Stage 2c coverage for per-person week-pattern freezing and sampling."""

from __future__ import annotations

import datetime as dt
import importlib
import json

import numpy as np
import pandas as pd
import pytest

week_pattern_module = importlib.import_module("mobility.cars.week_pattern")

LIBRARY_COLUMNS = week_pattern_module.LIBRARY_COLUMNS
build_library_index = week_pattern_module.build_library_index
build_leisure_pool_index = week_pattern_module.build_leisure_pool_index
build_person_week_library = week_pattern_module.build_person_week_library
sample_person_week = week_pattern_module.sample_person_week


def _make_trip(
    person_id: str,
    survey_year: int,
    day_id: str,
    trav_day: int,
    jour_seq: int,
    departure_time: float,
    arrival_time: float,
    distance_km: float,
    purpose_from: str,
    purpose_to: str,
) -> dict[str, object]:
    return {
        "IndividualID": person_id,
        "DayID": day_id,
        "TravDay": trav_day,
        "JourSeq": jour_seq,
        "departure_time": departure_time,
        "arrival_time": arrival_time,
        "distance_km": distance_km,
        "purpose_from": purpose_from,
        "purpose_to": purpose_to,
        "SurveyYear": survey_year,
    }


@pytest.fixture()
def synthetic_nts_trips() -> pd.DataFrame:
    rows: list[dict[str, object]] = []

    for trav_day in range(1, 8):
        rows.append(
            _make_trip(
                "person_alpha",
                2024,
                f"alpha_{trav_day}",
                trav_day,
                1,
                7.0 + trav_day,
                7.5 + trav_day,
                10.0 + trav_day,
                "home",
                "work" if trav_day < 6 else "leisure",
            )
        )
        rows.append(
            _make_trip(
                "person_alpha",
                2024,
                f"alpha_{trav_day}",
                trav_day,
                2,
                17.0 + trav_day / 10.0,
                17.5 + trav_day / 10.0,
                8.0 + trav_day,
                "work" if trav_day < 6 else "leisure",
                "home",
            )
        )

    for trav_day in range(1, 6):
        rows.append(
            _make_trip(
                "person_beta",
                2024,
                f"beta_{trav_day}",
                trav_day,
                1,
                8.0,
                8.5,
                15.0,
                "home",
                "work",
            )
        )

    for survey_year in [2024, 2025]:
        for trav_day in [1, 2]:
            rows.append(
                _make_trip(
                    "person_gamma",
                    survey_year,
                    f"gamma_{survey_year}_{trav_day}",
                    trav_day,
                    1,
                    9.0,
                    10.0,
                    12.0 + survey_year - 2024,
                    "home",
                    "shopping" if trav_day == 1 else "social",
                )
            )
        rows.append(
            _make_trip(
                "person_gamma",
                survey_year,
                f"gamma_{survey_year}_6",
                6,
                1,
                14.0,
                15.0,
                18.0,
                "home",
                "holiday",
            )
        )

    return pd.DataFrame(rows)


def test_schema_frozen(synthetic_nts_trips: pd.DataFrame) -> None:
    library_df = build_person_week_library(synthetic_nts_trips)

    assert list(library_df.columns) == LIBRARY_COLUMNS
    assert set(library_df["day_of_week"]) == set(range(7))
    group_sizes = library_df.groupby(["person_id", "pattern_id"], sort=False).size()
    assert (group_sizes == 7).all()


def test_json_roundtrip(synthetic_nts_trips: pd.DataFrame) -> None:
    library_df = build_person_week_library(synthetic_nts_trips)
    non_empty_rows = library_df[library_df["chain_json"] != "[]"].head(10)

    assert len(non_empty_rows) == 10
    for chain_json in non_empty_rows["chain_json"]:
        decoded = json.loads(chain_json)
        assert isinstance(decoded, list)
        assert decoded
        for leg in decoded:
            assert len(leg) == 5
            assert isinstance(leg[0], float)
            assert isinstance(leg[1], float)
            assert isinstance(leg[2], float)
            assert isinstance(leg[3], str)
            assert isinstance(leg[4], str)


def test_sample_determinism(synthetic_nts_trips: pd.DataFrame) -> None:
    library_df = build_person_week_library(synthetic_nts_trips)
    library_index = build_library_index(library_df)
    leisure_pool_index = build_leisure_pool_index(library_df)

    observed_a = sample_person_week(
        "person_alpha",
        dt.date(2026, 1, 5),
        library_index,
        leisure_pool_index,
        np.random.default_rng(42),
    )
    observed_b = sample_person_week(
        "person_alpha",
        dt.date(2026, 1, 5),
        library_index,
        leisure_pool_index,
        np.random.default_rng(42),
    )

    assert observed_a == observed_b


def test_distance_jitter_bounds(synthetic_nts_trips: pd.DataFrame) -> None:
    library_df = build_person_week_library(synthetic_nts_trips)
    library_index = build_library_index(library_df)
    leisure_pool_index = build_leisure_pool_index(library_df)
    original_pattern = library_index["person_alpha"][0]

    ratios: list[float] = []
    for seed in range(1000):
        sampled_week = sample_person_week(
            "person_alpha",
            dt.date(2026, 1, 5),
            library_index,
            leisure_pool_index,
            np.random.default_rng(seed),
        )
        for original_day, sampled_day in zip(original_pattern, sampled_week, strict=True):
            for original_leg, sampled_leg in zip(original_day, sampled_day, strict=True):
                ratios.append(sampled_leg[2] / original_leg[2])

    assert min(ratios) >= 0.9
    assert max(ratios) <= 1.1
    assert float(np.mean(ratios)) == pytest.approx(1.0, abs=0.01)


def test_holiday_week_transforms() -> None:
    work_chain = [
        (8.0, 9.0, 10.0, "home", "work"),
        (9.5, 10.0, 5.0, "work", "work"),
        (17.0, 18.0, 10.0, "work", "home"),
    ]
    library_index = {"person_work": [[work_chain, [], [], [], [], [], []]]}
    leisure_pool_index = {
        "person_work": [(13.0, 14.0, 6.0, "home", "leisure")]
    }

    sampled_week = sample_person_week(
        "person_work",
        dt.date(2026, 8, 3),
        library_index,
        leisure_pool_index,
        np.random.default_rng(7),
        is_holiday_week=True,
    )

    transformed_day = sampled_week[0]
    original_work_trips = sum(1 for *_rest, purpose_to in work_chain if purpose_to == "work")
    transformed_work_trips = sum(1 for *_rest, purpose_to in transformed_day if purpose_to == "work")
    transformed_leisure_trips = sum(
        1 for *_rest, purpose_to in transformed_day if purpose_to == "leisure"
    )

    assert transformed_work_trips < original_work_trips
    assert transformed_leisure_trips > 0


def test_empty_pattern_handled(synthetic_nts_trips: pd.DataFrame) -> None:
    library_df = build_person_week_library(synthetic_nts_trips)
    library_index = build_library_index(library_df)
    leisure_pool_index = build_leisure_pool_index(library_df)

    sampled_week = sample_person_week(
        "person_beta",
        dt.date(2026, 1, 5),
        library_index,
        leisure_pool_index,
        np.random.default_rng(21),
    )

    assert sampled_week[5] == []
    assert sampled_week[6] == []
