"""Stage 1 block-template sampling for depot-only EV bus stock scenarios."""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from .annual_lsoa_region import prefix_region_key


DEFAULT_SEED = 20260603
FULL_EV_INVENTORY_MODE = "full_ev_inventory"
PILOT_MODE = "pilot"
MAX_REGION_SAMPLE_SHARE = 0.35


def distance_bin(distance_km: Any) -> str:
    value = float(pd.to_numeric(pd.Series([distance_km]), errors="coerce").iloc[0])
    if not np.isfinite(value):
        return "unknown_distance"
    if value < 50.0:
        return "lt_50km"
    if value < 100.0:
        return "50_100km"
    if value < 200.0:
        return "100_200km"
    return "ge_200km"


def duration_bin(duration_h: Any) -> str:
    value = float(pd.to_numeric(pd.Series([duration_h]), errors="coerce").iloc[0])
    if not np.isfinite(value):
        return "unknown_duration"
    if value < 4.0:
        return "lt_4h"
    if value < 8.0:
        return "4_8h"
    if value < 12.0:
        return "8_12h"
    return "ge_12h"


def ensure_sampling_columns(block_templates: pd.DataFrame) -> pd.DataFrame:
    out = block_templates.copy()
    if "region_key" not in out.columns:
        source = out["end_lsoa"] if "end_lsoa" in out.columns else pd.Series("", index=out.index)
        out["region_key"] = source.fillna("").astype(str).map(prefix_region_key)
    out["region_key"] = out["region_key"].fillna("unknown").astype(str).replace("", "unknown")
    out["distance_bin"] = out["passenger_distance_km"].map(distance_bin)
    out["duration_bin"] = out["duration_h"].map(duration_bin)
    if "block_source" not in out.columns:
        out["block_source"] = "unknown"
    out["sampling_stratum"] = (
        out["region_key"].astype(str)
        + "|"
        + out["distance_bin"].astype(str)
        + "|"
        + out["duration_bin"].astype(str)
        + "|"
        + out["block_source"].fillna("unknown").astype(str)
    )
    return out


def sample_block_templates(
    block_templates: pd.DataFrame,
    *,
    n_valid_ev_bus_instances: int,
    sample_mode: str = FULL_EV_INVENTORY_MODE,
    n_blocks: int | None = None,
    seed: int = DEFAULT_SEED,
    exclude_block_template_ids: set[str] | None = None,
    max_region_sample_share: float = MAX_REGION_SAMPLE_SHARE,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Sample block templates without replacement using proportional strata."""
    if sample_mode not in {FULL_EV_INVENTORY_MODE, PILOT_MODE}:
        raise ValueError(f"Unsupported sample_mode: {sample_mode}")
    target = int(n_valid_ev_bus_instances) if sample_mode == FULL_EV_INVENTORY_MODE else int(n_blocks or 0)
    if sample_mode == PILOT_MODE and not n_blocks:
        raise ValueError("pilot mode requires --n-blocks.")
    if target <= 0:
        raise ValueError("n_blocks must be positive.")

    pool = ensure_sampling_columns(block_templates)
    if exclude_block_template_ids:
        exclude = {str(value) for value in exclude_block_template_ids}
        pool = pool.loc[~pool["block_template_id"].astype(str).isin(exclude)].copy()
    if target > len(pool):
        raise ValueError(f"Cannot sample {target} block templates without replacement from {len(pool)} available rows.")

    allocation = _allocate_by_stratum(pool, target)
    rng = np.random.default_rng(int(seed))
    sampled_parts: list[pd.DataFrame] = []
    for row in allocation.itertuples(index=False):
        if int(row.n_sampled) <= 0:
            continue
        group = pool.loc[pool["sampling_stratum"].eq(row.sampling_stratum)]
        positions = rng.choice(group.index.to_numpy(), size=int(row.n_sampled), replace=False)
        sampled_parts.append(pool.loc[positions].copy())
    sampled = pd.concat(sampled_parts, ignore_index=True) if sampled_parts else pool.iloc[0:0].copy()
    sampled["_sample_random"] = rng.random(len(sampled))
    sampled = sampled.sort_values(["_sample_random", "block_template_id"], kind="stable").drop(columns=["_sample_random"]).reset_index(drop=True)
    sampled.insert(0, "sample_seq", np.arange(len(sampled), dtype=int))
    sampled["sample_mode"] = sample_mode
    sampled["sampling_seed"] = int(seed)
    weight_lookup = allocation.set_index("sampling_stratum")["sample_weight"].to_dict()
    sampled["sample_weight"] = sampled["sampling_stratum"].map(weight_lookup).astype(float)
    sampled["weighting_mode"] = "sample_weight_diagnostic_only"

    diagnostics = _diagnostics(pool, sampled, allocation, sample_mode, target, seed, max_region_sample_share)
    return sampled.reset_index(drop=True), diagnostics


def _allocate_by_stratum(pool: pd.DataFrame, target: int) -> pd.DataFrame:
    sizes = pool.groupby("sampling_stratum", sort=True).size().rename("n_available").reset_index()
    sizes["raw_quota"] = sizes["n_available"] / float(len(pool)) * int(target)
    sizes["n_sampled"] = np.floor(sizes["raw_quota"]).astype(int)
    remainder = int(target - sizes["n_sampled"].sum())
    if remainder > 0:
        order = sizes.assign(frac=sizes["raw_quota"] - sizes["n_sampled"]).sort_values(
            ["frac", "n_available", "sampling_stratum"],
            ascending=[False, False, True],
            kind="stable",
        )
        for idx in order.index[:remainder]:
            sizes.loc[idx, "n_sampled"] += 1
    sizes["sample_weight"] = np.where(sizes["n_sampled"].gt(0), sizes["n_available"] / sizes["n_sampled"], np.nan)
    return sizes.sort_values("sampling_stratum", kind="stable").reset_index(drop=True)


def _diagnostics(
    pool: pd.DataFrame,
    sampled: pd.DataFrame,
    allocation: pd.DataFrame,
    sample_mode: str,
    target: int,
    seed: int,
    max_region_sample_share: float,
) -> pd.DataFrame:
    diag = allocation.copy()
    diag["sample_mode"] = sample_mode
    diag["requested_n_blocks"] = int(target)
    diag["sampling_seed"] = int(seed)
    diag["without_replacement"] = True
    diag["n_pool_total"] = int(len(pool))
    diag["region_cap_is_safety_net_not_balancer"] = True

    available_region = pool["region_key"].value_counts(normalize=True).rename("available_region_share")
    sampled_region = sampled["region_key"].value_counts(normalize=True).rename("sampled_region_share")
    region = pd.concat([available_region, sampled_region], axis=1).fillna(0.0).reset_index().rename(columns={"index": "region_key"})
    region["region_cap_breach"] = (region["sampled_region_share"] > float(max_region_sample_share)) & (
        region["available_region_share"] <= float(max_region_sample_share)
    )
    diag["any_region_cap_breach"] = bool(region["region_cap_breach"].any())
    diag["max_region_sample_share"] = float(max_region_sample_share)
    diag["region_distribution"] = [region.to_dict(orient="records")] + [None] * (len(diag) - 1)
    return diag


__all__ = [
    "DEFAULT_SEED",
    "FULL_EV_INVENTORY_MODE",
    "MAX_REGION_SAMPLE_SHARE",
    "PILOT_MODE",
    "distance_bin",
    "duration_bin",
    "ensure_sampling_columns",
    "sample_block_templates",
]
