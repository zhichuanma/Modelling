"""Scope: feed-year coach chain simulation. Reads journeys + builds chains + builds calendar + simulates each chain across the feed year. Does NOT do operator-real vehicle blocking; chain assignment is a first-fit heuristic (see chain_builder.py)."""

from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from mobility.coach.annual_simulation import simulate_coach_fleet_year
from mobility.coach.calendar import build_journey_date_index
from mobility.coach.chain_builder import build_coach_chains
from mobility.coach.coach_fleet import COACH_FLEET_PATH, load_coach_fleet
from mobility.coach.data_loader import DEFAULT_COACH_ROOT, DEFAULT_JOURNEYS_PATH, DEFAULT_STOP_SEQUENCES_PATH
from mobility.coach.stop_geometry import attach_lsoa_to_journeys


LOG = logging.getLogger("coach.annual_pipeline")

DEFAULT_PER_CHAIN_OUT = REPO_ROOT / "outputs" / "coach_annual_per_chain.parquet"
DEFAULT_LOAD_PROFILE_OUT = REPO_ROOT / "outputs" / "coach_annual_load_profile.parquet"


def _endpoint_coordinates(stop_sequences: pd.DataFrame) -> pd.DataFrame:
    if stop_sequences.empty or "journey_id" not in stop_sequences.columns:
        return pd.DataFrame(columns=["journey_id", "start_lat", "start_lon", "end_lat", "end_lon"])
    required = {"stop_sequence", "lat", "lon"}
    if not required.issubset(stop_sequences.columns):
        return pd.DataFrame(columns=["journey_id", "start_lat", "start_lon", "end_lat", "end_lon"])
    rows = []
    for journey_id, group in stop_sequences.groupby("journey_id", sort=False):
        ordered = group.sort_values("stop_sequence", kind="stable")
        first = ordered.iloc[0]
        last = ordered.iloc[-1]
        rows.append(
            {
                "journey_id": str(journey_id),
                "start_lat": first.get("lat"),
                "start_lon": first.get("lon"),
                "end_lat": last.get("lat"),
                "end_lon": last.get("lon"),
            }
        )
    return pd.DataFrame.from_records(rows)


def _prepare_journeys(journeys: pd.DataFrame, stop_sequences: pd.DataFrame) -> pd.DataFrame:
    out = journeys.copy()
    if not {"start_lat", "start_lon", "end_lat", "end_lon"}.issubset(out.columns):
        endpoints = _endpoint_coordinates(stop_sequences)
        if not endpoints.empty:
            out = out.merge(endpoints, on="journey_id", how="left")
    distance = pd.to_numeric(out.get("distance_km"), errors="coerce")
    before = len(out)
    out = out.loc[distance.notna()].copy()
    dropped = before - len(out)
    if dropped:
        LOG.warning("dropped %d journeys without distance_km before annual chaining", dropped)
    if {"start_lsoa", "end_lsoa"}.issubset(out.columns):
        return out
    if {"start_lat", "start_lon", "end_lat", "end_lon"}.issubset(out.columns):
        try:
            out = attach_lsoa_to_journeys(out)
        except Exception as exc:  # noqa: BLE001 - LSOA is post-hoc attribution only
            LOG.warning("could not attach coach journey LSOA codes: %s", exc)
    return out


def _limit_chains(chains: pd.DataFrame, limit: int | None) -> pd.DataFrame:
    if limit is None:
        return chains
    group_col = "coach_chain_template_id" if "coach_chain_template_id" in chains.columns else "coach_chain_id"
    keep = list(dict.fromkeys(chains[group_col].astype(str).tolist()))[: int(limit)]
    return chains.loc[chains[group_col].astype(str).isin(set(keep))].copy()


def run_pipeline(
    *,
    journeys_parquet: Path,
    stop_sequences_parquet: Path,
    fleet_path: Path,
    per_chain_out: Path,
    load_profile_out: Path,
    seed: int,
    warm_up_days: int,
    limit: int | None,
    n_workers: int,
    allow_layover_charging: bool = False,
    layover_charge_kw: float = 0.0,
    min_layover_for_charging_h: float = 0.0,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    if n_workers != 1:
        LOG.warning("n_workers=%s requested; annual v1 still runs serially.", n_workers)
    t0 = time.time()
    LOG.info("loading coach journeys from %s", journeys_parquet)
    journeys = pd.read_parquet(journeys_parquet)
    LOG.info("loading coach stop sequences from %s", stop_sequences_parquet)
    stop_sequences = pd.read_parquet(stop_sequences_parquet) if Path(stop_sequences_parquet).exists() else pd.DataFrame()
    journeys = _prepare_journeys(journeys, stop_sequences)

    LOG.info("building per-journey feed-year calendar")
    date_index = build_journey_date_index(journeys, DEFAULT_COACH_ROOT)
    LOG.info("building first-fit coach chains")
    chains = build_coach_chains(journeys, date_index)
    chains = _limit_chains(chains, limit)
    if chains.empty:
        raise ValueError("No coach chains were available after filtering/limit.")

    LOG.info("loading coach fleet from %s", fleet_path)
    fleet = load_coach_fleet(fleet_path)
    LOG.info("simulating %d synthetic chain templates", chains["coach_chain_template_id"].nunique())
    per_chain, load_profile = simulate_coach_fleet_year(
        chains,
        fleet,
        journeys,
        seed=int(seed),
        warm_up_days=int(warm_up_days),
        allow_layover_charging=allow_layover_charging,
        layover_charge_kw=float(layover_charge_kw),
        min_layover_for_charging_h=float(min_layover_for_charging_h),
    )

    per_chain_out.parent.mkdir(parents=True, exist_ok=True)
    load_profile_out.parent.mkdir(parents=True, exist_ok=True)
    per_chain.to_parquet(per_chain_out, index=False)
    load_profile.to_parquet(load_profile_out, index=False)
    LOG.info(
        "wrote %d per-chain rows and %d load rows in %.1fs",
        len(per_chain),
        len(load_profile),
        time.time() - t0,
    )
    return per_chain, load_profile


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--journeys-parquet", type=Path, default=DEFAULT_JOURNEYS_PATH)
    parser.add_argument("--stop-sequences-parquet", type=Path, default=DEFAULT_STOP_SEQUENCES_PATH)
    parser.add_argument("--fleet-path", type=Path, default=COACH_FLEET_PATH)
    parser.add_argument("--per-chain-out", type=Path, default=DEFAULT_PER_CHAIN_OUT)
    parser.add_argument("--load-profile-out", type=Path, default=DEFAULT_LOAD_PROFILE_OUT)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--warm-up-days", type=int, default=0)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--n-workers", type=int, default=1)
    parser.add_argument("--allow-layover-charging", action="store_true", default=False)
    parser.add_argument("--layover-charge-kw", type=float, default=0.0)
    parser.add_argument("--min-layover-for-charging-h", type=float, default=0.0)
    return parser


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    args = _parser().parse_args(argv)
    try:
        run_pipeline(
            journeys_parquet=args.journeys_parquet,
            stop_sequences_parquet=args.stop_sequences_parquet,
            fleet_path=args.fleet_path,
            per_chain_out=args.per_chain_out,
            load_profile_out=args.load_profile_out,
            seed=args.seed,
            warm_up_days=args.warm_up_days,
            limit=args.limit,
            n_workers=args.n_workers,
            allow_layover_charging=args.allow_layover_charging,
            layover_charge_kw=args.layover_charge_kw,
            min_layover_for_charging_h=args.min_layover_for_charging_h,
        )
    except Exception:  # noqa: BLE001 - top-level guard for CLI use
        LOG.exception("coach annual pipeline failed")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
