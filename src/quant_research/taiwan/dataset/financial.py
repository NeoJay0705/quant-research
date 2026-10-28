"""Exact, context-independent financial decimal parsing for dataset inputs."""

from __future__ import annotations

from decimal import ROUND_HALF_EVEN, Context, Decimal, InvalidOperation, localcontext
from typing import Any

MAX_DECIMAL_DIGITS = 64
MAX_DECIMAL_EXPONENT = 64
MAX_DECIMAL_TEXT = 1024


def parse_decimal(value: Any) -> Decimal | None:
    """Parse JSON financial values without accepting binary floats or non-finite values."""
    if value is None:
        return None
    if isinstance(value, bool) or isinstance(value, float):
        raise ValueError("financial_value_not_decimal_text_or_integer")
    if isinstance(value, int):
        raw = str(value)
    elif isinstance(value, str):
        raw = value.strip()
    else:
        raise ValueError("financial_value_not_decimal_text_or_integer")
    if not raw:
        raise ValueError("financial_value_empty")
    context = isolated_context()
    context.traps[InvalidOperation] = True
    try:
        with localcontext(context):
            result = Decimal(raw)
    except InvalidOperation as exc:
        raise ValueError("financial_value_invalid_decimal") from exc
    if not result.is_finite():
        raise ValueError("financial_value_nonfinite")
    tup = result.as_tuple()
    if not isinstance(tup.exponent, int):
        raise ValueError("financial_value_nonfinite")
    if len(tup.digits) > MAX_DECIMAL_DIGITS or abs(tup.exponent) > MAX_DECIMAL_EXPONENT:
        raise ValueError("financial_value_precision_or_exponent_limit")
    if len(canonical_decimal(result)) > MAX_DECIMAL_TEXT:
        raise ValueError("financial_value_storage_limit")
    return result


def canonical_decimal(value: Decimal) -> str:
    """Return exact plain decimal text, removing only insignificant fractional zeroes."""
    if not value.is_finite():
        raise ValueError("financial_value_nonfinite")
    sign, digits, exponent = value.as_tuple()
    if not isinstance(exponent, int):
        raise ValueError("financial_value_nonfinite")
    digit_count = len(digits)
    while digit_count > 1 and digits[digit_count - 1] == 0 and exponent < 0:
        digit_count -= 1
        exponent += 1
    if not any(digits):
        return "0"
    point = digit_count + exponent
    body_length = (
        digit_count + exponent
        if exponent >= 0
        else (digit_count + 1 if point > 0 else 2 - point + digit_count)
    )
    if body_length + int(bool(sign)) > MAX_DECIMAL_TEXT:
        raise ValueError("financial_value_storage_limit")
    ds = "".join(str(n) for n in digits[:digit_count])
    if exponent >= 0:
        body = ds + "0" * exponent
    else:
        point = len(ds) + exponent
        body = (ds[:point] + "." + ds[point:]) if point > 0 else ("0." + "0" * (-point) + ds)
    return ("-" if sign else "") + body


def isolated_context(precision: int = 512) -> Context:
    """A fresh deterministic context, independent of caller context and traps/flags."""
    context = Context(
        prec=precision,
        rounding=ROUND_HALF_EVEN,
        Emin=-999999,
        Emax=999999,
        capitals=1,
        clamp=0,
        flags=[],
        traps=[],
    )
    return context


def isolated_operation(function: Any, *args: Any, precision: int = 512) -> Any:
    with localcontext(isolated_context(precision)):
        return function(*args)
