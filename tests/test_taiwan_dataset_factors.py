from __future__ import annotations

from dataclasses import replace
from decimal import Decimal, Inexact, InvalidOperation, Rounded, getcontext
from fractions import Fraction

import pytest

from quant_research.taiwan.dataset.events import EventRef, EventValue
from quant_research.taiwan.dataset.factors import calculate_adjustments
from quant_research.taiwan.dataset.models import QuoteValue


def ref(n: int) -> EventRef:
    return EventRef("twse", "fixture", "task", "snap", n, "2024-01-01")


def ev(
    kind: str,
    day: str,
    multiplier: str | None = None,
    *,
    source: str = "twse",
    symbol: str = "2330",
    dividend: str = "0",
    bonus: str | None = None,
    cash_return: str = "0",
    before: str | None = None,
    reference: str | None = None,
    record: int = 1,
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
        ref(record),
    )


def quote(
    day: str,
    *,
    source: str = "twse",
    symbol: str = "2330",
    raw: str = "100",
    ohlc: tuple[Decimal | None, Decimal | None, Decimal | None, Decimal | None] | None = None,
) -> QuoteValue:
    values = ohlc or (Decimal(raw), Decimal(raw), Decimal(raw), Decimal(raw))
    return QuoteValue(
        source, symbol, day, *values, Decimal("10"), Decimal("20"), (f"{source}:{symbol}:{day}",)
    )


def test_five_quote_two_exchange_event_example_and_event_day_before_ohlc() -> None:
    quotes = [
        quote(f"2024-01-{day:02}", raw=str(raw))
        for day, raw in zip((1, 2, 3, 4, 5), (100, 102, 51, 53, 106), strict=True)
    ]
    events = [ev("capital_reduction", "2024-01-03", "2"), ev("par_value", "2024-01-05", "0.5")]
    rows = calculate_adjustments(quotes, events)
    assert [r.close for r in rows] == list(map(Decimal, ("100", "102", "102", "106", "106")))
    assert [r.factor for r in rows] == list(map(Decimal, ("1", "1", "2", "2", "1")))


def test_capital_ratio_cash_irrelevant_and_dividend_bonus_leave_factor_unchanged() -> None:
    quotes = [quote("2024-01-01"), quote("2024-01-02"), quote("2024-01-03")]
    rows = calculate_adjustments(
        quotes,
        [
            ev("capital_reduction", "2024-01-02", "0.94", cash_return="0.6", dividend="0.1"),
            ev("dividend", "2024-01-03", dividend="1.5", bonus="0.2", cash_return="0.4"),
        ],
    )
    assert [r.factor for r in rows] == [Decimal(1), Decimal("0.94"), Decimal("0.94")]
    assert rows[-1].close == Decimal("94.00")


def test_first_day_prior_and_future_events_are_ignored() -> None:
    quotes = [quote("2024-01-02"), quote("2024-01-03")]
    events = [
        ev("capital_reduction", "2024-01-01", "2"),
        ev("capital_reduction", "2024-01-02", "3"),
        ev("capital_reduction", "2024-01-04", "5"),
    ]
    assert [r.factor for r in calculate_adjustments(quotes, events)] == [Decimal(1), Decimal(1)]


def test_gap_event_applies_at_next_quote_and_source_symbol_streams_are_independent() -> None:
    quotes = [
        quote("2024-01-01"),
        quote("2024-01-05"),
        quote("2024-01-01", source="tpex"),
        quote("2024-01-10"),
        quote("2024-01-01", symbol="2311"),
        quote("2024-01-05", symbol="2311"),
    ]
    events = [
        ev("capital_reduction", "2024-01-03", "2"),
        ev("capital_reduction", "2024-01-03", "3", source="tpex"),
        ev("capital_reduction", "2024-01-03", "4", symbol="2311"),
    ]
    rows = calculate_adjustments(quotes, events)
    by_key = {(r.quote.source, r.quote.symbol, r.quote.date): r.factor for r in rows}
    assert by_key[("twse", "2330", "2024-01-01")] == 1
    assert by_key[("twse", "2330", "2024-01-05")] == 2
    assert by_key[("twse", "2330", "2024-01-10")] == 2
    assert by_key[("tpex", "2330", "2024-01-01")] == 1
    assert by_key[("twse", "2311", "2024-01-01")] == 1
    assert by_key[("twse", "2311", "2024-01-05")] == 4


def test_duplicate_event_applied_once_but_conflicting_duplicate_fails() -> None:
    item = ev("capital_reduction", "2024-01-02", "2")
    duplicate = replace(item, ref=ref(2))
    rows = calculate_adjustments([quote("2024-01-01"), quote("2024-01-02")], [item, duplicate])
    assert rows[-1].factor == 2
    with pytest.raises(ValueError, match="conflicting duplicate"):
        calculate_adjustments(
            [quote("2024-01-01")], [item, replace(item, share_multiplier=Decimal(3))]
        )


def test_ohlc_volume_amount_and_refs_preserved_and_nulls_not_invented() -> None:
    raw = quote("2024-01-01", ohlc=(Decimal("10"), None, Decimal("8"), Decimal("9")))
    result = calculate_adjustments([raw], [])[0]
    assert (result.open, result.high, result.low, result.close) == (
        Decimal(10),
        None,
        Decimal(8),
        Decimal(9),
    )
    assert result.quote == raw
    assert (result.quote.volume, result.quote.amount, result.quote.refs) == (
        Decimal(10),
        Decimal(20),
        raw.refs,
    )


def test_fraction_oracle_and_fifty_significant_digit_rounding() -> None:
    quotes = [quote("2024-01-01"), quote("2024-01-02"), quote("2024-01-03")]
    events = [
        ev(
            "capital_reduction",
            "2024-01-02",
            "1.23456789012345678901234567890123456789012345678901",
        ),
        ev(
            "capital_reduction",
            "2024-01-03",
            "0.33333333333333333333333333333333333333333333333333",
        ),
    ]
    rows = calculate_adjustments(quotes, events)
    expected = Fraction(123456789012345678901234567890123456789012345678901, 10**50) * Fraction(
        33333333333333333333333333333333333333333333333333, 10**50
    )
    with __import__("decimal").localcontext() as ctx:
        ctx.prec = 50
        expected_decimal = +(Decimal(expected.numerator) / Decimal(expected.denominator))
        expected_close = +(expected_decimal * Decimal(100))
    assert rows[-1].factor == expected_decimal
    assert rows[-1].close == expected_close


def test_unsorted_inputs_sorted_and_same_day_event_applies_before_quote() -> None:
    rows = calculate_adjustments(
        [quote("2024-01-03"), quote("2024-01-01"), quote("2024-01-02")],
        [ev("capital_reduction", "2024-01-02", "2")],
    )
    assert [r.quote.date for r in rows] == ["2024-01-01", "2024-01-02", "2024-01-03"]
    assert [r.factor for r in rows] == [1, 2, 2]


def test_computed_cumulative_factor_overflow_fails_contextually() -> None:
    quotes = [
        quote("2024-01-01", raw="1e-999999"),
        quote("2024-01-02", raw="1e-999999"),
        quote("2024-01-03", raw="1e-999999"),
    ]
    events = [
        ev("capital_reduction", "2024-01-02", "1e999999"),
        ev("capital_reduction", "2024-01-03", "1e999999"),
    ]
    message = "twse/2330 date=2024-01-03.*cumulative factor must be finite"
    with pytest.raises(ValueError, match=message):
        calculate_adjustments(quotes, events)


def test_computed_adjusted_price_overflow_and_underflow_fail_contextually() -> None:
    first = quote("2024-01-01")
    high = quote("2024-01-02", raw="9e999999")
    with pytest.raises(ValueError, match="twse/2330 date=2024-01-02 field=open.*must be finite"):
        calculate_adjustments([first, high], [ev("capital_reduction", "2024-01-02", "9")])
    tiny = quote("2024-01-02", raw="1e-1000048")
    with pytest.raises(ValueError, match="twse/2330 date=2024-01-02 field=open.*must be positive"):
        calculate_adjustments([first, tiny], [ev("capital_reduction", "2024-01-02", "0.1")])


def test_calculation_is_context_independent_and_appended_data_preserves_prefix() -> None:
    ctx = getcontext()
    before = ctx.copy()
    quotes = [
        quote("2024-01-01", ohlc=(Decimal("10"), Decimal("12"), Decimal("9"), Decimal("11"))),
        quote("2024-01-02", ohlc=(Decimal("20"), Decimal("22"), Decimal("19"), Decimal("21"))),
    ]
    events = [ev("capital_reduction", "2024-01-02", "1.5")]
    try:
        ctx.prec = 2
        ctx.Emin, ctx.Emax = -2, 2
        ctx.traps[Inexact] = ctx.traps[Rounded] = ctx.traps[InvalidOperation] = True
        baseline = calculate_adjustments(quotes, events)
        extended = calculate_adjustments(
            [*quotes, quote("2024-01-03", raw="30")],
            [*events, ev("capital_reduction", "2024-01-03", "2")],
        )
        assert extended[: len(baseline)] == baseline
        assert [(row.factor, row.open, row.high, row.low, row.close) for row in baseline] == [
            (Decimal(1), Decimal(10), Decimal(12), Decimal(9), Decimal(11)),
            (Decimal("1.5"), Decimal("30.0"), Decimal("33.0"), Decimal("28.5"), Decimal("31.5")),
        ]
    finally:
        ctx.prec, ctx.rounding = before.prec, before.rounding
        ctx.Emin, ctx.Emax = before.Emin, before.Emax
        ctx.traps, ctx.flags = before.traps.copy(), before.flags.copy()


def test_invalid_nonfinite_quote_is_contextual() -> None:
    bad = quote("2024-01-01", ohlc=(Decimal("NaN"), None, None, None))
    with pytest.raises(ValueError, match="twse/2330 date=2024-01-01 field=open"):
        calculate_adjustments([bad], [])
