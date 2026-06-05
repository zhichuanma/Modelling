from __future__ import annotations

import pandas as pd

from mobility.bus.depot_only_sampling import PILOT_MODE, ensure_sampling_columns, sample_block_templates


def _blocks(n: int = 20) -> pd.DataFrame:
    rows = []
    regions = ["London", "Wales", "Scotland", "North West"]
    for idx in range(n):
        rows.append(
            {
                "block_template_id": f"bt{idx}",
                "block_id": f"B{idx}",
                "agency_id": "OP",
                "service_id": "S",
                "block_source": "native" if idx % 2 else "inferred",
                "start_h": 8.0,
                "end_h": 10.0 + (idx % 5),
                "duration_h": 2.0 + (idx % 5),
                "passenger_distance_km": 20.0 + idx * 10.0,
                "start_lat": 51.0,
                "start_lon": -1.0,
                "end_lat": 51.0,
                "end_lon": -1.0,
                "start_lsoa": "E1",
                "end_lsoa": "E1",
                "region_key": regions[idx % len(regions)],
            }
        )
    return pd.DataFrame(rows)


def test_full_mode_samples_exactly_valid_ev_count() -> None:
    sampled, _ = sample_block_templates(_blocks(), n_valid_ev_bus_instances=7, seed=1)
    assert len(sampled) == 7
    assert sampled["sample_mode"].eq("full_ev_inventory").all()


def test_pilot_mode_samples_requested_number() -> None:
    sampled, _ = sample_block_templates(_blocks(), n_valid_ev_bus_instances=7, sample_mode=PILOT_MODE, n_blocks=5, seed=1)
    assert len(sampled) == 5
    assert sampled["sample_mode"].eq("pilot").all()


def test_without_replacement_and_deterministic() -> None:
    first, _ = sample_block_templates(_blocks(), n_valid_ev_bus_instances=8, seed=99)
    second, _ = sample_block_templates(_blocks(), n_valid_ev_bus_instances=8, seed=99)
    assert first["block_template_id"].is_unique
    assert first["block_template_id"].tolist() == second["block_template_id"].tolist()


def test_region_key_uses_gor_country_level_not_lad() -> None:
    prepared = ensure_sampling_columns(_blocks())
    assert {"London", "Wales", "Scotland", "North West"}.issubset(set(prepared["region_key"]))
    assert "E09000001" not in set(prepared["region_key"])


def test_sample_weight_definition_and_diagnostics_written() -> None:
    sampled, diag = sample_block_templates(_blocks(12), n_valid_ev_bus_instances=6, seed=3)
    for row in diag[diag["n_sampled"].gt(0)].itertuples(index=False):
        assert row.sample_weight == row.n_available / row.n_sampled
    assert {"n_available", "n_sampled", "sample_weight", "without_replacement"}.issubset(diag.columns)
    assert sampled["sample_weight"].notna().all()


def test_region_cap_is_safety_net_not_balancer() -> None:
    blocks = _blocks(10)
    blocks["region_key"] = ["London"] * 9 + ["Wales"]
    sampled, diag = sample_block_templates(blocks, n_valid_ev_bus_instances=10, seed=4)
    assert sampled["region_key"].value_counts(normalize=True)["London"] == 0.9
    assert bool(diag["region_cap_is_safety_net_not_balancer"].iloc[0])
    assert not bool(diag["any_region_cap_breach"].iloc[0])
