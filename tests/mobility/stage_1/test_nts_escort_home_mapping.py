"""Stage 1 fix coverage for NTS escort-home purpose decoding."""

from __future__ import annotations

import importlib

import pandas as pd

data_loader = importlib.import_module("mobility.cars.data_loader")

NTS_PURPOSE_MAP = data_loader.NTS_PURPOSE_MAP


def test_nts_code_17_and_23_labels_are_distinct() -> None:
    assert NTS_PURPOSE_MAP[17] == "personal_business"
    assert NTS_PURPOSE_MAP[23] == "home"


def test_synthetic_nts_trip_rows_map_escort_home_and_home_correctly() -> None:
    df = pd.DataFrame(
        {
            "TripPurpFrom_B01ID": [1, 1],
            "TripPurpTo_B01ID": [17, 23],
        }
    )

    df["purpose_from"] = df["TripPurpFrom_B01ID"].map(NTS_PURPOSE_MAP).fillna("other")
    df["purpose_to"] = df["TripPurpTo_B01ID"].map(NTS_PURPOSE_MAP).fillna("other")

    assert df.loc[0, "purpose_from"] == "work"
    assert df.loc[0, "purpose_to"] == "personal_business"
    assert df.loc[1, "purpose_to"] == "home"
