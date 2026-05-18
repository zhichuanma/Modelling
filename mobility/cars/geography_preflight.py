"""Small-area geography consistency checks for private-car station curves."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Iterable, Mapping

import pandas as pd


COUNTRY_PREFIXES = {
    "E": "England",
    "W": "Wales",
    "S": "Scotland",
    "N": "Northern Ireland",
}

PRIVATE_CAR_SUBTYPES = {"car", "cars", "private_car", "private cars", "privatecar"}

SCOTLAND_FAIL_FAST_MESSAGE = (
    "Scotland EV home_lsoa appears to use Data Zone 2011, while station / "
    "destination / centroid data appears to use Data Zone 2022. Exact "
    "lsoa_code matching is invalid until Scotland geography is unified."
)

HEAD_SAMPLE_WARNING = (
    "--max-vehicles currently uses head(n). This sample may exclude Scotland, "
    "Wales, or Northern Ireland depending on fleet ordering. It is not valid "
    "for national coverage validation."
)

SCOTLAND_CRITICAL_CHECKS = {
    "EV home_lsoa vs station lsoa_code",
    "EV home_lsoa vs destination origin_lsoa",
    "EV home_lsoa vs destination dest_lsoa",
    "EV home_lsoa vs centroid codes",
}


class GeographyPreflightError(RuntimeError):
    """Raised when private-car geography preflight finds a blocking mismatch."""

    def __init__(self, message: str, *, report_path: Path | None = None):
        super().__init__(message)
        self.report_path = Path(report_path) if report_path is not None else None


def _normalise_codes(values: Iterable[object] | pd.Series) -> set[str]:
    series = values if isinstance(values, pd.Series) else pd.Series(list(values), dtype="object")
    if series.empty:
        return set()
    text = series.astype("string").str.strip()
    lower = text.str.lower()
    valid = text.notna() & text.ne("") & ~lower.isin({"nan", "none", "null", "na", "<na>", "nat"})
    return set(text.loc[valid].astype(str))


def _code_set_from_frame(frame: pd.DataFrame | None, column: str) -> set[str]:
    if frame is None or column not in frame.columns:
        return set()
    return _normalise_codes(frame[column])


def _prepare_private_ev_fleet(ev_fleet: pd.DataFrame) -> pd.DataFrame:
    result = ev_fleet.copy()
    if "vehicle_subtype" in result.columns:
        result = result.loc[
            result["vehicle_subtype"].astype(str).str.lower().isin(PRIVATE_CAR_SUBTYPES)
        ].copy()
    if "home_lsoa" not in result.columns:
        if "LSOA_code" in result.columns:
            result["home_lsoa"] = result["LSOA_code"]
        else:
            result["home_lsoa"] = pd.NA
    return result


def _read_destination_frame(
    destination_table_path: Path | str | None,
    destination_df: pd.DataFrame | None,
) -> pd.DataFrame:
    if destination_df is not None:
        return destination_df.copy()
    if destination_table_path is None:
        return pd.DataFrame(columns=["origin_lsoa", "dest_lsoa"])
    path = Path(destination_table_path)
    if not path.exists():
        return pd.DataFrame(columns=["origin_lsoa", "dest_lsoa"])
    return pd.read_parquet(path, columns=["origin_lsoa", "dest_lsoa"])


def _read_attractiveness_frame(
    attractiveness_path: Path | str | None,
    attractiveness_df: pd.DataFrame | None,
) -> pd.DataFrame:
    if attractiveness_df is not None:
        return attractiveness_df.copy()
    if attractiveness_path is None:
        return pd.DataFrame(columns=["lsoa_code"])
    path = Path(attractiveness_path)
    if not path.exists():
        return pd.DataFrame(columns=["lsoa_code"])
    return pd.read_parquet(path, columns=["lsoa_code"])


def _codes_for_prefix(codes: set[str], prefix: str) -> set[str]:
    return {code for code in codes if code.startswith(prefix)}


def _examples(codes: set[str], limit: int = 5) -> list[str]:
    return sorted(codes)[:limit]


def _range(codes: set[str]) -> tuple[str | None, str | None]:
    ordered = sorted(codes)
    if not ordered:
        return None, None
    return ordered[0], ordered[-1]


def _scotland_numeric_suffixes(codes: set[str]) -> list[int]:
    values: list[int] = []
    for code in codes:
        if not code.startswith("S"):
            continue
        if code.startswith("S010") and len(code) > 4:
            digits = code[4:]
        else:
            digits = "".join(ch for ch in code if ch.isdigit())
        if not digits:
            continue
        try:
            values.append(int(digits))
        except ValueError:
            continue
    return values


def _infer_scotland_geography_version(codes: set[str]) -> str:
    scotland_codes = _codes_for_prefix(codes, "S")
    if not scotland_codes:
        return "not_applicable"
    suffixes = _scotland_numeric_suffixes(scotland_codes)
    if not suffixes:
        return "unknown"
    has_dz2011 = any(6506 <= value <= 13481 for value in suffixes)
    has_dz2022 = any(13482 <= value <= 20873 for value in suffixes)
    if has_dz2011 and has_dz2022:
        return "mixed_DZ2011_DZ2022"
    if has_dz2011:
        return "Data Zone 2011"
    if has_dz2022:
        return "Data Zone 2022"
    return "unknown"


def _artifact_rows(
    *,
    artifact_name: str,
    file_path: object,
    small_area_code_column: str,
    codes: set[str],
    notes: str = "",
) -> list[dict]:
    rows: list[dict] = []
    if not codes:
        rows.append(
            {
                "artifact_name": artifact_name,
                "file_path": str(file_path),
                "small_area_code_column": small_area_code_column,
                "country_or_prefix": "none",
                "country": "none",
                "scotland_geography_version": "not_applicable",
                "code_min": None,
                "code_max": None,
                "unique_code_count": 0,
                "example_codes": [],
                "notes": notes,
            }
        )
        return rows

    for prefix, country in COUNTRY_PREFIXES.items():
        prefix_codes = _codes_for_prefix(codes, prefix)
        if not prefix_codes:
            continue
        code_min, code_max = _range(prefix_codes)
        rows.append(
            {
                "artifact_name": artifact_name,
                "file_path": str(file_path),
                "small_area_code_column": small_area_code_column,
                "country_or_prefix": prefix,
                "country": country,
                "scotland_geography_version": (
                    _infer_scotland_geography_version(prefix_codes)
                    if prefix == "S"
                    else "not_applicable"
                ),
                "code_min": code_min,
                "code_max": code_max,
                "unique_code_count": int(len(prefix_codes)),
                "example_codes": _examples(prefix_codes),
                "notes": notes,
            }
        )
    return rows


def _overlap_rows(
    *,
    check_name: str,
    left_name: str,
    left_codes: set[str],
    right_name: str,
    right_codes: set[str],
) -> list[dict]:
    rows: list[dict] = []
    for prefix, country in COUNTRY_PREFIXES.items():
        left = _codes_for_prefix(left_codes, prefix)
        right = _codes_for_prefix(right_codes, prefix)
        overlap = left & right
        left_only = left - right
        right_only = right - left
        rows.append(
            {
                "check_name": check_name,
                "country_or_prefix": prefix,
                "country": country,
                "left_name": left_name,
                "right_name": right_name,
                "left_unique_count": int(len(left)),
                "right_unique_count": int(len(right)),
                "exact_overlap_count": int(len(overlap)),
                "overlap_rate_left": (float(len(overlap) / len(left)) if left else None),
                "overlap_rate_right": (float(len(overlap) / len(right)) if right else None),
                "left_scotland_geography_version": (
                    _infer_scotland_geography_version(left) if prefix == "S" else "not_applicable"
                ),
                "right_scotland_geography_version": (
                    _infer_scotland_geography_version(right) if prefix == "S" else "not_applicable"
                ),
                "example_left_only_codes": _examples(left_only),
                "example_right_only_codes": _examples(right_only),
            }
        )
    return rows


def _evaluate_blockers_and_warnings(
    overlap_checks: pd.DataFrame,
    *,
    sampling_context: Mapping[str, object] | None,
) -> tuple[list[dict], list[dict]]:
    blockers: list[dict] = []
    warnings: list[dict] = []
    if overlap_checks.empty:
        return blockers, warnings

    for row in overlap_checks.to_dict(orient="records"):
        has_both_sides = int(row["left_unique_count"]) > 0 and int(row["right_unique_count"]) > 0
        has_zero_overlap = int(row["exact_overlap_count"]) == 0
        if (
            row["country_or_prefix"] == "S"
            and row["check_name"] in SCOTLAND_CRITICAL_CHECKS
            and has_both_sides
            and has_zero_overlap
        ):
            blockers.append(
                {
                    "country_or_prefix": "S",
                    "country": "Scotland",
                    "check_name": row["check_name"],
                    "left_unique_count": int(row["left_unique_count"]),
                    "right_unique_count": int(row["right_unique_count"]),
                    "exact_overlap_count": int(row["exact_overlap_count"]),
                    "left_scotland_geography_version": row["left_scotland_geography_version"],
                    "right_scotland_geography_version": row["right_scotland_geography_version"],
                    "message": SCOTLAND_FAIL_FAST_MESSAGE,
                }
            )
        elif row["country_or_prefix"] == "N" and has_both_sides and has_zero_overlap:
            warnings.append(
                {
                    "country_or_prefix": "N",
                    "country": "Northern Ireland",
                    "check_name": row["check_name"],
                    "left_unique_count": int(row["left_unique_count"]),
                    "right_unique_count": int(row["right_unique_count"]),
                    "exact_overlap_count": int(row["exact_overlap_count"]),
                    "message": (
                        "Northern Ireland small-area codes have zero exact overlap for this check; "
                        "review N200/N210 geography consistency separately from the Scotland blocker."
                    ),
                }
            )

    sampling_mode = str((sampling_context or {}).get("sampling_mode", "full"))
    if sampling_mode == "head":
        warnings.append(
            {
                "country_or_prefix": "all",
                "country": "all",
                "check_name": "sampling",
                "message": HEAD_SAMPLE_WARNING,
            }
        )

    return blockers, warnings


def build_privatecar_geography_preflight_report(
    *,
    ev_fleet: pd.DataFrame,
    stations: pd.DataFrame,
    centroids: pd.DataFrame,
    destination_table_path: Path | str | None = None,
    attractiveness_path: Path | str | None = None,
    destination_df: pd.DataFrame | None = None,
    attractiveness_df: pd.DataFrame | None = None,
    parking_event_lsoas: Iterable[object] | None = None,
    source_paths: Mapping[str, object] | None = None,
    sampling_context: Mapping[str, object] | None = None,
    geography_context: Mapping[str, object] | None = None,
) -> dict:
    """Build artifact inventory and overlap checks for small-area code systems."""

    sources = dict(source_paths or {})
    geography_meta = dict(
        geography_context
        or getattr(ev_fleet, "attrs", {}).get("scotland_geography_unification", {})
        or {}
    )
    ev_private = _prepare_private_ev_fleet(ev_fleet)
    destination = _read_destination_frame(destination_table_path, destination_df)
    attractiveness = _read_attractiveness_frame(attractiveness_path, attractiveness_df)

    ev_home_codes = _code_set_from_frame(ev_private, "home_lsoa")
    station_codes = _code_set_from_frame(stations, "lsoa_code")
    centroid_codes = _code_set_from_frame(centroids, "lsoa_code")
    destination_origin_codes = _code_set_from_frame(destination, "origin_lsoa")
    destination_dest_codes = _code_set_from_frame(destination, "dest_lsoa")
    attractiveness_codes = _code_set_from_frame(attractiveness, "lsoa_code")
    if parking_event_lsoas is None:
        parking_codes = ev_home_codes | destination_dest_codes
        parking_notes = "Candidate runtime ParkingEvent.location_lsoa values: EV home_lsoa plus destination dest_lsoa."
    else:
        parking_codes = _normalise_codes(parking_event_lsoas)
        parking_notes = "Observed runtime ParkingEvent.location_lsoa values."

    artifact_rows: list[dict] = []
    artifact_rows.extend(
        _artifact_rows(
            artifact_name="EV allocation",
            file_path=sources.get("ev_allocation", "data/EV_UK_LSOA_2025_with_energy.csv"),
            small_area_code_column="home_lsoa (derived from LSOA_code when needed)",
            codes=ev_home_codes,
            notes="Private-car EV home small-area codes used by station matching fallback.",
        )
    )
    artifact_rows.extend(
        _artifact_rows(
            artifact_name="person_fleet",
            file_path=sources.get("person_fleet", "data/person_fleet.parquet"),
            small_area_code_column="none",
            codes=set(),
            notes="Binds ev_id to person_id and nts_region; geography comes from EV allocation.",
        )
    )
    artifact_rows.extend(
        _artifact_rows(
            artifact_name="station metadata",
            file_path=sources.get("stations", "data/UK_OCM_stations_labeled.csv"),
            small_area_code_column="lsoa_code",
            codes=station_codes,
            notes="Public charging station small-area code used by station_matcher._build_lsoa_indices.",
        )
    )
    artifact_rows.extend(
        _artifact_rows(
            artifact_name="destination choice table origin",
            file_path=destination_table_path or sources.get("destination_choice_table", ""),
            small_area_code_column="origin_lsoa",
            codes=destination_origin_codes,
            notes="LazyDestinationSampler lookup key.",
        )
    )
    artifact_rows.extend(
        _artifact_rows(
            artifact_name="destination choice table destination",
            file_path=destination_table_path or sources.get("destination_choice_table", ""),
            small_area_code_column="dest_lsoa",
            codes=destination_dest_codes,
            notes="Sampled non-home parking-event location.",
        )
    )
    artifact_rows.extend(
        _artifact_rows(
            artifact_name="centroid lookup",
            file_path=sources.get("centroids", "mobility.core.spatial.load_lsoa_centroids()"),
            small_area_code_column="lsoa_code",
            codes=centroid_codes,
            notes="Distance lookup for destinations and station scoring.",
        )
    )
    artifact_rows.extend(
        _artifact_rows(
            artifact_name="POI attractiveness",
            file_path=attractiveness_path or sources.get("lsoa_scene_attractiveness", ""),
            small_area_code_column="lsoa_code",
            codes=attractiveness_codes,
            notes="Destination-choice attractiveness universe.",
        )
    )
    artifact_rows.extend(
        _artifact_rows(
            artifact_name="parking event LSOA",
            file_path="runtime DailySchedule.parking_events",
            small_area_code_column="ParkingEvent.location_lsoa",
            codes=parking_codes,
            notes=parking_notes,
        )
    )

    overlap_rows: list[dict] = []
    specs = [
        (
            "EV home_lsoa vs station lsoa_code",
            "EV home_lsoa",
            ev_home_codes,
            "station lsoa_code",
            station_codes,
        ),
        (
            "EV home_lsoa vs destination origin_lsoa",
            "EV home_lsoa",
            ev_home_codes,
            "destination origin_lsoa",
            destination_origin_codes,
        ),
        (
            "EV home_lsoa vs destination dest_lsoa",
            "EV home_lsoa",
            ev_home_codes,
            "destination dest_lsoa",
            destination_dest_codes,
        ),
        (
            "EV home_lsoa vs centroid codes",
            "EV home_lsoa",
            ev_home_codes,
            "centroid lsoa_code",
            centroid_codes,
        ),
        (
            "station lsoa_code vs centroid codes",
            "station lsoa_code",
            station_codes,
            "centroid lsoa_code",
            centroid_codes,
        ),
        (
            "station lsoa_code vs POI attractiveness lsoa_code",
            "station lsoa_code",
            station_codes,
            "POI attractiveness lsoa_code",
            attractiveness_codes,
        ),
        (
            "parking event lsoa vs station lsoa_code",
            "parking event lsoa",
            parking_codes,
            "station lsoa_code",
            station_codes,
        ),
        (
            "parking event lsoa vs centroid codes",
            "parking event lsoa",
            parking_codes,
            "centroid lsoa_code",
            centroid_codes,
        ),
    ]
    for check_name, left_name, left_codes, right_name, right_codes in specs:
        overlap_rows.extend(
            _overlap_rows(
                check_name=check_name,
                left_name=left_name,
                left_codes=left_codes,
                right_name=right_name,
                right_codes=right_codes,
            )
        )

    artifact_inventory = pd.DataFrame(artifact_rows)
    overlap_checks = pd.DataFrame(overlap_rows)
    blockers, warnings = _evaluate_blockers_and_warnings(
        overlap_checks,
        sampling_context=sampling_context,
    )
    fail_fast = bool(blockers)
    crosswalk_used = bool(geography_meta.get("applied"))
    final_scotland_version = (
        str(geography_meta.get("target_geography_version") or "Data Zone 2022")
        if crosswalk_used and not fail_fast
        else ("blocked_not_unified" if fail_fast else "consistent_by_exact_overlap")
    )
    summary = {
        "status": "failed" if fail_fast else ("passed_with_warnings" if warnings else "passed"),
        "fail_fast": fail_fast,
        "blocker_count": int(len(blockers)),
        "warning_count": int(len(warnings)),
        "failure_message": SCOTLAND_FAIL_FAST_MESSAGE if fail_fast else "",
        "scotland_ev_home_lsoa_geography_version": _infer_scotland_geography_version(ev_home_codes),
        "scotland_station_geography_version": _infer_scotland_geography_version(station_codes),
        "scotland_centroid_geography_version": _infer_scotland_geography_version(centroid_codes),
        "scotland_destination_origin_geography_version": _infer_scotland_geography_version(
            destination_origin_codes
        ),
        "scotland_destination_dest_geography_version": _infer_scotland_geography_version(
            destination_dest_codes
        ),
        "scotland_geography_final_version": final_scotland_version,
        "crosswalk_used": crosswalk_used,
        "crosswalk_method": geography_meta.get("method", ""),
        "crosswalk_rows": geography_meta.get("crosswalk_rows"),
        "crosswalk_dz2011_count": geography_meta.get("crosswalk_dz2011_count"),
        "crosswalk_dz2022_count": geography_meta.get("crosswalk_dz2022_count"),
        "crosswalk_dz2011_boundary_path": geography_meta.get("dz2011_boundary_path", ""),
        "crosswalk_dz2022_boundary_path": geography_meta.get("dz2022_boundary_path", ""),
        "scotland_rows_reassigned_to_dz2022": geography_meta.get("rows_reassigned", 0),
        "scotland_rows_unmapped_to_dz2022": geography_meta.get("rows_unmapped", 0),
        "scotland_unique_dz2011_seen": geography_meta.get("unique_dz2011_seen", 0),
        "scotland_unique_dz2022_assigned": geography_meta.get("unique_dz2022_assigned", 0),
        "scotland_geography_unification_reason": geography_meta.get("reason", ""),
        "sampling_mode": str((sampling_context or {}).get("sampling_mode", "full")),
        "max_vehicles": (sampling_context or {}).get("max_vehicles"),
        "sample_n_per_country": (sampling_context or {}).get("sample_n_per_country"),
        "sample_fraction_by_country": (sampling_context or {}).get("sample_fraction_by_country"),
        "sample_warning": HEAD_SAMPLE_WARNING
        if str((sampling_context or {}).get("sampling_mode", "full")) == "head"
        else "",
    }

    return {
        "summary": summary,
        "artifact_inventory": artifact_inventory,
        "overlap_checks": overlap_checks,
        "blockers": blockers,
        "warnings": warnings,
    }


def _json_records(frame: pd.DataFrame) -> list[dict]:
    if frame.empty:
        return []
    return json.loads(frame.to_json(orient="records"))


def _geography_json_payload(report: Mapping[str, object]) -> dict:
    artifact_inventory = report.get("artifact_inventory")
    overlap_checks = report.get("overlap_checks")
    return {
        "summary": report.get("summary", {}),
        "artifact_inventory": _json_records(artifact_inventory)
        if isinstance(artifact_inventory, pd.DataFrame)
        else [],
        "overlap_checks": _json_records(overlap_checks)
        if isinstance(overlap_checks, pd.DataFrame)
        else [],
        "blockers": report.get("blockers", []),
        "warnings": report.get("warnings", []),
    }


def _csv_ready(frame: pd.DataFrame) -> pd.DataFrame:
    result = frame.copy()
    for column in result.columns:
        if result[column].map(lambda value: isinstance(value, list)).any():
            result[column] = result[column].map(
                lambda value: json.dumps(value, ensure_ascii=True) if isinstance(value, list) else value
            )
    return result


def geography_preflight_markdown_lines(
    report: Mapping[str, object],
    *,
    year: int,
    standalone: bool = False,
) -> list[str]:
    summary = dict(report.get("summary", {}))
    artifact_inventory = report.get("artifact_inventory")
    overlap_checks = report.get("overlap_checks")
    blockers = list(report.get("blockers", []))
    warnings = list(report.get("warnings", []))

    lines: list[str] = []
    if standalone:
        lines.extend([f"# Private Car Station Charging Curves {year} Data Quality Report", ""])

    lines.extend(
        [
            "## Geography Consistency Preflight",
            "",
            f"- status: `{summary.get('status', 'unknown')}`",
            f"- fail_fast: `{summary.get('fail_fast', False)}`",
            f"- blocker_count: `{summary.get('blocker_count', 0)}`",
            f"- warning_count: `{summary.get('warning_count', 0)}`",
            f"- Scotland EV home_lsoa geography: `{summary.get('scotland_ev_home_lsoa_geography_version', 'unknown')}`",
            f"- Scotland station geography: `{summary.get('scotland_station_geography_version', 'unknown')}`",
            f"- Scotland centroid geography: `{summary.get('scotland_centroid_geography_version', 'unknown')}`",
            f"- Scotland destination origin geography: `{summary.get('scotland_destination_origin_geography_version', 'unknown')}`",
            f"- Scotland destination dest geography: `{summary.get('scotland_destination_dest_geography_version', 'unknown')}`",
            f"- Scotland final geography version: `{summary.get('scotland_geography_final_version', 'unknown')}`",
            f"- crosswalk used: `{summary.get('crosswalk_used', False)}`",
        ]
    )
    if summary.get("crosswalk_used"):
        lines.extend(
            [
                f"- crosswalk method: `{summary.get('crosswalk_method', '')}`",
                f"- crosswalk rows: `{summary.get('crosswalk_rows', '')}`",
                f"- Scotland rows reassigned to DZ2022: `{summary.get('scotland_rows_reassigned_to_dz2022', 0)}`",
                f"- Scotland rows unmapped to DZ2022: `{summary.get('scotland_rows_unmapped_to_dz2022', 0)}`",
                f"- Scotland unique DZ2011 seen: `{summary.get('scotland_unique_dz2011_seen', 0)}`",
                f"- Scotland unique DZ2022 assigned: `{summary.get('scotland_unique_dz2022_assigned', 0)}`",
                f"- DZ2011 boundary source: `{summary.get('crosswalk_dz2011_boundary_path', '')}`",
                f"- DZ2022 boundary source: `{summary.get('crosswalk_dz2022_boundary_path', '')}`",
            ]
        )
    elif summary.get("scotland_geography_unification_reason"):
        lines.append(
            f"- Scotland geography unification reason: `{summary.get('scotland_geography_unification_reason', '')}`"
        )
    if summary.get("sample_warning"):
        lines.extend(["", f"- sample warning: {summary['sample_warning']}"])
    if summary.get("fail_fast"):
        lines.extend(
            [
                "",
                "### Blocking Result",
                "",
                SCOTLAND_FAIL_FAST_MESSAGE,
                "",
                "Scotland private-car public charging outputs are invalid / blocked until an official or otherwise verifiable DZ2011-to-DZ2022 crosswalk is applied, or the EV allocation is rebuilt directly on Scotland Data Zone 2022.",
            ]
        )

    if blockers:
        lines.extend(["", "### Geography Blockers", "", "```text"])
        lines.append(pd.DataFrame(blockers).to_string(index=False))
        lines.append("```")

    ni_warnings = [warning for warning in warnings if warning.get("country_or_prefix") == "N"]
    if ni_warnings:
        lines.extend(["", "### Northern Ireland Warnings", "", "```text"])
        lines.append(pd.DataFrame(ni_warnings).to_string(index=False))
        lines.append("```")

    other_warnings = [warning for warning in warnings if warning.get("country_or_prefix") != "N"]
    if other_warnings:
        lines.extend(["", "### Other Geography Warnings", "", "```text"])
        lines.append(pd.DataFrame(other_warnings).to_string(index=False))
        lines.append("```")

    if isinstance(artifact_inventory, pd.DataFrame) and not artifact_inventory.empty:
        display_columns = [
            "artifact_name",
            "small_area_code_column",
            "country_or_prefix",
            "scotland_geography_version",
            "code_min",
            "code_max",
            "unique_code_count",
            "example_codes",
        ]
        lines.extend(["", "### Small-Area Artifact Inventory", "", "```text"])
        lines.append(artifact_inventory.loc[:, display_columns].to_string(index=False))
        lines.append("```")

    if isinstance(overlap_checks, pd.DataFrame) and not overlap_checks.empty:
        display_columns = [
            "check_name",
            "country_or_prefix",
            "left_unique_count",
            "right_unique_count",
            "exact_overlap_count",
            "overlap_rate_left",
            "overlap_rate_right",
            "example_left_only_codes",
            "example_right_only_codes",
        ]
        lines.extend(["", "### Small-Area Overlap Checks", "", "```text"])
        lines.append(overlap_checks.loc[:, display_columns].to_string(index=False))
        lines.append("```")

    return lines


def write_preflight_geography_outputs(
    report: Mapping[str, object],
    output_dir: Path | str,
    *,
    year: int,
    append_markdown: bool = True,
) -> None:
    """Write geography preflight JSON, CSVs, and markdown report sections."""

    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)

    (out / f"preflight_geography_{year}.json").write_text(
        json.dumps(_geography_json_payload(report), ensure_ascii=True, indent=2, default=str),
        encoding="utf-8",
    )

    artifact_inventory = report.get("artifact_inventory")
    if isinstance(artifact_inventory, pd.DataFrame):
        _csv_ready(artifact_inventory).to_csv(
            out / f"preflight_small_area_artifact_inventory_{year}.csv",
            index=False,
        )
    overlap_checks = report.get("overlap_checks")
    if isinstance(overlap_checks, pd.DataFrame):
        _csv_ready(overlap_checks).to_csv(
            out / f"preflight_small_area_overlap_checks_{year}.csv",
            index=False,
        )
    if report.get("blockers"):
        pd.DataFrame(report["blockers"]).to_csv(out / f"preflight_geography_blockers_{year}.csv", index=False)
    if report.get("warnings"):
        pd.DataFrame(report["warnings"]).to_csv(out / f"preflight_geography_warnings_{year}.csv", index=False)
    scotland_crosswalk = report.get("scotland_crosswalk")
    if isinstance(scotland_crosswalk, pd.DataFrame) and not scotland_crosswalk.empty:
        _csv_ready(scotland_crosswalk).to_csv(
            out / f"scotland_dz2011_to_dz2022_area_crosswalk_{year}.csv",
            index=False,
        )

    report_path = out / "data_quality_report.md"
    standalone = not report_path.exists() or not append_markdown
    markdown = "\n".join(
        geography_preflight_markdown_lines(report, year=year, standalone=standalone)
    )
    if append_markdown and report_path.exists():
        existing = report_path.read_text(encoding="utf-8").rstrip()
        report_path.write_text(f"{existing}\n\n{markdown}\n", encoding="utf-8")
    else:
        report_path.write_text(f"{markdown}\n", encoding="utf-8")


def raise_for_geography_preflight(
    report: Mapping[str, object],
    *,
    report_path: Path | str | None = None,
) -> None:
    summary = dict(report.get("summary", {}))
    if bool(summary.get("fail_fast", False)):
        raise GeographyPreflightError(
            str(summary.get("failure_message") or SCOTLAND_FAIL_FAST_MESSAGE),
            report_path=Path(report_path) if report_path is not None else None,
        )
