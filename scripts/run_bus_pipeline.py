"""M1 chain-mode bus simulation pipeline runner.

This script builds the fixed depot / vehicle / charger registries, expands
dated block instances, assigns vehicles with time-space-only greedy logic, and
runs the SOC resolution cascade for every chain.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
import datetime as dt
from pathlib import Path
import sys
import time

import pandas as pd


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from mobility.bus.block_instances import build_block_instances_from_templates, build_block_templates  # noqa: E402
from mobility.bus.calendar import FEED_YEAR_END, FEED_YEAR_START, build_service_date_index, load_service_calendar  # noqa: E402
from mobility.bus.charger_registry import build_charger_registry  # noqa: E402
from mobility.bus.chain_resolver import SimulationError, build_resolution_summary, resolve_chain  # noqa: E402
from mobility.bus.depot_registry import build_depot_registry  # noqa: E402
from mobility.bus.event_ledger import MOVEMENT_EVENTS, build_event_ledger  # noqa: E402
from mobility.bus.txc_parser import DEFAULT_TXC_DIR, parse_txc_garages  # noqa: E402
from mobility.bus.vehicle_assignment import assign_vehicles_greedy  # noqa: E402
from mobility.bus.vehicle_inventory import DEFAULT_EV_LSOA_PATH, bridge_ev_lsoa_to_fleet, load_ev_lsoa_inventory  # noqa: E402
from mobility.core.spatial import load_lsoa_boundary_index  # noqa: E402


DEFAULT_BLOCKS = REPO_ROOT / "outputs" / "all_blocks.parquet"
DEFAULT_GTFS_DIR = REPO_ROOT.parent / "Data" / "EV_behavior" / "Bus_Data" / "GTFS_timetable"
DEFAULT_OUTPUT_DIR = REPO_ROOT / "outputs"
DEFAULT_OLD_PER_BLOCK = REPO_ROOT / "outputs" / "bus_annual_per_block.parquet"
RESOLUTION_SUMMARY_COLUMNS = [
    "service_date",
    "chain_id",
    "depot_id",
    "original_vehicle_id",
    "final_vehicle_id",
    "vehicle_upgraded",
    "resolution_level",
    "modifications_str",
    "min_soc_kwh_l0",
    "min_soc_kwh_l1",
    "min_soc_kwh_l2",
    "min_soc_kwh_l3",
    "min_soc_kwh_l4",
    "opportunity_charge_kwh",
    "mid_day_return_kwh",
    "n_stations_used",
    "station_ids_used_csv",
    "had_synthetic_overflow_vehicle",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--blocks", type=Path, default=DEFAULT_BLOCKS)
    parser.add_argument("--gtfs-dir", type=Path, default=DEFAULT_GTFS_DIR)
    parser.add_argument("--txc-dir", type=Path, default=DEFAULT_TXC_DIR)
    parser.add_argument("--ev-lsoa", type=Path, default=DEFAULT_EV_LSOA_PATH)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--start-date", default=FEED_YEAR_START.isoformat())
    parser.add_argument("--end-date", default=FEED_YEAR_END.isoformat())
    parser.add_argument("--limit-blocks", type=int, default=0)
    parser.add_argument("--skip-txc", action="store_true")
    parser.add_argument("--max-chains-resolve", type=int, default=0)
    parser.add_argument("--progress-interval", type=int, default=50000)
    parser.add_argument("--old-per-block", type=Path, default=DEFAULT_OLD_PER_BLOCK)
    parser.add_argument("--chunk-days", type=int, default=14)
    return parser.parse_args()


def _coerce_date(value: str | dt.date | pd.Timestamp) -> dt.date:
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    if isinstance(value, pd.Timestamp):
        return value.date()
    return dt.date.fromisoformat(str(value))


def _date_windows(
    start_date: str | dt.date | pd.Timestamp,
    end_date: str | dt.date | pd.Timestamp,
    chunk_days: int,
) -> list[tuple[dt.date, dt.date]]:
    start = _coerce_date(start_date)
    end = _coerce_date(end_date)
    if end < start:
        raise ValueError("end_date must be on or after start_date.")
    days = max(1, int(chunk_days))
    windows: list[tuple[dt.date, dt.date]] = []
    cursor = start
    while cursor <= end:
        window_end = min(end, cursor + dt.timedelta(days=days - 1))
        windows.append((cursor, window_end))
        cursor = window_end + dt.timedelta(days=1)
    return windows


class _ParquetAppendWriter:
    """Append DataFrame chunks to one parquet file without holding all rows."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._writer = None
        self._schema = None
        self.rows = 0

    def write(self, frame: pd.DataFrame) -> None:
        if frame is None or frame.empty:
            return
        import pyarrow as pa
        import pyarrow.parquet as pq

        table = pa.Table.from_pandas(frame.reset_index(drop=True), preserve_index=False)
        if self._writer is None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            if self.path.exists():
                try:
                    self.path.unlink()
                except PermissionError:
                    fallback = self.path.with_name(
                        f"{self.path.stem}.{time.strftime('%Y%m%d%H%M%S')}{self.path.suffix}"
                    )
                    print(
                        f"[m1] output file is locked, writing {fallback} instead of {self.path}",
                        flush=True,
                    )
                    self.path = fallback
            self._schema = table.schema
            self._writer = pq.ParquetWriter(self.path, self._schema)
        elif self._schema is not None and not table.schema.equals(self._schema, check_metadata=False):
            table = table.cast(self._schema)
        self._writer.write_table(table)
        self.rows += len(frame)

    def write_empty(self, frame: pd.DataFrame) -> None:
        if self.rows == 0 and not self.path.exists():
            self.path.parent.mkdir(parents=True, exist_ok=True)
            frame.to_parquet(self.path, index=False)

    def close(self) -> None:
        if self._writer is not None:
            self._writer.close()
            self._writer = None


def _add_count_map(target: dict[str, int], values: pd.Series) -> None:
    for key, value in values.items():
        target[str(key)] = target.get(str(key), 0) + int(value)


def _add_sum_map(target: dict[str, float], values: pd.Series) -> None:
    for key, value in values.items():
        target[str(key)] = target.get(str(key), 0.0) + float(value)


@dataclass
class _PipelineStats:
    block_instances: int = 0
    assigned_instances: int = 0
    chains_resolved: int = 0
    passenger_km_instances: float = 0.0
    passenger_km_events: float = 0.0
    inter_deadhead_km: float = 0.0
    depot_deadhead_km: float = 0.0
    movement_energy_kwh: float = 0.0
    greedy_synthetic_overflow: int = 0
    cascade_synthetic_overflow: int = 0
    l0_resolutions: int = 0
    l1_resolutions: int = 0
    opportunity_charge_kwh: float = 0.0
    mid_day_return_kwh: float = 0.0
    capacity_violations: int = 0
    resolved_bounded: bool = True
    active_instances_by_block: dict[str, int] = field(default_factory=dict)
    passenger_km_by_agency: dict[str, float] = field(default_factory=dict)
    station_ids_used: set[str] = field(default_factory=set)

    def add_block_instances(self, block_instances: pd.DataFrame) -> None:
        if block_instances.empty:
            return
        self.block_instances += len(block_instances)
        self.passenger_km_instances += float(block_instances["passenger_distance_km"].sum())
        _add_count_map(self.active_instances_by_block, block_instances.groupby("block_id", sort=False).size())
        _add_sum_map(
            self.passenger_km_by_agency,
            block_instances.groupby("agency_id", sort=False)["passenger_distance_km"].sum(),
        )

    def add_assignments(self, assignments: pd.DataFrame) -> None:
        if assignments.empty:
            return
        self.assigned_instances += len(assignments)
        self.greedy_synthetic_overflow += int(
            assignments["vehicle_provenance"].astype(str).eq("synthetic_overflow").sum()
        )
        chain_counts = assignments[
            ["service_date", "depot_id", "chain_id", "vehicle_id", "vehicle_provenance"]
        ].drop_duplicates()
        capacity = chain_counts.groupby(["service_date", "depot_id"], sort=False)["vehicle_id"].nunique()
        chain_count = chain_counts.groupby(["service_date", "depot_id"], sort=False)["chain_id"].nunique()
        self.capacity_violations += int((chain_count > capacity).sum()) if not chain_count.empty else 0

    def add_events(self, events: pd.DataFrame) -> None:
        if events.empty:
            return
        event_type = events["event_type"].astype(str)
        self.passenger_km_events += float(events.loc[event_type.eq("passenger_block"), "distance_km"].sum())
        self.inter_deadhead_km += float(events.loc[event_type.eq("inter_block_deadhead"), "distance_km"].sum())
        depot_deadhead_types = {"depot_deadhead", "return_deadhead", "midday_return_deadhead", "midday_out_deadhead"}
        self.depot_deadhead_km += float(events.loc[event_type.isin(depot_deadhead_types), "distance_km"].sum())
        self.movement_energy_kwh += float(events["energy_kwh_proxy"].sum())

    def add_resolution_summary(self, summary: pd.DataFrame) -> None:
        if summary.empty:
            return
        self.chains_resolved += len(summary)
        levels = summary["resolution_level"]
        self.l0_resolutions += int(levels.eq(0).sum())
        self.l1_resolutions += int(levels.eq(1).sum())
        self.resolved_bounded = self.resolved_bounded and bool(levels.between(0, 4).all())
        self.cascade_synthetic_overflow += int(summary["had_synthetic_overflow_vehicle"].sum())
        self.opportunity_charge_kwh += float(summary["opportunity_charge_kwh"].sum())
        self.mid_day_return_kwh += float(summary["mid_day_return_kwh"].sum())
        for csv_value in summary.get("station_ids_used_csv", pd.Series(dtype=object)).fillna("").astype(str):
            self.station_ids_used.update(station for station in csv_value.split(",") if station)


def _read_gtfs_tables(gtfs_dir: Path) -> tuple[pd.DataFrame, pd.DataFrame]:
    agency = pd.read_csv(gtfs_dir / "agency.txt")
    stops = pd.read_csv(gtfs_dir / "stops.txt")
    return agency, stops


def _limit_blocks(blocks: pd.DataFrame, limit_blocks: int) -> pd.DataFrame:
    if limit_blocks <= 0:
        return blocks
    keep_ids = list(dict.fromkeys(blocks["block_id"].astype(str).tolist()))[: int(limit_blocks)]
    return blocks[blocks["block_id"].astype(str).isin(set(keep_ids))].copy()


def _chain_vehicle(chain_events: pd.DataFrame) -> pd.Series:
    first = chain_events.sort_values("event_seq", kind="stable").iloc[0]
    return first[
        [
            "vehicle_id",
            "battery_kwh",
            "consumption_kwh_per_km",
            "ac_charge_kw_max",
            "dc_charge_kw_max",
            "usable_soc_min",
            "usable_soc_max",
            "vehicle_provenance",
        ]
    ].copy()


def _fast_l0_summary_row(chain_events: pd.DataFrame) -> dict | None:
    """Return a resolution-summary row when the chain is plainly L0 feasible."""
    if chain_events is None or chain_events.empty:
        return None
    events = chain_events.sort_values("event_seq", kind="stable")
    first = events.iloc[0]
    battery_kwh = float(first.get("battery_kwh", 300.0) or 300.0)
    usable_soc_max = float(first.get("usable_soc_max", 0.95) or 0.95)
    max_soc_kwh = battery_kwh * usable_soc_max
    event_type = events["event_type"].astype(str)
    movement_energy = pd.to_numeric(events["energy_kwh_proxy"], errors="coerce").fillna(0.0).where(
        event_type.isin(MOVEMENT_EVENTS),
        0.0,
    )
    min_soc_l0 = float(max_soc_kwh - movement_energy.cumsum().max())
    if min_soc_l0 < -1e-9:
        return None
    vehicle_id = str(first.get("vehicle_id", ""))
    return {
        "service_date": str(first.get("service_date", "")),
        "chain_id": str(first.get("chain_id", "")),
        "depot_id": str(first.get("depot_id", "")),
        "original_vehicle_id": vehicle_id,
        "final_vehicle_id": vehicle_id,
        "vehicle_upgraded": False,
        "resolution_level": 0,
        "modifications_str": "",
        "min_soc_kwh_l0": min_soc_l0,
        "min_soc_kwh_l1": float("nan"),
        "min_soc_kwh_l2": float("nan"),
        "min_soc_kwh_l3": float("nan"),
        "min_soc_kwh_l4": float("nan"),
        "opportunity_charge_kwh": 0.0,
        "mid_day_return_kwh": 0.0,
        "n_stations_used": 0,
        "station_ids_used_csv": "",
        "had_synthetic_overflow_vehicle": str(first.get("vehicle_provenance", "")) == "synthetic_overflow",
    }


def _depot_pool_for_chain(
    vehicles: pd.DataFrame,
    assignments: pd.DataFrame,
    chain_id: str,
    service_date: str,
    depot_id: str,
) -> pd.DataFrame:
    pool = vehicles[vehicles["depot_id"].astype(str).eq(str(depot_id))].copy()
    if pool.empty:
        return pool
    assigned_other = assignments[
        assignments["service_date"].astype(str).eq(str(service_date))
        & assignments["chain_id"].astype(str).ne(str(chain_id))
    ]["vehicle_id"].astype(str)
    used = set(assigned_other)
    pool["assigned_chain_id"] = pool["vehicle_id"].astype(str).map(lambda vehicle_id: "other" if vehicle_id in used else "")
    return pool


def _vehicle_pools_by_depot(vehicles: pd.DataFrame) -> dict[str, pd.DataFrame]:
    if vehicles is None or vehicles.empty or "depot_id" not in vehicles.columns:
        return {}
    return {
        str(depot_id): group.copy().reset_index(drop=True)
        for depot_id, group in vehicles.dropna(subset=["depot_id"]).groupby("depot_id", sort=False)
    }


def _assigned_vehicle_sets(assignments: pd.DataFrame) -> dict[tuple[str, str], set[str]]:
    if assignments is None or assignments.empty:
        return {}
    groups = assignments.groupby(["service_date", "depot_id"], sort=False)["vehicle_id"]
    return {
        (str(service_date), str(depot_id)): set(group.dropna().astype(str))
        for (service_date, depot_id), group in groups
    }


def _depot_pool_for_chain_cached(
    vehicle_pools: dict[str, pd.DataFrame],
    assigned_sets: dict[tuple[str, str], set[str]],
    *,
    service_date: str,
    depot_id: str,
    current_vehicle_id: str,
) -> pd.DataFrame:
    pool = vehicle_pools.get(str(depot_id), pd.DataFrame()).copy()
    if pool.empty:
        return pool
    used = set(assigned_sets.get((str(service_date), str(depot_id)), set()))
    used.discard(str(current_vehicle_id))
    pool["assigned_chain_id"] = pool["vehicle_id"].astype(str).map(lambda vehicle_id: "other" if vehicle_id in used else "")
    return pool


def _write_unresolvable_chain(
    output_dir: Path,
    chain_id: str,
    chain_events: pd.DataFrame,
) -> Path:
    diagnostic_dir = Path(output_dir) / "diagnostics" / "unresolvable_chains"
    diagnostic_dir.mkdir(parents=True, exist_ok=True)
    path = diagnostic_dir / f"{chain_id}.parquet"
    chain_events.to_parquet(path, index=False)
    return path


def _legacy_per_block(path: Path | None) -> pd.DataFrame:
    if path is None or not Path(path).exists():
        return pd.DataFrame()
    legacy = pd.read_parquet(path)
    if "block_id" not in legacy.columns:
        legacy = legacy.reset_index().rename(columns={legacy.index.name or "index": "block_id"})
    legacy["block_id"] = legacy["block_id"].astype(str)
    return legacy


def _scaled_legacy_window(
    legacy: pd.DataFrame,
    block_instances: pd.DataFrame,
) -> pd.DataFrame:
    if legacy.empty or block_instances.empty:
        return pd.DataFrame()
    counts = block_instances.groupby("block_id", sort=False).size().rename("m1_active_instances")
    out = legacy.merge(counts, left_on="block_id", right_index=True, how="inner")
    if out.empty:
        return out
    if "n_active_dates" in out.columns:
        active_days_raw = out["n_active_dates"]
    elif "active_days" in out.columns:
        active_days_raw = out["active_days"]
    else:
        active_days_raw = pd.Series(0, index=out.index)
    active_days = pd.to_numeric(active_days_raw, errors="coerce").replace(0, pd.NA)
    scale = pd.to_numeric(out["m1_active_instances"], errors="coerce") / active_days
    for col in ("annual_distance_km", "deadhead_total_km", "annual_energy_kwh"):
        if col in out.columns:
            out[f"m1_window_{col}"] = pd.to_numeric(out[col], errors="coerce") * scale
    if {"m1_window_annual_distance_km", "m1_window_deadhead_total_km"}.issubset(out.columns):
        out["m1_window_passenger_km_proxy"] = (
            out["m1_window_annual_distance_km"] - out["m1_window_deadhead_total_km"]
        )
    return out


def _scaled_legacy_from_counts(
    legacy: pd.DataFrame,
    active_instances_by_block: dict[str, int],
) -> pd.DataFrame:
    if legacy.empty or not active_instances_by_block:
        return pd.DataFrame()
    counts = pd.Series(active_instances_by_block, name="m1_active_instances")
    counts.index = counts.index.astype(str)
    out = legacy.merge(counts, left_on="block_id", right_index=True, how="inner")
    if out.empty:
        return out
    if "n_active_dates" in out.columns:
        active_days_raw = out["n_active_dates"]
    elif "active_days" in out.columns:
        active_days_raw = out["active_days"]
    else:
        active_days_raw = pd.Series(0, index=out.index)
    active_days = pd.to_numeric(active_days_raw, errors="coerce").replace(0, pd.NA)
    scale = pd.to_numeric(out["m1_active_instances"], errors="coerce") / active_days
    for col in ("annual_distance_km", "deadhead_total_km", "annual_energy_kwh"):
        if col in out.columns:
            out[f"m1_window_{col}"] = pd.to_numeric(out[col], errors="coerce") * scale
    if {"m1_window_annual_distance_km", "m1_window_deadhead_total_km"}.issubset(out.columns):
        out["m1_window_passenger_km_proxy"] = (
            out["m1_window_annual_distance_km"] - out["m1_window_deadhead_total_km"]
        )
    return out


def _pct_delta(new_value: float, old_value: float) -> float:
    if old_value == 0.0:
        return 0.0 if new_value == 0.0 else float("inf")
    return (float(new_value) - float(old_value)) / float(old_value) * 100.0


def _write_reconciliation_report(
    path: Path,
    *,
    blocks: pd.DataFrame,
    block_instances: pd.DataFrame,
    assignments: pd.DataFrame,
    events: pd.DataFrame,
    summary: pd.DataFrame,
    charger_registry: pd.DataFrame,
    old_per_block_path: Path | None,
    l5_errors: int = 0,
) -> None:
    passenger_km_templates = float(blocks["distance_km"].sum()) if "distance_km" in blocks.columns else 0.0
    passenger_km_instances = (
        float(block_instances["passenger_distance_km"].sum())
        if "passenger_distance_km" in block_instances.columns
        else 0.0
    )
    passenger_km_events = float(events.loc[events["event_type"].eq("passenger_block"), "distance_km"].sum()) if not events.empty else 0.0
    overflow_share = float(assignments["vehicle_provenance"].eq("synthetic_overflow").mean()) if not assignments.empty else 0.0
    l0_share = float(summary["resolution_level"].eq(0).mean()) if not summary.empty else 0.0
    l1_share = float(summary["resolution_level"].eq(1).mean()) if not summary.empty else 0.0
    l0_l1_share = l0_share + l1_share
    cascade_overflow_share = (
        float(summary["had_synthetic_overflow_vehicle"].mean())
        if not summary.empty and "had_synthetic_overflow_vehicle" in summary.columns
        else 0.0
    )
    opportunity_charge_kwh = (
        float(summary["opportunity_charge_kwh"].sum())
        if not summary.empty and "opportunity_charge_kwh" in summary.columns
        else 0.0
    )
    mid_day_return_kwh = (
        float(summary["mid_day_return_kwh"].sum())
        if not summary.empty and "mid_day_return_kwh" in summary.columns
        else 0.0
    )
    inter_deadhead_km = float(events.loc[events["event_type"].eq("inter_block_deadhead"), "distance_km"].sum()) if not events.empty else 0.0
    depot_deadhead_types = {"depot_deadhead", "return_deadhead", "midday_return_deadhead", "midday_out_deadhead"}
    depot_deadhead_km = float(events.loc[events["event_type"].isin(depot_deadhead_types), "distance_km"].sum()) if not events.empty else 0.0
    total_energy_kwh = float(events["energy_kwh_proxy"].sum()) if not events.empty else 0.0
    chain_counts = assignments[["service_date", "depot_id", "chain_id", "vehicle_id", "vehicle_provenance"]].drop_duplicates()
    capacity = chain_counts.groupby(["service_date", "depot_id"], sort=False)["vehicle_id"].nunique()
    chain_count = chain_counts.groupby(["service_date", "depot_id"], sort=False)["chain_id"].nunique()
    capacity_violations = int((chain_count > capacity).sum()) if not chain_count.empty else 0
    resolved_bounded = bool(summary["resolution_level"].between(0, 4).all()) if not summary.empty else True

    station_ids = {
        station
        for csv_value in summary.get("station_ids_used_csv", pd.Series(dtype=object)).fillna("").astype(str)
        for station in csv_value.split(",")
        if station
    }
    charger_lsoa = pd.Series(dtype=object)
    if not charger_registry.empty and station_ids:
        charger_lsoa = (
            charger_registry[charger_registry["station_id"].astype(str).isin(station_ids)]
            .get("lsoa_code", pd.Series(dtype=object))
            .dropna()
            .astype(str)
        )
    distinct_charge_lsoas = int(charger_lsoa[charger_lsoa.ne("")].nunique()) if not charger_lsoa.empty else 0

    legacy = _legacy_per_block(old_per_block_path)
    legacy_window = _scaled_legacy_window(legacy, block_instances)
    legacy_lines: list[str] = []
    agency_lines: list[str] = []
    if legacy_window.empty:
        legacy_status = f"skipped; legacy parquet not found or no overlapping block IDs ({old_per_block_path})"
    else:
        old_passenger = float(legacy_window.get("m1_window_passenger_km_proxy", pd.Series(dtype=float)).sum())
        old_deadhead = float(legacy_window.get("m1_window_deadhead_total_km", pd.Series(dtype=float)).sum())
        old_energy = float(legacy_window.get("m1_window_annual_energy_kwh", pd.Series(dtype=float)).sum())
        legacy_status = "available"
        legacy_lines.extend(
            [
                f"| Legacy-scaled passenger km | {old_passenger:,.3f} |",
                f"| Passenger km delta vs legacy-scaled | {_pct_delta(passenger_km_events, old_passenger):,.3f}% |",
                f"| Legacy-scaled intra-block deadhead km | {old_deadhead:,.3f} |",
                f"| New inter-chain deadhead km | {inter_deadhead_km:,.3f} |",
                f"| Inter/intra deadhead delta vs legacy-scaled | {_pct_delta(inter_deadhead_km, old_deadhead):,.3f}% |",
                f"| New depot-anchored deadhead km | {depot_deadhead_km:,.3f} |",
                f"| Legacy-scaled total energy kWh | {old_energy:,.3f} |",
                f"| New total movement energy kWh | {total_energy_kwh:,.3f} |",
                f"| Energy delta vs legacy-scaled | {_pct_delta(total_energy_kwh, old_energy):,.3f}% |",
            ]
        )
        if {"agency_id", "m1_window_passenger_km_proxy"}.issubset(legacy_window.columns):
            new_by_agency = (
                block_instances.groupby("agency_id", sort=False)["passenger_distance_km"]
                .sum()
                .rename("new_passenger_km")
            )
            old_by_agency = (
                legacy_window.groupby("agency_id", sort=False)["m1_window_passenger_km_proxy"]
                .sum()
                .rename("legacy_passenger_km")
            )
            agency_compare = pd.concat([new_by_agency, old_by_agency], axis=1).fillna(0.0)
            agency_compare["delta_pct"] = [
                _pct_delta(row.new_passenger_km, row.legacy_passenger_km)
                for row in agency_compare.itertuples()
            ]
            finite_delta = pd.to_numeric(agency_compare["delta_pct"], errors="coerce").abs()
            finite_delta = finite_delta[finite_delta.ne(float("inf"))]
            max_agency_delta = float(finite_delta.max(skipna=True) or 0.0)
            agency_lines.append(f"| Max passenger km delta by agency | {max_agency_delta:,.3f}% |")

    threshold_lines: list[str] = []
    if l0_l1_share < 0.70:
        threshold_lines.append(
            f"| M1 threshold note: L0+L1 share | Below 70% target by {(0.70 - l0_l1_share):.3%}; inspect resolution levels and depot/charger coverage. |"
        )
    if overflow_share > 0.05:
        threshold_lines.append(
            f"| M1 threshold note: greedy synthetic overflow | Above 5% target by {(overflow_share - 0.05):.3%}; vehicle inventory coverage is the likely driver. |"
        )
    if cascade_overflow_share > 0.05:
        threshold_lines.append(
            f"| M1 threshold note: any synthetic overflow vehicle | Above 5% target by {(cascade_overflow_share - 0.05):.3%}; includes greedy overflow and L4 SOC synthetic resolution. |"
        )
    if stats.opportunity_charge_kwh <= 0.0:
        threshold_lines.append(
            "| M1 threshold note: opportunity charge kWh | Non-positive; inspect station_kind propagation and L1 eligibility. |"
        )

    lines = [
        "# M1 Reconciliation Report",
        "",
        "| Check | Value |",
        "|---|---:|",
        f"| Block templates | {blocks['block_id'].nunique():,} |",
        f"| Block instances | {len(block_instances):,} |",
        f"| Assigned instances | {len(assignments):,} |",
        f"| Chains resolved | {len(summary):,} |",
        f"| Passenger km in selected block templates | {passenger_km_templates:,.3f} |",
        f"| Passenger km in active block instances | {passenger_km_instances:,.3f} |",
        f"| Passenger km in event ledger | {passenger_km_events:,.3f} |",
        f"| Passenger km exact active-instance match | {str(abs(passenger_km_events - passenger_km_instances) < 1e-6)} |",
        f"| Inter-chain deadhead km | {inter_deadhead_km:,.3f} |",
        f"| Depot-anchored deadhead km | {depot_deadhead_km:,.3f} |",
        f"| Movement energy proxy kWh | {total_energy_kwh:,.3f} |",
        f"| Distinct chain capacity violations | {capacity_violations:,} |",
        f"| Resolution levels bounded 0..4 | {str(resolved_bounded)} |",
        f"| L0 resolution share | {l0_share:.3%} |",
        f"| L1 resolution share | {l1_share:.3%} |",
        f"| L0+L1 resolution share | {l0_l1_share:.3%} |",
        f"| Greedy synthetic overflow share | {overflow_share:.3%} |",
        f"| Any synthetic overflow vehicle share | {cascade_overflow_share:.3%} |",
        f"| Opportunity charge kWh | {opportunity_charge_kwh:,.3f} |",
        f"| Mid-day return charge kWh | {mid_day_return_kwh:,.3f} |",
        f"| L5 hard errors | {int(l5_errors):,} |",
        f"| Distinct LSOAs receiving charge | {distinct_charge_lsoas:,} |",
        f"| Legacy comparison status | {legacy_status} |",
        *legacy_lines,
        *agency_lines,
        *threshold_lines,
        "",
        "The old per-block pipeline excludes depot-to-first-stop and last-stop-to-depot legs; those are reported only in the M1 event ledger.",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")


def _write_reconciliation_report_from_stats(
    path: Path,
    *,
    blocks: pd.DataFrame,
    stats: _PipelineStats,
    charger_registry: pd.DataFrame,
    old_per_block_path: Path | None,
    l5_errors: int = 0,
) -> None:
    passenger_km_templates = float(blocks["distance_km"].sum()) if "distance_km" in blocks.columns else 0.0
    l0_share = stats.l0_resolutions / stats.chains_resolved if stats.chains_resolved else 0.0
    l1_share = stats.l1_resolutions / stats.chains_resolved if stats.chains_resolved else 0.0
    l0_l1_share = l0_share + l1_share
    overflow_share = (
        stats.greedy_synthetic_overflow / stats.assigned_instances
        if stats.assigned_instances
        else 0.0
    )
    cascade_overflow_share = (
        stats.cascade_synthetic_overflow / stats.chains_resolved
        if stats.chains_resolved
        else 0.0
    )

    charger_lsoa = pd.Series(dtype=object)
    if not charger_registry.empty and stats.station_ids_used:
        charger_lsoa = (
            charger_registry[charger_registry["station_id"].astype(str).isin(stats.station_ids_used)]
            .get("lsoa_code", pd.Series(dtype=object))
            .dropna()
            .astype(str)
        )
    distinct_charge_lsoas = int(charger_lsoa[charger_lsoa.ne("")].nunique()) if not charger_lsoa.empty else 0

    legacy = _legacy_per_block(old_per_block_path)
    legacy_window = _scaled_legacy_from_counts(legacy, stats.active_instances_by_block)
    legacy_lines: list[str] = []
    agency_lines: list[str] = []
    if legacy_window.empty:
        legacy_status = f"skipped; legacy parquet not found or no overlapping block IDs ({old_per_block_path})"
    else:
        old_passenger = float(legacy_window.get("m1_window_passenger_km_proxy", pd.Series(dtype=float)).sum())
        old_deadhead = float(legacy_window.get("m1_window_deadhead_total_km", pd.Series(dtype=float)).sum())
        old_energy = float(legacy_window.get("m1_window_annual_energy_kwh", pd.Series(dtype=float)).sum())
        legacy_status = "available"
        legacy_lines.extend(
            [
                f"| Legacy-scaled passenger km | {old_passenger:,.3f} |",
                f"| Passenger km delta vs legacy-scaled | {_pct_delta(stats.passenger_km_events, old_passenger):,.3f}% |",
                f"| Legacy-scaled intra-block deadhead km | {old_deadhead:,.3f} |",
                f"| New inter-chain deadhead km | {stats.inter_deadhead_km:,.3f} |",
                f"| Inter/intra deadhead delta vs legacy-scaled | {_pct_delta(stats.inter_deadhead_km, old_deadhead):,.3f}% |",
                f"| New depot-anchored deadhead km | {stats.depot_deadhead_km:,.3f} |",
                f"| Legacy-scaled total energy kWh | {old_energy:,.3f} |",
                f"| New total movement energy kWh | {stats.movement_energy_kwh:,.3f} |",
                f"| Energy delta vs legacy-scaled | {_pct_delta(stats.movement_energy_kwh, old_energy):,.3f}% |",
            ]
        )
        if {"agency_id", "m1_window_passenger_km_proxy"}.issubset(legacy_window.columns):
            new_by_agency = pd.Series(stats.passenger_km_by_agency, name="new_passenger_km")
            old_by_agency = (
                legacy_window.groupby("agency_id", sort=False)["m1_window_passenger_km_proxy"]
                .sum()
                .rename("legacy_passenger_km")
            )
            agency_compare = pd.concat([new_by_agency, old_by_agency], axis=1).fillna(0.0)
            agency_compare["delta_pct"] = [
                _pct_delta(row.new_passenger_km, row.legacy_passenger_km)
                for row in agency_compare.itertuples()
            ]
            finite_delta = pd.to_numeric(agency_compare["delta_pct"], errors="coerce").abs()
            finite_delta = finite_delta[finite_delta.ne(float("inf"))]
            max_agency_delta = float(finite_delta.max(skipna=True) or 0.0)
            agency_lines.append(f"| Max passenger km delta by agency | {max_agency_delta:,.3f}% |")

    lines = [
        "# M1 Reconciliation Report",
        "",
        "| Check | Value |",
        "|---|---:|",
        f"| Block templates | {blocks['block_id'].nunique():,} |",
        f"| Block instances | {stats.block_instances:,} |",
        f"| Assigned instances | {stats.assigned_instances:,} |",
        f"| Chains resolved | {stats.chains_resolved:,} |",
        f"| Passenger km in selected block templates | {passenger_km_templates:,.3f} |",
        f"| Passenger km in active block instances | {stats.passenger_km_instances:,.3f} |",
        f"| Passenger km in event ledger | {stats.passenger_km_events:,.3f} |",
        f"| Passenger km exact active-instance match | {str(abs(stats.passenger_km_events - stats.passenger_km_instances) < 1e-6)} |",
        f"| Inter-chain deadhead km | {stats.inter_deadhead_km:,.3f} |",
        f"| Depot-anchored deadhead km | {stats.depot_deadhead_km:,.3f} |",
        f"| Movement energy proxy kWh | {stats.movement_energy_kwh:,.3f} |",
        f"| Distinct chain capacity violations | {stats.capacity_violations:,} |",
        f"| Resolution levels bounded 0..4 | {str(stats.resolved_bounded)} |",
        f"| L0 resolution share | {l0_share:.3%} |",
        f"| L1 resolution share | {l1_share:.3%} |",
        f"| L0+L1 resolution share | {l0_l1_share:.3%} |",
        f"| Greedy synthetic overflow share | {overflow_share:.3%} |",
        f"| Any synthetic overflow vehicle share | {cascade_overflow_share:.3%} |",
        f"| Opportunity charge kWh | {stats.opportunity_charge_kwh:,.3f} |",
        f"| Mid-day return charge kWh | {stats.mid_day_return_kwh:,.3f} |",
        f"| L5 hard errors | {int(l5_errors):,} |",
        f"| Distinct LSOAs receiving charge | {distinct_charge_lsoas:,} |",
        f"| Legacy comparison status | {legacy_status} |",
        *legacy_lines,
        *agency_lines,
        "",
        "The old per-block pipeline excludes depot-to-first-stop and last-stop-to-depot legs; those are reported only in the M1 event ledger.",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")


def run_pipeline(args: argparse.Namespace) -> dict:
    t0 = time.time()
    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    chunk_days = max(1, int(getattr(args, "chunk_days", 14)))

    print("[m1] reading blocks / GTFS tables", flush=True)
    blocks = pd.read_parquet(args.blocks)
    blocks = _limit_blocks(blocks, int(args.limit_blocks))
    agency, stops = _read_gtfs_tables(args.gtfs_dir)
    block_templates = build_block_templates(blocks)

    print("[m1] loading LSOA boundary index", flush=True)
    lsoa_index = load_lsoa_boundary_index()

    print("[m1] parsing TxC garages", flush=True)
    txc_garages = pd.DataFrame() if args.skip_txc else parse_txc_garages(args.txc_dir)
    depot_registry = build_depot_registry(blocks, agency, stops, lsoa_index, txc_garages)
    depot_registry.to_parquet(out_dir / "depot_registry.parquet", index=False)

    print("[m1] bridging EV-LSOA buses to depots", flush=True)
    ev_lsoa = load_ev_lsoa_inventory(args.ev_lsoa)
    vehicles = bridge_ev_lsoa_to_fleet(ev_lsoa, depot_registry)
    if not depot_registry.empty and not vehicles.empty:
        counts = vehicles.dropna(subset=["depot_id"]).groupby("depot_id").size()
        depot_registry["n_candidate_vehicles"] = depot_registry["depot_id"].map(counts).fillna(0).astype(int)
        depot_registry.to_parquet(out_dir / "depot_registry.parquet", index=False)
    vehicles.to_parquet(out_dir / "vehicles.parquet", index=False)

    print("[m1] building charger registry", flush=True)
    charger_registry = build_charger_registry(depot_registry, vehicles=vehicles)
    charger_registry.to_parquet(out_dir / "charger_registry.parquet", index=False)

    print("[m1] preparing service calendar", flush=True)
    calendar = load_service_calendar(args.gtfs_dir)
    service_index = build_service_date_index(
        blocks["service_id"].astype(str).unique(),
        start_date=args.start_date,
        end_date=args.end_date,
        calendar=calendar,
    )

    block_writer = _ParquetAppendWriter(out_dir / "block_instances.parquet")
    assignment_writer = _ParquetAppendWriter(out_dir / "vehicle_assignments.parquet")
    event_writer = _ParquetAppendWriter(out_dir / "vehicle_day_events.parquet")
    resolution_writer = _ParquetAppendWriter(out_dir / "resolution_summary.parquet")
    writers = [block_writer, assignment_writer, event_writer, resolution_writer]
    stats = _PipelineStats()
    l5_errors = 0
    resolved_total = 0
    max_chains = int(getattr(args, "max_chains_resolve", 0))
    windows = _date_windows(args.start_date, args.end_date, chunk_days)
    vehicle_pools = _vehicle_pools_by_depot(vehicles)
    print(f"[m1] processing {len(windows):,} date chunk(s) of up to {chunk_days:,} day(s)", flush=True)
    try:
        for chunk_index, (chunk_start, chunk_end) in enumerate(windows, start=1):
            label = f"{chunk_start.isoformat()}..{chunk_end.isoformat()}"
            print(f"[m1] chunk {chunk_index:,}/{len(windows):,}: {label} expanding block instances", flush=True)
            step_t = time.time()
            block_instances = build_block_instances_from_templates(
                block_templates,
                service_index,
                start_date=chunk_start,
                end_date=chunk_end,
                calendar=calendar,
            )
            if block_instances.empty:
                continue
            print(f"[m1] chunk {chunk_index:,}: expanded {len(block_instances):,} block instances in {time.time() - step_t:,.1f}s", flush=True)
            block_writer.write(block_instances)
            stats.add_block_instances(block_instances)

            print(f"[m1] chunk {chunk_index:,}/{len(windows):,}: assigning vehicles", flush=True)
            step_t = time.time()
            assignments = assign_vehicles_greedy(block_instances, vehicles, depot_registry)
            print(f"[m1] chunk {chunk_index:,}: assigned {len(assignments):,} instances in {time.time() - step_t:,.1f}s", flush=True)
            assignment_writer.write(assignments)
            stats.add_assignments(assignments)
            assigned_sets = _assigned_vehicle_sets(assignments)

            print(f"[m1] chunk {chunk_index:,}/{len(windows):,}: building vehicle-day event ledger", flush=True)
            step_t = time.time()
            events = build_event_ledger(assignments, block_instances, depot_registry, stops)
            print(f"[m1] chunk {chunk_index:,}: built {len(events):,} events in {time.time() - step_t:,.1f}s", flush=True)
            event_writer.write(events)
            stats.add_events(events)

            print(f"[m1] chunk {chunk_index:,}/{len(windows):,}: resolving chains", flush=True)
            step_t = time.time()
            resolutions: list[dict] = []
            fast_summary_rows: list[dict] = []
            chunk_chain_total = events["chain_id"].nunique() if not events.empty else 0
            for _, (chain_id, chain_events) in enumerate(events.groupby("chain_id", sort=True), start=1):
                if max_chains > 0 and resolved_total >= max_chains:
                    break
                fast_row = _fast_l0_summary_row(chain_events)
                if fast_row is not None:
                    fast_summary_rows.append(fast_row)
                    resolved_total += 1
                    if args.progress_interval > 0 and (
                        resolved_total % args.progress_interval == 0
                        or resolved_total == chunk_chain_total
                        or (max_chains > 0 and resolved_total >= max_chains)
                    ):
                        print(f"  resolved chains {resolved_total:,}", flush=True)
                    continue
                first = chain_events.iloc[0]
                depot_pool = _depot_pool_for_chain_cached(
                    vehicle_pools,
                    assigned_sets,
                    service_date=str(first["service_date"]),
                    depot_id=str(first["depot_id"]),
                    current_vehicle_id=str(first["vehicle_id"]),
                )
                try:
                    resolutions.append(
                        resolve_chain(
                            chain_events,
                            _chain_vehicle(chain_events),
                            depot_pool,
                            charger_registry,
                            str(first["depot_id"]),
                        )
                    )
                except SimulationError as exc:
                    l5_errors += 1
                    diagnostic_events = exc.chain_events if exc.chain_events is not None else chain_events
                    diagnostic_path = _write_unresolvable_chain(out_dir, str(chain_id), diagnostic_events)
                    raise SimulationError(
                        f"{exc} Diagnostic written to {diagnostic_path}",
                        chain_events=diagnostic_events,
                    ) from exc
                resolved_total += 1
                if args.progress_interval > 0 and (
                    resolved_total % args.progress_interval == 0
                    or resolved_total == chunk_chain_total
                    or (max_chains > 0 and resolved_total >= max_chains)
                ):
                    print(f"  resolved chains {resolved_total:,}", flush=True)

            slow_summary = build_resolution_summary(resolutions, assignments)
            resolution_summary = pd.concat(
                [
                    pd.DataFrame(fast_summary_rows, columns=RESOLUTION_SUMMARY_COLUMNS),
                    slow_summary.loc[:, RESOLUTION_SUMMARY_COLUMNS] if not slow_summary.empty else slow_summary,
                ],
                ignore_index=True,
            )
            print(f"[m1] chunk {chunk_index:,}: resolved {len(resolution_summary):,} chains in {time.time() - step_t:,.1f}s", flush=True)
            resolution_writer.write(resolution_summary)
            stats.add_resolution_summary(resolution_summary)
    finally:
        for writer in writers:
            writer.close()

    block_writer.write_empty(pd.DataFrame(columns=[]))
    assignment_writer.write_empty(pd.DataFrame(columns=[]))
    event_writer.write_empty(pd.DataFrame(columns=[]))
    resolution_writer.write_empty(pd.DataFrame(columns=[]))
    _write_reconciliation_report_from_stats(
        out_dir / "m1_reconciliation_report.md",
        blocks=blocks,
        stats=stats,
        charger_registry=charger_registry,
        old_per_block_path=args.old_per_block,
        l5_errors=l5_errors,
    )
    return {
        "runtime_s": time.time() - t0,
        "depot_registry": str(out_dir / "depot_registry.parquet"),
        "charger_registry": str(out_dir / "charger_registry.parquet"),
        "vehicles": str(out_dir / "vehicles.parquet"),
        "block_instances": str(block_writer.path),
        "vehicle_assignments": str(assignment_writer.path),
        "vehicle_day_events": str(event_writer.path),
        "resolution_summary": str(resolution_writer.path),
        "m1_reconciliation_report": str(out_dir / "m1_reconciliation_report.md"),
    }


def main() -> None:
    summary = run_pipeline(parse_args())
    print("\n=== M1 outputs ===")
    for key, value in summary.items():
        if isinstance(value, float):
            print(f"{key}: {value:,.1f}")
        else:
            print(f"{key}: {value}")


if __name__ == "__main__":
    main()
