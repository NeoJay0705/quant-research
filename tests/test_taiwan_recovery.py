from __future__ import annotations

import os
import select
import signal
import sqlite3
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

from quant_research.taiwan.storage import SQLiteStore, TaiwanData
from quant_research.taiwan.workflow import TaiwanWorkflow


def test_sigkill_recovers_running_task_and_resumes_only_remaining_tasks(tmp_path):
    root = tmp_path / "store"
    script = r"""
import sys, time
from datetime import UTC, datetime, timedelta
from quant_research.taiwan.workflow import TaiwanWorkflow
root = sys.argv[1]
clock = [datetime(2025, 1, 2, tzinfo=UTC)]
def sleep(seconds): clock[0] += timedelta(seconds=seconds)
def transport(url, headers, timeout, max_bytes):
    if "TaiwanStockPrice" in url:
        return 200, b'{"status":"success","data":[]}'
    print("SECOND_TRANSPORT_BLOCKED", flush=True)
    while True: time.sleep(60)
TaiwanWorkflow(root, transport=transport, now=lambda: clock[0], sleep=sleep).backfill(
    ["2330"], start="2024-01-01", end="2024-01-02")
"""
    env = os.environ.copy()
    env["PYTHONPATH"] = (
        str(Path(__file__).resolve().parents[1] / "src") + os.pathsep + env.get("PYTHONPATH", "")
    )
    child = subprocess.Popen(
        [sys.executable, "-c", script, str(root)],
        cwd=Path(__file__).resolve().parents[1],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )
    try:
        assert child.stdout is not None
        ready, _, _ = select.select([child.stdout], [], [], 20)
        assert ready, "child did not reach blocking transport"
        assert child.stdout.readline().strip() == "SECOND_TRANSPORT_BLOCKED"
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            with sqlite3.connect(root / "state.sqlite3") as db:
                states = dict(db.execute("SELECT dataset,state FROM tasks"))
            if (
                states.get("TaiwanStockPrice") == "completed"
                and states.get("TaiwanStockDividend") == "running"
            ):
                break
            time.sleep(0.05)
        assert states["TaiwanStockPrice"] == "completed"
        assert states["TaiwanStockDividend"] == "running"
        status = TaiwanData(root).status()
        assert status["running"] is True
        assert status["last_run"]["state"] == "running"
        child.send_signal(signal.SIGKILL)
        child.wait(timeout=10)
        assert child.returncode == -signal.SIGKILL
        stopped_status = TaiwanData(root).status()
        assert stopped_status["running"] is False
        assert stopped_status["last_run"]["state"] == "interrupted"

        calls = []
        clock = [datetime(2025, 1, 2, tzinfo=UTC)]

        def transport(url, headers, timeout, max_bytes):
            calls.append(url)
            return 200, b'{"status":"success","data":[]}'

        resumed = TaiwanWorkflow(
            root,
            transport=transport,
            now=lambda: clock[0],
            sleep=lambda seconds: clock.__setitem__(0, clock[0] + timedelta(seconds=seconds)),
        ).resume()
        assert resumed["state"] == "completed"
        assert resumed["completed"] == 3
        assert len(calls) == 3
        with SQLiteStore(root).connect() as db:
            tasks = db.execute("SELECT dataset,state FROM tasks").fetchall()
            runs = db.execute("SELECT state FROM runs ORDER BY started_at").fetchall()
        assert len(tasks) == 4
        assert {row[1] for row in tasks} == {"completed"}
        assert runs[0][0] == "interrupted"
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=10)
        if child.stdout:
            child.stdout.close()
        if child.stderr:
            child.stderr.close()


def test_expected_date_mismatch_keeps_valid_actual_data_and_isolated_on_resume(tmp_path):
    calls = []

    def transport(url, headers, timeout, max_bytes):
        calls.append(url)
        if "exchangeReport" in url:
            body = (
                "日期,證券代號,證券名稱,成交股數,成交金額,開盤價,最高價,最低價,收盤價\n"
                "1140102,2330,x,1,2,3,4,2,3\n"
            )
            return 200, body.encode()
        if "tpex.org.tw" in url:
            return 200, (
                "資料日期,代號,名稱,成交股數,成交金額,開盤,最高,最低,收盤\n"
                "1140102,2330,x,1,2,3,4,2,3\n"
            ).encode()
        return 200, b'{"status":"success","data":[]}'

    workflow = TaiwanWorkflow(tmp_path, transport=transport)
    first = workflow.sync(expected_date="2025-01-03", max_tasks=1)
    assert first["state"] == "partial"
    assert "expected-date mismatch" in str(first["error"])
    rows = TaiwanData(tmp_path).quotes("2330", source="twse")
    assert len(rows) == 1
    assert rows[0]["trade_date"] == "2025-01-02"
    assert rows[0]["research_ready"] is False
    with SQLiteStore(tmp_path).connect() as db:
        old = db.execute(
            "SELECT id,expected_date,state FROM tasks WHERE source='twse' ORDER BY rowid LIMIT 1"
        ).fetchone()
        snapshot = db.execute("SELECT validation_state,scope_key,error FROM snapshots").fetchone()
    assert old[1:] == ("2025-01-03", "failed")
    assert snapshot[:] == ("valid", "2025-01-02", "awaiting_publication")
    assert "2025-01-03" not in calls[0]

    ordinary = workflow.sync(max_tasks=1)
    assert ordinary["state"] in {"paused", "partial"}
    # The plain sync must not mutate the expected date attached to the earlier failed task.
    with SQLiteStore(tmp_path).connect() as db:
        assert (
            db.execute("SELECT expected_date FROM tasks WHERE id=?", (old[0],)).fetchone()[0]
            == "2025-01-03"
        )
    resumed = workflow.resume(max_tasks=1)
    assert resumed["state"] == "partial"
    with SQLiteStore(tmp_path).connect() as db:
        assert (
            db.execute("SELECT expected_date FROM tasks WHERE id=?", (old[0],)).fetchone()[0]
            == "2025-01-03"
        )
        assert (
            db.execute("SELECT error FROM tasks WHERE id=?", (old[0],)).fetchone()[0]
            == "expected-date mismatch"
        )
    assert len(TaiwanData(tmp_path).quotes("2330", source="twse")) == 1


def test_retry_after_larger_than_one_day_is_preserved_without_waiting(tmp_path):
    clock = datetime(2025, 1, 2, tzinfo=UTC)

    class Raw:
        retry_after = "172800"

    class Quota(Exception):
        raw = Raw()

    retry_at = TaiwanWorkflow(tmp_path, now=lambda: clock)._cooldown(Quota())
    assert datetime.fromisoformat(retry_at) - clock == timedelta(seconds=172800)
