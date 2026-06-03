from __future__ import annotations

import pytest

import pandas as pd

from mobility.bus.annual_block_templates import build_block_templates


def _trip_rows() -> pd.DataFrame:
    return pd.DataFrame(
        [
            ("t2", "OP", "S1", "B1", "native", 25.0, 25.5, "B", "C", 51.1, -1.1, 51.2, -1.2, 8.0),
            ("t1", "OP", "S1", "B1", "native", 23.0, 24.5, "A", "B", 51.0, -1.0, 51.1, -1.1, 10.0),
        ],
        columns=[
            "trip_id",
            "agency_id",
            "service_id",
            "block_id",
            "block_source",
            "start_h",
            "end_h",
            "start_stop",
            "end_stop",
            "start_lat",
            "start_lon",
            "end_lat",
            "end_lon",
            "distance_km",
        ],
    )


def test_trip_rows_aggregate_to_block_templates() -> None:
    templates, diagnostics = build_block_templates(_trip_rows())
    assert len(templates) == 1
    assert templates.loc[0, "n_trips"] == 2
    assert diagnostics.loc[0, "n_trip_rows"] == 2


def test_block_template_has_first_and_last_stop() -> None:
    templates, _ = build_block_templates(_trip_rows())
    assert templates.loc[0, "start_stop"] == "A"
    assert templates.loc[0, "end_stop"] == "C"


def test_block_template_preserves_trip_order() -> None:
    templates, _ = build_block_templates(_trip_rows())
    assert templates.loc[0, "trip_ids"] == ["t1", "t2"]
    assert templates.loc[0, "trip_start_times"] == [23.0, 25.0]


def test_cross_midnight_times_are_preserved() -> None:
    templates, _ = build_block_templates(_trip_rows())
    assert templates.loc[0, "end_h"] == pytest.approx(25.5)
    assert templates.loc[0, "end_time"] == "25:30:00"
