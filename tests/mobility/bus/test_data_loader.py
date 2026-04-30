from __future__ import annotations

import pytest

from mobility.bus.data_loader import BASE_COLUMNS, load_all_blocks, summarize_block_quality


@pytest.fixture(scope="module")
def all_blocks():
    return load_all_blocks()


def test_load_all_blocks_schema_and_distance_source(all_blocks) -> None:
    assert list(all_blocks.columns) == [*BASE_COLUMNS, "distance_source"]
    assert not all_blocks.loc[:, [col for col in BASE_COLUMNS if col != "shape_id"]].isna().any().any()

    has_shape = all_blocks["shape_id"].notna() & all_blocks["shape_id"].astype(str).str.strip().ne("")
    assert (all_blocks.loc[has_shape, "distance_source"] == "shape").all()
    assert (all_blocks.loc[~has_shape, "distance_source"] == "stop_haversine").all()


def test_quality_summary_matches_known_audit_values(all_blocks) -> None:
    summary = summarize_block_quality(all_blocks).iloc[0]

    assert summary["n_trips"] == len(all_blocks)
    assert summary["n_blocks"] == all_blocks["block_id"].nunique()
    assert abs(summary["pct_shape_distance"] - 47.0) <= 5.0
    assert abs(summary["pct_cross_midnight_blocks"] - 9.5) <= 5.0
    assert abs(summary["stop_continuity_native"] - 73.3) <= 5.0
    assert abs(summary["stop_continuity_inferred"] - 49.5) <= 5.0
