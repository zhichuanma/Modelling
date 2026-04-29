"""Stage 1 offline builders for LSOA scene attractiveness and Layer-1 choices."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Iterable, Sequence

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from mobility.core.constants import (
    HUFF_LAYER1_BETA,
    HUFF_LAYER1_DMIN_M,
    HUFF_LAYER1_DSCALE_KM,
    HUFF_LAYER1_TOPK,
    SCENE_CATEGORIES,
)
from mobility.core.spatial import load_lsoa_centroids, od_distance_matrix

REPO_ROOT = Path(__file__).resolve().parents[3]
DATA_DIR = REPO_ROOT / "Data" / "Charging_stations" / "OSM_POI_Labeling"
DEFAULT_ATTRACTIVENESS_PATH = DATA_DIR / "lsoa_scene_attractiveness.parquet"
DEFAULT_OUTPUT_PATH = DATA_DIR / "destination_choice_table.parquet"

ATTRACTIVENESS_COLUMNS = ["lsoa_code", *[f"A_{scene}" for scene in SCENE_CATEGORIES]]
CHOICE_COLUMNS = ["origin_lsoa", "purpose", "dest_lsoa", "prob"]
CHOICE_SCHEMA = pa.schema(
    [
        ("origin_lsoa", pa.string()),
        ("purpose", pa.string()),
        ("dest_lsoa", pa.string()),
        ("prob", pa.float32()),
    ]
)

DEFAULT_INTRA_LSOA_KM = 0.5
DEFAULT_ORIGIN_CHUNK_SIZE = 256
DEFAULT_ROW_GROUP_SIZE = 100_000


def build_lsoa_scene_attractiveness_table(poi_lsoa_df: pd.DataFrame) -> pd.DataFrame:
    """Aggregate POI areas to the frozen Stage-1 LSOA attractiveness schema."""
    required_columns = {"lsoa_code", "scene_label", "area_m2"}
    missing_columns = required_columns - set(poi_lsoa_df.columns)
    if missing_columns:
        raise KeyError(f"poi_lsoa_df is missing required columns: {sorted(missing_columns)}")

    prepared = poi_lsoa_df.loc[:, ["lsoa_code", "scene_label", "area_m2"]].copy()
    prepared["lsoa_code"] = prepared["lsoa_code"].astype("string").str.strip()
    prepared["scene_label"] = prepared["scene_label"].astype("string").str.strip()
    prepared["area_m2"] = pd.to_numeric(prepared["area_m2"], errors="coerce")

    valid = (
        prepared["lsoa_code"].notna()
        & prepared["lsoa_code"].ne("")
        & prepared["scene_label"].isin(SCENE_CATEGORIES)
        & prepared["area_m2"].notna()
        & prepared["area_m2"].ge(0.0)
    )
    filtered = prepared.loc[valid]

    if filtered.empty:
        empty = pd.DataFrame({"lsoa_code": pd.Series(dtype=object)})
        for scene in SCENE_CATEGORIES:
            empty[f"A_{scene}"] = pd.Series(dtype="float64")
        return empty

    grouped = (
        filtered.groupby(["lsoa_code", "scene_label"], sort=True, as_index=False)["area_m2"]
        .sum()
        .pivot(index="lsoa_code", columns="scene_label", values="area_m2")
        .reindex(columns=SCENE_CATEGORIES, fill_value=0.0)
        .fillna(0.0)
    )
    grouped = np.log1p(grouped)
    grouped.columns = [f"A_{scene}" for scene in grouped.columns]

    result = grouped.reset_index().sort_values("lsoa_code", kind="stable").reset_index(drop=True)
    return coerce_lsoa_scene_attractiveness_schema(result)


def coerce_lsoa_scene_attractiveness_schema(attractiveness_df: pd.DataFrame) -> pd.DataFrame:
    """Reorder and cast a wide attractiveness table to the frozen Stage-1 schema."""
    missing_columns = set(ATTRACTIVENESS_COLUMNS) - set(attractiveness_df.columns)
    if missing_columns:
        raise KeyError(
            "attractiveness_df is missing required columns: "
            f"{sorted(missing_columns)}"
        )

    result = attractiveness_df.loc[:, ATTRACTIVENESS_COLUMNS].copy()
    result["lsoa_code"] = result["lsoa_code"].astype("string").str.strip().astype(object)
    for scene in SCENE_CATEGORIES:
        column = f"A_{scene}"
        result[column] = pd.to_numeric(result[column], errors="coerce").astype("float64")

    if result["lsoa_code"].isna().any() or result["lsoa_code"].eq("").any():
        raise ValueError("lsoa_code must be present for every attractiveness row")
    if result.isna().any().any():
        raise ValueError("attractiveness_df must not contain NaN values")
    if result["lsoa_code"].duplicated().any():
        raise ValueError("lsoa_code must be unique in attractiveness_df")

    return result.sort_values("lsoa_code", kind="stable").reset_index(drop=True)


def build_destination_choice_table(
    centroids: pd.DataFrame,
    attractiveness_df: pd.DataFrame,
    *,
    seed: int,
    test_fraction: float = 1.0,
    intra_km: float = DEFAULT_INTRA_LSOA_KM,
    topk: int = HUFF_LAYER1_TOPK,
) -> pd.DataFrame:
    """Build an in-memory choice table for small exact problems.

    This helper is intended for tests and small sampled builds where origin and
    destination universes are the same set of LSOA centroids.
    """
    centroids_indexed = _prepare_centroids(centroids)
    attractiveness = _prepare_attractiveness(attractiveness_df, centroids_indexed.index)
    build_codes = _select_origin_codes(
        available_codes=attractiveness["lsoa_code"].to_numpy(dtype=object),
        seed=seed,
        test_fraction=test_fraction,
    )
    build_centroids = centroids_indexed.loc[list(build_codes)].reset_index()
    distance_km = od_distance_matrix(build_codes, build_centroids, intra_km=intra_km)
    return _build_choice_table_from_distance_matrix(
        origin_codes=build_codes,
        destination_codes=build_codes,
        distance_km=distance_km,
        attractiveness=attractiveness.set_index("lsoa_code").loc[list(build_codes)],
        topk=topk,
    )


def write_destination_choice_table_parquet(
    *,
    attractiveness_path: Path = DEFAULT_ATTRACTIVENESS_PATH,
    output_path: Path = DEFAULT_OUTPUT_PATH,
    seed: int,
    test_fraction: float = 1.0,
    intra_km: float = DEFAULT_INTRA_LSOA_KM,
    topk: int = HUFF_LAYER1_TOPK,
    origin_chunk_size: int = DEFAULT_ORIGIN_CHUNK_SIZE,
    row_group_size: int = DEFAULT_ROW_GROUP_SIZE,
) -> Path:
    """Build and stream the Stage-1 destination choice table to parquet."""
    centroids_indexed = _prepare_centroids(load_lsoa_centroids())
    attractiveness = _prepare_attractiveness(
        pd.read_parquet(attractiveness_path),
        centroids_indexed.index,
    )
    origin_codes = _select_origin_codes(
        available_codes=centroids_indexed.index.to_numpy(dtype=object),
        seed=seed,
        test_fraction=test_fraction,
    )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.unlink(missing_ok=True)

    writer: pq.ParquetWriter | None = None
    try:
        for scene in SCENE_CATEGORIES:
            scene_column = f"A_{scene}"
            destinations = attractiveness.loc[
                attractiveness[scene_column] > 0.0, ["lsoa_code", scene_column]
            ].copy()
            if destinations.empty:
                continue

            destination_codes = destinations["lsoa_code"].to_numpy(dtype=object)
            destination_points_m = centroids_indexed.loc[list(destination_codes), [
                "easting_m",
                "northing_m",
            ]].to_numpy(dtype=np.float64)
            scene_attractiveness = destinations[scene_column].to_numpy(dtype=np.float64)

            for start in range(0, len(origin_codes), origin_chunk_size):
                chunk_codes = origin_codes[start : start + origin_chunk_size]
                origin_points_m = centroids_indexed.loc[list(chunk_codes), [
                    "easting_m",
                    "northing_m",
                ]].to_numpy(dtype=np.float64)

                distance_km = _rectangular_distance_matrix_km(
                    origin_points_m=origin_points_m,
                    destination_points_m=destination_points_m,
                    origin_codes=chunk_codes,
                    destination_codes=destination_codes,
                    intra_km=intra_km,
                )
                score_matrix = _score_matrix_km(
                    distance_km=distance_km,
                    attractiveness_values=scene_attractiveness,
                )
                batch_frame = _build_choice_rows(
                    origin_codes=chunk_codes,
                    purpose=scene,
                    destination_codes=destination_codes,
                    score_matrix=score_matrix,
                    topk=topk,
                )
                if batch_frame.empty:
                    continue

                table = pa.Table.from_pandas(
                    batch_frame,
                    schema=CHOICE_SCHEMA,
                    preserve_index=False,
                )
                if writer is None:
                    writer = pq.ParquetWriter(
                        output_path,
                        CHOICE_SCHEMA,
                        compression="snappy",
                    )
                writer.write_table(table, row_group_size=row_group_size)

        if writer is None:
            empty_frame = pd.DataFrame(
                {
                    "origin_lsoa": pd.Series(dtype=object),
                    "purpose": pd.Series(dtype=object),
                    "dest_lsoa": pd.Series(dtype=object),
                    "prob": pd.Series(dtype="float32"),
                }
            )
            empty_table = pa.Table.from_pandas(
                empty_frame,
                schema=CHOICE_SCHEMA,
                preserve_index=False,
            )
            writer = pq.ParquetWriter(output_path, CHOICE_SCHEMA, compression="snappy")
            writer.write_table(empty_table, row_group_size=row_group_size)
    finally:
        if writer is not None:
            writer.close()

    return output_path


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed", type=int, required=True)
    parser.add_argument("--test-fraction", type=float, default=1.0)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    write_destination_choice_table_parquet(
        seed=args.seed,
        test_fraction=args.test_fraction,
    )
    return 0


def _prepare_centroids(centroids: pd.DataFrame) -> pd.DataFrame:
    result = centroids.copy()
    if "lsoa_code" in result.columns:
        result = result.set_index("lsoa_code", drop=True)
    result = result.loc[:, ["easting_m", "northing_m"]]
    if result.index.has_duplicates:
        raise ValueError("centroids must have unique lsoa_code values")
    result.index = result.index.astype("string").str.strip().astype(object)
    return result.sort_index(kind="stable")


def _prepare_attractiveness(
    attractiveness_df: pd.DataFrame,
    valid_codes: Iterable[str],
) -> pd.DataFrame:
    result = coerce_lsoa_scene_attractiveness_schema(attractiveness_df)
    valid_code_index = pd.Index(valid_codes, dtype=object)
    result = result.loc[result["lsoa_code"].isin(valid_code_index)].reset_index(drop=True)
    if result.empty:
        raise ValueError("No attractiveness rows remain after aligning to centroid LSOAs")
    return result


def _select_origin_codes(
    *,
    available_codes: Sequence[str] | np.ndarray,
    seed: int,
    test_fraction: float,
) -> np.ndarray:
    if not 0.0 < test_fraction <= 1.0:
        raise ValueError("test_fraction must be in the interval (0, 1]")

    codes = np.asarray(sorted(available_codes), dtype=object)
    rng = np.random.default_rng(seed)
    if test_fraction >= 1.0:
        return codes

    sample_size = max(1, int(np.floor(len(codes) * test_fraction)))
    selected_idx = np.sort(rng.choice(len(codes), size=sample_size, replace=False))
    return codes[selected_idx]


def _build_choice_table_from_distance_matrix(
    *,
    origin_codes: np.ndarray,
    destination_codes: np.ndarray,
    distance_km: np.ndarray,
    attractiveness: pd.DataFrame,
    topk: int,
) -> pd.DataFrame:
    frames: list[pd.DataFrame] = []
    for scene in SCENE_CATEGORIES:
        scene_values = attractiveness[f"A_{scene}"].to_numpy(dtype=np.float64)
        score_matrix = _score_matrix_km(
            distance_km=distance_km,
            attractiveness_values=scene_values,
        )
        scene_frame = _build_choice_rows(
            origin_codes=origin_codes,
            purpose=scene,
            destination_codes=destination_codes,
            score_matrix=score_matrix,
            topk=topk,
        )
        if not scene_frame.empty:
            frames.append(scene_frame)

    if not frames:
        return pd.DataFrame(
            {
                "origin_lsoa": pd.Series(dtype=object),
                "purpose": pd.Series(dtype=object),
                "dest_lsoa": pd.Series(dtype=object),
                "prob": pd.Series(dtype="float32"),
            }
        )

    return pd.concat(frames, ignore_index=True)


def _score_matrix_km(
    *,
    distance_km: np.ndarray,
    attractiveness_values: np.ndarray,
) -> np.ndarray:
    distance_km = distance_km.astype(np.float64, copy=False)
    dmin_km = HUFF_LAYER1_DMIN_M / 1000.0
    d_clip_km = np.maximum(distance_km, dmin_km)
    kernel = np.exp(-np.square(distance_km / HUFF_LAYER1_DSCALE_KM))
    return attractiveness_values[np.newaxis, :] * kernel / np.power(d_clip_km, HUFF_LAYER1_BETA)


def _rectangular_distance_matrix_km(
    *,
    origin_points_m: np.ndarray,
    destination_points_m: np.ndarray,
    origin_codes: np.ndarray,
    destination_codes: np.ndarray,
    intra_km: float,
) -> np.ndarray:
    delta_e_m = np.subtract.outer(origin_points_m[:, 0], destination_points_m[:, 0])
    delta_n_m = np.subtract.outer(origin_points_m[:, 1], destination_points_m[:, 1])
    distance_km = np.sqrt(np.square(delta_e_m) + np.square(delta_n_m)) / 1000.0
    same_lsoa = np.equal.outer(origin_codes, destination_codes)
    distance_km[same_lsoa] = float(intra_km)
    return distance_km


def _build_choice_rows(
    *,
    origin_codes: np.ndarray,
    purpose: str,
    destination_codes: np.ndarray,
    score_matrix: np.ndarray,
    topk: int,
) -> pd.DataFrame:
    origin_values: list[str] = []
    purpose_values: list[str] = []
    destination_values: list[str] = []
    probability_values: list[np.float32] = []

    for row_idx, origin_code in enumerate(origin_codes):
        selected_idx = _select_topk_indices(
            destination_codes=destination_codes,
            scores=score_matrix[row_idx],
            topk=topk,
        )
        if selected_idx.size == 0:
            continue

        selected_scores = score_matrix[row_idx, selected_idx]
        score_sum = selected_scores.sum()
        if score_sum <= 0.0:
            continue

        probabilities = (selected_scores / score_sum).astype(np.float32, copy=False)
        selected_destinations = destination_codes[selected_idx]

        origin_values.extend([origin_code] * len(selected_idx))
        purpose_values.extend([purpose] * len(selected_idx))
        destination_values.extend(selected_destinations.tolist())
        probability_values.extend(probabilities.tolist())

    frame = pd.DataFrame(
        {
            "origin_lsoa": origin_values,
            "purpose": purpose_values,
            "dest_lsoa": destination_values,
            "prob": np.asarray(probability_values, dtype=np.float32),
        },
        columns=CHOICE_COLUMNS,
    )
    if frame.empty:
        frame["prob"] = frame["prob"].astype("float32")
    return frame


def _select_topk_indices(
    *,
    destination_codes: np.ndarray,
    scores: np.ndarray,
    topk: int,
) -> np.ndarray:
    positive_idx = np.flatnonzero(scores > 0.0)
    if positive_idx.size == 0:
        return np.array([], dtype=np.int64)

    if positive_idx.size > topk:
        subset_scores = scores[positive_idx]
        keep_rel_idx = np.argpartition(-subset_scores, kth=topk - 1)[:topk]
        candidate_idx = positive_idx[keep_rel_idx]
    else:
        candidate_idx = positive_idx

    candidate_scores = scores[candidate_idx]
    candidate_codes = destination_codes[candidate_idx]
    order = np.lexsort((candidate_codes, -candidate_scores))
    return candidate_idx[order]


if __name__ == "__main__":
    raise SystemExit(main())
