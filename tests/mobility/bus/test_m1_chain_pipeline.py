from __future__ import annotations

import inspect

import pandas as pd
import pytest

from mobility.bus.block_instances import build_block_instances
from mobility.bus.chain_resolver import SimulationError, build_resolution_summary, resolve_chain
from mobility.bus.chain_soc import chain_soc_walk
from mobility.bus.event_ledger import build_event_ledger
from mobility.bus.vehicle_assignment import assign_vehicles_greedy
from mobility.core.postcode_geocoder import geocode_postcode, load_onspd


def _depot_registry() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "depot_id": "depot_OP1",
                "agency_id": "OP1",
                "operator_noc": "NOC1",
                "lat": 51.0,
                "lon": -0.1,
                "lsoa_code": "E01000001",
                "lsoa_method": "polygon",
                "depot_source": "virtual_operator_centroid",
                "depot_confidence": "low",
                "depot_assignment_method": "virtual_operator_centroid",
                "override_reason": "",
                "manual_review_flag": False,
                "n_candidate_vehicles": 2,
            }
        ]
    )


def _vehicles(n: int = 2) -> pd.DataFrame:
    rows = []
    for idx in range(n):
        rows.append(
            {
                "vehicle_id": f"v{idx + 1}",
                "depot_id": "depot_OP1",
                "battery_kwh": 100.0,
                "consumption_kwh_per_km": 1.0,
                "ac_charge_kw_max": 50.0,
                "dc_charge_kw_max": 100.0,
                "usable_soc_min": 0.10,
                "usable_soc_max": 1.0,
                "vehicle_provenance": "ev_uk_lsoa_real",
            }
        )
    return pd.DataFrame(rows)


def _blocks(block_id: str = "B1", service_id: str = "S1", start_h: float = 8.0) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "trip_id": f"{block_id}_t1",
                "agency_id": "OP1",
                "service_id": service_id,
                "block_id": block_id,
                "block_source": "native",
                "start_h": start_h,
                "end_h": start_h + 1.0,
                "start_stop": "A",
                "end_stop": "B",
                "start_lat": 51.0 + (start_h - 8.0) * 0.01,
                "start_lon": -0.1,
                "end_lat": 51.01 + (start_h - 8.0) * 0.01,
                "end_lon": -0.1,
                "distance_km": 10.0,
            }
        ]
    )


def test_block_instance_duplicate_block_same_date_gets_seq() -> None:
    blocks = pd.concat(
        [
            _blocks("B1", "S1", 8.0),
            _blocks("B1", "S2", 10.0),
        ],
        ignore_index=True,
    )
    out = build_block_instances(
        blocks,
        {"S1": [pd.Timestamp("2026-05-01").date()], "S2": [pd.Timestamp("2026-05-01").date()]},
        start_date="2026-05-01",
        end_date="2026-05-01",
    )

    assert out["seq"].tolist() == [1, 2]
    assert out["block_instance_id"].tolist() == ["2026-05-01_B1_01", "2026-05-01_B1_02"]


def test_block_instance_preserves_gtfs_time_over_24h() -> None:
    out = build_block_instances(
        _blocks("B1", "S1", 24.5),
        {"S1": [pd.Timestamp("2026-05-01").date()]},
        start_date="2026-05-01",
        end_date="2026-05-01",
    )

    assert out.loc[0, "start_time"] == pytest.approx(1470.0)
    assert out.loc[0, "end_time"] == pytest.approx(1530.0)


def test_greedy_assignment_has_no_soc_parameters() -> None:
    parameters = inspect.signature(assign_vehicles_greedy).parameters

    assert "battery_kwh" not in parameters
    assert "consumption_kwh_per_km" not in parameters
    assert "soc" not in "".join(parameters)


def test_greedy_tiebreak_reuses_minimum_deadhead_vehicle() -> None:
    instances = build_block_instances(
        pd.concat([_blocks("B1", "S1", 8.0), _blocks("B2", "S1", 10.0)], ignore_index=True),
        {"S1": [pd.Timestamp("2026-05-01").date()]},
        start_date="2026-05-01",
        end_date="2026-05-01",
    )

    assignments = assign_vehicles_greedy(instances, _vehicles(2), _depot_registry())

    assert assignments["vehicle_id"].tolist() == ["v1", "v1"]
    assert assignments["tiebreak_reason"].tolist() == [
        "min_deadhead_then_vehicle_id",
        "min_deadhead_then_vehicle_id",
    ]


def test_greedy_overflow_spawn_when_pool_exhausted() -> None:
    instances = build_block_instances(
        pd.concat([_blocks("B1", "S1", 8.0), _blocks("B2", "S1", 8.0)], ignore_index=True),
        {"S1": [pd.Timestamp("2026-05-01").date()]},
        start_date="2026-05-01",
        end_date="2026-05-01",
    )

    assignments = assign_vehicles_greedy(instances, _vehicles(1), _depot_registry())

    assert set(assignments["assignment_status"]) == {"assigned"}
    assert "uncovered" not in set(assignments["assignment_status"])
    assert "synthetic_overflow" in set(assignments["vehicle_provenance"])
    assert "event_type" not in assignments.columns


def test_greedy_assigns_block_to_nearest_agency_depot() -> None:
    depots = pd.concat(
        [
            _depot_registry(),
            pd.DataFrame(
                [
                    {
                        **_depot_registry().iloc[0].to_dict(),
                        "depot_id": "depot_OP1_far",
                        "lat": 52.0,
                        "lon": -0.1,
                    }
                ]
            ),
        ],
        ignore_index=True,
    )
    instances = build_block_instances(
        _blocks("B1", "S1", 8.0),
        {"S1": [pd.Timestamp("2026-05-01").date()]},
        start_date="2026-05-01",
        end_date="2026-05-01",
    )

    assignments = assign_vehicles_greedy(instances, _vehicles(1), depots)

    assert assignments.loc[0, "depot_id"] == "depot_OP1"


def test_greedy_recomputes_nearest_depot_when_input_has_depot_id() -> None:
    depots = pd.concat(
        [
            _depot_registry(),
            pd.DataFrame(
                [
                    {
                        **_depot_registry().iloc[0].to_dict(),
                        "depot_id": "depot_OP1_far",
                        "lat": 52.0,
                        "lon": -0.1,
                    }
                ]
            ),
        ],
        ignore_index=True,
    )
    instances = build_block_instances(
        _blocks("B1", "S1", 8.0),
        {"S1": [pd.Timestamp("2026-05-01").date()]},
        start_date="2026-05-01",
        end_date="2026-05-01",
    )
    instances["depot_id"] = "depot_OP1_far"

    assignments = assign_vehicles_greedy(instances, _vehicles(1), depots)

    assert assignments.loc[0, "depot_id"] == "depot_OP1"


def test_event_ledger_starts_and_ends_at_depot() -> None:
    instances = build_block_instances(
        pd.concat([_blocks("B1", "S1", 8.0), _blocks("B2", "S1", 10.0)], ignore_index=True),
        {"S1": [pd.Timestamp("2026-05-01").date()]},
        start_date="2026-05-01",
        end_date="2026-05-01",
    )
    assignments = assign_vehicles_greedy(instances, _vehicles(2), _depot_registry())
    events = build_event_ledger(assignments, instances, _depot_registry(), pd.DataFrame())

    chain = events[events["chain_id"].eq(assignments.loc[0, "chain_id"])]
    assert chain.iloc[0]["event_type"] == "depot_parking"
    assert chain.iloc[-1]["event_type"] == "depot_parking"
    assert chain["event_seq"].is_monotonic_increasing
    assert len(chain[chain["event_type"].eq("passenger_block")]) == 2
    assert set(chain["distance_method"]) == {"haversine_x_1.0"}


def test_chain_soc_does_not_clamp_at_zero() -> None:
    events = pd.DataFrame(
        [
            {
                "event_seq": 1,
                "event_type": "passenger_block",
                "duration_min": 60.0,
                "energy_kwh_proxy": 120.0,
            }
        ]
    )
    vehicle = pd.Series({"battery_kwh": 100.0, "usable_soc_max": 0.95})

    walked = chain_soc_walk(events, vehicle, pd.DataFrame({"event_seq": [1], "eligible": [False]}))

    assert walked.loc[0, "soc_end_kwh"] == pytest.approx(-25.0)


def test_chain_soc_uses_ac_for_depot_and_dc_for_opportunity() -> None:
    events = pd.DataFrame(
        [
            {"event_seq": 1, "event_type": "depot_parking", "duration_min": 60.0, "energy_kwh_proxy": 0.0},
            {"event_seq": 2, "event_type": "terminal_layover", "duration_min": 60.0, "energy_kwh_proxy": 0.0},
        ]
    )
    vehicle = pd.Series(
        {
            "battery_kwh": 100.0,
            "usable_soc_max": 1.0,
            "ac_charge_kw_max": 10.0,
            "dc_charge_kw_max": 50.0,
        }
    )
    eligibility = pd.DataFrame(
        {
            "event_seq": [1, 2],
            "eligible": [True, True],
            "power_kw": [999.0, 999.0],
            "station_id": ["depot_depot_OP1", "public_1"],
        }
    )

    walked = chain_soc_walk(events, vehicle, eligibility, initial_soc_kwh=0.0)

    assert walked["charge_kwh_added"].tolist() == pytest.approx([10.0, 50.0])


def test_resolution_summary_opportunity_charge_nonzero_when_l1_used() -> None:
    events = pd.DataFrame(
        [
            {"event_seq": 1, "event_type": "passenger_block", "duration_min": 60.0, "energy_kwh_proxy": 80.0},
            {"event_seq": 2, "event_type": "terminal_layover", "duration_min": 60.0, "energy_kwh_proxy": 0.0, "station_kind": "public"},
        ]
    )
    vehicle = pd.Series(
        {
            "vehicle_id": "v1",
            "battery_kwh": 100.0,
            "usable_soc_max": 1.0,
            "ac_charge_kw_max": 10.0,
            "dc_charge_kw_max": 50.0,
        }
    )
    eligibility = pd.DataFrame(
        {
            "event_seq": [1, 2],
            "eligible": [False, True],
            "power_kw": [0.0, 50.0],
            "station_id": ["", "public_1"],
        }
    )

    walked = chain_soc_walk(events, vehicle, eligibility)

    assert "station_kind" in walked.columns
    assert walked.loc[1, "station_kind"] == "public"

    chain_events = _manual_events(two_blocks_kwh=80.0, layover_min=60.0)
    chargers = pd.DataFrame(
        [
            {"station_id": "depot_depot_OP1", "station_kind": "depot", "lat": 51.0, "lon": -0.1, "lsoa_code": "", "power_kw": 100.0, "attached_depot_id": "depot_OP1", "source": "test"},
            {"station_id": "public_1", "station_kind": "public", "lat": 51.0, "lon": -0.1, "lsoa_code": "E01000001", "power_kw": 100.0, "attached_depot_id": "", "source": "test"},
        ]
    )
    result = resolve_chain(chain_events, chain_events.iloc[0], pd.DataFrame(), chargers, "depot_OP1")
    assignment = pd.DataFrame(
        [
            {
                "service_date": "2026-05-01",
                "chain_id": "v1_2026-05-01_00",
                "depot_id": "depot_OP1",
                "vehicle_id": "v1",
                "vehicle_provenance": "ev_uk_lsoa_real",
            }
        ]
    )

    summary = build_resolution_summary([result], assignment)

    assert result["resolution_level"] == 1
    assert "station_kind" in result["final_chain_events"].columns
    assert result["final_chain_events"].loc[
        result["final_chain_events"]["station_id"].eq("public_1"),
        "station_kind",
    ].eq("public").any()
    assert float(summary.loc[0, "opportunity_charge_kwh"]) > 0.0


def _manual_events(two_blocks_kwh: float = 80.0, layover_min: float = 60.0) -> pd.DataFrame:
    base = {
        "vehicle_id": "v1",
        "chain_id": "v1_2026-05-01_00",
        "service_date": "2026-05-01",
        "depot_id": "depot_OP1",
        "vehicle_provenance": "ev_uk_lsoa_real",
        "battery_kwh": 100.0,
        "consumption_kwh_per_km": 1.0,
        "ac_charge_kw_max": 100.0,
        "dc_charge_kw_max": 100.0,
        "usable_soc_min": 0.10,
        "usable_soc_max": 1.0,
        "distance_method": "haversine_x_1.0",
    }
    return pd.DataFrame(
        [
            base | {"event_seq": 1, "event_type": "depot_parking", "block_instance_id": "", "start_time": 0.0, "end_time": 0.0, "duration_min": 0.0, "start_lat": 51.0, "start_lon": -0.1, "end_lat": 51.0, "end_lon": -0.1, "distance_km": 0.0, "energy_kwh_proxy": 0.0},
            base | {"event_seq": 2, "event_type": "passenger_block", "block_instance_id": "b1", "start_time": 480.0, "end_time": 540.0, "duration_min": 60.0, "start_lat": 51.0, "start_lon": -0.1, "end_lat": 51.0, "end_lon": -0.1, "distance_km": two_blocks_kwh, "energy_kwh_proxy": two_blocks_kwh},
            base | {"event_seq": 3, "event_type": "terminal_layover", "block_instance_id": "", "start_time": 540.0, "end_time": 540.0 + layover_min, "duration_min": layover_min, "start_lat": 51.0, "start_lon": -0.1, "end_lat": 51.0, "end_lon": -0.1, "distance_km": 0.0, "energy_kwh_proxy": 0.0},
            base | {"event_seq": 4, "event_type": "passenger_block", "block_instance_id": "b2", "start_time": 540.0 + layover_min, "end_time": 600.0 + layover_min, "duration_min": 60.0, "start_lat": 51.0, "start_lon": -0.1, "end_lat": 51.0, "end_lon": -0.1, "distance_km": two_blocks_kwh, "energy_kwh_proxy": two_blocks_kwh},
        ]
    )


def test_resolver_l0_when_already_feasible() -> None:
    events = _manual_events(two_blocks_kwh=20.0)
    chargers = pd.DataFrame(
        [{"station_id": "depot_depot_OP1", "station_kind": "depot", "lat": 51.0, "lon": -0.1, "lsoa_code": "", "power_kw": 100.0, "attached_depot_id": "depot_OP1", "source": "test"}]
    )

    result = resolve_chain(events, events.iloc[0], pd.DataFrame(), chargers, "depot_OP1")

    assert result["resolution_level"] == 0


def test_resolver_l1_finds_public_charger_within_radius() -> None:
    events = _manual_events(two_blocks_kwh=80.0, layover_min=60.0)
    chargers = pd.DataFrame(
        [
            {"station_id": "depot_depot_OP1", "station_kind": "depot", "lat": 51.0, "lon": -0.1, "lsoa_code": "", "power_kw": 100.0, "attached_depot_id": "depot_OP1", "source": "test"},
            {"station_id": "public_1", "station_kind": "public", "lat": 51.0, "lon": -0.1, "lsoa_code": "", "power_kw": 100.0, "attached_depot_id": "", "source": "test"},
        ]
    )

    result = resolve_chain(events, events.iloc[0], pd.DataFrame(), chargers, "depot_OP1")

    assert result["resolution_level"] == 1
    assert "public_1" in result["station_ids_used"]


def test_resolver_l2_picks_highest_battery_spare() -> None:
    events = _manual_events(two_blocks_kwh=80.0, layover_min=0.0)
    chargers = pd.DataFrame(
        [{"station_id": "depot_depot_OP1", "station_kind": "depot", "lat": 51.0, "lon": -0.1, "lsoa_code": "", "power_kw": 100.0, "attached_depot_id": "depot_OP1", "source": "test"}]
    )
    pool = pd.DataFrame(
        [
            {"vehicle_id": "v2", "battery_kwh": 120.0, "consumption_kwh_per_km": 1.0, "ac_charge_kw_max": 100.0, "dc_charge_kw_max": 100.0, "usable_soc_min": 0.1, "usable_soc_max": 1.0},
            {"vehicle_id": "v3", "battery_kwh": 200.0, "consumption_kwh_per_km": 1.0, "ac_charge_kw_max": 100.0, "dc_charge_kw_max": 100.0, "usable_soc_min": 0.1, "usable_soc_max": 1.0},
        ]
    )

    result = resolve_chain(events, events.iloc[0], pool, chargers, "depot_OP1")

    assert result["resolution_level"] == 2
    assert result["final_vehicle_id"] == "v3"


def test_resolver_l3_inserts_mid_day_return() -> None:
    events = _manual_events(two_blocks_kwh=80.0, layover_min=60.0)
    chargers = pd.DataFrame(
        [{"station_id": "depot_depot_OP1", "station_kind": "depot", "lat": 51.0, "lon": -0.1, "lsoa_code": "", "power_kw": 100.0, "attached_depot_id": "depot_OP1", "source": "test"}]
    )

    result = resolve_chain(events, events.iloc[0], pd.DataFrame(), chargers, "depot_OP1")

    assert result["resolution_level"] == 3
    assert "mid_day_return" in result["modifications"]
    assert "midday_return_deadhead" in set(result["final_chain_events"]["event_type"])


def test_resolver_is_deterministic_under_rerun() -> None:
    events = _manual_events(two_blocks_kwh=80.0, layover_min=60.0)
    chargers = pd.DataFrame(
        [{"station_id": "depot_depot_OP1", "station_kind": "depot", "lat": 51.0, "lon": -0.1, "lsoa_code": "", "power_kw": 100.0, "attached_depot_id": "depot_OP1", "source": "test"}]
    )

    left = resolve_chain(events, events.iloc[0], pd.DataFrame(), chargers, "depot_OP1")
    right = resolve_chain(events, events.iloc[0], pd.DataFrame(), chargers, "depot_OP1")

    assert left["resolution_level"] == right["resolution_level"]
    assert left["min_soc_kwh_per_level"] == right["min_soc_kwh_per_level"]


def test_resolver_l5_raises_with_chain_events_without_writing() -> None:
    events = pd.DataFrame(columns=_manual_events().columns)
    chargers = pd.DataFrame()

    with pytest.raises(SimulationError) as excinfo:
        resolve_chain(events, pd.Series({"vehicle_id": "v1"}), pd.DataFrame(), chargers, "depot_OP1")

    assert excinfo.value.chain_events is not None
    assert excinfo.value.chain_events.empty


def test_postcode_geocoder_normalises_variants(tmp_path) -> None:
    path = tmp_path / "onspd.csv"
    path.write_text("pcds,lat,long\nSW1A 1AA,51.501,-0.141\n", encoding="utf-8")

    index = load_onspd(path)

    assert geocode_postcode("sw1a1aa", index) == pytest.approx((51.501, -0.141))
