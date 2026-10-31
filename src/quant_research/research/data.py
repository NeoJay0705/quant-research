"""Immutable market-neutral research dataset and prefix-safe time views."""

from __future__ import annotations

import hashlib
import json
from bisect import bisect_right
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field, fields, is_dataclass
from decimal import Decimal
from pathlib import Path
from types import MappingProxyType
from typing import overload

from .models import (
    Bar,
    CorporateAction,
    ResearchInputError,
    SourceDiagnostic,
    iso_date,
    validate_action,
    validate_bar,
)


def _canonical(value: object) -> bytes:
    def encode(item: object) -> object:
        if isinstance(item, Decimal):
            return str(item)
        if isinstance(item, Path):
            return str(item)
        if is_dataclass(item) and not isinstance(item, type):
            return {field.name: encode(getattr(item, field.name)) for field in fields(item)}
        if isinstance(item, dict):
            return {str(key): encode(val) for key, val in item.items()}
        if isinstance(item, (tuple, list, set, frozenset)):
            return [encode(val) for val in item]
        return item

    return json.dumps(
        encode(value), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()


@dataclass(frozen=True, slots=True)
class ResearchData:
    market: str
    symbols: tuple[str, ...]
    start_date: str
    end_date: str
    sessions: tuple[str, ...]
    bars: tuple[Bar, ...]
    events: tuple[CorporateAction, ...] = ()
    calendar_basis: str = "provided_sessions"
    source_diagnostics: tuple[SourceDiagnostic, ...] = ()
    classification_diagnostics: tuple[str, ...] = ()
    input_digest: str = ""
    protected_input_dirs: frozenset[Path] = frozenset()
    _bar_days: Mapping[str, tuple[str, ...]] = field(init=False, repr=False, compare=False)
    _bars_by_symbol: Mapping[str, tuple[Bar, ...]] = field(init=False, repr=False, compare=False)
    _events_by_symbol: Mapping[str, tuple[CorporateAction, ...]] = field(
        init=False, repr=False, compare=False
    )

    def __post_init__(self) -> None:
        if not isinstance(self.market, str) or not self.market.strip():
            raise ResearchInputError("market must be non-empty")
        if (
            not isinstance(self.symbols, tuple)
            or not self.symbols
            or any(not isinstance(s, str) or not s.strip() for s in self.symbols)
        ):
            raise ResearchInputError("symbols must be a non-empty tuple of explicit symbols")
        if len(set(self.symbols)) != len(self.symbols):
            raise ResearchInputError("symbols must be unique")
        object.__setattr__(self, "symbols", tuple(sorted(self.symbols)))
        start, end = iso_date(self.start_date, "start_date"), iso_date(self.end_date, "end_date")
        if start > end:
            raise ResearchInputError("start_date must not be after end_date")
        if not isinstance(self.sessions, tuple) or not self.sessions:
            raise ResearchInputError("sessions must be a non-empty tuple")
        for day in self.sessions:
            iso_date(day, "session")
        if tuple(sorted(set(self.sessions))) != self.sessions:
            raise ResearchInputError("sessions must be sorted and unique")
        if self.sessions[0] < start or self.sessions[-1] > end:
            raise ResearchInputError("sessions must be within load bounds")
        if not isinstance(self.bars, tuple) or not isinstance(self.events, tuple):
            raise ResearchInputError("bars and events must be immutable tuples")
        if not isinstance(self.calendar_basis, str) or not self.calendar_basis:
            raise ResearchInputError("calendar_basis must be a non-empty string")
        if not isinstance(self.source_diagnostics, tuple) or any(
            not isinstance(item, SourceDiagnostic)
            or not all(
                isinstance(value, str) for value in (item.source, item.dataset, item.task_id)
            )
            or (item.snapshot_id is not None and not isinstance(item.snapshot_id, str))
            or isinstance(item.record_count, bool)
            or not isinstance(item.record_count, int)
            or item.record_count < 0
            or not isinstance(item.diagnostics, tuple)
            or any(not isinstance(value, str) for value in item.diagnostics)
            for item in self.source_diagnostics
        ):
            raise ResearchInputError(
                "source_diagnostics must contain immutable SourceDiagnostic records"
            )
        if not isinstance(self.classification_diagnostics, tuple) or any(
            not isinstance(value, str) for value in self.classification_diagnostics
        ):
            raise ResearchInputError("classification_diagnostics must be a tuple of strings")
        keys: set[tuple[str, str]] = set()
        by_symbol: dict[str, list[Bar]] = {s: [] for s in self.symbols}
        for bar in self.bars:
            validate_bar(bar)
            if (
                bar.market != self.market
                or bar.symbol not in self.symbols
                or not start <= bar.date <= end
            ):
                raise ResearchInputError("bar outside dataset market, symbols, or bounds")
            key = (bar.symbol, bar.date)
            if key in keys:
                raise ResearchInputError(f"duplicate bar: {key}")
            keys.add(key)
            by_symbol[bar.symbol].append(bar)
            if bar.date not in self.sessions:
                raise ResearchInputError(f"bar date {bar.date} missing from sessions")
        ordered_bars = tuple(sorted(self.bars, key=lambda b: (b.symbol, b.date)))
        if self.bars != ordered_bars:
            raise ResearchInputError("bars must be sorted by symbol and date")
        event_keys: set[tuple[str, str, str]] = set()
        event_map: dict[str, list[CorporateAction]] = {s: [] for s in self.symbols}
        for event in self.events:
            validate_action(event)
            event_key = (event.symbol, event.effective_date, event.event_type)
            if (
                event.market != self.market
                or event.symbol not in self.symbols
                or not start <= event.effective_date <= end
            ):
                raise ResearchInputError("event outside dataset market, symbols, or bounds")
            if event_key in event_keys:
                raise ResearchInputError(f"duplicate event: {event_key}")
            event_keys.add(event_key)
            event_map[event.symbol].append(event)
        if self.events != tuple(
            sorted(self.events, key=lambda e: (e.symbol, e.effective_date, e.event_type))
        ):
            raise ResearchInputError("events must be sorted by symbol, date, event type")
        digest = hashlib.sha256(
            _canonical(
                {
                    "market": self.market,
                    "symbols": sorted(self.symbols),
                    "start": start,
                    "end": end,
                    "sessions": self.sessions,
                    "bars": [bar for bar in self.bars],
                    "events": [event for event in self.events],
                    "calendar_basis": self.calendar_basis,
                    "source_diagnostics": self.source_diagnostics,
                    "classification_diagnostics": self.classification_diagnostics,
                }
            )
        ).hexdigest()
        if self.input_digest and self.input_digest != digest:
            raise ResearchInputError("input_digest does not match canonical inputs")
        object.__setattr__(self, "input_digest", digest)
        object.__setattr__(
            self,
            "protected_input_dirs",
            frozenset(Path(p).resolve() for p in self.protected_input_dirs),
        )
        object.__setattr__(
            self, "_bars_by_symbol", MappingProxyType({s: tuple(v) for s, v in by_symbol.items()})
        )
        object.__setattr__(
            self,
            "_bar_days",
            MappingProxyType({s: tuple(b.date for b in by_symbol[s]) for s in by_symbol}),
        )
        object.__setattr__(
            self, "_events_by_symbol", MappingProxyType({s: tuple(v) for s, v in event_map.items()})
        )

    def at(self, as_of: str) -> TimeView:
        day = iso_date(as_of, "as_of")
        if day < self.start_date or day > self.end_date:
            raise ResearchInputError("as_of must be within load bounds")
        return TimeView(self, day)


@dataclass(frozen=True, slots=True)
class _BarHistory(Sequence[Bar]):
    """Read-only indexed window over the data's shared bar tuple."""

    _bars: tuple[Bar, ...]
    _start: int
    _stop: int

    def __len__(self) -> int:
        return self._stop - self._start

    @overload
    def __getitem__(self, index: int) -> Bar: ...

    @overload
    def __getitem__(self, index: slice) -> Sequence[Bar]: ...

    def __getitem__(self, index: int | slice) -> Bar | Sequence[Bar]:
        if isinstance(index, slice):
            start, stop, step = index.indices(len(self))
            if step != 1:
                return tuple(self._bars[self._start + i] for i in range(start, stop, step))
            stop = max(start, stop)
            return _BarHistory(self._bars, self._start + start, self._start + stop)
        if index < 0:
            index += len(self)
        if index < 0 or index >= len(self):
            raise IndexError(index)
        return self._bars[self._start + index]

    def __iter__(self) -> Iterator[Bar]:
        for index in range(self._start, self._stop):
            yield self._bars[index]


@dataclass(frozen=True, slots=True)
class TimeView:
    _data: ResearchData = field(repr=False)
    as_of: str

    def __post_init__(self) -> None:
        day = iso_date(self.as_of, "as_of")
        if day < self._data.start_date or day > self._data.end_date:
            raise ResearchInputError("as_of must be within load bounds")
        object.__setattr__(self, "as_of", day)

    @property
    def market(self) -> str:
        return self._data.market

    @property
    def symbols(self) -> tuple[str, ...]:
        return self._data.symbols

    @property
    def sessions(self) -> tuple[str, ...]:
        return self._data.sessions[: bisect_right(self._data.sessions, self.as_of)]

    def bar(self, symbol: str, day: str | None = None) -> Bar | None:
        target = self.as_of if day is None else iso_date(day)
        if target > self.as_of:
            raise ResearchInputError("future bars are not available in this view")
        rows = self._data._bars_by_symbol.get(symbol)
        if rows is None:
            raise ResearchInputError(f"unknown symbol: {symbol}")
        days = self._data._bar_days[symbol]
        index = bisect_right(days, target) - 1
        return rows[index] if index >= 0 and rows[index].date == target else None

    def history(self, symbol: str, *, start_date: str | None = None) -> Sequence[Bar]:
        if symbol not in self._data._bars_by_symbol:
            raise ResearchInputError(f"unknown symbol: {symbol}")
        rows = self._data._bars_by_symbol[symbol]
        end = bisect_right(self._data._bar_days[symbol], self.as_of)
        if start_date is None:
            return _BarHistory(rows, 0, end)
        start = iso_date(start_date)
        if start > self.as_of:
            raise ResearchInputError("history start cannot be in the future")
        begin = 0
        while begin < end and rows[begin].date < start:
            begin += 1
        return _BarHistory(rows, begin, end)

    def events(self, symbol: str) -> tuple[CorporateAction, ...]:
        if symbol not in self._data._events_by_symbol:
            raise ResearchInputError(f"unknown symbol: {symbol}")
        events = self._data._events_by_symbol[symbol]
        end = bisect_right(tuple(e.effective_date for e in events), self.as_of)
        return events[:end]
