"""Small internal offline quote-to-adjustment pipeline."""

from __future__ import annotations

import json
import sqlite3
import tempfile
from collections.abc import Iterator
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


def _iter_series_from_reader(reader: SourceReader, evidence: EvidenceSet) -> Iterator[QuoteSeries]:
    """Process selected records using an already-open, caller-owned reader."""
    spec = reader.spec
    # SQLite is only a bounded sorting/work area; it is never the source database.
    with tempfile.TemporaryDirectory(prefix="taiwan-series-") as directory:
        work = sqlite3.connect(Path(directory) / "work.sqlite3")
        work.execute("PRAGMA temp_store=FILE")
        work.execute(
            "CREATE TABLE quotes(source TEXT,symbol TEXT,day TEXT,payload TEXT,"
            "classification TEXT,reason TEXT,evidence TEXT,refs TEXT)"
        )
        work.execute("CREATE INDEX quotes_source_symbol_day ON quotes(source,symbol,day)")
        try:
            for scope in reader.scopes():
                if not scope.usable:
                    raise UnavailableDataError(
                        f"{scope.source}/{scope.dataset} task={scope.task_id}: {scope.diagnostics}"
                    )
                if scope.lane != "quotes":
                    continue
                for record in reader.iter_all_records(scope):
                    quote, classification, reason, refs = normalize_quote(scope, record, evidence)
                    if not spec.start_date <= quote.date <= spec.end_date:
                        continue
                    work.execute(
                        "INSERT INTO quotes VALUES(?,?,?,?,?,?,?,?)",
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
                        ),
                    )
            work.commit()
            identities = work.execute(
                "SELECT DISTINCT source,symbol FROM quotes ORDER BY source,symbol"
            )
            for source, symbol in identities:
                raw_rows = work.execute(
                    "SELECT day,payload,classification,reason,evidence,refs FROM quotes "
                    "WHERE source=? AND symbol=? ORDER BY day, rowid",
                    (source, symbol),
                ).fetchall()
                quotes: list[QuoteValue] = []
                classes: list[QuoteClassification] = []
                for day, raw, category, reason, evidence_json, refs_json in raw_rows:
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
                ordinary = all(
                    item.classification in {"ordinary_evidence_supported", "ordinary_rule_limited"}
                    for item in classes
                )
                own_events = load_events(reader, str(symbol), source=str(source))
                adjusted = calculate_adjustments(quotes, own_events) if ordinary else ()
                yield QuoteSeries(
                    str(source),
                    str(symbol),
                    tuple(quotes),
                    adjusted,
                    tuple(classes),
                    own_events,
                )
        except DatasetError:
            raise
        except Exception as exc:
            raise DatasetError(f"offline dataset pipeline failed: {exc}") from exc
        finally:
            work.close()
