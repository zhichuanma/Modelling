"""Run queue-aware private-car station post-processing from existing outputs."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONNECTOR_PATH = REPO_ROOT / "data" / "UK_OCM_connectors_expanded_with_bus_and_LAD_LSOA.csv"
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import pandas as pd

from mobility.cars.station_queue import (
    QueueModelConfig,
    export_queue_outputs,
    load_station_metadata_json,
    run_queue_model_for_events,
    write_queue_model_report,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Apply deterministic finite-connector station queueing to an existing "
            "private-car station curve output directory."
        )
    )
    parser.add_argument(
        "--input-dir",
        type=Path,
        default=REPO_ROOT / "outputs" / "privatecar_charging_curves_2025",
        help="Directory containing baseline private-car charging outputs.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help="Directory for queue-aware outputs. Defaults to --input-dir.",
    )
    parser.add_argument("--year", type=int, default=2025)
    parser.add_argument("--charging-events-path", type=Path, default=None)
    parser.add_argument("--station-curve-path", type=Path, default=None)
    parser.add_argument("--station-metadata-path", type=Path, default=None)
    parser.add_argument(
        "--connector-path",
        type=Path,
        default=None,
        help=(
            "Optional connector table with StationID/station_id plus Power_kW and optional Quantity. "
            "If omitted, data/UK_OCM_connectors_expanded_with_bus_and_LAD_LSOA.csv is used when present."
        ),
    )
    parser.add_argument("--fallback-connector-power-kw", type=float, default=7.0)
    parser.add_argument("--fallback-connector-count", type=int, default=None)
    parser.add_argument("--allow-service-after-window", action="store_true")
    parser.add_argument("--max-delay-min", type=float, default=0.0)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    input_dir = args.input_dir
    output_dir = args.output_dir or input_dir
    events_path = args.charging_events_path or input_dir / "private_car_charging_events.parquet"
    curve_path = args.station_curve_path or input_dir / f"station_charging_curve_15min_{args.year}.parquet"
    metadata_path = args.station_metadata_path or input_dir / f"station_metadata_{args.year}.json"

    charging_events = pd.read_parquet(events_path)
    baseline_curve = pd.read_parquet(curve_path)
    station_metadata = load_station_metadata_json(metadata_path)
    connector_path = args.connector_path
    if connector_path is None and DEFAULT_CONNECTOR_PATH.exists():
        connector_path = DEFAULT_CONNECTOR_PATH
    connector_table = pd.read_csv(connector_path) if connector_path is not None else None

    config = QueueModelConfig(
        fallback_connector_power_kw=args.fallback_connector_power_kw,
        fallback_connector_count=args.fallback_connector_count,
        allow_service_after_window=args.allow_service_after_window,
        max_delay_min=args.max_delay_min,
    )
    outputs = run_queue_model_for_events(
        charging_events,
        station_metadata,
        baseline_station_curve=baseline_curve,
        connector_table=connector_table,
        config=config,
        year=args.year,
    )
    export_queue_outputs(
        output_dir,
        year=args.year,
        config=config,
        station_capacity=outputs["station_capacity"],
        queue_sessions=outputs["queue_sessions"],
        queue_curve_15min=outputs["queue_curve_15min"],
        queue_curve_hourly=outputs["queue_curve_hourly"],
        queue_summary=outputs["queue_summary"],
        queue_comparison=outputs["queue_comparison"],
    )
    write_queue_model_report(
        output_dir,
        year=args.year,
        config=config,
        station_capacity=outputs["station_capacity"],
        queue_sessions=outputs["queue_sessions"],
        queue_summary=outputs["queue_summary"],
        queue_comparison=outputs["queue_comparison"],
    )

    print("\n=== Private-car station queue outputs ===")
    print(f"input_dir: {input_dir}")
    print(f"output_dir: {output_dir}")
    print(f"public_sessions: {len(outputs['queue_sessions']):,}")
    print(f"queue_curve_15min_rows: {len(outputs['queue_curve_15min']):,}")
    print(f"queue_summary_rows: {len(outputs['queue_summary']):,}")
    print(f"connector_path: {connector_path if connector_path is not None else 'fallback_station_capacity_only'}")
    print(f"queue_report: {output_dir / f'queue_model_report_{args.year}.md'}")


if __name__ == "__main__":
    main()
