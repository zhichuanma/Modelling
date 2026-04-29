"""Stage 2a coverage for frozen UK holiday rules."""

from __future__ import annotations

from copy import deepcopy
import datetime as dt
import importlib

import numpy as np
import pytest

holiday_rules = importlib.import_module("mobility.cars.holiday_rules")

UK_BANK_HOLIDAYS_2025_2026 = holiday_rules.UK_BANK_HOLIDAYS_2025_2026
UK_SCHOOL_TERMS_2025_2026 = holiday_rules.UK_SCHOOL_TERMS_2025_2026
apply_holiday_chain_transform = holiday_rules.apply_holiday_chain_transform
is_holiday_week = holiday_rules.is_holiday_week


def test_bank_holidays_schema() -> None:
    expected_regions = {"england", "wales", "scotland", "ni"}
    assert set(UK_BANK_HOLIDAYS_2025_2026) == expected_regions

    for region, holidays in UK_BANK_HOLIDAYS_2025_2026.items():
        assert isinstance(holidays, frozenset)
        assert dt.date(2025, 1, 1) in holidays, region
        assert dt.date(2026, 1, 1) in holidays, region
        assert dt.date(2025, 12, 25) in holidays, region
        assert dt.date(2026, 12, 25) in holidays, region

    assert dt.date(2025, 5, 5) in UK_BANK_HOLIDAYS_2025_2026["england"]


def test_is_holiday_week_christmas() -> None:
    assert is_holiday_week(dt.date(2025, 12, 22)) is True
    assert is_holiday_week(dt.date(2025, 11, 17)) is False


def test_is_holiday_week_half_term() -> None:
    assert is_holiday_week(dt.date(2026, 2, 16)) is True


def test_apply_transform_drops_work() -> None:
    chain = [
        (8.0, 9.0, 10.0, "home", "work"),
        (9.5, 10.0, 5.0, "work", "work"),
        (10.5, 11.0, 5.0, "work", "work"),
        (11.5, 12.0, 5.0, "work", "work"),
        (12.5, 13.0, 5.0, "work", "work"),
    ]
    rng = np.random.default_rng(0)
    drop_ratios = []

    for _ in range(500):
        transformed = apply_holiday_chain_transform(chain, leisure_pool=[], rng=rng)
        drop_ratios.append(1.0 - (len(transformed) / len(chain)))

    assert float(np.mean(drop_ratios)) == pytest.approx(0.85, abs=0.1)


def test_apply_transform_inserts_leisure() -> None:
    chain = [(8.0, 9.0, 6.0, "home", "leisure")]
    leisure_pool = [(14.0, 15.0, 8.0, "home", "leisure")]

    transformed = apply_holiday_chain_transform(
        chain,
        leisure_pool=leisure_pool,
        rng=np.random.default_rng(1),
    )

    assert sum(1 for *_rest, purpose_to in transformed if purpose_to == "leisure") == 2


def test_apply_transform_shift_and_clamp() -> None:
    transformed = apply_holiday_chain_transform(
        [(23.0, 23.2, 4.0, "home", "leisure")],
        leisure_pool=[],
        rng=np.random.default_rng(2),
    )

    dep, arr, *_rest = transformed[0]
    assert dep == pytest.approx(23.75)
    # 23.80 > 23.75 is intentional: the arr > dep invariant overrides the
    # jitter soft cap and mirrors trip_chain.chain_to_daily_schedule.
    assert arr == pytest.approx(23.80)


def test_no_mutation() -> None:
    chain = [
        (8.0, 9.0, 10.0, "home", "work"),
        (18.0, 19.0, 7.0, "work", "leisure"),
    ]
    leisure_pool = [(13.0, 14.0, 6.0, "home", "holiday")]
    chain_before = deepcopy(chain)
    leisure_pool_before = deepcopy(leisure_pool)

    _ = apply_holiday_chain_transform(
        chain,
        leisure_pool=leisure_pool,
        rng=np.random.default_rng(3),
    )

    assert chain == chain_before
    assert leisure_pool == leisure_pool_before


def test_school_terms_schema() -> None:
    assert UK_SCHOOL_TERMS_2025_2026
    assert all(
        kind in {"summer", "christmas", "easter", "half_term"}
        for _, _, kind in UK_SCHOOL_TERMS_2025_2026
    )
