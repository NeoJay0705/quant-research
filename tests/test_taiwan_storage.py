from __future__ import annotations

import pytest

from quant_research.taiwan._report_records import record_stats
from quant_research.taiwan.reports import _count_records
from quant_research.taiwan.storage import SQLiteStore, TaiwanData, WriterBusyError


def test_snapshot_index_supports_report_record_lookups(tmp_path) -> None:
    store = SQLiteStore(tmp_path)
    store.initialize()
    with store.connect() as db:
        index_columns = [row[2] for row in db.execute("PRAGMA index_info(records_snapshot)")]
        assert index_columns == ["snapshot_id"]

    matching = {
        "kind": "quote",
        "source": "synthetic",
        "dataset": "synthetic-quotes",
        "symbol": "SYNTH",
        "trade_date": "2026-10-23",
        "record_date": "2026-10-23",
        "venue_id": "synthetic-venue",
    }
    snapshot_id = store.save_snapshot(
        source="synthetic",
        dataset="synthetic-quotes",
        params={},
        safe_url="synthetic://fixture",
        scope_key="synthetic",
        fetched_at="2026-10-23T00:00:00+00:00",
        http_status=200,
        sha256="synthetic-fixture",
        raw_path="synthetic/raw",
        validation_state="valid",
        records=(matching, {**matching, "symbol": "OTHER", "venue_id": "other-venue"}),
    )
    with store.connect() as db:
        before = [
            tuple(row)
            for row in db.execute(
                "SELECT snapshot_id,kind,symbol,record_date,payload FROM records ORDER BY id"
            )
        ]
    store.initialize()

    with store.connect() as db:
        assert [row[2] for row in db.execute("PRAGMA index_info(records_snapshot)")] == [
            "snapshot_id"
        ]
        after = [
            tuple(row)
            for row in db.execute(
                "SELECT snapshot_id,kind,symbol,record_date,payload FROM records ORDER BY id"
            )
        ]
        assert after == before
        assert len(after) == 2
        assert db.execute("SELECT count(*) FROM records").fetchone()[0] == 2
        helper_sql: list[str] = []
        db.set_trace_callback(helper_sql.append)
        assert record_stats(
            db,
            [
                {
                    "snapshot_id": snapshot_id,
                    "source": "synthetic",
                    "dataset": "synthetic-quotes",
                    "kind": "quote",
                    "start": "2026-10-23",
                    "end": "2026-10-23",
                }
            ],
        ) == {snapshot_id: (2, 2)}
        join_sql = next(sql for sql in helper_sql if "LEFT JOIN records AS r" in sql)
        join_plan = db.execute("EXPLAIN QUERY PLAN " + join_sql).fetchall()
        assert any("records_snapshot" in str(row[3]) for row in join_plan)

        count_sql: list[str] = []
        db.set_trace_callback(count_sql.append)
        assert _count_records(db, [snapshot_id]) == {snapshot_id: 2}
        grouped_sql = next(sql for sql in count_sql if "json_each" in sql)
        grouped_plan = db.execute("EXPLAIN QUERY PLAN " + grouped_sql).fetchall()
        assert any("records_snapshot" in str(row[3]) for row in grouped_plan)


def test_explicit_init_raw_append_only_and_read_only_facade(tmp_path) -> None:
    missing = tmp_path / "not-created"
    api = TaiwanData(missing)
    assert api.status()["initialized"] is False
    assert not missing.exists()
    store = SQLiteStore(tmp_path)
    store.initialize()
    raw_path = store.save_raw("twse", b"fixture", "2026-10-22T00:00:00+00:00")
    assert (tmp_path / raw_path).read_bytes() == b"fixture"
    quote = {
        "kind": "quote",
        "symbol": "2330",
        "trade_date": "2026-10-22",
        "close": "100.1",
        "research_ready": False,
        "quality_flags": ["history_coverage_unverified"],
    }
    store.save_snapshot(
        source="twse",
        dataset=None,
        params={},
        safe_url="https://safe",
        scope_key="2026-10-22",
        fetched_at="2026-10-22T01:00:00+00:00",
        http_status=200,
        sha256="hash",
        raw_path=raw_path,
        validation_state="valid",
        records=(quote,),
    )
    rows = TaiwanData(tmp_path).quotes("2330", source="twse")
    assert rows[0]["close"] == "100.1"
    assert rows[0]["research_ready"] is False
    assert rows[0]["provenance"]["source"] == "twse"
    assert rows[0]["provenance"]["scope_key"] == "2026-10-22"
    with pytest.raises(ValueError):
        TaiwanData(tmp_path).quotes("2330", source="")


def test_newest_valid_scope_hides_previous_snapshot(tmp_path) -> None:
    store = SQLiteStore(tmp_path)
    store.initialize()
    for stamp, state, payload in [
        ("2026-10-22T01:00:00Z", "valid", {"close": "10"}),
        ("2026-10-22T02:00:00Z", "valid", {"close": "11"}),
        ("2026-10-22T03:00:00Z", "invalid", {"close": "12"}),
        ("2026-10-22T04:00:00Z", "valid", {"close": "13"}),
    ]:
        record = {"kind": "quote", "symbol": "2330", "trade_date": "2026-10-22", **payload}
        store.save_snapshot(
            source="twse",
            dataset=None,
            params={},
            safe_url="safe",
            scope_key="overlap" if stamp.endswith("04:00:00Z") else "same",
            fetched_at=stamp,
            http_status=200,
            sha256=stamp,
            raw_path="raw.bin",
            validation_state=state,
            records=(record,),
        )
    rows = TaiwanData(tmp_path).quotes("2330", source="twse")
    assert len(rows) == 1
    assert rows[0]["close"] == "13"


def test_same_day_events_and_empty_refresh_hide_old_quote(tmp_path) -> None:
    store = SQLiteStore(tmp_path)
    store.initialize()

    def snapshot(dataset, scope, stamp, records):
        store.save_snapshot(
            source="finmind",
            dataset=dataset,
            params={},
            safe_url="safe",
            scope_key=scope,
            fetched_at=stamp,
            http_status=200,
            sha256=stamp,
            raw_path="raw",
            validation_state="valid",
            records=records,
        )

    snapshot(
        "TaiwanStockDividend",
        "q",
        "2026-01-01T00:00:00Z",
        ({"kind": "event", "symbol": "2330", "record_date": "2026-01-01"},),
    )
    snapshot(
        "TaiwanStockCapitalReductionReferencePrice",
        "q",
        "2026-01-02T00:00:00Z",
        ({"kind": "event", "symbol": "2330", "record_date": "2026-01-01"},),
    )
    events = TaiwanData(tmp_path).events("2330", source="finmind")
    assert len(events) == 2
    snapshot(
        "TaiwanStockPrice",
        "price",
        "2026-01-01T00:00:00Z",
        ({"kind": "quote", "symbol": "2330", "trade_date": "2026-01-01", "close": "10"},),
    )
    snapshot("TaiwanStockPrice", "price", "2026-01-02T00:00:00Z", ())
    assert TaiwanData(tmp_path).quotes("2330", source="finmind") == []


def test_sources_are_isolated_and_venue_quotes_do_not_collapse(tmp_path) -> None:
    store = SQLiteStore(tmp_path)
    store.initialize()
    for source, venue, close in [
        ("twse", "primary", "10"),
        ("twse", "management", "9"),
        ("finmind", "primary", "8"),
    ]:
        store.save_snapshot(
            source=source,
            dataset="quotes",
            params={},
            safe_url="safe",
            scope_key=source + venue,
            fetched_at="2026-01-01T00:00:00Z",
            http_status=200,
            sha256=close,
            raw_path="raw",
            validation_state="valid",
            records=(
                {
                    "kind": "quote",
                    "symbol": "2330",
                    "trade_date": "2026-01-01",
                    "venue_id": venue,
                    "close": close,
                },
                {"kind": "event", "symbol": "2330", "record_date": "2026-01-01"},
            ),
        )
    data = TaiwanData(tmp_path)
    assert {row["close"] for row in data.quotes("2330", source="twse")} == {"9", "10"}
    assert len(data.events("2330")) == 2
    assert len(data.events("2330", source="finmind")) == 1
    official_symbols = data.symbols()
    assert len(official_symbols) == 1
    assert official_symbols[0]["source"] == "twse"
    assert official_symbols[0]["quote_presence"]["count"] == 2
    assert data.symbols(source="finmind") == []
    with pytest.raises(ValueError):
        data.events(source="bad")
    with pytest.raises(ValueError):
        data.symbols(source="bad")


def test_symbol_history_union_and_exclusion(tmp_path) -> None:
    store = SQLiteStore(tmp_path)
    store.initialize()
    for dataset, stamp, record in [
        (
            "TaiwanStockInfo",
            "2026-01-01T00:00:00Z",
            {
                "kind": "symbol",
                "symbol": "2330",
                "classification": "candidate_unverified",
                "type": "twse",
                "industry_category": "半導體業",
                "name": "old",
            },
        ),
        (
            "TaiwanStockDelisting",
            "2026-01-02T00:00:00Z",
            {
                "kind": "symbol",
                "symbol": "9999",
                "classification": "candidate_unverified",
                "date": "2025-12-31",
            },
        ),
        (
            "TaiwanStockInfo",
            "2026-01-03T00:00:00Z",
            {
                "kind": "symbol",
                "symbol": "0050",
                "type": "twse",
                "industry_category": "ETF",
            },
        ),
    ]:
        store.save_snapshot(
            source="finmind",
            dataset=dataset,
            params={},
            safe_url="safe",
            scope_key=stamp,
            fetched_at=stamp,
            http_status=200,
            sha256=stamp,
            raw_path="raw",
            validation_state="valid",
            records=(record,),
        )
    symbols = {row["symbol"]: row for row in TaiwanData(tmp_path).symbols(source="finmind")}
    assert set(symbols) == {"2330", "9999", "0050"}
    assert len(symbols["2330"]["evidence"]) == 1
    assert symbols["0050"]["classification"] == "excluded_by_provider_category"


def test_writer_lock_stale_recovery_and_status(tmp_path) -> None:
    store = SQLiteStore(tmp_path)
    store.initialize()
    with store.writer_lock():
        assert TaiwanData(tmp_path).status()["running"] is True
        with pytest.raises(WriterBusyError):
            with store.writer_lock():
                pass
    now = "2026-10-22T00:00:00Z"
    with store.connect() as db:
        db.execute(
            "INSERT INTO runs(id,kind,started_at,updated_at,state) VALUES ('r','sync',?,?, 'running')",  # noqa: E501
            (now, now),
        )
        db.execute(
            "INSERT INTO tasks(id,logical_key,edition,source,params,kind,state) VALUES ('t','x',1,'twse','{}','quote','running')"  # noqa: E501
        )
    # A stored running run without the lock is reported as interrupted, without DB mutation.
    status = TaiwanData(tmp_path).status()
    assert status["runs"][0]["state"] == "interrupted"  # type: ignore[index]
    assert status["runs"][0]["stored_state"] == "running"  # type: ignore[index]
    assert status["last_run"]["state"] == "interrupted"  # type: ignore[index]
    with store.connect() as db:
        assert db.execute("SELECT state FROM runs WHERE id='r'").fetchone()[0] == "running"
    store.recover_stale_runs()
    assert TaiwanData(tmp_path).status()["runs"][0]["state"] == "interrupted"  # type: ignore[index]
    with store.connect() as db:
        assert db.execute("SELECT state FROM tasks WHERE id='t'").fetchone()[0] == "pending"


def test_status_uses_official_runner_lock_when_writer_lock_is_free(tmp_path) -> None:
    from quant_research.taiwan.official_workflow import OfficialWorkflow

    store = SQLiteStore(tmp_path)
    store.initialize()
    with store.connect() as db:
        db.execute(
            "INSERT INTO runs(id,kind,started_at,updated_at,state) "
            "VALUES('official','official-quotes:sync','now','now','running')"
        )
    workflow = OfficialWorkflow(tmp_path)
    with workflow._runner_lock():
        status = TaiwanData(tmp_path).status()
    assert status["lanes"]["official_quotes_running"] is True
    assert status["runs"][0]["state"] == "running"


def test_quotes_reactivate_other_scope_and_stable_same_timestamp_tiebreak(tmp_path) -> None:
    store = SQLiteStore(tmp_path)
    store.initialize()

    def save(scope, stamp, records):
        store.save_snapshot(
            source="finmind",
            dataset="TaiwanStockPrice",
            params={},
            safe_url="safe",
            scope_key=scope,
            fetched_at=stamp,
            http_status=200,
            sha256=stamp,
            raw_path="raw",
            validation_state="valid",
            records=records,
        )

    def quote(close, date="2026-01-01"):
        return ({"kind": "quote", "symbol": "2330", "trade_date": date, "close": close},)

    save("A", "2026-01-01T00:00:00Z", quote("A"))
    save("B", "2026-01-02T00:00:00Z", quote("B"))
    observed = TaiwanData(tmp_path).quotes("2330", source="finmind")
    assert [row["close"] for row in observed] == ["B"]
    save("B", "2026-01-03T00:00:00Z", ())
    observed = TaiwanData(tmp_path).quotes("2330", source="finmind")
    assert [row["close"] for row in observed] == ["A"]
    save("A", "2026-01-04T00:00:00Z", quote("same-time-first"))
    save("A", "2026-01-04T00:00:00Z", quote("same-time-second"))
    observed = TaiwanData(tmp_path).quotes("2330", source="finmind")
    assert [row["close"] for row in observed] == ["same-time-second"]


def test_quote_latest_date_recedes_after_newer_scope_is_withdrawn(tmp_path) -> None:
    store = SQLiteStore(tmp_path)
    store.initialize()

    def save(scope, stamp, records):
        store.save_snapshot(
            source="finmind",
            dataset="TaiwanStockPrice",
            params={},
            safe_url="safe",
            scope_key=scope,
            fetched_at=stamp,
            http_status=200,
            sha256=stamp,
            raw_path="raw",
            validation_state="valid",
            records=records,
        )

    def quote(day):
        return ({"kind": "quote", "symbol": "2330", "trade_date": day, "close": day},)

    save("older", "2026-01-01T00:00:00Z", quote("2026-01-01"))
    save("newer", "2026-01-02T00:00:00Z", quote("2026-01-02"))
    save("newer", "2026-01-03T00:00:00Z", ())
    assert TaiwanData(tmp_path).status()["latest_quote_date"]["finmind"] == "2026-01-01"


def test_symbols_merge_categories_and_keep_removed_evidence(tmp_path) -> None:
    store = SQLiteStore(tmp_path)
    store.initialize()

    def save(dataset, stamp, records):
        store.save_snapshot(
            source="finmind",
            dataset=dataset,
            params={},
            safe_url="safe",
            scope_key=stamp,
            fetched_at=stamp,
            http_status=200,
            sha256=stamp,
            raw_path="raw",
            validation_state="valid",
            records=records,
        )

    save(
        "TaiwanStockInfo",
        "1",
        (
            {"kind": "symbol", "symbol": "0050", "type": "twse", "stock_name": "普通名稱"},
            {"kind": "symbol", "symbol": "2330", "type": "twse", "industry_category": "半導體業"},
            {"kind": "symbol", "symbol": "6488", "type": "tpex", "industry_category": "ETF"},
            {"kind": "symbol", "symbol": "9998", "type": "otc", "industry_category": "ETF"},
        ),
    )
    save(
        "TaiwanStockInfo",
        "2",
        ({"kind": "symbol", "symbol": "2330", "type": "twse", "industry_category": "ETF"},),
    )
    save("TaiwanStockInfo", "3", ())
    save(
        "TaiwanStockDelisting",
        "4",
        (
            {"kind": "symbol", "symbol": "9999", "date": "2025-01-01"},
            {"kind": "symbol", "symbol": "2330", "date": "2025-01-01"},
        ),
    )
    symbols = {row["symbol"]: row for row in TaiwanData(tmp_path).symbols(source="finmind")}
    assert set(symbols) == {"0050", "2330", "6488", "9999"}
    # stock_name alone is not an explicit classification.
    assert symbols["0050"]["classification"] == "candidate_unverified"
    assert symbols["2330"]["classification"] == "candidate_unverified"
    assert "classification_conflict" in symbols["2330"]["quality_flags"]
    assert symbols["6488"]["classification"] == "excluded_by_provider_category"
    included = TaiwanData(tmp_path).symbols(include_excluded=False, source="finmind")
    assert "6488" not in {row["symbol"] for row in included}
    assert symbols["9999"]["classification"] == "candidate_unverified"
    assert len(symbols["2330"]["evidence"]) == 3
    assert TaiwanData(tmp_path).status()["tasks"] == {
        "pending": 0,
        "running": 0,
        "completed": 0,
        "failed": 0,
        "superseded": 0,
    }
    assert "history_coverage_unverified" in TaiwanData(tmp_path).status()["coverage_warnings"]


def test_readonly_uri_special_path_and_connection_is_closed(tmp_path, monkeypatch) -> None:
    import sqlite3

    from quant_research.taiwan import storage

    root = tmp_path / "folder?#name"
    SQLiteStore(root).initialize()
    real_connect = sqlite3.connect
    opened = []

    def capture(*args, **kwargs):
        connection = real_connect(*args, **kwargs)
        opened.append(connection)
        return connection

    monkeypatch.setattr(storage.sqlite3, "connect", capture)
    assert TaiwanData(root).symbols() == []
    with pytest.raises(sqlite3.ProgrammingError):
        opened[0].execute("SELECT 1")
