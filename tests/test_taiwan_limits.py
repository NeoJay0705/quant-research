import json
from datetime import UTC, datetime, timedelta

import pytest

from quant_research.taiwan.storage import SQLiteStore, TaiwanData
from quant_research.taiwan.workflow import TaiwanWorkflow


def _advance(clock, seconds):
    clock[0] += timedelta(seconds=seconds)


def _empty():
    return b'{"status":"success","data":[]}'


def test_finmind_throttle_is_shared_and_respects_hourly_log(tmp_path):
    clock = [datetime(2025, 1, 2, tzinfo=UTC)]
    seen = []

    def transport(url, headers, timeout, max_bytes):
        seen.append(clock[0])
        return 200, _empty()

    def sleep(seconds):
        _advance(clock, seconds)

    first = TaiwanWorkflow(tmp_path, transport=transport, now=lambda: clock[0], sleep=sleep)
    result = first.backfill(["2330"], "2024-01-01", "2024-01-01", max_tasks=1)
    assert result["state"] == "paused"
    second = TaiwanWorkflow(tmp_path, transport=transport, now=lambda: clock[0], sleep=sleep)
    assert second.resume(max_tasks=1)["completed"] == 1
    assert (seen[1] - seen[0]).total_seconds() >= 12.1

    # A full persisted hour must be allowed to expire, without pruning its evidence.
    root = tmp_path / "full-hour"
    store = SQLiteStore(root)
    store.initialize()
    origin = clock[0]
    with store.connect() as db:
        for index in range(300):
            db.execute(
                "INSERT INTO request_log(source,requested_at) VALUES('finmind',?)",
                ((origin - timedelta(seconds=3599 - index * 0.001)).isoformat(),),
            )
    requests_before = []

    def full_transport(url, headers, timeout, max_bytes):
        requests_before.append(clock[0])
        return 200, _empty()

    waiting = []
    full_hour_start = clock[0]

    def full_sleep(seconds):
        waiting.append(seconds)
        _advance(clock, seconds)

    answer = TaiwanWorkflow(
        root, transport=full_transport, now=lambda: clock[0], sleep=full_sleep
    ).backfill(["2330"], "2024-02-01", "2024-02-01", max_tasks=1)
    assert answer["completed"] == 1
    assert requests_before
    assert (requests_before[0] - full_hour_start).total_seconds() >= 1.1
    with store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM request_log").fetchone()[0] == 301

    with pytest.raises(ValueError, match="FINMIND_TOKEN"):
        TaiwanWorkflow(tmp_path / "bad-limit", finmind_limit=600)
    token = "fake-token-600"
    later = []

    def token_transport(url, headers, timeout, max_bytes):
        assert headers.get("Authorization") == f"Bearer {token}"
        later.append(clock[0])
        return 200, _empty()

    result = TaiwanWorkflow(
        tmp_path / "600",
        transport=token_transport,
        now=lambda: clock[0],
        sleep=sleep,
        finmind_limit=600,
        token=token,
    ).backfill(["2330"], "2024-03-01", "2024-03-01", max_tasks=2)
    assert result["completed"] == 2
    assert (later[1] - later[0]).total_seconds() >= 6.1


def test_5xx_retry_saves_each_invalid_attempt_and_stops_after_three(tmp_path):
    clock = [datetime(2025, 1, 2, tzinfo=UTC)]
    calls = []

    def sleep(seconds):
        _advance(clock, seconds)

    def transport(url, headers, timeout, max_bytes):
        calls.append(url)
        if len(calls) <= 2:
            return 503, f"failure-{len(calls)}".encode()
        if "TaiwanStockPrice" in url:
            return 200, json.dumps(
                {
                    "status": "success",
                    "data": [
                        {
                            "date": "2024-01-01",
                            "stock_id": "2330",
                            "open": 10,
                            "max": 11,
                            "min": 9,
                            "close": 10,
                            "Trading_Volume": 100,
                            "Trading_Money": 1000,
                        }
                    ],
                }
            ).encode()
        return 200, _empty()

    root = tmp_path / "eventual-success"
    result = TaiwanWorkflow(root, transport=transport, now=lambda: clock[0], sleep=sleep).backfill(
        ["2330"], "2024-01-01", "2024-01-01"
    )
    assert result["state"] == "completed" and result["completed"] == 4
    assert len(calls) == 6 and calls[0] == calls[1] == calls[2]
    with SQLiteStore(root).connect() as db:
        attempts = db.execute(
            "SELECT attempts FROM tasks WHERE dataset='TaiwanStockPrice'"
        ).fetchone()[0]
        failures = db.execute(
            "SELECT http_status,validation_state,raw_path FROM snapshots "
            "WHERE http_status=503 ORDER BY rowid"
        ).fetchall()
    assert attempts == 3
    assert len(failures) == 2
    assert [(root / row[2]).read_bytes() for row in failures] == [b"failure-1", b"failure-2"]
    assert all(row[1] == "invalid" for row in failures)

    for label, failing_transport in (
        ("always-503", lambda url, headers, timeout, max_bytes: (503, b"still down")),
        (
            "network-error",
            lambda url, headers, timeout, max_bytes: (_ for _ in ()).throw(
                OSError("network unavailable")
            ),
        ),
    ):
        failure_root = tmp_path / label
        failed = TaiwanWorkflow(
            failure_root, transport=failing_transport, now=lambda: clock[0], sleep=sleep
        ).backfill(["2330"], "2024-01-01", "2024-01-01")
        assert failed["state"] == "failed"
        assert failed["completed"] == 0 and failed["pending"] == 3 and failed["failed"] == 1
        with SQLiteStore(failure_root).connect() as db:
            task = db.execute(
                "SELECT attempts,state FROM tasks WHERE dataset='TaiwanStockPrice'"
            ).fetchone()
            snapshots = db.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0]
        assert tuple(task) == (3, "failed")
        assert snapshots == (3 if label == "always-503" else 0)
        assert len(list((failure_root / "raw" / "finmind").rglob("*.bin"))) == (
            3 if label == "always-503" else 0
        )


def test_refresh_editions_quotes_and_token_secrecy(tmp_path):
    clock = [datetime(2025, 1, 2, tzinfo=UTC)]
    token = "FAKE-CREDENTIAL-never-persist"
    price_values = iter((10, 20))
    calls = []

    def sleep(seconds):
        _advance(clock, seconds)

    def transport(url, headers, timeout, max_bytes):
        assert headers.get("Authorization") == f"Bearer {token}"
        assert token not in url
        calls.append(url)
        if "TaiwanStockPrice" in url:
            value = next(price_values)
            body = {
                "status": "success",
                "data": [
                    {
                        "date": "2024-01-01",
                        "stock_id": "2330",
                        "open": value,
                        "max": value + 1,
                        "min": value - 1,
                        "close": value,
                        "Trading_Volume": 100,
                        "Trading_Money": 1000,
                    }
                ],
            }
            return 200, json.dumps(body).encode()
        return 200, _empty()

    root = tmp_path / "store"
    workflow = TaiwanWorkflow(
        root, transport=transport, now=lambda: clock[0], sleep=sleep, token=token
    )
    args = (["2330"], "2024-01-01", "2024-01-01")
    assert workflow.backfill(*args)["completed"] == 4
    initial_calls = len(calls)
    assert workflow.backfill(*args)["completed"] == 0
    assert len(calls) == initial_calls
    refreshed = workflow.backfill(*args, refresh=True)
    assert refreshed["completed"] == 4 and len(calls) == initial_calls + 4
    quotes = TaiwanData(root).quotes("2330", source="finmind")
    assert len(quotes) == 1 and quotes[0]["close"] == "20"
    assert quotes[0]["research_ready"] is False
    raw = list((root / "raw" / "finmind").rglob("*.bin"))
    assert len(raw) == 8
    assert any(b'"close": 10' in path.read_bytes() for path in raw)
    assert any(b'"close": 20' in path.read_bytes() for path in raw)
    with SQLiteStore(root).connect() as db:
        editions = db.execute(
            "SELECT edition,state FROM tasks WHERE dataset='TaiwanStockPrice' ORDER BY edition"
        ).fetchall()
    assert [tuple(row) for row in editions] == [(1, "completed"), (2, "completed")]

    def leaking_transport(url, headers, timeout, max_bytes):
        raise RuntimeError(f"transport leaked {token}")

    failure_root = tmp_path / "credential-error"
    failed = TaiwanWorkflow(
        failure_root, transport=leaking_transport, now=lambda: clock[0], sleep=sleep, token=token
    ).backfill(["2330"], "2024-02-01", "2024-02-01", max_tasks=1)
    assert failed["state"] == "failed" and token not in str(failed)
    for path in failure_root.rglob("*"):
        if path.is_file():
            assert token.encode() not in path.read_bytes(), str(path)
    for path in root.rglob("*"):
        if path.is_file():
            assert token.encode() not in path.read_bytes(), str(path)
    with SQLiteStore(failure_root).connect() as db:
        error = db.execute("SELECT error FROM tasks WHERE state='failed'").fetchone()[0]
    assert token not in error


def test_sync_event_tail_uses_completed_history_and_does_not_add_symbols(tmp_path):
    clock = [datetime(2025, 1, 2, tzinfo=UTC)]

    def sleep(seconds):
        _advance(clock, seconds)

    def transport(url, headers, timeout, max_bytes):
        if "exchangeReport" in url:
            return 200, (
                "日期,證券代號,證券名稱,成交股數,成交金額,開盤價,最高價,最低價,收盤價\n"
                "1140102,2330,x,1,2,3,4,2,3\n"
            ).encode()
        if "tpex.org.tw" in url:
            return 200, (
                "資料日期,代號,名稱,成交股數,成交金額,開盤,最高,最低,收盤\n"
                "1140102,2330,x,1,2,3,4,2,3\n"
            ).encode()
        if "TaiwanStockPrice" in url:
            row = {
                "date": "2024-01-02",
                "stock_id": "2330",
                "open": 10,
                "max": 11,
                "min": 9,
                "close": 10,
                "Trading_Volume": 100,
                "Trading_Money": 1000,
            }
            return 200, json.dumps({"status": "success", "data": [row]}).encode()
        if "TaiwanStockDividendResult" in url:
            row = {
                "date": "2024-01-02",
                "stock_id": "2330",
                "before_price": 10,
                "after_price": 9,
                "stock_and_cache_dividend": 1,
                "stock_or_cache_dividend": 0,
            }
            return 200, json.dumps({"status": "success", "data": [row]}).encode()
        if "TaiwanStockDividend" in url:
            row = {
                "date": "2024-01-02",
                "stock_id": "2330",
                "CashEarningsDistribution": 1,
                "CashStatutorySurplus": 0,
                "StockEarningsDistribution": 0,
                "StockStatutorySurplus": 0,
            }
            return 200, json.dumps({"status": "success", "data": [row]}).encode()
        if "TaiwanStockCapitalReductionReferencePrice" in url:
            row = {
                "date": "2024-01-02",
                "stock_id": "2330",
                "ClosingPriceonTheLastTradingDay": 10,
                "PostReductionReferencePrice": 9,
                "ReasonforCapitalReduction": "x",
            }
            return 200, json.dumps({"status": "success", "data": [row]}).encode()
        return 200, _empty()

    root = tmp_path / "store"
    workflow = TaiwanWorkflow(root, transport=transport, now=lambda: clock[0], sleep=sleep)
    assert workflow.backfill(["2330"], "2024-01-01", "2024-01-02")["completed"] == 4
    clock[0] += timedelta(days=61)
    synced = workflow.sync(max_tasks=7)
    assert synced["state"] == "paused" and synced["completed"] == 7
    with SQLiteStore(root).connect() as db:
        tails = db.execute(
            "SELECT dataset,params FROM tasks WHERE kind='event_tail' ORDER BY dataset"
        ).fetchall()
        assert (
            db.execute(
                "SELECT COUNT(*) FROM tasks WHERE kind='history' AND state='pending'"
            ).fetchone()[0]
            == 0
        )
        assert (
            db.execute(
                "SELECT COUNT(*) FROM tasks WHERE json_extract(params,'$.data_id') IS NOT NULL "
                "AND json_extract(params,'$.data_id')!='2330'"
            ).fetchone()[0]
            == 0
        )
        assert (
            db.execute(
                "SELECT COUNT(*) FROM tasks WHERE source='finmind' AND kind='metadata' "
                "AND state='completed'"
            ).fetchone()[0]
            == 4
        )
        assert (
            db.execute(
                "SELECT COUNT(*) FROM tasks WHERE source='local' AND dataset='plan_sync' "
                "AND state='completed'"
            ).fetchone()[0]
            == 1
        )
    assert len(tails) == 3
    assert {json.loads(row[1])["start_date"] for row in tails} == {"2024-01-01"}
    assert all(
        json.loads(row[1])["start_date"] != (clock[0].date() - timedelta(days=30)).isoformat()
        for row in tails
    )
    assert len(TaiwanData(root).quotes("2330", source="finmind")) == 1
    assert TaiwanData(root).quotes("2330", source="finmind")[0]["research_ready"] is False
    with SQLiteStore(root).connect() as db:
        assert (
            db.execute(
                "SELECT COUNT(*) FROM records WHERE kind='event' "
                "AND json_extract(payload,'$.research_ready')=0"
            ).fetchone()[0]
            == 3
        )


def test_stopping_during_throttle_interrupts_without_next_http(tmp_path):
    clock = [datetime(2025, 1, 2, tzinfo=UTC)]
    calls = []
    first = True

    def transport(url, headers, timeout, max_bytes):
        calls.append(url)
        return 200, _empty()

    def sleep(seconds):
        nonlocal first
        _advance(clock, seconds)
        if not first:
            workflow.stopping = True
        first = False

    workflow = TaiwanWorkflow(tmp_path, transport=transport, now=lambda: clock[0], sleep=sleep)
    initial = workflow.backfill(["2330"], "2024-01-01", "2024-01-01", max_tasks=1)
    assert initial["state"] == "paused" and len(calls) == 1
    resumed = workflow.resume(max_tasks=1)
    assert resumed["state"] == "interrupted"
    assert len(calls) == 1
    status = TaiwanData(tmp_path).status()
    assert status["last_run"]["state"] == "interrupted"
    with SQLiteStore(tmp_path).connect() as db:
        assert db.execute("SELECT COUNT(*) FROM tasks WHERE state='pending'").fetchone()[0] == 3
        assert db.execute("SELECT COUNT(*) FROM tasks WHERE state='completed'").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM runs WHERE state='completed'").fetchone()[0] == 0
