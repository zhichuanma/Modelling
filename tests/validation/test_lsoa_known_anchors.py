from __future__ import annotations

from pathlib import Path

import pandas as pd

from mobility.core.spatial import load_lsoa_boundary_index, query_lsoa_polygons


ANCHORS_PATH = Path(__file__).with_name("lsoa_anchors.csv")


def test_lsoa_known_anchors_match_boundary_polygons() -> None:
    anchors = pd.read_csv(ANCHORS_PATH)
    assert len(anchors) >= 20

    index = load_lsoa_boundary_index()
    codes, _, methods = query_lsoa_polygons(
        anchors["lat"].to_numpy(dtype=float),
        anchors["lon"].to_numpy(dtype=float),
        index,
    )

    actual = pd.Series(codes, name="actual")
    expected = anchors["expected_lsoa_or_dz_code"].astype(str)
    mismatches = anchors.loc[actual.ne(expected), ["name", "expected_lsoa_or_dz_code"]].copy()
    mismatches["actual"] = actual[actual.ne(expected)].to_numpy()
    assert mismatches.empty, mismatches.to_string(index=False)
    assert set(methods) == {"polygon"}
