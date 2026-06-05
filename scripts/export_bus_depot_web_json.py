# Export bus depot charging load as per-day fragments for the Web app, keyed
# by TARGET 2025 calendar dates (month-day alignment, decided 2026-06-05), plus
# a merge script that injects a top-level "depots" key into the existing
# results/YYYY-MM-DD.json files on the user's machine.
#
# Date mapping (month-day alignment): bus wall-clock days 2026-04-17..2026-12-31
# map to 2025-04-17..2025-12-31; 2027-01-01..2027-01-21 map to 2025-01-01..01-21.
# The two ranges do not collide. 2025-01-22..2025-04-16 has no bus data (outside
# the GTFS feed window) and is merged with an empty {} depots block.
#
# Day curves are WALL-CLOCK (charge summed across service_dates per half-hour),
# matching the audited depot-profile convention; 48 half-hour steps to match the
# existing per-day web JSON (connectors convention).

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

import numpy as np
import pandas as pd

STEPS_PER_DAY = 48


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Export bus depot load as per-day web JSON fragments.")
    parser.add_argument("--run-dir", type=Path, default=Path("~/Work/Nature_EV_2025/outputs/bus_annual_depot_load_carryover"))
    parser.add_argument("--out-dir", type=Path, default=Path("~/Work/Nature_EV_2025/outputs/web_export_depot_bus"))
    parser.add_argument("--min-annual-kwh", type=float, default=1.0)
    return parser.parse_args()


def _resolve(path: Path) -> Path:
    return Path(os.path.expandvars(os.path.expanduser(str(path)))).resolve()


def map_to_2025(wall_date: str) -> str:
    """Month-day alignment onto the web app's 2025 calendar."""
    return f"2025-{wall_date[5:]}"


def main() -> None:
    args = parse_args()
    run_dir = _resolve(args.run_dir)
    out_dir = _resolve(args.out_dir)
    fragments_dir = out_dir / "depot_bus_fragments"
    fragments_dir.mkdir(parents=True, exist_ok=True)

    print(f"[web_export] reading {run_dir}/depot_load_15min.parquet", flush=True)
    load = pd.read_parquet(
        run_dir / "depot_load_15min.parquet",
        columns=["depot_id", "depot_lsoa", "depot_lat", "depot_lon", "depot_confidence", "slot_start_datetime", "charge_kwh", "is_warmup"],
    )
    registry = pd.read_parquet(run_dir / "depot_registry.parquet")

    slot_start = pd.to_datetime(load["slot_start_datetime"])
    load["wall_date"] = slot_start.dt.strftime("%Y-%m-%d")
    load["half_hour"] = slot_start.dt.hour * 2 + slot_start.dt.minute // 30

    lat = pd.to_numeric(load["depot_lat"], errors="coerce")
    lon = pd.to_numeric(load["depot_lon"], errors="coerce")
    lsoa = load["depot_lsoa"].fillna("").astype(str).str.strip()
    load["mappable"] = ~(
        load["depot_id"].astype(str).str.endswith("_missing")
        | lsoa.str.lower().isin(("", "missing"))
        | ~np.isfinite(lat)
        | ~np.isfinite(lon)
    )
    stats = (
        load.groupby("depot_id", sort=True)
        .agg(
            annual_charge_kwh=("charge_kwh", "sum"),
            lat=("depot_lat", "first"),
            lon=("depot_lon", "first"),
            lsoa=("depot_lsoa", "first"),
            confidence=("depot_confidence", "first"),
            mappable=("mappable", "first"),
        )
        .reset_index()
    )
    stats = stats.loc[stats["annual_charge_kwh"] >= float(args.min_annual_kwh)].copy()
    load = load.loc[load["depot_id"].isin(set(stats["depot_id"]))]
    print(f"[web_export] {len(stats):,} depots ({int(stats['mappable'].sum()):,} mappable)", flush=True)

    grouped = load.groupby(["wall_date", "depot_id", "half_hour"], sort=True, observed=True)["charge_kwh"].sum()
    source_dates = sorted(load["wall_date"].unique())
    target_for = {date: map_to_2025(date) for date in source_dates}
    if len(set(target_for.values())) != len(target_for):
        raise RuntimeError("month-day mapping collision — source range assumption violated")
    warmup_targets = sorted({map_to_2025(d) for d in load.loc[load["is_warmup"].fillna(False).astype(bool), "wall_date"].unique()})

    peak_kw_by_depot: dict[str, float] = {}
    for source_date in source_dates:
        day = grouped.loc[source_date]
        depots_payload: dict[str, list[float]] = {}
        total = np.zeros(STEPS_PER_DAY)
        for depot_id, sub in day.groupby(level=0, sort=True, observed=True):
            values = np.zeros(STEPS_PER_DAY)
            values[sub.index.get_level_values(1).to_numpy()] = sub.to_numpy() / 0.5
            total += values
            depots_payload[str(depot_id)] = [round(float(v), 3) for v in values]
            day_peak = float(values.max())
            if day_peak > peak_kw_by_depot.get(str(depot_id), 0.0):
                peak_kw_by_depot[str(depot_id)] = day_peak
        target_date = target_for[source_date]
        payload = {
            "target_date": target_date,
            "source_date": source_date,
            "depots": depots_payload,
            "depots_system": {
                "load_kw": [round(float(v), 3) for v in total],
                "n_active_depots": len(depots_payload),
            },
        }
        (fragments_dir / f"{target_date}.json").write_text(json.dumps(payload, ensure_ascii=True, separators=(",", ":")), encoding="utf-8")
    print(f"[web_export] wrote {len(source_dates)} fragments to {fragments_dir}", flush=True)

    is_anchor = registry.drop_duplicates("depot_id").set_index("depot_id")["is_operational_anchor"] if "is_operational_anchor" in registry.columns else pd.Series(dtype=bool)
    depots_index = []
    for row in stats.itertuples(index=False):
        mappable = bool(row.mappable)
        depots_index.append(
            {
                "depot_id": str(row.depot_id),
                "lat": round(float(row.lat), 5) if mappable else None,
                "lon": round(float(row.lon), 5) if mappable else None,
                "lsoa": str(row.lsoa) if mappable else "",
                "confidence": str(row.confidence),
                "mappable": mappable,
                "annual_charge_kwh": round(float(row.annual_charge_kwh), 1),
                "peak_kw": round(peak_kw_by_depot.get(str(row.depot_id), 0.0), 1),
                "is_operational_anchor": bool(is_anchor.get(str(row.depot_id), False)),
            }
        )
    mappable_share = float(stats.loc[stats["mappable"], "annual_charge_kwh"].sum() / stats["annual_charge_kwh"].sum())
    index_payload = {
        "dataset": "bus_depot_charging_load",
        "source_run": str(run_dir),
        "soc_mode": "carryover",
        "time_steps_per_day": STEPS_PER_DAY,
        "step_minutes": 30,
        "value_unit": "avg_kw_per_half_hour",
        "date_mapping": "month_day_alignment_to_2025",
        "dates_with_data": sorted(target_for.values()),
        "dates_without_data_note": "2025-01-22..2025-04-16 falls outside the GTFS feed window; their depots block is empty.",
        "warmup_dates": warmup_targets,
        "n_depots": len(depots_index),
        "n_mappable_depots": int(stats["mappable"].sum()),
        "mappable_charge_share": round(mappable_share, 4),
        "caveats": [
            "Depot anchors are inferred from block terminals, not verified physical garages.",
            "mappable=false depots (opdepot_*_missing) have no resolvable coordinates; list them, never place them on the map.",
            "warmup_dates correspond to the 14 warm-up service days; badge or exclude them in annual aggregates.",
            "Weekday alignment is not preserved by the month-day mapping (a 2025 Saturday may show a bus Friday curve).",
            "source_date inside each day file records the original simulation day for traceability.",
        ],
        "depots": depots_index,
    }
    (out_dir / "depot_bus_index.json").write_text(json.dumps(index_payload, ensure_ascii=True, indent=1), encoding="utf-8")
    pd.DataFrame(depots_index).to_csv(out_dir / "Depots.csv", index=False)
    total_mb = sum(f.stat().st_size for f in fragments_dir.glob("*.json")) / 1e6
    print(f"[web_export] fragments total {total_mb:.1f} MB; index + Depots.csv written to {out_dir}", flush=True)


if __name__ == "__main__":
    main()
