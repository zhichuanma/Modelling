"""Shared helpers for reading row-like containers (Series, dict, attr-objects)."""

from __future__ import annotations

from typing import Any

import pandas as pd


def field(row: Any, key: str, default: Any = None) -> Any:
    """Look up ``key`` on a pandas Series, mapping, or attribute-bearing object."""
    if isinstance(row, pd.Series):
        return row.get(key, default)
    if isinstance(row, dict):
        return row.get(key, default)
    return getattr(row, key, default)
