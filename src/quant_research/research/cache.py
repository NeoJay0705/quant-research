"""Persistent SQLite cache of validated, derived research data."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

from quant_research.taiwan.dataset.arithmetic import event_multiplier, process_quotes
from quant_research.taiwan.dataset.build import _iter_raw_series
from quant_research.taiwan.dataset.classification import EvidenceSet, is_four_digit_code
from quant_research.taiwan.dataset.events import EventRef, EventValue, load_events
from quant_research.taiwan.dataset.factors import calculate_adjustments
from quant_research.taiwan.dataset.models import AdjustedQuote, BuildSpec, QuoteValue
from quant_research.taiwan.dataset.source import RecordResult, Scope, SourceReader

from .data import ResearchData
from .models import (
    Bar,
    CorporateAction,
    ResearchError,
    ResearchInputError,
    SourceDiagnostic,
    UnsupportedDataError,
    iso_date,
    validate_action,
    validate_bar,
)
from .taiwan import _build_actions, _build_bars

SCHEMA_VERSION = "1"
ADJUSTMENT_RULE = "forward-cumulative-v1"


class CacheError(ResearchError):
    """General cache operation failure, including unsafe paths and duplicate builds."""


class CacheIncompatibleError(CacheError):
    """The cache schema or adjustment semantics are incompatible."""


class SymbolUnavailableError(ResearchError):
    """A cached symbol was explicitly recorded as unavailable."""


class SymbolNotInCacheError(ResearchError):
    """The requested symbol has no entry in this market's cache."""


class NoQuotesError(ResearchError):
    """No requested available symbol has quotes in the requested date range."""


class CacheCoverageError(ResearchError):
    """The requested dates fall outside the stored cache coverage."""


@dataclass(frozen=True, slots=True)
class BuildSummary:
    market: str
    coverage_start: str
    built_through: str
    available_count: int
    unavailable_count: int


@dataclass(frozen=True, slots=True)
class SymbolStatus:
    symbol: str
    status: str
    reason: str | None
    first_date: str | None
    last_date: str | None


@dataclass(frozen=True, slots=True)
class CacheStatus:
    market: str
    coverage_start: str
    built_through: str
    available_count: int
    unavailable_count: int


_SCHEMA = (
    "CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL)",
    "CREATE TABLE markets("
    "market TEXT PRIMARY KEY, coverage_start TEXT NOT NULL, built_through TEXT NOT NULL, "
    "evidence_hashes TEXT NOT NULL, source_root TEXT NOT NULL, evidence_dirs TEXT NOT NULL)",
    "CREATE TABLE sessions(market TEXT NOT NULL, date TEXT NOT NULL, PRIMARY KEY(market,date))",
    "CREATE TABLE symbols("
    "market TEXT NOT NULL, symbol TEXT NOT NULL, status TEXT NOT NULL, reason TEXT, "
    "first_date TEXT, last_date TEXT, PRIMARY KEY(market,symbol))",
    "CREATE TABLE bars("
    "market TEXT NOT NULL, symbol TEXT NOT NULL, date TEXT NOT NULL, open TEXT, high TEXT, "
    "low TEXT, close TEXT, volume TEXT, amount TEXT, adjusted_open TEXT, adjusted_high TEXT, "
    "adjusted_low TEXT, adjusted_close TEXT, factor TEXT NOT NULL, classification TEXT NOT NULL, "
    "classification_reason TEXT NOT NULL, classification_refs TEXT NOT NULL, "
    "source_refs TEXT NOT NULL, quality_flags TEXT NOT NULL, PRIMARY KEY(market,symbol,date))",
    "CREATE TABLE events("
    "market TEXT NOT NULL, symbol TEXT NOT NULL, effective_date TEXT NOT NULL, "
    "event_type TEXT NOT NULL, dividend TEXT, bonus_share_ratio TEXT, "
    "cash_return_per_share TEXT, share_multiplier TEXT, par_before TEXT, par_after TEXT, "
    "before_price TEXT, reference_price TEXT, cash_credit_date TEXT, share_available_date TEXT, "
    "classification TEXT NOT NULL, classification_reason TEXT NOT NULL, "
    "classification_refs TEXT NOT NULL, source_refs TEXT NOT NULL, quality_flags TEXT NOT NULL, "
    "reasons TEXT NOT NULL, payload TEXT NOT NULL, multiplier TEXT, "
    "PRIMARY KEY(market,symbol,effective_date,event_type))",
    "CREATE TABLE scopes("
    "market TEXT NOT NULL, task_id TEXT NOT NULL, snapshot_id TEXT NOT NULL, source TEXT NOT NULL, "
    "dataset TEXT NOT NULL, record_count INTEGER NOT NULL, diagnostics TEXT NOT NULL, "
    "request_date TEXT, PRIMARY KEY(market,task_id,snapshot_id))",
)


def _connect_write(path: Path) -> sqlite3.Connection:
    db = sqlite3.connect(path, timeout=0)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA foreign_keys=ON")
    return db


def _connect_read(path: Path) -> sqlite3.Connection:
    try:
        db = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=0)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA query_only=ON")
        db.execute("BEGIN")
        _check_meta(db)
        return db
    except (sqlite3.Error, OSError) as exc:
        raise CacheError(f"cannot open cache read-only: {exc}") from exc


def _check_meta(db: sqlite3.Connection) -> None:
    try:
        values = dict(db.execute("SELECT key,value FROM meta"))
    except sqlite3.Error as exc:
        raise CacheIncompatibleError(f"incompatible cache schema: {exc}") from exc
    if values.get("schema_version") != SCHEMA_VERSION:
        raise CacheIncompatibleError("incompatible cache schema_version")
    if values.get("adjustment_rule") != ADJUSTMENT_RULE:
        raise CacheIncompatibleError("incompatible cache adjustment_rule")


def _inside(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def _decimal(value: Decimal | None) -> str | None:
    return None if value is None else str(value)


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _write_symbol_status(
    db: sqlite3.Connection,
    market: str,
    symbol: str,
    status: str,
    reason: str | None,
    first_date: str | None,
    last_date: str | None,
) -> None:
    db.execute(
        "INSERT OR REPLACE INTO symbols VALUES(?,?,?,?,?,?)",
        (market, symbol, status, reason, first_date, last_date),
    )


def _record_sessions(
    db: sqlite3.Connection, market: str, days: tuple[str, ...], *, append: bool
) -> None:
    verb = "INSERT OR IGNORE" if append else "INSERT"
    db.executemany(f"{verb} INTO sessions VALUES(?,?)", ((market, day) for day in days))


def _record_scopes(db: sqlite3.Connection, market: str, scopes: tuple[Scope, ...]) -> None:
    for scope in scopes:
        if scope.snapshot_id is None:
            continue
        db.execute(
            "INSERT OR REPLACE INTO scopes VALUES(?,?,?,?,?,?,?,?)",
            (
                market,
                scope.task_id,
                scope.snapshot_id,
                scope.source,
                scope.dataset,
                scope.source_record_count,
                _json(scope.diagnostics),
                (scope.request or {}).get("date"),
            ),
        )


def _scope_context(
    reader: SourceReader,
) -> tuple[dict[Any, Scope], dict[Any, Any], dict[Any, RecordResult], dict[Any, Any]]:
    scopes = reader.scopes()
    return (
        {(s.task_id, s.snapshot_id): s for s in scopes},
        {(s.task_id, s.snapshot_id): reader.links(s) for s in scopes if s.lane == "events"},
        {
            (s.task_id, s.snapshot_id, r.record_id): r
            for s in scopes
            if s.lane == "events"
            for r in reader.iter_all_records(s)
        },
        {},
    )


def build_cache(
    cache_path: Path,
    root: Path,
    market: str,
    start_date: str,
    end_date: str,
    classification_evidence_manifest: Path | None = None,
) -> BuildSummary:
    """Build one market atomically from the pinned read-only source snapshot."""
    if not isinstance(cache_path, Path) or not isinstance(root, Path):
        raise ResearchInputError("cache_path and root must be pathlib.Path")
    if classification_evidence_manifest is not None and not isinstance(
        classification_evidence_manifest, Path
    ):
        raise ResearchInputError("classification_evidence_manifest must be pathlib.Path or None")
    if market not in {"twse", "tpex"}:
        raise ResearchInputError("market must be 'twse' or 'tpex'")
    start_date, end_date = iso_date(start_date, "start_date"), iso_date(end_date, "end_date")
    if start_date > end_date:
        raise ResearchInputError("start_date must not be after end_date")
    if not cache_path.parent.is_dir():
        raise CacheError("cache parent directory must already exist")
    resolved_cache = cache_path.resolve()
    resolved_root = root.resolve()
    evidence = EvidenceSet.from_manifest(classification_evidence_manifest)
    protected = {p.resolve() for p in evidence.protected_input_directories}
    if _inside(resolved_cache, resolved_root) or any(_inside(resolved_cache, p) for p in protected):
        raise CacheError("cache path is inside a protected input directory")
    created = not resolved_cache.exists()
    db: sqlite3.Connection | None = None
    succeeded = False
    try:
        db = _connect_write(resolved_cache)
        if not created:
            _check_meta(db)
        db.execute("BEGIN IMMEDIATE")
        if created:
            for statement in _SCHEMA:
                db.execute(statement)
            db.executemany(
                "INSERT INTO meta VALUES(?,?)",
                (("schema_version", SCHEMA_VERSION), ("adjustment_rule", ADJUSTMENT_RULE)),
            )
        if db.execute("SELECT 1 FROM markets WHERE market=?", (market,)).fetchone():
            raise CacheError(f"market already built: {market}")
        spec = BuildSpec(
            root,
            start_date,
            end_date,
            symbols=None,
            sources=(market,),
            classification_evidence_manifest=classification_evidence_manifest,
        )
        with SourceReader(spec) as reader:
            scopes = reader.scopes()
            for scope in scopes:
                if not scope.usable:
                    raise UnsupportedDataError(
                        f"{market}/{scope.dataset} task={scope.task_id}: {scope.diagnostics}"
                    )
            quote_scopes = [s for s in scopes if s.lane == "quotes"]
            observed = tuple(
                sorted(
                    {
                        s.request["date"]
                        for s in quote_scopes
                        if s.source_record_count > 0
                        and s.request is not None
                        and start_date <= s.request["date"] <= end_date
                    }
                )
            )
            if not observed:
                raise UnsupportedDataError("no observed market sessions in requested range")
            unavailable: dict[str, str] = {}

            def skip(symbol: str) -> str | None:
                return (
                    None
                    if is_four_digit_code(symbol)
                    else "classification:unknown:invalid_symbol_shape"
                )

            raw_rows = _iter_raw_series(
                reader,
                evidence,
                skip_symbol=skip,
                on_skip=lambda _s, sym, why: unavailable.__setitem__(sym, why),
                on_symbol_error=lambda _s, sym, exc: unavailable.__setitem__(
                    sym, f"{type(exc).__name__}: {exc}"
                ),
            )
            scope_by_ref, link_cache, summary_records, child_cache = _scope_context(reader)
            bars_count = events_count = 0
            available: set[str] = set()
            try:
                for item in raw_rows:
                    classes = {c.classification for c in item.classifications}
                    ordinary = {"ordinary_evidence_supported", "ordinary_rule_limited"}
                    if not classes <= ordinary:
                        if len(classes) == 1:
                            c = item.classifications[0]
                            reason = f"classification:{c.classification}:{c.reason}"
                        else:
                            reason = "classification:mixed:" + ",".join(sorted(classes))
                        unavailable[item.symbol] = reason
                        continue
                    try:
                        events = load_events(reader, item.symbol, source=market)
                        adjusted = calculate_adjustments(item.quotes, events)

                        flags_by_date = {
                            quote.date: flags
                            for quote, flags in zip(item.quotes, item.quality_flags, strict=True)
                        }

                        def quality_for(
                            quote: QuoteValue,
                            quality_by_date: dict[str, tuple[str, ...]] = flags_by_date,
                        ) -> tuple[str, ...]:
                            return quality_by_date[quote.date]

                        bars = _build_bars(
                            market,
                            item.symbol,
                            item.quotes,
                            {q.quote.date: q for q in adjusted},
                            item.classifications,
                            quality_for,
                        )
                        actions = _build_actions(
                            market,
                            item.symbol,
                            events,
                            bars[0] if bars else None,
                            reader,
                            scope_by_ref,
                            link_cache,
                            summary_records,
                            child_cache,
                        )
                        _validate_symbol_output(
                            market, item.symbol, bars, actions, start_date, end_date
                        )
                        factor_by_date = {q.quote.date: q.factor for q in adjusted}
                        bar_rows = [_bar_row(market, bar, factor_by_date[bar.date]) for bar in bars]
                        event_rows = _event_rows(market, actions, events)
                    except Exception as exc:
                        unavailable[item.symbol] = f"{type(exc).__name__}: {exc}"
                    else:
                        _insert_bar_rows(db, bar_rows)
                        _insert_event_rows(db, event_rows)
                        bars_count += len(bar_rows)
                        events_count += len(event_rows)
                        available.add(item.symbol)
            finally:
                raw_rows.close()
            # all seen quote symbols are represented by either output or callbacks
            for symbol in sorted(available):
                dates = [
                    r[0]
                    for r in db.execute(
                        "SELECT date FROM bars WHERE market=? AND symbol=? ORDER BY date",
                        (market, symbol),
                    )
                ]
                _write_symbol_status(db, market, symbol, "available", None, dates[0], dates[-1])
            for symbol, reason in sorted(unavailable.items()):
                _write_symbol_status(db, market, symbol, "unavailable", reason, None, None)
            _record_sessions(db, market, observed, append=False)
            _record_scopes(db, market, tuple(scopes))
            db.execute(
                "INSERT INTO markets VALUES(?,?,?,?,?,?)",
                (
                    market,
                    start_date,
                    end_date,
                    _json(evidence.stable_input_hashes),
                    str(resolved_root),
                    _json(sorted(str(p.resolve()) for p in evidence.protected_input_directories)),
                ),
            )
        db.commit()
        succeeded = True
        return BuildSummary(market, start_date, end_date, len(available), len(unavailable))
    except BaseException:
        if db is not None:
            try:
                db.rollback()
            except sqlite3.Error:
                pass
        raise
    finally:
        if db is not None:
            db.close()
        if created and not succeeded:
            # A first build's failed transaction must leave no database or SQLite sidecars.
            for suffix in ("", "-wal", "-shm"):
                try:
                    Path(str(resolved_cache) + suffix).unlink(missing_ok=True)
                except OSError:
                    pass


def _insert_bar_rows(db: sqlite3.Connection, rows: list[tuple[Any, ...]]) -> None:
    db.executemany("INSERT INTO bars VALUES(" + ",".join("?" for _ in range(19)) + ")", rows)


def _insert_event_rows(db: sqlite3.Connection, rows: list[tuple[Any, ...]]) -> None:
    db.executemany("INSERT INTO events VALUES(" + ",".join("?" for _ in range(22)) + ")", rows)


def _validate_symbol_output(
    market: str,
    symbol: str,
    bars: tuple[Bar, ...] | list[Bar],
    actions: tuple[CorporateAction, ...] | list[CorporateAction],
    start_date: str,
    end_date: str,
) -> None:
    for bar in bars:
        validate_bar(bar)
        if bar.market != market or bar.symbol != symbol or not start_date <= bar.date <= end_date:
            raise ResearchInputError("bar outside cache market, symbol, or bounds")
    for action in actions:
        validate_action(action)
        if (
            action.market != market
            or action.symbol != symbol
            or not start_date <= action.effective_date <= end_date
        ):
            raise ResearchInputError("event outside cache market, symbol, or bounds")


def _event_rows(
    market: str, actions: tuple[CorporateAction, ...], events: tuple[EventValue, ...]
) -> list[tuple[Any, ...]]:
    rows: list[tuple[Any, ...]] = []
    for action, event in zip(actions, events, strict=True):
        rows.append(
            (
                market,
                action.symbol,
                action.effective_date,
                action.event_type,
                *(
                    _decimal(getattr(action, name))
                    for name in (
                        "dividend",
                        "bonus_share_ratio",
                        "cash_return_per_share",
                        "share_multiplier",
                        "par_before",
                        "par_after",
                        "before_price",
                        "reference_price",
                    )
                ),
                action.cash_credit_date,
                action.share_available_date,
                action.classification,
                action.classification_reason,
                _json(action.classification_refs),
                _json(action.source_refs),
                _json(action.quality_flags),
                _json(action.reasons),
                _json(action.payload),
                _decimal(event_multiplier(event)),
            )
        )
    return rows


def _bar_row(market: str, bar: Bar, factor: Decimal) -> tuple[Any, ...]:
    return (
        market,
        bar.symbol,
        bar.date,
        *(
            _decimal(getattr(bar, n))
            for n in (
                "open",
                "high",
                "low",
                "close",
                "volume",
                "amount",
                "adjusted_open",
                "adjusted_high",
                "adjusted_low",
                "adjusted_close",
            )
        ),
        str(factor),
        bar.classification,
        bar.classification_reason,
        _json(bar.classification_refs),
        _json(bar.source_refs),
        _json(bar.quality_flags),
    )


def update_cache(
    cache_path: Path,
    root: Path,
    market: str,
    end_date: str,
    classification_evidence_manifest: Path | None = None,
) -> BuildSummary:
    """Append source data after the market's current built-through date atomically."""
    if not isinstance(cache_path, Path) or not isinstance(root, Path):
        raise ResearchInputError("cache_path and root must be pathlib.Path")
    if classification_evidence_manifest is not None and not isinstance(
        classification_evidence_manifest, Path
    ):
        raise ResearchInputError("classification_evidence_manifest must be pathlib.Path or None")
    if market not in {"twse", "tpex"}:
        raise ResearchInputError("market must be 'twse' or 'tpex'")
    end_date = iso_date(end_date, "end_date")
    if not cache_path.exists():
        raise CacheError("cache file does not exist")
    resolved_cache, resolved_root = cache_path.resolve(), root.resolve()
    evidence = EvidenceSet.from_manifest(classification_evidence_manifest)
    protected = {p.resolve() for p in evidence.protected_input_directories}
    if _inside(resolved_cache, resolved_root) or any(_inside(resolved_cache, p) for p in protected):
        raise CacheError("cache path is inside a protected input directory")
    db: sqlite3.Connection | None = None
    try:
        db = _connect_write(resolved_cache)
        db.row_factory = sqlite3.Row
        _check_meta(db)
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT * FROM markets WHERE market=?", (market,)).fetchone()
        if row is None:
            raise CacheError(f"market not in cache: {market}")
        start, through = str(row["coverage_start"]), str(row["built_through"])
        if end_date <= through:
            raise ResearchInputError("end_date must be later than built_through")
        if _json(evidence.stable_input_hashes) != str(row["evidence_hashes"]):
            raise CacheError("classification evidence hashes differ from cached market")
        next_day = (date.fromisoformat(through) + timedelta(days=1)).isoformat()
        spec = BuildSpec(
            root,
            next_day,
            end_date,
            symbols=None,
            sources=(market,),
            classification_evidence_manifest=classification_evidence_manifest,
        )
        with SourceReader(spec) as reader:
            scopes = reader.scopes()
            for scope in scopes:
                if not scope.usable:
                    raise UnsupportedDataError(
                        f"{market}/{scope.dataset} task={scope.task_id}: {scope.diagnostics}"
                    )
            observed = tuple(
                sorted(
                    {
                        s.request["date"]
                        for s in scopes
                        if s.lane == "quotes"
                        and s.source_record_count > 0
                        and s.request is not None
                        and next_day <= s.request["date"] <= end_date
                    }
                )
            )
            if not observed:
                raise UnsupportedDataError("no observed market sessions in requested range")
            unavailable_stored = {
                str(r[0])
                for r in db.execute(
                    "SELECT symbol FROM symbols WHERE market=? AND status='unavailable'", (market,)
                )
            }
            known = {
                str(r[0])
                for r in db.execute("SELECT symbol FROM symbols WHERE market=?", (market,))
            }
            new_symbols: set[str] = set()
            errors: dict[str, str] = {}

            def skip(symbol: str) -> str | None:
                if symbol in unavailable_stored:
                    return "stored_unavailable"
                if not is_four_digit_code(symbol):
                    return "classification:unknown:invalid_symbol_shape"
                return None

            def on_skip(_source: str, symbol: str, reason: str) -> None:
                if symbol not in unavailable_stored:
                    errors[symbol] = reason
                if symbol not in known:
                    new_symbols.add(symbol)

            def on_error(_source: str, symbol: str, exc: Exception) -> None:
                errors[symbol] = f"{type(exc).__name__}: {exc}"
                if symbol not in known:
                    new_symbols.add(symbol)

            raw = _iter_raw_series(
                reader, evidence, skip_symbol=skip, on_skip=on_skip, on_symbol_error=on_error
            )
            scope_by_ref, link_cache, summary_records, child_cache = _scope_context(reader)
            available = 0
            processed_symbols: set[str] = set()
            try:
                for item in raw:
                    symbol = item.symbol
                    processed_symbols.add(symbol)
                    if symbol in unavailable_stored:
                        continue
                    if symbol not in known:
                        new_symbols.add(symbol)
                    old_classes = {
                        str(r[0])
                        for r in db.execute(
                            "SELECT DISTINCT classification FROM bars WHERE market=? AND symbol=?",
                            (market, symbol),
                        )
                    }
                    classes = old_classes | {c.classification for c in item.classifications}
                    ordinary = {"ordinary_evidence_supported", "ordinary_rule_limited"}
                    if not classes <= ordinary:
                        errors[symbol] = "classification:mixed:" + ",".join(sorted(classes))
                        db.execute("DELETE FROM bars WHERE market=? AND symbol=?", (market, symbol))
                        db.execute(
                            "DELETE FROM events WHERE market=? AND symbol=?", (market, symbol)
                        )
                        _write_symbol_status(
                            db, market, symbol, "unavailable", errors[symbol], None, None
                        )
                        continue
                    try:
                        old = db.execute(
                            "SELECT * FROM bars WHERE market=? AND symbol=? ORDER BY date LIMIT 1",
                            (market, symbol),
                        ).fetchone()
                        first_bar = _bar_from_row(old) if old is not None else None
                        # New symbols require historical events for complete event parity.
                        event_reader = reader
                        if symbol not in known:
                            event_reader = SourceReader(
                                BuildSpec(
                                    root,
                                    start,
                                    end_date,
                                    symbols=(symbol,),
                                    sources=(market,),
                                    classification_evidence_manifest=classification_evidence_manifest,
                                )
                            )
                            event_reader.__enter__()
                        try:
                            events = load_events(event_reader, symbol, source=market)
                            if event_reader is reader:
                                use_events = events
                            else:
                                use_events = events
                            if item.quotes:
                                last = db.execute(
                                    "SELECT * FROM bars WHERE market=? AND symbol=? "
                                    "ORDER BY date DESC LIMIT 1",
                                    (market, symbol),
                                ).fetchone()
                                if last is not None:
                                    x, factor = (
                                        str(last["date"]),
                                        Decimal(
                                            str(
                                                db.execute(
                                                    "SELECT factor FROM bars WHERE market=? "
                                                    "AND symbol=? AND date=?",
                                                    (market, symbol, last["date"]),
                                                ).fetchone()[0]
                                            )
                                        ),
                                    )
                                    pending = []
                                    for erow in db.execute(
                                        "SELECT * FROM events WHERE market=? AND symbol=? "
                                        "AND effective_date>? AND multiplier IS NOT NULL",
                                        (market, symbol, x),
                                    ):
                                        ref = EventRef(market, "cache", "", "", 0, "")
                                        pending.append(
                                            EventValue(
                                                market,
                                                symbol,
                                                str(erow["event_type"]),
                                                str(erow["effective_date"]),
                                                Decimal(0),
                                                None,
                                                Decimal(0),
                                                Decimal(str(erow["multiplier"])),
                                                None,
                                                None,
                                                ref,
                                            )
                                        )
                                    pending.extend(
                                        e for e in use_events if e.effective_date >= next_day
                                    )
                                    adjusted = process_quotes(
                                        item.quotes,
                                        pending,
                                        initial_factor=factor,
                                        applied_through=x,
                                    )
                                else:
                                    adjusted = calculate_adjustments(item.quotes, use_events)
                                flags = {
                                    q.date: f
                                    for q, f in zip(item.quotes, item.quality_flags, strict=True)
                                }

                                def quality_for(
                                    quote: QuoteValue,
                                    quality_by_date: dict[str, tuple[str, ...]] = flags,
                                ) -> tuple[str, ...]:
                                    return quality_by_date[quote.date]

                                bars = _build_bars(
                                    market,
                                    symbol,
                                    item.quotes,
                                    {q.quote.date: q for q in adjusted},
                                    item.classifications,
                                    quality_for,
                                )
                            else:
                                adjusted, bars = (), ()
                            actions = _build_actions(
                                market,
                                symbol,
                                use_events,
                                first_bar or (bars[0] if bars else None),
                                event_reader,
                                *_scope_context(event_reader),
                            )
                            new_bars = [b for b in bars if b.date >= next_day]
                            new_actions = [
                                (a, e)
                                for a, e in zip(actions, use_events, strict=True)
                                if a.effective_date >= next_day
                            ]
                            _validate_symbol_output(
                                market,
                                symbol,
                                new_bars,
                                [action for action, _event in new_actions],
                                next_day,
                                end_date,
                            )
                            bar_rows = [
                                _bar_row(
                                    market,
                                    b,
                                    next(q.factor for q in adjusted if q.quote.date == b.date),
                                )
                                for b in new_bars
                            ]
                            event_rows = []
                            for action, event in new_actions:
                                event_rows.append(
                                    (
                                        market,
                                        action.symbol,
                                        action.effective_date,
                                        action.event_type,
                                        *(
                                            _decimal(getattr(action, n))
                                            for n in (
                                                "dividend",
                                                "bonus_share_ratio",
                                                "cash_return_per_share",
                                                "share_multiplier",
                                                "par_before",
                                                "par_after",
                                                "before_price",
                                                "reference_price",
                                            )
                                        ),
                                        action.cash_credit_date,
                                        action.share_available_date,
                                        action.classification,
                                        action.classification_reason,
                                        _json(action.classification_refs),
                                        _json(action.source_refs),
                                        _json(action.quality_flags),
                                        _json(action.reasons),
                                        _json(action.payload),
                                        _decimal(event_multiplier(event)),
                                    )
                                )
                        finally:
                            if event_reader is not reader:
                                event_reader.__exit__(None, None, None)
                    except Exception as exc:
                        errors[symbol] = f"{type(exc).__name__}: {exc}"
                        db.execute("DELETE FROM bars WHERE market=? AND symbol=?", (market, symbol))
                        db.execute(
                            "DELETE FROM events WHERE market=? AND symbol=?", (market, symbol)
                        )
                        _write_symbol_status(
                            db, market, symbol, "unavailable", errors[symbol], None, None
                        )
                        continue
                    _insert_bar_rows(db, bar_rows)
                    _insert_event_rows(db, event_rows)
                    dates = [
                        str(r[0])
                        for r in db.execute(
                            "SELECT date FROM bars WHERE market=? AND symbol=? ORDER BY date",
                            (market, symbol),
                        )
                    ]
                    _write_symbol_status(db, market, symbol, "available", None, dates[0], dates[-1])
                    available += 1
                # Existing available symbols must refresh events even if they have no
                # quote in this append window.
                quiet_symbols = db.execute(
                    "SELECT symbol FROM symbols WHERE market=? AND status='available'",
                    (market,),
                ).fetchall()
                for quiet_row in quiet_symbols:
                    symbol = str(quiet_row[0])
                    if symbol in processed_symbols:
                        continue
                    try:
                        first = db.execute(
                            "SELECT * FROM bars WHERE market=? AND symbol=? ORDER BY date LIMIT 1",
                            (market, symbol),
                        ).fetchone()
                        events = load_events(reader, symbol, source=market)
                        actions = _build_actions(
                            market,
                            symbol,
                            events,
                            _bar_from_row(first) if first is not None else None,
                            reader,
                            scope_by_ref,
                            link_cache,
                            summary_records,
                            child_cache,
                        )
                        selected = [
                            (action, event)
                            for action, event in zip(actions, events, strict=True)
                            if action.effective_date >= next_day
                        ]
                        _validate_symbol_output(
                            market,
                            symbol,
                            [],
                            [action for action, _event in selected],
                            next_day,
                            end_date,
                        )
                    except Exception as exc:
                        reason = f"{type(exc).__name__}: {exc}"
                        db.execute("DELETE FROM bars WHERE market=? AND symbol=?", (market, symbol))
                        db.execute(
                            "DELETE FROM events WHERE market=? AND symbol=?", (market, symbol)
                        )
                        _write_symbol_status(db, market, symbol, "unavailable", reason, None, None)
                    else:
                        _insert_event_rows(
                            db,
                            _event_rows(
                                market,
                                tuple(action for action, _event in selected),
                                tuple(event for _action, event in selected),
                            ),
                        )
            finally:
                raw.close()
            for symbol, reason in errors.items():
                if symbol in unavailable_stored:
                    continue
                if symbol not in known:
                    _write_symbol_status(db, market, symbol, "unavailable", reason, None, None)
            _record_sessions(db, market, observed, append=True)
            _record_scopes(db, market, tuple(scopes))
            db.execute(
                "UPDATE markets SET built_through=?,source_root=?,evidence_dirs=? WHERE market=?",
                (
                    end_date,
                    str(resolved_root),
                    _json(sorted(str(p.resolve()) for p in evidence.protected_input_directories)),
                    market,
                ),
            )
        counts = db.execute(
            "SELECT status,COUNT(*) FROM symbols WHERE market=? GROUP BY status", (market,)
        ).fetchall()
        tally = {str(k): int(v) for k, v in counts}
        db.commit()
        return BuildSummary(
            market, start, end_date, tally.get("available", 0), tally.get("unavailable", 0)
        )
    except BaseException:
        if db is not None:
            db.rollback()
        raise
    finally:
        if db is not None:
            db.close()


def rebuild_cache(
    cache_path: Path,
    root: Path,
    market: str,
    start_date: str,
    symbols: tuple[str, ...] | None = None,
    classification_evidence_manifest: Path | None = None,
) -> BuildSummary:
    """Rebuild selected symbols through the cache's existing market coverage end."""
    if not isinstance(cache_path, Path) or not isinstance(root, Path):
        raise ResearchInputError("cache_path and root must be pathlib.Path")
    if classification_evidence_manifest is not None and not isinstance(
        classification_evidence_manifest, Path
    ):
        raise ResearchInputError("classification_evidence_manifest must be pathlib.Path or None")
    if market not in {"twse", "tpex"}:
        raise ResearchInputError("market must be 'twse' or 'tpex'")
    start_date = iso_date(start_date, "start_date")
    if symbols is not None and (
        not isinstance(symbols, tuple)
        or not symbols
        or any(not isinstance(s, str) or not s.strip() for s in symbols)
        or len(set(symbols)) != len(symbols)
    ):
        raise ResearchInputError("symbols must be a non-empty tuple of unique explicit symbols")
    if not cache_path.exists():
        raise CacheError("cache file does not exist")
    resolved_cache, resolved_root = cache_path.resolve(), root.resolve()
    evidence = EvidenceSet.from_manifest(classification_evidence_manifest)
    protected = {p.resolve() for p in evidence.protected_input_directories}
    if _inside(resolved_cache, resolved_root) or any(_inside(resolved_cache, p) for p in protected):
        raise CacheError("cache path is inside a protected input directory")
    db: sqlite3.Connection | None = None
    try:
        db = _connect_write(resolved_cache)
        db.row_factory = sqlite3.Row
        _check_meta(db)
        db.execute("BEGIN IMMEDIATE")
        market_row = _market(db, market)
        coverage_start, through = (
            str(market_row["coverage_start"]),
            str(market_row["built_through"]),
        )
        if not coverage_start <= start_date <= through:
            raise CacheError("start_date must be within the stored market coverage")
        hashes = _json(evidence.stable_input_hashes)
        full_market = symbols is None and start_date == coverage_start
        if hashes != str(market_row["evidence_hashes"]) and not full_market:
            raise CacheError("classification evidence hashes differ from cached market")
        old_status = {
            str(r["symbol"]): str(r["status"])
            for r in db.execute("SELECT symbol,status FROM symbols WHERE market=?", (market,))
        }
        if symbols is not None:
            missing = [s for s in symbols if s not in old_status]
            if missing:
                raise SymbolNotInCacheError(f"symbol not in cache: {missing[0]}")
            unavailable = [s for s in symbols if old_status[s] == "unavailable"]
            if unavailable and start_date != coverage_start:
                raise CacheError(
                    f"cannot partially rebuild unavailable symbol {unavailable[0]}; "
                    "rebuild it from coverage_start"
                )
            targets = set(symbols)
        elif start_date == coverage_start:
            targets = set(old_status)
        else:
            targets = {s for s, status in old_status.items() if status == "available"}
        spec = BuildSpec(
            root,
            start_date,
            through,
            symbols=tuple(sorted(targets)) if symbols is not None else None,
            sources=(market,),
            classification_evidence_manifest=classification_evidence_manifest,
        )
        with SourceReader(spec) as reader:
            scopes = reader.scopes()
            for scope in scopes:
                if not scope.usable:
                    raise UnsupportedDataError(
                        f"{market}/{scope.dataset} task={scope.task_id}: {scope.diagnostics}"
                    )
            observed = tuple(
                sorted(
                    {
                        s.request["date"]
                        for s in scopes
                        if s.lane == "quotes"
                        and s.source_record_count > 0
                        and s.request is not None
                        and start_date <= s.request["date"] <= through
                    }
                )
            )
            if not observed:
                raise UnsupportedDataError("no observed market sessions in requested range")
            failures: dict[str, str] = {}

            def skip(symbol: str) -> str | None:
                if not is_four_digit_code(symbol):
                    return "classification:unknown:invalid_symbol_shape"
                return None

            raw = _iter_raw_series(
                reader,
                evidence,
                skip_symbol=skip,
                on_skip=lambda _src, sym, why: failures.__setitem__(sym, why),
                on_symbol_error=lambda _src, sym, exc: failures.__setitem__(
                    sym, f"{type(exc).__name__}: {exc}"
                ),
            )
            raw_by_symbol = {item.symbol: item for item in raw}
            raw.close()
            discovered = set(raw_by_symbol) | set(failures)
            if symbols is None:
                targets.update(discovered)
            all_scopes, links, records, children = _scope_context(reader)
            for symbol in sorted(targets):
                if old_status.get(symbol) == "unavailable" and start_date > coverage_start:
                    continue
                db.execute(
                    "DELETE FROM bars WHERE market=? AND symbol=? AND date>=?",
                    (market, symbol, start_date),
                )
                db.execute(
                    "DELETE FROM events WHERE market=? AND symbol=? AND effective_date>=?",
                    (market, symbol, start_date),
                )
                if start_date == coverage_start:
                    db.execute("DELETE FROM bars WHERE market=? AND symbol=?", (market, symbol))
                    db.execute("DELETE FROM events WHERE market=? AND symbol=?", (market, symbol))
                    db.execute("DELETE FROM symbols WHERE market=? AND symbol=?", (market, symbol))
                if symbol in failures:
                    _write_symbol_status(
                        db, market, symbol, "unavailable", failures[symbol], None, None
                    )
                    continue
                item = raw_by_symbol.get(symbol)
                retained = db.execute(
                    "SELECT DISTINCT classification FROM bars WHERE market=? AND symbol=?",
                    (market, symbol),
                ).fetchall()
                classes = {str(x[0]) for x in retained}
                if item:
                    classes.update(c.classification for c in item.classifications)
                ordinary = {"ordinary_evidence_supported", "ordinary_rule_limited"}
                if not classes <= ordinary:
                    if len(classes) == 1 and item:
                        c = next(
                            c for c in item.classifications if c.classification not in ordinary
                        )
                        reason = f"classification:{c.classification}:{c.reason}"
                    else:
                        reason = "classification:mixed:" + ",".join(sorted(classes))
                    db.execute("DELETE FROM bars WHERE market=? AND symbol=?", (market, symbol))
                    db.execute("DELETE FROM events WHERE market=? AND symbol=?", (market, symbol))
                    _write_symbol_status(db, market, symbol, "unavailable", reason, None, None)
                    continue
                event_reader = reader
                if symbol not in old_status:
                    event_reader = SourceReader(
                        BuildSpec(
                            root,
                            coverage_start,
                            through,
                            symbols=(symbol,),
                            sources=(market,),
                            classification_evidence_manifest=classification_evidence_manifest,
                        )
                    )
                    event_reader.__enter__()
                try:
                    use_events = load_events(event_reader, symbol, source=market)
                    old_last = db.execute(
                        "SELECT * FROM bars WHERE market=? AND symbol=? ORDER BY date DESC LIMIT 1",
                        (market, symbol),
                    ).fetchone()
                    first = db.execute(
                        "SELECT * FROM bars WHERE market=? AND symbol=? ORDER BY date LIMIT 1",
                        (market, symbol),
                    ).fetchone()
                    quotes: tuple[QuoteValue, ...] = ()
                    adjusted: tuple[AdjustedQuote, ...] = ()
                    bars: tuple[Bar, ...] = ()
                    if item is None:
                        pass
                    elif old_last is not None and start_date > coverage_start:
                        x = str(old_last["date"])
                        factor = Decimal(str(old_last["factor"]))
                        pending = []
                        for erow in db.execute(
                            "SELECT * FROM events WHERE market=? AND symbol=? "
                            "AND effective_date>? AND multiplier IS NOT NULL",
                            (market, symbol, x),
                        ):
                            pending.append(
                                EventValue(
                                    market,
                                    symbol,
                                    str(erow["event_type"]),
                                    str(erow["effective_date"]),
                                    Decimal(0),
                                    None,
                                    Decimal(0),
                                    Decimal(str(erow["multiplier"])),
                                    None,
                                    None,
                                    EventRef(market, "cache", "", "", 0, ""),
                                )
                            )
                        # Stored events are authoritative only before the rebuild boundary.
                        # Beyond it, source revisions (including event removal) must replace
                        # rather than be compounded with the cached event rows.
                        pending = [e for e in pending if e.effective_date < start_date]
                        pending.extend(e for e in use_events if e.effective_date >= start_date)
                        adjusted = process_quotes(
                            item.quotes, pending, initial_factor=factor, applied_through=x
                        )
                        quotes = item.quotes
                    elif item is not None:
                        quotes = item.quotes
                        adjusted = calculate_adjustments(quotes, use_events)
                    if item is not None:
                        flags = {
                            q.date: f for q, f in zip(item.quotes, item.quality_flags, strict=True)
                        }

                        def quality_for(
                            quote: QuoteValue, quality_by_date: dict[str, tuple[str, ...]] = flags
                        ) -> tuple[str, ...]:
                            return quality_by_date[quote.date]

                        bars = _build_bars(
                            market,
                            symbol,
                            quotes,
                            {q.quote.date: q for q in adjusted},
                            item.classifications,
                            quality_for,
                        )
                    actions = _build_actions(
                        market,
                        symbol,
                        use_events,
                        _bar_from_row(first) if first else (bars[0] if bars else None),
                        event_reader,
                        *_scope_context(event_reader),
                    )
                    chosen_bars = [b for b in bars if b.date >= start_date]
                    chosen = [
                        (a, e)
                        for a, e in zip(actions, use_events, strict=True)
                        if a.effective_date >= start_date
                    ]
                    _validate_symbol_output(
                        market, symbol, chosen_bars, [a for a, _ in chosen], start_date, through
                    )
                    _insert_bar_rows(
                        db,
                        [
                            _bar_row(
                                market,
                                b,
                                next(q.factor for q in adjusted if q.quote.date == b.date),
                            )
                            for b in chosen_bars
                        ],
                    )
                    _insert_event_rows(
                        db,
                        _event_rows(
                            market, tuple(a for a, _ in chosen), tuple(e for _, e in chosen)
                        ),
                    )
                except sqlite3.Error:
                    raise
                except Exception as exc:
                    db.execute("DELETE FROM bars WHERE market=? AND symbol=?", (market, symbol))
                    db.execute("DELETE FROM events WHERE market=? AND symbol=?", (market, symbol))
                    _write_symbol_status(
                        db,
                        market,
                        symbol,
                        "unavailable",
                        f"{type(exc).__name__}: {exc}",
                        None,
                        None,
                    )
                else:
                    dates = [
                        str(r[0])
                        for r in db.execute(
                            "SELECT date FROM bars WHERE market=? AND symbol=? ORDER BY date",
                            (market, symbol),
                        )
                    ]
                    if dates:
                        _write_symbol_status(
                            db, market, symbol, "available", None, dates[0], dates[-1]
                        )
                    else:
                        db.execute(
                            "DELETE FROM symbols WHERE market=? AND symbol=?", (market, symbol)
                        )
                finally:
                    if event_reader is not reader:
                        event_reader.__exit__(None, None, None)
            _record_sessions(db, market, observed, append=True)
            _record_scopes(db, market, tuple(scopes))
        db.execute(
            "UPDATE markets SET source_root=?,evidence_dirs=?,evidence_hashes=? WHERE market=?",
            (
                str(resolved_root),
                _json(sorted(str(p.resolve()) for p in evidence.protected_input_directories)),
                hashes if full_market else str(market_row["evidence_hashes"]),
                market,
            ),
        )
        counts = dict(
            db.execute(
                "SELECT status,COUNT(*) FROM symbols WHERE market=? GROUP BY status", (market,)
            )
        )
        db.commit()
        return BuildSummary(
            market,
            coverage_start,
            through,
            int(counts.get("available", 0)),
            int(counts.get("unavailable", 0)),
        )
    except BaseException:
        if db is not None:
            db.rollback()
        raise
    finally:
        if db is not None:
            db.close()


def _market(db: sqlite3.Connection, market: str) -> sqlite3.Row:
    row = db.execute("SELECT * FROM markets WHERE market=?", (market,)).fetchone()
    if row is None:
        raise CacheError(f"market not in cache: {market}")
    return cast(sqlite3.Row, row)


def cache_status(cache_path: Path) -> tuple[CacheStatus, ...]:
    db = _connect_read(cache_path)
    try:
        rows = db.execute(
            "SELECT market,coverage_start,built_through FROM markets ORDER BY market"
        ).fetchall()
        return tuple(
            CacheStatus(
                str(r[0]),
                str(r[1]),
                str(r[2]),
                int(
                    db.execute(
                        "SELECT COUNT(*) FROM symbols WHERE market=? AND status='available'",
                        (r[0],),
                    ).fetchone()[0]
                ),
                int(
                    db.execute(
                        "SELECT COUNT(*) FROM symbols WHERE market=? AND status='unavailable'",
                        (r[0],),
                    ).fetchone()[0]
                ),
            )
            for r in rows
        )
    finally:
        db.close()


def list_symbols(
    cache_path: Path, market: str, status: str | None = None
) -> tuple[SymbolStatus, ...]:
    db = _connect_read(cache_path)
    try:
        _market(db, market)
        if status not in {None, "available", "unavailable"}:
            raise ResearchInputError("status must be available, unavailable, or None")
        sql = "SELECT symbol,status,reason,first_date,last_date FROM symbols WHERE market=?"
        args: tuple[Any, ...] = (market,)
        if status is not None:
            sql += " AND status=?"
            args += (status,)
        sql += " ORDER BY symbol"
        return tuple(SymbolStatus(*row) for row in db.execute(sql, args))
    finally:
        db.close()


def _tuple(text: str) -> tuple[Any, ...]:
    return tuple(json.loads(text))


def _bar_from_row(row: sqlite3.Row) -> Bar:
    return Bar(
        market=str(row["market"]),
        symbol=str(row["symbol"]),
        date=str(row["date"]),
        open=_decimal_from(row["open"]),
        high=_decimal_from(row["high"]),
        low=_decimal_from(row["low"]),
        close=_decimal_from(row["close"]),
        volume=_decimal_from(row["volume"]),
        amount=_decimal_from(row["amount"]),
        adjusted_open=_decimal_from(row["adjusted_open"]),
        adjusted_high=_decimal_from(row["adjusted_high"]),
        adjusted_low=_decimal_from(row["adjusted_low"]),
        adjusted_close=_decimal_from(row["adjusted_close"]),
        classification=str(row["classification"]),
        classification_reason=str(row["classification_reason"]),
        classification_refs=_tuple(row["classification_refs"]),
        source_refs=_tuple(row["source_refs"]),
        quality_flags=_tuple(row["quality_flags"]),
    )


def _action_from_row(row: sqlite3.Row) -> CorporateAction:
    return CorporateAction(
        market=str(row["market"]),
        symbol=str(row["symbol"]),
        event_type=str(row["event_type"]),
        effective_date=str(row["effective_date"]),
        dividend=_decimal_from(row["dividend"]),
        bonus_share_ratio=_decimal_from(row["bonus_share_ratio"]),
        cash_return_per_share=_decimal_from(row["cash_return_per_share"]),
        share_multiplier=_decimal_from(row["share_multiplier"]),
        par_before=_decimal_from(row["par_before"]),
        par_after=_decimal_from(row["par_after"]),
        before_price=_decimal_from(row["before_price"]),
        reference_price=_decimal_from(row["reference_price"]),
        cash_credit_date=row["cash_credit_date"],
        share_available_date=row["share_available_date"],
        classification=str(row["classification"]),
        classification_reason=str(row["classification_reason"]),
        classification_refs=_tuple(row["classification_refs"]),
        source_refs=_tuple(row["source_refs"]),
        quality_flags=_tuple(row["quality_flags"]),
        reasons=_tuple(row["reasons"]),
        payload=tuple(tuple(x) for x in json.loads(row["payload"])),
    )


def load_cached(
    cache_path: Path,
    market: str,
    symbols: tuple[str, ...] | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
) -> ResearchData:
    if not isinstance(cache_path, Path):
        raise ResearchInputError("cache_path must be pathlib.Path")
    if symbols is not None and (
        not isinstance(symbols, tuple)
        or not symbols
        or any(not isinstance(s, str) or not s.strip() for s in symbols)
        or len(set(symbols)) != len(symbols)
    ):
        raise ResearchInputError("symbols must be a non-empty tuple of unique explicit symbols")
    db = _connect_read(cache_path)
    try:
        m = _market(db, market)
        start = (
            iso_date(start_date, "start_date")
            if start_date is not None
            else str(m["coverage_start"])
        )
        end = iso_date(end_date, "end_date") if end_date is not None else str(m["built_through"])
        if start > end:
            raise ResearchInputError("start_date must not be after end_date")
        if start < m["coverage_start"] or end > m["built_through"]:
            raise CacheCoverageError(
                "requested range outside cache coverage "
                f"[{m['coverage_start']}, {m['built_through']}]"
            )
        known = {
            str(r[0]): str(r[1])
            for r in db.execute("SELECT symbol,status FROM symbols WHERE market=?", (market,))
        }
        selected: tuple[str, ...]
        if symbols is not None:
            for symbol in symbols:
                if symbol not in known:
                    raise SymbolNotInCacheError(f"symbol not in cache: {symbol}")
                if known[symbol] == "unavailable":
                    reason = db.execute(
                        "SELECT reason FROM symbols WHERE market=? AND symbol=?", (market, symbol)
                    ).fetchone()[0]
                    raise SymbolUnavailableError(f"symbol unavailable: {symbol}: {reason}")
            selected = symbols
        else:
            selected = tuple(
                str(r[0])
                for r in db.execute(
                    "SELECT DISTINCT b.symbol FROM bars b JOIN symbols s USING(market,symbol) "
                    "WHERE b.market=? AND s.status='available' AND b.date BETWEEN ? AND ? "
                    "ORDER BY b.symbol",
                    (market, start, end),
                )
            )
            if not selected:
                raise NoQuotesError("no available symbols have quotes in range")
        placeholders = ",".join("?" for _ in selected)
        if symbols is not None:
            for symbol in selected:
                found = db.execute(
                    "SELECT 1 FROM bars WHERE market=? AND symbol=? "
                    "AND date BETWEEN ? AND ? LIMIT 1",
                    (market, symbol, start, end),
                ).fetchone()
                if found is None:
                    raise NoQuotesError(
                        f"no quotes for requested symbol in requested range: {symbol}"
                    )
        bar_rows = db.execute(
            f"SELECT * FROM bars WHERE market=? AND symbol IN ({placeholders}) "
            "AND date BETWEEN ? AND ? ORDER BY symbol,date",
            (market, *selected, start, end),
        ).fetchall()
        if not bar_rows:
            raise NoQuotesError("no quotes for requested symbol in requested range")
        sessions = tuple(
            str(r[0])
            for r in db.execute(
                "SELECT date FROM sessions WHERE market=? AND date BETWEEN ? AND ? ORDER BY date",
                (market, start, end),
            )
        )
        if not sessions:
            raise NoQuotesError("no sessions in requested range")
        bars = tuple(_bar_from_row(r) for r in bar_rows)
        event_rows = db.execute(
            f"SELECT * FROM events WHERE market=? AND symbol IN ({placeholders}) "
            "AND effective_date BETWEEN ? AND ? "
            "ORDER BY symbol,effective_date,event_type",
            (market, *selected, start, end),
        ).fetchall()
        events = tuple(_action_from_row(r) for r in event_rows)
        diagnostics = tuple(
            SourceDiagnostic(
                str(r["source"]),
                str(r["dataset"]),
                str(r["task_id"]),
                str(r["snapshot_id"]),
                int(r["record_count"]),
                _tuple(r["diagnostics"]),
            )
            for r in db.execute("SELECT * FROM scopes WHERE market=? ORDER BY rowid", (market,))
        )
        classification = [
            f"{b.symbol}:{b.date}:{b.classification}:{b.classification_reason}" for b in bars
        ]
        classification.extend(
            f"classification_input:{name}:{digest}"
            for name, digest in json.loads(m["evidence_hashes"]).items()
        )
        protected = {
            Path(str(m["source_root"])),
            *(Path(p) for p in json.loads(m["evidence_dirs"])),
            cache_path.resolve().parent,
        }
        return ResearchData(
            market,
            tuple(sorted({b.symbol for b in bars})),
            start,
            end,
            sessions,
            bars,
            events,
            "observed_market_quote_dates",
            diagnostics,
            tuple(sorted(classification)),
            protected_input_dirs=frozenset(protected),
        )
    finally:
        db.close()


def _decimal_from(value: str | None) -> Decimal | None:
    return Decimal(value) if value is not None else None
