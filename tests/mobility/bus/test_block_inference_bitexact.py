from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from mobility.bus.block_inference import BlockInferenceConfig, infer_blocks
from mobility.bus.data_loader import load_all_blocks


@pytest.mark.slow
def test_infer_blocks_full_inferred_bit_exact() -> None:
    """Regression over complete inferred groups covering at least 1000 blocks."""
    df = load_all_blocks()
    inferred = df[df["block_source"] == "inferred"].sort_values(
        ["agency_id", "service_id", "start_h", "route_id", "trip_id"]
    )
    groups = []
    block_ids: set[str] = set()
    for _key, group in inferred.groupby(["agency_id", "service_id"], sort=True):
        groups.append(group)
        block_ids.update(str(block_id) for block_id in group["block_id"].unique())
        if len(block_ids) >= 1000:
            break

    subset = pd.concat(groups)
    assert subset["block_id"].nunique() >= 1000

    need = [
        "trip_id",
        "agency_id",
        "service_id",
        "route_id",
        "start_h",
        "end_h",
        "start_lat",
        "start_lon",
        "end_lat",
        "end_lon",
        "start_stop",
        "end_stop",
    ]
    config = BlockInferenceConfig(
        same_stop_bonus_h=1.0,
        route_continuity_bonus_h=0.5,
        max_layover_h=4.0,
        max_shift_h=16.0,
        deadhead_speed_kmh=30.0,
        min_dwell_after_deadhead_h=0.05,
        max_inferred_deadhead_km=5.0,
        deadhead_penalty_h_per_km=0.05,
    )
    observed = infer_blocks(subset[need], config)
    repeated = infer_blocks(subset.sample(frac=1.0, random_state=20260506)[need], config)

    assert observed.notna().all()
    assert set(observed.astype(str)) == set(repeated.astype(str))
    assert set(observed.astype(str)) == set(subset["block_id"].astype(str))


def test_native_rows_match_legacy_backup_when_available() -> None:
    legacy_path = Path("outputs/all_blocks.parquet.legacy.bak")
    if not legacy_path.exists():
        pytest.skip("legacy all_blocks backup is only present after full parquet regeneration")

    old_native = pd.read_parquet(legacy_path)
    old_native = old_native[old_native["block_source"].eq("native")].sort_values(["trip_id", "start_h", "end_h"]).reset_index(drop=True)
    new_native = load_all_blocks()
    new_native = new_native[new_native["block_source"].eq("native")].sort_values(["trip_id", "start_h", "end_h"]).reset_index(drop=True)
    new_native = new_native.loc[:, old_native.columns]

    assert old_native.shape == new_native.shape
    for col in old_native.columns:
        if pd.api.types.is_numeric_dtype(old_native[col]) and pd.api.types.is_numeric_dtype(new_native[col]):
            assert np.allclose(
                old_native[col].to_numpy(dtype=float),
                new_native[col].to_numpy(dtype=float),
                rtol=0.0,
                atol=1e-9,
                equal_nan=True,
            )
        else:
            assert old_native[col].astype("string").fillna("<NA>").equals(new_native[col].astype("string").fillna("<NA>"))
