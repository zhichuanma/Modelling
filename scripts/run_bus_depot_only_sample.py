#!/usr/bin/env python
"""Run the depot-only current EV bus stock scenario pipeline."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from mobility.core.spatial import DEFAULT_ONSPD_PATH, load_lsoa_centroids  # noqa: E402
from mobility.bus.depot_only_assignment import build_simulation_cases  # noqa: E402
from mobility.bus.depot_only_events import build_vehicle_day_events  # noqa: E402
from mobility.bus.depot_only_outputs import (  # noqa: E402
    WEIGHTING_MODE,
    aggregate_depot_load_15min,
    build_run_summary,
    depot_load_energy_matches_events,
)
from mobility.bus.depot_only_preflight import run_stage0_preflight, write_preflight_outputs  # noqa: E402
from mobility.bus.depot_only_sampling import DEFAULT_SEED, FULL_EV_INVENTORY_MODE, PILOT_MODE, sample_block_templates  # noqa: E402
from mobility.bus.depot_only_soc import apply_depot_only_soc  # noqa: E402
from mobility.bus.ev_bus_instances import build_ev_bus_instances  # noqa: E402
from mobility.bus.operational_depot import infer_operational_depots  # noqa: E402


DEFAULT_BLOCKS = Path("../Data/EV_behavior/Bus_Data/all_blocks.parquet")
DEFAULT_EV_INVENTORY = Path("data/EV_UK_LSOA_2025_with_energy.csv")
DEFAULT_OUT_DIR = Path("outputs/bus_depot_only_sample")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--blocks", type=Path, default=DEFAULT_BLOCKS)
    parser.add_argument("--ev-inventory", type=Path, default=DEFAULT_EV_INVENTORY)
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    parser.add_argument("--sample-mode", choices=(FULL_EV_INVENTORY_MODE, PILOT_MODE), default=FULL_EV_INVENTORY_MODE)
    parser.add_argument("--n-blocks", type=int, default=None)
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument("--charging-mode", choices=("depot_only",), default="depot_only")
    parser.add_argument("--depot-power-kw", type=float, default=100.0)
    parser.add_argument("--service-date", default="2026-06-03")
    parser.add_argument("--overwrite", action="store_true", help="Allow writing into an existing non-empty output directory.")
    parser.add_argument("--onspd-path", type=Path, default=DEFAULT_ONSPD_PATH)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    blocks_path = _resolve_path(args.blocks)
    inventory_path = _resolve_path(args.ev_inventory)
    out_dir = _resolve_path(args.out_dir)
    _validate_inputs(blocks_path, inventory_path, out_dir, overwrite=bool(args.overwrite))
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"[depot_only] repo_root={REPO_ROOT}", flush=True)
    print(f"[depot_only] blocks={blocks_path}", flush=True)
    print(f"[depot_only] ev_inventory={inventory_path}", flush=True)
    print(f"[depot_only] out_dir={out_dir}", flush=True)

    centroids = load_lsoa_centroids(args.onspd_path)
    block_templates, lsoa_diag, invalid_vehicle_rows, preflight_summary = run_stage0_preflight(
        blocks_path,
        inventory_path,
        onspd_path=args.onspd_path,
        centroids=centroids,
    )
    write_preflight_outputs(
        out_dir,
        summary=preflight_summary,
        block_templates=block_templates,
        lsoa_attach_diagnostics_frame=lsoa_diag,
        invalid_vehicle_rows=invalid_vehicle_rows,
    )

    ev_bus_instances, invalid_vehicle_rows, ev_summary = build_ev_bus_instances(inventory_path)
    ev_bus_instances.to_parquet(out_dir / "ev_bus_instances.parquet", index=False)
    invalid_vehicle_rows.to_parquet(out_dir / "invalid_vehicle_rows.parquet", index=False)
    n_valid = int(ev_summary["n_valid_ev_bus_instances"])

    sampled_blocks, sample_diag = _sample_with_depot_topup(
        block_templates,
        n_valid_ev_bus_instances=n_valid,
        sample_mode=args.sample_mode,
        n_blocks=args.n_blocks,
        seed=args.seed,
        centroids=centroids,
    )
    sampled_blocks.to_parquet(out_dir / "sampled_blocks.parquet", index=False)
    sample_diag.to_parquet(out_dir / "block_sample_diagnostics.parquet", index=False)

    sampled_with_depots, depot_registry, depot_diag = infer_operational_depots(sampled_blocks, centroids=centroids)
    sampled_with_depots.to_parquet(out_dir / "sampled_blocks.parquet", index=False)
    depot_registry.to_parquet(out_dir / "operational_depot_registry.parquet", index=False)
    depot_diag.to_parquet(out_dir / "depot_inference_diagnostics.parquet", index=False)

    cases, assignment_diag = build_simulation_cases(
        ev_bus_instances,
        sampled_with_depots,
        sample_mode=args.sample_mode,
        seed=args.seed,
        service_date=args.service_date,
    )
    cases.to_parquet(out_dir / "simulation_cases.parquet", index=False)
    assignment_diag.to_parquet(out_dir / "vehicle_assignment_diagnostics.parquet", index=False)

    events = build_vehicle_day_events(cases, depot_power_kw=args.depot_power_kw)
    events, soc_summary = apply_depot_only_soc(events, depot_power_kw=args.depot_power_kw)
    events.to_parquet(out_dir / "vehicle_day_events.parquet", index=False)
    soc_summary.to_parquet(out_dir / "case_soc_summary.parquet", index=False)

    depot_load = aggregate_depot_load_15min(events)
    depot_load.to_parquet(out_dir / "depot_load_15min.parquet", index=False)
    if not depot_load_energy_matches_events(depot_load, events):
        raise RuntimeError("depot_load_15min charge_kwh does not match depot charging energy in vehicle_day_events.")

    summary = build_run_summary(
        preflight_summary=preflight_summary,
        sampled_blocks=sampled_with_depots,
        block_sample_diagnostics=sample_diag,
        operational_depot_registry=depot_registry,
        simulation_cases=cases,
        vehicle_day_events=events,
        case_soc_summary=soc_summary,
        depot_load_15min=depot_load,
        sample_mode=args.sample_mode,
        weighting_mode=WEIGHTING_MODE,
    )
    (out_dir / "run_summary.md").write_text(summary, encoding="utf-8")
    print(f"[depot_only] complete cases={len(cases)} charge_kwh={depot_load['charge_kwh'].sum() if not depot_load.empty else 0.0:.3f}", flush=True)


def _sample_with_depot_topup(
    block_templates: pd.DataFrame,
    *,
    n_valid_ev_bus_instances: int,
    sample_mode: str,
    n_blocks: int | None,
    seed: int,
    centroids: pd.DataFrame,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    target = n_valid_ev_bus_instances if sample_mode == FULL_EV_INVENTORY_MODE else int(n_blocks or 0)
    sampled, diag = sample_block_templates(
        block_templates,
        n_valid_ev_bus_instances=n_valid_ev_bus_instances,
        sample_mode=sample_mode,
        n_blocks=n_blocks,
        seed=seed,
    )
    if sample_mode != FULL_EV_INVENTORY_MODE:
        return sampled, diag
    inferred, _, depot_diag = infer_operational_depots(sampled, centroids=centroids)
    missing_ids = set(
        inferred.loc[inferred["operational_depot_lsoa"].fillna("").astype(str).str.strip().eq(""), "block_template_id"].astype(str)
    )
    if not missing_ids:
        return sampled, diag
    keep_ids = set(inferred.loc[~inferred["block_template_id"].astype(str).isin(missing_ids), "block_template_id"].astype(str))
    extra_needed = target - len(keep_ids)
    excluded = keep_ids | missing_ids
    topup, topup_diag = sample_block_templates(
        block_templates,
        n_valid_ev_bus_instances=extra_needed,
        sample_mode=FULL_EV_INVENTORY_MODE,
        seed=seed + 1,
        exclude_block_template_ids=excluded,
    )
    combined = pd.concat(
        [
            sampled.loc[sampled["block_template_id"].astype(str).isin(keep_ids)].copy(),
            topup,
        ],
        ignore_index=True,
    )
    if len(combined) != target:
        raise RuntimeError(f"Unable to top up sampled blocks after missing depot drops: {len(combined)} != {target}")
    combined = combined.reset_index(drop=True)
    combined["sample_seq"] = range(len(combined))
    diag = pd.concat([diag.assign(topup_round=0), topup_diag.assign(topup_round=1)], ignore_index=True)
    diag["n_missing_depot_blocks_dropped_before_assignment"] = len(missing_ids)
    diag["n_topup_blocks_sampled"] = len(topup)
    diag["missing_depot_diagnostic_rows"] = [depot_diag.to_dict(orient="records")] + [None] * (len(diag) - 1)
    return combined, diag


def _resolve_path(path: Path) -> Path:
    path = Path(path).expanduser()
    if path.is_absolute():
        return path
    return (REPO_ROOT / path).resolve()


def _validate_inputs(blocks_path: Path, inventory_path: Path, out_dir: Path, *, overwrite: bool) -> None:
    if not blocks_path.is_file():
        raise FileNotFoundError(f"Block parquet input not found: {blocks_path}")
    if not inventory_path.is_file():
        raise FileNotFoundError(f"EV inventory input not found: {inventory_path}")
    if out_dir.exists() and any(out_dir.iterdir()) and not overwrite:
        raise FileExistsError(f"Output directory exists and is not empty: {out_dir}. Use --overwrite or choose a new directory.")


if __name__ == "__main__":
    main()
