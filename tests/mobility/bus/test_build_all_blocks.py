from __future__ import annotations

from pathlib import Path

import pandas as pd

from mobility.bus.block_inference import BlockInferenceConfig
from mobility.bus.build_all_blocks import OUTPUT_COLUMNS, build_all_blocks, parse_gtfs_time


def _write_csv(path: Path, rows: list[tuple], columns: list[str]) -> None:
    pd.DataFrame(rows, columns=columns).to_csv(path, index=False)


def test_parse_gtfs_time_allows_wrapped_hours() -> None:
    assert parse_gtfs_time("25:30:00") == 25.5
    assert pd.isna(parse_gtfs_time("bad"))


def test_build_all_blocks_uses_current_inference(tmp_path: Path) -> None:
    gtfs = tmp_path / "gtfs"
    out = tmp_path / "out"
    gtfs.mkdir()

    _write_csv(
        gtfs / "routes.txt",
        [("R1", "OP1"), ("R2", "OP1")],
        ["route_id", "agency_id"],
    )
    _write_csv(
        gtfs / "stops.txt",
        [
            ("A", 51.0, -0.10),
            ("B", 51.0, -0.10),
            ("C", 51.018, -0.10),
            ("D", 51.020, -0.10),
            ("E", 51.100, -0.10),
            ("F", 51.120, -0.10),
        ],
        ["stop_id", "stop_lat", "stop_lon"],
    )
    _write_csv(
        gtfs / "trips.txt",
        [
            ("R1", "S1", "native_trip", 0, "NATIVE_1", "SH1"),
            ("R1", "S1", "inf_a", 0, "", "SH1"),
            ("R2", "S1", "inf_b", 0, "", "SH2"),
        ],
        ["route_id", "service_id", "trip_id", "direction_id", "block_id", "shape_id"],
    )
    _write_csv(
        gtfs / "stop_times.txt",
        [
            ("native_trip", "07:00:00", "07:00:00", "A", 1),
            ("native_trip", "07:30:00", "07:30:00", "B", 2),
            ("inf_a", "08:00:00", "08:00:00", "A", 1),
            ("inf_a", "08:30:00", "08:30:00", "B", 2),
            ("inf_b", "09:00:00", "09:00:00", "C", 1),
            ("inf_b", "09:30:00", "09:30:00", "D", 2),
        ],
        ["trip_id", "arrival_time", "departure_time", "stop_id", "stop_sequence"],
    )
    _write_csv(
        gtfs / "shapes.txt",
        [
            ("SH1", 51.0, -0.10, 1),
            ("SH1", 51.0, -0.10, 2),
            ("SH2", 51.018, -0.10, 1),
            ("SH2", 51.020, -0.10, 2),
        ],
        ["shape_id", "shape_pt_lat", "shape_pt_lon", "shape_pt_sequence"],
    )

    blocks = build_all_blocks(
        gtfs_dir=gtfs,
        output=out / "all_blocks.parquet",
        out_dir=out,
        chunk_rows=2,
        config=BlockInferenceConfig(max_inferred_deadhead_km=5.0),
        write_checkpoints=False,
        verbose=False,
    )

    assert list(blocks.columns) == OUTPUT_COLUMNS
    assert blocks.loc[blocks["trip_id"].eq("native_trip"), "block_id"].item() == "NATIVE_1"
    inferred = blocks[blocks["block_source"].eq("inferred")]
    assert inferred["block_id"].nunique() == 1
    assert (out / "all_blocks.parquet").exists()
