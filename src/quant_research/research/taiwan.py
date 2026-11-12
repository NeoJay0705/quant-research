"""Read-only adapter from the existing Taiwan Dataset into ResearchData."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from decimal import Decimal
from pathlib import Path
from typing import Any

from quant_research.taiwan.dataset.build import (
    QuoteClassification,
    _iter_series_from_reader,
)
from quant_research.taiwan.dataset.classification import EvidenceSet
from quant_research.taiwan.dataset.events import EventValue
from quant_research.taiwan.dataset.financial import parse_decimal
from quant_research.taiwan.dataset.models import (
    AdjustedQuote,
    BuildSpec,
    DatasetError,
    QuoteValue,
    SourceError,
)
from quant_research.taiwan.dataset.source import RecordResult, Scope, SourceReader

from .data import ResearchData
from .models import (
    Bar,
    CorporateAction,
    ResearchError,
    ResearchInputError,
    SourceDiagnostic,
    UnsupportedDataError,
)


def _build_bars(
    market: str,
    symbol: str,
    raw_quotes: tuple[QuoteValue, ...],
    adjusted_by_day: Mapping[str, AdjustedQuote],
    classifications: tuple[QuoteClassification, ...],
    quality_flags: Callable[[QuoteValue], tuple[str, ...]],
) -> tuple[Bar, ...]:
    """Convert raw quotes and independent adjustment/classification inputs to bars."""
    result: list[Bar] = []
    for quote, cls in zip(raw_quotes, classifications, strict=True):
        adjusted = adjusted_by_day.get(quote.date)
        result.append(
            Bar(
                market=market,
                symbol=symbol,
                date=quote.date,
                open=quote.open,
                high=quote.high,
                low=quote.low,
                close=quote.close,
                volume=quote.volume,
                amount=quote.amount,
                adjusted_open=adjusted.open if adjusted else None,
                adjusted_high=adjusted.high if adjusted else None,
                adjusted_low=adjusted.low if adjusted else None,
                adjusted_close=adjusted.close if adjusted else None,
                classification=cls.classification,
                classification_reason=cls.reason,
                classification_refs=cls.evidence_refs,
                source_refs=quote.refs,
                quality_flags=quality_flags(quote),
            )
        )
    return tuple(result)


def _build_actions(
    market: str,
    symbol: str,
    events: tuple[EventValue, ...],
    selected_bar: Bar | None,
    reader: SourceReader,
    scope_by_ref: Mapping[tuple[str, str | None], Scope],
    link_cache: Mapping[tuple[str, str | None], tuple[dict[str, Any], ...]],
    summary_records: Mapping[tuple[str, str | None, int], RecordResult],
    child_record_cache: dict[tuple[int, str, str], tuple[RecordResult, ...]],
) -> tuple[CorporateAction, ...]:
    """Resolve event provenance and convert normalized events without opening a reader."""
    actions: list[CorporateAction] = []
    for event in events:
        er = event.ref
        summary_scope = scope_by_ref.get((er.task_id, er.snapshot_id))
        if summary_scope is None:
            raise UnsupportedDataError(f"missing event summary scope for {er}")
        summary = summary_records.get((er.task_id, er.snapshot_id, er.record_id))
        if summary is None or summary.diagnostic or summary.payload is None:
            raise UnsupportedDataError(f"invalid event summary record for {er}: {summary}")
        flags = summary.quality_flags
        payload: dict[str, object] = dict(summary.payload)
        if er.detail_task_id is not None:
            links = link_cache.get((er.task_id, er.snapshot_id), ())
            child_link = next(
                (
                    link
                    for link in links
                    if link["child_task_id"] == er.detail_task_id
                    and link["parent_task_id"] == er.task_id
                    and link["parent_snapshot_id"] == er.snapshot_id
                ),
                None,
            )
            if child_link is None:
                raise UnsupportedDataError(f"missing exact event child link for {er}")
            child_meta = reader.child_scope(child_link)
            if (
                child_meta is None
                or not child_meta["available"]
                or child_meta["task_id"] != er.detail_task_id
                or child_meta["snapshot_id"] != er.detail_snapshot_id
                or child_meta["dataset"] != er.detail_dataset
            ):
                raise UnsupportedDataError(
                    f"invalid exact event child scope for {er}: {child_meta}"
                )
            link_key = (
                int(child_link["link_id"]),
                er.detail_task_id,
                er.detail_snapshot_id or "",
            )
            if link_key not in child_record_cache:
                child_record_cache[link_key] = tuple(
                    reader.iter_child_records(child_link, limit=10000)
                )
            child = next(
                (r for r in child_record_cache[link_key] if r.record_id == er.detail_record_id),
                None,
            )
            if child is None or child.diagnostic or child.payload is None:
                raise UnsupportedDataError(f"invalid event child record for {er}: {child}")
            flags = tuple(dict.fromkeys((*flags, *child.quality_flags)))
            payload.update({k: v for k, v in child.payload.items() if v is not None})

        def decimal_field(
            name: str, *, record_ref: object = er, fields: dict[str, object] = payload
        ) -> Decimal | None:
            value = fields.get(name)
            if value is None:
                return None
            try:
                return parse_decimal(value)
            except (ValueError, TypeError) as exc:
                raise UnsupportedDataError(f"invalid {name} in {record_ref}") from exc

        if selected_bar is None:
            raise UnsupportedDataError(f"event has no selected quote bar for {symbol}: {er}")
        actions.append(
            CorporateAction(
                market=market,
                symbol=event.symbol,
                event_type=event.event_type,
                effective_date=event.effective_date,
                dividend=event.dividend,
                bonus_share_ratio=event.bonus_share_ratio,
                cash_return_per_share=event.cash_return_per_share,
                share_multiplier=event.share_multiplier,
                par_before=decimal_field("par_before"),
                par_after=decimal_field("par_after"),
                before_price=event.before_price,
                reference_price=event.reference_price,
                cash_credit_date=(
                    str(payload["cash_credit_date"])
                    if payload.get("cash_credit_date") is not None
                    else None
                ),
                share_available_date=(
                    str(payload["share_available_date"])
                    if payload.get("share_available_date") is not None
                    else None
                ),
                classification=selected_bar.classification,
                classification_reason=selected_bar.classification_reason,
                classification_refs=selected_bar.classification_refs,
                source_refs=(f"{er.dataset}:{er.snapshot_id}:{er.record_id}",)
                + (
                    (f"{er.detail_dataset}:{er.detail_snapshot_id}:{er.detail_record_id}",)
                    if er.detail_dataset
                    else ()
                ),
                quality_flags=flags,
                reasons=tuple(flags),
                payload=tuple(
                    sorted(
                        (str(k), str(v) if v is not None else None)
                        for k, v in payload.items()
                        if isinstance(v, (str, int, bool)) or v is None
                    )
                ),
            )
        )
    return tuple(actions)


def load_taiwan(
    root: Path,
    market: str,
    symbols: tuple[str, ...],
    start_date: str,
    end_date: str,
    classification_evidence_manifest: Path | None = None,
    max_rows: int = 100_000,
    sessions: tuple[str, ...] | None = None,
) -> ResearchData:
    """Load an explicitly scoped read-only Taiwan research dataset (not a Dataset bundle)."""
    if market not in {"twse", "tpex"}:
        raise ResearchInputError("market must be 'twse' or 'tpex'")
    if not isinstance(root, Path):
        raise ResearchInputError("root must be pathlib.Path")
    from .models import iso_date

    start_date = iso_date(start_date, "start_date")
    end_date = iso_date(end_date, "end_date")
    if start_date > end_date:
        raise ResearchInputError("start_date must not be after end_date")
    if (
        not isinstance(symbols, tuple)
        or not symbols
        or any(not isinstance(s, str) or not s.strip() for s in symbols)
    ):
        raise ResearchInputError("symbols must be a non-empty tuple of explicit symbols")
    if len(set(symbols)) != len(symbols):
        raise ResearchInputError("symbols must be unique")
    if isinstance(max_rows, bool) or not isinstance(max_rows, int) or max_rows < 1:
        raise ResearchInputError("max_rows must be a positive integer")
    spec = BuildSpec(
        root,
        start_date,
        end_date,
        symbols=symbols,
        sources=(market,),
        classification_evidence_manifest=classification_evidence_manifest,
    )
    if sessions is not None:
        if not isinstance(sessions, tuple) or not sessions:
            raise ResearchInputError("explicit sessions must be a non-empty tuple")
        # Core model performs canonical date/order/range checks; source bars are checked below.
        for day in sessions:
            iso_date(day, "session")
        if tuple(sorted(set(sessions))) != sessions:
            raise ResearchInputError("sessions must be sorted and unique")
        if sessions[0] < start_date or sessions[-1] > end_date:
            raise ResearchInputError("sessions must be within requested bounds")
    try:
        evidence = EvidenceSet.from_manifest(classification_evidence_manifest)
        with SourceReader(spec) as reader:
            quote_scopes = [s for s in reader.scopes() if s.lane == "quotes"]
            for scope in reader.scopes():
                if not scope.usable:
                    raise UnsupportedDataError(
                        f"{market}/{scope.dataset} task={scope.task_id}: {scope.diagnostics}"
                    )
            selected_count = sum(reader.selected_record_count(s) for s in quote_scopes)
            if selected_count > max_rows:
                raise ResearchInputError(
                    f"selected quote rows {selected_count} exceed max_rows={max_rows}"
                )
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
            quote_flags: dict[tuple[str, str], tuple[str, ...]] = {}
            for scope in quote_scopes:
                for quote_record in reader.iter_all_records(scope):
                    if quote_record.payload is not None:
                        key = (
                            str(quote_record.payload.get("symbol", "")),
                            str(quote_record.payload.get("trade_date", "")),
                        )
                        quote_flags[key] = quote_record.quality_flags
            series = tuple(_iter_series_from_reader(reader, evidence))
            if not series:
                raise UnsupportedDataError(
                    f"no quote records for requested {market} symbols {symbols}"
                )
            bars: list[Bar] = []
            actions: list[CorporateAction] = []
            classifications: list[str] = []
            scope_by_ref = {(s.task_id, s.snapshot_id): s for s in reader.scopes()}
            link_cache = {
                (s.task_id, s.snapshot_id): reader.links(s)
                for s in reader.scopes()
                if s.lane == "events"
            }
            summary_records = {
                (s.task_id, s.snapshot_id, record.record_id): record
                for s in reader.scopes()
                if s.lane == "events"
                for record in reader.iter_all_records(s)
            }
            child_record_cache: dict[tuple[int, str, str], tuple[RecordResult, ...]] = {}
            bar_by_symbol: dict[str, Bar] = {}
            for item in series:
                item_bars = _build_bars(
                    market,
                    item.symbol,
                    item.raw_quotes,
                    {row.quote.date: row for row in item.quotes},
                    item.classifications,
                    lambda quote: quote_flags.get((quote.symbol, quote.date), ()),
                )
                bars.extend(item_bars)
                for bar in item_bars:
                    bar_by_symbol.setdefault(bar.symbol, bar)
                classifications.extend(
                    f"{item.symbol}:{quote.date}:{cls.classification}:{cls.reason}"
                    for quote, cls in zip(item.raw_quotes, item.classifications, strict=True)
                )
                actions.extend(
                    _build_actions(
                        market,
                        item.symbol,
                        item.events,
                        bar_by_symbol.get(item.symbol),
                        reader,
                        scope_by_ref,
                        link_cache,
                        summary_records,
                        child_record_cache,
                    )
                )
            if not bars:
                missing = tuple(s for s in symbols if not any(b.symbol == s for b in bars))
                raise UnsupportedDataError(f"no quote records for requested symbols: {missing}")
            missing = tuple(s for s in symbols if not any(b.symbol == s for b in bars))
            if missing:
                raise UnsupportedDataError(f"no quote records for requested symbols: {missing}")
            used_sessions = sessions if sessions is not None else observed
            if not used_sessions:
                raise UnsupportedDataError("no observed market sessions in requested range")
            for bar in bars:
                if bar.date not in used_sessions:
                    raise ResearchInputError(f"provided sessions omit source bar date {bar.date}")
            diagnostics = tuple(
                SourceDiagnostic(
                    s.source,
                    s.dataset,
                    s.task_id,
                    s.snapshot_id,
                    s.source_record_count,
                    tuple(s.diagnostics),
                )
                for s in reader.scopes()
            )
            protected = {root.resolve()}
            if evidence.protected_input_directories:
                protected.update(evidence.protected_input_directories)
            classification_diagnostics = [
                f"classification_input:{name}:{digest}"
                for name, digest in evidence.stable_input_hashes.items()
            ]
            return ResearchData(
                market,
                symbols,
                start_date,
                end_date,
                used_sessions,
                tuple(sorted(bars, key=lambda b: (b.symbol, b.date))),
                tuple(sorted(actions, key=lambda e: (e.symbol, e.effective_date, e.event_type))),
                "explicit_sessions" if sessions is not None else "observed_market_quote_dates",
                diagnostics,
                tuple(sorted((*classifications, *classification_diagnostics))),
                protected_input_dirs=frozenset(protected),
            )
    except ResearchError:
        raise
    except (DatasetError, SourceError, OSError) as exc:
        raise UnsupportedDataError(str(exc)) from exc
