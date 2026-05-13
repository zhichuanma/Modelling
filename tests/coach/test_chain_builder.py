from __future__ import annotations

import datetime as dt

import pandas as pd

from mobility.coach.chain_builder import build_coach_chains


ACTIVE_DATE = dt.date(2026, 5, 11)


def _journeys(*, second_start_h: float, second_start_lon: float = -0.101) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "journey_id": ["J1", "J2"],
            "operator_code": ["OP"] * 2,
            "start_h": [8.0, second_start_h],
            "end_h": [10.0, second_start_h + 1.0],
            "start_lat": [51.500, 51.501],
            "start_lon": [-0.100, second_start_lon],
            "end_lat": [51.501, 51.502],
            "end_lon": [-0.101, second_start_lon + 0.001],
        }
    )


def _date_index() -> pd.DataFrame:
    return pd.DataFrame({"journey_id": ["J1", "J2"], "date": [ACTIVE_DATE, ACTIVE_DATE]})


def test_non_overlapping_nearby_journeys_share_one_chain() -> None:
    chains = build_coach_chains(_journeys(second_start_h=10.75), _date_index())

    assert chains["coach_chain_id"].nunique() == 1
    assert chains["position_in_chain"].tolist() == [1, 2]
    assert chains["coach_chain_id"].iloc[0] == "OP_2026-05-11_001"


def test_overlapping_journeys_split_into_two_chains() -> None:
    chains = build_coach_chains(_journeys(second_start_h=9.75), _date_index())

    assert chains["coach_chain_id"].nunique() == 2
    assert chains["position_in_chain"].tolist() == [1, 1]


def test_far_relocation_splits_into_two_chains() -> None:
    chains = build_coach_chains(
        _journeys(second_start_h=10.75, second_start_lon=-3.0),
        _date_index(),
        max_relocation_km=10.0,
    )

    assert chains["coach_chain_id"].nunique() == 2
    template_ids = set(chains["coach_chain_template_id"])
    assert len(template_ids) == 2
    assert all(template_id.startswith("OP_") for template_id in template_ids)


def test_template_id_uses_journey_set_not_daily_chain_index() -> None:
    d1 = ACTIVE_DATE
    d2 = ACTIVE_DATE + dt.timedelta(days=1)
    journeys = pd.DataFrame(
        {
            "journey_id": ["J1", "J2", "J3"],
            "operator_code": ["OP"] * 3,
            "start_h": [8.0, 11.0, 11.0],
            "end_h": [10.0, 12.0, 12.0],
            "start_lat": [51.500, 51.501, 51.501],
            "start_lon": [-0.100, -0.101, -0.101],
            "end_lat": [51.501, 51.502, 51.502],
            "end_lon": [-0.101, -0.102, -0.102],
        }
    )
    date_index = pd.DataFrame(
        {
            "journey_id": ["J1", "J2", "J1", "J3"],
            "date": [d1, d1, d2, d2],
        }
    )

    chains = build_coach_chains(journeys, date_index)
    per_date = chains.groupby("date").agg(
        chain_id=("coach_chain_id", "first"),
        template_id=("coach_chain_template_id", "first"),
    )

    assert per_date.loc[d1, "chain_id"].endswith("_001")
    assert per_date.loc[d2, "chain_id"].endswith("_001")
    assert per_date.loc[d1, "template_id"] != per_date.loc[d2, "template_id"]
