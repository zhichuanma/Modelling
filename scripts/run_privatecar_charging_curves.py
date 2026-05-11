"""Build private-car public station charging curves for web export."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import json

import pandas as pd

from mobility.cars.station_curves import (
    export_web_json_files,
    run_preflight_referential_integrity_check,
    run_privatecar_station_curve_pipeline,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Reuse the existing private-car schedule/station/simulation model and "
            "export public station-level 15-minute charging curves."
        )
    )
    parser.add_argument("--data-dir", type=Path, default=REPO_ROOT / "data")
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=REPO_ROOT / "outputs" / "privatecar_charging_curves_2025",
    )
    parser.add_argument(
        "--destination-table-path",
        type=Path,
        default=REPO_ROOT.parent
        / "Data"
        / "Charging_stations"
        / "OSM_POI_Labeling"
        / "destination_choice_table.parquet",
    )
    parser.add_argument("--year", type=int, default=2025)
    parser.add_argument(
        "--max-vehicles",
        type=int,
        default=None,
        help="Optional deterministic smoke/sample limit. Omit for full private-car fleet.",
    )
    parser.add_argument("--chunk-size", type=int, default=100)
    parser.add_argument(
        "--vehicle-shard-index",
        type=int,
        default=None,
        help=(
            "Run only this deterministic round-robin vehicle shard. "
            "Use with --vehicle-shard-count; indexes are zero-based."
        ),
    )
    parser.add_argument(
        "--vehicle-shard-count",
        type=int,
        default=None,
        help="Total deterministic round-robin vehicle shards for parallel full-fleet runs.",
    )
    parser.add_argument(
        "--destination-cache-mode",
        choices=["origin", "key"],
        default="origin",
        help="Destination parquet cache granularity. 'origin' loads all purposes for an origin LSOA.",
    )
    parser.add_argument("--main-seed", type=int, default=20260422)
    parser.add_argument("--warmup-seed", type=int, default=20260423)
    parser.add_argument("--progress-interval", type=int, default=0)
    parser.add_argument("--resume", action="store_true", help="Reuse completed chunk checkpoints in output-dir.")
    parser.add_argument(
        "--no-checkpoint",
        action="store_true",
        help="Do not write chunk-level intermediate station curves/counts.",
    )
    parser.add_argument(
        "--skip-web-json",
        action="store_true",
        help="Write parquet/csv/metadata/report only; skip web station-date JSON.",
    )
    parser.add_argument(
        "--web-json-only",
        action="store_true",
        help="Generate Web JSON from existing parquet/csv outputs in output-dir without rerunning simulation.",
    )
    parser.add_argument(
        "--preflight-only",
        action="store_true",
        help="Only check private-car person_id referential integrity against person_week_library.",
    )
    parser.add_argument(
        "--web-station-id",
        action="append",
        default=None,
        help="Limit Web JSON export to this station_id. Can be supplied multiple times.",
    )
    parser.add_argument("--web-date-from", default=None, help="Limit Web JSON export to dates >= YYYY-MM-DD.")
    parser.add_argument("--web-date-to", default=None, help="Limit Web JSON export to dates <= YYYY-MM-DD.")
    parser.add_argument(
        "--compact-web-json",
        action="store_true",
        help="Write compact JSON rather than pretty-printed daily JSON.",
    )
    parser.add_argument(
        "--skip-json-parse-validation",
        action="store_true",
        help="Skip rereading written Web JSON files for parse validation.",
    )
    return parser.parse_args()


def _load_station_metadata_from_json(path: Path) -> pd.DataFrame:
    payload = json.loads(path.read_text(encoding="utf-8"))
    records = payload.get("stations", payload if isinstance(payload, list) else [])
    return pd.DataFrame(records)


def _run_web_json_only(args: argparse.Namespace) -> None:
    out = args.output_dir
    station_curve = pd.read_parquet(out / f"station_charging_curve_15min_{args.year}.parquet")
    station_summary = pd.read_csv(out / f"station_summary_{args.year}.csv")
    station_metadata = _load_station_metadata_from_json(out / f"station_metadata_{args.year}.json")
    counts_path = out / f"station_day_counts_{args.year}.parquet"
    if counts_path.exists():
        station_day_counts = pd.read_parquet(counts_path)
    else:
        station_day_counts = pd.DataFrame(
            columns=["station_id", "date", "unique_vehicles", "total_sessions"]
        )

    metrics = export_web_json_files(
        station_curve,
        station_summary,
        station_metadata,
        station_day_counts,
        out,
        year=args.year,
        station_ids=args.web_station_id,
        date_from=args.web_date_from,
        date_to=args.web_date_to,
        json_indent=None if args.compact_web_json else 2,
        validate_written_json=not args.skip_json_parse_validation,
    )
    print("\n=== Private-car Web JSON export ===")
    print(f"output_dir: {out}")
    print(f"json_files: {metrics['json_file_count']:,}")
    print(f"station_dates_with_96_points: {metrics['station_dates_with_96_points']:,}")
    print(f"station_dates_without_96_points: {metrics['station_dates_without_96_points']:,}")
    print(f"json_parse_failures: {metrics['json_parse_failures']:,}")


def main() -> None:
    args = parse_args()
    if args.preflight_only:
        report = run_preflight_referential_integrity_check(
            data_dir=args.data_dir,
            output_dir=args.output_dir,
            year=args.year,
        )
        summary = report["summary"]
        print("\n=== Private-car referential integrity preflight ===")
        print(f"output_dir: {args.output_dir}")
        print(f"private_car_vehicle_rows: {summary['private_car_vehicle_rows']:,}")
        print(f"private_car_unique_person_ids: {summary['private_car_unique_person_ids']:,}")
        print(f"person_week_library_unique_person_ids: {summary['person_week_library_unique_person_ids']:,}")
        print(f"missing_person_id_count: {summary['missing_person_id_count']:,}")
        print(f"missing_person_id_rate: {summary['missing_person_id_rate']:.9f}")
        print(f"missing_vehicle_row_count: {summary['missing_vehicle_row_count']:,}")
        print(f"dtype_mismatch_resolved_by_normalization: {summary['dtype_mismatch_resolved_by_normalization']}")
        print(f"report: {args.output_dir / 'data_quality_report.md'}")
        return

    if args.web_json_only:
        _run_web_json_only(args)
        return

    result = run_privatecar_station_curve_pipeline(
        data_dir=args.data_dir,
        output_dir=args.output_dir,
        destination_table_path=args.destination_table_path,
        destination_cache_mode=args.destination_cache_mode,
        year=args.year,
        max_vehicles=args.max_vehicles,
        vehicle_shard_index=args.vehicle_shard_index,
        vehicle_shard_count=args.vehicle_shard_count,
        chunk_size=args.chunk_size,
        main_seed=args.main_seed,
        warmup_seed=args.warmup_seed,
        write_web_json=not args.skip_web_json,
        web_station_ids=args.web_station_id,
        web_date_from=args.web_date_from,
        web_date_to=args.web_date_to,
        web_json_indent=None if args.compact_web_json else 2,
        validate_written_json=not args.skip_json_parse_validation,
        checkpoint_chunks=not args.no_checkpoint,
        resume=args.resume,
        progress_interval=args.progress_interval,
    )
    metrics = result["metrics"]
    print("\n=== Private-car station curve outputs ===")
    print(f"output_dir: {result['output_dir']}")
    print(f"vehicles_processed: {metrics.private_vehicle_count_run:,}")
    print(f"failed_vehicles: {metrics.failed_vehicle_count:,}")
    print(f"station_curve_rows: {metrics.station_curve_row_count:,}")
    print(f"station_summary_rows: {metrics.station_summary_row_count:,}")
    print(f"json_files: {metrics.json_file_count:,}")
    print(f"public_station_energy_kwh: {metrics.station_curve_energy_kwh:.3f}")
    print(f"profile_log: {Path(result['output_dir']) / f'profiling_log_{args.year}.csv'}")


if __name__ == "__main__":
    main()
