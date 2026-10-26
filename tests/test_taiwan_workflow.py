import json
from datetime import UTC, datetime

import pytest

from quant_research.taiwan.storage import SQLiteStore, TaiwanData, WriterBusyError
from quant_research.taiwan.workflow import TaiwanWorkflow


def test_resume_repairs_legacy_capital_reduction_without_mutating_evidence(tmp_path):
    import hashlib
    import json

    store = SQLiteStore(tmp_path)
    store.initialize()
    params = {"data_id": "2409", "start_date": "2022-10-11", "end_date": "2022-10-11"}
    raw_path = store.save_raw("finmind", b"legacy raw")
    snapshot_id = store.save_snapshot(
        source="finmind",
        dataset="TaiwanStockCapitalReduction",
        params=params,
        safe_url="https://api.finmindtrade.com/api/v4/data?dataset=TaiwanStockCapitalReduction",
        scope_key="legacy",
        fetched_at="2022-10-11T00:00:00+00:00",
        http_status=422,
        sha256=hashlib.sha256(b"legacy raw").hexdigest(),
        raw_path=raw_path,
        validation_state="invalid",
        error="HTTP 422 enum rejection",
    )
    with store.connect() as db:
        db.execute(
            "INSERT INTO tasks(id,logical_key,edition,source,dataset,params,kind,state,"
            "snapshot_id,error) "
            "VALUES('legacy-task','legacy-key',1,'finmind',"
            "'TaiwanStockCapitalReduction',?,'history','failed',?,"
            "'HTTP 422 enum rejection')",
            (json.dumps(params, sort_keys=True), snapshot_id),
        )
    calls = []

    def transport(url, headers, timeout, max_bytes):
        calls.append(url)
        return 200, b'{"status":"success","data":[]}'

    workflow = TaiwanWorkflow(tmp_path, transport=transport, sleep=lambda seconds: None)
    first = workflow.resume()
    assert first["state"] == "completed", first
    assert len(calls) == 1
    assert "dataset=TaiwanStockCapitalReductionReferencePrice" in calls[0]
    assert "dataset=TaiwanStockCapitalReduction&" not in calls[0]
    with store.connect() as db:
        old = db.execute(
            "SELECT dataset,state,error,snapshot_id FROM tasks WHERE id='legacy-task'"
        ).fetchone()
        replacement = db.execute(
            "SELECT id,dataset,state,params FROM tasks WHERE id<> 'legacy-task'"
        ).fetchone()
        assert old["dataset"] == "TaiwanStockCapitalReduction"
        assert old["state"] == "superseded"
        assert "HTTP 422 enum rejection" in old["error"]
        assert replacement["id"] in old["error"]
        assert replacement["dataset"] == "TaiwanStockCapitalReductionReferencePrice"
        assert replacement["state"] == "completed"
        assert json.loads(replacement["params"]) == params
        assert old["snapshot_id"] == snapshot_id
        assert (
            db.execute("SELECT COUNT(*) FROM snapshots WHERE id=?", (snapshot_id,)).fetchone()[0]
            == 1
        )
    assert (tmp_path / raw_path).read_bytes() == b"legacy raw"
    assert TaiwanData(tmp_path).status()["tasks"]["superseded"] == 1
    second = workflow.resume()
    assert second["state"] == "completed"
    assert len(calls) == 1


def test_backfill_pauses_and_resumes_without_refetching_completed(tmp_path):
    calls = []

    def transport(url, headers, timeout, max_bytes):
        calls.append(url)
        return 200, json.dumps({"status": "success", "data": []}).encode()

    clock = [datetime(2025, 1, 2, tzinfo=UTC)]

    def sleep(seconds):
        from datetime import timedelta

        clock[0] += timedelta(seconds=seconds)

    workflow = TaiwanWorkflow(tmp_path, transport=transport, now=lambda: clock[0], sleep=sleep)
    first = workflow.backfill(["2330"], start="2024-01-01", end="2024-01-02", max_tasks=2)
    assert first["state"] == "paused"
    assert first["completed"] == 2
    assert len(calls) == 2
    second = workflow.resume()
    assert second["state"] == "completed"
    assert second["completed"] == 2
    assert len(calls) == 4
    assert len(set(calls)) == 4


def test_fresh_root_sync_initializes_under_lock(tmp_path):
    from datetime import timedelta

    from quant_research.taiwan.sources import TWSE_URL

    clock = [datetime(2025, 1, 2, tzinfo=UTC)]
    calls = []

    def sleep(seconds):
        clock[0] += timedelta(seconds=seconds)

    def transport(url, headers, timeout, max_bytes):
        calls.append(url)
        if "exchangeReport" in url:
            body = (
                "日期,證券代號,證券名稱,成交股數,成交金額,開盤價,最高價,最低價,收盤價\n"
                "1140102,2330,x,1,2,3,4,2,3\n"
            ).encode()
            return 200, body
        if "tpex.org.tw" in url:
            body = (
                "資料日期,代號,名稱,成交股數,成交金額,開盤,最高,最低,收盤\n"
                "1140102,2330,x,1,2,3,4,2,3\n"
            ).encode()
            return 200, body
        return 200, b'{"status":"success","data":[]}'

    result = TaiwanWorkflow(tmp_path, transport=transport, now=lambda: clock[0], sleep=sleep).sync()
    assert result["state"] == "completed"
    assert result["planned"] == 7
    assert len(calls) == 6
    assert TWSE_URL in calls
    assert (
        TaiwanData(tmp_path).status()["runs"][0]["started_at"]
        != TaiwanData(tmp_path).status()["runs"][0]["finished_at"]
    )


def test_busy_resume_does_not_change_database(tmp_path):
    store = SQLiteStore(tmp_path)
    store.initialize()
    with store.connect() as db:
        db.execute(
            "INSERT INTO runs(id,kind,started_at,updated_at,state) "
            "VALUES('live','sync','s','u','running')"
        )
    with store.writer_lock():
        with pytest.raises(WriterBusyError):
            TaiwanWorkflow(tmp_path).resume()
    with store.connect() as db:
        assert db.execute("SELECT state,finished_at FROM runs WHERE id='live'").fetchone()[:] == (
            "running",
            None,
        )


def test_403_persists_block_and_saves_raw_once(tmp_path):
    calls = []

    def denied(url, headers, timeout, max_bytes):
        calls.append(url)
        return 403, b"blocked"

    workflow = TaiwanWorkflow(tmp_path, transport=denied)
    first = workflow.backfill(["2330"], start="2024-01-01", end="2024-01-01")
    assert first["state"] == "blocked"
    assert len(calls) == 1
    raw_files = list((tmp_path / "raw" / "finmind").rglob("*.bin"))
    assert len(raw_files) == 1 and raw_files[0].read_bytes() == b"blocked"
    second = workflow.resume()
    assert second["state"] == "blocked"
    assert len(calls) == 1


def test_parse_error_keeps_one_raw_and_invalid_snapshot(tmp_path):
    import sqlite3

    def malformed(url, headers, timeout, max_bytes):
        return 200, b"not-json"

    result = TaiwanWorkflow(tmp_path, transport=malformed).backfill(
        ["2330"], start="2024-01-01", end="2024-01-01", max_tasks=1
    )
    assert result["state"] == "failed"
    assert len(list((tmp_path / "raw" / "finmind").rglob("*.bin"))) == 1
    with sqlite3.connect(tmp_path / "state.sqlite3") as db:
        assert db.execute("SELECT validation_state FROM snapshots").fetchone()[0] == "invalid"


def test_200_quota_envelope_saves_raw_once_and_honors_retry_after(tmp_path):
    from datetime import timedelta

    clock = [datetime(2025, 1, 2, tzinfo=UTC)]
    calls = []

    def transport(url, headers, timeout, max_bytes):
        calls.append(url)
        return 200, b'{"status":429,"data":[]}', {"Retry-After": "7200"}

    workflow = TaiwanWorkflow(
        tmp_path,
        transport=transport,
        now=lambda: clock[0],
        sleep=lambda seconds: clock.__setitem__(0, clock[0] + timedelta(seconds=seconds)),
    )
    result = workflow.backfill(["2330"], start="2024-01-01", end="2024-01-01")
    assert result["state"] == "partial"
    assert len(calls) == 1
    assert len(list((tmp_path / "raw" / "finmind").rglob("*.bin"))) == 1
    with SQLiteStore(tmp_path).connect() as db:
        retry_at = db.execute("SELECT retry_at FROM source_blocks").fetchone()[0]
    assert (datetime.fromisoformat(retry_at) - clock[0]).total_seconds() >= 7200


def test_new_backfill_only_executes_tasks_it_claimed(tmp_path):
    calls = []

    def transport(url, headers, timeout, max_bytes):
        calls.append(url)
        return 200, b'{"status":"success","data":[]}'

    from datetime import timedelta

    clock = [datetime(2025, 1, 2, tzinfo=UTC)]

    def sleep(seconds):
        clock[0] += timedelta(seconds=seconds)

    workflow = TaiwanWorkflow(tmp_path, transport=transport, now=lambda: clock[0], sleep=sleep)
    workflow.backfill(["1111"], start="2024-01-01", end="2024-01-02", max_tasks=1)
    first_count = len(calls)
    workflow.backfill(["2222"], start="2024-01-01", end="2024-01-02", max_tasks=1)
    assert len(calls) == first_count + 1
    with SQLiteStore(tmp_path).connect() as db:
        old_pending = db.execute(
            "SELECT COUNT(*) FROM tasks WHERE json_extract(params,'$.data_id')='1111' "
            "AND state='pending'"
        ).fetchone()[0]
    assert old_pending == 3


def test_same_backfill_reclaims_unfinished_task_instead_of_reporting_zero(tmp_path):
    calls = []

    def transport(url, headers, timeout, max_bytes):
        calls.append(url)
        return 200, b'{"status":"success","data":[]}'

    workflow = TaiwanWorkflow(tmp_path, transport=transport)
    args = (["2330"],)
    kwargs = {"start": "2024-01-01", "end": "2024-01-02", "max_tasks": 1}
    first = workflow.backfill(*args, **kwargs)
    second = workflow.backfill(*args, **kwargs)
    assert first["state"] == second["state"] == "paused"
    assert second["planned"] > 0 and second["completed"] == 1
    assert len(calls) == 2


def test_sync_expected_date_is_persisted_on_tasks_and_iso_validated(tmp_path):
    def transport(url, headers, timeout, max_bytes):
        if "exchangeReport" in url:
            body = (
                "日期,證券代號,證券名稱,成交股數,成交金額,開盤價,最高價,最低價,收盤價\n"
                "1140102,2330,x,1,2,3,4,2,3\n"
            )
            return 200, body.encode()
        if "tpex.org.tw" in url:
            body = (
                "資料日期,代號,名稱,成交股數,成交金額,開盤,最高,最低,收盤\n"
                "1140102,2330,x,1,2,3,4,2,3\n"
            )
            return 200, body.encode()
        return 200, b'{"status":"success","data":[]}'

    workflow = TaiwanWorkflow(tmp_path, transport=transport)
    with pytest.raises(ValueError, match="expected-date"):
        workflow.sync(expected_date="20250102")
    result = workflow.sync(expected_date="2025-01-01", max_tasks=1)
    assert result["state"] == "partial"
    with SQLiteStore(tmp_path).connect() as db:
        first = db.execute(
            "SELECT expected_date FROM tasks WHERE source='twse' ORDER BY rowid DESC LIMIT 1"
        ).fetchone()[0]
    # A subsequent plain sync uses a different edition; the saved first task retains its date.
    workflow.sync(max_tasks=1)
    with SQLiteStore(tmp_path).connect() as db:
        assert (
            db.execute(
                "SELECT expected_date FROM tasks WHERE source='twse' ORDER BY rowid LIMIT 1"
            ).fetchone()[0]
            == first
            == "2025-01-01"
        )


def test_full_backfill_waits_for_metadata_before_scheduling_candidates(tmp_path):
    from datetime import timedelta

    clock = [datetime(2025, 1, 2, tzinfo=UTC)]
    calls = []

    def sleep(seconds):
        clock[0] += timedelta(seconds=seconds)

    def transport(url, headers, timeout, max_bytes):
        calls.append(url)
        body = {"status": "success", "data": []}
        if "TaiwanStockInfo" in url:
            body["data"] = [
                {"stock_id": "2330", "type": "twse", "industry_category": "semiconductor"}
            ]
        return 200, json.dumps(body).encode()

    result = TaiwanWorkflow(
        tmp_path, transport=transport, now=lambda: clock[0], sleep=sleep
    ).backfill(start="2024-01-01", end="2024-01-02", max_tasks=3)
    assert result["state"] == "paused", result.get("error")
    assert len(calls) == 2
    with SQLiteStore(tmp_path).connect() as db:
        rows = db.execute(
            "SELECT dataset,state FROM tasks WHERE json_extract(params,'$.data_id')='2330'"
        ).fetchall()
    assert {(row["dataset"], row["state"]) for row in rows} == {
        ("TaiwanStockPrice", "pending"),
        ("TaiwanStockDividend", "pending"),
        ("TaiwanStockDividendResult", "pending"),
        ("TaiwanStockCapitalReductionReferencePrice", "pending"),
    }


def test_cooldown_blocks_until_expiry_then_allows_request(tmp_path):
    from datetime import timedelta

    clock = [datetime(2025, 1, 2, tzinfo=UTC)]
    calls = []
    store = SQLiteStore(tmp_path)
    store.initialize()
    with store.connect() as db:
        db.execute(
            "INSERT INTO tasks(id,logical_key,edition,source,dataset,params,kind,state) "
            "VALUES('t','k',1,'finmind','TaiwanStockInfo','{}','metadata','pending')"
        )
        db.execute(
            "INSERT INTO source_blocks VALUES('finmind','rate limit','cooldown',?,?)",
            (clock[0].isoformat(), (clock[0] + timedelta(seconds=30)).isoformat()),
        )

    def transport(url, headers, timeout, max_bytes):
        calls.append(url)
        return 200, b'{"status":"success","data":[]}'

    workflow = TaiwanWorkflow(
        tmp_path, transport=transport, now=lambda: clock[0], sleep=lambda seconds: None
    )
    assert workflow.resume()["state"] == "blocked"
    assert calls == []
    clock[0] += timedelta(seconds=31)
    assert workflow.resume()["state"] == "completed"
    assert len(calls) == 1


def test_sync_plans_first_backfill_for_new_tracked_candidate(tmp_path):
    from datetime import timedelta

    clock = [datetime(2025, 1, 2, tzinfo=UTC)]
    store = SQLiteStore(tmp_path)
    store.initialize()
    with store.connect() as db:
        db.execute("INSERT INTO settings VALUES('track_all','1')")
        db.execute("INSERT INTO settings VALUES('backfill_start','2024-01-01')")

    def sleep(seconds):
        clock[0] += timedelta(seconds=seconds)

    def transport(url, headers, timeout, max_bytes):
        if "exchangeReport" in url:
            body = (
                "日期,證券代號,證券名稱,成交股數,成交金額,開盤價,最高價,最低價,收盤價\n"
                "1140102,2330,x,1,2,3,4,2,3\n"
            )
            return 200, body.encode()
        if "tpex.org.tw" in url:
            body = (
                "資料日期,代號,名稱,成交股數,成交金額,開盤,最高,最低,收盤\n"
                "1140102,2330,x,1,2,3,4,2,3\n"
            )
            return 200, body.encode()
        body = {"status": "success", "data": []}
        if "TaiwanStockInfo" in url:
            body["data"] = [
                {"stock_id": "2330", "type": "twse", "industry_category": "semiconductor"}
            ]
        return 200, json.dumps(body).encode()

    result = TaiwanWorkflow(tmp_path, transport=transport, now=lambda: clock[0], sleep=sleep).sync(
        max_tasks=7
    )
    assert result["state"] == "paused"
    with store.connect() as db:
        rows = db.execute(
            "SELECT dataset,state FROM tasks WHERE json_extract(params,'$.data_id')='2330'"
        ).fetchall()
    assert {(row["dataset"], row["state"]) for row in rows} == {
        ("TaiwanStockPrice", "pending"),
        ("TaiwanStockDividend", "pending"),
        ("TaiwanStockDividendResult", "pending"),
        ("TaiwanStockCapitalReductionReferencePrice", "pending"),
    }
