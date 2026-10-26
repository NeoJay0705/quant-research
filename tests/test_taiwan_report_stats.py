from __future__ import annotations

import json
import sqlite3

import pytest

from quant_research.taiwan._report_records import record_stats


@pytest.fixture
def db() -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:")
    connection.execute(
        "CREATE TABLE records (id INTEGER PRIMARY KEY, snapshot_id TEXT, kind TEXT, "
        "symbol TEXT, record_date TEXT, payload TEXT)"
    )
    return connection


def _insert(
    db: sqlite3.Connection,
    snapshot_id: str,
    kind: str,
    symbol: str,
    record_date: str,
    payload: dict[str, object] | str,
) -> None:
    encoded = payload if isinstance(payload, str) else json.dumps(payload)
    db.execute(
        "INSERT INTO records(snapshot_id, kind, symbol, record_date, payload) "
        "VALUES (?, ?, ?, ?, ?)",
        (snapshot_id, kind, symbol, record_date, encoded),
    )


def _quote(snapshot_id: str, source: str = "twse") -> dict[str, str]:
    return {
        "snapshot_id": snapshot_id,
        "source": source,
        "dataset": "daily_quotes",
        "kind": "quote",
        "start": "2025-01-02",
        "end": "2025-01-02",
    }


def _quote_payload(source: str, venue_id: object = "TWSE") -> dict[str, object]:
    return {
        "kind": "quote",
        "source": source,
        "dataset": "daily_quotes",
        "symbol": "2330",
        "record_date": "2025-01-02",
        "trade_date": "2025-01-02",
        "venue_id": venue_id,
    }


def test_quote_scopes_count_rows_and_match_metadata(db: sqlite3.Connection) -> None:
    _insert(db, "twse", "quote", "2330", "2025-01-02", _quote_payload("twse"))
    _insert(db, "twse", "quote", "2317", "2025-01-02", _quote_payload("twse"))
    _insert(db, "twse", "quote", "2330", "2025-01-03", _quote_payload("twse"))
    _insert(db, "twse", "quote", "2330", "2025-01-02", _quote_payload("tpex"))
    _insert(db, "twse", "quote", "2330", "2025-01-02", _quote_payload("twse", 12))
    _insert(db, "twse", "quote", "2330", "2025-01-02", "{bad json")
    _insert(
        db,
        "tpex",
        "quote",
        "6488",
        "2025-01-02",
        {**_quote_payload("tpex", "TPEx"), "symbol": "6488"},
    )
    assert record_stats(db, [_quote("twse"), _quote("tpex", "tpex")]) == {
        "twse": (6, 1),
        "tpex": (1, 1),
    }


def test_event_summary_match_and_zero_record_scope(db: sqlite3.Connection) -> None:
    good = {
        "kind": "event",
        "source": "finmind",
        "dataset": "events",
        "symbol": "2330",
        "record_date": "2025-01-02",
        "observation_type": "summary",
        "event_type": "dividend",
        "event_date": "2025-01-02",
        "date": "2025-01-02",
    }
    _insert(db, "event", "event", "2330", "2025-01-02", good)
    _insert(db, "event", "event", "2330", "2025-01-02", {**good, "observation_type": "detail"})
    _insert(db, "event", "event", "2330", "2025-01-02", {**good, "event_type": "split"})
    _insert(db, "event", "event", "2330", "2025-01-02", {**good, "event_date": "2025-01-03"})
    _insert(db, "event", "event", "2330", "2025-01-02", {**good, "date": "2025-01-03"})
    scope = {
        "snapshot_id": "event",
        "source": "finmind",
        "dataset": "events",
        "kind": "event",
        "start": "2025-01-02",
        "end": "2025-01-02",
        "event_type": "dividend",
    }
    assert record_stats(db, [scope, {**scope, "snapshot_id": "empty"}]) == {
        "event": (5, 1),
        "empty": (0, 0),
    }


def test_empty_scopes_run_no_query_and_duplicate_metadata_deduplicates(
    db: sqlite3.Connection,
) -> None:
    statements: list[str] = []
    db.set_trace_callback(statements.append)
    assert record_stats(db, []) == {}
    assert statements == []
    assert record_stats(db, [_quote("none"), _quote("none")]) == {"none": (0, 0)}
    assert len([statement for statement in statements if "SELECT" in statement.upper()]) == 1


def test_duplicate_snapshot_with_conflicting_metadata_is_rejected(db: sqlite3.Connection) -> None:
    with pytest.raises(ValueError, match="conflicting metadata"):
        record_stats(db, [_quote("same"), _quote("same", "tpex")])


@pytest.mark.parametrize("value", ["2025-1-02", "2025-01-02T00:00:00", "20250201"])
def test_scope_dates_must_be_strict_iso(value: str, db: sqlite3.Connection) -> None:
    scope = _quote("bad-date")
    scope["start"] = value
    with pytest.raises(ValueError, match="strict ISO date"):
        record_stats(db, [scope])


def test_large_scope_batch_uses_one_bound_json_argument(db: sqlite3.Connection) -> None:
    scopes = [_quote(f"snapshot-{index}") for index in range(1200)]
    statements: list[str] = []
    db.set_trace_callback(statements.append)
    assert len(record_stats(db, scopes)) == 1200
    selects = [statement for statement in statements if "SELECT" in statement.upper()]
    assert len(selects) == 1
