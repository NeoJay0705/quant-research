"""Small internal offline quote-to-adjustment pipeline."""

from __future__ import annotations

import json
import sqlite3
import tempfile
from collections.abc import Callable, Generator, Iterator
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from .classification import EvidenceSet
from .events import EventValue, load_events
from .factors import calculate_adjustments
from .models import AdjustedQuote, BuildSpec, DatasetError, QuoteValue, UnavailableDataError
from .quotes import normalize_quote
from .source import SourceReader


@dataclass(frozen=True, slots=True)
class QuoteClassification:
    classification: str
    reason: str
    evidence_refs: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RawSeries:
    """Normalized quote rows for one source/security, before event adjustments."""

    source: str
    symbol: str
    quotes: tuple[QuoteValue, ...]
    classifications: tuple[QuoteClassification, ...]
    quality_flags: tuple[tuple[str, ...], ...]


@dataclass(frozen=True, slots=True)
class QuoteSeries:
    """Internal per-market/security result; classifications align with quotes."""

    source: str
    symbol: str
    raw_quotes: tuple[QuoteValue, ...]
    quotes: tuple[AdjustedQuote, ...]
    classifications: tuple[QuoteClassification, ...]
    events: tuple[EventValue, ...]


def iter_series(spec: BuildSpec) -> Iterator[QuoteSeries]:
    """Yield adjusted series one source/security at a time from selected local records."""
    evidence = EvidenceSet.from_manifest(spec.classification_evidence_manifest)
    with SourceReader(spec) as reader:
        yield from _iter_series_from_reader(reader, evidence)


def _iter_raw_series(
    reader: SourceReader,
    evidence: EvidenceSet,
    *,
    skip_symbol: Callable[[str], str | None] | None = None,
    on_skip: Callable[[str, str, str], None] | None = None,
    on_symbol_error: Callable[[str, str, Exception], None] | None = None,
) -> Generator[RawSeries]:
    """Read and normalize quote records using a caller-owned reader."""
    spec = reader.spec
    with tempfile.TemporaryDirectory(prefix="taiwan-series-") as directory:
        work = sqlite3.connect(Path(directory) / "work.sqlite3")
        work.execute("PRAGMA temp_store=FILE")
        work.execute(
            "CREATE TABLE quotes(source TEXT,symbol TEXT,day TEXT,payload TEXT,"
            "classification TEXT,reason TEXT,evidence TEXT,refs TEXT,quality TEXT)"
        )
        work.execute("CREATE INDEX quotes_source_symbol_day ON quotes(source,symbol,day)")
        failed: set[tuple[str, str]] = set()
        skipped: set[tuple[str, str]] = set()
        try:
            for scope in reader.scopes():
                if not scope.usable:
                    raise UnavailableDataError(
                        f"{scope.source}/{scope.dataset} task={scope.task_id}: {scope.diagnostics}"
                    )
                if scope.lane != "quotes":
                    continue
                for record in reader.iter_all_records(scope):
                    payload_symbol = (
                        record.payload.get("symbol") if record.payload is not None else None
                    )
                    identity = (
                        (scope.source, payload_symbol) if isinstance(payload_symbol, str) else None
                    )
                    if identity is not None and identity in failed:
                        continue
                    if identity is not None and skip_symbol is not None:
                        assert isinstance(payload_symbol, str)
                        skip_reason = skip_symbol(payload_symbol)
                        if skip_reason is not None:
                            if identity not in skipped:
                                skipped.add(identity)
                                if on_skip is not None:
                                    on_skip(scope.source, payload_symbol, skip_reason)
                            continue
                    try:
                        quote, classification, reason, refs = normalize_quote(
                            scope, record, evidence
                        )
                    except DatasetError as exc:
                        if on_symbol_error is None or identity is None or not payload_symbol:
                            raise
                        assert isinstance(payload_symbol, str)
                        failed.add(identity)
                        on_symbol_error(scope.source, payload_symbol, exc)
                        work.execute("DELETE FROM quotes WHERE source=? AND symbol=?", identity)
                        continue
                    if not spec.start_date <= quote.date <= spec.end_date:
                        continue
                    work.execute(
                        "INSERT INTO quotes VALUES(?,?,?,?,?,?,?,?,?)",
                        (
                            quote.source,
                            quote.symbol,
                            quote.date,
                            json.dumps(
                                [
                                    str(x) if x is not None else None
                                    for x in (
                                        quote.open,
                                        quote.high,
                                        quote.low,
                                        quote.close,
                                        quote.volume,
                                        quote.amount,
                                    )
                                ]
                            ),
                            classification,
                            reason,
                            json.dumps(refs),
                            json.dumps(quote.refs),
                            json.dumps(record.quality_flags),
                        ),
                    )
            work.commit()
            identities = work.execute(
                "SELECT DISTINCT source,symbol FROM quotes ORDER BY source,symbol"
            )
            for source, symbol in identities:
                raw_rows = work.execute(
                    "SELECT day,payload,classification,reason,evidence,refs,quality FROM quotes "
                    "WHERE source=? AND symbol=? ORDER BY day, rowid",
                    (source, symbol),
                ).fetchall()
                quotes: list[QuoteValue] = []
                classes: list[QuoteClassification] = []
                flags: list[tuple[str, ...]] = []
                for day, raw, category, reason, evidence_json, refs_json, quality_json in raw_rows:
                    vals = json.loads(raw)
                    values = [Decimal(v) if v is not None else None for v in vals]
                    quotes.append(
                        QuoteValue(
                            str(source),
                            str(symbol),
                            str(day),
                            values[0],
                            values[1],
                            values[2],
                            values[3],
                            values[4],
                            values[5],
                            tuple(json.loads(refs_json)),
                        )
                    )
                    classes.append(
                        QuoteClassification(
                            str(category), str(reason), tuple(json.loads(evidence_json))
                        )
                    )
                    flags.append(tuple(json.loads(quality_json)))
                yield RawSeries(
                    str(source), str(symbol), tuple(quotes), tuple(classes), tuple(flags)
                )
        except DatasetError:
            raise
        except Exception as exc:
            raise DatasetError(f"offline dataset pipeline failed: {exc}") from exc
        finally:
            work.close()


def _iter_series_from_reader(reader: SourceReader, evidence: EvidenceSet) -> Iterator[QuoteSeries]:
    """Process selected records using an already-open, caller-owned reader."""
    raw_series = _iter_raw_series(reader, evidence)
    try:
        for raw in raw_series:
            ordinary = all(
                item.classification in {"ordinary_evidence_supported", "ordinary_rule_limited"}
                for item in raw.classifications
            )
            own_events = load_events(reader, raw.symbol, source=raw.source)
            adjusted = calculate_adjustments(raw.quotes, own_events) if ordinary else ()
            yield QuoteSeries(
                raw.source, raw.symbol, raw.quotes, adjusted, raw.classifications, own_events
            )
    except DatasetError:
        raise
    except Exception as exc:
        raise DatasetError(f"offline dataset pipeline failed: {exc}") from exc
    finally:
        raw_series.close()
