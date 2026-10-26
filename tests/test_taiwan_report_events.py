"""Availability event-scope validation using real official parser fixtures."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from quant_research.taiwan import official, sources
from quant_research.taiwan.reports import availability
from quant_research.taiwan.storage import SQLiteStore, TaiwanData, _corporate_actions_read

START = "2018-01-01"
END = "2018-01-05"
EVENT_START = "2018-01-02"
EVENT_END = "2018-01-03"


def _params(dataset: str, start: str = EVENT_START, end: str = EVENT_END) -> dict[str, object]:
    return {
        "pipeline": "official-v1",
        "lane": "events",
        "request": {"start_date": start, "end_date": end},
    }


def _parsed_event(
    dataset: str,
    start: str,
    end: str,
    *,
    envelope: bool = True,
    cash: str = "8",
    event_day: str | None = None,
):
    fields = ["資料日期", "股票代號", "詳細資料", "現金股利", "除權息前收盤價", "除權息參考價"]
    event_day = event_day or max(EVENT_START, start)
    roc_day = event_day.replace("-", "")
    row = [roc_day, "2330", f"2330,{roc_day}", cash, "100", "99"]
    body: dict[str, object] = {
        "stat": "OK",
        "fields": fields,
        "data": [row],
    }
    if envelope:
        body["strDate"] = start.replace("-", "")
        body["endDate"] = end.replace("-", "")
    raw = sources.RawBatch(
        "twse",
        dataset,
        "offline",
        "",
        200,
        json.dumps(body, ensure_ascii=False).encode(),
        "fixture-sha",
        len(json.dumps(body)),
    )
    return official.parse(raw, {"start_date": start, "end_date": end})


def _insert_scope(
    store: SQLiteStore,
    *,
    dataset: str = "TWT49U",
    source: str = "twse",
    logical: str = "scope",
    edition: int = 1,
    state: str = "completed",
    validation: str = "valid",
    start: str = EVENT_START,
    end: str = EVENT_END,
    snapshot_source: str | None = None,
    snapshot_dataset: str | None = None,
    snapshot_params: dict[str, object] | str | None = None,
    parse_records: bool = True,
    envelope: bool = True,
) -> tuple[str | None, str]:
    params = _params(dataset, start, end)
    records = _parsed_event(dataset, start, end, envelope=envelope).records if parse_records else ()
    snapshot_id = None
    if state == "completed" or validation != "valid":
        snapshot_id = store.save_snapshot(
            source=snapshot_source or source,
            dataset=snapshot_dataset or dataset,
            params=(snapshot_params if snapshot_params is not None else params),
            safe_url="offline",
            scope_key=f"{logical}-{edition}",
            fetched_at=f"2026-10-24T00:00:0{edition}+00:00",
            http_status=200,
            sha256=f"sha-{logical}-{edition}",
            raw_path=f"raw/{logical}-{edition}",
            validation_state=validation,
            records=tuple(records),
        )
    task_params = json.dumps(params, sort_keys=True)
    with store.connect() as db:
        db.execute(
            "INSERT INTO tasks(id,logical_key,edition,source,dataset,params,kind,state,"
            "expected_date,snapshot_id) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                f"{logical}-{edition}",
                logical,
                edition,
                source,
                dataset,
                task_params,
                "official_event_summary",
                state,
                None,
                snapshot_id,
            ),
        )
    return snapshot_id, f"{logical}-{edition}"


def _scope(report: dict[str, object], logical: str) -> dict[str, object]:
    return next(s for s in report["event_scopes"] if s.get("logical_key") == logical)


def test_scopes_intersect_query_and_latest_editions_define_state(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path)
    store.initialize()
    _insert_scope(store, logical="overlap-a", start="2018-01-02", end="2018-01-03")
    _insert_scope(store, logical="overlap-b", start="2018-01-03", end="2018-01-04")
    _insert_scope(store, dataset="TWTAUU", logical="failed", state="failed", parse_records=False)
    _insert_scope(
        store, dataset="TWTB8U", logical="empty", validation="empty_unverified", parse_records=False
    )
    parsed_warning = _parsed_event("TWT49U", EVENT_START, EVENT_END, envelope=False)
    assert "period_envelope_unverified" in parsed_warning.warnings
    _insert_scope(
        store,
        logical="warning",
        envelope=False,
        validation="period_envelope_unverified",
    )
    report = availability(tmp_path, START, END)
    assert report["unplanned_event_gaps"] == [
        {"source": "twse", "dataset": "TWT49U", "start": START, "end": START},
        {"source": "twse", "dataset": "TWT49U", "start": "2018-01-05", "end": END},
        *[
            gap
            for ds in ("TWTAUU", "TWTB8U")
            for gap in (
                {"source": "twse", "dataset": ds, "start": START, "end": START},
                {"source": "twse", "dataset": ds, "start": "2018-01-04", "end": END},
            )
        ],
        *[
            {"source": "tpex", "dataset": ds, "start": START, "end": END}
            for ds in ("exDailyQ", "pvChgRslt", "revivt")
        ],
    ]
    assert _scope(report, "overlap-a")["acquisition_state"] == "available"
    assert _scope(report, "overlap-b")["acquisition_state"] == "available"
    assert _scope(report, "failed")["acquisition_state"] == "failed"
    assert _scope(report, "empty")["acquisition_state"] == "empty_unverified"
    assert _scope(report, "warning")["acquisition_state"] == "unverified"

    # A later version remains authoritative even when it is not a valid summary.
    _insert_scope(store, logical="overlap-a", edition=2, state="failed", parse_records=False)
    assert _scope(availability(tmp_path, START, END), "overlap-a")["acquisition_state"] == "failed"
    _insert_scope(store, logical="overlap-b", edition=2, state="pending", parse_records=False)
    assert _scope(availability(tmp_path, START, END), "overlap-b")["acquisition_state"] == "pending"


def test_bad_latest_provenance_and_record_metadata_never_become_available(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path)
    store.initialize()
    sid, task_id = _insert_scope(store, logical="snapshot-request")
    assert sid
    with store.connect() as db:
        db.execute("UPDATE snapshots SET params=? WHERE id=?", ("{bad", sid))
    assert (
        _scope(availability(tmp_path, EVENT_START, EVENT_END, source="twse"), "snapshot-request")[
            "acquisition_state"
        ]
        == "invalid"
    )

    _insert_scope(store, logical="snapshot-source", snapshot_source="tpex")
    _insert_scope(store, logical="snapshot-dataset", snapshot_dataset="dailyQuotes")
    assert (
        _scope(availability(tmp_path, EVENT_START, EVENT_END, source="twse"), "snapshot-source")[
            "acquisition_state"
        ]
        == "invalid"
    )

    _insert_scope(store, logical="bad-json", edition=1)
    _insert_scope(store, logical="bad-json", edition=2, state="failed", parse_records=False)
    _insert_scope(store, logical="bad-kind", edition=1)
    _insert_scope(store, logical="bad-kind", edition=2, parse_records=False)
    _insert_scope(store, logical="wrong-lane")
    _insert_scope(store, source="finmind", logical="finmind")
    with store.connect() as db:
        db.execute("UPDATE tasks SET params=? WHERE id=?", ("{broken", "bad-kind-2"))
        db.execute("UPDATE tasks SET kind='official_event_detail' WHERE id='bad-kind-2'")
        db.execute(
            "UPDATE tasks SET params=json_set(params,'$.lane','quotes') WHERE id='wrong-lane-1'"
        )
    report = availability(tmp_path, EVENT_START, EVENT_END, source="twse")
    assert _scope(report, "bad-json")["acquisition_state"] == "failed"
    assert report["financial_conditions"] is None
    assert report["financial_conditions_error"] == {
        "code": "invalid_stored_event_json",
        "message": "stored event JSON is invalid",
    }
    assert "financial_conditions_unavailable" in report["warnings"]
    assert _scope(report, "bad-kind")["date_filter_applied"] is False
    assert _scope(report, "wrong-lane")["date_filter_applied"] is False
    assert not any(scope.get("logical_key") == "finmind" for scope in report["event_scopes"])

    bad_type, _ = _insert_scope(store, logical="bad-type")
    assert bad_type
    with store.connect() as db:
        db.execute("UPDATE records SET kind='quote' WHERE snapshot_id=?", (bad_type,))
    assert (
        _scope(availability(tmp_path, EVENT_START, EVENT_END, source="twse"), "bad-type")[
            "acquisition_state"
        ]
        == "invalid"
    )

    bad_date, _ = _insert_scope(store, logical="bad-date")
    assert bad_date
    with store.connect() as db:
        row = db.execute(
            "SELECT id,payload FROM records WHERE snapshot_id=?", (bad_date,)
        ).fetchone()
        payload = json.loads(row["payload"])
        payload["record_date"] = "2018-02-01"
        db.execute("UPDATE records SET payload=? WHERE id=?", (json.dumps(payload), row["id"]))
    assert (
        _scope(availability(tmp_path, EVENT_START, EVENT_END, source="twse"), "bad-date")[
            "acquisition_state"
        ]
        == "invalid"
    )

    bad_source, _ = _insert_scope(store, logical="bad-record-source")
    assert bad_source
    with store.connect() as db:
        row = db.execute(
            "SELECT id,payload FROM records WHERE snapshot_id=?", (bad_source,)
        ).fetchone()
        payload = json.loads(row["payload"])
        payload["source"] = "tpex"
        db.execute("UPDATE records SET payload=? WHERE id=?", (json.dumps(payload), row["id"]))
    assert (
        _scope(availability(tmp_path, EVENT_START, EVENT_END, source="twse"), "bad-record-source")[
            "acquisition_state"
        ]
        == "invalid"
    )

    corrupt, _ = _insert_scope(store, logical="raw-json")
    assert corrupt
    with store.connect() as db:
        db.execute("UPDATE records SET payload='{not-json' WHERE snapshot_id=?", (corrupt,))
    # Corrupt payload is intentional DB corruption. Report must diagnose it without SQL failure.
    result = availability(tmp_path, EVENT_START, EVENT_END, source="twse")
    assert _scope(result, "raw-json")["acquisition_state"] == "invalid"


def test_source_blocks_empty_evidence_and_financial_action_baseline(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path)
    store.initialize()
    _insert_scope(store, logical="action")
    store.put(
        "source_blocks",
        "source",
        {
            "source": "twse",
            "reason": "test",
            "state": "blocked",
            "blocked_at": "2026-10-24T00:00:00+00:00",
            "retry_at": None,
        },
    )
    store.put(
        "source_blocks",
        "source",
        {
            "source": "tpex",
            "reason": "other-source",
            "state": "blocked",
            "blocked_at": "2026-10-24T00:00:00+00:00",
            "retry_at": None,
        },
    )
    report = availability(tmp_path, EVENT_START, EVENT_END, source="twse")
    assert [block["source"] for block in report["source_blocks"]] == ["twse"]
    scope = _scope(report, "action")
    assert scope["acquisition_state"] == "available"
    empty = _insert_scope(
        store,
        dataset="TWTAUU",
        logical="known-empty",
        validation="empty_unverified",
        parse_records=False,
    )
    assert empty[0]
    assert (
        _scope(availability(tmp_path, EVENT_START, EVENT_END, source="twse"), "known-empty")[
            "acquisition_state"
        ]
        == "empty_unverified"
    )
    report_after_empty = availability(tmp_path, EVENT_START, EVENT_END, source="twse")
    assert report_after_empty["financial_conditions"] == TaiwanData(tmp_path).corporate_actions(
        source="twse", start=EVENT_START, end=EVENT_END
    )
    assert report_after_empty["financial_conditions"]["actions"]
    action = report_after_empty["financial_conditions"]["actions"][0]
    assert action["input_state"] == "incomplete"
    assert "missing_required_detail" in action["quality_flags"]


def test_availability_uses_one_wal_read_snapshot_for_events_and_financial_helper(
    tmp_path: Path, monkeypatch
) -> None:
    store = SQLiteStore(tmp_path)
    store.initialize()
    sid, _ = _insert_scope(store, logical="wal-scope")
    assert sid
    reader_path = tmp_path / "state.sqlite3"
    writer = sqlite3.connect(reader_path)
    writer.execute("PRAGMA wal_autocheckpoint=0")
    assert writer.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    real_read = _corporate_actions_read
    observed = {"calls": 0, "commits": 0, "mode": None}
    readers: list[sqlite3.Connection] = []

    def commit_edition_two(reader, **kwargs):
        readers.append(reader)
        observed["calls"] += 1
        if observed["calls"] > 1:
            return real_read(reader, **kwargs)
        observed["mode"] = reader.execute("PRAGMA query_only").fetchone()[0]
        observed["transaction"] = reader.in_transaction
        try:
            reader.execute("CREATE TABLE report_reader_write_probe(value INTEGER)")
        except sqlite3.OperationalError as exc:
            observed["readonly"] = "readonly" in str(exc).lower()
        else:
            raise AssertionError("availability reader unexpectedly allowed a write")
        params = _params("TWT49U")
        parsed = _parsed_event("TWT49U", EVENT_START, EVENT_END, cash="9")
        next_sid = store.save_snapshot(
            source="twse",
            dataset="TWT49U",
            params=params,
            safe_url="offline",
            scope_key="wal-scope-2",
            fetched_at="2026-10-24T00:01:00+00:00",
            http_status=200,
            sha256="sha-wal-2",
            raw_path="raw/wal-2",
            validation_state="valid",
            records=tuple(parsed.records),
            connection=writer,
        )
        writer.execute(
            "INSERT INTO tasks(id,logical_key,edition,source,dataset,params,kind,state,"
            "snapshot_id) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (
                "wal-scope-2",
                "wal-scope",
                2,
                "twse",
                "TWT49U",
                json.dumps(params, sort_keys=True),
                "official_event_summary",
                "completed",
                next_sid,
            ),
        )
        writer.commit()
        observed["commits"] += 1
        return real_read(reader, **kwargs)

    monkeypatch.setattr("quant_research.taiwan.reports._corporate_actions_read", commit_edition_two)
    try:
        first = availability(tmp_path, EVENT_START, EVENT_END, source="twse")
        second = availability(tmp_path, EVENT_START, EVENT_END, source="twse")
    finally:
        writer.close()
    assert len(readers) == 2
    for reader in readers:
        try:
            reader.execute("SELECT 1")
        except sqlite3.ProgrammingError:
            pass
        else:
            raise AssertionError("availability reader connection was not closed")
    assert observed == {
        "calls": 2,
        "commits": 1,
        "mode": 0,
        "transaction": True,
        "readonly": True,
    }
    assert _scope(first, "wal-scope")["edition"] == 1
    assert _scope(first, "wal-scope")["snapshot_id"] == sid
    assert _scope(second, "wal-scope")["edition"] == 2
    assert _scope(second, "wal-scope")["snapshot_id"] != sid
    first_action = first["financial_conditions"]["actions"][0]
    second_action = second["financial_conditions"]["actions"][0]
    assert first_action["cash_dividend_per_share"] == "8"
    assert second_action["cash_dividend_per_share"] == "9"
    assert first_action["scope_versions"][0]["snapshot_id"] == sid
    assert (
        second_action["scope_versions"][0]["snapshot_id"]
        == _scope(second, "wal-scope")["snapshot_id"]
    )
    assert second["financial_conditions"] == TaiwanData(tmp_path).corporate_actions(
        source="twse", start=EVENT_START, end=EVENT_END
    )
