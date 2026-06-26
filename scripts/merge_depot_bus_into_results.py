# Merge bus depot charging fragments into the Web app's per-day results JSON.
#
# Run this ON THE MACHINE THAT OWNS THE LIVE FILES (the Mac), e.g.:
#   python3 merge_depot_bus_into_results.py \
#     --bundle ~/Downloads/web_export_depot_bus \
#     --web-public "/Users/zm348/Library/CloudStorage/OneDrive-UniversityofExeter/Projects/Nature_EV_2025/Web/public"
#
# For every YYYY-MM-DD.json in <web-public>/data/results and
# <web-public>/data/results_with_connectors it injects two top-level keys:
#   "depots":        {depot_id: [48 half-hour avg kW]}   ({} on days without bus data)
#   "depots_system": {"load_kw": [48], "n_active_depots": N, "source_date": "..."}
# and copies depot_bus_index.json + Depots.csv into <web-public>/data/.
#
# Safety: atomic writes (tmp + rename), OneDrive conflict copies (e.g.
# "...-MacBook Pro (4).json") are skipped, re-running is idempotent.
# Requires only the Python standard library.

from __future__ import annotations

import argparse
import json
import re
import shutil
from pathlib import Path

DATE_FILE = re.compile(r"^\d{4}-\d{2}-\d{2}\.json$")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Merge depot(bus/coach) fragments into per-day results JSON.")
    parser.add_argument("--bundle", type=Path, required=True, help="Directory containing the fragments subdir, index JSON, depots CSV")
    parser.add_argument("--web-public", type=Path, required=True, help="Path to Web/public")
    parser.add_argument("--results-dirs", nargs="*", default=["data/results", "data/results_with_connectors"])
    parser.add_argument("--dry-run", action="store_true")
    # Mode parameterization; defaults = bus layout/keys. Coach must use distinct
    # injection keys (e.g. depots_coach) so it cannot overwrite the bus data.
    parser.add_argument("--fragments-subdir", default="depot_bus_fragments")
    parser.add_argument("--index-name", default="depot_bus_index.json")
    parser.add_argument("--depots-csv-name", default="Depots.csv")
    parser.add_argument("--depots-key", default="depots", help="Top-level key injected into each results day file.")
    parser.add_argument("--system-key", default="depots_system", help="Top-level system-curve key injected alongside --depots-key.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    bundle = args.bundle.expanduser().resolve()
    web_public = args.web_public.expanduser().resolve()
    fragments_dir = bundle / args.fragments_subdir
    if not fragments_dir.is_dir():
        raise SystemExit(f"fragments dir not found: {fragments_dir}")

    fragments: dict[str, dict] = {}
    for path in sorted(fragments_dir.glob("*.json")):
        fragment = json.loads(path.read_text(encoding="utf-8"))
        fragments[str(fragment["target_date"])] = fragment
    print(f"[merge] loaded {len(fragments)} fragments from {fragments_dir}")

    for results_rel in args.results_dirs:
        results_dir = web_public / results_rel
        if not results_dir.is_dir():
            print(f"[merge] SKIP missing dir: {results_dir}")
            continue
        day_files = [p for p in sorted(results_dir.iterdir()) if DATE_FILE.match(p.name)]
        merged = with_data = 0
        for path in day_files:
            date = path.name[:-5]
            fragment = fragments.get(date)
            payload = json.loads(path.read_text(encoding="utf-8"))
            if fragment is not None:
                payload[args.depots_key] = fragment["depots"]
                payload[args.system_key] = {**fragment["depots_system"], "source_date": fragment["source_date"]}
                with_data += 1
            else:
                payload[args.depots_key] = {}
                payload[args.system_key] = {"load_kw": [0.0] * 48, "n_active_depots": 0, "source_date": None}
            if not args.dry_run:
                tmp = path.with_suffix(".json.tmp")
                tmp.write_text(json.dumps(payload, ensure_ascii=True, separators=(",", ":")), encoding="utf-8")
                tmp.replace(path)
            merged += 1
        print(f"[merge] {results_rel}: {merged} day files updated ({with_data} with depot data, {merged - with_data} empty)")

    if not args.dry_run:
        for name in (args.index_name, args.depots_csv_name):
            source = bundle / name
            if source.exists():
                shutil.copy2(source, web_public / "data" / name)
                print(f"[merge] copied {name} -> {web_public / 'data'}")
    print("[merge] done" + (" (dry run, nothing written)" if args.dry_run else ""))


if __name__ == "__main__":
    main()
