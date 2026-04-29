"""Hardcoded UK holiday rules for Stage 2a (no runtime wiring yet).

Bank holiday dates are copied from GOV.UK's UK bank holidays page for calendar
years 2025 and 2026. England school-holiday periods use one representative
English local-authority calendar (Royal Borough of Greenwich 2024/25, 2025/26,
and 2026/27), which is the approximation referenced by GOV.UK school-term
guidance. Scotland/Wales/Northern Ireland term-date differences are
intentionally out of scope for this stage, so school-holiday periods are shared
across regions while bank holidays remain region-specific.
"""

from __future__ import annotations

import datetime as dt

import numpy as np

HOLIDAY_CHAIN_SHIFT_HOURS = 1.0
HOLIDAY_CHAIN_SOFT_CAP_H = 23.75
HOLIDAY_CHAIN_MIN_DURATION_H = 0.05

WORKLIKE_PURPOSES = frozenset({"work", "education"})
LEISURELIKE_PURPOSES = frozenset({"leisure", "holiday"})

_ENGLAND_WALES_BANK_HOLIDAYS = frozenset(
    [
        dt.date(2025, 1, 1),  # New Year's Day
        dt.date(2025, 4, 18),  # Good Friday
        dt.date(2025, 4, 21),  # Easter Monday
        dt.date(2025, 5, 5),  # Early May bank holiday
        dt.date(2025, 5, 26),  # Spring bank holiday
        dt.date(2025, 8, 25),  # Summer bank holiday
        dt.date(2025, 12, 25),  # Christmas Day
        dt.date(2025, 12, 26),  # Boxing Day
        dt.date(2026, 1, 1),  # New Year's Day
        dt.date(2026, 4, 3),  # Good Friday
        dt.date(2026, 4, 6),  # Easter Monday
        dt.date(2026, 5, 4),  # Early May bank holiday
        dt.date(2026, 5, 25),  # Spring bank holiday
        dt.date(2026, 8, 31),  # Summer bank holiday
        dt.date(2026, 12, 25),  # Christmas Day
        dt.date(2026, 12, 28),  # Boxing Day (substitute day)
    ]
)

_SCOTLAND_BANK_HOLIDAYS = frozenset(
    [
        dt.date(2025, 1, 1),  # New Year's Day
        dt.date(2025, 1, 2),  # 2nd January
        dt.date(2025, 4, 18),  # Good Friday
        dt.date(2025, 5, 5),  # Early May bank holiday
        dt.date(2025, 5, 26),  # Spring bank holiday
        dt.date(2025, 8, 4),  # Summer bank holiday
        dt.date(2025, 12, 1),  # St Andrew's Day (substitute day)
        dt.date(2025, 12, 25),  # Christmas Day
        dt.date(2025, 12, 26),  # Boxing Day
        dt.date(2026, 1, 1),  # New Year's Day
        dt.date(2026, 1, 2),  # 2nd January
        dt.date(2026, 4, 3),  # Good Friday
        dt.date(2026, 5, 4),  # Early May bank holiday
        dt.date(2026, 5, 25),  # Spring bank holiday
        dt.date(2026, 8, 3),  # Summer bank holiday
        dt.date(2026, 11, 30),  # St Andrew's Day
        dt.date(2026, 12, 25),  # Christmas Day
        dt.date(2026, 12, 28),  # Boxing Day (substitute day)
    ]
)

_NORTHERN_IRELAND_BANK_HOLIDAYS = frozenset(
    [
        dt.date(2025, 1, 1),  # New Year's Day
        dt.date(2025, 3, 17),  # St Patrick's Day
        dt.date(2025, 4, 18),  # Good Friday
        dt.date(2025, 4, 21),  # Easter Monday
        dt.date(2025, 5, 5),  # Early May bank holiday
        dt.date(2025, 5, 26),  # Spring bank holiday
        dt.date(2025, 7, 14),  # Battle of the Boyne (substitute day)
        dt.date(2025, 8, 25),  # Summer bank holiday
        dt.date(2025, 12, 25),  # Christmas Day
        dt.date(2025, 12, 26),  # Boxing Day
        dt.date(2026, 1, 1),  # New Year's Day
        dt.date(2026, 3, 17),  # St Patrick's Day
        dt.date(2026, 4, 3),  # Good Friday
        dt.date(2026, 4, 6),  # Easter Monday
        dt.date(2026, 5, 4),  # Early May bank holiday
        dt.date(2026, 5, 25),  # Spring bank holiday
        dt.date(2026, 7, 13),  # Battle of the Boyne (substitute day)
        dt.date(2026, 8, 31),  # Summer bank holiday
        dt.date(2026, 12, 25),  # Christmas Day
        dt.date(2026, 12, 28),  # Boxing Day (substitute day)
    ]
)

# Representative England school-holiday windows taken from Royal Borough of
# Greenwich term dates linked from GOV.UK school-term guidance:
# - summer: late July to end/early September
# - christmas: 2 weeks
# - easter: 2 weeks
# - half_term: 1 week (spring, late May, autumn)
UK_SCHOOL_TERMS_2025_2026: list[tuple[dt.date, dt.date, str]] = [
    (dt.date(2025, 2, 17), dt.date(2025, 2, 21), "half_term"),
    (dt.date(2025, 4, 7), dt.date(2025, 4, 21), "easter"),
    (dt.date(2025, 5, 26), dt.date(2025, 5, 30), "half_term"),
    (dt.date(2025, 7, 23), dt.date(2025, 8, 31), "summer"),
    (dt.date(2025, 10, 27), dt.date(2025, 10, 31), "half_term"),
    (dt.date(2025, 12, 22), dt.date(2026, 1, 2), "christmas"),
    (dt.date(2026, 2, 16), dt.date(2026, 2, 20), "half_term"),
    (dt.date(2026, 3, 30), dt.date(2026, 4, 10), "easter"),
    (dt.date(2026, 5, 25), dt.date(2026, 5, 29), "half_term"),
    (dt.date(2026, 7, 21), dt.date(2026, 9, 1), "summer"),
    (dt.date(2026, 10, 26), dt.date(2026, 10, 30), "half_term"),
    (dt.date(2026, 12, 21), dt.date(2027, 1, 1), "christmas"),
]

UK_BANK_HOLIDAYS_2025_2026: dict[str, frozenset[dt.date]] = {
    "england": _ENGLAND_WALES_BANK_HOLIDAYS,
    "wales": _ENGLAND_WALES_BANK_HOLIDAYS,
    "scotland": _SCOTLAND_BANK_HOLIDAYS,
    "ni": _NORTHERN_IRELAND_BANK_HOLIDAYS,
}

_REGION_ALIASES = {
    "england": "england",
    "wales": "wales",
    "scotland": "scotland",
    "ni": "ni",
    "northern_ireland": "ni",
    "northern-ireland": "ni",
    "northern ireland": "ni",
}


def _normalise_region(region: str) -> str:
    region_key = region.strip().lower()
    if region_key not in _REGION_ALIASES:
        raise ValueError(f"Unsupported UK holiday region: {region}")
    return _REGION_ALIASES[region_key]


def _week_overlaps_period(
    week_start: dt.date,
    period_start: dt.date,
    period_end: dt.date,
) -> bool:
    week_end = week_start + dt.timedelta(days=6)
    return period_start <= week_end and period_end >= week_start


def _shift_and_clamp_hour(value: float) -> float:
    return max(0.0, min(HOLIDAY_CHAIN_SOFT_CAP_H, value + HOLIDAY_CHAIN_SHIFT_HOURS))


def is_holiday_week(week_start: dt.date, region: str = "england") -> bool:
    """Return True when any day in the week overlaps a bank or school holiday."""
    region_key = _normalise_region(region)
    week_days = frozenset(week_start + dt.timedelta(days=offset) for offset in range(7))

    if UK_BANK_HOLIDAYS_2025_2026[region_key].intersection(week_days):
        return True

    return any(
        _week_overlaps_period(week_start, period_start, period_end)
        for period_start, period_end, _kind in UK_SCHOOL_TERMS_2025_2026
    )


def apply_holiday_chain_transform(
    chain: list[tuple[float, float, float, str, str]],
    leisure_pool: list[tuple[float, float, float, str, str]],
    rng: np.random.Generator,
) -> list[tuple[float, float, float, str, str]]:
    """Drop most work/education trips, inject leisure trips, then shift times.

    Extra leisure/holiday trips are sampled from ``leisure_pool`` without
    replacement when possible and with replacement otherwise so the transform can
    always satisfy ``n_extra``.
    """
    filtered_chain: list[tuple[float, float, float, str, str]] = []
    for dep, arr, distance_km, purpose_from, purpose_to in chain:
        if purpose_to in WORKLIKE_PURPOSES and rng.random() >= 0.15:
            continue
        filtered_chain.append((dep, arr, distance_km, purpose_from, purpose_to))

    n_extra = sum(1 for *_rest, purpose_to in chain if purpose_to in LEISURELIKE_PURPOSES)
    augmented_chain = list(filtered_chain)

    if leisure_pool and n_extra > 0:
        replace = n_extra > len(leisure_pool)
        sampled_indices = np.atleast_1d(
            rng.choice(len(leisure_pool), size=n_extra, replace=replace)
        )
        for sampled_index in sampled_indices.tolist():
            dep, arr, distance_km, purpose_from, purpose_to = leisure_pool[int(sampled_index)]
            augmented_chain.append((dep, arr, distance_km, purpose_from, purpose_to))

    transformed_chain: list[tuple[float, float, float, str, str]] = []
    for dep, arr, distance_km, purpose_from, purpose_to in augmented_chain:
        shifted_dep = _shift_and_clamp_hour(dep)
        shifted_arr = _shift_and_clamp_hour(arr)
        # Keep the dep < arr invariant even when both values hit the 23.75 soft
        # cap; allowing 23.80 here intentionally mirrors chain_to_daily_schedule.
        if shifted_arr <= shifted_dep:
            shifted_arr = shifted_dep + HOLIDAY_CHAIN_MIN_DURATION_H
        transformed_chain.append(
            (shifted_dep, shifted_arr, distance_km, purpose_from, purpose_to)
        )

    return sorted(transformed_chain, key=lambda leg: leg[0])


__all__ = [
    "UK_BANK_HOLIDAYS_2025_2026",
    "UK_SCHOOL_TERMS_2025_2026",
    "apply_holiday_chain_transform",
    "is_holiday_week",
]
