from __future__ import annotations

from pathlib import Path

import pandas as pd

from mobility.coach.data_loader import (
    build_all_coach_tables,
    load_all_coach_journeys,
    load_all_coach_stop_sequences,
    summarize_journey_quality,
)


FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures"


def test_data_loader_builds_and_reads_parquet(tmp_path) -> None:
    inventory = tmp_path / "inventory.csv"
    inventory.write_text(
        "NationalOperatorCode,OperatorShortName,LineName,ServiceCode,OutboundDescription,InboundDescription,"
        "TotalStopPoints,CustomStopPoints,RouteSections,Routes,JourneyPatternSections,VehicleJourneys,"
        "FilePath,ServiceStartDate,ServiceEndDate,EventService\n"
        "BHAT,New Bharat Coaches,NB1,UZ000BHAT:NB1,London - Leicester,Leicester - London,"
        "3,0,2,2,2,2,coach_bhat_nb1.xml,2026-04-17,2027-04-17,False\n",
        encoding="utf-8",
    )
    stops = pd.DataFrame(
        {
            "stop_point_ref": ["490002205ZC", "03700337", "269030094"],
            "lat": [51.505, 51.51, 52.636],
            "lon": [-0.36, -0.59, -1.13],
            "source": ["test", "test", "test"],
        }
    )

    journeys, stop_sequences = build_all_coach_tables(inventory, FIXTURE_DIR, stops_geom=stops)
    journeys_path = tmp_path / "journeys.parquet"
    stops_path = tmp_path / "stops.parquet"
    journeys.to_parquet(journeys_path, index=False)
    stop_sequences.to_parquet(stops_path, index=False)

    loaded_journeys = load_all_coach_journeys(journeys_path)
    loaded_stops = load_all_coach_stop_sequences(stops_path)
    quality = summarize_journey_quality(loaded_journeys)

    assert len(loaded_journeys) == 2
    assert loaded_journeys["distance_km"].notna().all()
    assert loaded_stops["journey_id"].nunique() == 2
    assert quality.loc[0, "known_distance_journeys"] == 2
