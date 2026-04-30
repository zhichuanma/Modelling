from __future__ import annotations

import numpy as np

from mobility.bus.data_loader import load_all_blocks
from mobility.bus.selection import render_block_identity_card, sample_protagonist_block


def test_sample_protagonist_block_is_seeded_and_matches_constraints() -> None:
    df = load_all_blocks()
    rng_a = np.random.default_rng(20260430)
    rng_b = np.random.default_rng(20260430)

    block_a = sample_protagonist_block(df, rng_a)
    block_b = sample_protagonist_block(df, rng_b)
    assert block_a == block_b

    block_df = df[df["block_id"].astype(str) == block_a]
    assert block_df["block_source"].eq("native").all()
    assert not (block_df["end_h"] >= 24.0).any()
    assert 10 <= len(block_df) <= 30

    card = render_block_identity_card(df, block_a)
    assert card.loc[0, "service_day_label"] == "a representative service day"
    assert "date" not in card.columns
