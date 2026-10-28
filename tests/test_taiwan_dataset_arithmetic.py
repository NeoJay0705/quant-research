from __future__ import annotations

from dataclasses import replace
from decimal import (
    ROUND_HALF_EVEN,
    Decimal,
    Inexact,
    InvalidOperation,
    Rounded,
    getcontext,
    localcontext,
)
from fractions import Fraction

import pytest

from quant_research.taiwan.dataset.arithmetic import event_multiplier
from quant_research.taiwan.dataset.events import EventRef, EventValue
from quant_research.taiwan.dataset.financial import parse_decimal


def event(
    kind: str,
    *,
    day: str = "2024-01-02",
    source: str = "twse",
    symbol: str = "2330",
    dividend: str = "0",
    bonus: str | None = None,
    cash_return: str = "0",
    multiplier: str | None = None,
    before: str | None = None,
    reference: str | None = None,
) -> EventValue:
    return EventValue(
        source,
        symbol,
        kind,
        day,
        Decimal(dividend),
        None if bonus is None else Decimal(bonus),
        Decimal(cash_return),
        None if multiplier is None else Decimal(multiplier),
        None if before is None else Decimal(before),
        None if reference is None else Decimal(reference),
        EventRef(source, "fixture", "task", "snapshot", 1, "2024-01-01"),
    )


def test_capital_reduction_direct_multiplier_ignores_cash() -> None:
    assert event_multiplier(
        event("capital_reduction", multiplier="0.94", cash_return="0.6", dividend="0.1")
    ) == Decimal("0.94")


def test_par_price_ratio_and_direct_multiplier_precedence() -> None:
    assert event_multiplier(event("par_value", before="58.8", reference="29.4")) == Decimal("2")
    assert event_multiplier(
        event("par_value", multiplier="3", before="58.8", reference="29.4")
    ) == Decimal("3")


def test_dividend_bonus_cash_return_do_not_produce_multiplier() -> None:
    assert (
        event_multiplier(event("dividend", dividend="1.5", bonus="0.2", cash_return="0.4")) is None
    )


@pytest.mark.parametrize("value", [Decimal("NaN"), Decimal("Infinity"), Decimal("-Infinity")])
def test_nonfinite_required_operand_fails_contextually(value: Decimal) -> None:
    item = event("capital_reduction", multiplier="2")
    item = replace(item, share_multiplier=value)
    with pytest.raises(ValueError, match=r"twse/2330 date=2024-01-02.*finite"):
        event_multiplier(item)


@pytest.mark.parametrize("multiplier", ["0", "-1"])
def test_zero_or_negative_multiplier_fails(multiplier: str) -> None:
    with pytest.raises(ValueError, match="positive"):
        event_multiplier(event("capital_reduction", multiplier=multiplier))


@pytest.mark.parametrize("before,reference", [("0", "1"), ("1", "0"), ("-1", "2")])
def test_invalid_par_division_operands_fail(before: str, reference: str) -> None:
    with pytest.raises(ValueError, match="positive"):
        event_multiplier(event("par_value", before=before, reference=reference))


@pytest.mark.parametrize(
    "before,reference,reason",
    [("1e999999", "1e-999999", "finite"), ("1e-999999", "1e999999", "positive")],
)
def test_par_ratio_computed_result_must_be_finite_and_positive(
    before: str, reference: str, reason: str
) -> None:
    message = rf"twse/2330 date=2024-01-02.*computed exchange multiplier.*{reason}"
    with pytest.raises(ValueError, match=message):
        event_multiplier(event("par_value", before=before, reference=reference))


def test_parse_decimal_remains_strict() -> None:
    for invalid in (True, 0.1, "NaN", "Infinity", ""):
        with pytest.raises(ValueError):
            parse_decimal(invalid)
    assert parse_decimal(None) is None


def test_50_digit_independent_fraction_rounding_and_external_context() -> None:
    ctx = getcontext()
    before = ctx.copy()
    try:
        ctx.prec = 2
        ctx.traps[Inexact] = ctx.traps[Rounded] = ctx.traps[InvalidOperation] = True
        result = event_multiplier(event("par_value", before="1", reference="3"))
        with localcontext() as oracle_ctx:
            oracle_ctx.prec = 50
            oracle_ctx.rounding = ROUND_HALF_EVEN
            oracle_ctx.traps[Inexact] = oracle_ctx.traps[Rounded] = False
            oracle = +(Decimal(Fraction(1, 3).numerator) / Decimal(Fraction(1, 3).denominator))
        assert result == oracle
        assert len(result.as_tuple().digits) == 50
    finally:
        ctx.prec, ctx.rounding = before.prec, before.rounding
        ctx.Emin, ctx.Emax = before.Emin, before.Emax
        ctx.traps, ctx.flags = before.traps.copy(), before.flags.copy()
