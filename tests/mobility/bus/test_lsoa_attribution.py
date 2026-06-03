from __future__ import annotations

import pandas as pd

from mobility.bus.lsoa_attribution import chain_home_lsoa, lsoa_view


def test_chain_home_lsoa_uses_mode_of_end_lsoa() -> None:
    blocks = pd.DataFrame(
        {
            "block_instance_id": ["B1", "B2", "B3", "B4"],
            "end_lsoa": ["E01000001", "E01000001", "E01000002", "E01000003"],
        }
    )
    chains = pd.DataFrame(
        {
            "chain_id": ["C1", "C1", "C1", "C2"],
            "block_instance_id": ["B1", "B2", "B3", "B4"],
        }
    )

    home = chain_home_lsoa(blocks, chains)

    assert home.loc["C1"] == "E01000001"
    assert home.loc["C2"] == "E01000003"


def test_chain_home_lsoa_ignores_blank_end_lsoa() -> None:
    blocks = pd.DataFrame(
        {
            "block_instance_id": ["B1", "B2", "B3"],
            "end_lsoa": ["", "E01000099", None],
        }
    )
    chains = pd.DataFrame(
        {
            "chain_id": ["C9", "C9", "C9"],
            "block_instance_id": ["B1", "B2", "B3"],
        }
    )

    home = chain_home_lsoa(blocks, chains)

    assert home.loc["C9"] == "E01000099"


def test_chain_home_lsoa_returns_unknown_when_all_missing() -> None:
    blocks = pd.DataFrame(
        {
            "block_instance_id": ["B1", "B2"],
            "end_lsoa": [None, ""],
        }
    )
    chains = pd.DataFrame(
        {
            "chain_id": ["C0", "C0"],
            "block_instance_id": ["B1", "B2"],
        }
    )

    home = chain_home_lsoa(blocks, chains)

    assert home.loc["C0"] == "unknown"


def test_lsoa_view_computes_gap_ratio_and_sorts_descending() -> None:
    per_chain = pd.DataFrame(
        {
            "chain_id": ["C1", "C2", "C3"],
            "energy_charged_kwh": [100.0, 300.0, 50.0],
            "depot_charge_kw": [10.0, 20.0, 5.0],
        }
    )
    chain_to_lsoa = pd.Series(
        {"C1": "E01000001", "C2": "E01000002", "C3": "E01000001"},
        name="home_lsoa",
    )

    view = lsoa_view(per_chain, chain_to_lsoa, hours_per_year=10)

    assert view["lsoa_code"].tolist() == ["E01000002", "E01000001"]
    assert view.loc[0, "n_home_chains"] == 1
    assert view.loc[0, "sim_kwh_year"] == 300.0
    assert view.loc[0, "depot_total_kw"] == 20.0
    assert view.loc[0, "ceiling_kwh_year"] == 200.0
    assert view.loc[0, "gap_ratio"] == 1.5
    assert view.loc[1, "n_home_chains"] == 2
