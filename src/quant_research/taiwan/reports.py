"""Read-only observation-universe and data-availability reports."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections.abc import Iterator
from datetime import date, timedelta
from pathlib import Path
from typing import Any, cast

from ._report_records import record_stats
from .corporate_actions import build_corporate_actions
from .storage import _corporate_actions_read

_DATE = re.compile(r"\d{4}-\d{2}-\d{2}\Z")
_SOURCES = {"official", "twse", "tpex"}
_SCHEMA = {"runs", "tasks", "snapshots", "records", "source_blocks"}
_QUOTE_DATASETS = {"twse": "MI_INDEX", "tpex": "dailyQuotes"}
_EVENT_DATASETS = {
    "twse": {"TWT49U", "TWTAUU", "TWTB8U"},
    "tpex": {"exDailyQ", "revivt", "pvChgRslt"},
}


def _date(value: str, label: str) -> date:
    if not isinstance(value, str) or not _DATE.fullmatch(value):
        raise ValueError(f"{label} must be strict ISO YYYY-MM-DD")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{label} must be strict ISO YYYY-MM-DD") from exc
    if parsed.isoformat() != value:
        raise ValueError(f"{label} must be strict ISO YYYY-MM-DD")
    return parsed


def _validate_source(source: str) -> None:
    if source not in _SOURCES:
        raise ValueError("source must be official, twse, or tpex")


def _json_object(value: object) -> dict[str, Any] | None:
    try:
        decoded: Any = json.loads(str(value))
    except (ValueError, TypeError):
        return None
    return decoded if isinstance(decoded, dict) else None


def _open(root: Path) -> sqlite3.Connection | None:
    path = root / "state.sqlite3"
    if not path.exists():
        return None
    db = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=5)
    try:
        db.row_factory = sqlite3.Row
        db.execute("BEGIN")
        tables = {
            str(row[0]) for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        if not _SCHEMA <= tables:
            raise sqlite3.DatabaseError("Taiwan data schema is incomplete")
        return db
    except BaseException:
        db.close()
        raise


def _markets(source: str) -> list[str]:
    return ["twse", "tpex"] if source == "official" else [source]


def _task_rows(db: sqlite3.Connection, kinds: tuple[str, ...], source: str) -> list[dict[str, Any]]:
    source_list = _markets(source)
    placeholders = ",".join("?" for _ in source_list)
    rows = db.execute(
        "SELECT t.rowid AS task_rowid,t.id task_id,t.logical_key,t.edition,t.source,t.dataset,"
        "t.params task_params,t.kind,t.state,t.expected_date,t.snapshot_id,s.id snapshot_id_joined,"
        "s.source snapshot_source,s.dataset snapshot_dataset,s.params snapshot_params,"
        "s.validation_state,s.sha256,s.raw_path FROM tasks t "
        "LEFT JOIN snapshots s ON s.id=t.snapshot_id "
        f"WHERE t.source IN ({placeholders}) "
        "ORDER BY t.source,t.logical_key,t.edition,t.rowid",
        tuple(source_list),
    ).fetchall()
    latest: dict[tuple[str, str], dict[str, Any]] = {}
    histories: dict[tuple[str, str], set[str]] = {}
    for raw in rows:
        row = dict(raw)
        key = (str(row["source"]), str(row["logical_key"]))
        if row.get("kind") in kinds:
            histories.setdefault(key, set()).add(str(row.get("dataset")))
        previous = latest.get(key)
        edition = int(row["edition"])
        if previous is None or (edition, int(row["task_rowid"])) > (
            int(previous["edition"]),
            int(previous["task_rowid"]),
        ):
            latest[key] = row
    return [
        {**row, "_historical_datasets": sorted(histories[key])}
        for key, row in latest.items()
        if key in histories
    ]


def _namespace(params: dict[str, Any] | None, lane: str) -> bool:
    return bool(params and params.get("pipeline") == "official-v1" and params.get("lane") == lane)


def _lineage_ok(task: dict[str, Any], params: dict[str, Any] | None, lane: str) -> bool:
    snapshot = _json_object(task.get("snapshot_params"))
    if task.get("snapshot_id") is None:
        return False
    return bool(
        task.get("snapshot_id") == task.get("snapshot_id_joined")
        and task.get("snapshot_source") == task.get("source")
        and task.get("snapshot_dataset") == task.get("dataset")
        and _namespace(snapshot, lane)
        and params is not None
        and snapshot is not None
        and snapshot.get("request") == params.get("request")
    )


def _count_records(db: sqlite3.Connection, snapshot_ids: list[str]) -> dict[str, int]:
    if not snapshot_ids:
        return {}
    rows = db.execute(
        "SELECT snapshot_id,COUNT(*) AS record_count FROM records "
        "WHERE snapshot_id IN (SELECT value FROM json_each(?)) GROUP BY snapshot_id",
        (json.dumps(snapshot_ids),),
    )
    return {str(row["snapshot_id"]): int(row["record_count"]) for row in rows}


def _refs(task: dict[str, Any], record_id: object = None) -> dict[str, object]:
    result: dict[str, object] = {
        "task_id": task.get("task_id"),
        "source": task.get("source"),
        "dataset": task.get("dataset"),
        "snapshot_id": task.get("snapshot_id"),
        "sha256": task.get("sha256"),
        "raw_path": task.get("raw_path"),
    }
    if record_id is not None:
        result["record_id"] = record_id
    return result


def _quote_scope(
    task: dict[str, Any], day: str, total: int, matching: int, *, route_valid: bool = True
) -> dict[str, Any]:
    params = _json_object(task.get("task_params"))
    request = params.get("request") if params else None
    req_date = request.get("date") if isinstance(request, dict) else None
    correct_namespace = _namespace(params, "quotes")
    correct_fixed = task.get("dataset") == _QUOTE_DATASETS.get(str(task.get("source")))
    request_ok = isinstance(request, dict) and set(request) == {"date"} and req_date == day
    date_ok = request_ok and task.get("expected_date") in (None, day) and route_valid
    lineage_ok = _lineage_ok(task, params, "quotes")
    valid = bool(
        correct_namespace
        and correct_fixed
        and date_ok
        and lineage_ok
        and task.get("state") == "completed"
        and task.get("validation_state") == "valid"
        and total > 0
        and matching == total
    )
    task["namespace_valid"] = correct_namespace
    task["metadata_valid"] = bool(correct_fixed and date_ok)
    task["lineage_valid"] = lineage_ok
    task["matching_count"] = matching
    acquisition = _acquisition_state(task, total, valid)
    return {
        **_refs(task),
        "logical_key": task.get("logical_key"),
        "edition": task.get("edition"),
        "request_date": req_date,
        "planned_date": day,
        "task_state": task.get("state"),
        "validation_state": task.get("validation_state"),
        "record_count": total,
        "matching_record_count": matching,
        "acquisition_state": acquisition,
        "namespace_valid": correct_namespace,
        "lineage_valid": lineage_ok,
        "metadata_valid": bool(correct_fixed and date_ok),
        "eligible": valid,
    }


def _quote_view(db: sqlite3.Connection, start: str, end: str, source: str) -> dict[str, Any]:
    """Select current quote-history tasks and validate all snapshot rows in one batch."""
    markets = _markets(source)
    placeholders = ",".join("?" for _ in markets)
    rows = db.execute(
        "SELECT t.rowid AS task_rowid,t.id task_id,t.logical_key,t.edition,t.source,t.dataset,"
        "t.params task_params,t.kind,t.state,t.expected_date,t.snapshot_id,s.id snapshot_id_joined,"
        "s.source snapshot_source,s.dataset snapshot_dataset,s.params snapshot_params,"
        "s.validation_state,s.sha256,s.raw_path FROM tasks t "
        "LEFT JOIN snapshots s ON s.id=t.snapshot_id "
        f"WHERE t.source IN ({placeholders}) "
        "ORDER BY t.source,t.logical_key,t.edition,t.rowid",
        tuple(markets),
    ).fetchall()
    histories: set[tuple[str, str]] = set()
    latest: dict[tuple[str, str], dict[str, Any]] = {}
    for raw in rows:
        task = dict(raw)
        key = (str(task["source"]), str(task["logical_key"]))
        if task.get("kind") == "official_quote":
            histories.add(key)
        prev = latest.get(key)
        if prev is None or (int(task["edition"]), int(task["task_rowid"])) > (
            int(prev["edition"]),
            int(prev["task_rowid"]),
        ):
            latest[key] = task
    tasks = [t for key, t in latest.items() if key in histories]
    scopes: list[dict[str, Any]] = []
    diagnostics: list[dict[str, Any]] = []
    wanted: list[dict[str, str]] = []
    route_info: dict[str, tuple[str, bool]] = {}
    for task in tasks:
        params = _json_object(task.get("task_params"))
        request = params.get("request") if params else None
        req_date = request.get("date") if isinstance(request, dict) else None
        expected = task.get("expected_date")
        expected_valid = False
        if isinstance(expected, str):
            try:
                _date(expected, "expected_date")
                expected_valid = True
            except ValueError:
                pass
        request_valid = False
        if isinstance(req_date, str):
            try:
                _date(req_date, "request.date")
                request_valid = True
            except ValueError:
                pass
        routed = expected if expected_valid else (req_date if request_valid else None)
        conflict = bool(expected_valid and request_valid and expected != req_date)
        if routed is None:
            diagnostics.append(
                {
                    **_refs(task),
                    "date_filter_applied": False,
                    "reason": "invalid_or_unmapped_request",
                }
            )
            continue
        if not start <= routed <= end:
            continue
        route_info[str(task.get("task_id"))] = (routed, not conflict)
        task["params_obj"] = params
        if task.get("snapshot_id") is not None:
            sid = str(task["snapshot_id"])
            if (
                task.get("kind") == "official_quote"
                and _namespace(params, "quotes")
                and task.get("dataset") == _QUOTE_DATASETS.get(str(task.get("source")))
                and isinstance(request, dict)
                and set(request) == {"date"}
                and req_date == routed
                and not conflict
                and _lineage_ok(task, params, "quotes")
            ):
                wanted.append(
                    {
                        "snapshot_id": sid,
                        "source": str(task["source"]),
                        "dataset": str(task["dataset"]),
                        "kind": "quote",
                        "start": routed,
                        "end": routed,
                    }
                )
    statistics = record_stats(db, wanted)
    routed_sids = [
        str(t["snapshot_id"])
        for t in tasks
        if t.get("snapshot_id") is not None and str(t.get("task_id")) in route_info
    ]
    raw_counts = _count_records(db, routed_sids)
    eligible_by_sid: dict[str, list[dict[str, Any]]] = {}
    for task in tasks:
        info = route_info.get(str(task.get("task_id")))
        if info is None:
            continue
        day, route_valid = info
        snapshot_id_value = task.get("snapshot_id")
        total, matching = (
            statistics.get(str(snapshot_id_value), (raw_counts.get(str(snapshot_id_value), 0), 0))
            if snapshot_id_value is not None
            else (0, 0)
        )
        scope = _quote_scope(task, day, total, matching, route_valid=route_valid)
        if task.get("kind") != "official_quote":
            scope.update(eligible=False, acquisition_state="invalid", metadata_valid=False)
        scopes.append(scope)
        if scope["eligible"] and snapshot_id_value is not None:
            eligible_by_sid.setdefault(str(snapshot_id_value), []).append(task)
        if not scope["eligible"]:
            diagnostics.append(
                {
                    **_refs(task),
                    "date_filter_applied": True,
                    "reason": "latest_scope_not_eligible",
                    "acquisition_state": scope["acquisition_state"],
                }
            )
    return {
        "tasks": tasks,
        "scopes": scopes,
        "diagnostics": diagnostics,
        "tasks_by_snapshot": eligible_by_sid,
    }


def _acquisition_state(task: dict[str, Any], count: int, valid: bool) -> str:
    if not task.get("namespace_valid", True) or not task.get("metadata_valid", True):
        return "invalid"
    if task.get("state") == "pending":
        return "pending"
    if task.get("state") == "running":
        return "running"
    if task.get("state") == "failed":
        return "failed"
    if task.get("state") != "completed":
        return "unverified"
    if not task.get("lineage_valid", True):
        return "invalid"
    if task.get("validation_state") == "empty_unverified" and count == 0:
        return "empty_unverified"
    if task.get("validation_state") == "period_envelope_unverified":
        if count > 0 and int(task.get("matching_count", count)) == count:
            return "unverified"
        return "invalid"
    if task.get("validation_state") != "valid":
        return "invalid" if task.get("validation_state") else "unverified"
    if count == 0 or int(task.get("matching_count", count)) != count:
        return "invalid"
    return "available" if valid else "invalid"


def observation_universe(
    root: Path, day: str, *, source: str = "official", limit: int = 20000, offset: int = 0
) -> dict[str, object]:
    _date(day, "date")
    _validate_source(source)
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100000:
        raise ValueError("limit must be an integer from 1 through 100000")
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
        raise ValueError("offset must be a nonnegative integer")
    base: dict[str, object] = {
        "schema_version": "observation-universe-v1",
        "date": day,
        "source": source,
        "scope": "observed_daily_quotes",
        "time_semantics": "latest_stored_not_point_in_time",
        "coverage_complete": False,
        "research_ready": False,
        "classification": "unknown",
        "classification_basis": "no_dated_official_security_type_evidence",
    }
    db = _open(root)
    if db is None:
        return {
            **base,
            "total_count": 0,
            "members": [],
            "limit": limit,
            "offset": offset,
            "truncated": False,
            "snapshot_fingerprint": hashlib.sha256(b"[]").hexdigest(),
            "diagnostics": [],
            "warnings": [
                "database_missing",
                "unknown_classification",
                "quote_presence_not_executability",
            ],
        }
    try:
        view = _quote_view(db, day, day, source)
        scopes = view["scopes"]
        task_by_snapshot = view["tasks_by_snapshot"]
        diagnostics: list[dict[str, object]] = view["diagnostics"]
        eligible_ids = list(task_by_snapshot)
        records: list[sqlite3.Row] = []
        if eligible_ids:
            records = db.execute(
                "SELECT r.id,r.snapshot_id,r.symbol,r.record_date,r.kind,r.payload,r.quality_flags,"
                "s.source,s.dataset,s.params,s.sha256,s.raw_path FROM records r "
                "JOIN snapshots s ON s.id=r.snapshot_id "
                "WHERE r.snapshot_id IN (SELECT value FROM json_each(?)) ORDER BY r.id",
                (json.dumps(sorted(set(eligible_ids))),),
            ).fetchall()
        members: dict[tuple[str, str, str], dict[str, Any]] = {}
        invalid_records: set[str] = set()
        for row in records:
            payload = _json_object(row["payload"])
            try:
                flags: Any = json.loads(str(row["quality_flags"]))
            except (ValueError, TypeError):
                flags = []
            params = _json_object(row["params"])
            request = params.get("request") if params else None
            symbol = payload.get("symbol") if payload else None
            record_day = payload.get("trade_date") if payload else None
            stored_venue_id = payload.get("venue_id") if payload else None
            venue = stored_venue_id
            management_title = bool(
                payload is not None and "管理股票" in str(payload.get("venue", ""))
            )
            if management_title:
                venue = "management"
            matches = bool(
                payload is not None
                and row["kind"] == "quote"
                and row["record_date"] == day
                and payload.get("kind", "quote") == "quote"
                and symbol == row["symbol"]
                and isinstance(symbol, str)
                and bool(symbol)
                and record_day == day
                and payload.get("source") == row["source"]
                and payload.get("dataset") == row["dataset"]
                and row["source"] in _markets(source)
                and row["dataset"] == _QUOTE_DATASETS.get(str(row["source"]))
                and venue is not None
                and str(venue).strip() != ""
                and isinstance(request, dict)
                and set(request) == {"date"}
                and request.get("date") == day
            )
            if not matches:
                invalid_records.add(str(row["snapshot_id"]))
                continue
            assert payload is not None
            key = (str(row["source"]), str(venue), str(symbol))
            refs = [
                {
                    "task_id": task.get("task_id"),
                    "record_id": row["id"],
                    "source": row["source"],
                    "dataset": row["dataset"],
                    "snapshot_id": row["snapshot_id"],
                    "sha256": row["sha256"],
                    "raw_path": row["raw_path"],
                    "quality_flags": flags,
                }
                for task in task_by_snapshot[str(row["snapshot_id"])]
            ]
            item = members.setdefault(
                key,
                {
                    "source": key[0],
                    "date": day,
                    "symbol": key[2],
                    "name": payload.get("name"),
                    "venue_id": key[1],
                    "stored_venue_id": stored_venue_id,
                    "venue": payload.get("venue"),
                    "venue_basis": (
                        "stored_official_quote_table_title"
                        if management_title
                        else "venue_id_from_stored_quote"
                    ),
                    "classification": "unknown",
                    "classification_basis": "no_dated_official_security_type_evidence",
                    "classification_evidence": [],
                    "provenance": [],
                },
            )
            item["provenance"].extend(refs)
        if invalid_records:
            for scope in scopes:
                if scope.get("snapshot_id") in invalid_records:
                    scope["eligible"] = False
                    scope["acquisition_state"] = "invalid"
                    diagnostics.append(
                        {
                            "snapshot_id": scope["snapshot_id"],
                            "reason": "record_metadata_mismatch",
                            "date_filter_applied": True,
                        }
                    )
            # A snapshot with one malformed row is excluded in full.
            for key in list(members):
                member = members[key]
                member["provenance"] = [
                    ref
                    for ref in member["provenance"]
                    if str(ref["snapshot_id"]) not in invalid_records
                ]
                if not member["provenance"]:
                    del members[key]
        allmembers = [members[k] for k in sorted(members)]
        fingerprint = hashlib.sha256(
            json.dumps(
                [(m["source"], m["venue_id"], m["symbol"], m["provenance"]) for m in allmembers],
                sort_keys=True,
                ensure_ascii=False,
            ).encode()
        ).hexdigest()
        return {
            **base,
            "total_count": len(allmembers),
            "members": allmembers[offset : offset + limit],
            "limit": limit,
            "offset": offset,
            "truncated": offset + limit < len(allmembers),
            "snapshot_fingerprint": fingerprint,
            "diagnostics": diagnostics,
            "warnings": [
                "not_complete",
                "not_point_in_time",
                "unknown_classification",
                "quote_presence_not_executability",
            ],
        }
    finally:
        db.close()


def _event_request(task: dict[str, Any]) -> tuple[str | None, str | None, bool, bool]:
    params = _json_object(task.get("task_params"))
    request = params.get("request") if params else None
    if not isinstance(request, dict):
        return None, None, False, False
    lo, hi = request.get("start_date"), request.get("end_date")
    if not isinstance(lo, str) or not isinstance(hi, str):
        return None, None, _namespace(params, "events"), False
    try:
        _date(lo, "start_date")
        _date(hi, "end_date")
    except ValueError:
        return None, None, _namespace(params, "events"), False
    return str(lo), str(hi), _namespace(params, "events"), str(lo) <= str(hi)


def _event_state(task: dict[str, Any], count: int, valid: bool) -> str:
    return _acquisition_state(task, count, valid)


def _gaps(
    source: str, dataset: str, start: date, end: date, intervals: list[tuple[date, date]]
) -> list[dict[str, str]]:
    cursor = start
    result: list[dict[str, str]] = []
    for lo, hi in sorted(intervals):
        if hi < cursor:
            continue
        if lo > cursor:
            result.append(
                {
                    "source": source,
                    "dataset": dataset,
                    "start": cursor.isoformat(),
                    "end": (lo - timedelta(days=1)).isoformat(),
                }
            )
        if hi >= end:
            return result
        cursor = max(cursor, hi + timedelta(days=1))
    if cursor <= end:
        result.append(
            {
                "source": source,
                "dataset": dataset,
                "start": cursor.isoformat(),
                "end": end.isoformat(),
            }
        )
    return result


def availability(
    root: Path, start: str, end: str, *, source: str = "official"
) -> dict[str, object]:
    a, b = _date(start, "start"), _date(end, "end")
    _validate_source(source)
    if b < a or (b - a).days + 1 > 366:
        raise ValueError("availability range must be a closed interval of at most 366 days")
    base: dict[str, object] = {
        "schema_version": "data-availability-v1",
        "scope": "selected_source_intervals",
        "source": source,
        "start": start,
        "end": end,
        "time_semantics": "latest_stored_not_point_in_time",
        "coverage_complete": False,
        "research_ready": False,
        "warnings": [
            "not_complete",
            "not_point_in_time",
            "unknown_classification",
            "quote_presence_not_executability",
        ],
    }
    db = _open(root)
    if db is None:
        return {
            **base,
            "source_blocks": [],
            "quote_days": [
                {"date": d.isoformat(), "source": market, "state": "unplanned", "scopes": []}
                for d in _dates(a, b)
                for market in _markets(source)
            ],
            "quote_diagnostics": [],
            "event_scopes": [],
            "unplanned_event_gaps": [
                {"source": market, "dataset": dataset, "start": start, "end": end}
                for market in _markets(source)
                for dataset in sorted(_EVENT_DATASETS[market])
            ],
            "financial_conditions": build_corporate_actions(
                [], [], [], source=source, start=start, end=end
            ),
            "financial_conditions_error": None,
        }
    try:
        markets = _markets(source)
        blocks = [
            dict(r)
            for r in db.execute(
                "SELECT source,reason,state,blocked_at,retry_at FROM source_blocks "
                "WHERE source IN (SELECT value FROM json_each(?)) ORDER BY source",
                (json.dumps(markets),),
            )
        ]
        quote_view = _quote_view(db, start, end, source)
        quote_by_day: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for scope in quote_view["scopes"]:
            scoped_day = scope.get("planned_date")
            if isinstance(scoped_day, str):
                quote_by_day.setdefault((str(scope.get("source")), scoped_day), []).append(scope)
        quote_days = []
        for d in _dates(a, b):
            for market in markets:
                scopes = quote_by_day.get((market, d.isoformat()), [])
                states = {str(scope["acquisition_state"]) for scope in scopes}
                day_state = (
                    "unplanned"
                    if not scopes
                    else next(iter(states))
                    if len(states) == 1
                    else "mixed"
                )
                quote_days.append(
                    {"date": d.isoformat(), "source": market, "state": day_state, "scopes": scopes}
                )
        event_tasks = _task_rows(db, ("official_event_summary",), source)
        event_scopes: list[dict[str, Any]] = []
        gaps: list[dict[str, str]] = []
        event_candidates: list[tuple[dict[str, Any], str, str, str, str]] = []
        planned: dict[tuple[str, str], list[tuple[date, date]]] = {}
        event_type = {
            "TWT49U": "dividend",
            "TWTAUU": "capital_reduction",
            "TWTB8U": "par_value",
            "exDailyQ": "dividend",
            "revivt": "capital_reduction",
            "pvChgRslt": "par_value",
        }
        for task in event_tasks:
            market = str(task.get("source"))
            historical = task.get("_historical_datasets", [])
            datasets = (
                historical if isinstance(historical, list) and historical else [task.get("dataset")]
            )
            for dataset_value in datasets:
                dataset = str(dataset_value)
                if market not in markets or dataset not in _EVENT_DATASETS.get(market, set()):
                    continue
                # Only the current task can establish a valid planned interval.
                lo_text, hi_text, namespace_ok, interval_ok = _event_request(task)
                params = _json_object(task.get("task_params"))
                fixed_ok = (
                    task.get("kind") == "official_event_summary"
                    and task.get("dataset") == dataset
                    and task.get("source") in _EVENT_DATASETS
                )
                request = params.get("request") if params else None
                fixed_ok = bool(
                    fixed_ok
                    and isinstance(request, dict)
                    and set(request) == {"start_date", "end_date"}
                    and str(task.get("dataset")) in _EVENT_DATASETS.get(market, set())
                )
                if (
                    not namespace_ok
                    or not interval_ok
                    or not fixed_ok
                    or lo_text is None
                    or hi_text is None
                ):
                    event_scopes.append(
                        {
                            **_refs(task),
                            "logical_key": task.get("logical_key"),
                            "edition": task.get("edition"),
                            "date_filter_applied": False,
                            "reason": "invalid_or_unmapped_request",
                        }
                    )
                    continue
                lo, hi = date.fromisoformat(lo_text), date.fromisoformat(hi_text)
                if hi < a or lo > b:
                    continue
                planned.setdefault((market, dataset), []).append((max(a, lo), min(b, hi)))
                task_params = json.dumps(params)
                task["task_params"] = task_params
                task["namespace_valid"] = True
                task["metadata_valid"] = True
                task["lineage_valid"] = _lineage_ok(task, params, "events")
                event_candidates.append((task, market, dataset, lo_text, hi_text))
        wanted = [
            {
                "snapshot_id": str(task["snapshot_id"]),
                "source": market,
                "dataset": dataset,
                "kind": "event",
                "event_type": event_type[dataset],
                "start": lo_text,
                "end": hi_text,
            }
            for task, market, dataset, lo_text, hi_text in event_candidates
            if task.get("snapshot_id") is not None
        ]
        event_stats = record_stats(db, wanted)
        for task, _market, _dataset, lo_text, hi_text in event_candidates:
            count, matching = event_stats.get(str(task.get("snapshot_id")), (0, 0))
            lineage_ok = bool(task["lineage_valid"])
            valid = bool(lineage_ok and matching == count and count > 0)
            if task.get("validation_state") == "empty_unverified" and count == 0:
                valid = lineage_ok
            task["matching_count"] = matching
            acquisition = _event_state(task, count, valid)
            if count > 0 and matching != count:
                acquisition = "invalid"
            event_scopes.append(
                {
                    **_refs(task),
                    "logical_key": task.get("logical_key"),
                    "edition": task.get("edition"),
                    "request_start": lo_text,
                    "request_end": hi_text,
                    "task_state": task.get("state"),
                    "validation_state": task.get("validation_state"),
                    "record_count": count,
                    "matching_record_count": matching,
                    "acquisition_state": acquisition,
                    "date_filter_applied": True,
                }
            )
        for market in markets:
            for dataset in sorted(_EVENT_DATASETS[market]):
                gaps.extend(_gaps(market, dataset, a, b, planned.get((market, dataset), [])))
        financial_error = None
        try:
            financial = _corporate_actions_read(db, source=source, start=start, end=end)
        except json.JSONDecodeError:
            # Preserve report availability without presenting corrupt evidence as no actions.
            financial = None
            financial_error = {
                "code": "invalid_stored_event_json",
                "message": "stored event JSON is invalid",
            }
        warnings = list(cast(list[str], base["warnings"]))
        if financial_error is not None:
            warnings.append("financial_conditions_unavailable")
        return {
            **base,
            "source_blocks": blocks,
            "quote_days": quote_days,
            "quote_diagnostics": quote_view["diagnostics"],
            "event_scopes": event_scopes,
            "unplanned_event_gaps": gaps,
            "financial_conditions": financial,
            "financial_conditions_error": financial_error,
            "warnings": warnings,
        }
    finally:
        db.close()


def _dates(a: date, b: date) -> Iterator[date]:
    return (a + timedelta(days=i) for i in range((b - a).days + 1))
