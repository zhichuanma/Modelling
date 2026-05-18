"""Scotland Data Zone geography unification helpers for private-car inputs."""

from __future__ import annotations

import zlib
from functools import lru_cache
from pathlib import Path
from typing import Iterable

import numpy as np
import pandas as pd


MODELLING_ROOT = Path(__file__).resolve().parents[2]
PROJECT_ROOT = MODELLING_ROOT.parent
DATA_CHARGING = PROJECT_ROOT / "Data" / "Charging_stations"
DATA_LOADS = PROJECT_ROOT / "Data" / "Loads"

DEFAULT_DZ2011_BOUNDARY_PATHS = (
    DATA_CHARGING / "SG_DataZoneBdry_2011" / "SG_DataZone_Bdry_2011.shp",
)
DEFAULT_DZ2022_BOUNDARY_PATHS = (
    DATA_CHARGING / "SG_DataZoneBdry_2022" / "SG_DataZone_Bdry_2022.shp",
    DATA_CHARGING / "SG_DataZoneBdry_2022" / "SG_DataZone_Bdry_2022.geojson",
    DATA_LOADS / "SG_DataZoneBdry_2022" / "SG_DataZone_Bdry_2022.shp",
    DATA_LOADS / "SG_DataZone_Bdry_2022.geojson",
)

SCOTLAND_DZ2011_RANGE = (6506, 13481)
SCOTLAND_DZ2022_RANGE = (13482, 20873)
DEFAULT_SCOTLAND_ASSIGNMENT_SEED = 20260422


def _first_existing_path(paths: Iterable[Path]) -> Path | None:
    for path in paths:
        if Path(path).exists():
            return Path(path)
    return None


def _scotland_suffix(value: object) -> int | None:
    text = str(value).strip()
    if not text.startswith("S010") or len(text) < 5:
        return None
    digits = text[4:]
    if not digits.isdigit():
        return None
    return int(digits)


def is_scotland_dz2011_code(value: object) -> bool:
    suffix = _scotland_suffix(value)
    return suffix is not None and SCOTLAND_DZ2011_RANGE[0] <= suffix <= SCOTLAND_DZ2011_RANGE[1]


def is_scotland_dz2022_code(value: object) -> bool:
    suffix = _scotland_suffix(value)
    return suffix is not None and SCOTLAND_DZ2022_RANGE[0] <= suffix <= SCOTLAND_DZ2022_RANGE[1]


def _home_code_series(ev_fleet: pd.DataFrame) -> pd.Series:
    if "home_lsoa" in ev_fleet.columns:
        home = ev_fleet["home_lsoa"].astype("string").str.strip()
        if "LSOA_code" in ev_fleet.columns:
            fallback = ev_fleet["LSOA_code"].astype("string").str.strip()
            home = home.where(home.notna() & home.ne(""), fallback)
        return home
    if "LSOA_code" in ev_fleet.columns:
        return ev_fleet["LSOA_code"].astype("string").str.strip()
    return pd.Series(pd.NA, index=ev_fleet.index, dtype="string")


def _read_data_zone_boundaries(path: Path, code_column: str, output_column: str):
    try:
        import geopandas as gpd
    except ImportError as exc:
        raise RuntimeError(
            "geopandas is required to build the Scotland DZ2011-to-DZ2022 area crosswalk"
        ) from exc

    frame = gpd.read_file(path)
    if code_column not in frame.columns:
        raise KeyError(f"{path} is missing required column {code_column!r}")
    result = frame.loc[:, [code_column, "geometry"]].rename(columns={code_column: output_column})
    result[output_column] = result[output_column].astype("string").str.strip()
    result = result.loc[result[output_column].notna() & result.geometry.notna()].copy()
    return result


@lru_cache(maxsize=4)
def _cached_area_crosswalk(
    dz2011_boundary_path: str,
    dz2022_boundary_path: str,
    min_overlap_area_m2: float,
) -> pd.DataFrame:
    try:
        import geopandas as gpd
    except ImportError as exc:
        raise RuntimeError(
            "geopandas is required to build the Scotland DZ2011-to-DZ2022 area crosswalk"
        ) from exc

    dz2011 = _read_data_zone_boundaries(Path(dz2011_boundary_path), "DataZone", "dz2011")
    dz2022 = _read_data_zone_boundaries(Path(dz2022_boundary_path), "dzcode", "dz2022")
    if dz2011.crs != dz2022.crs:
        dz2022 = dz2022.to_crs(dz2011.crs)

    dz2011["source_area_m2"] = dz2011.geometry.area.astype(float)
    dz2022["target_area_m2"] = dz2022.geometry.area.astype(float)
    intersections = gpd.overlay(
        dz2011,
        dz2022,
        how="intersection",
        keep_geom_type=True,
    )
    intersections["overlap_area_m2"] = intersections.geometry.area.astype(float)
    intersections = intersections.loc[
        intersections["overlap_area_m2"] > float(min_overlap_area_m2)
    ].copy()
    if intersections.empty:
        raise ValueError("Scotland DZ2011-to-DZ2022 area overlay produced no intersections")

    overlap_total = intersections.groupby("dz2011")["overlap_area_m2"].transform("sum")
    intersections["area_weight"] = intersections["overlap_area_m2"] / overlap_total
    intersections["source_overlap_coverage_rate"] = overlap_total / intersections["source_area_m2"]
    intersections = intersections.sort_values(
        ["dz2011", "area_weight", "overlap_area_m2", "dz2022"],
        ascending=[True, False, False, True],
        kind="stable",
    ).reset_index(drop=True)
    intersections["target_rank"] = intersections.groupby("dz2011").cumcount() + 1
    intersections["is_primary_overlap"] = intersections["target_rank"].eq(1)
    intersections["method"] = "area_weighted_boundary_overlay"
    intersections["dz2011_boundary_path"] = str(dz2011_boundary_path)
    intersections["dz2022_boundary_path"] = str(dz2022_boundary_path)
    columns = [
        "dz2011",
        "dz2022",
        "area_weight",
        "overlap_area_m2",
        "source_area_m2",
        "target_area_m2",
        "source_overlap_coverage_rate",
        "target_rank",
        "is_primary_overlap",
        "method",
        "dz2011_boundary_path",
        "dz2022_boundary_path",
    ]
    return pd.DataFrame(intersections.loc[:, columns])


def build_scotland_dz2011_to_dz2022_area_crosswalk(
    *,
    dz2011_boundary_path: Path | str | None = None,
    dz2022_boundary_path: Path | str | None = None,
    min_overlap_area_m2: float = 0.0,
) -> pd.DataFrame:
    """Build an area-weighted Scotland DZ2011-to-DZ2022 crosswalk from boundaries."""

    source_path = (
        Path(dz2011_boundary_path)
        if dz2011_boundary_path is not None
        else _first_existing_path(DEFAULT_DZ2011_BOUNDARY_PATHS)
    )
    target_path = (
        Path(dz2022_boundary_path)
        if dz2022_boundary_path is not None
        else _first_existing_path(DEFAULT_DZ2022_BOUNDARY_PATHS)
    )
    if source_path is None:
        raise FileNotFoundError("Could not find Scotland Data Zone 2011 boundary file")
    if target_path is None:
        raise FileNotFoundError("Could not find Scotland Data Zone 2022 boundary file")
    return _cached_area_crosswalk(
        str(source_path),
        str(target_path),
        float(min_overlap_area_m2),
    ).copy()


def _normalise_crosswalk(crosswalk: pd.DataFrame) -> pd.DataFrame:
    required = {"dz2011", "dz2022", "area_weight"}
    missing = required - set(crosswalk.columns)
    if missing:
        raise KeyError(f"Scotland crosswalk is missing required columns: {sorted(missing)}")
    result = crosswalk.loc[:, ["dz2011", "dz2022", "area_weight"]].copy()
    result["dz2011"] = result["dz2011"].astype("string").str.strip()
    result["dz2022"] = result["dz2022"].astype("string").str.strip()
    result["area_weight"] = pd.to_numeric(result["area_weight"], errors="coerce")
    result = result.loc[
        result["dz2011"].notna()
        & result["dz2022"].notna()
        & result["area_weight"].notna()
        & result["area_weight"].gt(0)
    ].copy()
    if result.empty:
        raise ValueError("Scotland crosswalk has no usable positive-weight rows")
    result = result.sort_values(
        ["dz2011", "area_weight", "dz2022"],
        ascending=[True, False, True],
        kind="stable",
    ).reset_index(drop=True)
    return result


def _target_counts(n_rows: int, weights: np.ndarray) -> np.ndarray:
    normalised = weights.astype(float) / float(weights.sum())
    expected = normalised * int(n_rows)
    counts = np.floor(expected).astype(int)
    remaining = int(n_rows) - int(counts.sum())
    if remaining > 0:
        fractions = expected - counts
        order = np.argsort(-fractions, kind="stable")
        counts[order[:remaining]] += 1
    return counts


def _stable_order_for_rows(
    ev_ids: pd.Series,
    row_labels: Iterable[object],
    *,
    seed: int,
) -> np.ndarray:
    keys = [
        zlib.crc32(f"{seed}|{ev_id}|{label}".encode("utf-8"))
        for ev_id, label in zip(ev_ids.astype(str).tolist(), row_labels)
    ]
    return np.argsort(np.asarray(keys, dtype=np.uint32), kind="stable")


def _metadata(
    *,
    applied: bool,
    reason: str = "",
    crosswalk: pd.DataFrame | None = None,
    rows_seen_scotland_dz2011: int = 0,
    rows_reassigned: int = 0,
    rows_unmapped: int = 0,
    unique_dz2011_seen: int = 0,
    unique_dz2022_assigned: int = 0,
    missing_source_examples: list[str] | None = None,
) -> dict:
    meta = {
        "applied": bool(applied),
        "method": "area_weighted_boundary_overlay" if applied else "",
        "source_geography_version": "Data Zone 2011" if rows_seen_scotland_dz2011 else "",
        "target_geography_version": "Data Zone 2022" if applied else "",
        "rows_seen_scotland_dz2011": int(rows_seen_scotland_dz2011),
        "rows_reassigned": int(rows_reassigned),
        "rows_unmapped": int(rows_unmapped),
        "unique_dz2011_seen": int(unique_dz2011_seen),
        "unique_dz2022_assigned": int(unique_dz2022_assigned),
        "missing_source_examples": missing_source_examples or [],
        "reason": reason,
    }
    if crosswalk is not None and not crosswalk.empty:
        meta.update(
            {
                "crosswalk_rows": int(len(crosswalk)),
                "crosswalk_dz2011_count": int(crosswalk["dz2011"].nunique()),
                "crosswalk_dz2022_count": int(crosswalk["dz2022"].nunique()),
            }
        )
        for column in ("method", "dz2011_boundary_path", "dz2022_boundary_path"):
            if column in crosswalk.columns:
                meta[column] = str(crosswalk[column].dropna().astype(str).iloc[0])
    return meta


def unify_scotland_ev_home_lsoa_to_dz2022(
    ev_fleet: pd.DataFrame,
    *,
    crosswalk: pd.DataFrame | None = None,
    assignment_seed: int = DEFAULT_SCOTLAND_ASSIGNMENT_SEED,
) -> tuple[pd.DataFrame, dict, pd.DataFrame | None]:
    """Return EV fleet copy with Scotland DZ2011 home_lsoa reassigned to DZ2022."""

    result = ev_fleet.copy()
    source_home = _home_code_series(result)
    if "home_lsoa" not in result.columns:
        result["home_lsoa"] = source_home

    dz2011_mask = source_home.map(is_scotland_dz2011_code).fillna(False).to_numpy(dtype=bool)
    rows_seen = int(dz2011_mask.sum())
    if rows_seen == 0:
        meta = _metadata(applied=False, reason="no_scotland_dz2011_home_lsoa_rows")
        result.attrs["scotland_geography_unification"] = meta
        return result, meta, crosswalk

    if crosswalk is None:
        try:
            crosswalk = build_scotland_dz2011_to_dz2022_area_crosswalk()
        except (FileNotFoundError, ImportError, RuntimeError, ValueError, KeyError) as exc:
            meta = _metadata(
                applied=False,
                reason=f"crosswalk_unavailable: {exc}",
                rows_seen_scotland_dz2011=rows_seen,
                unique_dz2011_seen=int(source_home.loc[dz2011_mask].nunique()),
            )
            result.attrs["scotland_geography_unification"] = meta
            return result, meta, None

    crosswalk_metadata = crosswalk.copy()
    prepared = _normalise_crosswalk(crosswalk)
    by_source = {str(key): group.copy() for key, group in prepared.groupby("dz2011", sort=False)}
    work = pd.DataFrame({"source_code": source_home.loc[dz2011_mask]}, index=result.index[dz2011_mask])
    assigned = 0
    missing_sources: list[str] = []
    assigned_targets: set[str] = set()

    ev_id_series = (
        result["EV_ID"].astype(str)
        if "EV_ID" in result.columns
        else pd.Series(result.index.astype(str), index=result.index)
    )
    for source_code, row_index in work.groupby("source_code", sort=True).groups.items():
        source_key = str(source_code)
        choices = by_source.get(source_key)
        if choices is None or choices.empty:
            missing_sources.append(source_key)
            continue

        labels = list(row_index)
        n_rows = len(labels)
        target_codes = choices["dz2022"].astype(str).to_numpy(dtype=object)
        weights = choices["area_weight"].to_numpy(dtype=float)
        counts = _target_counts(n_rows, weights)
        replacement = np.repeat(target_codes, counts).astype(object)
        if len(replacement) != n_rows:
            raise RuntimeError("Internal Scotland geography assignment length mismatch")

        order = _stable_order_for_rows(ev_id_series.loc[labels], labels, seed=assignment_seed)
        ordered_labels = np.asarray(labels, dtype=object)[order].tolist()
        result.loc[ordered_labels, "home_lsoa"] = replacement
        assigned += n_rows
        assigned_targets.update(str(code) for code in replacement)

    meta = _metadata(
        applied=assigned > 0,
        crosswalk=crosswalk_metadata,
        rows_seen_scotland_dz2011=rows_seen,
        rows_reassigned=assigned,
        rows_unmapped=rows_seen - assigned,
        unique_dz2011_seen=int(work["source_code"].nunique()),
        unique_dz2022_assigned=len(assigned_targets),
        missing_source_examples=sorted(missing_sources)[:5],
    )
    result.attrs["scotland_geography_unification"] = meta
    return result, meta, crosswalk_metadata
