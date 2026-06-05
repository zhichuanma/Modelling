from __future__ import annotations

import pandas as pd

from mobility.bus.operational_depot import ANCHOR_LIMITATION_NOTE, infer_operational_depot, infer_operational_depots


def _row(**kwargs) -> pd.Series:
    base = {
        "block_template_id": "bt",
        "agency_id": "OP",
        "start_lsoa": "E1",
        "end_lsoa": "E2",
        "start_lat": 51.0,
        "start_lon": -1.0,
        "end_lat": 51.1,
        "end_lon": -1.1,
        "region_key": "London",
    }
    base.update(kwargs)
    return pd.Series(base)


def test_uses_block_level_signal_not_global_mode() -> None:
    rows = pd.DataFrame(
        [
            _row(block_template_id="bt1", start_lsoa="LON", end_lsoa="LON", region_key="London"),
            _row(block_template_id="bt2", start_lsoa="WAL1", end_lsoa="WAL2", region_key="Wales"),
        ]
    )
    inferred, _, _ = infer_operational_depots(rows)
    assert inferred.loc[inferred["block_template_id"].eq("bt2"), "operational_depot_lsoa"].iloc[0] == "WAL2"


def test_closed_block_high_confidence() -> None:
    result = infer_operational_depot(_row(start_lsoa="E1", end_lsoa="E1"))
    assert result["operational_depot_lsoa"] == "E1"
    assert result["depot_confidence"] == "high"


def test_end_lsoa_fallback_low_confidence() -> None:
    result = infer_operational_depot(_row(start_lsoa="E1", end_lsoa="E2"))
    assert result["operational_depot_lsoa"] == "E2"
    assert result["depot_confidence"] == "low"
    assert result["depot_inference_method"] == "end_lsoa_fallback"


def test_tie_prefers_final_end_lsoa() -> None:
    result = infer_operational_depot(
        _row(
            start_lsoa="E1",
            end_lsoa="E2",
            trip_start_lsoas=["E1", "E2"],
            trip_end_lsoas=["E2", "E1"],
        )
    )
    assert result["operational_depot_lsoa"] == "E2"
    assert result["depot_inference_method"] == "trip_terminal_tie_break_final_end_lsoa"


def test_missing_lsoa_manual_review() -> None:
    result = infer_operational_depot(_row(start_lsoa="", end_lsoa=""))
    assert result["depot_confidence"] == "missing"
    assert result["manual_review_flag"] is True


def test_depot_id_includes_agency_and_lsoa() -> None:
    result = infer_operational_depot(_row(agency_id="OP9", start_lsoa="E1", end_lsoa="E1"))
    assert result["depot_id"] == "opdepot_OP9_E1"


def test_london_does_not_absorb_non_london_blocks() -> None:
    rows = pd.DataFrame(
        [
            _row(block_template_id=f"lon{i}", start_lsoa="LON", end_lsoa="LON", region_key="London")
            for i in range(5)
        ]
        + [_row(block_template_id="nonlon", start_lsoa="S1", end_lsoa="S2", region_key="Scotland")]
    )
    inferred, _, _ = infer_operational_depots(rows)
    assert inferred.loc[inferred["block_template_id"].eq("nonlon"), "operational_depot_lsoa"].iloc[0] == "S2"


def test_summary_mentions_anchor_limitation() -> None:
    assert "not a real" in ANCHOR_LIMITATION_NOTE
