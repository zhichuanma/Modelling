from __future__ import annotations

import numpy as np

from mobility.bus.annual_simulation import annual_load_matrix_to_frame


def test_annual_load_matrix_to_frame_vectorized_shape_and_values() -> None:
    matrix = np.arange(3 * 96, dtype=float).reshape(3, 96)

    frame = annual_load_matrix_to_frame(matrix, "2026-04-17", "2026-04-19")

    assert len(frame) == 288
    assert frame.loc[0, "date"].strftime("%Y-%m-%d") == "2026-04-17"
    assert frame.loc[0, "step_index"] == 0
    assert frame.loc[95, "step_index"] == 95
    assert frame.loc[96, "date"].strftime("%Y-%m-%d") == "2026-04-18"
    assert frame.loc[96, "step_index"] == 0
    assert frame.loc[97, "hour_of_day"] == 0.25
    assert frame["load_kw"].to_numpy().tolist() == matrix.ravel().tolist()
