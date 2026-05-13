from __future__ import annotations

import datetime as dt
from pathlib import Path

import pandas as pd

from mobility.coach.calendar import (
    COACH_FEED_YEAR_END,
    COACH_FEED_YEAR_START,
    PROFILE_SOURCE_FALLBACK,
    PROFILE_SOURCE_TXC,
    build_journey_date_index,
    parse_operating_profile,
)


def _write_txc(path: Path) -> None:
    path.write_text(
        """<?xml version="1.0" encoding="utf-8"?>
        <TransXChange xmlns="http://www.transxchange.org.uk/">
          <Services>
            <Service>
              <OperatingPeriod>
                <StartDate>2026-05-01</StartDate>
                <EndDate>2026-05-15</EndDate>
              </OperatingPeriod>
            </Service>
          </Services>
          <VehicleJourneys>
            <VehicleJourney>
              <OperatingProfile>
                <RegularDayType>
                  <DaysOfWeek><Monday /></DaysOfWeek>
                </RegularDayType>
                <SpecialDaysOperation>
                  <DaysOfNonOperation>
                    <BankHolidays><AllBankHolidays /></BankHolidays>
                  </DaysOfNonOperation>
                </SpecialDaysOperation>
              </OperatingProfile>
              <VehicleJourneyCode>VJ1</VehicleJourneyCode>
            </VehicleJourney>
            <VehicleJourney>
              <VehicleJourneyCode>VJ2</VehicleJourneyCode>
            </VehicleJourney>
          </VehicleJourneys>
        </TransXChange>
        """,
        encoding="utf-8",
    )


def test_parse_operating_profile_weekday_and_bank_holiday_exclusion(tmp_path: Path) -> None:
    xml_path = tmp_path / "coach.xml"
    _write_txc(xml_path)

    profiles = parse_operating_profile(xml_path)

    assert dt.date(2026, 5, 4) not in profiles["VJ1"]
    assert dt.date(2026, 5, 11) in profiles["VJ1"]
    assert min(profiles["VJ1"]) >= COACH_FEED_YEAR_START
    assert max(profiles["VJ1"]) <= COACH_FEED_YEAR_END


def test_build_journey_date_index_keeps_profile_source_enum(tmp_path: Path) -> None:
    xml_path = tmp_path / "coach.xml"
    _write_txc(xml_path)
    journeys = pd.DataFrame(
        {
            "journey_id": ["coach.xml::VJ1", "coach.xml::VJ2"],
            "vehicle_journey_code": ["VJ1", "VJ2"],
            "file_name": ["coach.xml", "coach.xml"],
        }
    )

    index = build_journey_date_index(journeys, tmp_path)

    assert set(index["profile_source"]) == {PROFILE_SOURCE_TXC, PROFILE_SOURCE_FALLBACK}
    assert set(index["profile_source"]).issubset({PROFILE_SOURCE_TXC, PROFILE_SOURCE_FALLBACK})
    assert index["date"].between(COACH_FEED_YEAR_START, COACH_FEED_YEAR_END).all()
    assert index.loc[index["journey_id"].eq("coach.xml::VJ2"), "date"].nunique() == 15


def test_build_journey_date_index_falls_back_when_xml_missing(tmp_path: Path) -> None:
    journeys = pd.DataFrame(
        {
            "journey_id": ["missing.xml::VJ9"],
            "vehicle_journey_code": ["VJ9"],
            "file_name": ["missing.xml"],
        }
    )

    index = build_journey_date_index(journeys, tmp_path)

    assert set(index["profile_source"]) == {PROFILE_SOURCE_FALLBACK}
    assert index["date"].min() == COACH_FEED_YEAR_START
    assert index["date"].max() == COACH_FEED_YEAR_END
