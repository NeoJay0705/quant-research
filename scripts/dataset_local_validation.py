"""Bounded local discover/verify helper; development-only, read-only."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from dataclasses import fields, is_dataclass
from datetime import date
from decimal import ROUND_HALF_EVEN, Context, Decimal, InvalidOperation, localcontext
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dataset_local_inventory import inventory  # noqa: E402

from quant_research.taiwan.dataset.build import iter_series  # noqa: E402
from quant_research.taiwan.dataset.events import EventRef, EventValue  # noqa: E402
from quant_research.taiwan.dataset.models import BuildSpec, DatasetError, QuoteValue, SourceError  # noqa: E402
from quant_research.taiwan.dataset.source import SourceReader  # noqa: E402


def _spec(args: argparse.Namespace, symbol: str | None = None) -> BuildSpec:
    try:
        start = date.fromisoformat(args.start)
        end = date.fromisoformat(args.end)
    except ValueError as exc:
        raise ValueError("start/end must be valid ISO dates") from exc
    if start.isoformat() != args.start or end.isoformat() != args.end or end < start:
        raise ValueError("start/end must be ordered ISO dates")
    if (end - start).days + 1 > 62:
        raise ValueError("date range may contain at most 62 inclusive days")
    return BuildSpec(
        Path(args.root).expanduser(),
        args.start,
        args.end,
        (symbol,) if symbol else None,
        (args.market,) if getattr(args, "market", None) else ("twse", "tpex"),
    )


def _json(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if is_dataclass(value):
        return {f.name: _json(getattr(value, f.name)) for f in fields(value)}
    if isinstance(value, dict):
        return {str(k): _json(v) for k, v in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json(v) for v in value]
    return value


def discover(spec: BuildSpec, limit: int) -> dict[str, Any]:
    if limit < 1 or limit > 100:
        raise ValueError("--max-candidates must be between 1 and 100")
    with SourceReader(spec) as reader:
        candidates = []
        event_left = {
            "twse": {"dividend": limit, "capital_reduction": limit, "par_value": limit},
            "tpex": {"dividend": limit, "capital_reduction": limit, "par_value": limit},
        }
        quote_left = {"twse": min(limit, 10), "tpex": min(limit, 10)}
        for scope in reader.scopes():
            item = {
                "source": scope.source,
                "lane": scope.lane,
                "dataset": scope.dataset,
                "task_id": scope.task_id,
                "edition": scope.edition,
                "snapshot_id": scope.snapshot_id,
                "fetched_at": scope.fetched_at,
                "sha256": scope.sha256,
                "raw_path": scope.raw_path,
                "availability": scope.availability,
                "diagnostics": scope.diagnostics,
                "rows": [],
            }
            if scope.usable and scope.lane == "quotes" and quote_left[scope.source]:
                for rec in reader.iter_all_records(scope, page_size=100):
                    p = rec.payload or {}
                    day = p.get("trade_date")
                    if (
                        not rec.diagnostic
                        and isinstance(day, str)
                        and spec.start_date <= day <= spec.end_date
                    ):
                        item["rows"].append(
                            {
                                "symbol": p.get("symbol"),
                                "date": day,
                                "record_id": rec.record_id,
                                "task_id": scope.task_id,
                                "snapshot_id": scope.snapshot_id,
                            }
                        )
                        quote_left[scope.source] -= 1
                        if not quote_left[scope.source]:
                            break
            elif scope.usable and scope.lane == "events" and any(event_left[scope.source].values()):
                for rec in reader.iter_all_records(scope, page_size=100):
                    p = rec.payload or {}
                    day = p.get("event_date")
                    typ = p.get("event_type")
                    if (
                        rec.diagnostic
                        or not isinstance(day, str)
                        or not spec.start_date <= day <= spec.end_date
                        or not event_left[scope.source].get(typ, 0)
                    ):
                        continue
                    links = []
                    for link in reader.links(scope):
                        context = link.get("context")
                        if isinstance(context, dict) and (
                            context.get("symbol"),
                            context.get("event_date"),
                        ) == (p.get("symbol"), day):
                            meta = reader.child_scope(link)
                            links.append(
                                {
                                    "dataset": meta.get("dataset") if meta else None,
                                    "available": bool(meta and meta.get("available")),
                                    "task_id": meta.get("task_id") if meta else None,
                                    "snapshot_id": meta.get("snapshot_id") if meta else None,
                                }
                            )
                    item["rows"].append(
                        {
                            "symbol": p.get("symbol"),
                            "date": day,
                            "event_type": typ,
                            "record_id": rec.record_id,
                            "task_id": scope.task_id,
                            "snapshot_id": scope.snapshot_id,
                            "detail_links": links,
                        }
                    )
                    event_left[scope.source][typ] -= 1
                    if not any(event_left[scope.source].values()):
                        break
            candidates.append(item)
        return {
            "operation": "discover",
            "range": [spec.start_date, spec.end_date],
            "event_candidates_per_market_type_limit": limit,
            "quote_sample_per_market_limit": min(limit, 10),
            "source_reader_selection_reused": True,
            "scopes": candidates,
        }


def _oracle(quotes: list[Any], events: tuple[Any, ...]) -> list[dict[str, Any]]:
    """Independent sequential arithmetic, isolated from caller Decimal context."""
    with localcontext(Context(prec=50, rounding=ROUND_HALF_EVEN, Emin=-999999, Emax=999999)):
        ordered_events = sorted(events, key=lambda e: e.effective_date)
        output = []
        factor = Decimal(1)
        previous = quotes[0].date if quotes else ""
        for quote in quotes:
            for event in ordered_events:
                if previous < event.effective_date <= quote.date and event.event_type in {
                    "capital_reduction",
                    "par_value",
                }:
                    if event.event_type == "capital_reduction":
                        multiplier = event.share_multiplier
                    elif event.share_multiplier is not None:
                        multiplier = event.share_multiplier
                    else:
                        multiplier = event.before_price / event.reference_price
                    if multiplier is None or not multiplier.is_finite() or multiplier <= 0:
                        raise ValueError("event multiplier must be finite and positive")
                    factor = +(factor * multiplier)
            values = [
                None if v is None else +(v * factor)
                for v in (quote.open, quote.high, quote.low, quote.close)
            ]
            output.append({"date": quote.date, "factor": factor, "ohlc": values})
            previous = quote.date
        return output


def _decimal(value: Any, *, optional: bool = False) -> Decimal | None:
    if value is None and optional:
        return None
    if isinstance(value, bool) or isinstance(value, float):
        raise DatasetError(f"invalid decimal source value: {value!r}")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise DatasetError(f"invalid decimal source value: {value!r}") from exc
    if not result.is_finite():
        raise DatasetError(f"non-finite decimal source value: {value!r}")
    return result


def _raw_inputs(reader: SourceReader, market: str, symbol: str, spec: BuildSpec):
    from quant_research.taiwan.dataset.build import QuoteClassification
    from quant_research.taiwan.dataset.classification import EvidenceSet

    evidence = EvidenceSet.from_manifest(spec.classification_evidence_manifest)
    quotes, classes = [], []
    selected_events = {}
    for scope in reader.scopes():
        if scope.source != market:
            continue
        if not scope.usable:
            if scope.lane == "quotes" or scope.lane == "events":
                raise DatasetError(f"latest {scope.lane} scope unavailable: {scope.diagnostics}")
        if scope.lane == "quotes":
            for rec in reader.iter_all_records(scope, symbol=symbol):
                p = rec.payload
                if rec.diagnostic or not isinstance(p, dict):
                    raise DatasetError(f"invalid quote record {rec.record_id}: {rec.diagnostic}")
                day = p.get("trade_date")
                if not isinstance(day, str) or not spec.start_date <= day <= spec.end_date:
                    continue
                values = [
                    _decimal(p.get(k), optional=True)
                    for k in ("open", "high", "low", "close", "volume", "amount")
                ]
                quote = QuoteValue(
                    market,
                    symbol,
                    day,
                    *values,
                    (f"{scope.dataset}:{scope.snapshot_id}:{rec.record_id}",),
                )
                venue_id, venue = p.get("venue_id"), p.get("venue")
                ambiguous = not (
                    venue_id == "primary"
                    or market == "twse"
                    and isinstance(venue_id, str)
                    and "每日收盤行情" in venue_id
                )
                ambiguous |= isinstance(venue, str) and "管理" in venue
                classification = evidence.classify(symbol, p.get("name"), ambiguous=ambiguous)
                quotes.append(quote)
                classes.append(
                    QuoteClassification(
                        classification.classification,
                        classification.reason,
                        classification.evidence_refs,
                    )
                )
        elif scope.lane == "events":
            # Candidate processing stops at bounded source rows after matching this symbol/window.
            for rec in reader.iter_all_records(scope, symbol=symbol, page_size=250):
                p = rec.payload
                if rec.diagnostic or not isinstance(p, dict):
                    raise DatasetError(f"invalid event record {rec.record_id}: {rec.diagnostic}")
                day, kind = p.get("event_date"), p.get("event_type")
                if not isinstance(day, str) or not spec.start_date <= day <= spec.end_date:
                    continue
                key = (symbol, kind, day)
                stamp = (scope.fetched_at or "", scope.task_id, str(rec.record_id))
                if key not in selected_events or stamp > selected_events[key][0]:
                    selected_events[key] = (stamp, scope, rec, p)
    quotes_with_sort = sorted(zip(quotes, classes, strict=True), key=lambda x: x[0].date)
    quotes = [x[0] for x in quotes_with_sort]
    classes = [x[1] for x in quotes_with_sort]
    events = []
    for _, scope, rec, p in selected_events.values():
        detail_payload, detail_ref = None, None
        required = market == "twse" and scope.dataset in {"TWT49U", "TWTAUU"}
        expected_detail = {"TWT49U": "TWT49UDetail", "TWTAUU": "TWTAVUDetail"}.get(scope.dataset)
        if required:
            for link in reader.links(scope):
                context = link.get("context")
                if not isinstance(context, dict) or (
                    context.get("summary_dataset"),
                    context.get("symbol"),
                    context.get("event_date"),
                ) != (scope.dataset, symbol, p["event_date"]):
                    continue
                meta = reader.child_scope(link)
                if not meta or meta.get("dataset") != expected_detail or not meta.get("available"):
                    raise DatasetError(
                        f"first matching detail link unavailable or wrong dataset: {meta}"
                    )
                children = reader.iter_child_records(link, limit=10000)
                child = next(iter(children), None)
                if child is None or child.diagnostic or not isinstance(child.payload, dict):
                    raise DatasetError(
                        f"first matching detail child unavailable/diagnostic: "
                        f"{getattr(child, 'diagnostic', None)}"
                    )
                if child.payload.get("symbol") != symbol:
                    raise DatasetError(
                        f"first matching detail child symbol mismatch: "
                        f"expected={symbol}, actual={child.payload.get('symbol')}"
                    )
                detail_payload = child.payload
                detail_ref = (meta, child.record_id)
                break
            if detail_payload is None:
                raise DatasetError(
                    "required linked detail unavailable for "
                    f"{scope.dataset} {symbol} {p['event_date']}"
                )
        data = detail_payload or p

        dividend = _decimal(data.get("cash_dividend_per_share"), optional=True) or Decimal(0)
        returned = _decimal(data.get("cash_return_per_share"), optional=True) or Decimal(0)
        bonus = _decimal(data.get("bonus_share_ratio"), optional=True)
        mult = _decimal(data.get("share_multiplier"), optional=True)
        before, reference = (
            _decimal(p.get("before_price"), optional=True),
            _decimal(p.get("reference_price"), optional=True),
        )
        meta, child_id = detail_ref if detail_ref else (None, None)
        ref = EventRef(
            market,
            scope.dataset,
            scope.task_id,
            scope.snapshot_id or "",
            rec.record_id,
            scope.fetched_at or "",
            meta["dataset"] if meta else None,
            meta["task_id"] if meta else None,
            meta["snapshot_id"] if meta else None,
            child_id,
        )
        if p["event_type"] == "capital_reduction" and (mult is None or mult <= 0):
            raise DatasetError("capital reduction requires a finite positive share multiplier")
        if p["event_type"] == "par_value":
            if mult is not None and mult <= 0:
                raise DatasetError("par-value multiplier must be positive")
            if mult is None and (
                before is None or reference is None or before <= 0 or reference <= 0
            ):
                raise DatasetError("par-value event requires positive before/reference prices")
        events.append(
            EventValue(
                market,
                symbol,
                p["event_type"],
                p["event_date"],
                dividend,
                bonus,
                returned,
                mult,
                before,
                reference,
                ref,
            )
        )
    return quotes, classes, tuple(sorted(events, key=lambda e: (e.event_type, e.effective_date)))


def verify(spec: BuildSpec, market: str, symbol: str) -> dict[str, Any]:
    dbpath = spec.root / "state.sqlite3"
    if not dbpath.is_file():
        raise SourceError(f"source database does not exist: {dbpath}")
    # Dedicated autocommit read-only connection: data_version is sampled without a pinned txn.
    version = sqlite3.connect(
        dbpath.resolve().as_uri() + "?mode=ro", uri=True, isolation_level=None
    )
    try:
        version.execute("PRAGMA query_only=ON")
        before = int(version.execute("PRAGMA data_version").fetchone()[0])
        with SourceReader(spec) as reader:
            quotes, classes, events = _raw_inputs(reader, market, symbol, spec)
            evidence_scopes = [
                {
                    "source": scope.source,
                    "lane": scope.lane,
                    "dataset": scope.dataset,
                    "task_id": scope.task_id,
                    "snapshot_id": scope.snapshot_id,
                    "fetched_at": scope.fetched_at,
                    "sha256": scope.sha256,
                    "raw_path": scope.raw_path,
                }
                for scope in reader.scopes()
                if scope.source == market and scope.lane in {"quotes", "events"}
            ]
            results = [s for s in iter_series(spec) if s.source == market and s.symbol == symbol]
            if len(quotes) == 0 or len(results) != 1:
                raise DatasetError("empty or missing data cannot verify a nonempty case")
            result = results[0]
            expected = _oracle(quotes, events)
            checks = []

            def check(name: str, ok: bool, **details: Any) -> None:
                checks.append({"check": name, "status": "passed" if ok else "failed", **details})

            actual_rows = []
            identities = [(q.source, q.symbol, q.date) for q in result.raw_quotes]
            expected_ids = [(market, symbol, q.date) for q in quotes]
            check(
                "raw_result_identities",
                identities == expected_ids and len(set(identities)) == len(identities),
                expected=expected_ids,
                actual=identities,
            )
            class_ok = len(result.classifications) == len(classes)
            check(
                "classification_count",
                class_ok,
                expected=len(classes),
                actual=len(result.classifications),
            )
            raw_map = {(q.source, q.symbol, q.date): q for q in result.raw_quotes}
            adjusted_ids = [(q.quote.source, q.quote.symbol, q.quote.date) for q in result.quotes]
            ordinary_series = all(
                c.classification in {"ordinary_evidence_supported", "ordinary_rule_limited"}
                for c in classes
            )
            expected_adj_ids = expected_ids if ordinary_series else []
            check(
                "adjusted_result_identities",
                adjusted_ids == expected_adj_ids and len(set(adjusted_ids)) == len(adjusted_ids),
                expected=expected_adj_ids,
                actual=adjusted_ids,
            )
            if class_ok:
                for i, q in enumerate(quotes):
                    cls = classes[i]
                    result_class = result.classifications[i]
                    check(
                        f"classification:{q.date}",
                        (
                            result_class.classification,
                            result_class.reason,
                            result_class.evidence_refs,
                        )
                        == (cls.classification, cls.reason, cls.evidence_refs),
                        expected=_json(cls),
                        actual=_json(result_class),
                    )
                    raw_ok = raw_map.get((market, symbol, q.date)) == q
                    check(
                        f"raw_and_refs:{q.date}",
                        raw_ok,
                        expected=_json(q),
                        actual=_json(raw_map.get((market, symbol, q.date))),
                    )
                    aq = result.quotes[i] if ordinary_series and i < len(result.quotes) else None
                    expected_row = expected[i]
                    financial_ok = (
                        (
                            aq is not None
                            and aq.quote == q
                            and aq.factor == expected_row["factor"]
                            and [aq.open, aq.high, aq.low, aq.close] == expected_row["ohlc"]
                        )
                        if ordinary_series
                        else aq is None
                    )
                    check(
                        f"adjustment:{q.date}",
                        financial_ok,
                        expected={**expected_row, "raw": _json(q)},
                        actual=_json(aq),
                    )
                    actual_rows.append(
                        {
                            "date": q.date,
                            "raw": _json(q),
                            "classification": cls.classification,
                            "expected": expected_row,
                            "actual": _json(aq),
                        }
                    )
            check(
                "event_terms_and_refs",
                result.events == events,
                expected=_json(events),
                actual=_json(result.events),
            )
            first_day, last_day = quotes[0].date, quotes[-1].date
            applied_events = [
                e for e in events if e.event_type in {"capital_reduction", "par_value"}
            ]
            categories = {
                "ordinary_no_exchange": ordinary_series and not applied_events,
                "reduction_boundary": any(
                    e.event_type == "capital_reduction"
                    and first_day < e.effective_date <= last_day
                    and any(q.date < e.effective_date for q in quotes)
                    and any(q.date >= e.effective_date for q in quotes)
                    for e in events
                ),
                "par_value_boundary": any(
                    e.event_type == "par_value"
                    and first_day < e.effective_date <= last_day
                    and any(q.date < e.effective_date for q in quotes)
                    and any(q.date >= e.effective_date for q in quotes)
                    for e in events
                ),
                "dividend": any(e.event_type == "dividend" and e.dividend != 0 for e in events),
                "bonus": any(e.bonus_share_ratio not in (None, 0) for e in events),
                "cash_return": any(e.cash_return_per_share != 0 for e in events),
                "missing_values": any(
                    any(v is None for v in (q.open, q.high, q.low, q.close, q.volume, q.amount))
                    for q in quotes
                ),
                "nonordinary_or_unknown": any(
                    c.classification not in {"ordinary_evidence_supported", "ordinary_rule_limited"}
                    for c in classes
                ),
                "first_day_exclusion": any(
                    e.event_type in {"capital_reduction", "par_value"}
                    and e.effective_date == first_day
                    for e in events
                ),
                "no_quote_day": any(
                    e.event_type in {"capital_reduction", "par_value"}
                    and first_day < e.effective_date <= last_day
                    and not any(q.date == e.effective_date for q in quotes)
                    for e in events
                ),
            }
            unverified = [name for name, present in categories.items() if not present]
            after = int(version.execute("PRAGMA data_version").fetchone()[0])
            check("source_unchanged", before == after, before=before, after=after)
            comparisons_ok = all(c["status"] != "failed" for c in checks)
            for name, present in categories.items():
                checks.append(
                    {
                        "check": "case:" + name,
                        "status": "unverified"
                        if not present
                        else ("passed" if comparisons_ok else "failed"),
                    }
                )
            return {
                "operation": "verify",
                "market": market,
                "symbol": symbol,
                "range": [spec.start_date, spec.end_date],
                "source_reader_and_evidence_classifier_reused": True,
                "classification_evidence_manifest": str(spec.classification_evidence_manifest)
                if spec.classification_evidence_manifest
                else None,
                "selected_source_scopes": evidence_scopes,
                "original_values_independently_exchange_certified": False,
                "oracle": (
                    "independent strict Decimal parsing and arithmetic; "
                    "no production normalizers or adjustment functions"
                ),
                "checks": checks,
                "unverified": unverified,
                "rows": actual_rows,
                "passed": all(c["status"] != "failed" for c in checks),
            }
    finally:
        version.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, help="existing local Taiwan data root (read-only)")
    sub = parser.add_subparsers(dest="operation", required=True)
    sub.add_parser("inventory", help="inventory the whole saved official source (read-only)")
    for name in ("discover", "verify"):
        p = sub.add_parser(name)
        p.add_argument("--start", required=True)
        p.add_argument("--end", required=True)
        if name == "discover":
            p.add_argument("--max-candidates", type=int, default=20)
        else:
            p.add_argument("--market", required=True, choices=("twse", "tpex"))
            p.add_argument("--symbol", required=True)
    args = parser.parse_args(argv)
    try:
        if args.operation == "inventory":
            spec = BuildSpec(Path(args.root).expanduser(), "0001-01-01", "9999-12-31")
            with SourceReader(spec) as reader:
                result = inventory(reader)
        else:
            spec = _spec(args, getattr(args, "symbol", None))
            result = (
                discover(spec, args.max_candidates)
                if args.operation == "discover"
                else verify(spec, args.market, args.symbol)
            )
        print(json.dumps(_json(result), ensure_ascii=False, sort_keys=True))
        return 0 if result.get("passed", True) else 1
    except (ValueError, DatasetError, OSError, sqlite3.Error) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
