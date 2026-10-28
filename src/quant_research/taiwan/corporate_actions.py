"""Pure aggregation of stored official Taiwan corporate-action summaries."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import date
from decimal import Context, Decimal, InvalidOperation, localcontext
from typing import Any

SUMMARY = {
    ("twse", "TWT49U"): "dividend",
    ("twse", "TWTAUU"): "capital_reduction",
    ("twse", "TWTB8U"): "par_value",
    ("tpex", "exDailyQ"): "dividend",
    ("tpex", "revivt"): "capital_reduction",
    ("tpex", "pvChgRslt"): "par_value",
}
DETAIL = {"TWT49UDetail", "TWTAVUDetail", "TWTB8UDetail", "TWTB7UDetail"}
FIELDS = (
    "cash_dividend_per_share",
    "bonus_share_ratio",
    "share_multiplier",
    "cash_return_per_share",
    "par_before",
    "par_after",
    "before_price",
    "reference_price",
    "reference_price_kind",
    "stop_date",
    "resume_date",
)
REQUIRED = {
    "dividend": (
        "cash_dividend_per_share",
        "bonus_share_ratio",
        "before_price",
        "reference_price",
        "reference_price_kind",
    ),
    "capital_reduction": (
        "share_multiplier",
        "cash_return_per_share",
        "cash_dividend_per_share",
        "before_price",
        "reference_price",
        "reference_price_kind",
    ),
    "par_value": (
        "share_multiplier",
        "par_before",
        "par_after",
        "before_price",
        "reference_price",
        "reference_price_kind",
    ),
}
NA = {"", "NA", "N/A", "－", "X"}


def _date(v: object) -> bool:
    if not isinstance(v, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", v):
        return False
    try:
        return date.fromisoformat(v).isoformat() == v
    except ValueError:
        return False


def _getdate(v: object, label: str) -> str | None:
    if v is None:
        return None
    if not _date(v):
        raise ValueError(f"{label} must be valid YYYY-MM-DD")
    return str(v)


def _params(t: dict[str, Any]) -> dict[str, Any] | None:
    p = t.get("params")
    if isinstance(p, str):
        try:
            p = json.loads(p)
        except (ValueError, TypeError):
            return None
    return (
        p
        if isinstance(p, dict)
        and p.get("pipeline") == "official-v1"
        and p.get("lane") == "events"
        and isinstance(p.get("request"), dict)
        else None
    )


def _number(v: object) -> Decimal | None:
    if (
        v is None
        or isinstance(v, (bool, float))
        or (isinstance(v, str) and v.strip().upper() in NA)
        or not isinstance(v, (str, int, Decimal))
    ):
        return None
    try:
        n = Decimal(str(v))
    except (InvalidOperation, ValueError):
        return None
    exponent = n.as_tuple().exponent
    if (
        not n.is_finite()
        or len(n.as_tuple().digits) > 64
        or not isinstance(exponent, int)
        or abs(exponent) > 64
    ):
        return None
    return n


def _has_quality_flag(payload: dict[str, Any], flag: str) -> bool:
    flags = payload.get("quality_flags")
    return isinstance(flags, list) and flag in flags


def _canon(n: Decimal) -> str:
    if n == 0:
        return "0"
    s = format(n, "f")
    return s.rstrip("0").rstrip(".") if "." in s else s


def _valid_task(t: dict[str, Any], *, detail: bool = False) -> bool:
    expected_kind = "official_event_detail" if detail else "official_event_summary"
    known = t.get("dataset") in DETAIL if detail else (t.get("source"), t.get("dataset")) in SUMMARY
    return (
        (t.get("source") == "twse" if detail else t.get("source") in {"twse", "tpex"})
        and known
        and t.get("kind") == expected_kind
        and _params(t) is not None
    )


def build_corporate_actions(
    observations: list[dict[str, Any]],
    links: list[dict[str, Any]],
    tasks: list[dict[str, Any]],
    *,
    symbol: str | None = None,
    source: str = "official",
    start: str | None = None,
    end: str | None = None,
    context: Context | None = None,
) -> dict[str, object]:
    """Aggregate approved event summaries in an isolated Decimal context."""
    with localcontext(context if context is not None else Context(prec=512)):
        return _build_corporate_actions(
            observations, links, tasks, symbol=symbol, source=source, start=start, end=end
        )


def _build_corporate_actions(
    observations: list[dict[str, Any]],
    links: list[dict[str, Any]],
    tasks: list[dict[str, Any]],
    *,
    symbol: str | None = None,
    source: str = "official",
    start: str | None = None,
    end: str | None = None,
) -> dict[str, object]:
    """Aggregate approved event summaries without I/O or mutation."""
    if source not in {"official", "twse", "tpex"}:
        raise ValueError("source must be official, twse, or tpex")
    start, end = _getdate(start, "start"), _getdate(end, "end")
    if start and end and start > end:
        raise ValueError("start must not exceed end")
    allowed = [
        t for t in tasks if _valid_task(t) and (source == "official" or t["source"] == source)
    ]
    detail_tasks = [
        t
        for t in tasks
        if _valid_task(t, detail=True) and (source == "official" or t["source"] == source)
    ]
    all_task_rows = allowed + detail_tasks
    snaps = {t.get("snapshot_id"): t for t in all_task_rows if t.get("snapshot_id") is not None}
    attempts: dict[str, list[dict[str, Any]]] = {}
    for t in allowed:
        attempts.setdefault(str(t.get("logical_key")), []).append(t)

    def order(t: dict[str, Any]) -> tuple[int, str, str]:
        return (
            int(t.get("edition") or 0),
            str(t.get("fetched_at") or ""),
            str(t.get("task_id") or ""),
        )

    latest = {k: max(v, key=order) for k, v in attempts.items()}
    available = {
        k: max(
            [
                t
                for t in v
                if t.get("state") == "completed"
                and t.get("validation_state") in {"valid", "period_envelope_unverified"}
                and isinstance(t.get("record_count"), int)
                and not isinstance(t.get("record_count"), bool)
                and t["record_count"] > 0
            ],
            key=order,
        )
        for k, v in attempts.items()
        if any(
            t.get("state") == "completed"
            and t.get("validation_state") in {"valid", "period_envelope_unverified"}
            and isinstance(t.get("record_count"), int)
            and not isinstance(t.get("record_count"), bool)
            and t["record_count"] > 0
            for t in v
        )
    }
    diag: dict[str, list[object]] = {
        k: []
        for k in (
            "stale_scopes",
            "invalid_observations",
            "superseded_observations",
            "revision_candidates",
            "invalid_links",
            "orphan_details",
        )
    }
    scoped: dict[str, list[dict[str, Any]]] = {}
    detail_observations: dict[object, list[dict[str, Any]]] = {}
    for o in observations:
        task_row = snaps.get(o.get("snapshot_id"))
        if task_row is None:
            continue
        if task_row.get("dataset") in DETAIL:
            detail_observations.setdefault(o.get("snapshot_id"), []).append(o)
            continue
        p = o.get("payload")
        if not isinstance(p, dict):
            diag["invalid_observations"].append(
                {"record_id": o.get("record_id"), "reason": "payload_not_mapping"}
            )
            continue
        params = _params(task_row)
        if params is None:
            continue
        expected_type = SUMMARY.get((str(task_row.get("source")), str(task_row.get("dataset"))))
        if (
            p.get("kind") != "event"
            or p.get("observation_type") != "summary"
            or p.get("source") != task_row.get("source")
            or p.get("dataset") != task_row.get("dataset")
            or p.get("event_type") != expected_type
        ):
            diag["invalid_observations"].append(
                {"record_id": o.get("record_id"), "reason": "payload_task_metadata_mismatch"}
            )
            continue
        req = params["request"]
        d = p.get("event_date") or p.get("date")
        sym = p.get("symbol")
        period_ok = (
            _date(req.get("start_date"))
            and _date(req.get("end_date"))
            and req["start_date"] <= req["end_date"]
        )
        if not _date(d) or not isinstance(sym, str) or not sym.strip():
            diag["invalid_observations"].append(
                {"record_id": o.get("record_id"), "reason": "invalid_symbol_or_event_date"}
            )
            continue
        if not period_ok or not req["start_date"] <= d <= req["end_date"]:
            diag["invalid_observations"].append(
                {"record_id": o.get("record_id"), "reason": "event_date_outside_request_period"}
            )
            continue
        scoped.setdefault(str(task_row.get("logical_key")), []).append(
            {
                "obs": o,
                "task": task_row,
                "payload": p,
                "symbol": sym,
                "date": d,
                "type": SUMMARY[(task_row["source"], task_row["dataset"])],
            }
        )
    linked_record_ids: set[object] = set()
    links_by_parent: dict[tuple[object, object], list[dict[str, Any]]] = {}
    for link in links:
        links_by_parent.setdefault(
            (link.get("parent_task_id"), link.get("parent_snapshot_id")), []
        ).append(link)
    detail_tasks_by_id = {t.get("task_id"): t for t in detail_tasks}
    candidates: dict[tuple[str, str, str, str], list[dict[str, Any]]] = {}
    same_day_type_index: dict[tuple[str, str, str], set[str]] = {}

    def linked_details(r: dict[str, Any]) -> tuple[list[dict[str, Any]], bool]:
        expected_detail = {
            "TWT49U": "TWT49UDetail",
            "TWTAUU": "TWTAVUDetail",
            "TWTB8U": "TWTB8UDetail",
        }.get(r["task"].get("dataset"))
        detail_params = r["payload"].get("detail_params")
        is_required = expected_detail in {"TWT49UDetail", "TWTAVUDetail"} and isinstance(
            detail_params, dict
        )
        matching = links_by_parent.get((r["task"].get("task_id"), r["task"].get("snapshot_id")), [])
        token = r["payload"].get("original", {}).get("詳細資料")
        token = token.strip() if isinstance(token, str) else None
        expected_context = {
            "symbol": r["symbol"],
            "event_date": r["date"],
            "event_type": r["type"],
            "summary_dataset": r["task"].get("dataset"),
            "summary_token": token,
        }
        linked: list[dict[str, Any]] = []
        valid_link_found = False
        for link in matching:
            child_req: Any = None
            child = detail_tasks_by_id.get(link.get("child_task_id"))
            ctx = link.get("context")
            reason = None
            if not isinstance(ctx, dict) or any(
                ctx.get(key) != value for key, value in expected_context.items()
            ):
                reason = "context_mismatch"
            elif child is None:
                reason = "invalid_child_task"
            else:
                cp = _params(child)
                child_req = cp.get("request") if cp else None
                if (
                    child.get("source") != "twse"
                    or child.get("state") != "completed"
                    or child.get("validation_state") != "valid"
                    or child.get("snapshot_id") is None
                ):
                    reason = "child_not_valid_completed_snapshot"
                elif (
                    child.get("dataset") == "TWTB7UDetail" and r["task"].get("dataset") == "TWTB8U"
                ):
                    if (
                        not isinstance(detail_params, dict)
                        or not isinstance(child_req, dict)
                        or set(child_req) != {"symbol", "file_date"}
                        or child_req.get("symbol") != r["symbol"]
                        or not _date(child_req.get("file_date"))
                    ):
                        reason = "child_request_or_dataset_mismatch"
                elif child.get("dataset") != expected_detail or child_req != detail_params:
                    reason = "child_request_or_dataset_mismatch"
            child = child or {}
            child_req = child_req if isinstance(child_req, dict) else {}
            child_rows = (
                detail_observations.get(child.get("snapshot_id"), [])
                if child and reason is None
                else []
            )
            accepted = []
            if reason is None and not child_rows:
                reason = "child_observation_missing"
            if reason is None:
                for co in child_rows:
                    pchild = co.get("payload")
                    obs_type = "prices" if child.get("dataset") == "TWTB8UDetail" else "conditions"
                    if (
                        not isinstance(pchild, dict)
                        or pchild.get("kind") != "event"
                        or pchild.get("observation_type") != obs_type
                        or pchild.get("source") != child.get("source")
                        or pchild.get("dataset") != child.get("dataset")
                        or pchild.get("event_type") != r["type"]
                        or pchild.get("symbol") != r["symbol"]
                    ):
                        reason = "child_payload_mismatch"
                        break
                    if (
                        pchild.get("event_date") is not None
                        and pchild.get("event_date") != r["date"]
                    ):
                        reason = "child_event_date_mismatch"
                        break
                    if child.get("dataset") == "TWTB7UDetail":
                        if (
                            child_req.get("symbol") != r["symbol"]
                            or not _date(child_req.get("file_date"))
                            or not _date(pchild.get("stop_date"))
                            or pchild.get("stop_date") != r["payload"].get("stop_date")
                        ):
                            reason = "b7_stop_date_mismatch"
                            break
                    accepted.append(
                        {
                            "obs": co,
                            "task": child,
                            "payload": pchild,
                            "parent_task_id": r["task"].get("task_id"),
                        }
                    )
            if reason:
                diag["invalid_links"].append(
                    {
                        "parent_task_id": r["task"].get("task_id"),
                        "child_task_id": link.get("child_task_id"),
                        "reason": reason,
                    }
                )
            else:
                valid_link_found = True
                linked.extend(accepted)
                linked_record_ids.update(item["obs"].get("record_id") for item in accepted)
        return linked, is_required and not valid_link_found

    for scope in attempts:
        latest_task = latest[scope]
        selected_task = available.get(scope)
        if selected_task is None:
            diag["stale_scopes"].append(
                {
                    "logical_key": scope,
                    "latest_task_id": latest_task.get("task_id"),
                    "state": latest_task.get("state"),
                    "validation_state": latest_task.get("validation_state"),
                }
            )
            continue
        stale = selected_task.get("task_id") != latest_task.get("task_id")
        if stale:
            diag["stale_scopes"].append(
                {
                    "logical_key": scope,
                    "latest_task_id": latest_task.get("task_id"),
                    "selected_task_id": selected_task.get("task_id"),
                    "state": latest_task.get("state"),
                    "validation_state": latest_task.get("validation_state"),
                }
            )
        allrows = scoped.get(scope, [])
        selected = [r for r in allrows if r["task"].get("task_id") == selected_task.get("task_id")]
        # The selected metadata snapshot is authoritative even when filtering leaves no rows.
        for r in allrows:
            if (
                not stale
                and r["task"].get("task_id") != selected_task.get("task_id")
                and not any(
                    (cur["symbol"], cur["date"], cur["type"]) == (r["symbol"], r["date"], r["type"])
                    for cur in selected
                )
            ):
                diag["superseded_observations"].append(
                    {
                        "record_id": r["obs"].get("record_id"),
                        "logical_key": scope,
                        "symbol": r["symbol"],
                        "event_date": r["date"],
                        "reason": "not_reobserved_not_cancellation",
                    }
                )
        for r in selected:
            if (
                (symbol is not None and r["symbol"] != symbol)
                or (start and r["date"] < start)
                or (end and r["date"] > end)
            ):
                continue
            history = [
                old
                for old in allrows
                if old["task"].get("task_id") != selected_task.get("task_id")
                and (old["symbol"], old["date"], old["type"]) == (r["symbol"], r["date"], r["type"])
            ]
            r["linked"], r["missing_required_detail"] = linked_details(r)
            if r["missing_required_detail"]:
                diag["invalid_links"].append(
                    {
                        "parent_task_id": r["task"].get("task_id"),
                        "reason": "missing_required_detail",
                    }
                )
            for old in history:
                old["linked"], _ = linked_details(old)
            candidate_key = (r["task"]["source"], r["symbol"], r["date"], r["type"])
            same_day_type_index.setdefault(candidate_key[:3], set()).add(candidate_key[3])
            candidates.setdefault(candidate_key, []).append(
                {
                    **r,
                    "history": history,
                    "stale": stale,
                    "unverified": selected_task.get("validation_state")
                    == "period_envelope_unverified",
                }
            )
    actions = []
    for (src, sym, d, kind), rows in sorted(
        candidates.items(), key=lambda x: (x[0][2], x[0][0], x[0][1], x[0][3])
    ):
        values: dict[str, object] = {}
        conflicts = {}
        provenance = {}
        flags = set()
        invalid = False
        invalid_fields: set[str] = set()
        evidence_rows = []
        for r in rows:
            evidence_rows.append(
                {
                    "obs": r["obs"],
                    "task": r["task"],
                    "payload": r["payload"],
                    "parent_task_id": r["task"].get("task_id"),
                    "current": True,
                }
            )
            evidence_rows.extend([{**child, "current": True} for child in r.get("linked", [])])
            q = r["payload"].get("quality_flags", [])
            if isinstance(q, list):
                flags.update(q)
            for child in r.get("linked", []):
                cq = child["payload"].get("quality_flags", [])
                if isinstance(cq, list):
                    flags.update(cq)
        for f in FIELDS:
            found: dict[str, list[dict[str, Any]]] = {}
            for r in evidence_rows:
                p = r["payload"]
                field_conf = p.get("field_conflicts")
                rawvals = (
                    field_conf[f]
                    if isinstance(field_conf, dict) and f in field_conf
                    else [p.get(f)]
                )
                for raw in rawvals:
                    if raw is None or isinstance(raw, str) and raw.strip().upper() in NA:
                        continue
                    if f in {"reference_price_kind", "stop_date", "resume_date"}:
                        if f in {"stop_date", "resume_date"} and not _date(raw):
                            invalid = True
                            flags.add("invalid_financial_value")
                            continue
                        if f == "reference_price_kind" and not isinstance(raw, str):
                            invalid = True
                            flags.add("invalid_financial_value")
                            continue
                        val = raw
                    else:
                        n = _number(raw)
                        if (
                            n is None
                            or (
                                f
                                in {
                                    "cash_dividend_per_share",
                                    "bonus_share_ratio",
                                    "cash_return_per_share",
                                }
                                and n < 0
                            )
                            or (
                                f
                                in {
                                    "share_multiplier",
                                    "par_before",
                                    "par_after",
                                    "before_price",
                                    "reference_price",
                                }
                                and n <= 0
                            )
                        ):
                            invalid = True
                            invalid_fields.add(f)
                            flags.add("invalid_financial_value")
                            continue
                        val = _canon(n)
                    found.setdefault(val, []).append(
                        {
                            "value": val,
                            "record_id": r["obs"].get("record_id"),
                            "snapshot_id": r["obs"].get("snapshot_id"),
                            "task_id": r["task"].get("task_id"),
                            "dataset": r["task"].get("dataset"),
                            "source": src,
                            "sha256": r["task"].get("sha256"),
                            "raw_path": r["task"].get("raw_path"),
                            "current": r.get("current", True),
                            "linked_parent_task_id": r.get("parent_task_id")
                            if r.get("parent_task_id") != r.get("task", {}).get("task_id")
                            else None,
                        }
                    )
            if len(found) > 1:
                conflicts[f] = [e for entries in found.values() for e in entries]
                values[f] = None
            elif found:
                values[f] = next(iter(found.keys()))
                provenance[f] = next(iter(found.values()))
            else:
                values[f] = None
        bonus = values.get("bonus_share_ratio")
        if (
            kind == "dividend"
            and bonus is not None
            and "share_multiplier" not in conflicts
            and "share_multiplier" not in invalid_fields
        ):
            mult = _canon(Decimal(str(bonus)) + 1)
            if values.get("share_multiplier") is not None and values["share_multiplier"] != mult:
                conflicts["share_multiplier"] = [
                    {
                        "value": values["share_multiplier"],
                        "evidence": provenance.get("share_multiplier"),
                    },
                    {"value": mult, "provenance": "derived from bonus_share_ratio"},
                ]
                values["share_multiplier"] = None
            elif values.get("share_multiplier") is None:
                values["share_multiplier"] = mult
                provenance["share_multiplier"] = [
                    {
                        "value": mult,
                        "derived": True,
                        "from": "bonus_share_ratio",
                        "basis": provenance.get("bonus_share_ratio", []),
                    }
                ]
        if conflicts:
            flags.add("conflicting_source_fields")
        if any(r.get("missing_required_detail") for r in rows):
            flags.add("missing_required_detail")
        tokens = {
            r["payload"].get("original", {}).get("詳細資料", "").strip()
            for r in rows
            if isinstance(r["payload"].get("original", {}).get("詳細資料"), str)
        }
        identity_ambiguous = len(tokens) > 1
        missing = [f for f in REQUIRED[kind] if values.get(f) is None]
        stale = any(r["stale"] for r in rows)
        unverified = any(r["unverified"] for r in rows) or any(
            _has_quality_flag(r["payload"], "period_envelope_unverified") for r in evidence_rows
        )
        cash_increase = any(
            bool(r["payload"].get("cash_capital_increase_present"))
            or _has_quality_flag(r["payload"], "cash_capital_increase_present")
            for r in evidence_rows
        )
        employee_bonus = any(
            bool(r["payload"].get("employee_bonus_shares_present"))
            or _has_quality_flag(r["payload"], "employee_bonus_shares_present")
            for r in evidence_rows
        )
        same_day_types = sorted(same_day_type_index.get((src, sym, d), set()))
        compound = len(same_day_types) > 1
        if compound:
            flags.add("same_day_multiple_event_types")
        if cash_increase:
            flags.add("cash_capital_increase_present")
        if employee_bonus:
            flags.add("employee_bonus_shares_present")
        cash_dividend = values.get("cash_dividend_per_share")
        cash_return = values.get("cash_return_per_share")
        bonus_ratio = values.get("bonus_share_ratio")
        par_before, par_after = values.get("par_before"), values.get("par_after")
        unmodeled_embedded_component = (
            (
                kind == "dividend"
                and (
                    (cash_return is not None and Decimal(str(cash_return)) > 0)
                    or (
                        par_before is not None
                        and par_after is not None
                        and Decimal(str(par_before)) > 0
                        and Decimal(str(par_after)) > 0
                        and Decimal(str(par_before)) != Decimal(str(par_after))
                    )
                )
            )
            or (
                kind == "capital_reduction"
                and bonus_ratio is not None
                and Decimal(str(bonus_ratio)) > 0
            )
            or (
                kind == "par_value"
                and any(
                    value is not None and Decimal(str(value)) > 0
                    for value in (cash_dividend, cash_return, bonus_ratio)
                )
            )
        )
        if unmodeled_embedded_component:
            flags.add("unmodeled_embedded_component")
        par_value_check = {
            "status": "not_checked",
            "expected": None,
            "observed": values.get("share_multiplier"),
            "relative_tolerance": "1e-8",
        }
        if kind == "par_value" and all(
            values.get(field) is not None
            for field in ("share_multiplier", "par_before", "par_after")
        ):
            before = Decimal(str(values["par_before"]))
            after = Decimal(str(values["par_after"]))
            actual = Decimal(str(values["share_multiplier"]))
            expected = before / after
            par_value_check["expected"] = _canon(expected)
            par_value_check["status"] = (
                "match"
                if abs(before - actual * after) <= abs(before) * Decimal("1e-8")
                else "mismatch"
            )
            if par_value_check["status"] == "mismatch":
                conflicts["par_value_check"] = [
                    {
                        "share_multiplier": values["share_multiplier"],
                        "face_ratio": _canon(expected),
                    }
                ]
                flags.add("par_value_check_mismatch")
        allowed_kind = {
            ("twse", "dividend"): "除權息參考價",
            ("tpex", "dividend"): "除權息參考價",
            ("twse", "capital_reduction"): "恢復買賣參考價",
            ("twse", "par_value"): "恢復買賣參考價",
            ("tpex", "capital_reduction"): "減資恢復買賣開始日參考價格",
            ("tpex", "par_value"): "恢復買賣開始參考價",
        }.get((src, kind))
        reference_kind_allowed = values.get("reference_price_kind") == allowed_kind
        if not reference_kind_allowed:
            flags.add("unknown_reference_price_kind")
        if conflicts:
            state = "conflict"
        elif stale:
            state = "stale"
        elif unverified:
            state = "unverified"
        elif (
            missing
            or invalid
            or identity_ambiguous
            or compound
            or cash_increase
            or employee_bonus
            or unmodeled_embedded_component
            or not reference_kind_allowed
            or any(r.get("missing_required_detail") for r in rows)
        ):
            state = "incomplete"
        else:
            state = "complete"
        fp = set()
        for r in rows:
            normalized = []
            for f in FIELDS:
                raw = r["payload"].get(f)
                if f in {"reference_price_kind", "stop_date", "resume_date"}:
                    value = raw
                else:
                    number = _number(raw)
                    value = _canon(number) if number is not None else raw
                normalized.append((f, value))
            fp.add(tuple(normalized))
        event_id = hashlib.sha256(
            json.dumps((src, sym, d, kind), separators=(",", ":"), ensure_ascii=False).encode()
        ).hexdigest()
        current_evidence_ids = {
            (
                r["task"].get("task_id"),
                r["obs"].get("snapshot_id"),
                r["obs"].get("record_id"),
                r.get("parent_task_id"),
            )
            for row in rows
            for r in [row, *row.get("linked", [])]
        }
        action = {
            "event_id": event_id,
            "source": src,
            "market": src,
            "symbol": sym,
            "event_date": d,
            "date_basis": {
                "kind": "summary_observation",
                "evidence": [
                    {
                        "record_id": r["obs"].get("record_id"),
                        "snapshot_id": r["obs"].get("snapshot_id"),
                        "task_id": r["task"].get("task_id"),
                    }
                    for r in rows
                ],
            },
            "event_type": kind,
            **values,
            "input_state": state,
            "missing_fields": missing,
            "conflicts": conflicts,
            "field_conflicts": conflicts,
            "quality_flags": sorted(
                flags | ({"identity_ambiguous"} if identity_ambiguous else set())
            ),
            "field_provenance": provenance,
            "summary_duplicate_count": max(0, len(rows) - len(fp)),
            "scope_versions": [
                {
                    "task_id": version["task"].get("task_id"),
                    "snapshot_id": version["obs"].get("snapshot_id"),
                    "edition": version["task"].get("edition"),
                    "dataset": version["task"].get("dataset"),
                    "source": src,
                    "record_id": version["obs"].get("record_id"),
                    "sha256": version["task"].get("sha256"),
                    "raw_path": version["task"].get("raw_path"),
                    "current": (
                        version["task"].get("task_id"),
                        version["obs"].get("snapshot_id"),
                        version["obs"].get("record_id"),
                        version.get("parent_task_id"),
                    )
                    in current_evidence_ids,
                    "linked_parent_task_id": version.get("parent_task_id")
                    if version.get("parent_task_id") != version["task"].get("task_id")
                    else None,
                }
                for version in {
                    (
                        r["task"].get("task_id"),
                        r["obs"].get("snapshot_id"),
                        r["obs"].get("record_id"),
                        r.get("parent_task_id"),
                    ): r
                    for row in rows
                    for r in [
                        row,
                        *row.get("history", []),
                        *(
                            child
                            for old in row.get("history", [])
                            for child in old.get("linked", [])
                        ),
                        *row.get("linked", []),
                    ]
                }.values()
            ],
            "field_history": [
                {
                    "record_id": old["obs"].get("record_id"),
                    "snapshot_id": old["obs"].get("snapshot_id"),
                    "task_id": old["task"].get("task_id"),
                    "edition": old["task"].get("edition"),
                    "dataset": old["task"].get("dataset"),
                    "source": src,
                    "sha256": old["task"].get("sha256"),
                    "raw_path": old["task"].get("raw_path"),
                    "current": False,
                    "linked_parent_task_id": old.get("parent_task_id"),
                    "values": {
                        field: (
                            _canon(number)
                            if (number := _number(old["payload"].get(field))) is not None
                            else None
                            if field not in {"reference_price_kind", "stop_date", "resume_date"}
                            else old["payload"].get(field)
                            if isinstance(old["payload"].get(field), str)
                            else None
                        )
                        for field in FIELDS
                    },
                }
                for old in {
                    (
                        old["task"].get("task_id"),
                        old["obs"].get("snapshot_id"),
                        old["obs"].get("record_id"),
                        old.get("parent_task_id"),
                    ): old
                    for row in rows
                    for old in [
                        *row.get("history", []),
                        *(
                            child
                            for historical in row.get("history", [])
                            for child in historical.get("linked", [])
                        ),
                    ]
                }.values()
            ],
            "compound_group_id": hashlib.sha256(
                json.dumps((src, sym, d), separators=(",", ":")).encode()
            ).hexdigest()
            if compound
            else None,
            "related_event_ids": [],
            "reference_check": {},
            "par_value_check": par_value_check,
        }
        if kind == "par_value" and "par_value_check" in conflicts:
            state = "conflict"
        reasons = []
        if state != "complete":
            reasons.append("input_state_not_complete")
        if compound:
            reasons.append("same_day_multiple_event_types")
        if cash_increase:
            reasons.append("cash_capital_increase_present")
        if employee_bonus:
            reasons.append("employee_bonus_shares_present")
        if unmodeled_embedded_component:
            reasons.append("unmodeled_embedded_component")
        if not reference_kind_allowed:
            reasons.append("unknown_reference_price_kind")
        if kind == "par_value" and par_value_check["status"] == "mismatch":
            reasons.append("par_value_check_mismatch")
        check = {"scope": "simple_event_conditional", "status": "not_checked", "reasons": reasons}
        if not reasons:
            price = Decimal(str(values["before_price"]))
            if kind == "dividend":
                dividend_cash, bonus_ratio = (
                    Decimal(str(values["cash_dividend_per_share"])),
                    Decimal(str(values["bonus_share_ratio"])),
                )
                numerator = price - dividend_cash
                denominator = Decimal(1) + bonus_ratio
                formula = "(P-c)/(1+b)"
            elif kind == "capital_reduction":
                returned_amount, reduction_cash, reduction_multiplier = (
                    Decimal(str(values[x]))
                    for x in (
                        "cash_return_per_share",
                        "cash_dividend_per_share",
                        "share_multiplier",
                    )
                )
                numerator = price - returned_amount - reduction_cash
                denominator = reduction_multiplier
                formula = "(P-return-c)/m"
            else:
                numerator = price
                denominator = Decimal(str(values["share_multiplier"]))
                formula = "P/m"
            theoretical = numerator / denominator
            if theoretical <= 0:
                flags.add("nonpositive_theoretical_reference_price")
                state = "incomplete"
                action["input_state"] = state
                action["quality_flags"] = sorted(
                    flags | ({"identity_ambiguous"} if identity_ambiguous else set())
                )
                check = {
                    "scope": "simple_event_conditional",
                    "status": "not_checked",
                    "reasons": ["nonpositive_theoretical_reference_price"],
                    "formula": formula,
                    "theoretical_reference_price": _canon(theoretical),
                }
            else:
                observed = Decimal(str(values["reference_price"]))
                difference = abs(theoretical - observed)
                check = {
                    "scope": "simple_event_conditional",
                    "status": (
                        "match"
                        if abs(numerator - observed * denominator) <= Decimal("0.01") * denominator
                        else "mismatch"
                    ),
                    "reasons": [],
                    "formula": formula,
                    "theoretical_reference_price": _canon(theoretical),
                    "observed_reference_price": _canon(observed),
                    "difference": _canon(difference),
                    "tolerance": "0.01",
                    "assumptions": [
                        "conditional simple-event diagnostic only",
                        "not an exchange rounding policy",
                        "does not produce an adjusted price series",
                    ],
                }
        action["reference_check"] = check
        if compound:
            action["related_event_ids"] = [
                hashlib.sha256(
                    json.dumps((src, sym, d, k), separators=(",", ":"), ensure_ascii=False).encode()
                ).hexdigest()
                for k in same_day_types
                if k != kind
            ]
        for row in rows:
            for old in row.get("history", []):
                changed = {}
                for field in FIELDS:
                    old_value = old["payload"].get(field)
                    new_value = row["payload"].get(field)
                    if field not in {"reference_price_kind", "stop_date", "resume_date"}:
                        old_num, new_num = _number(old_value), _number(new_value)
                        old_value = _canon(old_num) if old_num is not None else None
                        new_value = _canon(new_num) if new_num is not None else None
                    if old_value != new_value and (old_value is not None or new_value is not None):
                        changed[field] = {"previous": old_value, "current": new_value}
                old_children = {
                    child["task"].get("dataset"): child for child in old.get("linked", [])
                }
                new_children = {
                    child["task"].get("dataset"): child for child in row.get("linked", [])
                }
                for dataset in old_children.keys() & new_children.keys():
                    previous, current = old_children[dataset], new_children[dataset]
                    child_financial_change = False
                    for field in FIELDS:
                        old_value = previous["payload"].get(field)
                        new_value = current["payload"].get(field)
                        if field not in {"reference_price_kind", "stop_date", "resume_date"}:
                            old_num, new_num = _number(old_value), _number(new_value)
                            old_value = _canon(old_num) if old_num is not None else None
                            new_value = _canon(new_num) if new_num is not None else None
                        if old_value != new_value and (
                            old_value is not None or new_value is not None
                        ):
                            child_financial_change = True
                            changed[field] = {
                                "previous": old_value,
                                "current": new_value,
                                "previous_ref": {
                                    "record_id": previous["obs"].get("record_id"),
                                    "snapshot_id": previous["obs"].get("snapshot_id"),
                                    "task_id": previous["task"].get("task_id"),
                                },
                                "current_ref": {
                                    "record_id": current["obs"].get("record_id"),
                                    "snapshot_id": current["obs"].get("snapshot_id"),
                                    "task_id": current["task"].get("task_id"),
                                },
                            }
                    if child_financial_change and previous["obs"].get("record_id") != current[
                        "obs"
                    ].get("record_id"):
                        changed["linked_child_record_id"] = {
                            "previous": previous["obs"].get("record_id"),
                            "current": current["obs"].get("record_id"),
                        }
                if changed:
                    diag["revision_candidates"].append(
                        {
                            "logical_key": row["task"].get("logical_key"),
                            "record_id": row["obs"].get("record_id"),
                            "previous_record_id": old["obs"].get("record_id"),
                            "symbol": sym,
                            "event_date": d,
                            "changes": changed,
                        }
                    )
        actions.append(action)
    for detail_rows in detail_observations.values():
        for detail_row in detail_rows:
            if detail_row.get("record_id") not in linked_record_ids:
                diag["orphan_details"].append(
                    {
                        "record_id": detail_row.get("record_id"),
                        "snapshot_id": detail_row.get("snapshot_id"),
                        "task_id": snaps[detail_row.get("snapshot_id")].get("task_id"),
                        "date_filter_applied": False,
                        "reason": "not_linked_to_included_summary",
                    }
                )
    return {
        "schema_version": "corporate-actions-v1",
        "scope": "stored_official_observations",
        "time_semantics": "latest_stored_not_point_in_time",
        "coverage_complete": False,
        "research_ready": False,
        "actions": actions,
        "diagnostics": diag,
    }
