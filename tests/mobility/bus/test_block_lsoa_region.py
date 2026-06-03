from __future__ import annotations

import pandas as pd

from mobility.bus.annual_lsoa_region import attach_lsoa_and_region


def _templates() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "block_template_id": ["bt1", "bt2"],
            "start_lat": [51.50, 55.95],
            "start_lon": [-0.10, -3.20],
            "end_lat": [51.51, 55.96],
            "end_lon": [-0.11, -3.21],
        }
    )


def test_lsoa_attach_adds_start_and_end_lsoa() -> None:
    centroids = pd.DataFrame(
        {
            "lsoa_code": ["E01000001", "S01000001"],
            "lat": [51.50, 55.95],
            "lon": [-0.10, -3.20],
            "easting_m": [0.0, 1.0],
            "northing_m": [0.0, 1.0],
        }
    )
    out, _ = attach_lsoa_and_region(_templates(), centroids=centroids, max_distance_km=5.0, region_lookup=pd.DataFrame())
    assert set(out["start_lsoa"]) == {"E01000001", "S01000001"}
    assert set(out["end_lsoa"]) == {"E01000001", "S01000001"}


def test_lsoa_attach_adds_trip_endpoint_lsoas() -> None:
    templates = _templates().iloc[:1].copy()
    templates["trip_start_lats"] = [[51.50, 51.51]]
    templates["trip_start_lons"] = [[-0.10, -0.11]]
    templates["trip_end_lats"] = [[51.51, 51.50]]
    templates["trip_end_lons"] = [[-0.11, -0.10]]
    centroids = pd.DataFrame(
        {
            "lsoa_code": ["E01000001"],
            "lat": [51.50],
            "lon": [-0.10],
            "easting_m": [0.0],
            "northing_m": [0.0],
        }
    )

    out, diagnostics = attach_lsoa_and_region(templates, centroids=centroids, max_distance_km=5.0, region_lookup=pd.DataFrame())

    assert out.loc[0, "trip_start_lsoas"] == ["E01000001", "E01000001"]
    assert out.loc[0, "trip_end_lsoas"] == ["E01000001", "E01000001"]
    assert diagnostics.loc[0, "trip_endpoint_lsoa_success_rate"] == 1.0


def test_region_key_is_gor_or_country_level() -> None:
    centroids = pd.DataFrame({"lsoa_code": ["E01000001"], "lat": [51.50], "lon": [-0.10], "easting_m": [0.0], "northing_m": [0.0]})
    lookup = pd.DataFrame({"lsoa_code": ["E01000001"], "region_key": ["London"], "region_source": ["test"]})
    out, _ = attach_lsoa_and_region(_templates().iloc[:1], centroids=centroids, max_distance_km=5.0, region_lookup=lookup)
    assert out.loc[0, "region_key"] == "London"


def test_london_is_single_region_not_boroughs() -> None:
    centroids = pd.DataFrame({"lsoa_code": ["E01000001"], "lat": [51.50], "lon": [-0.10], "easting_m": [0.0], "northing_m": [0.0]})
    lookup = pd.DataFrame({"lsoa_code": ["E01000001"], "region_key": ["London"], "region_source": ["test"]})
    out, _ = attach_lsoa_and_region(_templates().iloc[:1], centroids=centroids, max_distance_km=5.0, region_lookup=lookup)
    assert out.loc[0, "region_key"] == "London"
    assert "E090" not in out.loc[0, "region_key"]


def test_region_lookup_reports_missing_values() -> None:
    centroids = pd.DataFrame({"lsoa_code": ["E01000001"], "lat": [51.50], "lon": [-0.10], "easting_m": [0.0], "northing_m": [0.0]})
    out, diagnostics = attach_lsoa_and_region(_templates().iloc[:1], centroids=centroids, max_distance_km=5.0, region_lookup=pd.DataFrame())
    assert out.loc[0, "region_source"] == "prefix_fallback"
    assert "region_lookup_success_rate" in diagnostics.columns
