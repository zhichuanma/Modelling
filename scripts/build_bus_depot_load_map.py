# Build a self-contained interactive HTML map of bus depot charging load.
#
# Reads an annual depot-load run directory (depot_load_15min + depot_registry)
# and writes one HTML file: depots as circles on a Leaflet map (radius = annual
# charge, color = peak kW); clicking a depot shows its typical-day wall-clock
# charging curves (weekday vs weekend, 96 x 15-min slots) and headline stats.
#
# Honesty guards baked in (plan v2 §16):
# - Depots without resolvable coordinates (opdepot_*_missing) are NOT mapped;
#   they are listed in a side panel with their charge share so the map's
#   partial coverage is explicit.
# - Warm-up service dates (is_warmup=True) are excluded from the curves.
# - Wall-clock curves aggregate by slot_start_datetime (summing across
#   service_dates), the audited-correct way to build depot profiles.

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import numpy as np
import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build the interactive depot charging-load map.")
    parser.add_argument("--run-dir", type=Path, default=Path("~/Work/Nature_EV_2025/outputs/bus_annual_depot_load_carryover"))
    parser.add_argument("--out-html", type=Path, default=None, help="Default: <run-dir>/depot_load_map.html")
    parser.add_argument("--include-warmup", action="store_true", help="Keep is_warmup=True rows (excluded by default).")
    parser.add_argument("--min-annual-kwh", type=float, default=1.0, help="Drop mapped depots below this annual charge.")
    parser.add_argument("--title", default="Bus depot charging load — annual carryover run", help="HTML page title (e.g. for the coach run).")
    return parser.parse_args()


def _resolve(path: Path) -> Path:
    return Path(os.path.expandvars(os.path.expanduser(str(path)))).resolve()


def load_depot_load(run_dir: Path) -> pd.DataFrame:
    dataset = run_dir / "depot_load_15min"
    if dataset.is_dir():
        frame = pd.read_parquet(dataset)
        if "service_date" not in frame.columns:  # hive partition column may come back as category
            raise RuntimeError("depot_load_15min partitions lack service_date.")
    else:
        frame = pd.read_parquet(run_dir / "depot_load_15min.parquet")
    frame["service_date"] = frame["service_date"].astype(str)
    return frame


def main() -> None:
    args = parse_args()
    run_dir = _resolve(args.run_dir)
    out_html = _resolve(args.out_html) if args.out_html else run_dir / "depot_load_map.html"

    print(f"[depot_map] reading depot load from {run_dir}", flush=True)
    load = load_depot_load(run_dir)
    n_rows_total = len(load)
    if not args.include_warmup and "is_warmup" in load.columns:
        load = load.loc[~load["is_warmup"].fillna(False).astype(bool)].copy()
    print(f"[depot_map] {n_rows_total:,} load rows, {len(load):,} after warmup filter", flush=True)

    registry = pd.read_parquet(run_dir / "depot_registry.parquet")
    confidence_by_depot = registry.drop_duplicates("depot_id").set_index("depot_id")["depot_confidence"].astype(str) if "depot_confidence" in registry.columns else pd.Series(dtype=str)

    # Unknown-depot isolation (must not be mapped spatially).
    depot_id = load["depot_id"].astype(str)
    lat = pd.to_numeric(load["depot_lat"], errors="coerce")
    lon = pd.to_numeric(load["depot_lon"], errors="coerce")
    lsoa = load["depot_lsoa"].fillna("").astype(str).str.strip().str.lower()
    unknown_mask = depot_id.str.endswith("_missing") | lsoa.isin(("", "missing")) | ~np.isfinite(lat) | ~np.isfinite(lon)

    total_kwh = float(load["charge_kwh"].sum())
    unknown = load.loc[unknown_mask]
    unknown_kwh = float(unknown["charge_kwh"].sum())
    unknown_top = (
        unknown.groupby("depot_id", as_index=False)["charge_kwh"].sum().sort_values("charge_kwh", ascending=False).head(50)
    )

    mapped = load.loc[~unknown_mask].copy()
    print(f"[depot_map] mapped charge share: {(total_kwh - unknown_kwh) / total_kwh:.4f} (unknown-depot share {unknown_kwh / total_kwh:.4f})", flush=True)

    # Wall-clock 15-min profile: sum across service_dates per slot_start_datetime
    # (audited-correct; overnight tails land on the calendar day they occur).
    slot_start = pd.to_datetime(mapped["slot_start_datetime"])
    mapped["wall_date"] = slot_start.dt.date
    mapped["wall_slot"] = slot_start.dt.hour * 4 + slot_start.dt.minute // 15
    mapped["is_weekend"] = slot_start.dt.dayofweek >= 5

    per_depot = mapped.groupby("depot_id", sort=True)
    summary = per_depot.agg(
        annual_charge_kwh=("charge_kwh", "sum"),
        peak_kw=("average_kw", "max"),
        lat=("depot_lat", "first"),
        lon=("depot_lon", "first"),
        lsoa=("depot_lsoa", "first"),
        n_service_dates=("service_date", "nunique"),
    ).reset_index()
    summary = summary.loc[summary["annual_charge_kwh"] >= float(args.min_annual_kwh)]
    keep = set(summary["depot_id"])
    mapped = mapped.loc[mapped["depot_id"].isin(keep)]
    print(f"[depot_map] {len(summary):,} mapped depots (>= {args.min_annual_kwh} kWh/yr)", flush=True)

    # Per wall-clock slot: total kW that slot-day, then mean over days by daytype.
    slot_day = mapped.groupby(["depot_id", "wall_date", "wall_slot", "is_weekend"], as_index=False, sort=False).agg(kw=("average_kw", "sum"))
    n_days = mapped.groupby(["depot_id", "is_weekend"])["wall_date"].nunique().rename("n_days")
    curve_sum = slot_day.groupby(["depot_id", "is_weekend", "wall_slot"])["kw"].sum()
    curves: dict[str, dict[str, list[float]]] = {}
    for (depot, weekend), n in n_days.items():
        key = "weekend" if weekend else "weekday"
        values = np.zeros(96)
        if (depot, weekend) in curve_sum.index.droplevel("wall_slot"):
            sub = curve_sum.loc[(depot, weekend)]
            values[sub.index.to_numpy()] = sub.to_numpy() / max(1, int(n))
        curves.setdefault(depot, {})[key] = [round(float(v), 2) for v in values]

    depots_payload = []
    for row in summary.itertuples(index=False):
        depots_payload.append(
            {
                "id": row.depot_id,
                "lat": round(float(row.lat), 5),
                "lon": round(float(row.lon), 5),
                "lsoa": str(row.lsoa),
                "kwh": round(float(row.annual_charge_kwh), 1),
                "peak": round(float(row.peak_kw), 1),
                "days": int(row.n_service_dates),
                "conf": str(confidence_by_depot.get(row.depot_id, "")),
                "wd": curves.get(row.depot_id, {}).get("weekday", [0.0] * 96),
                "we": curves.get(row.depot_id, {}).get("weekend", [0.0] * 96),
            }
        )

    meta = {
        "title": str(args.title),
        "run_dir": str(run_dir),
        "total_charge_kwh": round(total_kwh, 1),
        "mapped_charge_kwh": round(total_kwh - unknown_kwh, 1),
        "unknown_charge_kwh": round(unknown_kwh, 1),
        "unknown_share": round(unknown_kwh / total_kwh, 4) if total_kwh else 0.0,
        "n_mapped_depots": len(depots_payload),
        "warmup_excluded": not args.include_warmup,
        "unknown_top": [
            {"id": str(row.depot_id), "kwh": round(float(row.charge_kwh), 1)} for row in unknown_top.itertuples(index=False)
        ],
    }

    html = _render_html(depots_payload, meta)
    out_html.parent.mkdir(parents=True, exist_ok=True)
    out_html.write_text(html, encoding="utf-8")
    print(f"[depot_map] wrote {out_html} ({out_html.stat().st_size / 1e6:.1f} MB)", flush=True)


def _render_html(depots: list[dict], meta: dict) -> str:
    data_json = json.dumps({"depots": depots, "meta": meta}, separators=(",", ":"))
    html = (
        """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<title>__PAGE_TITLE__</title>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<style>
  html, body { margin:0; height:100%; font-family: system-ui, sans-serif; }
  #map { position:absolute; inset:0 320px 0 0; }
  #side { position:absolute; right:0; top:0; bottom:0; width:320px; overflow-y:auto;
          background:#fafafa; border-left:1px solid #ddd; padding:10px 14px; box-sizing:border-box; font-size:13px; }
  #side h2 { font-size:15px; margin:8px 0 4px; }
  #side table { width:100%; border-collapse:collapse; font-size:11px; }
  #side td { padding:1px 4px; border-bottom:1px solid #eee; }
  .caveat { background:#fff3cd; border:1px solid #ffe69c; padding:6px 8px; border-radius:4px; margin:6px 0; }
  .popup-chart { width:420px; }
  .legend { background:white; padding:6px 10px; border-radius:4px; box-shadow:0 1px 4px rgba(0,0,0,.3); font-size:12px; }
</style>
</head>
<body>
<div id="map"></div>
<div id="side">
  <h2>Depot charging load</h2>
  <div id="meta"></div>
  <div class="caveat" id="caveat"></div>
  <h2>Unmapped depots (top 50 by charge)</h2>
  <div style="font-size:11px;color:#666">opdepot_*_missing: no resolvable coordinates — never map these spatially.</div>
  <table id="unknown"></table>
</div>
<script>
const DATA = """
        + data_json
        + """;
const meta = DATA.meta;
const fmt = (x) => x.toLocaleString(undefined, {maximumFractionDigits: 0});
document.getElementById('meta').innerHTML =
  `<b>${fmt(meta.n_mapped_depots)}</b> mapped depots · total <b>${fmt(meta.total_charge_kwh/1000)}</b> MWh` +
  (meta.warmup_excluded ? ' · warm-up excluded' : '');
document.getElementById('caveat').innerHTML =
  `Map covers <b>${((1-meta.unknown_share)*100).toFixed(1)}%</b> of charge; ` +
  `<b>${(meta.unknown_share*100).toFixed(1)}%</b> (${fmt(meta.unknown_charge_kwh/1000)} MWh) sits at depots with ` +
  `unknown coordinates (listed right). Depot anchors are inferred, not verified garages.`;
document.getElementById('unknown').innerHTML =
  DATA.meta.unknown_top.map(u => `<tr><td>${u.id}</td><td style="text-align:right">${fmt(u.kwh)} kWh</td></tr>`).join('');

const map = L.map('map').setView([54.5, -2.5], 6);
L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png',
  {attribution: '&copy; OpenStreetMap contributors', maxZoom: 18}).addTo(map);

const peaks = DATA.depots.map(d => d.peak).sort((a,b)=>a-b);
const q = (p) => peaks[Math.min(peaks.length-1, Math.floor(p*peaks.length))] || 1;
const colorFor = (peak) => peak >= q(0.95) ? '#b30000' : peak >= q(0.75) ? '#e34a33' : peak >= q(0.5) ? '#fc8d59' : '#fdcc8a';
const kwhMax = Math.max(...DATA.depots.map(d => d.kwh));
const radiusFor = (kwh) => 3 + 14 * Math.sqrt(kwh / kwhMax);

function chartSVG(d) {
  const W=420, H=150, P=28;
  const maxKw = Math.max(1, ...d.wd, ...d.we);
  const x = (i) => P + (W-P-6) * i / 95;
  const y = (v) => H-18 - (H-30) * v / maxKw;
  const path = (arr) => arr.map((v,i) => (i? 'L':'M') + x(i).toFixed(1) + ',' + y(v).toFixed(1)).join('');
  const ticks = [0,24,48,72,95].map(i =>
    `<text x="${x(i)}" y="${H-4}" font-size="9" text-anchor="middle" fill="#666">${String(Math.floor(i/4)).padStart(2,'0')}:00</text>`).join('');
  return `<svg width="${W}" height="${H}">
    <text x="2" y="10" font-size="9" fill="#666">${maxKw.toFixed(0)} kW</text>
    <path d="${path(d.wd)}" fill="none" stroke="#1f78b4" stroke-width="1.6"/>
    <path d="${path(d.we)}" fill="none" stroke="#ff7f00" stroke-width="1.6" stroke-dasharray="4 3"/>
    ${ticks}
    <text x="${W-120}" y="10" font-size="10" fill="#1f78b4">— weekday</text>
    <text x="${W-50}" y="10" font-size="10" fill="#ff7f00">- - weekend</text>
  </svg>`;
}

DATA.depots.forEach(d => {
  const marker = L.circleMarker([d.lat, d.lon], {
    radius: radiusFor(d.kwh), color: colorFor(d.peak), weight: 1,
    fillColor: colorFor(d.peak), fillOpacity: 0.55,
  }).addTo(map);
  marker.bindPopup(() =>
    `<div class="popup-chart"><b>${d.id}</b> · LSOA ${d.lsoa} · confidence ${d.conf}<br/>` +
    `annual <b>${fmt(d.kwh)}</b> kWh · peak <b>${fmt(d.peak)}</b> kW · ${d.days} service days<br/>` +
    `<div style="margin-top:4px">${chartSVG(d)}</div>` +
    `<div style="font-size:10px;color:#666">Typical-day wall-clock profile (mean kW per 15-min slot), warm-up excluded.</div></div>`,
    {maxWidth: 460});
});

const legend = L.control({position:'bottomleft'});
legend.onAdd = () => {
  const div = L.DomUtil.create('div','legend');
  div.innerHTML = '<b>Circle</b>: size = annual kWh, color = peak kW quartile<br/>' +
    '<span style="color:#b30000">&#9679;</span> top 5% &nbsp; <span style="color:#e34a33">&#9679;</span> top 25% &nbsp;' +
    '<span style="color:#fc8d59">&#9679;</span> top 50% &nbsp; <span style="color:#fdcc8a">&#9679;</span> rest';
  return div;
};
legend.addTo(map);
</script>
</body>
</html>
"""
    )
    return html.replace("__PAGE_TITLE__", str(meta.get("title", "Depot charging load")))


if __name__ == "__main__":
    main()
