"""Pure exchange multipliers and sequential, source-isolated quote adjustment."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date
from decimal import Decimal, localcontext

from .events import EventValue
from .financial import isolated_context
from .models import AdjustedQuote, QuoteValue


def _event_error(event: EventValue, reason: str) -> ValueError:
    return ValueError(
        f"{event.source}/{event.symbol} date={event.effective_date} "
        f"event={event.event_type}: {reason}"
    )


def _finite(value: Decimal | None, field: str, event: EventValue) -> Decimal:
    if value is None:
        raise _event_error(event, f"missing required {field}")
    if not value.is_finite():
        raise _event_error(event, f"{field} must be finite")
    return value


def _positive_result(value: Decimal, event: EventValue, field: str) -> Decimal:
    if not value.is_finite():
        raise _event_error(event, f"computed {field} must be finite")
    if value <= 0:
        raise _event_error(event, f"computed {field} must be positive")
    return value


def event_multiplier(event: EventValue) -> Decimal | None:
    """Return the exchange multiplier; cash/dividend/bonus inputs do not adjust prices."""
    with localcontext(isolated_context(50)):
        if event.event_type == "capital_reduction":
            multiplier = _finite(event.share_multiplier, "share_multiplier", event)
        elif event.event_type == "par_value":
            if event.share_multiplier is not None:
                multiplier = _finite(event.share_multiplier, "share_multiplier", event)
            else:
                before = _finite(event.before_price, "before_price", event)
                reference = _finite(event.reference_price, "reference_price", event)
                if before <= 0 or reference <= 0:
                    raise _event_error(event, "before_price and reference_price must be positive")
                multiplier = _positive_result(before / reference, event, "exchange multiplier")
        elif event.event_type == "dividend":
            # Validate optional financial values for finiteness, but never use them in C.
            for field, value in (
                ("dividend", event.dividend),
                ("bonus_share_ratio", event.bonus_share_ratio),
                ("cash_return_per_share", event.cash_return_per_share),
            ):
                if value is not None and not value.is_finite():
                    raise _event_error(event, f"{field} must be finite")
            return None
        else:
            raise _event_error(event, "unsupported event type")
        if multiplier <= 0:
            raise _event_error(event, "exchange multiplier must be positive")
        return _positive_result(+multiplier, event, "exchange multiplier")


def _quote_day(quote: QuoteValue) -> date:
    try:
        parsed = date.fromisoformat(quote.date)
        if parsed.isoformat() != quote.date:
            raise ValueError
        return parsed
    except ValueError as exc:
        message = f"{quote.source}/{quote.symbol} date={quote.date}: invalid quote date"
        raise ValueError(message) from exc


def _event_day(event: EventValue) -> date:
    try:
        parsed = date.fromisoformat(event.effective_date)
        if parsed.isoformat() != event.effective_date:
            raise ValueError
        return parsed
    except ValueError as exc:
        raise _event_error(event, "invalid effective date") from exc


def _deduplicate(events: Iterable[EventValue]) -> list[EventValue]:
    """Deduplicate semantic event identities; conflicting duplicates fail explicitly."""
    unique: dict[tuple[str, str, str, str], EventValue] = {}
    for event in events:
        key = (event.source, event.symbol, event.event_type, event.effective_date)
        previous = unique.get(key)
        if previous is not None:
            same_terms = (
                previous.dividend,
                previous.bonus_share_ratio,
                previous.cash_return_per_share,
                previous.share_multiplier,
                previous.before_price,
                previous.reference_price,
            ) == (
                event.dividend,
                event.bonus_share_ratio,
                event.cash_return_per_share,
                event.share_multiplier,
                event.before_price,
                event.reference_price,
            )
            if not same_terms:
                raise _event_error(event, "conflicting duplicate event identity")
            continue
        unique[key] = event
    return sorted(
        unique.values(), key=lambda e: (e.effective_date, e.source, e.symbol, e.event_type)
    )


def process_quotes(
    quotes: Iterable[QuoteValue], events: Iterable[EventValue]
) -> tuple[AdjustedQuote, ...]:
    """Process quotes in ascending date order, independently per source and symbol.

    Inputs may be unsorted. Each stream's first included quote establishes d0 and C=1;
    event updates are consumed once by a date cursor before that day's OHLC calculation.
    """
    ordered_quotes = sorted(quotes, key=lambda q: (q.source, q.symbol, _quote_day(q), q.refs))
    grouped: dict[tuple[str, str], list[EventValue]] = {}
    for event in _deduplicate(events):
        grouped.setdefault((event.source, event.symbol), []).append(event)
    result: list[AdjustedQuote] = []
    index = 0
    while index < len(ordered_quotes):
        first = ordered_quotes[index]
        stream_key = (first.source, first.symbol)
        end = index + 1
        while (
            end < len(ordered_quotes)
            and (ordered_quotes[end].source, ordered_quotes[end].symbol) == stream_key
        ):
            end += 1
        stream = ordered_quotes[index:end]
        stream_events = grouped.get(stream_key, [])
        d0 = _quote_day(stream[0])
        event_index = 0
        while event_index < len(stream_events) and _event_day(stream_events[event_index]) <= d0:
            event_index += 1
        cumulative = Decimal(1)
        with localcontext(isolated_context(50)):
            for quote in stream:
                quote_day = _quote_day(quote)
                while (
                    event_index < len(stream_events)
                    and _event_day(stream_events[event_index]) <= quote_day
                ):
                    event = stream_events[event_index]
                    multiplier = event_multiplier(event)
                    if multiplier is not None:
                        cumulative = cumulative * multiplier
                        if not cumulative.is_finite():
                            raise _event_error(event, "computed cumulative factor must be finite")
                        if cumulative <= 0:
                            raise _event_error(event, "computed cumulative factor must be positive")
                        cumulative = +cumulative
                    event_index += 1
                raw = (quote.open, quote.high, quote.low, quote.close)
                adjusted: list[Decimal | None] = []
                for field, value in zip(("open", "high", "low", "close"), raw, strict=True):
                    if value is not None and not value.is_finite():
                        message = (
                            f"{quote.source}/{quote.symbol} date={quote.date} "
                            f"field={field}: non-finite raw value"
                        )
                        raise ValueError(message)
                    if value is None:
                        adjusted.append(None)
                        continue
                    adjusted_value = value * cumulative
                    if not adjusted_value.is_finite():
                        message = (
                            f"{quote.source}/{quote.symbol} date={quote.date} "
                            f"field={field}: computed adjusted price must be finite"
                        )
                        raise ValueError(message)
                    if value > 0 and adjusted_value <= 0:
                        message = (
                            f"{quote.source}/{quote.symbol} date={quote.date} "
                            f"field={field}: computed adjusted price must be positive"
                        )
                        raise ValueError(message)
                    adjusted.append(+adjusted_value)
                result.append(AdjustedQuote(quote, +cumulative, *adjusted))
        index = end
    return tuple(result)
