from __future__ import annotations

import pandas as pd

from mobility.coach.stop_geometry import load_unified_stops


def test_load_unified_stops_merges_naptan_and_custom(tmp_path) -> None:
    naptan = tmp_path / "Stops.csv"
    custom = tmp_path / "CustomStops.csv"
    naptan.write_text(
        "ATCOCode,Latitude,Longitude\nA,51.0,-1.0\nB,52.0,-2.0\n",
        encoding="utf-8",
    )
    custom.write_text(
        "AtcoCode,Latitude,Longitude\nB,53.0,-3.0\nC,54.0,-4.0\n",
        encoding="utf-8",
    )

    stops = load_unified_stops(naptan, custom)

    assert list(stops.columns) == ["stop_point_ref", "lat", "lon", "source"]
    assert stops.set_index("stop_point_ref").loc["A", "source"] == "naptan"
    assert stops.set_index("stop_point_ref").loc["B", "source"] == "custom"
    assert stops.set_index("stop_point_ref").loc["B", "lat"] == 53.0


def test_load_unified_stops_tolerates_missing_naptan(tmp_path) -> None:
    custom = tmp_path / "CustomStops.csv"
    custom.write_text("AtcoCode,Latitude,Longitude\nC,54.0,-4.0\n", encoding="utf-8")

    stops = load_unified_stops(tmp_path / "missing.csv", custom)

    pd.testing.assert_frame_equal(
        stops,
        pd.DataFrame({"stop_point_ref": ["C"], "lat": [54.0], "lon": [-4.0], "source": ["custom"]}),
        check_dtype=False,
    )
