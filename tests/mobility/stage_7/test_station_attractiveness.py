from pathlib import Path
import subprocess
import sys

import numpy as np
import pandas as pd
import pytest

from mobility.core.station_attractiveness import compute_station_attractiveness


UPSTREAM_STATION_PATH = Path(
    "Data/Charging_stations/OSM_POI_Labeling/UK_OCM_stations_labeled.csv"
)
UPSTREAM_BACKUP_PATH = UPSTREAM_STATION_PATH.with_suffix(
    UPSTREAM_STATION_PATH.suffix + ".pre_stage7.bak"
)
RUNTIME_STATION_PATH = Path("Modelling/data/UK_OCM_stations_labeled.csv")


def test_formula_matches_log1p():
    df = pd.DataFrame({"TotalCapacity_kW": [0.0, 1.0, 7.0, 50.0, 150.0]})
    compute_station_attractiveness(df)
    expected = np.log1p(df["TotalCapacity_kW"].to_numpy(dtype=float))
    np.testing.assert_allclose(df["station_attractiveness"].to_numpy(), expected)


def test_zero_capacity_gives_zero_score():
    df = pd.DataFrame({"TotalCapacity_kW": [0.0]})
    compute_station_attractiveness(df)
    assert df["station_attractiveness"].iloc[0] == 0.0


def test_monotonic_in_capacity():
    caps = [0.0, 3.6, 7.0, 11.0, 22.0, 50.0, 150.0, 350.0]
    df = pd.DataFrame({"TotalCapacity_kW": caps})
    compute_station_attractiveness(df)
    scores = df["station_attractiveness"].to_numpy()
    assert np.all(np.diff(scores) > 0)


def test_missing_capacity_column_raises():
    df = pd.DataFrame({"SomethingElse": [1, 2]})
    with pytest.raises(KeyError):
        compute_station_attractiveness(df)


def test_nan_capacity_raises():
    df = pd.DataFrame({"TotalCapacity_kW": [7.0, np.nan, 22.0]})
    with pytest.raises(ValueError, match="NaN"):
        compute_station_attractiveness(df)


def test_negative_capacity_raises():
    df = pd.DataFrame({"TotalCapacity_kW": [7.0, -1.0, 22.0]})
    with pytest.raises(ValueError, match="negative"):
        compute_station_attractiveness(df)


def test_other_columns_untouched():
    df = pd.DataFrame(
        {
            "StationID": [1, 2, 3],
            "TotalCapacity_kW": [7.0, 22.0, 50.0],
            "huff_score_work": [0.1, 0.2, 0.3],
            "label": ["a", "b", "c"],
        }
    )
    df_ref = df.copy(deep=True)
    compute_station_attractiveness(df)

    assert "station_attractiveness" in df.columns
    for col in df_ref.columns:
        pd.testing.assert_series_equal(df[col], df_ref[col], check_names=False)


@pytest.mark.parametrize("script_path", [])
def test_legacy_script_is_frozen(script_path: str):
    result = subprocess.run(
        [sys.executable, script_path],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode != 0
    combined = result.stdout + result.stderr
    assert "frozen as of Stage 7" in combined


@pytest.mark.skipif(
    not UPSTREAM_STATION_PATH.exists() or not UPSTREAM_BACKUP_PATH.exists(),
    reason="real upstream station table not available in this env",
)
def test_stage7_preserves_other_columns_and_adds_one():
    if UPSTREAM_STATION_PATH.suffix == ".parquet":
        pre = pd.read_parquet(UPSTREAM_BACKUP_PATH)
        post = pd.read_parquet(UPSTREAM_STATION_PATH)
    else:
        pre = pd.read_csv(UPSTREAM_BACKUP_PATH)
        post = pd.read_csv(UPSTREAM_STATION_PATH)

    added = set(post.columns) - set(pre.columns)
    removed = set(pre.columns) - set(post.columns)
    assert added == {"station_attractiveness"}
    assert removed == set()

    huff_cols = [col for col in pre.columns if col.startswith("huff_score_")]
    for col in huff_cols:
        pd.testing.assert_series_equal(pre[col], post[col], check_names=False)

    if "label" in pre.columns:
        pd.testing.assert_series_equal(pre["label"], post["label"], check_names=False)

    assert post["station_attractiveness"].min() >= 0
    assert post["station_attractiveness"].notna().all()


@pytest.mark.skipif(
    not RUNTIME_STATION_PATH.exists(),
    reason="runtime station table not available in this env",
)
def test_stage7_runtime_copy_has_new_column():
    runtime_df = pd.read_csv(RUNTIME_STATION_PATH, nrows=1)
    assert "station_attractiveness" in runtime_df.columns


@pytest.mark.skipif(
    not UPSTREAM_STATION_PATH.exists() or not RUNTIME_STATION_PATH.exists(),
    reason="station tables not available in this env",
)
def test_stage7_runtime_copy_matches_upstream_copy():
    upstream_df = pd.read_csv(UPSTREAM_STATION_PATH).sort_values("StationID").reset_index(
        drop=True
    )
    runtime_df = pd.read_csv(RUNTIME_STATION_PATH).sort_values("StationID").reset_index(
        drop=True
    )

    assert list(upstream_df.columns) == list(runtime_df.columns)
    assert len(upstream_df.columns) == 22
    pd.testing.assert_frame_equal(runtime_df, upstream_df)
