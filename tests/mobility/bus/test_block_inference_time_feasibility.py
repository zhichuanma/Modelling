from __future__ import annotations

import pandas as pd

from mobility.bus.block_inference import BlockInferenceConfig, infer_blocks


def _rows(candidate_start_h: float) -> pd.DataFrame:
    return pd.DataFrame(
        [
            ("t0", "OP", "S1", "R1", 7.0, 8.0, 51.0, -0.1, 51.0, -0.1, "A", "B"),
            ("t1", "OP", "S1", "R1", candidate_start_h, candidate_start_h + 1.0, 51.27, -0.1, 51.28, -0.1, "C", "D"),
        ],
        columns=[
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
        ],
    )


def test_infer_blocks_rejects_time_infeasible_deadhead() -> None:
    observed = infer_blocks(
        _rows(8.05),
        BlockInferenceConfig(max_inferred_deadhead_km=100.0),
    )

    assert observed.nunique() == 2


def test_infer_blocks_allows_time_feasible_deadhead() -> None:
    observed = infer_blocks(
        _rows(9.5),
        BlockInferenceConfig(max_inferred_deadhead_km=100.0),
    )

    assert observed.nunique() == 1


def test_same_stop_candidate_beats_five_km_deadhead_candidate() -> None:
    df = pd.DataFrame(
        [
            ("same_stop_predecessor", "OP", "S1", "R1", 7.0, 8.0, 51.0, -0.1, 51.0, -0.1, "A", "X"),
            ("deadhead_predecessor", "OP", "S1", "R1", 7.1, 8.0, 51.0, -0.2, 51.045, -0.1, "B", "Y"),
            ("candidate", "OP", "S1", "R2", 8.5, 9.0, 51.0, -0.1, 51.0, -0.2, "X", "Z"),
        ],
        columns=[
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
        ],
    )

    observed = infer_blocks(df, BlockInferenceConfig(max_inferred_deadhead_km=10.0))

    assert observed.iloc[2] == observed.iloc[0]
    assert observed.iloc[2] != observed.iloc[1]
