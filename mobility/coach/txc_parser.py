"""Helpers for turning TransXChange coach XML into tabular trip data.

The functions in this module are intentionally notebook-friendly:
they return pandas DataFrames for each intermediate layer so the user
can inspect how a raw TxC file becomes a trip table.
"""

from __future__ import annotations

from pathlib import Path
import xml.etree.ElementTree as ET

import pandas as pd

from mobility.core.txc_parser import (
    TXC_NS,
    _find_attr,
    _findtext,
    _local_name,
    parse_clock_to_seconds,
    parse_duration_to_seconds,
    seconds_to_clock,
)


def _parse_wait_seconds(elem: ET.Element | None) -> int:
    if elem is None:
        return 0
    from_wait = parse_duration_to_seconds(_findtext(elem, "tx:From/tx:WaitTime"))
    to_wait = parse_duration_to_seconds(_findtext(elem, "tx:To/tx:WaitTime"))
    return from_wait + to_wait


def _parse_operating_profile(op_elem: ET.Element | None) -> dict:
    regular_day_types: list[str] = []
    date_ranges: list[tuple[str, str, str]] = []

    if op_elem is not None:
        regular = op_elem.find("tx:RegularDayType", TXC_NS)
        if regular is not None:
            regular_day_types = [_local_name(child.tag) for child in list(regular)]

        for dr in op_elem.findall(
            ".//tx:SpecialDaysOperation/tx:DaysOfOperation/tx:DateRange", TXC_NS
        ):
            date_ranges.append(
                (
                    _findtext(dr, "tx:StartDate"),
                    _findtext(dr, "tx:EndDate"),
                    _findtext(dr, "tx:Note"),
                )
            )

    start_dates = [item[0] for item in date_ranges if item[0]]
    end_dates = [item[1] for item in date_ranges if item[1]]
    notes = [item[2] for item in date_ranges if item[2]]

    note_summary = "; ".join(notes[:3])
    if len(notes) > 3:
        note_summary += f" ... (+{len(notes) - 3} more)"

    return {
        "regular_day_types": ",".join(regular_day_types),
        "n_date_ranges": len(date_ranges),
        "first_operating_date": min(start_dates) if start_dates else "",
        "last_operating_date": max(end_dates) if end_dates else "",
        "operating_note_summary": note_summary,
    }


def parse_service_metadata(root: ET.Element, xml_path: str | Path) -> dict:
    """Extract the file-level service metadata."""
    operator = root.find("./tx:Operators/tx:Operator", TXC_NS)
    service = root.find("./tx:Services/tx:Service", TXC_NS)
    line = service.find("./tx:Lines/tx:Line", TXC_NS) if service is not None else None

    return {
        "xml_path": str(xml_path),
        "file_name": Path(xml_path).name,
        "operator_code": _findtext(operator, "tx:NationalOperatorCode"),
        "operator_name": _findtext(operator, "tx:OperatorShortName"),
        "service_code": _findtext(service, "tx:ServiceCode"),
        "line_name": _findtext(line, "tx:LineName"),
        "outbound_description": _findtext(line, "tx:OutboundDescription/tx:Description"),
        "inbound_description": _findtext(line, "tx:InboundDescription/tx:Description"),
        "origin": _findtext(service, "./tx:StandardService/tx:Origin"),
        "destination": _findtext(service, "./tx:StandardService/tx:Destination"),
    }


def parse_stop_points(root: ET.Element) -> pd.DataFrame:
    rows: list[dict] = []
    for stop in root.findall("./tx:StopPoints/tx:AnnotatedStopPointRef", TXC_NS):
        rows.append(
            {
                "stop_point_ref": _findtext(stop, "tx:StopPointRef"),
                "common_name": _findtext(stop, "tx:CommonName"),
                "indicator": _findtext(stop, "tx:Indicator"),
                "locality_name": _findtext(stop, "tx:LocalityName"),
                "locality_qualifier": _findtext(stop, "tx:LocalityQualifier"),
            }
        )
    return pd.DataFrame(rows)


def parse_route_sections(root: ET.Element) -> pd.DataFrame:
    rows: list[dict] = []
    for section in root.findall("./tx:RouteSections/tx:RouteSection", TXC_NS):
        section_id = section.attrib.get("id", "")
        for order, link in enumerate(section.findall("tx:RouteLink", TXC_NS), start=1):
            rows.append(
                {
                    "route_section_id": section_id,
                    "route_link_id": link.attrib.get("id", ""),
                    "link_order": order,
                    "from_stop_ref": _findtext(link, "tx:From/tx:StopPointRef"),
                    "to_stop_ref": _findtext(link, "tx:To/tx:StopPointRef"),
                }
            )
    return pd.DataFrame(rows)


def parse_routes(root: ET.Element) -> pd.DataFrame:
    rows: list[dict] = []
    for route in root.findall("./tx:Routes/tx:Route", TXC_NS):
        rows.append(
            {
                "route_id": route.attrib.get("id", ""),
                "description": _findtext(route, "tx:Description"),
                "route_section_ref": _findtext(route, "tx:RouteSectionRef"),
            }
        )
    return pd.DataFrame(rows)


def parse_journey_pattern_sections(root: ET.Element) -> pd.DataFrame:
    rows: list[dict] = []
    for section in root.findall("./tx:JourneyPatternSections/tx:JourneyPatternSection", TXC_NS):
        section_id = section.attrib.get("id", "")
        for order, link in enumerate(
            section.findall("tx:JourneyPatternTimingLink", TXC_NS), start=1
        ):
            rows.append(
                {
                    "journey_pattern_section_id": section_id,
                    "journey_pattern_timing_link_id": link.attrib.get("id", ""),
                    "link_order": order,
                    "from_stop_ref": _findtext(link, "tx:From/tx:StopPointRef"),
                    "to_stop_ref": _findtext(link, "tx:To/tx:StopPointRef"),
                    "from_sequence_number": _find_attr(
                        link, "tx:From", "SequenceNumber"
                    ),
                    "to_sequence_number": _find_attr(
                        link, "tx:To", "SequenceNumber"
                    ),
                    "from_activity": _findtext(link, "tx:From/tx:Activity"),
                    "to_activity": _findtext(link, "tx:To/tx:Activity"),
                    "route_link_ref": _findtext(link, "tx:RouteLinkRef"),
                    "base_runtime_s": parse_duration_to_seconds(
                        _findtext(link, "tx:RunTime")
                    ),
                    "base_wait_s": _parse_wait_seconds(link),
                }
            )
    df = pd.DataFrame(rows)
    if not df.empty:
        df["from_sequence_number"] = pd.to_numeric(
            df["from_sequence_number"], errors="coerce"
        )
        df["to_sequence_number"] = pd.to_numeric(
            df["to_sequence_number"], errors="coerce"
        )
    return df


def parse_journey_patterns(root: ET.Element) -> pd.DataFrame:
    rows: list[dict] = []
    for pattern in root.findall(
        "./tx:Services/tx:Service/tx:StandardService/tx:JourneyPattern", TXC_NS
    ):
        refs = _findtext(pattern, "tx:JourneyPatternSectionRefs").split()
        rows.append(
            {
                "journey_pattern_id": pattern.attrib.get("id", ""),
                "destination_display": _findtext(pattern, "tx:DestinationDisplay"),
                "direction": _findtext(pattern, "tx:Direction"),
                "description": _findtext(pattern, "tx:Description"),
                "route_ref": _findtext(pattern, "tx:RouteRef"),
                "journey_pattern_section_refs": refs,
            }
        )
    return pd.DataFrame(rows)


def parse_vehicle_journeys(root: ET.Element) -> pd.DataFrame:
    rows: list[dict] = []
    for vehicle_journey in root.findall("./tx:VehicleJourneys/tx:VehicleJourney", TXC_NS):
        profile = _parse_operating_profile(vehicle_journey.find("tx:OperatingProfile", TXC_NS))
        rows.append(
            {
                "vehicle_journey_code": _findtext(vehicle_journey, "tx:VehicleJourneyCode"),
                "private_code": _findtext(vehicle_journey, "tx:PrivateCode"),
                "service_ref": _findtext(vehicle_journey, "tx:ServiceRef"),
                "line_ref": _findtext(vehicle_journey, "tx:LineRef"),
                "journey_pattern_ref": _findtext(vehicle_journey, "tx:JourneyPatternRef"),
                "departure_time": _findtext(vehicle_journey, "tx:DepartureTime"),
                "departure_seconds": parse_clock_to_seconds(
                    _findtext(vehicle_journey, "tx:DepartureTime")
                ),
                **profile,
            }
        )
    return pd.DataFrame(rows)


def parse_vehicle_journey_timing_links(root: ET.Element) -> pd.DataFrame:
    rows: list[dict] = []
    for vehicle_journey in root.findall("./tx:VehicleJourneys/tx:VehicleJourney", TXC_NS):
        vj_code = _findtext(vehicle_journey, "tx:VehicleJourneyCode")
        for order, timing_link in enumerate(
            vehicle_journey.findall("tx:VehicleJourneyTimingLink", TXC_NS), start=1
        ):
            rows.append(
                {
                    "vehicle_journey_code": vj_code,
                    "vehicle_journey_timing_link_id": timing_link.attrib.get("id", ""),
                    "link_order": order,
                    "journey_pattern_timing_link_ref": _findtext(
                        timing_link, "tx:JourneyPatternTimingLinkRef"
                    ),
                    "runtime_s_override": parse_duration_to_seconds(
                        _findtext(timing_link, "tx:RunTime")
                    ),
                    "wait_s_override": _parse_wait_seconds(timing_link),
                }
            )
    return pd.DataFrame(rows)


def load_txc_components(xml_path: str | Path) -> dict[str, object]:
    """Parse a TxC file into notebook-friendly DataFrames."""
    xml_path = Path(xml_path)
    root = ET.parse(xml_path).getroot()
    return {
        "metadata": parse_service_metadata(root, xml_path),
        "stop_points": parse_stop_points(root),
        "route_sections": parse_route_sections(root),
        "routes": parse_routes(root),
        "journey_pattern_sections": parse_journey_pattern_sections(root),
        "journey_patterns": parse_journey_patterns(root),
        "vehicle_journeys": parse_vehicle_journeys(root),
        "vehicle_journey_timing_links": parse_vehicle_journey_timing_links(root),
    }


def build_journey_pattern_link_table(components: dict[str, object]) -> pd.DataFrame:
    """Flatten JourneyPattern -> JourneyPatternSection -> TimingLink references."""
    patterns = components["journey_patterns"].copy()
    sections = components["journey_pattern_sections"].copy()

    section_groups = {
        key: group.sort_values("link_order")
        for key, group in sections.groupby("journey_pattern_section_id")
    }

    rows: list[dict] = []
    for _, pattern in patterns.iterrows():
        refs = pattern["journey_pattern_section_refs"]
        global_order = 1
        for section_ref in refs:
            group = section_groups.get(section_ref)
            if group is None:
                continue
            for _, link in group.iterrows():
                rows.append(
                    {
                        "journey_pattern_id": pattern["journey_pattern_id"],
                        "direction": pattern["direction"],
                        "pattern_description": pattern["description"],
                        "destination_display": pattern["destination_display"],
                        "route_ref": pattern["route_ref"],
                        "journey_pattern_section_id": section_ref,
                        "journey_pattern_timing_link_id": link[
                            "journey_pattern_timing_link_id"
                        ],
                        "pattern_link_order": global_order,
                        "from_stop_ref": link["from_stop_ref"],
                        "to_stop_ref": link["to_stop_ref"],
                        "from_activity": link["from_activity"],
                        "to_activity": link["to_activity"],
                        "base_runtime_s": link["base_runtime_s"],
                        "base_wait_s": link["base_wait_s"],
                    }
                )
                global_order += 1
    return pd.DataFrame(rows)


def expand_vehicle_journeys_to_timing_rows(
    components: dict[str, object],
) -> pd.DataFrame:
    """Create one row per vehicle-journey timing segment."""
    metadata = components["metadata"]
    vehicle_journeys = components["vehicle_journeys"].copy()
    journey_pattern_links = build_journey_pattern_link_table(components)
    timing_overrides = components["vehicle_journey_timing_links"].copy()

    override_lookup: dict[tuple[str, str], dict] = {}
    for _, row in timing_overrides.iterrows():
        override_lookup[(row["vehicle_journey_code"], row["journey_pattern_timing_link_ref"])] = {
            "runtime_s_override": row["runtime_s_override"],
            "wait_s_override": row["wait_s_override"],
        }

    pattern_groups = {
        key: group.sort_values("pattern_link_order")
        for key, group in journey_pattern_links.groupby("journey_pattern_id")
    }

    rows: list[dict] = []
    for _, vehicle_journey in vehicle_journeys.iterrows():
        vj_code = vehicle_journey["vehicle_journey_code"]
        pattern_ref = vehicle_journey["journey_pattern_ref"]
        pattern_rows = pattern_groups.get(pattern_ref)
        if pattern_rows is None:
            continue

        departure_seconds = vehicle_journey["departure_seconds"]
        offset_seconds = 0

        for _, link in pattern_rows.iterrows():
            override = override_lookup.get((vj_code, link["journey_pattern_timing_link_id"]), {})
            runtime_s = override.get("runtime_s_override", 0)
            wait_s = override.get("wait_s_override", 0)
            runtime_s = runtime_s if runtime_s > 0 else int(link["base_runtime_s"])
            wait_s = wait_s if wait_s > 0 else int(link["base_wait_s"])
            segment_s = runtime_s + wait_s

            segment_start_s = (departure_seconds or 0) + offset_seconds
            segment_end_s = segment_start_s + segment_s

            rows.append(
                {
                    "xml_path": metadata["xml_path"],
                    "operator_code": metadata["operator_code"],
                    "operator_name": metadata["operator_name"],
                    "service_code": metadata["service_code"],
                    "line_name": metadata["line_name"],
                    "vehicle_journey_code": vj_code,
                    "private_code": vehicle_journey["private_code"],
                    "journey_pattern_ref": pattern_ref,
                    "direction": link["direction"],
                    "pattern_description": link["pattern_description"],
                    "pattern_link_order": link["pattern_link_order"],
                    "journey_pattern_timing_link_id": link[
                        "journey_pattern_timing_link_id"
                    ],
                    "from_stop_ref": link["from_stop_ref"],
                    "to_stop_ref": link["to_stop_ref"],
                    "from_activity": link["from_activity"],
                    "to_activity": link["to_activity"],
                    "departure_time": vehicle_journey["departure_time"],
                    "segment_start_time": seconds_to_clock(segment_start_s),
                    "segment_end_time": seconds_to_clock(segment_end_s),
                    "runtime_s": runtime_s,
                    "wait_s": wait_s,
                    "segment_total_s": segment_s,
                    "regular_day_types": vehicle_journey["regular_day_types"],
                    "n_date_ranges": vehicle_journey["n_date_ranges"],
                    "first_operating_date": vehicle_journey["first_operating_date"],
                    "last_operating_date": vehicle_journey["last_operating_date"],
                    "operating_note_summary": vehicle_journey["operating_note_summary"],
                }
            )

            offset_seconds = segment_end_s - (departure_seconds or 0)

    return pd.DataFrame(rows)


def build_vehicle_journey_stop_times(
    timing_rows: pd.DataFrame,
    stop_points: pd.DataFrame,
) -> pd.DataFrame:
    """Turn segment rows into stop-level arrival times."""
    if timing_rows.empty:
        return pd.DataFrame()

    stop_lookup = stop_points.set_index("stop_point_ref").to_dict("index")
    vehicle_info = (
        timing_rows.sort_values(["vehicle_journey_code", "pattern_link_order"])
        .groupby("vehicle_journey_code")
    )

    rows: list[dict] = []
    for vj_code, group in vehicle_info:
        first = group.iloc[0]
        stop_sequence = 1
        first_stop = stop_lookup.get(first["from_stop_ref"], {})
        rows.append(
            {
                "vehicle_journey_code": vj_code,
                "journey_pattern_ref": first["journey_pattern_ref"],
                "direction": first["direction"],
                "stop_sequence": stop_sequence,
                "stop_point_ref": first["from_stop_ref"],
                "common_name": first_stop.get("common_name", ""),
                "locality_name": first_stop.get("locality_name", ""),
                "time": first["departure_time"],
            }
        )

        for _, link in group.iterrows():
            stop_sequence += 1
            stop_info = stop_lookup.get(link["to_stop_ref"], {})
            rows.append(
                {
                    "vehicle_journey_code": vj_code,
                    "journey_pattern_ref": link["journey_pattern_ref"],
                    "direction": link["direction"],
                    "stop_sequence": stop_sequence,
                    "stop_point_ref": link["to_stop_ref"],
                    "common_name": stop_info.get("common_name", ""),
                    "locality_name": stop_info.get("locality_name", ""),
                    "time": link["segment_end_time"],
                }
            )

    return pd.DataFrame(rows)


def build_trip_table_from_xml(xml_path: str | Path) -> pd.DataFrame:
    """Create a one-row-per-vehicle-journey trip table from one TxC XML."""
    components = load_txc_components(xml_path)
    timing_rows = expand_vehicle_journeys_to_timing_rows(components)
    stop_times = build_vehicle_journey_stop_times(timing_rows, components["stop_points"])
    metadata = components["metadata"]
    vehicle_journeys = components["vehicle_journeys"].copy()

    if stop_times.empty:
        return pd.DataFrame()

    summaries: list[dict] = []
    grouped_stops = {
        key: group.sort_values("stop_sequence")
        for key, group in stop_times.groupby("vehicle_journey_code")
    }
    grouped_links = {
        key: group.sort_values("pattern_link_order")
        for key, group in timing_rows.groupby("vehicle_journey_code")
    }
    vj_lookup = vehicle_journeys.set_index("vehicle_journey_code").to_dict("index")

    for vj_code, stop_group in grouped_stops.items():
        link_group = grouped_links.get(vj_code)
        if link_group is None:
            continue
        vj = vj_lookup.get(vj_code, {})
        first_stop = stop_group.iloc[0]
        last_stop = stop_group.iloc[-1]
        total_runtime_s = int(link_group["segment_total_s"].sum())

        summaries.append(
            {
                "xml_path": metadata["xml_path"],
                "file_name": metadata["file_name"],
                "operator_code": metadata["operator_code"],
                "operator_name": metadata["operator_name"],
                "service_code": metadata["service_code"],
                "line_name": metadata["line_name"],
                "vehicle_journey_code": vj_code,
                "private_code": vj.get("private_code", ""),
                "journey_pattern_ref": first_stop["journey_pattern_ref"],
                "direction": first_stop["direction"],
                "pattern_description": link_group.iloc[0]["pattern_description"],
                "departure_time": first_stop["time"],
                "arrival_time": last_stop["time"],
                "start_stop_ref": first_stop["stop_point_ref"],
                "start_stop_name": first_stop["common_name"],
                "end_stop_ref": last_stop["stop_point_ref"],
                "end_stop_name": last_stop["common_name"],
                "n_stops": int(stop_group["stop_sequence"].max()),
                "n_segments": int(link_group["pattern_link_order"].max()),
                "runtime_min": total_runtime_s / 60.0,
                "regular_day_types": vj.get("regular_day_types", ""),
                "n_date_ranges": vj.get("n_date_ranges", 0),
                "first_operating_date": vj.get("first_operating_date", ""),
                "last_operating_date": vj.get("last_operating_date", ""),
                "operating_note_summary": vj.get("operating_note_summary", ""),
            }
        )

    return pd.DataFrame(summaries)


def build_trip_table_from_inventory(
    inventory: pd.DataFrame,
    coach_root: str | Path,
) -> pd.DataFrame:
    """Batch-convert inventory rows into one combined trip table."""
    coach_root = Path(coach_root)
    tables: list[pd.DataFrame] = []

    for _, row in inventory.iterrows():
        xml_path = coach_root / row["FilePath"]
        if not xml_path.exists():
            continue
        trip_table = build_trip_table_from_xml(xml_path)
        if trip_table.empty:
            continue
        for col in inventory.columns:
            trip_table[col] = row[col]
        tables.append(trip_table)

    if not tables:
        return pd.DataFrame()
    return pd.concat(tables, ignore_index=True)
