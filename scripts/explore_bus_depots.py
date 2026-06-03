"""Explore bus block endpoint LSOAs for depot threshold selection."""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import matplotlib
import numpy as np
import pandas as pd

matplotlib.use("Agg")
import matplotlib.pyplot as plt

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from mobility.core.spatial import (  # noqa: E402
    load_lsoa_boundary_index,
    load_lsoa_centroids,
    nearest_lsoa_for_points,
    query_lsoa_polygons,
)

DEFAULT_BLOCKS = REPO_ROOT / "outputs" / "all_blocks.parquet"
DEFAULT_OUTPUT_DIR = REPO_ROOT / "outputs" / "diagnostics" / "bus_depot_eda"
FALLBACK_MAX_KM = 0.25
THRESHOLDS = (1, 3, 5, 10)


def _log(msg: str) -> None:
    print(f"[bus-depot-eda] {msg}", flush=True)


def _nonempty(s: pd.Series) -> pd.Series:
    return s.notna() & s.astype(str).str.strip().ne("")


def _pct(num: float, den: float) -> float:
    return float(num) / float(den) * 100.0 if den else float("nan")


def _fmt_pct(value: float) -> str:
    return "n/a" if np.isnan(value) else f"{value:.1f}%"


def _read_endpoints(path: Path) -> tuple[pd.DataFrame, int, int]:
    cols = ["block_id", "agency_id", "start_h", "end_h", "start_lat", "start_lon", "end_lat", "end_lon"]
    df = pd.read_parquet(path, columns=cols).sort_values(
        ["block_id", "start_h", "end_h"],
        kind="stable",
    )
    first = df.groupby("block_id", sort=False).head(1)
    last = df.groupby("block_id", sort=False).tail(1)
    endpoints = first[["block_id", "agency_id", "start_lat", "start_lon"]].merge(
        last[["block_id", "end_lat", "end_lon"]],
        on="block_id",
        how="inner",
        validate="one_to_one",
    )
    coord_cols = ["start_lat", "start_lon", "end_lat", "end_lon"]
    for col in coord_cols:
        endpoints[col] = pd.to_numeric(endpoints[col], errors="coerce")
    valid = np.isfinite(endpoints[coord_cols].to_numpy(dtype=float)).all(axis=1)
    return endpoints.loc[valid].reset_index(drop=True), int(endpoints["block_id"].nunique()), int((~valid).sum())


def _resolve_lsoas(endpoints: pd.DataFrame) -> pd.DataFrame:
    out = endpoints.copy()
    n = len(out)
    lat = np.r_[out["start_lat"].to_numpy(float), out["end_lat"].to_numpy(float)]
    lon = np.r_[out["start_lon"].to_numpy(float), out["end_lon"].to_numpy(float)]
    valid = np.isfinite(lat) & np.isfinite(lon)
    codes = np.full(len(lat), "", dtype=object)
    methods = np.full(len(lat), "no_match", dtype=object)
    if valid.any():
        _log(f"loading LSOA boundary index for {int(valid.sum()):,} endpoint points")
        index = load_lsoa_boundary_index()
        _log("running polygon lookup")
        poly_codes, _, poly_methods = query_lsoa_polygons(lat[valid], lon[valid], index)
        target = np.flatnonzero(valid)
        codes[target] = poly_codes
        methods[target] = poly_methods
        fallback = valid & (methods == "no_match") & bool(index)
        if fallback.any():
            _log(f"running centroid fallback for {int(fallback.sum()):,} endpoint points")
            try:
                fallback_codes, _ = nearest_lsoa_for_points(
                    lat[fallback],
                    lon[fallback],
                    load_lsoa_centroids(),
                    max_distance_km=FALLBACK_MAX_KM,
                )
            except (FileNotFoundError, KeyError, ValueError, pd.errors.EmptyDataError):
                fallback_codes = np.full(int(fallback.sum()), "", dtype=object)
            fallback_target = np.flatnonzero(fallback)
            matched = fallback_codes != ""
            codes[fallback_target[matched]] = fallback_codes[matched]
    out["morning_lsoa"] = codes[:n]
    out["night_lsoa"] = codes[n:]
    return out


def _lsoa_table(endpoints: pd.DataFrame) -> pd.DataFrame:
    morning = endpoints.loc[_nonempty(endpoints["morning_lsoa"]), ["morning_lsoa", "block_id", "agency_id"]]
    morning = morning.rename(columns={"morning_lsoa": "lsoa_code"})
    night = endpoints.loc[_nonempty(endpoints["night_lsoa"]), ["night_lsoa", "block_id", "agency_id"]]
    night = night.rename(columns={"night_lsoa": "lsoa_code"})
    assoc = pd.concat([morning, night], ignore_index=True).drop_duplicates(["lsoa_code", "block_id"])
    assoc["agency_id"] = assoc["agency_id"].astype(str)

    agency_counts = (
        assoc.groupby(["lsoa_code", "agency_id"], as_index=False)["block_id"]
        .nunique()
        .rename(columns={"block_id": "n_blocks"})
        .sort_values(["lsoa_code", "n_blocks", "agency_id"], ascending=[True, False, True])
    )
    same = _nonempty(endpoints["morning_lsoa"]) & (endpoints["morning_lsoa"] == endpoints["night_lsoa"])
    table = pd.DataFrame(index=assoc.groupby("lsoa_code")["block_id"].nunique().index)
    table["n_blocks_morning"] = morning.groupby("lsoa_code")["block_id"].nunique()
    table["n_blocks_night"] = night.groupby("lsoa_code")["block_id"].nunique()
    table["n_blocks_total"] = assoc.groupby("lsoa_code")["block_id"].nunique()
    table["n_round_trip_blocks"] = endpoints.loc[same].groupby("morning_lsoa")["block_id"].nunique()
    table["n_agencies"] = assoc.groupby("lsoa_code")["agency_id"].nunique()
    table["primary_agency_id"] = agency_counts.groupby("lsoa_code", sort=False)["agency_id"].first()
    table["agencies_top3"] = agency_counts.groupby("lsoa_code", sort=False).head(3).groupby("lsoa_code")["agency_id"].agg("|".join)
    table = table.fillna({"primary_agency_id": "", "agencies_top3": ""}).fillna(0)
    ints = ["n_blocks_morning", "n_blocks_night", "n_blocks_total", "n_round_trip_blocks", "n_agencies"]
    table[ints] = table[ints].astype(int)
    table["round_trip_share"] = table["n_round_trip_blocks"] / table["n_blocks_total"]
    table["lad"] = ""
    first = table.index.to_series().str[0]
    table["country"] = first.where(first.isin(["E", "S", "W", "N"]), "").to_numpy()
    cols = ["lsoa_code", "n_blocks_morning", "n_blocks_night", "n_blocks_total", "n_round_trip_blocks", "round_trip_share", "n_agencies", "primary_agency_id", "agencies_top3", "lad", "country"]
    return table.reset_index(names="lsoa_code").loc[:, cols].sort_values(
        ["n_blocks_total", "lsoa_code"],
        ascending=[False, True],
        kind="stable",
    )


def _plot_cdf(per_lsoa: pd.DataFrame, block_max: pd.Series, n_blocks: int, path: Path) -> None:
    values = np.sort(per_lsoa["n_blocks_total"].to_numpy(int))
    x = np.unique(values[values > 0])
    fig, ax1 = plt.subplots(figsize=(9, 5))
    if len(x):
        lsoa_at_or_above = len(values) - np.searchsorted(values, x, side="left")
        block_values = np.sort(block_max[block_max > 0].to_numpy(int))
        blocks_covered = len(block_values) - np.searchsorted(block_values, x, side="left")
        ax1.step(x, lsoa_at_or_above, where="post", color="#1f77b4")
        ax2 = ax1.twinx()
        ax2.plot(x, blocks_covered / max(n_blocks, 1), color="#d62728")
        ax2.set_ylabel("Share of blocks covered")
        ax2.set_ylim(0, 1.02)
    ax1.set_xscale("log")
    ax1.set_xlabel("n_blocks_total threshold")
    ax1.set_ylabel("Number of candidate LSOAs")
    ax1.grid(True, which="both", axis="x", alpha=0.25)
    for threshold in THRESHOLDS:
        ax1.axvline(threshold, color="#555555", linestyle="--", linewidth=1, alpha=0.65)
        ax1.text(threshold, 0.98, str(threshold), transform=ax1.get_xaxis_transform(), ha="center", va="top")
    fig.tight_layout()
    fig.savefig(path, dpi=160)
    plt.close(fig)


def _is_london_lad(lad: str) -> bool:
    lad = str(lad)
    return lad.startswith("E090000") or "london" in lad.lower()


def run(blocks_path: Path, output_dir: Path) -> dict[str, object]:
    output_dir.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    _log(f"reading {blocks_path}")
    endpoints, n_read, n_dropped = _read_endpoints(blocks_path)
    _log(f"prepared {len(endpoints):,} coordinate-valid block endpoints")
    endpoints = _resolve_lsoas(endpoints)
    _log("aggregating per-LSOA table")
    per_lsoa = _lsoa_table(endpoints)
    per_lsoa.to_parquet(output_dir / "lsoa_block_count.parquet", index=False)

    counts = per_lsoa.set_index("lsoa_code")["n_blocks_total"]
    morning_counts = endpoints["morning_lsoa"].map(counts).fillna(0).astype(int)
    night_counts = endpoints["night_lsoa"].map(counts).fillna(0).astype(int)
    block_max = pd.Series(np.maximum(morning_counts, night_counts), index=endpoints.index)
    stats = {
        t: {
            "n_lsoas": int((per_lsoa["n_blocks_total"] >= t).sum()),
            "n_blocks_covered": int((block_max >= t).sum()),
            "coverage_pct": _pct(int((block_max >= t).sum()), len(endpoints)),
        }
        for t in THRESHOLDS
    }
    _plot_cdf(per_lsoa, block_max, len(endpoints), output_dir / "lsoa_block_count_cdf.png")

    lad = per_lsoa[["lsoa_code", "lad"]].copy()
    for t in THRESHOLDS:
        lad[f"n_lsoa_at_thresh_{t}"] = (per_lsoa["n_blocks_total"] >= t).astype(int)
    assoc = pd.concat(
        [
            endpoints.loc[_nonempty(endpoints["morning_lsoa"]), ["block_id", "morning_lsoa"]].rename(columns={"morning_lsoa": "lsoa_code"}),
            endpoints.loc[_nonempty(endpoints["night_lsoa"]), ["block_id", "night_lsoa"]].rename(columns={"night_lsoa": "lsoa_code"}),
        ],
        ignore_index=True,
    ).merge(per_lsoa[["lsoa_code", "lad"]], on="lsoa_code", how="left")
    lad_blocks = assoc.drop_duplicates(["lad", "block_id"]).groupby("lad", dropna=False)["block_id"].nunique()
    lad_summary = lad.drop(columns="lsoa_code").groupby("lad", dropna=False, as_index=False).sum()
    lad_summary = lad_summary.merge(lad_blocks.rename("n_blocks_total"), on="lad", how="left").fillna({"n_blocks_total": 0})
    lad_cols = ["lad", "n_lsoa_at_thresh_1", "n_lsoa_at_thresh_3", "n_lsoa_at_thresh_5", "n_lsoa_at_thresh_10", "n_blocks_total"]
    lad_summary = lad_summary[lad_cols]
    lad_summary = lad_summary.sort_values(["n_blocks_total", "lad"], ascending=[False, True], kind="stable")
    lad_summary.to_csv(output_dir / "n_depots_by_lad.csv", index=False)

    both = _nonempty(endpoints["morning_lsoa"]) & _nonempty(endpoints["night_lsoa"])
    rt = endpoints.loc[both].copy()
    rt["same"] = rt["morning_lsoa"] == rt["night_lsoa"]
    global_rt = pd.DataFrame([{"section": "global", "agency_id": "", "n_blocks": len(rt), "share": rt["same"].mean()}])
    agency_rt = (
        rt.groupby("agency_id", as_index=False)["same"]
        .agg(n_blocks="size", share="mean")
        .assign(section="agency")[["section", "agency_id", "n_blocks", "share"]]
        .sort_values(["share", "n_blocks", "agency_id"], ascending=[True, False, True], kind="stable")
    )
    round_trip = pd.concat([global_rt, agency_rt], ignore_index=True)
    round_trip.to_csv(output_dir / "morning_eq_night_share.csv", index=False)

    unmatched_rows = []
    for t in THRESHOLDS:
        unmatched = (morning_counts < t) & (night_counts < t)
        unmatched_rows.append(
            {
                "min_blocks_threshold": t,
                "n_blocks_unmatched": int(unmatched.sum()),
                "n_agencies_unmatched": int(endpoints.loc[unmatched, "agency_id"].nunique()),
            }
        )
    pd.DataFrame(unmatched_rows).to_csv(output_dir / "unmatched_blocks_summary.csv", index=False)

    recommended = next((t for t in reversed(THRESHOLDS) if stats[t]["coverage_pct"] >= 95.0), None)
    recommended = recommended or next((t for t in reversed(THRESHOLDS) if stats[t]["coverage_pct"] >= 90.0), 1)
    lsoa_bits = "; ".join(f"T={t}: {stats[t]['n_lsoas']:,} LSOAs, {_fmt_pct(stats[t]['coverage_pct'])} blocks" for t in THRESHOLDS)
    top_lads = "; ".join(
        f"{row.lad or '(no LAD lookup)'}: {int(row.n_blocks_total):,} blocks, {int(row.n_lsoa_at_thresh_5):,} LSOAs at T=5"
        for row in lad_summary.head(10).itertuples(index=False)
    ) or "No LAD rows were available."
    lad_available = per_lsoa["lad"].astype(str).str.strip().ne("").any()
    london_share = (
        _pct(((per_lsoa["n_blocks_total"] >= 5) & per_lsoa["lad"].map(_is_london_lad)).sum(), (per_lsoa["n_blocks_total"] >= 5).sum())
        if lad_available
        else float("nan")
    )
    outliers = round_trip.loc[(round_trip["section"] == "agency") & (round_trip["share"] < 0.5)].head(10)
    outlier_text = "; ".join(f"{r.agency_id}: {r.share:.2f} ({int(r.n_blocks):,} blocks)" for r in outliers.itertuples(index=False))
    outlier_text = outlier_text or "No agency with share < 0.5 among blocks with both LSOAs."
    global_share = float(round_trip.loc[round_trip["section"] == "global", "share"].iloc[0]) * 100.0
    rec_stats = stats[recommended]
    summary = f"""# Bus Depot EDA Summary

## Inputs
Read {n_read:,} distinct blocks from `outputs/all_blocks.parquet`. Dropped {n_dropped:,} blocks with NaN endpoint coordinates. Aggregated {len(endpoints):,} coordinate-valid blocks.

## LSOA distribution
Found {len(per_lsoa):,} candidate LSOAs from the morning/night union. Threshold coverage: {lsoa_bits}.

## Geographic spread
Top LAD rows by `n_blocks_total`: {top_lads}. London share at T=5 is {_fmt_pct(london_share)}. LAD lookup is unavailable in `mobility/`, so `lad` is blank and London dominance cannot be assessed from this PR-1 output.

## Round-trip share
Global `morning_lsoa == night_lsoa` share is {_fmt_pct(global_share)} across blocks with both LSOAs. Agency-level outliers: {outlier_text}.

## Recommended `MIN_BLOCKS_PER_DEPOT`
Recommend `{recommended}` because it keeps {_fmt_pct(rec_stats["coverage_pct"])} of coordinate-valid blocks while reducing the candidate LSOA set to {rec_stats["n_lsoas"]:,}.

## Caveats / follow-ups
Endpoint LSOA resolution left {(endpoints["morning_lsoa"] == "").sum():,} morning and {(endpoints["night_lsoa"] == "").sum():,} night endpoints unmatched after polygon plus {FALLBACK_MAX_KM} km centroid fallback. Add or approve a project LSOA->LAD helper before using the LAD/London diagnostics as a decision input.
"""
    (output_dir / "eda_summary.md").write_text(summary, encoding="utf-8")
    _log(f"wrote diagnostics to {output_dir} in {time.time() - t0:.1f}s")
    return {"per_lsoa": per_lsoa, "stats": stats, "recommended": recommended}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--blocks", type=Path, default=DEFAULT_BLOCKS)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    args = parser.parse_args(argv)
    run(args.blocks, args.output_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
