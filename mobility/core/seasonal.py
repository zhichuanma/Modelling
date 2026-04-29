"""Seasonal consumption correction factor lookup (Stage 5)."""

from .constants import MONTH_TO_SEASON, SEASONAL_CONSUMPTION_FACTOR


def get_seasonal_factor(month: int) -> float:
    """Return the energy consumption multiplier for the given calendar month.

    month must be an integer in [1, 12]. Raises ValueError otherwise.
    """
    if not isinstance(month, int) or isinstance(month, bool) or month not in MONTH_TO_SEASON:
        raise ValueError(f"month must be in 1..12, got {month!r}")
    return SEASONAL_CONSUMPTION_FACTOR[MONTH_TO_SEASON[month]]
