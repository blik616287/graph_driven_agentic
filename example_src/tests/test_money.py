"""Exactness is the whole point, so it is what these test."""

from __future__ import annotations

from decimal import Decimal

import pytest

from marketd.core.money import (
    MONEY_EXP,
    apply_bps,
    format_units,
    from_units,
    mul_div,
    notional_units,
    to_units,
)


@pytest.mark.parametrize(
    "value,exp,expected",
    [("123.45", 2, 12_345), ("0.00000001", 8, 1), ("1", 0, 1), ("-5.25", 2, -525), (7, 2, 700)],
)
def test_to_units_is_exact(value, exp, expected):
    assert to_units(value, exp) == expected


def test_excess_precision_is_rejected_not_rounded():
    # Silently rounding a user's price is the bug that shows up in a
    # reconciliation report six weeks later.
    with pytest.raises(ValueError, match="decimal places"):
        to_units("1.234", 2)


def test_round_trip_through_units_is_lossless():
    for raw in ("0.00000001", "99999.99", "1.5", "0"):
        value = Decimal(raw)
        assert from_units(to_units(value, MONEY_EXP), MONEY_EXP) == value


def test_float_arithmetic_would_have_been_wrong():
    """The motivating example, pinned as a test."""
    assert 0.1 + 0.2 != 0.3
    tenth, fifth = to_units("0.1", MONEY_EXP), to_units("0.2", MONEY_EXP)
    assert tenth + fifth == to_units("0.3", MONEY_EXP)


@pytest.mark.parametrize(
    "a,b,denominator,expected",
    [(5, 1, 2, 3), (-5, 1, 2, -3), (1, 1, 3, 0), (2, 1, 3, 1), (10, 3, 4, 8)],
)
def test_mul_div_rounds_half_up_symmetrically(a, b, denominator, expected):
    assert mul_div(a, b, denominator) == expected


def test_notional_matches_decimal_arithmetic():
    price, qty = "30000.50", "1.50000000"
    computed = notional_units(to_units(price, 2), 2, to_units(qty, 8), 8)
    assert from_units(computed, MONEY_EXP) == Decimal(price) * Decimal(qty)


def test_fee_is_basis_points():
    assert apply_bps(to_units("10000", MONEY_EXP), 10) == to_units("10", MONEY_EXP)
    assert apply_bps(0, 100) == 0


def test_format_units_keeps_trailing_zeros():
    # Fixed-width output is what makes responses byte-stable.
    assert format_units(12_345, 2) == "123.45"
    assert format_units(100_000_000, 8) == "1.00000000"
    assert format_units(-500, 2) == "-5.00"
