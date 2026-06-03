from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from mobility.bus.annual_home_depot import (
    HOME_DEPOT_STATUS_ASSIGNED,
    HOME_DEPOT_STATUS_MISSING_CENTROID,
    HOME_DEPOT_STATUS_MISSING_SOURCE_LSOA,
    assign_home_depots,
    build_depot_supply_demand,
)


def _registry() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "depot_id": ["depA", "depB"],
            "depot_lat": [51.5, 51.5],
            "depot_lon": [0.05, 0.95],
            "depot_lsoa": ["E_A", "E_B"],
        }
    )


def _centroids() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "lsoa_code": ["L1", "L2"],
            "lat": [51.5, 51.5],
            "lon": [0.0, 1.0],
        }
    )


def _specs(source_lsoas: list[str]) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "vehicle_spec_id": [f"ev{i}" for i in range(len(source_lsoas))],
            "source_lsoa": source_lsoas,
        }
    )


def test_nearest_depot_per_source_lsoa() -> None:
    specs, per_depot = assign_home_depots(_specs(["L1", "L1", "L2"]), _registry(), _centroids())
    assert specs["home_depot_id"].tolist() == ["depA", "depA", "depB"]
    assert specs["home_depot_lsoa"].tolist() == ["E_A", "E_A", "E_B"]
    assert specs["home_depot_status"].eq(HOME_DEPOT_STATUS_ASSIGNED).all()
    # ~0.05 deg lon at lat 51.5 is ~3.5 km.
    assert specs["home_depot_distance_km"].between(3.0, 4.0).all()
    counts = per_depot.set_index("depot_id")["n_home_vehicles"]
    assert counts["depA"] == 2
    assert counts["depB"] == 1
    assert np.isclose(per_depot["share_of_fleet"].sum(), 1.0)


def test_missing_source_lsoa_and_missing_centroid_statuses() -> None:
    specs, per_depot = assign_home_depots(_specs(["L1", "", "L_UNKNOWN"]), _registry(), _centroids())
    assert specs["home_depot_status"].tolist() == [
        HOME_DEPOT_STATUS_ASSIGNED,
        HOME_DEPOT_STATUS_MISSING_SOURCE_LSOA,
        HOME_DEPOT_STATUS_MISSING_CENTROID,
    ]
    unassigned = specs["home_depot_status"].ne(HOME_DEPOT_STATUS_ASSIGNED)
    assert specs.loc[unassigned, "home_depot_id"].eq("").all()
    assert specs.loc[unassigned, "home_depot_distance_km"].isna().all()
    assert per_depot["n_specs_unassigned_home_depot"].eq(2).all()


def test_unknown_method_raises() -> None:
    with pytest.raises(NotImplementedError):
        assign_home_depots(_specs(["L1"]), _registry(), _centroids(), method="region")


def test_deterministic_output() -> None:
    first, first_depot = assign_home_depots(_specs(["L1", "L2"]), _registry(), _centroids())
    second, second_depot = assign_home_depots(_specs(["L1", "L2"]), _registry(), _centroids())
    pd.testing.assert_frame_equal(first, second)
    pd.testing.assert_frame_equal(first_depot, second_depot)


def test_depots_without_coordinates_are_never_home() -> None:
    registry = pd.concat(
        [_registry(), pd.DataFrame({"depot_id": ["depNaN"], "depot_lat": [np.nan], "depot_lon": [np.nan], "depot_lsoa": ["E_N"]})],
        ignore_index=True,
    )
    specs, _ = assign_home_depots(_specs(["L1", "L2"]), registry, _centroids())
    assert not specs["home_depot_id"].eq("depNaN").any()


def test_depot_supply_demand_table() -> None:
    specs, _ = assign_home_depots(_specs(["L1", "L1", "L2"]), _registry(), _centroids())
    block_instances = pd.DataFrame(
        {
            "depot_id": ["depA", "depA", "depA", "depOther"],
            "block_instance_id": ["b1", "b2", "b3", "b4"],
            "service_date": ["2026-04-17", "2026-04-17", "2026-04-18", "2026-04-17"],
        }
    )
    table = build_depot_supply_demand(specs, block_instances, _registry()).set_index("depot_id")
    assert table.loc["depA", "n_home_vehicles"] == 2
    assert table.loc["depA", "n_block_instances"] == 3
    assert table.loc["depA", "n_service_dates"] == 2
    assert np.isclose(table.loc["depA", "mean_daily_blocks"], 1.5)
    assert np.isclose(table.loc["depA", "supply_demand_ratio"], 2 / 1.5)
    # Home fleet with no block demand, and block demand with no home fleet.
    assert table.loc["depB", "n_home_vehicles"] == 1
    assert table.loc["depB", "n_block_instances"] == 0
    assert table.loc["depOther", "n_home_vehicles"] == 0
    assert table.loc["depA", "depot_lsoa"] == "E_A"
