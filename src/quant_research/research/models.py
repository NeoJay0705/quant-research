"""Market-neutral immutable records for local research inputs."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal


class ResearchError(Exception):
    """Base class for research input and data failures."""


class ResearchInputError(ResearchError, ValueError):
    """Invalid caller-supplied research input."""


class UnsupportedDataError(ResearchError):
    """Valid source data that this adapter cannot safely interpret."""


@dataclass(frozen=True, slots=True)
class Bar:
    market: str
    symbol: str
    date: str
    open: Decimal | None = None
    high: Decimal | None = None
    low: Decimal | None = None
    close: Decimal | None = None
    volume: Decimal | None = None
    amount: Decimal | None = None
    adjusted_open: Decimal | None = None
    adjusted_high: Decimal | None = None
    adjusted_low: Decimal | None = None
    adjusted_close: Decimal | None = None
    classification: str = "unknown"
    classification_reason: str = "not_provided"
    classification_refs: tuple[str, ...] = ()
    source_refs: tuple[str, ...] = ()
    quality_flags: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class CorporateAction:
    market: str
    symbol: str
    event_type: str
    effective_date: str
    dividend: Decimal | None = None
    bonus_share_ratio: Decimal | None = None
    cash_return_per_share: Decimal | None = None
    share_multiplier: Decimal | None = None
    par_before: Decimal | None = None
    par_after: Decimal | None = None
    before_price: Decimal | None = None
    reference_price: Decimal | None = None
    cash_credit_date: str | None = None
    share_available_date: str | None = None
    classification: str = "unknown"
    classification_reason: str = "not_provided"
    classification_refs: tuple[str, ...] = ()
    source_refs: tuple[str, ...] = ()
    quality_flags: tuple[str, ...] = ()
    reasons: tuple[str, ...] = ()
    payload: tuple[tuple[str, str | None], ...] = ()


@dataclass(frozen=True, slots=True)
class SourceDiagnostic:
    source: str
    dataset: str
    task_id: str
    snapshot_id: str | None
    record_count: int
    diagnostics: tuple[str, ...] = ()


def iso_date(value: object, field: str = "date") -> str:
    if not isinstance(value, str):
        raise ResearchInputError(f"{field} must be an ISO date string")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise ResearchInputError(f"{field} must be a valid ISO date") from exc
    if parsed.isoformat() != value:
        raise ResearchInputError(f"{field} must be canonical ISO date")
    return value


def validate_decimal(value: Decimal | None, field: str, *, minimum: Decimal | None = None) -> None:
    if value is None:
        return
    if not isinstance(value, Decimal) or not value.is_finite():
        raise ResearchInputError(f"{field} must be a finite Decimal or None")
    if minimum is not None and value < minimum:
        raise ResearchInputError(f"{field} must be >= {minimum}")


def validate_bar(bar: Bar) -> None:
    if not isinstance(bar, Bar):
        raise ResearchInputError("bars must contain Bar records")
    iso_date(bar.date)
    if not all(isinstance(value, str) and value for value in (bar.market, bar.symbol)):
        raise ResearchInputError("bar market and symbol must be non-empty strings")
    if not all(isinstance(value, str) for value in (bar.classification, bar.classification_reason)):
        raise ResearchInputError("bar classification metadata must be strings")
    for field in (
        "open",
        "high",
        "low",
        "close",
        "adjusted_open",
        "adjusted_high",
        "adjusted_low",
        "adjusted_close",
    ):
        validate_decimal(getattr(bar, field), field, minimum=Decimal(0))
    for field in ("volume", "amount"):
        validate_decimal(getattr(bar, field), field, minimum=Decimal(0))
    for field in ("classification_refs", "source_refs", "quality_flags"):
        values = getattr(bar, field)
        if not isinstance(values, tuple) or any(not isinstance(value, str) for value in values):
            raise ResearchInputError(f"{field} must be a tuple of strings")


def validate_action(action: CorporateAction) -> None:
    if not isinstance(action, CorporateAction):
        raise ResearchInputError("events must contain CorporateAction records")
    iso_date(action.effective_date)
    for name in ("cash_credit_date", "share_available_date"):
        if getattr(action, name) is not None:
            value = iso_date(getattr(action, name), name)
            if value < action.effective_date:
                raise ResearchInputError(f"{name} must not precede effective_date")
    if not all(
        isinstance(value, str) and value
        for value in (action.market, action.symbol, action.event_type)
    ):
        raise ResearchInputError("event market, symbol and event_type must be non-empty strings")
    if not all(
        isinstance(value, str) for value in (action.classification, action.classification_reason)
    ):
        raise ResearchInputError("event classification metadata must be strings")
    for field in (
        "dividend",
        "bonus_share_ratio",
        "cash_return_per_share",
        "share_multiplier",
        "par_before",
        "par_after",
        "before_price",
        "reference_price",
    ):
        validate_decimal(getattr(action, field), field, minimum=Decimal(0))
    for field in ("share_multiplier", "par_before", "par_after"):
        value = getattr(action, field)
        if value is not None and value == 0:
            raise ResearchInputError(f"{field} must be positive")
    for field in ("classification_refs", "source_refs", "quality_flags", "reasons"):
        values = getattr(action, field)
        if not isinstance(values, tuple) or any(not isinstance(value, str) for value in values):
            raise ResearchInputError(f"{field} must be a tuple of strings")
    if not isinstance(action.payload, tuple) or any(
        not isinstance(item, tuple)
        or len(item) != 2
        or not isinstance(item[0], str)
        or (item[1] is not None and not isinstance(item[1], str))
        for item in action.payload
    ):
        raise ResearchInputError("payload must be an immutable tuple of string pairs")
    for payload_key, payload_value in action.payload:
        if (
            payload_key in {"stop_date", "resume_date", "cash_credit_date", "share_available_date"}
            and payload_value is not None
        ):
            iso_date(payload_value, payload_key)
