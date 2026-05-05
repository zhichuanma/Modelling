from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from mobility.bus.data_loader import attach_lsoa
from mobility.bus.trip_chain_bus import block_to_daily_schedules
from mobility.core.spatial import nearest_lsoa_for_points


@pytest.fixture()
def lsoa_centroids() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "lsoa_code": ["E010A", "E010B", "E010C"],
            "easting_m": [530000.0, 531300.0, 524500.0],
            "northing_m": [180000.0, 182250.0, 180000.0],
            "lat": [51.5000, 51.5200, 51.5000],
            "lon": [-0.1200, -0.1000, -0.2000],
        }
    )


def _blocks(with_lsoa: bool = False) -> pd.DataFrame:
    rows = [
        ("t0", 8.0, 9.0, "A", "B", 51.5001, -0.1201, 51.5202, -0.1002),
        ("t1", 9.5, 10.5, "B", "C", 51.5202, -0.1002, 51.5002, -0.1998),
    ]
    data = pd.DataFrame(
        [
            (
                tid,
                "OP",
                "R1",
                "S1",
                0,
                "B1",
                "native",
                start,
                end,
                10.0,
                a,
                b,
                start_lat,
                start_lon,
                end_lat,
                end_lon,
                "shape",
            )
            for tid, start, end, a, b, start_lat, start_lon, end_lat, end_lon in rows
        ],
        columns=[
            "trip_id",
            "agency_id",
            "route_id",
            "service_id",
            "direction_id",
            "block_id",
            "block_source",
            "start_h",
            "end_h",
            "distance_km",
            "start_stop",
            "end_stop",
            "start_lat",
            "start_lon",
            "end_lat",
            "end_lon",
            "shape_id",
        ],
    )
    if with_lsoa:
        data["start_lsoa"] = ["E010A", "E010B"]
        data["end_lsoa"] = ["E010B", "E010C"]
    return data


def test_nearest_lsoa_for_points_basic(lsoa_centroids: pd.DataFrame) -> None:
    codes, distances_km = nearest_lsoa_for_points(
        np.array([51.5001, 51.5201, 51.5001]),
        np.array([-0.1201, -0.1001, -0.1999]),
        lsoa_centroids,
    )

    assert codes.tolist() == ["E010A", "E010B", "E010C"]
    assert np.all(np.isfinite(distances_km))
    assert np.all(distances_km < 2.0)


def test_nearest_lsoa_for_points_max_distance(lsoa_centroids: pd.DataFrame) -> None:
    unlimited_codes, unlimited_distances_km = nearest_lsoa_for_points(
        np.array([56.0]),
        np.array([0.0]),
        lsoa_centroids,
    )
    limited_codes, limited_distances_km = nearest_lsoa_for_points(
        np.array([56.0]),
        np.array([0.0]),
        lsoa_centroids,
        max_distance_km=5.0,
    )

    assert unlimited_codes[0] in {"E010A", "E010B", "E010C"}
    assert limited_codes.tolist() == [""]
    assert limited_distances_km[0] == pytest.approx(unlimited_distances_km[0])
    assert limited_distances_km[0] > 5.0


def test_attach_lsoa_columns_added(lsoa_centroids: pd.DataFrame) -> None:
    attached = attach_lsoa(_blocks(), centroids=lsoa_centroids)

    for col in ("start_lsoa", "end_lsoa", "start_lsoa_distance_km", "end_lsoa_distance_km"):
        assert col in attached.columns
    assert attached["start_lsoa"].tolist() == ["E010A", "E010B"]
    assert attached["end_lsoa"].tolist() == ["E010B", "E010C"]
    assert attached.attrs["lsoa_join"]["max_distance_km"] == 5.0
    assert attached.attrs["lsoa_join"]["n_unmatched"] == 0


def test_attach_lsoa_does_not_mutate_input(lsoa_centroids: pd.DataFrame) -> None:
    blocks = _blocks()
    original_columns = list(blocks.columns)

    attach_lsoa(blocks, centroids=lsoa_centroids)

    assert list(blocks.columns) == original_columns


def test_attach_lsoa_requires_coordinate_columns(lsoa_centroids: pd.DataFrame) -> None:
    with pytest.raises(ValueError, match="coordinate columns"):
        attach_lsoa(_blocks().drop(columns=["end_lon"]), centroids=lsoa_centroids)


def test_trip_chain_propagates_lsoa() -> None:
    schedule = block_to_daily_schedules(
        _blocks(with_lsoa=True),
        "bus_B1",
        consumption_kwh_per_km=1.0,
        depot_charge_kw=100.0,
    )[0]

    assert [trip.origin_lsoa for trip in schedule.trips] == ["E010A", "E010B"]
    assert [trip.destination_lsoa for trip in schedule.trips] == ["E010B", "E010C"]
    assert [event.location_lsoa for event in schedule.parking_events] == [
        "E010A",
        "E010B",
        "E010C",
    ]


def test_trip_chain_lsoa_absent_columns() -> None:
    schedule = block_to_daily_schedules(
        _blocks(with_lsoa=False),
        "bus_B1",
        consumption_kwh_per_km=1.0,
        depot_charge_kw=100.0,
    )[0]

    assert all(trip.origin_lsoa == "" for trip in schedule.trips)
    assert all(trip.destination_lsoa == "" for trip in schedule.trips)
    assert all(event.location_lsoa == "" for event in schedule.parking_events)
