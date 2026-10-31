"""Whole saved-source inventory; read-only and development-only."""

from __future__ import annotations

import json
from collections import Counter
from typing import Any

from quant_research.taiwan.dataset.source import (
    DETAIL_DATASETS,
    EVENT_DATASETS,
    QUOTE_DATASETS,
    Scope,
    SourceReader,
)


def _params(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, str):
        return None

    def reject_constant(value: str) -> None:
        raise ValueError(f"non-finite JSON constant: {value}")

    def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, item in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON key: {key}")
            result[key] = item
        return result

    try:
        result = json.loads(value, parse_constant=reject_constant, object_pairs_hook=unique_object)
    except (TypeError, ValueError):
        return None
    return result if isinstance(result, dict) else None


def _official_identity(source: Any, dataset: Any, params_value: Any) -> tuple[str, str, str] | None:
    params = _params(params_value)
    if not params or params.get("pipeline") != "official-v1":
        return None
    if (
        source in QUOTE_DATASETS
        and dataset == QUOTE_DATASETS[source]
        and params.get("lane") == "quotes"
    ):
        return str(source), str(dataset), "quotes"
    if source in EVENT_DATASETS and dataset in EVENT_DATASETS[source]:
        if params.get("lane") == "events":
            return str(source), str(dataset), "events"
    if source == "twse" and dataset in DETAIL_DATASETS:
        if params.get("lane") == "events":
            return str(source), str(dataset), "details"
    return None


def _ref(scope: Scope, record_id: int | None = None) -> dict[str, Any]:
    result = {"task_id": scope.task_id, "edition": scope.edition, "snapshot_id": scope.snapshot_id}
    if record_id is not None:
        result["record_id"] = record_id
    return result


def inventory(reader: SourceReader) -> dict[str, Any]:
    db = reader._connection()  # reuse the reader's pinned read-only transaction
    historical: dict[str, dict[str, Any]] = {}
    snapshot_stats = {
        str(row[0]): (int(row[1]), int(row[2]), row[3], row[4])
        for row in db.execute(
            "SELECT snapshot_id,COUNT(*),COUNT(record_date),MIN(record_date),MAX(record_date) "
            "FROM records INDEXED BY records_snapshot GROUP BY snapshot_id"
        )
    }
    snap_rows = db.execute(
        "SELECT id,source,dataset,params,fetched_at,validation_state FROM snapshots ORDER BY id"
    )
    official_details: dict[str, tuple[str, str]] = {}
    official_summary_keys: set[str] = set()
    for task in db.execute(
        "SELECT logical_key,source,dataset,params,kind FROM tasks ORDER BY rowid"
    ):
        identity = _official_identity(task["source"], task["dataset"], task["params"])
        if identity is not None:
            if identity[2] == "details" and task["kind"] == "official_event_detail":
                official_details.setdefault(str(task["logical_key"]), identity[:2])
            elif identity[2] in {"quotes", "events"} and task["kind"] == (
                "official_quote" if identity[2] == "quotes" else "official_event_summary"
            ):
                official_summary_keys.add(str(task["logical_key"]))
    for row in snap_rows:
        ident = _official_identity(row["source"], row["dataset"], row["params"])
        if ident is None:
            continue
        source, dataset, lane = ident
        stat = snapshot_stats.get(str(row["id"]), (0, 0, None, None))
        group = historical.setdefault(
            f"{source}/{dataset}/{lane}",
            {
                "source": source,
                "dataset": dataset,
                "lane": lane,
                "snapshot_count": 0,
                "snapshot_row_count": 0,
                "snapshot_validation_states": {},
                "actual_stored_record_date_min": None,
                "actual_stored_record_date_max": None,
                "missing_record_date_snapshot_rows": 0,
                "fetched_at_min": None,
                "fetched_at_max": None,
            },
        )
        count, dated_count, low, high = stat
        group["snapshot_count"] += 1
        group["snapshot_row_count"] += count
        state = str(row["validation_state"])
        group["snapshot_validation_states"][state] = (
            group["snapshot_validation_states"].get(state, 0) + 1
        )
        group["missing_record_date_snapshot_rows"] += count - dated_count
        if low is not None:
            group["actual_stored_record_date_min"] = min(
                filter(None, (group["actual_stored_record_date_min"], low))
            )
            group["actual_stored_record_date_max"] = max(
                filter(None, (group["actual_stored_record_date_max"], high))
            )
        stamp = row["fetched_at"]
        if stamp is not None:
            group["fetched_at_min"] = min(filter(None, (group["fetched_at_min"], stamp)))
            group["fetched_at_max"] = max(filter(None, (group["fetched_at_max"], stamp)))

    scopes = reader.scopes()
    latest_groups: dict[str, dict[str, Any]] = {}
    for scope in scopes:
        if scope.logical_key not in official_summary_keys:
            continue
        key = f"{scope.source}/{scope.dataset}/{scope.lane}"
        group = latest_groups.setdefault(
            key,
            {
                "source": scope.source,
                "dataset": scope.dataset,
                "lane": scope.lane,
                "task_states": {},
                "snapshot_validation_states": {},
                "usable_scope_count": 0,
                "unavailable_scope_count": 0,
                "source_record_rows": 0,
                "unique_usable_snapshot_count": 0,
                "unique_usable_snapshot_row_count": 0,
                "actual_stored_record_date_min": None,
                "actual_stored_record_date_max": None,
                "requested_ranges": [],
                "fetched_at_min": None,
                "fetched_at_max": None,
                "unavailable_scopes": [],
                "malformed_event_rows": [],
                "raw_usable_summary_rows": 0,
            },
        )
        group["task_states"][scope.task_state] = group["task_states"].get(scope.task_state, 0) + 1
        validation = scope.snapshot_validation or "missing"
        group["snapshot_validation_states"][validation] = (
            group["snapshot_validation_states"].get(validation, 0) + 1
        )
        if scope.request is not None:
            group["requested_ranges"].append(scope.request)
        if scope.fetched_at:
            group["fetched_at_min"] = min(filter(None, (group["fetched_at_min"], scope.fetched_at)))
            group["fetched_at_max"] = max(filter(None, (group["fetched_at_max"], scope.fetched_at)))
        if not scope.usable or scope.snapshot_id is None:
            group["unavailable_scope_count"] += 1
            group["unavailable_scopes"].append(
                {
                    **_ref(scope),
                    "diagnostics": scope.diagnostics,
                    "task_state": scope.task_state,
                    "snapshot_validation": scope.snapshot_validation,
                }
            )
            continue
        group["usable_scope_count"] += 1
        group["source_record_rows"] += scope.source_record_count
        _, _, date_low, date_high = snapshot_stats.get(
            scope.snapshot_id, (scope.source_record_count, scope.source_record_count, None, None)
        )
        if date_low is not None:
            group["actual_stored_record_date_min"] = min(
                filter(None, (group["actual_stored_record_date_min"], date_low))
            )
            group["actual_stored_record_date_max"] = max(
                filter(None, (group["actual_stored_record_date_max"], date_high))
            )
        unique_ids = group.setdefault("_snapshot_ids", set())
        if scope.snapshot_id not in unique_ids:
            unique_ids.add(scope.snapshot_id)
            group["unique_usable_snapshot_count"] += 1
            group["unique_usable_snapshot_row_count"] += scope.source_record_count
        if scope.lane == "events":
            group["raw_usable_summary_rows"] += scope.source_record_count

    # SourceReader intentionally excludes details; preserve their latest task states separately.
    detail_tasks = db.execute(
        "SELECT t.id,t.logical_key,t.edition,t.source,t.dataset,t.params,t.kind,t.state,"
        "t.snapshot_id,s.validation_state FROM tasks t LEFT JOIN snapshots s ON s.id=t.snapshot_id "
        "ORDER BY t.logical_key,t.edition,t.rowid"
    ).fetchall()
    latest_detail: dict[str, Any] = {}
    grouped: dict[str, list[Any]] = {}
    for task in detail_tasks:
        if str(task["logical_key"]) in official_details:
            grouped.setdefault(str(task["logical_key"]), []).append(task)
    for logical_key, rows in grouped.items():
        task = max(rows, key=lambda x: (int(x["edition"]), str(x["id"])))
        expected_source, dataset = official_details[logical_key]
        actual_identity = (task["source"], task["dataset"])
        group = latest_detail.setdefault(
            f"{expected_source}/{dataset}/details",
            {
                "source": expected_source,
                "dataset": dataset,
                "lane": "details",
                "task_states": {},
                "snapshot_validation_states": {},
                "latest_scope_count": 0,
                "latest_snapshot_row_count": 0,
                "latest_snapshot_count": 0,
                "latest_snapshot_unique_row_count": 0,
                "actual_stored_record_date_min": None,
                "actual_stored_record_date_max": None,
                "missing_record_date_snapshot_rows": 0,
                "tasks": [],
            },
        )
        group["latest_scope_count"] += 1
        group["task_states"][str(task["state"])] = (
            group["task_states"].get(str(task["state"]), 0) + 1
        )
        state = str(task["validation_state"] or "missing")
        group["snapshot_validation_states"][state] = (
            group["snapshot_validation_states"].get(state, 0) + 1
        )
        snapshot_id = str(task["snapshot_id"]) if task["snapshot_id"] is not None else None
        stat = (
            snapshot_stats.get(snapshot_id, (0, 0, None, None))
            if snapshot_id
            else (0, 0, None, None)
        )
        group["latest_snapshot_row_count"] += stat[0]
        group["missing_record_date_snapshot_rows"] += stat[0] - stat[1]
        if snapshot_id is not None:
            unique = group.setdefault("_snapshot_ids", set())
            if snapshot_id not in unique:
                unique.add(snapshot_id)
                group["latest_snapshot_unique_row_count"] += stat[0]
                group["latest_snapshot_count"] += 1
        if stat[2] is not None:
            group["actual_stored_record_date_min"] = min(
                filter(None, (group["actual_stored_record_date_min"], stat[2]))
            )
            group["actual_stored_record_date_max"] = max(
                filter(None, (group["actual_stored_record_date_max"], stat[3]))
            )
        group["tasks"].append(
            {
                "task_id": task["id"],
                "edition": task["edition"],
                "state": task["state"],
                "snapshot_id": task["snapshot_id"],
                "snapshot_validation": task["validation_state"],
                "expected_source": expected_source,
                "expected_dataset": dataset,
                "actual_source": actual_identity[0],
                "actual_dataset": actual_identity[1],
                "identity_changed": actual_identity != (expected_source, dataset),
                "diagnostics": [
                    reason
                    for condition, reason in (
                        (task["source"] != expected_source, "task_source_mismatch"),
                        (task["dataset"] != dataset, "task_dataset_mismatch"),
                        (
                            _official_identity(task["source"], task["dataset"], task["params"])
                            is None,
                            "invalid_official_namespace",
                        ),
                    )
                    if condition
                ],
                "official_namespace": _official_identity(
                    task["source"], task["dataset"], task["params"]
                )
                is not None,
            }
        )

    dedup: dict[tuple[str, str, str, str], tuple[tuple[str, str, str], dict[str, Any]]] = {}
    detail_cache: dict[tuple[str, str], tuple[dict[str, Any], ...]] = {}
    link_results = Counter()
    link_reasons: list[dict[str, Any]] = []
    for scope in scopes:
        if (
            scope.logical_key not in official_summary_keys
            or scope.lane != "events"
            or not scope.usable
        ):
            continue
        key = f"{scope.source}/{scope.dataset}/{scope.lane}"
        group = latest_groups[key]
        links = reader.links(scope)
        detail_cache[(scope.task_id, scope.snapshot_id or "")] = links
        for rec in reader.iter_all_records(scope, page_size=500):
            p = rec.payload
            if (
                rec.diagnostic
                or not isinstance(p, dict)
                or not all(
                    isinstance(p.get(k), str) for k in ("symbol", "event_type", "event_date")
                )
            ):
                group["malformed_event_rows"].append(
                    {
                        **_ref(scope, rec.record_id),
                        "diagnostic": rec.diagnostic or "invalid_event_identity",
                    }
                )
                continue
            identity = (scope.source, p["symbol"], p["event_type"], p["event_date"])
            stamp = (scope.fetched_at or "", scope.task_id, str(rec.record_id))
            old = dedup.get(identity)
            if old is None or stamp > old[0]:
                dedup[identity] = (
                    stamp,
                    {
                        "identity": list(identity),
                        "scope": scope,
                        "record_id": rec.record_id,
                        "symbol": p["symbol"],
                        "event_type": p["event_type"],
                        "event_date": p["event_date"],
                    },
                )
    unique_by_group: Counter[str] = Counter()
    for _, (_, event) in dedup.items():
        scope, rec_id = event["scope"], event["record_id"]
        key = f"{scope.source}/{scope.dataset}/{scope.lane}"
        unique_by_group[key] += 1
        if scope.source != "twse" or scope.dataset not in {"TWT49U", "TWTAUU"}:
            continue
        link_results["required"] += 1
        candidate = None
        for link in detail_cache[(scope.task_id, scope.snapshot_id or "")]:
            ctx = link.get("context")
            if isinstance(ctx, dict) and (
                ctx.get("summary_dataset"),
                ctx.get("symbol"),
                ctx.get("event_date"),
                ctx.get("event_type"),
            ) == (scope.dataset, event["symbol"], event["event_date"], event["event_type"]):
                candidate = link
                break
        meta = reader.child_scope(candidate) if candidate is not None else None
        child = (
            next(iter(reader.iter_child_records(candidate, limit=1)), None)
            if candidate and meta and meta.get("available")
            else None
        )
        expected_child_dataset = {"TWT49U": "TWT49UDetail", "TWTAUU": "TWTAVUDetail"}.get(
            scope.dataset
        )
        child_payload = child.payload if child is not None else None
        if (
            meta
            and meta.get("available")
            and meta.get("dataset") == expected_child_dataset
            and child is not None
            and child.diagnostic is None
            and isinstance(child_payload, dict)
            and child_payload.get("symbol") == event["symbol"]
            and child_payload.get("dataset") == expected_child_dataset
        ):
            link_results["available"] += 1
        else:
            link_results["unavailable"] += 1
            if candidate is None:
                reason = "required_link_missing"
            elif child is None and meta and meta.get("available"):
                reason = "linked_child_missing_first_record"
            elif (
                child is not None
                and child.diagnostic is None
                and (
                    not isinstance(child_payload, dict)
                    or child_payload.get("symbol") != event["symbol"]
                    or child_payload.get("dataset") != expected_child_dataset
                )
            ):
                reason = "linked_child_identity_mismatch"
            else:
                reason = (
                    (meta.get("diagnostic") or "linked_child_unavailable")
                    if meta
                    else "linked_child_unavailable"
                )
            link_reasons.append(
                {
                    "summary_ref": _ref(scope, rec_id),
                    "reason": reason,
                    "link_id": candidate.get("link_id") if candidate else None,
                    "child_task_id": candidate.get("child_task_id") if candidate else None,
                }
            )
    for key, group in latest_groups.items():
        group["unique_latest_event_identities"] = (
            unique_by_group[key] if group["lane"] == "events" else None
        )
        group["snapshot_row_sum_is_not_unique_effective_quotes_or_events"] = True
        group.pop("_snapshot_ids", None)
    for group in latest_detail.values():
        group.pop("_snapshot_ids", None)
        group["latest_snapshot_row_count_is_not_dataset_usable_linked_detail"] = True
        group["latest_snapshot_row_sum_is_not_unique_effective_detail"] = True
    for group in historical.values():
        group["snapshot_row_sum_is_not_unique_effective_quotes_or_events"] = True
    return {
        "operation": "inventory",
        "scope": "whole saved official-v1 TWSE/TPEx quote, summary, and detail records",
        "requested_dates": None,
        "source_reader_available_count_semantics": (
            "SourceReader availability accepts valid or period_envelope_unverified snapshots "
            "and successful zero-record empty_unverified snapshots; this is not "
            "completeness certification."
        ),
        "historical_saved": sorted(
            historical.values(), key=lambda x: (x["source"], x["dataset"], x["lane"])
        ),
        "latest": sorted(
            latest_groups.values(), key=lambda x: (x["source"], x["dataset"], x["lane"])
        ),
        "latest_detail_tasks": sorted(
            latest_detail.values(), key=lambda x: (x["source"], x["dataset"])
        ),
        "required_twse_summary_detail_links": {
            "required": link_results["required"],
            "available": link_results["available"],
            "unavailable": link_results["unavailable"],
            "missing": sum(x["reason"] == "required_link_missing" for x in link_reasons),
            "reasons": link_reasons,
        },
        "event_deduplication": (
            "latest response stamp (fetched_at, task_id, str(record_id)) wins per "
            "(source,symbol,event_type,effective_date); malformed rows excluded and reported."
        ),
        "inventory_only": True,
        "no_financial_normalization_or_adjustment": True,
    }
