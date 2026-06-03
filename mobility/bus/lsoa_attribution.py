"""Post-hoc LSOA attribution for the bus M1 chain-mode simulator.

Mirrors :mod:`mobility.coach.lsoa_attribution` but operates on block instances
and the ``vehicle_assignments`` chain mapping. ``end_lsoa`` is expected to be
attached upstream by :func:`mobility.bus.data_loader.attach_lsoa`.
"""

from __future__ import annotations

import numpy as np
import pandas as pd


def _chain_col(chains: pd.DataFrame) -> str:
    for column in ("bus_chain_template_id", "bus_chain_id", "chain_id"):
        if column in chains.columns:
            return column
    raise ValueError("chains must include bus_chain_id or chain_id.")


def _mode_or_unknown(values: pd.Series) -> str:
    cleaned = values.dropna().astype(str)
    cleaned = cleaned[cleaned.str.strip().ne("")]
    if cleaned.empty:
        return "unknown"
    modes = cleaned.mode()
    return str(modes.iloc[0]) if not modes.empty else "unknown"


def chain_home_lsoa(blocks: pd.DataFrame, chains: pd.DataFrame) -> pd.Series:
    """Return ``chain_id -> home_lsoa`` using mode of ``end_lsoa`` over blocks.

    Parameters
    ----------
    blocks:
        Per-block-instance records. Must include ``block_instance_id`` and
        ``end_lsoa`` (attach via :func:`mobility.bus.data_loader.attach_lsoa`).
    chains:
        Per-(chain, block_instance) assignment records (e.g. the
        ``vehicle_assignments`` table). Must include ``block_instance_id`` and
        one of ``bus_chain_template_id`` / ``bus_chain_id`` / ``chain_id``.
    """
    if "block_instance_id" not in blocks.columns or "end_lsoa" not in blocks.columns:
        raise ValueError("blocks must include block_instance_id and end_lsoa.")
    if "block_instance_id" not in chains.columns:
        raise ValueError("chains must include block_instance_id.")
    chain_col = _chain_col(chains)
    merged = chains.loc[:, [chain_col, "block_instance_id"]].merge(
        blocks.loc[:, ["block_instance_id", "end_lsoa"]],
        on="block_instance_id",
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
    charge_kw_col: str = "depot_charge_kw",
) -> pd.DataFrame:
    """Aggregate annual bus charging demand and depot capacity by LSOA.

    Mirrors :func:`mobility.coach.lsoa_attribution.lsoa_view`. ``charge_kw_col``
    defaults to ``depot_charge_kw`` to match bus vehicle-parameter naming;
    callers using the M1 ``vehicle_assignments`` schema should pass
    ``charge_kw_col="ac_charge_kw_max"``.
    """
    required = {"chain_id", charge_kw_col}
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
    demand[charge_kw_col] = pd.to_numeric(demand[charge_kw_col], errors="coerce").fillna(0.0)
    grouped = (
        demand.groupby("lsoa_code", as_index=False)
        .agg(
            n_home_chains=("chain_id", "nunique"),
            sim_kwh_year=(energy_col, "sum"),
            depot_total_kw=(charge_kw_col, "sum"),
        )
    )
    grouped["ceiling_kwh_year"] = grouped["depot_total_kw"] * float(hours_per_year)
    grouped["gap_ratio"] = np.where(
        grouped["ceiling_kwh_year"].gt(0.0),
        grouped["sim_kwh_year"] / grouped["ceiling_kwh_year"],
        np.nan,
    )
    return grouped.sort_values("sim_kwh_year", ascending=False, kind="stable").reset_index(drop=True)


__all__ = ["chain_home_lsoa", "lsoa_view"]
