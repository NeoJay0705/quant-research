"""Pure sequential quote-factor processing."""

from __future__ import annotations

from collections.abc import Iterable

from .arithmetic import process_quotes
from .events import EventValue
from .models import AdjustedQuote, QuoteValue


def calculate_adjustments(
    quotes: Iterable[QuoteValue], events: Iterable[EventValue]
) -> tuple[AdjustedQuote, ...]:
    """Return sequentially adjusted rows; inputs may arrive in any order."""
    return process_quotes(quotes, events)
