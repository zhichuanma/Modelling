"""Private-car small-area geography preflight tests."""

from __future__ import annotations

import pandas as pd

from mobility.cars.geography_preflight import (
    SCOTLAND_FAIL_FAST_MESSAGE,
    build_privatecar_geography_preflight_report,
)
from mobility.cars.scotland_geography import unify_scotland_ev_home_lsoa_to_dz2022
from mobility.cars.station_curves import _select_stratified_private_car_sample


def _base_centroids(codes: list[str]) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "lsoa_code": codes,
            "easting_m": range(1000, 1000 + len(codes)),
            "northing_m": range(2000, 2000 + len(codes)),
            "lat": [55.0] * len(codes),
            "lon": [-4.0] * len(codes),
        }
    )


def test_geography_preflight_blocks_scotland_dz2011_vs_dz2022() -> None:
    ev_fleet = pd.DataFrame(
        {
            "EV_ID": ["car_s"],
            "LSOA_code": ["S01006506"],
            "vehicle_subtype": ["cars"],
        }
    )
    stations = pd.DataFrame({"lsoa_code": ["S01013495"]})
    centroids = _base_centroids(["S01013495"])
    destination = pd.DataFrame(
        {
            "origin_lsoa": ["S01013495"],
            "dest_lsoa": ["S01013495"],
        }
    )
    attractiveness = pd.DataFrame({"lsoa_code": ["S01013495"]})

    report = build_privatecar_geography_preflight_report(
        ev_fleet=ev_fleet,
        stations=stations,
        centroids=centroids,
        destination_df=destination,
        attractiveness_df=attractiveness,
    )

    assert report["summary"]["fail_fast"] is True
    assert report["summary"]["scotland_ev_home_lsoa_geography_version"] == "Data Zone 2011"
    assert report["summary"]["scotland_station_geography_version"] == "Data Zone 2022"
    assert report["blockers"][0]["message"] == SCOTLAND_FAIL_FAST_MESSAGE
    scotland_station_overlap = report["overlap_checks"].loc[
        (report["overlap_checks"]["check_name"] == "EV home_lsoa vs station lsoa_code")
        & (report["overlap_checks"]["country_or_prefix"] == "S")
    ].iloc[0]
    assert scotland_station_overlap["exact_overlap_count"] == 0


def test_geography_preflight_passes_when_scotland_codes_overlap() -> None:
    ev_fleet = pd.DataFrame(
        {
            "EV_ID": ["car_s"],
            "LSOA_code": ["S01013495"],
            "vehicle_subtype": ["cars"],
        }
    )
    stations = pd.DataFrame({"lsoa_code": ["S01013495"]})
    centroids = _base_centroids(["S01013495"])
    destination = pd.DataFrame(
        {
            "origin_lsoa": ["S01013495"],
            "dest_lsoa": ["S01013495"],
        }
    )
    attractiveness = pd.DataFrame({"lsoa_code": ["S01013495"]})

    report = build_privatecar_geography_preflight_report(
        ev_fleet=ev_fleet,
        stations=stations,
        centroids=centroids,
        destination_df=destination,
        attractiveness_df=attractiveness,
    )

    assert report["summary"]["fail_fast"] is False
    assert report["summary"]["status"] == "passed"


def test_scotland_ev_home_lsoa_unified_to_dz2022_with_area_crosswalk() -> None:
    ev_fleet = pd.DataFrame(
        {
            "EV_ID": ["car_s1", "car_s2", "car_s3", "car_s4"],
            "LSOA_code": ["S01006506", "S01006506", "S01006506", "S01006506"],
            "vehicle_subtype": ["cars", "cars", "cars", "cars"],
        }
    )
    crosswalk = pd.DataFrame(
        {
            "dz2011": ["S01006506", "S01006506"],
            "dz2022": ["S01013482", "S01013483"],
            "area_weight": [0.75, 0.25],
            "method": ["area_weighted_boundary_overlay", "area_weighted_boundary_overlay"],
            "dz2011_boundary_path": ["dz2011.shp", "dz2011.shp"],
            "dz2022_boundary_path": ["dz2022.shp", "dz2022.shp"],
        }
    )

    unified, meta, _ = unify_scotland_ev_home_lsoa_to_dz2022(
        ev_fleet,
        crosswalk=crosswalk,
    )

    assert meta["applied"] is True
    assert meta["rows_reassigned"] == 4
    assert meta["rows_unmapped"] == 0
    assert set(unified["home_lsoa"]) == {"S01013482", "S01013483"}
    assert unified["home_lsoa"].value_counts().to_dict() == {"S01013482": 3, "S01013483": 1}


def test_geography_preflight_reports_crosswalk_used_after_unification() -> None:
    ev_fleet = pd.DataFrame(
        {
            "EV_ID": ["car_s1", "car_s2"],
            "LSOA_code": ["S01006506", "S01006506"],
            "vehicle_subtype": ["cars", "cars"],
        }
    )
    crosswalk = pd.DataFrame(
        {
            "dz2011": ["S01006506"],
            "dz2022": ["S01013495"],
            "area_weight": [1.0],
            "method": ["area_weighted_boundary_overlay"],
            "dz2011_boundary_path": ["dz2011.shp"],
            "dz2022_boundary_path": ["dz2022.shp"],
        }
    )
    unified, meta, _ = unify_scotland_ev_home_lsoa_to_dz2022(
        ev_fleet,
        crosswalk=crosswalk,
    )
    stations = pd.DataFrame({"lsoa_code": ["S01013495"]})
    centroids = _base_centroids(["S01013495"])
    destination = pd.DataFrame(
        {
            "origin_lsoa": ["S01013495"],
            "dest_lsoa": ["S01013495"],
        }
    )
    attractiveness = pd.DataFrame({"lsoa_code": ["S01013495"]})

    report = build_privatecar_geography_preflight_report(
        ev_fleet=unified,
        stations=stations,
        centroids=centroids,
        destination_df=destination,
        attractiveness_df=attractiveness,
        geography_context=meta,
    )

    assert report["summary"]["fail_fast"] is False
    assert report["summary"]["crosswalk_used"] is True
    assert report["summary"]["scotland_geography_final_version"] == "Data Zone 2022"


def test_geography_preflight_reports_ni_mismatch_without_scotland_blocker() -> None:
    ev_fleet = pd.DataFrame(
        {
            "EV_ID": ["car_ni"],
            "LSOA_code": ["N20000001"],
            "vehicle_subtype": ["cars"],
        }
    )
    stations = pd.DataFrame({"lsoa_code": ["N20000001"]})
    centroids = _base_centroids(["N21000001"])
    destination = pd.DataFrame(
        {
            "origin_lsoa": ["N21000001"],
            "dest_lsoa": ["N21000001"],
        }
    )
    attractiveness = pd.DataFrame({"lsoa_code": ["N21000001"]})

    report = build_privatecar_geography_preflight_report(
        ev_fleet=ev_fleet,
        stations=stations,
        centroids=centroids,
        destination_df=destination,
        attractiveness_df=attractiveness,
    )

    assert report["summary"]["fail_fast"] is False
    assert report["summary"]["status"] == "passed_with_warnings"
    assert any(warning["country_or_prefix"] == "N" for warning in report["warnings"])


def test_stratified_private_car_sample_keeps_each_country_prefix() -> None:
    person_fleet = pd.DataFrame(
        {
            "ev_id": ["ev_e1", "ev_e2", "ev_n1", "ev_s1", "ev_w1"],
            "person_id": ["p1", "p2", "p3", "p4", "p5"],
        }
    )
    ev_fleet = pd.DataFrame(
        {
            "EV_ID": ["ev_e1", "ev_e2", "ev_n1", "ev_s1", "ev_w1"],
            "home_lsoa": ["E01000001", "E01000002", "N20000001", "S01006506", "W01000001"],
        }
    )

    sample = _select_stratified_private_car_sample(
        person_fleet,
        ev_fleet,
        sample_n_per_country=1,
        sample_seed=7,
    )

    sampled_ev_ids = set(sample["ev_id"])
    sampled_prefixes = set(
        ev_fleet.set_index("EV_ID").loc[list(sampled_ev_ids), "home_lsoa"].str[0]
    )
    assert len(sample) == 4
    assert sampled_prefixes == {"E", "N", "S", "W"}
