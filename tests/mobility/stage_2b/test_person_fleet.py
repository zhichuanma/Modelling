"""Stage 2b coverage for person_fleet binding and parquet freeze."""

from __future__ import annotations

from pathlib import Path
import importlib

import numpy as np
import pandas as pd
import pandas.testing as pdt
import pytest

person_fleet_module = importlib.import_module("mobility.cars.person_fleet")

PERSON_FLEET_COLUMNS = person_fleet_module.PERSON_FLEET_COLUMNS
build_person_fleet = person_fleet_module.build_person_fleet
write_person_fleet_parquet = person_fleet_module.write_person_fleet_parquet


def _make_ev_fleet(num_rows: int, lsoa_code: str = "UNKNOWN") -> pd.DataFrame:
    return pd.DataFrame(
        {
            "EV_ID": [f"ev_{idx}" for idx in range(num_rows)],
            "LSOA_code": [lsoa_code] * num_rows,
        }
    )


def _make_nts_persons(
    weights: list[float],
    regions: list[str] | None = None,
    *,
    include_invalid: bool = False,
) -> pd.DataFrame:
    if regions is None:
        regions = ["london"] * len(weights)

    rows = []
    for idx, (weight, region) in enumerate(zip(weights, regions), start=1):
        rows.append(
            {
                "IndividualID": f"person_{idx}",
                "HouseholdID": f"hh_{idx}",
                "W5": weight,
                "nts_region": region,
            }
        )

    if include_invalid:
        rows.append(
            {
                "IndividualID": "person_invalid",
                "HouseholdID": "hh_invalid",
                "W5": 1000.0,
                "nts_region": regions[0],
            }
        )

    return pd.DataFrame(rows)


@pytest.fixture()
def empty_region_lookup(monkeypatch: pytest.MonkeyPatch) -> None:
    if hasattr(person_fleet_module._load_lsoa_region_lookup, "cache_clear"):
        person_fleet_module._load_lsoa_region_lookup.cache_clear()
    monkeypatch.setattr(person_fleet_module, "_load_lsoa_region_lookup", lambda *args, **kwargs: {})


def test_schema_frozen(tmp_path: Path, empty_region_lookup: None) -> None:
    ev_fleet = _make_ev_fleet(4)
    nts_persons = _make_nts_persons([1.0, 2.0, 3.0, 4.0])
    valid_ids = {f"person_{idx}" for idx in range(1, 5)}

    result = build_person_fleet(ev_fleet, nts_persons, valid_ids, np.random.default_rng(42))
    out_path = tmp_path / "person_fleet.parquet"
    write_person_fleet_parquet(result, out_path)
    observed = pd.read_parquet(out_path)

    assert list(observed.columns) == PERSON_FLEET_COLUMNS
    for column in PERSON_FLEET_COLUMNS:
        assert pd.api.types.is_object_dtype(observed[column].dtype) or pd.api.types.is_string_dtype(
            observed[column].dtype
        )


def test_row_count(empty_region_lookup: None) -> None:
    ev_fleet = _make_ev_fleet(17)
    nts_persons = _make_nts_persons([1.0, 2.0, 3.0])
    valid_ids = {"person_1", "person_2", "person_3"}

    result = build_person_fleet(ev_fleet, nts_persons, valid_ids, np.random.default_rng(7))
    assert len(result) == len(ev_fleet)


def test_determinism(empty_region_lookup: None) -> None:
    ev_fleet = _make_ev_fleet(50)
    nts_persons = _make_nts_persons([1.0, 2.0, 3.0, 4.0, 5.0])
    valid_ids = {f"person_{idx}" for idx in range(1, 6)}

    observed_a = build_person_fleet(ev_fleet, nts_persons, valid_ids, np.random.default_rng(42))
    observed_b = build_person_fleet(ev_fleet, nts_persons, valid_ids, np.random.default_rng(42))

    pdt.assert_frame_equal(observed_a, observed_b)


def test_weighted_sampling_not_uniform(empty_region_lookup: None) -> None:
    num_people = 10
    ev_fleet = _make_ev_fleet(5000)
    nts_persons = _make_nts_persons([100.0] + [1.0] * (num_people - 1))
    valid_ids = {f"person_{idx}" for idx in range(1, num_people + 1)}

    result = build_person_fleet(ev_fleet, nts_persons, valid_ids, np.random.default_rng(0))
    selected_share = float((result["person_id"] == "person_1").mean())
    uniform_p = 1.0 / num_people
    uniform_sd = (uniform_p * (1.0 - uniform_p) / len(ev_fleet)) ** 0.5
    upper_99_ci = uniform_p + (2.58 * uniform_sd)

    assert selected_share > upper_99_ci


def test_region_alignment_when_present(monkeypatch: pytest.MonkeyPatch) -> None:
    if hasattr(person_fleet_module._load_lsoa_region_lookup, "cache_clear"):
        person_fleet_module._load_lsoa_region_lookup.cache_clear()
    monkeypatch.setattr(
        person_fleet_module,
        "_load_lsoa_region_lookup",
        lambda *args, **kwargs: {"LSOA_LON": "london", "LSOA_SCO": "scotland"},
    )

    ev_fleet = pd.DataFrame(
        {
            "EV_ID": [f"ev_{idx}" for idx in range(200)],
            "LSOA_code": ["LSOA_LON"] * 100 + ["LSOA_SCO"] * 100,
        }
    )
    nts_persons = pd.DataFrame(
        {
            "IndividualID": [f"person_{idx}" for idx in range(20)],
            "HouseholdID": [f"hh_{idx}" for idx in range(20)],
            "W5": [1.0] * 20,
            "nts_region": ["london"] * 10 + ["scotland"] * 10,
        }
    )
    valid_ids = set(nts_persons["IndividualID"].astype(str))

    result = build_person_fleet(ev_fleet, nts_persons, valid_ids, np.random.default_rng(5))
    same_region_share = float(
        (
            ((ev_fleet["LSOA_code"] == "LSOA_LON") & (result["nts_region"] == "london"))
            | ((ev_fleet["LSOA_code"] == "LSOA_SCO") & (result["nts_region"] == "scotland"))
        ).mean()
    )

    assert same_region_share >= 0.95


def test_all_persons_have_trips(empty_region_lookup: None) -> None:
    ev_fleet = _make_ev_fleet(100)
    nts_persons = _make_nts_persons([1.0, 2.0, 3.0], include_invalid=True)
    valid_ids = {"person_1", "person_2", "person_3"}

    result = build_person_fleet(ev_fleet, nts_persons, valid_ids, np.random.default_rng(3))
    assert set(result["person_id"].astype(str)).issubset(valid_ids)
