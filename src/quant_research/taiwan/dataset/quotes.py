"""Strict normalization of official parsed quote records."""

from __future__ import annotations

from typing import Any

from .financial import parse_decimal
from .models import DatasetError, QuoteValue
from .source import RecordResult, Scope

_FIELDS = ("open", "high", "low", "close", "volume", "amount")


def normalize_quote(
    scope: Scope, record: RecordResult, classifier: Any
) -> tuple[QuoteValue, str, str, tuple[str, ...]]:
    """Return one raw quote and its independent classification evidence."""
    payload = record.payload
    if not scope.usable or record.diagnostic or payload is None:
        raise DatasetError(
            f"{scope.source}/{scope.dataset} task={scope.task_id} record={record.record_id}: "
            f"{record.diagnostic or 'unavailable or invalid record'}"
        )
    symbol, day = payload.get("symbol"), payload.get("trade_date")
    if not isinstance(symbol, str) or not symbol or not isinstance(day, str):
        raise DatasetError(
            f"{scope.source}/{scope.dataset} record={record.record_id}: invalid identity"
        )
    values = {}
    for field in _FIELDS:
        try:
            values[field] = parse_decimal(payload.get(field))
        except (ValueError, TypeError) as exc:
            raise DatasetError(
                f"{scope.source}/{scope.dataset} symbol={symbol} date={day} "
                f"record={record.record_id} field={field}: {exc}"
            ) from exc
    quote = QuoteValue(
        scope.source,
        symbol,
        day,
        values["open"],
        values["high"],
        values["low"],
        values["close"],
        values["volume"],
        values["amount"],
        (f"{scope.dataset}:{scope.snapshot_id}:{record.record_id}",),
    )
    venue, venue_id = payload.get("venue"), payload.get("venue_id")
    ambiguous = not (
        venue_id == "primary"
        or scope.source == "twse"
        and isinstance(venue_id, str)
        and "每日收盤行情" in venue_id
    )
    ambiguous |= isinstance(venue, str) and "管理" in venue
    result = classifier.classify(
        symbol,
        payload.get("name") if isinstance(payload.get("name"), str) else None,
        ambiguous=ambiguous,
    )
    return quote, result.classification, result.reason, result.evidence_refs
