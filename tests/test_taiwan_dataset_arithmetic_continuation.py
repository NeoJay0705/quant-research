from __future__ import annotations

from dataclasses import fields
from decimal import Decimal

import pytest

from quant_research.taiwan.dataset.arithmetic import process_quotes
from quant_research.taiwan.dataset.events import EventRef, EventValue
from quant_research.taiwan.dataset.models import AdjustedQuote, QuoteValue


def quote(day: str, *, symbol: str = "2330") -> QuoteValue:
    return QuoteValue("twse", symbol, day, *(Decimal("10"),) * 4, Decimal("1"), Decimal("1"))


def event(day: str, kind: str = "capital_reduction", *, multiplier: str = "0.9") -> EventValue:
    return EventValue(
        "twse",
        "2330",
        kind,
        day,
        Decimal("1"),
        None,
        Decimal("0"),
        Decimal(multiplier) if kind == "capital_reduction" else None,
        Decimal("10") if kind == "par_value" else None,
        Decimal("3") if kind == "par_value" else None,
        EventRef("twse", "fixture", "task", "snapshot", 1, "2024-01-01"),
    )


def signature(rows: tuple[AdjustedQuote, ...]) -> tuple[tuple[str, ...], ...]:
    return tuple(
        tuple(str(getattr(row, field.name)) for field in fields(row) if field.name != "quote")
        + tuple(str(getattr(row.quote, field.name)) for field in fields(row.quote))
        for row in rows
    )


def continued(parts: tuple[tuple[QuoteValue, ...], ...], events: list[EventValue]) -> None:
    output: tuple[AdjustedQuote, ...] = ()
    through: str | None = None
    factor: Decimal | None = None
    for part in parts:
        part_events = events if through is None else events
        result = process_quotes(
            part,
            part_events,
            **({} if through is None else {"initial_factor": factor, "applied_through": through}),
        )
        output += result
        through, factor = result[-1].quote.date, result[-1].factor
    all_quotes = tuple(q for part in parts for q in part)
    assert signature(output) == signature(process_quotes(all_quotes, events))


def test_continuation_no_events() -> None:
    continued(((quote("2024-01-01"),), (quote("2024-01-02"),)), [])


def test_capital_reduction_inside_second_part() -> None:
    continued(((quote("2024-01-01"),), (quote("2024-01-03"),)), [event("2024-01-03")])


def test_event_in_quote_gap() -> None:
    continued(((quote("2024-01-01"),), (quote("2024-01-04"),)), [event("2024-01-02")])


def test_event_on_first_new_quote_day() -> None:
    continued(((quote("2024-01-01"),), (quote("2024-01-02"),)), [event("2024-01-02")])


def test_chained_recurring_decimal_events_keep_rounding() -> None:
    events = [
        event("2024-01-02", "par_value"),
        event("2024-01-03"),
        event("2024-01-04", "par_value"),
    ]
    continued(
        ((quote("2024-01-01"), quote("2024-01-02")), (quote("2024-01-03"), quote("2024-01-04"))),
        events,
    )


def test_cash_events_mixed_in() -> None:
    continued(
        ((quote("2024-01-01"),), (quote("2024-01-03"),)),
        [event("2024-01-02", "dividend"), event("2024-01-03")],
    )


def test_pre_cut_event_passed_to_continuation_is_ignored() -> None:
    quotes = (quote("2024-01-01"), quote("2024-01-03"))
    a = process_quotes(quotes[:1], [event("2024-01-01")])
    b = process_quotes(
        quotes[1:], [event("2024-01-01")], initial_factor=a[-1].factor, applied_through="2024-01-01"
    )
    assert signature(a + b) == signature(process_quotes(quotes, [event("2024-01-01")]))


def test_event_at_or_before_first_quote_ignored() -> None:
    continued(((quote("2024-01-02"),), (quote("2024-01-03"),)), [event("2024-01-01")])


def test_three_consecutive_parts() -> None:
    continued(
        tuple((quote(f"2024-01-0{i}"),) for i in range(1, 4)),
        [event("2024-01-02"), event("2024-01-03")],
    )


def test_no_new_events_preserves_non_one_initial_factor() -> None:
    result = process_quotes(
        (quote("2024-01-02"),), [], initial_factor=Decimal("2"), applied_through="2024-01-01"
    )
    assert result[0].factor == Decimal("2")


@pytest.mark.parametrize(
    "factor", [Decimal("0"), Decimal("-1"), Decimal("NaN"), Decimal("Infinity")]
)
def test_invalid_initial_factor(factor: Decimal) -> None:
    with pytest.raises(ValueError, match="initial_factor"):
        process_quotes((quote("2024-01-02"),), [], initial_factor=factor)


def test_invalid_applied_through() -> None:
    with pytest.raises(ValueError, match="applied_through"):
        process_quotes((quote("2024-01-02"),), [], applied_through="not-a-date")


def test_quote_must_follow_applied_through() -> None:
    with pytest.raises(ValueError, match="every quote date must be later"):
        process_quotes((quote("2024-01-02"),), [], applied_through="2024-01-02")


def test_continuation_rejects_multiple_streams() -> None:
    with pytest.raises(ValueError, match="one source and symbol"):
        process_quotes(
            (quote("2024-01-02"), quote("2024-01-03", symbol="2317")), [], initial_factor=Decimal(1)
        )
