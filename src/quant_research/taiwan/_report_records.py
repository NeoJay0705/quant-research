"""Read-only record metadata statistics for report scopes."""

from __future__ import annotations

import json
import sqlite3
from datetime import date


def _iso_date(value: str, field: str) -> str:
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{field} must be a strict ISO date") from exc
    if parsed.isoformat() != value:
        raise ValueError(f"{field} must be a strict ISO date")
    return value


def record_stats(
    db: sqlite3.Connection, scopes: list[dict[str, str]]
) -> dict[str, tuple[int, int]]:
    """Count records and metadata-matching records for each unique snapshot scope."""
    if not scopes:
        return {}

    wanted: list[dict[str, str]] = []
    by_snapshot: dict[str, dict[str, str]] = {}
    for scope in scopes:
        required = {"snapshot_id", "source", "dataset", "kind", "start", "end"}
        if not required.issubset(scope) or scope["kind"] not in {"quote", "event"}:
            raise ValueError("scope is missing required metadata or has an invalid kind")
        if scope["kind"] == "event" and "event_type" not in scope:
            raise ValueError("event scope requires event_type")
        normalized = {
            key: scope[key] for key in ("snapshot_id", "source", "dataset", "kind", "start", "end")
        }
        normalized["start"] = _iso_date(scope["start"], "start")
        normalized["end"] = _iso_date(scope["end"], "end")
        if normalized["start"] > normalized["end"]:
            raise ValueError("scope start must not be after end")
        if scope["kind"] == "event":
            normalized["event_type"] = scope["event_type"]
        snapshot_id = normalized["snapshot_id"]
        prior = by_snapshot.get(snapshot_id)
        if prior is not None and prior != normalized:
            raise ValueError(f"conflicting metadata for snapshot_id {snapshot_id!r}")
        if prior is None:
            by_snapshot[snapshot_id] = normalized
            wanted.append(normalized)

    query = """
        WITH wanted AS (
            SELECT
                json_extract(value, '$.snapshot_id') AS snapshot_id,
                json_extract(value, '$.source') AS source,
                json_extract(value, '$.dataset') AS dataset,
                json_extract(value, '$.kind') AS kind,
                json_extract(value, '$.start') AS start,
                json_extract(value, '$.end') AS end,
                json_extract(value, '$.event_type') AS event_type
            FROM json_each(?)
        )
        SELECT w.snapshot_id,
               count(r.rowid) AS total_count,
               sum(CASE WHEN r.rowid IS NOT NULL AND json_valid(r.payload) THEN
                   CASE WHEN
                       r.kind = w.kind
                       AND json_type(r.payload, '$.kind') = 'text'
                       AND json_extract(r.payload, '$.kind') = w.kind
                       AND json_type(r.payload, '$.source') = 'text'
                       AND json_extract(r.payload, '$.source') = w.source
                       AND json_type(r.payload, '$.dataset') = 'text'
                       AND json_extract(r.payload, '$.dataset') = w.dataset
                       AND json_type(r.payload, '$.symbol') = 'text'
                       AND length(json_extract(r.payload, '$.symbol')) > 0
                       AND json_extract(r.payload, '$.symbol') = r.symbol
                       AND json_type(r.payload, '$.record_date') = 'text'
                       AND json_extract(r.payload, '$.record_date') = r.record_date
                       AND r.record_date >= w.start AND r.record_date <= w.end
                       AND ((w.kind = 'quote'
                           AND json_type(r.payload, '$.trade_date') = 'text'
                           AND json_extract(r.payload, '$.trade_date') = r.record_date
                           AND json_type(r.payload, '$.venue_id') = 'text'
                           AND length(json_extract(r.payload, '$.venue_id')) > 0)
                         OR (w.kind = 'event'
                           AND json_type(r.payload, '$.observation_type') = 'text'
                           AND json_extract(r.payload, '$.observation_type') = 'summary'
                           AND json_type(r.payload, '$.event_type') = 'text'
                           AND json_extract(r.payload, '$.event_type') = w.event_type
                           AND json_type(r.payload, '$.event_date') = 'text'
                           AND json_extract(r.payload, '$.event_date') = r.record_date
                           AND json_type(r.payload, '$.date') = 'text'
                           AND json_extract(r.payload, '$.date') = r.record_date))
                   THEN 1 ELSE 0 END
                 ELSE 0 END) AS matching_count
        FROM wanted AS w
        LEFT JOIN records AS r ON r.snapshot_id = w.snapshot_id
        GROUP BY w.snapshot_id
    """
    rows = db.execute(query, (json.dumps(wanted),)).fetchall()
    return {str(row[0]): (int(row[1]), int(row[2] or 0)) for row in rows}
