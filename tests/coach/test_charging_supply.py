from __future__ import annotations

from pathlib import Path

import pandas as pd

from mobility.coach.charging_supply import eligible_lsoa_kw, load_coach_eligible_stations


def test_load_coach_eligible_stations_filters_and_aggregates(tmp_path: Path) -> None:
    path = tmp_path / "ocm.csv"
    pd.DataFrame(
        {
            "StationID": [1, 2, 3],
            "lsoa_code": ["E01", "E01", "E01"],
            "TotalCapacity_kW": [30.0, 60.0, 150.0],
            "Bands": ["Fast (8-49kW)", "Rapid (50-149 kW)", "Rapid (50-149 kW)"],
        }
    ).to_csv(path, index=False)

    stations = load_coach_eligible_stations(path)
    capacity = eligible_lsoa_kw(stations)

    assert stations["StationID"].tolist() == [2, 3]
    assert float(capacity.loc["E01"]) == 210.0
