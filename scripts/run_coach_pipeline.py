"""Coach fleet single-journey simulation pipeline (v1).

Scope: batch single-journey simulation only. Does NOT do vehicle-to-journey
assignment or year-long scheduling.

For each journey row in the input parquet, this script samples one coach EV
from the prepared fleet table and runs ``simulate_coach_journey`` on the pair,
writing one row of summary metrics per journey to the output parquet.
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from mobility.coach.coach_fleet import COACH_FLEET_PATH, load_coach_fleet, sample_coach_ev
from mobility.coach.data_loader import DEFAULT_JOURNEYS_PATH, DEFAULT_STOP_SEQUENCES_PATH
from mobility.coach.sim_adapter import simulate_coach_journey


LOG = logging.getLogger("coach.pipeline")

OUTPUT_COLUMNS = [
    "journey_id",
    "ev_id",
    "feasible",
    "total_kwh",
    "soc_floor_hit_h",
    "soc_clamped_to_zero",
]


def _resolve_journey_id(row: pd.Series, fallback_index: int) -> str:
    value = row.get("journey_id") if "journey_id" in row.index else None
    if pd.notna(value):
        return str(value)
    code = row.get("vehicle_journey_code") if "vehicle_journey_code" in row.index else None
    if pd.notna(code):
        return str(code)
    return f"journey_{fallback_index}"


def _stop_seq_for_journey(stop_sequences: pd.DataFrame | None, journey_id: str) -> pd.DataFrame:
    if stop_sequences is None or stop_sequences.empty or "journey_id" not in stop_sequences.columns:
        return pd.DataFrame(columns=["stop_sequence", "stop_point_ref"])
    matched = stop_sequences.loc[stop_sequences["journey_id"].astype(str).eq(journey_id)]
    return matched if not matched.empty else pd.DataFrame(columns=["stop_sequence", "stop_point_ref"])


def _simulate_one(
    journey_row: pd.Series,
    stop_seq: pd.DataFrame,
    ev_row: pd.Series,
    journey_id: str,
) -> dict[str, Any]:
    try:
        result = simulate_coach_journey(journey_row, stop_seq, ev_row)
        feasible = bool(result["feasibility"]["feasible_single_charge"])
        total_kwh = float(result["total_consumed_kwh"])
        soc_floor_hit_h = result["soc_floor_hit_h"]
        soc_clamped_to_zero = bool(result["soc_clamped_to_zero"])
    except Exception as exc:  # noqa: BLE001 — keep batch alive
        LOG.warning("journey %s failed: %s", journey_id, exc)
        feasible = False
        total_kwh = float("nan")
        soc_floor_hit_h = None
        soc_clamped_to_zero = False
    return {
        "journey_id": journey_id,
        "ev_id": str(ev_row.get("EV_ID", "")) if "EV_ID" in ev_row.index else "",
        "feasible": feasible,
        "total_kwh": total_kwh,
        "soc_floor_hit_h": float(soc_floor_hit_h) if soc_floor_hit_h is not None else float("nan"),
        "soc_clamped_to_zero": soc_clamped_to_zero,
    }


def run_pipeline(
    journeys_parquet: Path,
    output_parquet: Path,
    *,
    fleet_path: Path = COACH_FLEET_PATH,
    stop_sequences_parquet: Path | None = None,
    seed: int = 0,
    limit: int | None = None,
    n_workers: int = 1,
) -> pd.DataFrame:
    if n_workers != 1:
        LOG.warning("n_workers=%s requested; v1 only supports serial execution.", n_workers)

    journeys_parquet = Path(journeys_parquet)
    output_parquet = Path(output_parquet)

    LOG.info("loading journeys from %s", journeys_parquet)
    journeys = pd.read_parquet(journeys_parquet)
    if limit is not None:
        journeys = journeys.head(int(limit))

    LOG.info("loading coach fleet from %s", fleet_path)
    fleet = load_coach_fleet(fleet_path)

    stop_sequences: pd.DataFrame | None
    if stop_sequences_parquet is not None and Path(stop_sequences_parquet).exists():
        LOG.info("loading stop sequences from %s", stop_sequences_parquet)
        stop_sequences = pd.read_parquet(stop_sequences_parquet)
    else:
        stop_sequences = None

    rng = np.random.default_rng(int(seed))

    records: list[dict[str, Any]] = []
    start = time.time()
    for index, (_, journey_row) in enumerate(journeys.iterrows()):
        journey_id = _resolve_journey_id(journey_row, index)
        ev_row = sample_coach_ev(fleet, rng)
        stop_seq = _stop_seq_for_journey(stop_sequences, journey_id)
        records.append(_simulate_one(journey_row, stop_seq, ev_row, journey_id))
    elapsed = time.time() - start
    LOG.info("simulated %d journeys in %.1fs", len(records), elapsed)

    output_df = pd.DataFrame(records, columns=OUTPUT_COLUMNS)
    output_parquet.parent.mkdir(parents=True, exist_ok=True)
    output_df.to_parquet(output_parquet, index=False)
    LOG.info("wrote %d rows to %s", len(output_df), output_parquet)
    return output_df


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--journeys-parquet", type=Path, default=DEFAULT_JOURNEYS_PATH)
    parser.add_argument("--output-parquet", type=Path, required=True)
    parser.add_argument("--fleet-path", type=Path, default=COACH_FLEET_PATH)
    parser.add_argument("--stop-sequences-parquet", type=Path, default=DEFAULT_STOP_SEQUENCES_PATH)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--n-workers", type=int, default=1)
    return parser


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    args = _parser().parse_args(argv)
    try:
        run_pipeline(
            journeys_parquet=args.journeys_parquet,
            output_parquet=args.output_parquet,
            fleet_path=args.fleet_path,
            stop_sequences_parquet=args.stop_sequences_parquet,
            seed=args.seed,
            limit=args.limit,
            n_workers=args.n_workers,
        )
    except Exception:  # noqa: BLE001 — top-level guard
        LOG.exception("coach pipeline failed")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
