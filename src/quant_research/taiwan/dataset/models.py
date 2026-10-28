"""Internal dataset errors and source selection parameters."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path


class DatasetError(Exception):
    """Invalid or unavailable dataset input."""


class SourceError(DatasetError):
    """The existing source database is unavailable or incompatible."""


class UnavailableDataError(DatasetError):
    """The selected latest source edition is not usable."""


@dataclass(frozen=True, slots=True)
class QuoteValue:
    """Internal raw quote input; date is an ISO-8601 calendar date."""

    source: str
    symbol: str
    date: str
    open: Decimal | None
    high: Decimal | None
    low: Decimal | None
    close: Decimal | None
    volume: Decimal | None
    amount: Decimal | None
    refs: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class AdjustedQuote:
    """Internal result preserving the raw quote alongside adjusted OHLC."""

    quote: QuoteValue
    factor: Decimal
    open: Decimal | None
    high: Decimal | None
    low: Decimal | None
    close: Decimal | None


@dataclass(frozen=True, slots=True)
class BuildSpec:
    root: Path
    start_date: str
    end_date: str
    symbols: tuple[str, ...] | None = None
    sources: tuple[str, ...] = ("twse", "tpex")
    classification_evidence_manifest: Path | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.root, Path):
            raise TypeError("root must be a Path")
        for name in ("start_date", "end_date"):
            value = getattr(self, name)
            try:
                if not isinstance(value, str) or date.fromisoformat(value).isoformat() != value:
                    raise ValueError
            except ValueError as exc:
                raise ValueError(f"{name} must be a valid ISO date") from exc
        if self.start_date > self.end_date:
            raise ValueError("start_date must not be after end_date")
        if self.symbols is not None and (
            not isinstance(self.symbols, tuple)
            or any(not isinstance(x, str) or not x.strip() for x in self.symbols)
        ):
            raise ValueError("symbols must be a tuple of non-empty strings or None")
        if (
            not isinstance(self.sources, tuple)
            or not self.sources
            or any(x not in {"twse", "tpex"} for x in self.sources)
        ):
            raise ValueError("sources must contain twse and/or tpex")
        if self.classification_evidence_manifest is not None and not isinstance(
            self.classification_evidence_manifest, Path
        ):
            raise TypeError("classification_evidence_manifest must be a Path or None")
