"""Offline builder for coach journey parquet outputs."""

from __future__ import annotations

import argparse
from pathlib import Path

from .data_loader import (
    DEFAULT_INVENTORY_PATH,
    DEFAULT_JOURNEYS_PATH,
    DEFAULT_STOP_SEQUENCES_PATH,
    write_all_coach_tables,
    summarize_journey_quality,
)


def main() -> None:
    parser = argparse.ArgumentParser(description="Build all coach journey parquet outputs.")
    parser.add_argument("--inventory", type=Path, default=DEFAULT_INVENTORY_PATH)
    parser.add_argument("--journeys-out", type=Path, default=DEFAULT_JOURNEYS_PATH)
    parser.add_argument("--stop-sequences-out", type=Path, default=DEFAULT_STOP_SEQUENCES_PATH)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--progress-interval", type=int, default=25)
    args = parser.parse_args()

    journeys, _stop_sequences = write_all_coach_tables(
        inventory_path=args.inventory,
        journeys_path=args.journeys_out,
        stop_sequences_path=args.stop_sequences_out,
        limit=args.limit,
        progress_interval=args.progress_interval,
    )
    quality = summarize_journey_quality(journeys).iloc[0]
    print(
        "Built coach journeys: "
        f"{int(quality['total_journeys']):,} total, "
        f"{int(quality['known_distance_journeys']):,} known-distance, "
        f"{quality['known_distance_pct']:.1f}% known."
    )


if __name__ == "__main__":
    main()
