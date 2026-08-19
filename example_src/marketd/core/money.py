"""Exact money arithmetic on scaled integers.

Rule one, and it is not negotiable: **never float**.  ``0.1 + 0.2`` is not
``0.3``, and a venue that loses a satoshi per trade loses a lot of satoshis.

Rule two is **Decimal at the edges, int in the middle**.  Values arrive as
decimal strings, are converted to scaled integers on the way in, and are
converted back on the way out.  A "unit" is the smallest representable amount at
a given decimal exponent: at ``exp=2``, ``12345`` units means ``123.45``.

The usual justification for rule two is "Decimal is slow".  Measured, on
CPython 3.13, that is mostly folklore - ``_decimal`` is a C extension and a bare
multiply is *faster* than the integer path here::

    dec: bare multiply (no rounding)          46 ns
    int: notional_units (exact, rounds)      153 ns
    dec: multiply + quantize to 8dp          177 ns   <- the fair comparison
    int: accumulate 20 fills                3031 ns
    dec: accumulate 20 fills                4376 ns

So the honest reasons to prefer scaled integers are these, in order:

* **There is no context to get wrong.**  Decimal arithmetic carries a precision
  and a rounding mode from a thread-local context.  Integer arithmetic has one
  behaviour, and :func:`mul_div` makes the rounding decision explicit and local
  at the only place it can occur.
* **Equality and hashing are trivially correct.**  ``Decimal("1.50") ==
  Decimal("1.5")`` is true but they are different objects with different
  ``as_tuple()`` - which matters the moment a price becomes a dict key.
* **Memory.**  32 bytes per int against 120 per Decimal, on every resting order
  in the book.
* **Speed, modestly.**  About 1.4x on accumulation - real, but the smallest of
  the four reasons.

That ordering is the point.  Reproduce the numbers with ``python bench.py money``
before believing any of it, including this.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation

# Balances and notionals are carried at a fixed, generous precision so that
# instruments with different price/quantity exponents share one ledger scale.
MONEY_EXP = 8
MONEY_ONE = 10**MONEY_EXP

# Powers of ten get looked up constantly; a small table beats ``10 ** n``.
_POW10: tuple[int, ...] = tuple(10**i for i in range(19))


def pow10(exp: int) -> int:
    return _POW10[exp] if 0 <= exp < len(_POW10) else 10**exp


def to_units(value: Decimal | str | int, exp: int) -> int:
    """Convert a decimal-ish value to scaled integer units.

    Raises if the value carries more precision than ``exp`` allows: silently
    rounding a user's price is a bug that only shows up in a reconciliation
    report weeks later.
    """
    if isinstance(value, int) and not isinstance(value, bool):
        dec = Decimal(value)
    else:
        try:
            dec = Decimal(str(value))
        except (InvalidOperation, ValueError) as exc:
            raise ValueError(f"not a decimal value: {value!r}") from exc
    if not dec.is_finite():
        raise ValueError(f"not a finite value: {value!r}")
    scaled = dec * pow10(exp)
    integral = int(scaled)
    if scaled != integral:
        raise ValueError(f"{value} has more than {exp} decimal places")
    return integral


def from_units(units: int, exp: int) -> Decimal:
    """Inverse of :func:`to_units`.  Exact, no rounding."""
    return Decimal(units).scaleb(-exp)


def format_units(units: int, exp: int) -> str:
    """Fixed-point string with exactly ``exp`` decimals - stable for wire use."""
    sign = "-" if units < 0 else ""
    units = abs(units)
    if exp == 0:
        return f"{sign}{units}"
    scale = pow10(exp)
    whole, frac = divmod(units, scale)
    return f"{sign}{whole}.{frac:0{exp}d}"


def mul_div(a: int, b: int, denominator: int) -> int:
    """``a * b / denominator`` with half-up rounding, in exact integer math.

    Python ints are arbitrary precision, so the intermediate product cannot
    overflow - the reason this is three lines here and a minefield in C.
    """
    if denominator <= 0:
        raise ValueError("denominator must be positive")
    product = a * b
    if product < 0:
        return -((-product * 2 + denominator) // (2 * denominator))
    return (product * 2 + denominator) // (2 * denominator)


# --- HOT PATH -------------------------------------------------------------
# Runs once per fill, and again per fill inside settlement.
def notional_units(price_units: int, price_exp: int, qty_units: int, qty_exp: int) -> int:
    """Value of ``qty`` at ``price``, expressed in ledger units (``MONEY_EXP``).

        price * qty  ==  (p / 10^pe) * (q / 10^qe)
                     ==  p * q * 10^(MONEY_EXP - pe - qe)  ledger units
    """
    shift = MONEY_EXP - price_exp - qty_exp
    if shift >= 0:
        return price_units * qty_units * pow10(shift)
    return mul_div(price_units, qty_units, pow10(-shift))


def apply_bps(amount_units: int, bps: int) -> int:
    """Basis points of an amount, rounded half-up.  ``100 bps == 1%``."""
    return mul_div(amount_units, bps, 10_000)


def bump_bps(value: int, bps: int) -> int:
    """Scale ``value`` up by ``bps`` basis points (used for price bands)."""
    return value + mul_div(value, bps, 10_000)
