from __future__ import annotations

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
    observed = infer_blocks(
        subset[need],
        BlockInferenceConfig(same_stop_bonus_h=1.0, route_continuity_bonus_h=0.5),
    )

    assert set(observed.astype(str)) == set(subset["block_id"].astype(str))
