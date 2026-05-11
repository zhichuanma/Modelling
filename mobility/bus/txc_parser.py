"""Bus TransXChange garage parser for the M1 chain-mode pipeline."""

from __future__ import annotations

from pathlib import Path
import warnings
import xml.etree.ElementTree as ET

import numpy as np
import pandas as pd

from mobility.core.postcode_geocoder import (
    DEFAULT_ONSPD_LATEST_PATH,
    geocode_postcode,
    load_onspd,
)
from mobility.core.txc_parser import TXC_NS, _findtext


MODELLING_ROOT = Path(__file__).resolve().parents[2]
PROJECT_ROOT = MODELLING_ROOT.parent
DEFAULT_TXC_DIR = PROJECT_ROOT / "Data" / "EV_behavior" / "Bus_Data"

GARAGE_COLUMNS = [
    "garage_id",
    "garage_code",
    "operator_noc",
    "operator_name",
    "postcode",
    "approx_lat",
    "approx_lon",
    "source_file",
    "parse_warnings",
]


class DataQualityWarning(UserWarning):
    """Warning raised when input quality is too weak for high-confidence use."""


def _empty_garages() -> pd.DataFrame:
    return pd.DataFrame(columns=GARAGE_COLUMNS)


def _first_text_by_local_name(elem: ET.Element | None, names: tuple[str, ...]) -> str:
    if elem is None:
        return ""
    wanted = {name.lower() for name in names}
    for child in elem.iter():
        local = child.tag.rsplit("}", 1)[-1].lower()
        if local in wanted and child.text:
            return str(child.text).strip()
    return ""


def _first_float_by_local_name(elem: ET.Element | None, names: tuple[str, ...]) -> float:
    text = _first_text_by_local_name(elem, names)
    if not text:
        return float("nan")
    try:
        return float(text)
    except ValueError:
        return float("nan")


def _parse_operator(root: ET.Element) -> tuple[str, str]:
    operator = root.find("./tx:Operators/tx:Operator", TXC_NS)
    if operator is None:
        return "", ""
    noc = (
        _findtext(operator, "tx:NationalOperatorCode")
        or _findtext(operator, "tx:OperatorCode")
        or operator.attrib.get("id", "")
    )
    name = (
        _findtext(operator, "tx:OperatorShortName")
        or _findtext(operator, "tx:OperatorName")
        or _findtext(operator, "tx:TradingName")
    )
    return str(noc).strip(), str(name).strip()


def _garage_code(garage: ET.Element, row_number: int) -> str:
    return (
        garage.attrib.get("id", "")
        or _findtext(garage, "tx:GarageCode")
        or _findtext(garage, "tx:Code")
        or f"garage_{row_number:03d}"
    ).strip()


def _garage_name(garage: ET.Element) -> str:
    return (
        _findtext(garage, "tx:GarageName")
        or _findtext(garage, "tx:Name")
        or _findtext(garage, "tx:Description")
    ).strip()


def _parse_one_xml(
    xml_path: Path,
    postcode_index: dict[str, tuple[float, float]],
) -> list[dict]:
    root = ET.parse(xml_path).getroot()
    operator_noc, operator_name = _parse_operator(root)
    rows: list[dict] = []
    garages = root.findall(".//tx:Garage", TXC_NS)
    for row_number, garage in enumerate(garages, start=1):
        warnings_out: list[str] = []
        code = _garage_code(garage, row_number)
        name = _garage_name(garage)
        postcode = _first_text_by_local_name(
            garage,
            ("PostCode", "Postcode", "PostalCode"),
        )
        lat = _first_float_by_local_name(garage, ("Latitude", "Lat"))
        lon = _first_float_by_local_name(garage, ("Longitude", "Long", "Lon"))
        if not (np.isfinite(lat) and np.isfinite(lon)) and postcode:
            match = geocode_postcode(postcode, postcode_index)
            if match is None:
                warnings_out.append("postcode_geocode_failed")
            else:
                lat, lon = match
        if not (np.isfinite(lat) and np.isfinite(lon)):
            warnings_out.append("coordinates_missing")

        rows.append(
            {
                "garage_id": f"{operator_noc or 'unknown'}_{code}",
                "garage_code": code,
                "operator_noc": operator_noc,
                "operator_name": operator_name,
                "postcode": postcode,
                "approx_lat": lat,
                "approx_lon": lon,
                "source_file": str(xml_path),
                "parse_warnings": ";".join(warnings_out),
            }
        )
    return rows


def parse_txc_garages(txc_dir: Path = DEFAULT_TXC_DIR) -> pd.DataFrame:
    """Recursively parse bus TxC files for garage records.

    The parser returns an empty, correctly shaped DataFrame when no XML is
    present or all files fail. Missing coordinates are retained for audit;
    depot-registry construction decides whether to use a virtual fallback.
    """
    root_dir = Path(txc_dir)
    if not root_dir.exists():
        return _empty_garages()
    xml_paths = sorted(root_dir.rglob("*.xml"))
    if not xml_paths:
        return _empty_garages()

    postcode_index = load_onspd(DEFAULT_ONSPD_LATEST_PATH)
    rows: list[dict] = []
    failed = 0
    for xml_path in xml_paths:
        try:
            rows.extend(_parse_one_xml(xml_path, postcode_index))
        except ET.ParseError:
            failed += 1
        except OSError:
            failed += 1

    if not rows:
        return _empty_garages()

    out = pd.DataFrame(rows, columns=GARAGE_COLUMNS)
    out = (
        out.sort_values(["operator_noc", "garage_code", "source_file"], kind="stable")
        .drop_duplicates(["operator_noc", "garage_code"], keep="first")
        .reset_index(drop=True)
    )
    n_garages = int(len(out))
    n_with_coords = int(out[["approx_lat", "approx_lon"]].notna().all(axis=1).sum())
    out.attrs["n_garages_parsed"] = n_garages
    out.attrs["n_with_coords"] = n_with_coords
    out.attrs["n_without_coords"] = int(n_garages - n_with_coords)
    out.attrs["n_parse_failures"] = int(failed)
    if n_with_coords / max(1, n_garages) < 0.5:
        warnings.warn(
            "Fewer than half of parsed TxC garages have coordinates; "
            "check the postcode loader and source XML quality.",
            DataQualityWarning,
            stacklevel=2,
        )
    return out
