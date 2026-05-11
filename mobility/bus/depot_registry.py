"""Depot registry construction for the M1 bus chain-mode simulator."""

from __future__ import annotations

from pathlib import Path
import re
import warnings

import numpy as np
import pandas as pd

from mobility.core.spatial import (
    load_lsoa_centroids,
    nearest_lsoa_for_points,
    query_lsoa_polygons,
)


MODELLING_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DROPPED_DEPOTS_PATH = (
    MODELLING_ROOT / "outputs" / "diagnostics" / "depot_registry_dropped.parquet"
)
LSOA_FALLBACK_MAX_KM = 0.25

DEPOT_COLUMNS = [
    "depot_id",
    "agency_id",
    "operator_noc",
    "lat",
    "lon",
    "lsoa_code",
    "lsoa_method",
    "depot_source",
    "depot_confidence",
    "depot_assignment_method",
    "override_reason",
    "manual_review_flag",
    "n_candidate_vehicles",
]


def _normalise_name(value: object) -> str:
    text = "" if pd.isna(value) else str(value)
    return re.sub(r"[^a-z0-9]+", "", text.lower())


def _agency_lookup(agency_df: pd.DataFrame) -> tuple[dict[str, str], dict[str, str], dict[str, str]]:
    if agency_df is None or agency_df.empty:
        return {}, {}, {}
    agency = agency_df.copy()
    agency["agency_id"] = agency["agency_id"].astype(str)
    noc_lookup: dict[str, str] = {}
    if "agency_noc" in agency.columns:
        for row in agency[["agency_id", "agency_noc"]].itertuples(index=False):
            noc = "" if pd.isna(row.agency_noc) else str(row.agency_noc).strip().upper()
            if noc and noc not in noc_lookup:
                noc_lookup[noc] = str(row.agency_id)
    name_lookup = {
        _normalise_name(row.agency_name): str(row.agency_id)
        for row in agency[["agency_id", "agency_name"]].itertuples(index=False)
        if _normalise_name(row.agency_name)
    } if "agency_name" in agency.columns else {}
    id_to_noc = {
        str(row.agency_id): ("" if pd.isna(row.agency_noc) else str(row.agency_noc).strip().upper())
        for row in agency[["agency_id", "agency_noc"]].itertuples(index=False)
    } if "agency_noc" in agency.columns else {}
    return noc_lookup, name_lookup, id_to_noc


def _agency_ids(blocks_df: pd.DataFrame, agency_df: pd.DataFrame) -> list[str]:
    ids: set[str] = set()
    if blocks_df is not None and "agency_id" in blocks_df.columns:
        ids.update(blocks_df["agency_id"].dropna().astype(str))
    if agency_df is not None and "agency_id" in agency_df.columns:
        ids.update(agency_df["agency_id"].dropna().astype(str))
    return sorted(ids)


def _coords_for_agency(blocks_df: pd.DataFrame, agency_id: str) -> np.ndarray:
    if blocks_df is None or blocks_df.empty:
        return np.empty((0, 2), dtype=float)
    required = {"agency_id", "start_lat", "start_lon"}
    if not required.issubset(blocks_df.columns):
        return np.empty((0, 2), dtype=float)
    data = blocks_df.loc[
        blocks_df["agency_id"].astype(str).eq(str(agency_id)),
        ["start_lat", "start_lon"],
    ].copy()
    for col in ("start_lat", "start_lon"):
        data[col] = pd.to_numeric(data[col], errors="coerce")
    data = data.dropna()
    if data.empty:
        return np.empty((0, 2), dtype=float)
    return data.to_numpy(dtype=float)


def _geometric_median(points: np.ndarray) -> tuple[float, float]:
    if points.size == 0:
        return float("nan"), float("nan")
    current = np.nanmedian(points, axis=0)
    if not np.isfinite(current).all():
        return float("nan"), float("nan")
    for _ in range(100):
        distances = np.linalg.norm(points - current, axis=1)
        if np.any(distances < 1e-12):
            current = points[int(np.argmin(distances))]
            break
        weights = 1.0 / np.maximum(distances, 1e-12)
        updated = np.sum(points * weights[:, None], axis=0) / np.sum(weights)
        if np.linalg.norm(updated - current) < 1e-10:
            current = updated
            break
        current = updated
    return float(current[0]), float(current[1])


def _operator_centroid(blocks_df: pd.DataFrame, agency_id: str) -> tuple[float, float]:
    points = _coords_for_agency(blocks_df, agency_id)
    return _geometric_median(points)


def _assign_lsoa(registry: pd.DataFrame, lsoa_index: dict | None) -> pd.DataFrame:
    out = registry.copy()
    n = len(out)
    codes = np.full(n, "", dtype=object)
    methods = np.full(n, "no_match", dtype=object)
    lat = pd.to_numeric(out["lat"], errors="coerce").to_numpy(dtype=float)
    lon = pd.to_numeric(out["lon"], errors="coerce").to_numpy(dtype=float)
    valid = np.isfinite(lat) & np.isfinite(lon)

    if valid.any() and lsoa_index and {"codes", "bboxes", "polygons"}.issubset(lsoa_index):
        polygon_codes, _, polygon_methods = query_lsoa_polygons(lat[valid], lon[valid], lsoa_index)
        target = np.flatnonzero(valid)
        codes[target] = polygon_codes
        methods[target] = polygon_methods

    fallback_mask = valid & (methods == "no_match") & bool(lsoa_index)
    if fallback_mask.any():
        try:
            centroids = load_lsoa_centroids()
            fallback_codes, _ = nearest_lsoa_for_points(
                lat[fallback_mask],
                lon[fallback_mask],
                centroids,
                max_distance_km=LSOA_FALLBACK_MAX_KM,
            )
        except (FileNotFoundError, KeyError, ValueError, pd.errors.EmptyDataError):
            fallback_codes = np.full(int(fallback_mask.sum()), "", dtype=object)
        target = np.flatnonzero(fallback_mask)
        if fallback_codes.size:
            matched = fallback_codes != ""
            codes[target[matched]] = fallback_codes[matched]
            methods[target[matched]] = "centroid_fallback"

    out["lsoa_code"] = np.where(codes == "", np.nan, codes)
    out["lsoa_method"] = methods
    out["manual_review_flag"] = out["lsoa_code"].isna()
    return out


def _match_txc_to_agency(
    txc_garages: pd.DataFrame,
    agency_df: pd.DataFrame,
) -> pd.DataFrame:
    if txc_garages is None or txc_garages.empty:
        return pd.DataFrame()
    noc_lookup, name_lookup, _ = _agency_lookup(agency_df)
    matched = txc_garages.copy()
    agency_ids: list[str] = []
    methods: list[str] = []
    for row in matched.itertuples(index=False):
        noc = str(getattr(row, "operator_noc", "") or "").strip().upper()
        name = _normalise_name(getattr(row, "operator_name", ""))
        if noc and noc in noc_lookup:
            agency_ids.append(noc_lookup[noc])
            methods.append("txc_operator_noc")
        elif name and name in name_lookup:
            agency_ids.append(name_lookup[name])
            methods.append("txc_operator_name")
        else:
            agency_ids.append("")
            methods.append("unmatched_operator")
    matched["agency_id"] = agency_ids
    matched["depot_assignment_method"] = methods
    return matched


def _write_dropped(dropped: pd.DataFrame, path: Path) -> None:
    if dropped.empty:
        return
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    try:
        dropped.to_parquet(out, index=False)
    except ImportError:
        warnings.warn(
            f"Unable to write dropped depot diagnostics without parquet support: {out}",
            RuntimeWarning,
            stacklevel=2,
        )


def _txc_rows(
    blocks_df: pd.DataFrame,
    matched_txc: pd.DataFrame,
) -> list[dict]:
    rows: list[dict] = []
    if matched_txc.empty:
        return rows
    matched = matched_txc[matched_txc["agency_id"].astype(str).ne("")].copy()
    for index, row in enumerate(matched.itertuples(index=False), start=1):
        lat = pd.to_numeric(getattr(row, "approx_lat", np.nan), errors="coerce")
        lon = pd.to_numeric(getattr(row, "approx_lon", np.nan), errors="coerce")
        has_coords = bool(np.isfinite(lat) and np.isfinite(lon))
        if not has_coords:
            lat, lon = _operator_centroid(blocks_df, str(row.agency_id))
        rows.append(
            {
                "depot_id": f"txc_{row.agency_id}_{index:03d}",
                "agency_id": str(row.agency_id),
                "operator_noc": str(getattr(row, "operator_noc", "") or ""),
                "lat": float(lat),
                "lon": float(lon),
                "lsoa_code": np.nan,
                "lsoa_method": "no_match",
                "depot_source": "txc_garage",
                "depot_confidence": "high" if has_coords else "medium",
                "depot_assignment_method": str(row.depot_assignment_method)
                if has_coords
                else f"{row.depot_assignment_method}_operator_centroid_fallback",
                "override_reason": "",
                "manual_review_flag": False,
                "n_candidate_vehicles": 0,
            }
        )
    return rows


def _external_rows(external_depots: pd.DataFrame | None) -> list[dict]:
    if external_depots is None or external_depots.empty:
        return []
    rows: list[dict] = []
    for idx, row in enumerate(external_depots.itertuples(index=False), start=1):
        agency_id = str(getattr(row, "agency_id", ""))
        rows.append(
            {
                "depot_id": str(getattr(row, "depot_id", f"external_{agency_id}_{idx:03d}")),
                "agency_id": agency_id,
                "operator_noc": str(getattr(row, "operator_noc", "")),
                "lat": float(getattr(row, "lat")),
                "lon": float(getattr(row, "lon")),
                "lsoa_code": np.nan,
                "lsoa_method": "no_match",
                "depot_source": str(getattr(row, "depot_source", "external")),
                "depot_confidence": str(getattr(row, "depot_confidence", "medium")),
                "depot_assignment_method": str(getattr(row, "depot_assignment_method", "external")),
                "override_reason": str(getattr(row, "override_reason", "")),
                "manual_review_flag": False,
                "n_candidate_vehicles": 0,
            }
        )
    return rows


def _virtual_rows(
    blocks_df: pd.DataFrame,
    agency_ids: list[str],
    covered_agencies: set[str],
    id_to_noc: dict[str, str],
) -> list[dict]:
    rows: list[dict] = []
    for agency_id in agency_ids:
        if agency_id in covered_agencies:
            continue
        lat, lon = _operator_centroid(blocks_df, agency_id)
        rows.append(
            {
                "depot_id": f"virtual_{agency_id}",
                "agency_id": agency_id,
                "operator_noc": id_to_noc.get(agency_id, ""),
                "lat": lat,
                "lon": lon,
                "lsoa_code": np.nan,
                "lsoa_method": "no_match",
                "depot_source": "virtual_operator_centroid",
                "depot_confidence": "low",
                "depot_assignment_method": "virtual_operator_centroid",
                "override_reason": "",
                "manual_review_flag": False,
                "n_candidate_vehicles": 0,
            }
        )
    return rows


def build_depot_registry(
    blocks_df: pd.DataFrame,
    agency_df: pd.DataFrame,
    stops_df: pd.DataFrame,
    lsoa_index: dict,
    txc_garages_df: pd.DataFrame | None = None,
    external_depots: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """Build a tiered fixed-depot registry for M1.

    Depot locations come from TxC garages when matchable, external curated
    rows when supplied, or one virtual operator-centroid depot per uncovered
    agency. Stop clustering is deliberately not used.
    """
    del stops_df  # Blocks already carry first-stop coordinates for M1 fallback.
    _, _, id_to_noc = _agency_lookup(agency_df)
    all_agencies = _agency_ids(blocks_df, agency_df)

    matched_txc = _match_txc_to_agency(txc_garages_df, agency_df)
    if not matched_txc.empty:
        dropped = matched_txc[matched_txc["agency_id"].astype(str).eq("")].copy()
        _write_dropped(dropped, DEFAULT_DROPPED_DEPOTS_PATH)

    rows = _txc_rows(blocks_df, matched_txc)
    rows.extend(_external_rows(external_depots))
    covered = {str(row["agency_id"]) for row in rows if np.isfinite(row["lat"]) and np.isfinite(row["lon"])}
    rows.extend(_virtual_rows(blocks_df, all_agencies, covered, id_to_noc))
    if not rows:
        return pd.DataFrame(columns=DEPOT_COLUMNS)

    registry = pd.DataFrame(rows, columns=DEPOT_COLUMNS)
    registry = registry.dropna(subset=["lat", "lon"]).copy()
    registry = registry.sort_values(["agency_id", "depot_confidence", "depot_id"], kind="stable")
    registry = registry.drop_duplicates("depot_id", keep="first").reset_index(drop=True)
    registry = _assign_lsoa(registry, lsoa_index)
    return registry.loc[:, DEPOT_COLUMNS].reset_index(drop=True)
