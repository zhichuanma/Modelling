from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from mobility.bus.vehicle_sampling import load_bus_vehicle_params, sample_bus_vehicle_specs


def _params() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "make": ["A", "B"],
            "gen_model": ["Alpha", "Beta"],
            "stock_2025_q2": [10.0, 90.0],
            "battery_kwh": [200.0, 500.0],
            "consumption_kwh_per_km": [0.7, 1.1],
            "depot_charge_kw": [80.0, 150.0],
        }
    )


def _write_lookup(tmp_path: Path, rows: list[tuple[str, str]]) -> Path:
    """rows = [(GenModel, subtype), ...] - writes a 3-column CSV."""
    path = tmp_path / "lookup.csv"
    df = pd.DataFrame(
        [(gen_model, subtype, "test_note") for gen_model, subtype in rows],
        columns=["GenModel", "subtype", "source_note"],
    )
    df.to_csv(path, index=False)
    return path


def _spec_row(
    gen_model: str,
    *,
    make: str,
    stock: float = 10.0,
    battery_kwh: float = 300.0,
    charge_kw: float = 100.0,
    efficiency_wh_per_km: float = 900.0,
) -> dict[str, object]:
    return {
        "BodyType": "Buses and coaches",
        "Fuel": "Battery electric",
        "Make": make,
        "GenModel": gen_model,
        "2025 Q2": stock,
        "energy_capacity_kWh": battery_kwh,
        "power_capacity_kW": charge_kw,
        "efficiency_wh_per_km": efficiency_wh_per_km,
        "source_url": "https://example.test/spec",
        "ac_charge_power_kW": charge_kw,
    }


def _write_spec(tmp_path: Path, rows: list[dict[str, object]]) -> Path:
    """rows = [{"GenModel": ..., "Make": ..., "2025 Q2": ..., ...}]."""
    path = tmp_path / "spec.csv"
    pd.DataFrame(rows).to_csv(path, index=False)
    return path


def test_sample_bus_vehicle_specs_is_seeded_and_weighted() -> None:
    rng_a = np.random.default_rng(20260430)
    rng_b = np.random.default_rng(20260430)

    sample_a = sample_bus_vehicle_specs(_params(), rng_a, n=8)
    sample_b = sample_bus_vehicle_specs(_params(), rng_b, n=8)

    pd.testing.assert_frame_equal(sample_a, sample_b)
    assert set(sample_a["battery_kwh"]).issubset({200.0, 500.0})
    assert sample_a["gen_model"].nunique() > 1


def test_load_bus_vehicle_params_exposes_real_table_coverage() -> None:
    params = load_bus_vehicle_params()

    assert {"make", "gen_model", "battery_kwh", "consumption_kwh_per_km", "depot_charge_kw"}.issubset(params.columns)
    assert params["battery_kwh"].gt(0).all()
    assert params["consumption_kwh_per_km"].gt(0).all()
    assert params.attrs["stock_coverage_pct"] > 90.0


def test_load_bus_vehicle_params_excludes_coach(tmp_path: Path) -> None:
    spec_path = _write_spec(
        tmp_path,
        [
            _spec_row("OPTARE METROCITY", make="OPTARE"),
            _spec_row("YUTONG TC12", make="YUTONG"),
        ],
    )
    lookup_path = _write_lookup(
        tmp_path,
        [
            ("OPTARE METROCITY", "bus"),
            ("YUTONG TC12", "coach"),
        ],
    )

    loaded = load_bus_vehicle_params(spec_path, subtype_lookup_path=lookup_path)

    assert len(loaded) == 1
    assert loaded["gen_model"].iloc[0] == "OPTARE METROCITY"
    assert loaded.attrs["dropped_by_subtype"] == {"coach": 1}
    assert loaded.attrs["include_subtypes"] == ("bus", "minibus", "unknown")


def test_load_bus_vehicle_params_excludes_minibus_when_requested(tmp_path: Path) -> None:
    spec_path = _write_spec(
        tmp_path,
        [
            _spec_row("OPTARE METROCITY", make="OPTARE"),
            _spec_row("YUTONG TC12", make="YUTONG"),
            _spec_row("FORD TRANSIT", make="FORD"),
        ],
    )
    lookup_path = _write_lookup(
        tmp_path,
        [
            ("OPTARE METROCITY", "bus"),
            ("YUTONG TC12", "coach"),
            ("FORD TRANSIT", "minibus"),
        ],
    )

    loaded = load_bus_vehicle_params(spec_path, subtype_lookup_path=lookup_path, include_subtypes=("bus",))

    assert loaded["gen_model"].to_list() == ["OPTARE METROCITY"]
    assert loaded.attrs["dropped_by_subtype"] == {"coach": 1, "minibus": 1}
    assert loaded.attrs["include_subtypes"] == ("bus",)


def test_load_bus_vehicle_params_skips_filter_when_lookup_path_none(tmp_path: Path) -> None:
    spec_path = _write_spec(
        tmp_path,
        [
            _spec_row("OPTARE METROCITY", make="OPTARE"),
            _spec_row("YUTONG TC12", make="YUTONG"),
            _spec_row("FORD TRANSIT", make="FORD"),
        ],
    )

    loaded = load_bus_vehicle_params(spec_path, subtype_lookup_path=None)

    assert len(loaded) == 3
    assert loaded.attrs["subtype_lookup_path"] is None
    assert loaded.attrs["dropped_by_subtype"] == {}
    assert set(loaded["subtype"]) == {"unknown"}


def test_load_bus_vehicle_params_raises_when_filter_empties_frame(tmp_path: Path) -> None:
    spec_path = _write_spec(
        tmp_path,
        [
            _spec_row("YUTONG TC9", make="YUTONG"),
            _spec_row("YUTONG TC12", make="YUTONG"),
        ],
    )
    lookup_path = _write_lookup(
        tmp_path,
        [
            ("YUTONG TC9", "coach"),
            ("YUTONG TC12", "coach"),
        ],
    )

    with pytest.raises(ValueError, match="subtype filter") as excinfo:
        load_bus_vehicle_params(spec_path, subtype_lookup_path=lookup_path)

    message = str(excinfo.value)
    assert str(lookup_path) in message
    assert "coach" in message
    assert "2" in message


def test_sample_bus_vehicle_specs_carries_subtype() -> None:
    vehicle_params = _params().assign(subtype=["bus", "bus"])
    rng = np.random.default_rng(20260430)

    sampled = sample_bus_vehicle_specs(vehicle_params, rng, n=4)

    assert "subtype" in sampled.columns
    assert sampled["subtype"].to_list() == ["bus", "bus", "bus", "bus"]
