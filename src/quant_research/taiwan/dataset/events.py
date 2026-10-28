"""Internal normalization of selected official event summaries and linked details."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from .financial import parse_decimal
from .models import DatasetError, UnavailableDataError
from .source import RecordResult, Scope, SourceReader


@dataclass(frozen=True, slots=True)
class EventRef:
    source: str
    dataset: str
    task_id: str
    snapshot_id: str
    record_id: int
    response_timestamp: str
    detail_dataset: str | None = None
    detail_task_id: str | None = None
    detail_snapshot_id: str | None = None
    detail_record_id: int | None = None


@dataclass(frozen=True, slots=True)
class EventValue:
    source: str
    symbol: str
    event_type: str
    effective_date: str
    dividend: Decimal
    bonus_share_ratio: Decimal | None
    cash_return_per_share: Decimal
    share_multiplier: Decimal | None
    before_price: Decimal | None
    reference_price: Decimal | None
    ref: EventRef


def _decimal(
    value: Any, field: str, scope: Scope, record: RecordResult, *, optional: bool = False
) -> Decimal:
    if value is None and optional:
        return Decimal(0)
    try:
        result = parse_decimal(value)
        if result is None:
            raise ValueError("missing decimal")
        return result
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise DatasetError(
            f"{scope.source}/{scope.dataset} "
            f"symbol={record.payload.get('symbol') if record.payload else '?'} "
            f"date={record.payload.get('event_date') if record.payload else '?'} "
            f"task={scope.task_id} record={record.record_id} field={field}: invalid value {value!r}"
        ) from exc


def _valid_record(scope: Scope, record: RecordResult) -> dict[str, Any]:
    if not scope.usable:
        raise UnavailableDataError(
            f"{scope.source}/{scope.dataset} task={scope.task_id}: {scope.diagnostics}"
        )
    if record.diagnostic or record.payload is None:
        raise DatasetError(
            f"{scope.source}/{scope.dataset} task={scope.task_id} "
            f"record={record.record_id}: {record.diagnostic or 'invalid payload'}"
        )
    return record.payload


def load_events(
    reader: SourceReader, symbol: str | None = None, *, source: str | None = None
) -> tuple[EventValue, ...]:
    """Read selected event summaries and normalize their exact linked official inputs."""
    selected: dict[
        tuple[str, str, str, str], tuple[Scope, RecordResult, dict[str, Any], tuple[str, str, str]]
    ] = {}
    scopes = [
        scope
        for scope in reader.scopes()
        if scope.lane == "events" and (source is None or scope.source == source)
    ]
    for scope in scopes:
        if not scope.usable:
            raise UnavailableDataError(
                f"{scope.source}/{scope.dataset} task={scope.task_id}: {scope.diagnostics}"
            )
        for record in reader.iter_all_records(scope, symbol=symbol):
            payload = record.payload
            if isinstance(payload, dict):
                record_day = payload.get("event_date")
                if isinstance(record_day, str) and not (
                    reader.spec.start_date <= record_day <= reader.spec.end_date
                ):
                    continue
            payload = _valid_record(scope, record)
            record_symbol, day, kind = (
                payload.get("symbol"),
                payload.get("event_date"),
                payload.get("event_type"),
            )
            if (
                not isinstance(record_symbol, str)
                or not isinstance(day, str)
                or kind not in {"dividend", "capital_reduction", "par_value"}
            ):
                raise DatasetError(
                    f"{scope.source}/{scope.dataset} record={record.record_id}: "
                    "missing event identity"
                )
            key = (scope.source, record_symbol, str(kind), day)
            stamp = (scope.fetched_at or "", scope.task_id, str(record.record_id))
            previous = selected.get(key)
            if previous is None or stamp > previous[3]:
                selected[key] = (scope, record, payload, stamp)

    candidates: dict[tuple[str, str, str, str], EventValue] = {}
    for key, (scope, record, payload, _) in sorted(selected.items()):
        record_symbol, day, kind = key[1], key[3], key[2]
        links = reader.links(scope)

        child_payload: dict[str, Any] | None = None
        child_ref: dict[str, Any] | None = None
        required = scope.source == "twse" and scope.dataset in {"TWT49U", "TWTAUU"}
        if scope.source == "twse" and scope.dataset in {"TWT49U", "TWTAUU"}:
            expected = "TWT49UDetail" if scope.dataset == "TWT49U" else "TWTAVUDetail"
            for link in links:
                ctx = link.get("context")
                if not isinstance(ctx, dict) or (
                    ctx.get("symbol"),
                    ctx.get("event_date"),
                    ctx.get("summary_dataset"),
                ) != (record_symbol, day, scope.dataset):
                    continue
                meta = reader.child_scope(link)
                if not meta or meta.get("dataset") != expected:
                    continue
                if not meta.get("available"):
                    raise DatasetError(
                        f"{scope.source}/{scope.dataset} symbol={symbol} date={day} "
                        f"task={scope.task_id} record={record.record_id}: "
                        f"required {expected} child unavailable"
                    )
                rows = list(reader.iter_child_records(link, limit=10000))
                for child in rows:
                    cp = child.payload
                    if (
                        child.diagnostic
                        or cp is None
                        or cp.get("symbol") != record_symbol
                        or cp.get("dataset") != expected
                    ):
                        raise DatasetError(
                            f"{scope.source}/{scope.dataset} symbol={symbol} date={day} "
                            f"task={scope.task_id} record={record.record_id}: "
                            f"invalid {expected} child record={child.record_id}"
                        )
                    child_payload, child_ref = cp, {**meta, "record_id": child.record_id}
                    break
                if child_payload:
                    break
            if required and child_payload is None:
                raise DatasetError(
                    f"{scope.source}/{scope.dataset} task={scope.task_id} "
                    f"record={record.record_id}: required {expected} detail link missing or invalid"
                )
        data = child_payload or payload
        used_fields = (
            ("cash_dividend_per_share", "bonus_share_ratio")
            if kind == "dividend"
            else ("cash_return_per_share", "cash_dividend_per_share", "share_multiplier")
            if kind == "capital_reduction"
            else ("share_multiplier",)
        )
        for source_payload in (payload, data):
            conflicts = source_payload.get("field_conflicts")
            if isinstance(conflicts, dict):
                for field in used_fields:
                    if field in conflicts and conflicts[field]:
                        raise DatasetError(
                            f"{scope.source}/{scope.dataset} symbol={symbol} date={day} "
                            f"task={scope.task_id} record={record.record_id} field={field}: "
                            f"conflicting source values {conflicts[field]!r}"
                        )
        div = _decimal(
            data.get("cash_dividend_per_share"),
            "cash_dividend_per_share",
            scope,
            record,
            optional=True,
        )
        ret = _decimal(
            data.get("cash_return_per_share"),
            "cash_return_per_share",
            scope,
            record,
            optional=True,
        )
        bonus = (
            None
            if data.get("bonus_share_ratio") is None
            else _decimal(data["bonus_share_ratio"], "bonus_share_ratio", scope, record)
        )
        mult = (
            None
            if data.get("share_multiplier") is None
            else _decimal(data["share_multiplier"], "share_multiplier", scope, record)
        )
        before = (
            None
            if payload.get("before_price") is None
            else _decimal(payload["before_price"], "before_price", scope, record)
        )
        reference = (
            None
            if payload.get("reference_price") is None
            else _decimal(payload["reference_price"], "reference_price", scope, record)
        )
        if kind == "capital_reduction" and (mult is None or mult <= 0):
            raise DatasetError(
                f"{scope.source}/{scope.dataset} {symbol} {day}: "
                "required positive share_multiplier missing/invalid"
            )
        if kind == "par_value" and mult is not None and mult <= 0:
            raise DatasetError(
                f"{scope.source}/{scope.dataset} {symbol} {day}: share_multiplier must be positive"
            )
        if kind == "par_value" and mult is None:
            if before is None or reference is None or before <= 0 or reference <= 0:
                raise DatasetError(
                    f"{scope.source}/{scope.dataset} {symbol} {day}: "
                    "positive before_price and reference_price required"
                )
        ref = EventRef(
            scope.source,
            scope.dataset,
            scope.task_id,
            scope.snapshot_id or "",
            record.record_id,
            scope.fetched_at or "",
            str(child_ref["dataset"]) if child_ref else None,
            str(child_ref["task_id"]) if child_ref else None,
            str(child_ref["snapshot_id"]) if child_ref else None,
            int(child_ref["record_id"]) if child_ref else None,
        )
        event = EventValue(
            scope.source,
            record_symbol,
            str(kind),
            day,
            div,
            bonus,
            ret,
            mult,
            before,
            reference,
            ref,
        )
        candidates[key] = event
    return tuple(candidates[key] for key in sorted(candidates))
