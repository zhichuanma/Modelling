"""CLI to freeze per-person week patterns for Stage 2c."""

from __future__ import annotations

import argparse
from pathlib import Path
import zlib

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from mobility.cars.data_loader import MILES_TO_KM, NTS_PURPOSE_MAP
from mobility.cars.week_pattern import BUILD_REQUIRED_COLUMNS, LIBRARY_COLUMNS, build_person_week_library

REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_NTS_TRIPS_PATH = REPO_ROOT / "Modelling" / "data" / "trip_recent_filtered.csv"
DEFAULT_OUT_PATH = REPO_ROOT / "Modelling" / "data" / "person_week_library.parquet"
ROW_GROUP_BUCKETS = 16


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--nts-trips", type=Path, default=DEFAULT_NTS_TRIPS_PATH)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT_PATH)
    return parser


def _load_nts_trips_for_library(nts_trips_path: Path) -> pd.DataFrame:
    available_columns = pd.read_csv(nts_trips_path, nrows=0).columns.tolist()
    if set(BUILD_REQUIRED_COLUMNS).issubset(available_columns):
        return pd.read_csv(nts_trips_path, usecols=BUILD_REQUIRED_COLUMNS)

    raw_columns = [
        "IndividualID",
        "DayID",
        "TravDay",
        "JourSeq",
        "TripPurpFrom_B01ID",
        "TripPurpTo_B01ID",
        "TripStartHours",
        "TripStartMinutes",
        "TripEndHours",
        "TripEndMinutes",
        "TripDisExSW",
        "SurveyYear",
    ]
    missing_raw_columns = sorted(set(raw_columns).difference(available_columns))
    if missing_raw_columns:
        raise ValueError(
            f"{nts_trips_path} is missing Stage 2c raw columns: {missing_raw_columns}"
        )

    nts_trips = pd.read_csv(nts_trips_path, usecols=raw_columns)
    nts_trips["departure_time"] = (
        pd.to_numeric(nts_trips["TripStartHours"], errors="coerce")
        + pd.to_numeric(nts_trips["TripStartMinutes"], errors="coerce") / 60.0
    )
    nts_trips["arrival_time"] = (
        pd.to_numeric(nts_trips["TripEndHours"], errors="coerce")
        + pd.to_numeric(nts_trips["TripEndMinutes"], errors="coerce") / 60.0
    )
    nts_trips["distance_km"] = pd.to_numeric(nts_trips["TripDisExSW"], errors="coerce") * MILES_TO_KM
    nts_trips["purpose_from"] = nts_trips["TripPurpFrom_B01ID"].map(NTS_PURPOSE_MAP).fillna("other")
    nts_trips["purpose_to"] = nts_trips["TripPurpTo_B01ID"].map(NTS_PURPOSE_MAP).fillna("other")
    nts_trips = nts_trips.dropna(subset=["departure_time", "arrival_time"]).reset_index(drop=True)
    return nts_trips.loc[:, BUILD_REQUIRED_COLUMNS]


def _person_bucket(person_id: str) -> int:
    return zlib.crc32(str(person_id).encode("utf-8")) % ROW_GROUP_BUCKETS


def _write_person_week_library_parquet(library_df: pd.DataFrame, out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)

    ordered = library_df.loc[:, LIBRARY_COLUMNS].copy()
    ordered["person_id"] = ordered["person_id"].astype("string[python]")
    ordered["pattern_id"] = pd.to_numeric(ordered["pattern_id"], errors="raise").astype("int64")
    ordered["day_of_week"] = pd.to_numeric(ordered["day_of_week"], errors="raise").astype("int64")
    ordered["chain_json"] = ordered["chain_json"].astype("string[python]")
    ordered["person_bucket"] = ordered["person_id"].map(_person_bucket).astype("int64")
    ordered = ordered.sort_values(
        ["person_bucket", "person_id", "pattern_id", "day_of_week"],
        kind="stable",
    ).reset_index(drop=True)

    schema = pa.schema(
        [
            pa.field("person_id", pa.string()),
            pa.field("pattern_id", pa.int64()),
            pa.field("day_of_week", pa.int64()),
            pa.field("chain_json", pa.string()),
        ]
    )

    with pq.ParquetWriter(out_path, schema=schema, compression="zstd") as writer:
        for person_bucket in range(ROW_GROUP_BUCKETS):
            bucket_df = (
                ordered.loc[ordered["person_bucket"] == person_bucket, LIBRARY_COLUMNS]
                .reset_index(drop=True)
            )
            if bucket_df.empty:
                continue
            table = pa.Table.from_pandas(bucket_df, schema=schema, preserve_index=False)
            writer.write_table(table, row_group_size=len(bucket_df))


def main() -> None:
    args = _build_parser().parse_args()
    nts_trips = _load_nts_trips_for_library(args.nts_trips)
    library_df = build_person_week_library(nts_trips)
    _write_person_week_library_parquet(library_df, args.out)

    person_count = int(library_df["person_id"].nunique())
    pattern_count = int(library_df.loc[:, ["person_id", "pattern_id"]].drop_duplicates().shape[0])
    avg_patterns_per_person = pattern_count / person_count if person_count else 0.0

    print(f"Wrote {len(library_df):,} rows to {args.out}")
    print(f"#persons: {person_count:,}")
    print(f"#patterns: {pattern_count:,}")
    print(f"avg patterns/person: {avg_patterns_per_person:.3f}")


if __name__ == "__main__":
    main()
