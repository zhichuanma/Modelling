"""Vehicle-day assignment for ev_stock_scale annual bus depot-load runs."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd


RNG_SEED = 20260603
# Must match annual_depot_events deadhead speed so the carry-over temporal
# guard compares against the same deadhead start the event ledger will use.
DEADHEAD_SPEED_KMH = 30.0


def stable_daily_seed(seed: int, service_date: str) -> int:
    digest = hashlib.sha1(f"{int(seed)}:{service_date}".encode("utf-8")).hexdigest()[:16]
    return int(digest, 16) % (2**32)


def _distance_bin(distance_km: float) -> str:
    value = float(distance_km)
    if value < 50:
        return "lt_50km"
    if value < 100:
        return "50_100km"
    if value < 200:
        return "100_200km"
    return "ge_200km"


def _duration_bin(duration_h: float) -> str:
    value = float(duration_h)
    if value < 4:
        return "lt_4h"
    if value < 8:
        return "4_8h"
    if value < 12:
        return "8_12h"
    return "ge_12h"


def build_vehicle_day_assignments(
    block_instances: pd.DataFrame,
    ev_bus_specs: pd.DataFrame,
    *,
    seed: int = RNG_SEED,
    scenario_mode: str = "ev_stock_scale",
    max_vehicle_days: int | None = None,
) -> pd.DataFrame:
    """Assign one EV spec to at most one sampled active block per service date."""
    if scenario_mode != "ev_stock_scale":
        raise NotImplementedError("Only scenario_mode='ev_stock_scale' is implemented.")
    if block_instances.empty or ev_bus_specs.empty:
        return pd.DataFrame(columns=_assignment_columns())
    required_instances = {"service_date", "block_instance_id", "block_template_id", "agency_id", "service_id", "block_id", "depot_id", "region_key"}
    required_specs = {"vehicle_spec_id"}
    missing = sorted(required_instances - set(block_instances.columns))
    if missing:
        raise ValueError(f"block_instances is missing required columns: {missing}")
    missing_specs = sorted(required_specs - set(ev_bus_specs.columns))
    if missing_specs:
        raise ValueError(f"ev_bus_specs is missing required columns: {missing_specs}")

    specs = ev_bus_specs.sort_values("vehicle_spec_id", kind="stable").reset_index(drop=True)
    records: list[dict[str, Any]] = []
    for service_date, day_blocks in block_instances.groupby("service_date", sort=True):
        day_blocks = day_blocks.copy().sort_values(["region_key", "passenger_distance_km", "duration_h", "block_instance_id"], kind="stable")
        rng = np.random.default_rng(stable_daily_seed(seed, str(service_date)))
        n_assign = min(len(specs), len(day_blocks))
        if n_assign <= 0:
            continue
        n_available = int(len(day_blocks))
        n_unassigned = int(n_available - n_assign)
        coverage_share = float(n_assign / n_available) if n_available else np.nan
        block_positions = rng.choice(np.arange(len(day_blocks)), size=n_assign, replace=False)
        spec_positions = rng.permutation(np.arange(len(specs)))[:n_assign]
        sampled_blocks = day_blocks.iloc[block_positions].reset_index(drop=True)
        sampled_specs = specs.iloc[spec_positions].reset_index(drop=True)
        for idx, (block_row, spec_row) in enumerate(zip(sampled_blocks.itertuples(index=False), sampled_specs.itertuples(index=False))):
            distance = float(getattr(block_row, "passenger_distance_km", 0.0))
            duration = float(getattr(block_row, "duration_h", 0.0))
            vehicle_day_id = f"vd_{service_date}_{idx:05d}_{getattr(spec_row, 'vehicle_spec_id')}"
            records.append(
                {
                    "service_date": str(service_date),
                    "vehicle_day_id": vehicle_day_id,
                    "vehicle_spec_id": str(getattr(spec_row, "vehicle_spec_id")),
                    "block_instance_id": str(getattr(block_row, "block_instance_id")),
                    "block_template_id": str(getattr(block_row, "block_template_id")),
                    "agency_id": str(getattr(block_row, "agency_id")),
                    "service_id": str(getattr(block_row, "service_id")),
                    "block_id": str(getattr(block_row, "block_id")),
                    "depot_id": str(getattr(block_row, "depot_id", "")),
                    "region_key": str(getattr(block_row, "region_key", "unknown")),
                    "distance_bin": _distance_bin(distance),
                    "duration_bin": _duration_bin(duration),
                    "assignment_method": "ev_stock_scale_random_representative_duty",
                    "scenario_mode": scenario_mode,
                    "sample_weight": 1.0,
                    "assignment_seed": stable_daily_seed(seed, str(service_date)),
                    "n_available_block_instances_for_service_date": n_available,
                    "n_assigned_block_instances_for_service_date": int(n_assign),
                    "n_unassigned_block_instances_for_service_date": n_unassigned,
                    "daily_assignment_coverage_share": coverage_share,
                }
            )
        if max_vehicle_days is not None and len(records) >= int(max_vehicle_days):
            break

    assignments = pd.DataFrame.from_records(records, columns=_assignment_columns())
    if max_vehicle_days is not None and len(assignments) > int(max_vehicle_days):
        assignments = assignments.iloc[: int(max_vehicle_days)].copy()
    return assignments.reset_index(drop=True)


def _assignment_columns() -> list[str]:
    return [
        "service_date",
        "vehicle_day_id",
        "vehicle_spec_id",
        "block_instance_id",
        "block_template_id",
        "agency_id",
        "service_id",
        "block_id",
        "depot_id",
        "region_key",
        "distance_bin",
        "duration_bin",
        "assignment_method",
        "scenario_mode",
        "sample_weight",
        "assignment_seed",
        "n_available_block_instances_for_service_date",
        "n_assigned_block_instances_for_service_date",
        "n_unassigned_block_instances_for_service_date",
        "daily_assignment_coverage_share",
    ]


FEASIBLE_ASSIGNMENT_METHOD = "sample_then_feasible_match"
FEASIBLE_HOME_DEPOT_ASSIGNMENT_METHOD = "sample_then_feasible_match_home_depot"
_EARTH_RADIUS_KM = 6371.0088  # must match annual_depot_events.haversine_km


def _haversine_km_vec(lat1: np.ndarray, lon1: np.ndarray, lat2: np.ndarray, lon2: np.ndarray) -> np.ndarray:
    """Vectorized haversine matching annual_depot_events.haversine_km (x 1.0, NaN-propagating)."""
    phi1 = np.radians(lat1)
    phi2 = np.radians(lat2)
    d_phi = np.radians(lat2 - lat1)
    d_lam = np.radians(lon2 - lon1)
    a = np.sin(d_phi / 2.0) ** 2 + np.cos(phi1) * np.cos(phi2) * np.sin(d_lam / 2.0) ** 2
    return 2.0 * _EARTH_RADIUS_KM * np.arcsin(np.minimum(1.0, np.sqrt(a)))


def _spec_range_km(specs: pd.DataFrame) -> np.ndarray:
    """Maximum distance each spec can drive within its usable SOC window (daily-reset mode).

    Invalid parameters yield NaN, which never satisfies the feasibility comparison.
    """
    battery = pd.to_numeric(specs["battery_kwh"], errors="coerce").to_numpy(dtype=float)
    soc_min = pd.to_numeric(specs["usable_soc_min"], errors="coerce").to_numpy(dtype=float)
    soc_max = pd.to_numeric(specs["usable_soc_max"], errors="coerce").to_numpy(dtype=float)
    consumption = pd.to_numeric(specs["consumption_kwh_per_km"], errors="coerce").to_numpy(dtype=float)
    available_kwh = battery * (soc_max - soc_min)
    with np.errstate(divide="ignore", invalid="ignore"):
        range_km = np.where(
            np.isfinite(available_kwh) & np.isfinite(consumption) & (consumption > 0.0) & (available_kwh > 0.0),
            available_kwh / consumption,
            np.nan,
        )
    return range_km


def _block_supply_weights(
    day_blocks: pd.DataFrame,
    home_depot_ids: np.ndarray,
    home_lat: np.ndarray,
    home_lon: np.ndarray,
    valid_group_sizes: np.ndarray,
    radius_km: float,
) -> np.ndarray:
    """Per-block weight = number of home-depot vehicles admitting the block's depot."""
    block_depot_ids = day_blocks["depot_id"].astype(str).to_numpy()
    unique_depots, first_index, block_depot_index = np.unique(block_depot_ids, return_index=True, return_inverse=True)
    depot_lat = _coord_column(day_blocks, "depot_lat")[first_index]
    depot_lon = _coord_column(day_blocks, "depot_lon")[first_index]
    depot_distance = _haversine_km_vec(home_lat[:, None], home_lon[:, None], depot_lat[None, :], depot_lon[None, :])
    in_radius = home_depot_ids[:, None] == unique_depots[None, :]
    in_radius |= np.nan_to_num(depot_distance, nan=np.inf) <= radius_km
    unique_weights = valid_group_sizes @ in_radius.astype(np.int64)
    return unique_weights[block_depot_index].astype(float)


def _coord_column(frame: pd.DataFrame, name: str) -> np.ndarray:
    if name in frame.columns:
        return pd.to_numeric(frame[name], errors="coerce").to_numpy(dtype=float)
    return np.full(len(frame), np.nan, dtype=float)


def _block_deadhead_km(day_blocks: pd.DataFrame) -> tuple[np.ndarray, np.ndarray]:
    """Per-block deadhead estimate: attached depot <-> block start/end, haversine x 1.0.

    Missing coordinates contribute 0 km and set the incomplete flag, matching the
    event-stage behavior so the feasibility screen and SOC walk stay consistent.
    """
    n = len(day_blocks)
    coord_cols = ("depot_lat", "depot_lon", "start_lat", "start_lon", "end_lat", "end_lon")
    if n == 0 or not all(col in day_blocks.columns for col in coord_cols):
        return np.zeros(n, dtype=float), np.ones(n, dtype=bool)
    values = {col: pd.to_numeric(day_blocks[col], errors="coerce").to_numpy(dtype=float) for col in coord_cols}
    to_block = _haversine_km_vec(values["depot_lat"], values["depot_lon"], values["start_lat"], values["start_lon"])
    from_block = _haversine_km_vec(values["end_lat"], values["end_lon"], values["depot_lat"], values["depot_lon"])
    incomplete = ~np.isfinite(to_block) | ~np.isfinite(from_block)
    deadhead = np.nan_to_num(to_block, nan=0.0) + np.nan_to_num(from_block, nan=0.0)
    return deadhead, incomplete


def _max_bipartite_match(feasible: np.ndarray, rng: np.random.Generator) -> np.ndarray:
    """Seeded maximum-cardinality bipartite matching on a vehicle x block edge mask.

    Returns the matched vehicle index per block (-1 unmatched). Hopcroft-Karp is
    deterministic for a fixed matrix, so both sides are relabelled with seeded
    permutations to avoid a systematic bias toward low-index vehicles/blocks when
    several maximum matchings exist.
    """
    try:
        from scipy.sparse import csr_matrix
        from scipy.sparse.csgraph import maximum_bipartite_matching
    except ImportError as exc:  # pragma: no cover - environment guard
        raise ImportError(
            "Home-depot constrained matching requires scipy "
            "(install with: python -m pip install --user scipy)."
        ) from exc
    n_vehicles, n_blocks = feasible.shape
    match = np.full(n_blocks, -1, dtype=int)
    if n_vehicles == 0 or n_blocks == 0 or not feasible.any():
        return match
    perm_vehicles = rng.permutation(n_vehicles)
    perm_blocks = rng.permutation(n_blocks)
    permuted = feasible[perm_vehicles][:, perm_blocks]
    matched_row_for_col = maximum_bipartite_matching(csr_matrix(permuted), perm_type="row")
    for permuted_col, permuted_row in enumerate(matched_row_for_col):
        if permuted_row >= 0:
            match[int(perm_blocks[permuted_col])] = int(perm_vehicles[permuted_row])
    return match


def build_feasible_vehicle_day_assignments(
    block_instances: pd.DataFrame,
    ev_bus_specs: pd.DataFrame,
    depot_registry: pd.DataFrame | None = None,
    *,
    seed: int = RNG_SEED,
    scenario_mode: str = "ev_stock_scale",
    sample_block_multiplier: float = 1.0,
    max_vehicle_days: int | None = None,
    home_depot_radius_km: float | None = None,
    block_sampling: str = "uniform",
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Sample blocks per service date and match EVs to blocks feasibility-aware.

    Feasibility under daily-reset SOC is a pure threshold rule:
    ``(passenger_km + deadhead_km) <= battery_kwh * (usable_soc_max - usable_soc_min)
    / consumption_kwh_per_km``, i.e. each block is feasible exactly for the specs
    whose range covers its total distance. With ``home_depot_radius_km=None``
    (PR 1 behavior) the feasible spec-sets are nested by inclusion, so processing
    blocks in descending total distance and assigning a uniformly random spec from
    the currently feasible, still-unused pool yields an exact maximum-cardinality
    matching (exchange argument on the nested set system).

    With ``home_depot_radius_km`` set (PR 1.5), an EV-block pair is additionally
    admissible only if the block's attached depot is the EV's home depot or lies
    within the radius of it, and deadhead is computed pair-specifically
    home depot <-> block start/end. The constraint breaks the nested structure, so
    matching switches to exact Hopcroft-Karp (scipy) with seeded relabelling.
    ``home_depot_radius_km=0.0`` means strict same-depot assignment. EV specs then
    require the ``home_depot_*`` columns from
    :func:`mobility.bus.annual_home_depot.assign_home_depots`; specs without an
    assigned home depot are excluded from matching.

    ``block_sampling`` controls the daily block sample. ``uniform`` draws without
    replacement from all active blocks (PR 1 semantics). ``supply_weighted``
    (constrained mode only, plan v2 §15 decision (b)) weights each block by the
    number of home-depot vehicles admitting its depot under the radius, so blocks
    with no reachable fleet are never sampled and dense fleets simulate
    proportionally more local duty. The per-block weight is recorded as
    ``block_sampling_weight``.

    Returns ``(assignments, daily_diagnostics, unmatched_sampled_blocks)``.
    """
    if scenario_mode != "ev_stock_scale":
        raise NotImplementedError("Only scenario_mode='ev_stock_scale' is implemented.")
    if float(sample_block_multiplier) <= 0.0:
        raise ValueError("sample_block_multiplier must be positive.")
    if block_sampling not in ("uniform", "supply_weighted"):
        raise ValueError(f"block_sampling must be 'uniform' or 'supply_weighted', got {block_sampling!r}.")
    if block_sampling == "supply_weighted" and home_depot_radius_km is None:
        raise ValueError("block_sampling='supply_weighted' requires home_depot_radius_km (constrained mode).")
    if block_instances.empty or ev_bus_specs.empty:
        return (
            pd.DataFrame(columns=_feasible_assignment_columns()),
            pd.DataFrame(columns=_feasible_diagnostic_columns()),
            pd.DataFrame(columns=_unmatched_block_columns()),
        )
    required_instances = {"service_date", "block_instance_id", "block_template_id", "agency_id", "service_id", "block_id", "depot_id", "region_key", "passenger_distance_km"}
    missing = sorted(required_instances - set(block_instances.columns))
    if missing:
        raise ValueError(f"block_instances is missing required columns: {missing}")
    required_specs = {"vehicle_spec_id", "battery_kwh", "consumption_kwh_per_km", "usable_soc_min", "usable_soc_max"}
    missing_specs = sorted(required_specs - set(ev_bus_specs.columns))
    if missing_specs:
        raise ValueError(f"ev_bus_specs is missing required columns: {missing_specs}")

    instances = merge_depot_coords(block_instances, depot_registry)
    context = build_matching_context(ev_bus_specs, home_depot_radius_km)

    assignment_records: list[dict[str, Any]] = []
    diagnostic_records: list[dict[str, Any]] = []
    unmatched_records: list[dict[str, Any]] = []
    for service_date, day_blocks in instances.groupby("service_date", sort=True):
        day_assignments, day_diagnostics, day_unmatched = assign_vehicle_days_for_date(
            day_blocks,
            context,
            service_date=str(service_date),
            seed=seed,
            scenario_mode=scenario_mode,
            sample_block_multiplier=sample_block_multiplier,
            block_sampling=block_sampling,
        )
        assignment_records.extend(day_assignments)
        diagnostic_records.extend(day_diagnostics)
        unmatched_records.extend(day_unmatched)
        if max_vehicle_days is not None and len(assignment_records) >= int(max_vehicle_days):
            break

    assignments = pd.DataFrame.from_records(assignment_records, columns=_feasible_assignment_columns())
    if max_vehicle_days is not None and len(assignments) > int(max_vehicle_days):
        assignments = assignments.iloc[: int(max_vehicle_days)].copy()
    diagnostics = pd.DataFrame.from_records(diagnostic_records, columns=_feasible_diagnostic_columns())
    unmatched = pd.DataFrame.from_records(unmatched_records, columns=_unmatched_block_columns())
    return assignments.reset_index(drop=True), diagnostics.reset_index(drop=True), unmatched.reset_index(drop=True)


def assignment_frames_from_records(
    assignment_records: list[dict[str, Any]],
    diagnostic_records: list[dict[str, Any]],
    unmatched_records: list[dict[str, Any]],
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Convert per-date record lists to schema-stable frames (carryover day loop)."""
    return (
        pd.DataFrame.from_records(assignment_records, columns=_feasible_assignment_columns()),
        pd.DataFrame.from_records(diagnostic_records, columns=_feasible_diagnostic_columns()),
        pd.DataFrame.from_records(unmatched_records, columns=_unmatched_block_columns()),
    )


def merge_depot_coords(block_instances: pd.DataFrame, depot_registry: pd.DataFrame | None) -> pd.DataFrame:
    """Attach registry depot coordinates to block instances (idempotent helper)."""
    if depot_registry is not None and not depot_registry.empty and {"depot_id", "depot_lat", "depot_lon"}.issubset(depot_registry.columns):
        registry_cols = depot_registry.loc[:, ["depot_id", "depot_lat", "depot_lon"]].drop_duplicates("depot_id")
        return block_instances.drop(columns=["depot_lat", "depot_lon"], errors="ignore").merge(registry_cols, on="depot_id", how="left")
    return block_instances


@dataclass
class MatchingContext:
    """Per-run spec arrays precomputed once and reused for every service date."""

    specs: pd.DataFrame
    range_km: np.ndarray
    spec_order_desc: np.ndarray
    spec_ids: np.ndarray
    spec_consumption: np.ndarray
    spec_available_kwh: np.ndarray
    n_valid_specs: int
    constrained: bool
    radius_km: float
    assignment_method: str
    n_specs_with_home: float
    spec_home_ids: np.ndarray | None = None
    valid_vehicle: np.ndarray | None = None
    home_depot_ids: np.ndarray | None = None
    spec_home_idx: np.ndarray | None = None
    home_lat: np.ndarray | None = None
    home_lon: np.ndarray | None = None
    valid_group_sizes: np.ndarray | None = None


def build_matching_context(ev_bus_specs: pd.DataFrame, home_depot_radius_km: float | None) -> MatchingContext:
    specs = ev_bus_specs.sort_values("vehicle_spec_id", kind="stable").reset_index(drop=True)
    range_km = _spec_range_km(specs)
    spec_order_desc = np.argsort(-np.nan_to_num(range_km, nan=-np.inf), kind="stable")
    spec_ids = specs["vehicle_spec_id"].astype(str).to_numpy()
    spec_consumption = pd.to_numeric(specs["consumption_kwh_per_km"], errors="coerce").to_numpy(dtype=float)
    spec_available_kwh = (
        pd.to_numeric(specs["battery_kwh"], errors="coerce").to_numpy(dtype=float)
        * (
            pd.to_numeric(specs["usable_soc_max"], errors="coerce").to_numpy(dtype=float)
            - pd.to_numeric(specs["usable_soc_min"], errors="coerce").to_numpy(dtype=float)
        )
    )
    n_valid_specs = int(np.isfinite(range_km).sum())

    constrained = home_depot_radius_km is not None
    if constrained:
        radius_km = float(home_depot_radius_km)
        if radius_km < 0.0:
            raise ValueError("home_depot_radius_km must be >= 0.")
        required_home = {"home_depot_id", "home_depot_lat", "home_depot_lon"}
        missing_home = sorted(required_home - set(specs.columns))
        if missing_home:
            raise ValueError(
                f"home_depot_radius_km is set but ev_bus_specs is missing home-depot columns {missing_home}; "
                "run mobility.bus.annual_home_depot.assign_home_depots first."
            )
        spec_home_ids = specs["home_depot_id"].fillna("").astype(str).to_numpy()
        if "home_depot_status" in specs.columns:
            spec_has_home = specs["home_depot_status"].astype(str).eq("assigned").to_numpy() & (spec_home_ids != "")
        else:
            spec_has_home = spec_home_ids != ""
        valid_vehicle = spec_has_home & np.isfinite(range_km)
        home_depot_ids, spec_home_idx = np.unique(spec_home_ids, return_inverse=True)
        home_coords = (
            specs.assign(_home_id=spec_home_ids)
            .drop_duplicates("_home_id", keep="first")
            .set_index("_home_id")[["home_depot_lat", "home_depot_lon"]]
            .reindex(home_depot_ids)
        )
        home_lat = pd.to_numeric(home_coords["home_depot_lat"], errors="coerce").to_numpy(dtype=float)
        home_lon = pd.to_numeric(home_coords["home_depot_lon"], errors="coerce").to_numpy(dtype=float)
        valid_group_sizes = np.bincount(spec_home_idx[valid_vehicle], minlength=len(home_depot_ids)).astype(int)
        return MatchingContext(
            specs=specs,
            range_km=range_km,
            spec_order_desc=spec_order_desc,
            spec_ids=spec_ids,
            spec_consumption=spec_consumption,
            spec_available_kwh=spec_available_kwh,
            n_valid_specs=n_valid_specs,
            constrained=True,
            radius_km=radius_km,
            assignment_method=FEASIBLE_HOME_DEPOT_ASSIGNMENT_METHOD,
            n_specs_with_home=int(valid_vehicle.sum()),
            spec_home_ids=spec_home_ids,
            valid_vehicle=valid_vehicle,
            home_depot_ids=home_depot_ids,
            spec_home_idx=spec_home_idx,
            home_lat=home_lat,
            home_lon=home_lon,
            valid_group_sizes=valid_group_sizes,
        )
    return MatchingContext(
        specs=specs,
        range_km=range_km,
        spec_order_desc=spec_order_desc,
        spec_ids=spec_ids,
        spec_consumption=spec_consumption,
        spec_available_kwh=spec_available_kwh,
        n_valid_specs=n_valid_specs,
        constrained=False,
        radius_km=np.nan,
        assignment_method=FEASIBLE_ASSIGNMENT_METHOD,
        n_specs_with_home=np.nan,
    )


def assign_vehicle_days_for_date(
    day_blocks: pd.DataFrame,
    context: MatchingContext,
    *,
    service_date: str,
    seed: int = RNG_SEED,
    scenario_mode: str = "ev_stock_scale",
    sample_block_multiplier: float = 1.0,
    block_sampling: str = "uniform",
    soc_mode: str = "daily_reset",
    available_kwh_by_spec: dict[str, float] | None = None,
    available_from_by_spec: dict[str, pd.Timestamp] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Match EVs to one day's sampled blocks; the per-date core of the annual loop.

    Under ``soc_mode="daily_reset"`` this reproduces the historical behavior
    byte-for-byte (same RNG call sequence, same emitted values).

    Under ``soc_mode="carryover"`` (plan v2 §8.2, constrained mode only):

    - ``available_kwh_by_spec`` replaces the static ``battery * (usable_max -
      usable_min)`` energy screen with the state-projected energy above the
      usable floor; a missing spec key is fatal (§8.5).
    - ``available_from_by_spec`` adds the temporal guard: a candidate block is
      admissible for a spec only if its pair-specific deadhead start is at or
      after the instant the vehicle is physically free at its depot. Blocks
      unmatched solely because every energy-feasible vehicle was still out
      get ``unmatched_reason="vehicle_busy_overnight"``.
    """
    specs = context.specs
    range_km = context.range_km
    spec_available_kwh = context.spec_available_kwh
    if available_kwh_by_spec is not None:
        if not context.constrained:
            raise ValueError("available_kwh_by_spec (carryover screening) requires constrained home-depot matching.")
        spec_available_kwh = np.array([float(available_kwh_by_spec[spec_id]) for spec_id in context.spec_ids], dtype=float)
        with np.errstate(divide="ignore", invalid="ignore"):
            range_km = np.where(
                np.isfinite(spec_available_kwh) & np.isfinite(context.spec_consumption) & (context.spec_consumption > 0.0),
                spec_available_kwh / context.spec_consumption,
                np.nan,
            )
    available_from_ns: np.ndarray | None = None
    if available_from_by_spec is not None:
        available_from_ns = np.array(
            [pd.Timestamp(available_from_by_spec[spec_id]).value for spec_id in context.spec_ids],
            dtype=np.int64,
        )

    assignment_records: list[dict[str, Any]] = []
    diagnostic_records: list[dict[str, Any]] = []
    unmatched_records: list[dict[str, Any]] = []

    day_blocks = day_blocks.copy().sort_values(["region_key", "passenger_distance_km", "duration_h", "block_instance_id"], kind="stable").reset_index(drop=True)
    daily_seed = stable_daily_seed(seed, str(service_date))
    rng = np.random.default_rng(daily_seed)
    n_available = int(len(day_blocks))
    n_sample = min(n_available, int(np.ceil(len(specs) * float(sample_block_multiplier))))
    if n_sample <= 0:
        return assignment_records, diagnostic_records, unmatched_records
    if block_sampling == "supply_weighted":
        day_weights = _block_supply_weights(day_blocks, context.home_depot_ids, context.home_lat, context.home_lon, context.valid_group_sizes, context.radius_km)
        positive = np.flatnonzero(day_weights > 0)
        n_blocks_positive_weight = int(positive.size)
        n_sample = min(n_sample, n_blocks_positive_weight)
        if n_sample <= 0:
            return assignment_records, diagnostic_records, unmatched_records
        # Efraimidis-Spirakis weighted sampling without replacement:
        # exponential keys scaled by 1/weight, take the n_sample smallest.
        keys = rng.exponential(1.0, size=positive.size) / day_weights[positive]
        sampled_positions = positive[np.argsort(keys, kind="stable")[:n_sample]]
    else:
        day_weights = None
        n_blocks_positive_weight = -1
        sampled_positions = rng.choice(np.arange(n_available), size=n_sample, replace=False)
    sampled = day_blocks.iloc[sampled_positions].reset_index(drop=True)
    sampled_weights = day_weights[sampled_positions] if day_weights is not None else np.full(n_sample, np.nan)

    passenger_km = pd.to_numeric(sampled["passenger_distance_km"], errors="coerce").fillna(0.0).to_numpy(dtype=float)
    n_feasible_pre_temporal: np.ndarray | None = None
    if context.constrained:
        # Pair-specific admissibility (home depot within radius of the block's
        # attached depot) and pair-specific deadhead (home depot <-> block
        # start/end). The spatial constraint breaks the nested threshold
        # structure, so exact matching uses Hopcroft-Karp.
        block_depot_ids = sampled["depot_id"].astype(str).to_numpy()
        block_depot_lat = _coord_column(sampled, "depot_lat")
        block_depot_lon = _coord_column(sampled, "depot_lon")
        start_lat = _coord_column(sampled, "start_lat")
        start_lon = _coord_column(sampled, "start_lon")
        end_lat = _coord_column(sampled, "end_lat")
        end_lon = _coord_column(sampled, "end_lon")
        home_lat = context.home_lat
        home_lon = context.home_lon
        home_depot_ids = context.home_depot_ids
        spec_home_idx = context.spec_home_idx
        valid_vehicle = context.valid_vehicle

        depot_distance = _haversine_km_vec(home_lat[:, None], home_lon[:, None], block_depot_lat[None, :], block_depot_lon[None, :])
        in_radius = home_depot_ids[:, None] == block_depot_ids[None, :]
        in_radius |= np.nan_to_num(depot_distance, nan=np.inf) <= context.radius_km
        to_block = _haversine_km_vec(home_lat[:, None], home_lon[:, None], start_lat[None, :], start_lon[None, :])
        from_block = _haversine_km_vec(end_lat[None, :], end_lon[None, :], home_lat[:, None], home_lon[:, None])
        pair_incomplete = ~np.isfinite(to_block) | ~np.isfinite(from_block)
        pair_deadhead = np.nan_to_num(to_block, nan=0.0) + np.nan_to_num(from_block, nan=0.0)
        pair_total = passenger_km[None, :] + pair_deadhead

        deadhead_start_ns: np.ndarray | None = None
        if available_from_ns is not None:
            # Temporal guard: pair-specific deadhead start mirrors the event
            # ledger (NaN distance contributes 0 h, matching events-stage
            # behavior for missing coordinates).
            start_dt_ns = pd.to_datetime(sampled["start_datetime"]).astype("datetime64[ns]").astype("int64").to_numpy()
            to_block_h = np.where(np.isfinite(to_block), to_block / DEADHEAD_SPEED_KMH, 0.0)
            deadhead_start_ns = start_dt_ns[None, :] - (to_block_h * 3.6e12).astype(np.int64)

        feasible = np.zeros((len(specs), n_sample), dtype=bool)
        if deadhead_start_ns is not None:
            pre_temporal = np.zeros((len(specs), n_sample), dtype=bool)
        for group in range(len(home_depot_ids)):
            rows = np.flatnonzero(valid_vehicle & (spec_home_idx == group))
            if rows.size == 0:
                continue
            pair_ok = in_radius[group][None, :] & (range_km[rows][:, None] >= pair_total[group][None, :])
            if deadhead_start_ns is not None:
                pre_temporal[rows, :] = pair_ok
                pair_ok = pair_ok & (deadhead_start_ns[group][None, :] >= available_from_ns[rows][:, None])
            feasible[rows, :] = pair_ok
        if deadhead_start_ns is not None:
            n_feasible_pre_temporal = pre_temporal.sum(axis=0).astype(int)
        n_vehicles_in_radius = (context.valid_group_sizes @ in_radius.astype(np.int64)).astype(int)
        n_feasible_vehicles = feasible.sum(axis=0).astype(int)
        matched_spec_pos = _max_bipartite_match(feasible, rng)

        # Per-block emission values: matched pair, else best case over
        # in-radius home depots (NaN when no depot is in radius).
        any_in_radius = in_radius.any(axis=0)
        masked_total = np.where(in_radius, pair_total, np.inf)
        best_group = np.argmin(masked_total, axis=0)
        column_index = np.arange(n_sample)
        deadhead_km = np.where(any_in_radius, pair_deadhead[best_group, column_index], np.nan)
        total_km = np.where(any_in_radius, pair_total[best_group, column_index], np.nan)
        deadhead_incomplete = np.zeros(n_sample, dtype=bool)
        matched_columns = np.flatnonzero(matched_spec_pos >= 0)
        if matched_columns.size:
            matched_groups = spec_home_idx[matched_spec_pos[matched_columns]]
            deadhead_km[matched_columns] = pair_deadhead[matched_groups, matched_columns]
            total_km[matched_columns] = pair_total[matched_groups, matched_columns]
            deadhead_incomplete[matched_columns] = pair_incomplete[matched_groups, matched_columns]
        block_order = np.argsort(-np.nan_to_num(total_km, nan=-np.inf), kind="stable")
    else:
        deadhead_km, deadhead_incomplete = _block_deadhead_km(sampled)
        total_km = passenger_km + deadhead_km
        n_vehicles_in_radius = np.full(n_sample, np.nan)

        # Exact maximum matching on the nested threshold structure: descending
        # block distance, random pick from the feasible unused-spec pool.
        block_order = np.argsort(-total_km, kind="stable")
        pool: list[int] = []
        admitted = 0
        matched_spec_pos = np.full(n_sample, -1, dtype=int)
        n_feasible_vehicles = np.zeros(n_sample, dtype=int)
        for block_pos in block_order:
            km = float(total_km[block_pos])
            while admitted < len(context.spec_order_desc):
                candidate = int(context.spec_order_desc[admitted])
                if np.isfinite(range_km[candidate]) and range_km[candidate] >= km:
                    pool.append(candidate)
                    admitted += 1
                else:
                    break
            n_feasible_vehicles[block_pos] = admitted
            if pool:
                pick = int(rng.integers(len(pool)))
                pool[pick], pool[-1] = pool[-1], pool[pick]
                matched_spec_pos[block_pos] = pool.pop()

    n_matched = 0
    for emit_index, block_pos in enumerate(block_order):
        block_row = sampled.iloc[int(block_pos)]
        spec_pos = int(matched_spec_pos[block_pos])
        if spec_pos < 0:
            if context.constrained:
                if int(n_vehicles_in_radius[block_pos]) == 0:
                    reason = "no_vehicle_in_radius"
                elif int(n_feasible_vehicles[block_pos]) == 0:
                    if n_feasible_pre_temporal is not None and int(n_feasible_pre_temporal[block_pos]) > 0:
                        # Energy-feasible fleet exists but every vehicle is
                        # still out on (or charging after) the previous duty at
                        # this block's pull-out (carryover temporal guard).
                        reason = "vehicle_busy_overnight"
                    else:
                        reason = "no_feasible_vehicle_in_radius"
                else:
                    reason = "lost_matching_competition"
            else:
                reason = "no_feasible_vehicle" if n_feasible_vehicles[block_pos] == 0 else "lost_matching_competition"
            unmatched_records.append(
                {
                    "service_date": str(service_date),
                    "block_instance_id": str(block_row["block_instance_id"]),
                    "block_id": str(block_row["block_id"]),
                    "unmatched_reason": reason,
                    "n_feasible_vehicles": int(n_feasible_vehicles[block_pos]),
                    "n_vehicles_in_radius": float(n_vehicles_in_radius[block_pos]),
                    "block_sampling_weight": float(sampled_weights[block_pos]),
                    "passenger_distance_km": float(passenger_km[block_pos]),
                    "deadhead_km_est": float(deadhead_km[block_pos]),
                    "total_distance_km_est": float(total_km[block_pos]),
                    "assignment_method": context.assignment_method,
                    "daily_soc_mode": soc_mode,
                }
            )
            continue
        distance = float(passenger_km[block_pos])
        duration = float(np.nan_to_num(pd.to_numeric(block_row.get("duration_h"), errors="coerce"), nan=0.0))
        assignment_records.append(
            {
                "service_date": str(service_date),
                "vehicle_day_id": f"vd_{service_date}_{emit_index:05d}_{context.spec_ids[spec_pos]}",
                "vehicle_spec_id": context.spec_ids[spec_pos],
                "block_instance_id": str(block_row["block_instance_id"]),
                "block_template_id": str(block_row["block_template_id"]),
                "agency_id": str(block_row["agency_id"]),
                "service_id": str(block_row["service_id"]),
                "block_id": str(block_row["block_id"]),
                "depot_id": str(block_row.get("depot_id", "")),
                "home_depot_id": str(context.spec_home_ids[spec_pos]) if context.constrained else "",
                "region_key": str(block_row.get("region_key", "unknown")),
                "distance_bin": _distance_bin(distance),
                "duration_bin": _duration_bin(duration),
                "assignment_status": "matched_feasible",
                "assignment_method": context.assignment_method,
                "scenario_mode": scenario_mode,
                "sample_weight": 1.0,
                "block_sampling_weight": float(sampled_weights[block_pos]),
                "assignment_seed": daily_seed,
                "required_kwh_est": float(total_km[block_pos] * context.spec_consumption[spec_pos]),
                "available_kwh_at_assignment": float(spec_available_kwh[spec_pos]),
                "deadhead_km_est": float(deadhead_km[block_pos]),
                "deadhead_estimate_incomplete": bool(deadhead_incomplete[block_pos]),
                "daily_soc_mode": soc_mode,
            }
        )
        n_matched += 1

    n_unmatched = n_sample - n_matched
    n_blocks_any_feasible = int((n_feasible_vehicles > 0).sum())
    n_no_feasible = int((n_feasible_vehicles == 0).sum())
    if context.constrained:
        n_no_vehicle_in_radius = int((n_vehicles_in_radius == 0).sum())
        if n_feasible_pre_temporal is not None:
            busy_mask = (n_vehicles_in_radius > 0) & (n_feasible_vehicles == 0) & (n_feasible_pre_temporal > 0)
            n_busy_overnight = int(busy_mask.sum())
            n_no_feasible_in_radius = int(((n_vehicles_in_radius > 0) & (n_feasible_vehicles == 0) & ~busy_mask).sum())
        else:
            n_busy_overnight = 0
            n_no_feasible_in_radius = int(((n_vehicles_in_radius > 0) & (n_feasible_vehicles == 0)).sum())
    else:
        n_no_vehicle_in_radius = 0
        n_no_feasible_in_radius = 0
        n_busy_overnight = 0
    diagnostic_records.append(
        {
            "service_date": str(service_date),
            "n_ev_specs": int(len(specs)),
            "n_ev_specs_valid_params": context.n_valid_specs,
            "n_ev_specs_with_home_depot": context.n_specs_with_home,
            "home_depot_radius_km": context.radius_km,
            "n_active_block_instances_for_service_date": n_available,
            "n_sampled_block_instances_for_service_date": int(n_sample),
            "n_feasible_edges": int(n_feasible_vehicles.sum()),
            "n_blocks_with_any_feasible_vehicle": n_blocks_any_feasible,
            "n_matched_feasible_block_instances_for_service_date": int(n_matched),
            "n_unmatched_sampled_block_instances_for_service_date": int(n_unmatched),
            "n_unmatched_no_feasible_vehicle": n_no_feasible,
            "n_unmatched_no_vehicle_in_radius": n_no_vehicle_in_radius,
            "n_unmatched_no_feasible_vehicle_in_radius": n_no_feasible_in_radius,
            "n_unmatched_vehicle_busy_overnight": n_busy_overnight,
            "n_unmatched_lost_matching_competition": int(n_unmatched - n_no_feasible),
            "sampled_block_coverage_share": float(n_sample / n_available) if n_available else np.nan,
            "matched_sample_share": float(n_matched / n_sample) if n_sample else np.nan,
            "matched_active_block_share": float(n_matched / n_available) if n_available else np.nan,
            "block_sampling": block_sampling,
            "n_blocks_positive_sampling_weight": n_blocks_positive_weight if n_blocks_positive_weight >= 0 else np.nan,
            "assignment_method": context.assignment_method,
            "daily_soc_mode": soc_mode,
            # Legacy-named columns kept for run-summary compatibility.
            "n_available_block_instances_for_service_date": n_available,
            "n_assigned_block_instances_for_service_date": int(n_matched),
            "n_unassigned_block_instances_for_service_date": int(n_available - n_matched),
            "daily_assignment_coverage_share": float(n_matched / n_available) if n_available else np.nan,
        }
    )
    return assignment_records, diagnostic_records, unmatched_records


def _feasible_assignment_columns() -> list[str]:
    return [
        "service_date",
        "vehicle_day_id",
        "vehicle_spec_id",
        "block_instance_id",
        "block_template_id",
        "agency_id",
        "service_id",
        "block_id",
        "depot_id",
        "home_depot_id",
        "region_key",
        "distance_bin",
        "duration_bin",
        "assignment_status",
        "assignment_method",
        "scenario_mode",
        "sample_weight",
        "block_sampling_weight",
        "assignment_seed",
        "required_kwh_est",
        "available_kwh_at_assignment",
        "deadhead_km_est",
        "deadhead_estimate_incomplete",
        "daily_soc_mode",
    ]


def _feasible_diagnostic_columns() -> list[str]:
    return [
        "service_date",
        "n_ev_specs",
        "n_ev_specs_valid_params",
        "n_ev_specs_with_home_depot",
        "home_depot_radius_km",
        "n_active_block_instances_for_service_date",
        "n_sampled_block_instances_for_service_date",
        "n_feasible_edges",
        "n_blocks_with_any_feasible_vehicle",
        "n_matched_feasible_block_instances_for_service_date",
        "n_unmatched_sampled_block_instances_for_service_date",
        "n_unmatched_no_feasible_vehicle",
        "n_unmatched_no_vehicle_in_radius",
        "n_unmatched_no_feasible_vehicle_in_radius",
        "n_unmatched_vehicle_busy_overnight",
        "n_unmatched_lost_matching_competition",
        "sampled_block_coverage_share",
        "matched_sample_share",
        "matched_active_block_share",
        "block_sampling",
        "n_blocks_positive_sampling_weight",
        "assignment_method",
        "daily_soc_mode",
        "n_available_block_instances_for_service_date",
        "n_assigned_block_instances_for_service_date",
        "n_unassigned_block_instances_for_service_date",
        "daily_assignment_coverage_share",
    ]


def _unmatched_block_columns() -> list[str]:
    return [
        "service_date",
        "block_instance_id",
        "block_id",
        "unmatched_reason",
        "n_feasible_vehicles",
        "n_vehicles_in_radius",
        "block_sampling_weight",
        "passenger_distance_km",
        "deadhead_km_est",
        "total_distance_km_est",
        "assignment_method",
        "daily_soc_mode",
    ]


__all__ = [
    "DEADHEAD_SPEED_KMH",
    "FEASIBLE_ASSIGNMENT_METHOD",
    "FEASIBLE_HOME_DEPOT_ASSIGNMENT_METHOD",
    "MatchingContext",
    "RNG_SEED",
    "assign_vehicle_days_for_date",
    "assignment_frames_from_records",
    "build_feasible_vehicle_day_assignments",
    "build_matching_context",
    "build_vehicle_day_assignments",
    "merge_depot_coords",
    "stable_daily_seed",
]
