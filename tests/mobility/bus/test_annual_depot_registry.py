from __future__ import annotations

import pandas as pd

from mobility.bus.annual_depot_registry import build_operational_depot_registry


def _templates() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "block_template_id": ["bt_closed", "bt_open", "bt_missing"],
            "agency_id": ["OP", "OP", "OP"],
            "start_lsoa": ["E0101", "E0102", ""],
            "end_lsoa": ["E0101", "E0103", ""],
            "start_lat": [51.0, 52.0, float("nan")],
            "start_lon": [-1.0, -2.0, float("nan")],
            "end_lat": [51.01, 52.5, float("nan")],
            "end_lon": [-1.01, -2.5, float("nan")],
        }
    )


def test_closed_loop_block_gets_high_confidence_anchor() -> None:
    _, diagnostics, _ = build_operational_depot_registry(_templates())
    row = diagnostics.set_index("block_template_id").loc["bt_closed"]
    assert row["depot_confidence"] == "high"


def test_end_lsoa_fallback_gets_low_confidence() -> None:
    _, diagnostics, _ = build_operational_depot_registry(_templates())
    row = diagnostics.set_index("block_template_id").loc["bt_open"]
    assert row["depot_confidence"] == "low"
    assert row["depot_assignment_method"] == "end_lsoa_fallback"


def test_depot_id_includes_agency_and_lsoa_for_operational_anchor() -> None:
    _, diagnostics, _ = build_operational_depot_registry(_templates())
    row = diagnostics.set_index("block_template_id").loc["bt_open"]
    assert row["depot_id"] == "opdepot_OP_E0103"


def test_depot_registry_has_lat_lon_lsoa_confidence() -> None:
    registry, _, _ = build_operational_depot_registry(_templates())
    required = {"depot_lat", "depot_lon", "depot_lsoa", "depot_confidence"}
    assert required.issubset(registry.columns)


def test_missing_depot_sets_manual_review_flag() -> None:
    _, diagnostics, _ = build_operational_depot_registry(_templates())
    row = diagnostics.set_index("block_template_id").loc["bt_missing"]
    assert bool(row["manual_review_flag"]) is True
