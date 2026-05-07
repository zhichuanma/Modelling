from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest

from mobility.bus.data_loader import attach_lsoa


def _write_geojson(path: Path, code_col: str, code: str, coords: list[list[float]]) -> None:
    path.write_text(
        json.dumps(
            {
                "type": "FeatureCollection",
                "features": [
                    {
                        "type": "Feature",
                        "properties": {code_col: code},
                        "geometry": {"type": "Polygon", "coordinates": [coords]},
                    }
                ],
            }
        )
    )


def _blocks() -> pd.DataFrame:
    return pd.DataFrame(
        [
            ("t0", 51.50, -0.15, 55.95, -3.15),
            ("t1", 54.55, -6.65, 51.52, -0.18),
        ],
        columns=["trip_id", "start_lat", "start_lon", "end_lat", "end_lon"],
    )


@pytest.fixture()
def boundary_paths(tmp_path: Path) -> tuple:
    ew = tmp_path / "ew.geojson"
    scot = tmp_path / "scot.geojson"
    ni = tmp_path / "ni.geojson"
    _write_geojson(ew, "LSOA21CD", "E01000001", [[-0.2, 51.4], [-0.1, 51.4], [-0.1, 51.6], [-0.2, 51.6], [-0.2, 51.4]])
    _write_geojson(scot, "dzcode", "S01000001", [[-3.2, 55.9], [-3.1, 55.9], [-3.1, 56.0], [-3.2, 56.0], [-3.2, 55.9]])
    _write_geojson(ni, "DZ2021_cd", "N00000001", [[-6.7, 54.5], [-6.6, 54.5], [-6.6, 54.6], [-6.7, 54.6], [-6.7, 54.5]])
    return (
        (ew, "LSOA21CD", "EW_LSOA21"),
        (scot, "dzcode", "Scotland_DZ2022"),
        (ni, "DZ2021_cd", "NI_DZ2021"),
    )


def test_attach_lsoa_uses_polygon_namespaces(boundary_paths: tuple) -> None:
    attached = attach_lsoa(_blocks(), boundary_paths=boundary_paths, centroids=pd.DataFrame())

    assert attached["start_lsoa"].tolist() == ["E01000001", "N00000001"]
    assert attached["end_lsoa"].tolist() == ["S01000001", "E01000001"]
    assert set(attached["start_lsoa_source"]) == {"EW_LSOA21", "NI_DZ2021"}
    assert set(attached["end_lsoa_source"]) == {"Scotland_DZ2022", "EW_LSOA21"}
    assert set(attached["start_lsoa_match_method"]) == {"polygon"}
    assert set(attached["end_lsoa_match_method"]) == {"polygon"}


def test_attach_lsoa_polygon_attrs_sum_to_approximately_100(boundary_paths: tuple) -> None:
    attached = attach_lsoa(_blocks(), boundary_paths=boundary_paths, centroids=pd.DataFrame())
    attrs = attached.attrs["lsoa_join"]

    assert attrs["method"] == "polygon_with_centroid_fallback"
    assert attrs["polygon_pct"] + attrs["centroid_fallback_pct"] + attrs["no_match_pct"] == pytest.approx(100.0)
    assert attrs["source_breakdown"]["EW_LSOA21"] == pytest.approx(50.0)


def test_attach_lsoa_falls_back_to_centroid_for_no_polygon_match(boundary_paths: tuple) -> None:
    blocks = _blocks()
    blocks.loc[0, ["start_lat", "start_lon"]] = [51.80, -0.50]
    centroids = pd.DataFrame(
        {
            "lsoa_code": ["E010FALLBACK"],
            "easting_m": [530000.0],
            "northing_m": [180000.0],
            "lat": [51.80],
            "lon": [-0.50],
        }
    )

    attached = attach_lsoa(blocks, boundary_paths=boundary_paths, centroids=centroids, max_distance_km=5.0)

    assert attached.loc[0, "start_lsoa"] == "E010FALLBACK"
    assert attached.loc[0, "start_lsoa_source"] == "centroid_fallback"
    assert attached.loc[0, "start_lsoa_match_method"] == "centroid_fallback"


def test_attach_lsoa_keeps_no_match_when_centroid_fallback_empty(boundary_paths: tuple) -> None:
    blocks = _blocks()
    blocks.loc[0, ["start_lat", "start_lon"]] = [51.80, -0.50]

    attached = attach_lsoa(blocks, boundary_paths=boundary_paths, centroids=pd.DataFrame())

    assert attached.loc[0, "start_lsoa"] == ""
    assert attached.loc[0, "start_lsoa_match_method"] == "no_match"
    assert attached.attrs["lsoa_join"]["no_match_pct"] > 0.0
