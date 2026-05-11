"""Shared TransXChange XML parsing helpers.

This module intentionally contains only small, dependency-light utilities so
bus and coach code can parse TxC files without importing across package
boundaries.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET

import numpy as np


TXC_NS = {"tx": "http://www.transxchange.org.uk/"}
_DURATION_RE = re.compile(
    r"^P(?:T(?:(?P<h>\d+)H)?(?:(?P<m>\d+)M)?(?:(?P<s>\d+)S)?)?$"
)


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def _findtext(elem: ET.Element | None, path: str, default: str = "") -> str:
    if elem is None:
        return default
    text = elem.findtext(path, default=default, namespaces=TXC_NS)
    return text if text is not None else default


def _find_attr(
    elem: ET.Element | None,
    path: str,
    attr_name: str,
    default: str = "",
) -> str:
    if elem is None:
        return default
    child = elem.find(path, TXC_NS)
    if child is None:
        return default
    return child.attrib.get(attr_name, default)


def parse_clock_to_seconds(value: str) -> int | None:
    """Convert HH:MM:SS into seconds after midnight."""
    if not isinstance(value, str) or not value:
        return None
    parts = value.split(":")
    if len(parts) != 3:
        return None
    try:
        hours, minutes, seconds = (int(part) for part in parts)
    except ValueError:
        return None
    return hours * 3600 + minutes * 60 + seconds


def seconds_to_clock(value: int | float | None) -> str | None:
    """Convert seconds after midnight back to HH:MM:SS."""
    if value is None or (isinstance(value, float) and np.isnan(value)):
        return None
    total = int(round(float(value)))
    hours = total // 3600
    minutes = (total % 3600) // 60
    seconds = total % 60
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def parse_duration_to_seconds(value: str) -> int:
    """Convert an ISO-8601 TxC runtime like PT2H15M0S into seconds."""
    if not isinstance(value, str) or not value:
        return 0
    match = _DURATION_RE.match(value.strip())
    if not match:
        return 0
    hours = int(match.group("h") or 0)
    minutes = int(match.group("m") or 0)
    seconds = int(match.group("s") or 0)
    return hours * 3600 + minutes * 60 + seconds
