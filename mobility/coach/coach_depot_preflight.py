"""Preflight gate for the coach depot-load pipeline (Tier A, plan 2026-06-05).

The TxC source data is supplied by the user after the code lands, so this gate
must fail loudly with a complete problem list before any simulation stage runs.
It also reports the calibration numbers the analyst needs to set
``--sample-block-multiplier`` and ``--home-depot-radius-km`` intentionally
(chains/day vs the 201-coach fleet).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd


# Hard gates (problems -> preflight fails).
MIN_INVENTORY_FILE_RESOLVE_RATE = 0.95
MIN_COORDS_COVERAGE = 0.90
# Soft gates (warnings recorded, preflight still passes).
WARN_PROFILE_FALLBACK_SHARE = 0.50
WARN_LSOA_ATTACH_RATE = 0.85


def run_coach_preflight(
    *,
    coach_root: Path,
    inventory_path: Path,
    journeys: pd.DataFrame | None = None,
    date_index: pd.DataFrame | None = None,
    chains_long: pd.DataFrame | None = None,
    coach_specs: pd.DataFrame | None = None,
    coach_specs_summary: dict[str, Any] | None = None,
    max_relocation_km: float = 50.0,
    lsoa_attach_available: bool = True,
) -> tuple[dict[str, Any], list[str]]:
    """Validate the uploaded TxC tree and the derived frames; list every problem."""
    problems: list[str] = []
    warnings: list[str] = []
    summary: dict[str, Any] = {"coach_root": str(coach_root), "inventory_path": str(inventory_path)}

    # --- TxC tree -----------------------------------------------------------
    coach_root = Path(coach_root)
    inventory_path = Path(inventory_path)
    if not coach_root.is_dir():
        problems.append(f"coach data root does not exist: {coach_root}")
    if not inventory_path.is_file():
        problems.append(f"TxC inventory CSV does not exist: {inventory_path}")
    inventory = None
    if inventory_path.is_file():
        try:
            inventory = pd.read_csv(inventory_path, low_memory=False)
        except Exception as exc:  # noqa: BLE001 - report, do not crash the gate
            problems.append(f"TxC inventory CSV unreadable: {exc}")
    if inventory is not None:
        summary["n_inventory_rows"] = int(len(inventory))
        file_col = next((col for col in ("FilePath", "file_path", "Filename", "FileName") if col in inventory.columns), None)
        if file_col is None:
            problems.append(f"TxC inventory lacks a file-path column (have: {list(inventory.columns)[:10]})")
        else:
            paths = inventory[file_col].dropna().astype(str)
            resolved = sum(1 for value in paths if (coach_root / value).is_file() or Path(value).is_file())
            rate = resolved / len(paths) if len(paths) else 0.0
            summary["inventory_file_resolve_rate"] = round(rate, 4)
            if rate < MIN_INVENTORY_FILE_RESOLVE_RATE:
                problems.append(
                    f"only {rate:.1%} of inventory FilePath entries resolve to files under {coach_root} "
                    f"(gate {MIN_INVENTORY_FILE_RESOLVE_RATE:.0%})"
                )
        for col in ("ServiceStartDate", "ServiceEndDate"):
            if col in inventory.columns:
                values = pd.to_datetime(inventory[col], errors="coerce").dropna()
                if not values.empty:
                    summary[f"{col.lower()}_min"] = values.min().date().isoformat()
                    summary[f"{col.lower()}_max"] = values.max().date().isoformat()
    if coach_root.is_dir():
        n_xml = sum(1 for _ in coach_root.rglob("*.xml"))
        summary["n_xml_files"] = int(n_xml)
        if n_xml == 0:
            problems.append(f"no TransXChange XML files found under {coach_root}")

    # --- journeys -----------------------------------------------------------
    if journeys is not None:
        summary["n_journeys"] = int(len(journeys))
        if journeys.empty:
            problems.append("0 journeys parsed from the TxC tree")
        else:
            coords_ok = pd.Series(True, index=journeys.index)
            for col in ("start_lat", "start_lon", "end_lat", "end_lon"):
                coords_ok &= np.isfinite(pd.to_numeric(journeys.get(col, np.nan), errors="coerce"))
            coords_rate = float(coords_ok.mean())
            summary["journey_coords_coverage"] = round(coords_rate, 4)
            if coords_rate < MIN_COORDS_COVERAGE:
                problems.append(f"journey endpoint coords coverage {coords_rate:.1%} < gate {MIN_COORDS_COVERAGE:.0%}")
            distance = pd.to_numeric(journeys.get("distance_km", np.nan), errors="coerce")
            summary["journey_known_distance_share"] = round(float(distance.notna().mean()), 4)
            if distance.notna().sum() == 0:
                problems.append("no journeys carry a usable distance_km")
            cross = pd.to_numeric(journeys.get("end_h", np.nan), errors="coerce") > 24.0
            summary["journey_cross_midnight_share"] = round(float(cross.mean()), 4)
            if "start_lsoa" in journeys.columns and "end_lsoa" in journeys.columns:
                lsoa_rate = float(
                    (journeys["start_lsoa"].fillna("").astype(str).str.strip().ne("") & journeys["end_lsoa"].fillna("").astype(str).str.strip().ne("")).mean()
                )
                summary["journey_lsoa_attach_rate"] = round(lsoa_rate, 4)
                if lsoa_rate < WARN_LSOA_ATTACH_RATE:
                    warnings.append(f"journey LSOA attach rate {lsoa_rate:.1%} < {WARN_LSOA_ATTACH_RATE:.0%}: depot inference will skew low-confidence")

    # --- operating profiles ---------------------------------------------------
    if date_index is not None and not date_index.empty:
        summary["n_journey_dates"] = int(len(date_index))
        if "profile_source" in date_index.columns:
            fallback_share = float(date_index["profile_source"].astype(str).eq("fallback_uniform").mean())
            summary["profile_fallback_share"] = round(fallback_share, 4)
            if fallback_share > WARN_PROFILE_FALLBACK_SHARE:
                warnings.append(
                    f"{fallback_share:.1%} of journey-dates use fallback_uniform operating profiles: "
                    "active days are uniformly inflated — treat annual totals with caution"
                )
        per_date = date_index.groupby("date")["journey_id"].nunique()
        if per_date.empty or float(per_date.median()) <= 0:
            problems.append("journey date index has no active journeys on any date")
        else:
            summary["journeys_per_date"] = {
                "min": int(per_date.min()),
                "median": float(per_date.median()),
                "p90": float(per_date.quantile(0.9)),
                "max": int(per_date.max()),
                "n_dates": int(len(per_date)),
            }

    # --- chains (calibration numbers) ----------------------------------------
    if chains_long is not None and not chains_long.empty:
        chains_per_date = chains_long.groupby("date")["coach_chain_id"].nunique()
        journeys_per_chain = chains_long.groupby("coach_chain_id")["journey_id"].nunique()
        summary["chains_per_date"] = {
            "min": int(chains_per_date.min()),
            "median": float(chains_per_date.median()),
            "p90": float(chains_per_date.quantile(0.9)),
            "max": int(chains_per_date.max()),
        }
        summary["journeys_per_chain"] = {
            "median": float(journeys_per_chain.median()),
            "p99": float(journeys_per_chain.quantile(0.99)),
            "max": int(journeys_per_chain.max()),
        }
        summary["n_chain_templates"] = int(chains_long["coach_chain_template_id"].nunique())
        if coach_specs is not None and len(coach_specs):
            summary["fleet_coverage_of_median_day"] = round(float(len(coach_specs) / chains_per_date.median()), 4)

    # --- fleet -----------------------------------------------------------------
    if coach_specs is not None:
        summary["n_coach_specs_valid"] = int(len(coach_specs))
        if len(coach_specs) == 0:
            problems.append("no valid coach EV specs after sanity filtering")
    if coach_specs_summary:
        summary.update({f"specs_{key}": value for key, value in coach_specs_summary.items()})

    if not lsoa_attach_available:
        problems.append("ONSPD centroids unavailable: LSOA/region attach and depot inference cannot run")

    summary["warnings"] = warnings
    summary["problems"] = problems
    summary["preflight_ok"] = not problems
    return summary, problems


def write_coach_preflight_summary(summary: dict[str, Any], out_dir: Path | str) -> Path:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    (out / "coach_preflight_summary.json").write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")
    lines = ["# Coach depot-load preflight", ""]
    lines.append(f"- preflight_ok: {summary.get('preflight_ok')}")
    for key, value in summary.items():
        if key in ("problems", "warnings", "preflight_ok"):
            continue
        lines.append(f"- {key}: {value}")
    if summary.get("warnings"):
        lines.extend(["", "## Warnings"])
        lines.extend(f"- {item}" for item in summary["warnings"])
    if summary.get("problems"):
        lines.extend(["", "## Problems (preflight FAILED)"])
        lines.extend(f"- {item}" for item in summary["problems"])
    path = out / "coach_preflight_summary.md"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


__all__ = ["run_coach_preflight", "write_coach_preflight_summary"]
