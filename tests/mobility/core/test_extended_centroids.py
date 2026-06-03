from __future__ import annotations

import pandas as pd

from mobility.core.spatial import load_extended_lsoa_centroids


def _onspd_csv(tmp_path):
    frame = pd.DataFrame(
        {
            "lsoa21": ["E01000001", "E01000001", "S01013482", pd.NA, pd.NA],
            "lsoa11": ["E01000001", "E01000001", "S01006506", "S01006508", "S01006508"],
            "oa21": ["E00000001", "E00000002", "S00090001", "N20000279", "N20000279"],
            "oseast1m": [530000.0, 530100.0, 320000.0, 330000.0, 330200.0],
            "osnrth1m": [180000.0, 180100.0, 670000.0, 380000.0, 380200.0],
            "lat": [51.50, 51.51, 55.90, 54.60, 54.61],
            "long": [-0.10, -0.11, -3.20, -5.90, -5.91],
        }
    )
    path = tmp_path / "onspd.csv"
    frame.to_csv(path, index=False)
    return path


def test_extended_centroids_fallback_priority_and_means(tmp_path) -> None:
    centroids = load_extended_lsoa_centroids(_onspd_csv(tmp_path)).set_index("lsoa_code")

    # lsoa21 codes resolved from lsoa21 with postcode-mean coordinates.
    assert centroids.loc["E01000001", "centroid_source"] == "lsoa21"
    assert abs(centroids.loc["E01000001", "lat"] - 51.505) < 1e-9
    assert centroids.loc["S01013482", "centroid_source"] == "lsoa21"

    # Scotland DZ2011 codes appear only via the lsoa11 fallback.
    assert centroids.loc["S01006508", "centroid_source"] == "lsoa11"
    assert abs(centroids.loc["S01006508", "lat"] - 54.605) < 1e-9
    # E01000001 is NOT duplicated from lsoa11; lsoa21 takes priority.
    assert (centroids.index == "E01000001").sum() == 1

    # NI DZ2021 codes appear only via the oa21 fallback.
    assert centroids.loc["N20000279", "centroid_source"] == "oa21"
    assert abs(centroids.loc["N20000279", "lon"] - (-5.905)) < 1e-9

    # Same schema as load_lsoa_centroids plus the source tag.
    assert {"easting_m", "northing_m", "lat", "lon", "centroid_source"}.issubset(centroids.columns)
