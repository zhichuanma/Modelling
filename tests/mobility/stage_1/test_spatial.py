"""Stage 1a coverage for LSOA centroid loading and OD distance helpers."""

from __future__ import annotations

import importlib

import numpy as np
import pandas as pd
import pytest

constants = importlib.import_module("mobility.core.constants")
spatial = importlib.import_module("mobility.core.spatial")

HUFF_LAYER1_TOPK = constants.HUFF_LAYER1_TOPK
SCENE_CATEGORIES = constants.SCENE_CATEGORIES
load_lsoa_centroids = spatial.load_lsoa_centroids
od_distance_km = spatial.od_distance_km
od_distance_matrix = spatial.od_distance_matrix


@pytest.fixture(scope="session")
def real_centroids() -> pd.DataFrame:
    return load_lsoa_centroids()


@pytest.fixture()
def known_centroids() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "lsoa_code": ["E01000001", "E01000002", "E01000003"],
            "easting_m": [0.0, 3000.0, 3000.0],
            "northing_m": [0.0, 4000.0, 0.0],
        }
    )


def test_load_lsoa_centroids_returns_expected_schema(real_centroids: pd.DataFrame) -> None:
    assert len(real_centroids) >= 40000
    assert list(real_centroids.columns) == ["lsoa_code", "easting_m", "northing_m"]
    assert real_centroids["lsoa_code"].dtype == object
    assert real_centroids["easting_m"].dtype == np.dtype("float64")
    assert real_centroids["northing_m"].dtype == np.dtype("float64")
    assert real_centroids["lsoa_code"].is_unique


@pytest.mark.parametrize(
    ("origin_lsoa", "dest_lsoa", "expected_km"),
    [
        ("E01000001", "E01000002", 5.0),
        ("E01000001", "E01000003", 3.0),
        ("E01000002", "E01000003", 4.0),
    ],
)
def test_od_distance_km_matches_manual_euclidean_distance(
    known_centroids: pd.DataFrame,
    origin_lsoa: str,
    dest_lsoa: str,
    expected_km: float,
) -> None:
    observed_km = od_distance_km(origin_lsoa, dest_lsoa, known_centroids)
    assert observed_km == pytest.approx(expected_km, abs=1e-6)


def test_od_distance_km_same_lsoa_returns_intra_km(known_centroids: pd.DataFrame) -> None:
    assert od_distance_km("E01000001", "E01000001", known_centroids, intra_km=0.75) == 0.75


def test_od_distance_matrix_matches_pairwise_distance_calls(
    real_centroids: pd.DataFrame,
) -> None:
    rng = np.random.default_rng(20260422)
    sample_codes = rng.choice(
        real_centroids["lsoa_code"].to_numpy(dtype=object),
        size=5,
        replace=False,
    )
    intra_km = 0.75

    matrix_km = od_distance_matrix(sample_codes, real_centroids, intra_km=intra_km)
    expected_km = np.array(
        [
            [
                od_distance_km(origin_lsoa, dest_lsoa, real_centroids, intra_km=intra_km)
                for dest_lsoa in sample_codes
            ]
            for origin_lsoa in sample_codes
        ],
        dtype=float,
    )

    assert np.allclose(matrix_km, expected_km)
    assert np.allclose(np.diag(matrix_km), intra_km)


def test_stage1_constants_match_frozen_contract() -> None:
    assert isinstance(SCENE_CATEGORIES, list)
    assert len(SCENE_CATEGORIES) == 7
    assert "home" not in SCENE_CATEGORIES
    assert HUFF_LAYER1_TOPK == 500
