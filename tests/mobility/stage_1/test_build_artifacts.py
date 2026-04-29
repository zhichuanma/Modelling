"""Stage 1b coverage for offline attractiveness and destination artifacts."""

from __future__ import annotations

import importlib

import numpy as np
import pandas as pd
import pytest

builder = importlib.import_module("mobility.cars.build_destination_choice_table")
constants = importlib.import_module("mobility.core.constants")
spatial = importlib.import_module("mobility.core.spatial")

HUFF_LAYER1_BETA = constants.HUFF_LAYER1_BETA
HUFF_LAYER1_DMIN_M = constants.HUFF_LAYER1_DMIN_M
HUFF_LAYER1_DSCALE_KM = constants.HUFF_LAYER1_DSCALE_KM
SCENE_CATEGORIES = constants.SCENE_CATEGORIES
build_destination_choice_table = builder.build_destination_choice_table
build_lsoa_scene_attractiveness_table = builder.build_lsoa_scene_attractiveness_table
load_lsoa_centroids = spatial.load_lsoa_centroids
od_distance_matrix = spatial.od_distance_matrix


@pytest.fixture(scope="session")
def sample_lsoa_centroids() -> pd.DataFrame:
    centroids = load_lsoa_centroids()
    rng = np.random.default_rng(20260422)
    sample_codes = np.sort(
        rng.choice(centroids["lsoa_code"].to_numpy(dtype=object), size=5, replace=False)
    )
    return (
        centroids.loc[centroids["lsoa_code"].isin(sample_codes)]
        .sort_values("lsoa_code", kind="stable")
        .reset_index(drop=True)
    )


@pytest.fixture()
def raw_poi_rows(sample_lsoa_centroids: pd.DataFrame) -> pd.DataFrame:
    rows: list[dict[str, object]] = []
    for lsoa_idx, lsoa_code in enumerate(sample_lsoa_centroids["lsoa_code"], start=1):
        for scene_idx, scene in enumerate(SCENE_CATEGORIES, start=1):
            rows.append(
                {
                    "lsoa_code": lsoa_code,
                    "scene_label": scene,
                    "area_m2": float((lsoa_idx * 100.0) + scene_idx),
                }
            )
            rows.append(
                {
                    "lsoa_code": lsoa_code,
                    "scene_label": scene,
                    "area_m2": float((scene_idx * 10.0) + lsoa_idx),
                }
            )
    rows.append(
        {
            "lsoa_code": sample_lsoa_centroids["lsoa_code"].iloc[0],
            "scene_label": "home",
            "area_m2": 999.0,
        }
    )
    return pd.DataFrame(rows)


@pytest.fixture()
def micro_attractiveness(sample_lsoa_centroids: pd.DataFrame) -> pd.DataFrame:
    frame = pd.DataFrame({"lsoa_code": sample_lsoa_centroids["lsoa_code"].to_numpy(dtype=object)})
    base_values = np.array([1.0, 2.5, 4.0, 7.0, 11.0], dtype=np.float64)
    for scene_idx, scene in enumerate(SCENE_CATEGORIES, start=1):
        frame[f"A_{scene}"] = base_values + float(scene_idx)
    return frame


def test_lsoa_scene_attractiveness_schema_is_frozen(
    raw_poi_rows: pd.DataFrame,
) -> None:
    attractiveness = build_lsoa_scene_attractiveness_table(raw_poi_rows)

    expected_columns = ["lsoa_code", *[f"A_{scene}" for scene in SCENE_CATEGORIES]]
    assert list(attractiveness.columns) == expected_columns
    assert "A_home" not in attractiveness.columns
    assert attractiveness["lsoa_code"].dtype == object
    assert attractiveness["lsoa_code"].is_unique
    assert not attractiveness.isna().any().any()
    for scene in SCENE_CATEGORIES:
        assert attractiveness[f"A_{scene}"].dtype == np.dtype("float64")


def test_destination_choice_groups_are_normalised_and_unique(
    sample_lsoa_centroids: pd.DataFrame,
    micro_attractiveness: pd.DataFrame,
) -> None:
    choice_table = build_destination_choice_table(
        centroids=sample_lsoa_centroids,
        attractiveness_df=micro_attractiveness,
        seed=42,
    )

    assert choice_table["prob"].dtype == np.dtype("float32")
    assert set(choice_table["purpose"]).issubset(set(SCENE_CATEGORIES))
    assert "home" not in set(choice_table["purpose"])

    grouped = choice_table.groupby(["origin_lsoa", "purpose"], sort=False)
    for (_origin_lsoa, _purpose), group in grouped:
        assert abs(float(group["prob"].sum()) - 1.0) < 1e-5
        assert len(group) <= 500
        assert group["dest_lsoa"].is_unique


def test_destination_choice_build_is_deterministic_for_fixed_seed(
    sample_lsoa_centroids: pd.DataFrame,
    micro_attractiveness: pd.DataFrame,
) -> None:
    first = build_destination_choice_table(
        centroids=sample_lsoa_centroids,
        attractiveness_df=micro_attractiveness,
        seed=42,
        test_fraction=0.6,
    )
    second = build_destination_choice_table(
        centroids=sample_lsoa_centroids,
        attractiveness_df=micro_attractiveness,
        seed=42,
        test_fraction=0.6,
    )

    pd.testing.assert_frame_equal(first, second)


def test_top2_probability_ratio_matches_score_ratio(
    sample_lsoa_centroids: pd.DataFrame,
    micro_attractiveness: pd.DataFrame,
) -> None:
    choice_table = build_destination_choice_table(
        centroids=sample_lsoa_centroids,
        attractiveness_df=micro_attractiveness,
        seed=42,
    )
    codes = sample_lsoa_centroids["lsoa_code"].to_numpy(dtype=object)
    origin_lsoa = codes[0]
    purpose = "work"

    group = (
        choice_table.loc[
            (choice_table["origin_lsoa"] == origin_lsoa) & (choice_table["purpose"] == purpose)
        ]
        .sort_values(["prob", "dest_lsoa"], ascending=[False, True], kind="stable")
        .reset_index(drop=True)
    )
    top2 = group.iloc[:2]

    distance_km = od_distance_matrix(codes, sample_lsoa_centroids, intra_km=0.5)
    origin_idx = int(np.where(codes == origin_lsoa)[0][0])
    attractiveness_values = (
        micro_attractiveness.set_index("lsoa_code").loc[list(codes), f"A_{purpose}"].to_numpy(dtype=float)
    )
    d_clip_km = np.maximum(distance_km[origin_idx], HUFF_LAYER1_DMIN_M / 1000.0)
    scores = attractiveness_values * np.exp(
        -np.square(distance_km[origin_idx] / HUFF_LAYER1_DSCALE_KM)
    ) / np.power(d_clip_km, HUFF_LAYER1_BETA)

    first_dest_idx = int(np.where(codes == top2.loc[0, "dest_lsoa"])[0][0])
    second_dest_idx = int(np.where(codes == top2.loc[1, "dest_lsoa"])[0][0])
    expected_ratio = scores[first_dest_idx] / scores[second_dest_idx]
    observed_ratio = float(top2.loc[0, "prob"] / top2.loc[1, "prob"])

    assert observed_ratio == pytest.approx(expected_ratio, rel=1e-6)
