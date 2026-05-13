"""Post-hoc LSOA attribution for synthetic coach annual chains."""

from __future__ import annotations

import numpy as np
import pandas as pd


def _chain_col(chains: pd.DataFrame) -> str:
    for column in ("coach_chain_template_id", "coach_chain_id", "chain_id"):
        if column in chains.columns:
            return column
    raise ValueError("chains must include coach_chain_id or chain_id.")


def _mode_or_unknown(values: pd.Series) -> str:
    cleaned = values.dropna().astype(str)
    cleaned = cleaned[cleaned.str.strip().ne("")]
    if cleaned.empty:
        return "unknown"
    modes = cleaned.mode()
    return str(modes.iloc[0]) if not modes.empty else "unknown"


def chain_home_lsoa(journeys: pd.DataFrame, chains: pd.DataFrame) -> pd.Series:
    """Return ``chain_id -> home_lsoa`` using mode of ``end_lsoa`` over journeys."""
    if "journey_id" not in journeys.columns or "end_lsoa" not in journeys.columns:
        raise ValueError("journeys must include journey_id and end_lsoa.")
    if "journey_id" not in chains.columns:
        raise ValueError("chains must include journey_id.")
    chain_col = _chain_col(chains)
    merged = chains.loc[:, [chain_col, "journey_id"]].merge(
        journeys.loc[:, ["journey_id", "end_lsoa"]],
        on="journey_id",
        how="left",
    )
    home = merged.groupby(chain_col, sort=True)["end_lsoa"].agg(_mode_or_unknown)
    home.index = home.index.astype(str)
    home.name = "home_lsoa"
    return home


def lsoa_view(
    per_chain_df: pd.DataFrame,
    chain_to_lsoa: pd.Series,
    *,
    hours_per_year: int = 8760,
) -> pd.DataFrame:
    """Aggregate annual coach charging demand and synthetic terminus capacity by LSOA."""
    required = {"chain_id", "terminus_charge_kw"}
    missing = required - set(per_chain_df.columns)
    if missing:
        raise ValueError(f"per_chain_df is missing required columns: {sorted(missing)}")
    energy_col = "energy_charged_kwh" if "energy_charged_kwh" in per_chain_df.columns else "total_kwh"
    if energy_col not in per_chain_df.columns:
        raise ValueError("per_chain_df must include energy_charged_kwh or total_kwh.")

    mapping = chain_to_lsoa.rename("lsoa_code").reset_index()
    mapping.columns = ["chain_id", "lsoa_code"]
    demand = per_chain_df.merge(mapping, on="chain_id", how="left")
    demand["lsoa_code"] = demand["lsoa_code"].fillna("unknown").replace("", "unknown")
    demand[energy_col] = pd.to_numeric(demand[energy_col], errors="coerce").fillna(0.0)
    demand["terminus_charge_kw"] = pd.to_numeric(demand["terminus_charge_kw"], errors="coerce").fillna(0.0)
    grouped = (
        demand.groupby("lsoa_code", as_index=False)
        .agg(
            n_home_chains=("chain_id", "nunique"),
            sim_kwh_year=(energy_col, "sum"),
            terminus_total_kw=("terminus_charge_kw", "sum"),
        )
    )
    grouped["ceiling_kwh_year"] = grouped["terminus_total_kw"] * float(hours_per_year)
    grouped["gap_ratio"] = np.where(
        grouped["ceiling_kwh_year"].gt(0.0),
        grouped["sim_kwh_year"] / grouped["ceiling_kwh_year"],
        np.nan,
    )
    return grouped.sort_values("sim_kwh_year", ascending=False, kind="stable").reset_index(drop=True)


__all__ = ["chain_home_lsoa", "lsoa_view"]
