from __future__ import annotations

import json
import os
import selectors
import signal
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlparse

import pytest

from quant_research.taiwan.official_workflow import OfficialWorkflow
from quant_research.taiwan.storage import SQLiteStore


def response(source: str, day: str) -> bytes:
    if source == "twse":
        doc = {
            "stat": "OK",
            "date": day.replace("-", ""),
            "fields": [
                "證券代號",
                "證券名稱",
                "開盤價",
                "最高價",
                "最低價",
                "收盤價",
                "成交股數",
                "成交金額",
            ],
            "data": [["管理股", "測試", "10", "11", "9", "10", "100", "1000"]],
        }
    else:
        roc = int(day[:4]) - 1911
        doc = {
            "stat": "OK",
            "date": f"{roc}/{int(day[5:7])}/{int(day[8:10])}",
            "tables": [
                {
                    "title": "一般",
                    "fields": [
                        "代號",
                        "名稱",
                        "開盤",
                        "最高",
                        "最低",
                        "收盤",
                        "成交股數",
                        "成交金額",
                    ],
                    "data": [["管理股", "測試", "10", "11", "9", "10", "100", "1000"]],
                }
            ],
        }
    return json.dumps(doc, ensure_ascii=False).encode()


def make(tmp_path, transport, now=None, sleep=None):
    clock = [datetime(2024, 1, 5, tzinfo=UTC)]
    if now is None:

        def now():
            return clock[0]

    if sleep is None:

        def sleep(seconds):
            clock[0] += timedelta(seconds=seconds)

    return OfficialWorkflow(tmp_path, transport=transport, now=now, sleep=sleep)


def test_backfill_calendar_all_markets_and_deduplicate_refresh(tmp_path):
    calls = []

    def transport(url, headers, timeout, max_bytes):
        source = "twse" if "twse.com" in url else "tpex"
        value = parse_qs(urlparse(url).query)["date"][0]
        if source == "twse":
            day = f"{value[:4]}-{value[4:6]}-{value[6:8]}"
        else:
            bits = value.split("/")
            day = f"{int(bits[0]):04d}-{int(bits[1]):02d}-{int(bits[2]):02d}"
        calls.append((source, day))
        return 200, response(source, day)

    w = make(tmp_path, transport)
    first = w.backfill("2024-01-06", "2024-01-07")
    assert first["state"] == "completed" and first["counts"]["completed"] == 4, first
    assert len(calls) == 4
    assert w.backfill("2024-01-06", "2024-01-07")["counts"]["completed"] == 0
    assert len(calls) == 4
    assert (
        w.backfill("2024-01-06", "2024-01-06", markets=["twse"], refresh=True)["counts"][
            "completed"
        ]
        == 1
    )
    with SQLiteStore(tmp_path).connect() as db:
        row = db.execute(
            "SELECT params,edition FROM tasks WHERE source='twse' "
            "AND expected_date='2024-01-06' ORDER BY edition DESC LIMIT 1"
        ).fetchone()
        assert json.loads(row["params"])["pipeline"] == "official-v1"
        assert row["edition"] == 2
        assert db.execute("SELECT COUNT(*) FROM records WHERE symbol='管理股'").fetchone()[0] == 5


def test_sync_prioritizes_recent_and_empty_is_not_valid(tmp_path):
    calls = []

    def transport(url, headers, timeout, max_bytes):
        source = "twse" if "twse.com" in url else "tpex"
        calls.append((source, url))
        if "20240105" in url or "/2024/01/05" in url:
            return 200, json.dumps(
                {"stat": "OK", "date": "20240105" if source == "twse" else "113/1/5", "tables": []}
            ).encode()
        day = "2024-01-04"
        return 200, response(source, day)

    w = make(tmp_path, transport)
    w.backfill("2024-01-04", "2024-01-04", markets=["twse"])
    result = w.sync(expected_date="2024-01-05", markets=["twse"], max_tasks=1)
    assert result["state"] == "partial"
    assert result["error"] == "empty_unverified"
    with SQLiteStore(tmp_path).connect() as db:
        snap = db.execute(
            "SELECT validation_state FROM snapshots ORDER BY rowid DESC LIMIT 1"
        ).fetchone()[0]
        assert snap == "empty_unverified"


def test_legacy_tasks_ignored_and_other_lane_untouched(tmp_path):
    w = make(tmp_path, lambda *args: (200, b"{}"))
    with w.store.writer_lock():
        w.store.initialize()
        w.store.insert_row(
            "runs",
            dict(
                id="oldrun",
                kind="official-events:resume",
                started_at="x",
                updated_at="x",
                state="running",
                counts="{}",
            ),
        )
        w.store.insert_row(
            "tasks",
            dict(
                id="oldtask",
                logical_key="old",
                edition=1,
                source="twse",
                dataset="events",
                params='{"pipeline":"legacy-events","lane":"events"}',
                kind="quote",
                state="running",
            ),
        )
    result = w.resume()
    assert result["legacy_tasks_ignored"] == 1
    with w.store.connect() as db:
        assert db.execute("SELECT state FROM tasks WHERE id='oldtask'").fetchone()[0] == "running"
        assert db.execute("SELECT state FROM runs WHERE id='oldrun'").fetchone()[0] == "running"


def test_identity_lane_and_markets_rejected_without_io(tmp_path):
    w = make(tmp_path, lambda *args: pytest.fail("unexpected HTTP"))
    with pytest.raises(ValueError):
        w.resume(lane="identity")
    with pytest.raises(ValueError):
        w.backfill("2024-01-01", "2024-01-01", markets=["bad"])
    assert not (tmp_path / "state.sqlite3").exists()


def test_fixed_throttle_uses_bounded_fake_sleep_across_commands(tmp_path):
    calls = []
    clock = [datetime(2024, 1, 5, tzinfo=UTC)]
    steps = []

    def sleep(seconds):
        steps.append(seconds)
        clock[0] += timedelta(seconds=seconds)

    def transport(url, headers, timeout, max_bytes):
        source = "twse" if "twse.com" in url else "tpex"
        value = parse_qs(urlparse(url).query)["date"][0]
        day = f"{value[:4]}-{value[4:6]}-{value[6:8]}"
        calls.append((source, day))
        return 200, response(source, day)

    w = make(tmp_path, transport, now=lambda: clock[0], sleep=sleep)
    assert w.backfill("2024-01-06", "2024-01-06", markets=["twse"])["state"] == "completed"
    assert w.backfill("2024-01-07", "2024-01-07", markets=["twse"])["state"] == "completed"

    assert calls == [("twse", "2024-01-06"), ("twse", "2024-01-07")]
    assert sum(steps) >= 2
    assert len(steps) >= 10
    assert all(0 < step <= 0.2 for step in steps)


def test_stop_during_throttle_prevents_next_http_request(tmp_path):
    clock = [datetime(2024, 1, 5, tzinfo=UTC)]
    calls = []
    steps = []
    workflow = None

    def sleep(seconds):
        steps.append(seconds)
        clock[0] += timedelta(seconds=seconds)
        workflow._stop.set()

    def transport(url, headers, timeout, max_bytes):
        source = "twse" if "twse.com" in url else "tpex"
        value = parse_qs(urlparse(url).query)["date"][0]
        day = f"{value[:4]}-{value[4:6]}-{value[6:8]}"
        calls.append((source, day))
        return 200, response(source, day)

    workflow = make(tmp_path, transport, now=lambda: clock[0], sleep=sleep)
    assert workflow.backfill("2024-01-06", "2024-01-06", markets=["twse"])["state"] == "completed"
    result = workflow.backfill("2024-01-07", "2024-01-07", markets=["twse"])

    assert result["state"] == "interrupted"
    assert calls == [("twse", "2024-01-06")]
    assert len(steps) == 1
    assert steps[0] <= 0.2


def _run_counts(workflow, run_id):
    with workflow.store.connect() as db:
        rows = db.execute(
            "SELECT state,COUNT(*) AS n FROM tasks WHERE run_id=? GROUP BY state", (run_id,)
        ).fetchall()
    return {row["state"]: row["n"] for row in rows}


@pytest.mark.parametrize(
    ("status", "body", "state"),
    [
        (403, b"denied", "blocked"),
        (200, b"captcha verification", "blocked"),
        (200, b'{"stat":"429"}', "partial"),
    ],
)
def test_blocked_source_is_persisted_before_next_command_without_http(
    tmp_path, status, body, state
):
    calls = []

    def transport(*args):
        calls.append(1)
        return status, body

    workflow = make(tmp_path, transport)
    first = workflow.backfill("2024-01-06", "2024-01-06", markets=["twse"])
    assert first["state"] == state
    assert calls == [1]
    assert len(list((tmp_path / "raw" / "twse").rglob("*.bin"))) == 1
    second = workflow.backfill("2024-01-07", "2024-01-07", markets=["twse"])
    assert second["state"] == "blocked"
    assert calls == [1]
    assert len(list((tmp_path / "raw" / "twse").rglob("*.bin"))) == 1
    assert second["counts"]["pending"] == 1
    assert _run_counts(workflow, second["run_id"]) == {"pending": 1}


def test_transient_retries_three_times_then_fails_without_next_date(tmp_path):
    calls = []

    def transport(*args):
        calls.append(1)
        return 503, b"temporary unavailable"

    workflow = make(tmp_path, transport)
    result = workflow.backfill("2024-01-06", "2024-01-07", markets=["twse"])
    assert result["state"] == "failed"
    assert calls == [1, 1, 1]
    assert len(list((tmp_path / "raw" / "twse").rglob("*.bin"))) == 3
    assert result["counts"]["failed"] == 1
    assert result["counts"]["pending"] == 1
    assert _run_counts(workflow, result["run_id"]) == {"failed": 1, "pending": 1}


@pytest.mark.parametrize(
    ("retry_after", "expected"),
    [
        ("172800", datetime(2024, 1, 7, tzinfo=UTC)),
        ("Sun, 07 Jan 2024 00:00:00 GMT", datetime(2024, 1, 7, tzinfo=UTC)),
        ("9" * 300, datetime.max.replace(tzinfo=UTC)),
    ],
)
def test_retry_after_handles_long_dates_and_huge_values(tmp_path, retry_after, expected):
    from quant_research.taiwan.sources import QuotaError, RawBatch

    workflow = make(tmp_path, lambda *args: pytest.fail("unexpected HTTP"))
    workflow.store.initialize()
    raw = RawBatch(
        "twse", "MI_INDEX", "https://example.invalid", "x", 429, b"x", "x", 0, True, retry_after
    )
    workflow._block("twse", QuotaError("quota", raw), raw)
    with workflow.store.connect() as db:
        block = db.execute(
            "SELECT state,retry_at FROM source_blocks WHERE source='twse'"
        ).fetchone()
    assert block["state"] == "cooldown"
    assert datetime.fromisoformat(block["retry_at"]) == expected


def test_preflight_pending_and_max_tasks_counts_match_database(tmp_path):
    calls = []

    def transport(url, headers, timeout, max_bytes):
        calls.append(url)
        value = parse_qs(urlparse(url).query)["date"][0]
        day = f"{value[:4]}-{value[4:6]}-{value[6:8]}"
        return 200, response("twse", day)

    workflow = make(tmp_path, transport)
    workflow.backfill("2024-01-06", "2024-01-07", markets=["twse"], max_tasks=1)
    paused = workflow.backfill("2024-01-08", "2024-01-09", markets=["twse"], max_tasks=1)
    counts = _run_counts(workflow, paused["run_id"])
    assert paused["counts"]["completed"] == counts.get("completed", 0)
    assert paused["counts"]["pending"] == counts.get("pending", 0)
    assert paused["counts"]["failed"] == counts.get("failed", 0)
    assert counts == {"completed": 1, "pending": 1}


def test_stop_during_wait_returns_current_task_to_pending(tmp_path):
    clock = [datetime(2024, 1, 5, tzinfo=UTC)]
    workflow = None

    def sleep(seconds):
        clock[0] += timedelta(seconds=seconds)
        workflow._stop.set()

    def transport(url, headers, timeout, max_bytes):
        return 200, response("twse", "2024-01-06")

    workflow = make(tmp_path, transport, now=lambda: clock[0], sleep=sleep)
    workflow.backfill("2024-01-06", "2024-01-06", markets=["twse"])
    result = workflow.backfill("2024-01-07", "2024-01-07", markets=["twse"])
    assert result["state"] == "interrupted"
    assert result["counts"]["pending"] == 1
    assert result["counts"]["failed"] == 0
    assert _run_counts(workflow, result["run_id"]) == {"pending": 1}
    with workflow.store.connect() as db:
        saved = json.loads(
            db.execute("SELECT counts FROM runs WHERE id=?", (result["run_id"],)).fetchone()[0]
        )
    assert saved == result["counts"]


def test_writer_lock_is_available_during_transport_and_sleep(tmp_path):
    acquired = []
    clock = [datetime(2024, 1, 5, tzinfo=UTC)]
    workflow = None

    def inspect_lock():
        with workflow.store.writer_lock():
            acquired.append(True)

    def sleep(seconds):
        clock[0] += timedelta(seconds=seconds)
        inspect_lock()

    def transport(url, headers, timeout, max_bytes):
        inspect_lock()
        value = parse_qs(urlparse(url).query)["date"][0]
        day = f"{value[:4]}-{value[4:6]}-{value[6:8]}"
        return 200, response("twse", day)

    workflow = make(tmp_path, transport, now=lambda: clock[0], sleep=sleep)
    first = workflow.backfill("2024-01-06", "2024-01-06", markets=["twse"])
    second = workflow.backfill("2024-01-07", "2024-01-07", markets=["twse"])
    assert first["state"] == second["state"] == "completed"
    assert len(acquired) >= 2


def test_refresh_resume_skips_superseded_edition(tmp_path):
    calls = []

    def transport(url, headers, timeout, max_bytes):
        calls.append(url)
        return 200, response("twse", "2024-01-06")

    workflow = make(tmp_path, transport)
    workflow._plan_dates(["2024-01-06"], ("twse",), refresh=False)
    planned = workflow._plan_dates(["2024-01-06"], ("twse",), refresh=True)
    assert len(planned) == 1
    with workflow.store.connect() as db:
        editions = db.execute("SELECT edition,state FROM tasks ORDER BY edition").fetchall()
    assert [(row["edition"], row["state"]) for row in editions] == [
        (1, "superseded"),
        (2, "pending"),
    ]

    result = workflow.resume()
    assert result["state"] == "completed"
    assert len(calls) == 1
    with workflow.store.connect() as db:
        assert db.execute("SELECT state FROM tasks WHERE edition=1").fetchone()[0] == "superseded"
        assert db.execute("SELECT state FROM tasks WHERE edition=2").fetchone()[0] == "completed"


def test_sigterm_after_valid_transport_preserves_completed_task(tmp_path):
    calls = []
    workflow = None

    def transport(url, headers, timeout, max_bytes):
        value = parse_qs(urlparse(url).query)["date"][0]
        day = f"{value[:4]}-{value[4:6]}-{value[6:8]}"
        calls.append(day)
        if len(calls) == 1:
            signal.raise_signal(signal.SIGTERM)
        return 200, response("twse", day)

    workflow = make(tmp_path, transport)
    result = workflow.backfill("2024-01-06", "2024-01-07", markets=["twse"])
    assert result["state"] == "interrupted"
    assert result["counts"]["completed"] == 1
    assert result["counts"]["pending"] == 1
    with workflow.store.connect() as db:
        rows = db.execute(
            "SELECT expected_date,state,snapshot_id FROM tasks ORDER BY expected_date DESC"
        ).fetchall()
        completed_snapshot = rows[0]["snapshot_id"]
        assert rows[0]["state"] == "completed" and completed_snapshot
        assert rows[1]["state"] == "pending" and rows[1]["snapshot_id"] is None
        assert (
            db.execute("SELECT state FROM runs WHERE id=?", (result["run_id"],)).fetchone()[0]
            == "interrupted"
        )

    resumed = workflow.resume()
    assert resumed["state"] == "completed"
    assert calls == ["2024-01-07", "2024-01-06"]
    with workflow.store.connect() as db:
        row = db.execute(
            "SELECT snapshot_id FROM tasks WHERE expected_date='2024-01-07'"
        ).fetchone()
        assert row[0] == completed_snapshot


def test_subprocess_sigkill_recovers_only_running_task_and_keeps_artifacts(tmp_path):
    script = r"""
import json, sys, threading
from datetime import UTC, datetime
from quant_research.taiwan.official_workflow import OfficialWorkflow

def payload(day):
    doc = {
        "stat": "OK",
        "date": day.replace("-", ""),
        "fields": [
            "證券代號", "證券名稱", "開盤價", "最高價", "最低價", "收盤價", "成交股數", "成交金額"
        ],
        "data": [["管理股", "測試", "10", "11", "9", "10", "100", "1000"]],
    }
    return json.dumps(doc, ensure_ascii=False).encode()

calls = [0]
def transport(url, headers, timeout, max_bytes):
    calls[0] += 1
    value = url.split("date=")[1][:8]
    day = value[:4] + "-" + value[4:6] + "-" + value[6:8]
    if calls[0] == 2:
        print("SECOND_TRANSPORT_BLOCKED", flush=True)
        threading.Event().wait()
    return 200, payload(day)

clock = [datetime(2024,1,5,tzinfo=UTC)]
def sleep(seconds):
    from datetime import timedelta
    clock[0] += timedelta(seconds=seconds)

workflow = OfficialWorkflow(sys.argv[1], transport=transport, now=lambda: clock[0], sleep=sleep)
workflow.backfill("2024-01-06", "2024-01-07", markets=["twse"])
"""
    env = os.environ.copy()
    source_path = os.path.abspath("src")
    env["PYTHONPATH"] = source_path + os.pathsep + env.get("PYTHONPATH", "")
    child = subprocess.Popen(
        [sys.executable, "-c", script, str(tmp_path)],
        cwd=os.path.abspath("."),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )
    try:
        assert child.stdout is not None
        selector = selectors.DefaultSelector()
        selector.register(child.stdout, selectors.EVENT_READ)
        deadline = time.monotonic() + 5
        lines = []
        while time.monotonic() < deadline:
            ready = selector.select(max(0, deadline - time.monotonic()))
            if not ready:
                break
            line = child.stdout.readline()
            lines.append(line)
            if "SECOND_TRANSPORT_BLOCKED" in line:
                break
        else:
            pytest.fail("transport handshake deadline expired")
        assert any("SECOND_TRANSPORT_BLOCKED" in line for line in lines), (
            lines,
            child.poll(),
            child.stderr.read() if child.poll() is not None and child.stderr else "child running",
        )

        store = SQLiteStore(tmp_path)
        with store.connect() as db:
            tasks = db.execute(
                "SELECT expected_date,state,snapshot_id,run_id FROM tasks "
                "ORDER BY expected_date DESC"
            ).fetchall()
            assert [(row["state"], row["snapshot_id"] is not None) for row in tasks] == [
                ("completed", True),
                ("running", False),
            ]
            run_id = tasks[1]["run_id"]
            completed_date = tasks[0]["expected_date"]
            snapshot_id = tasks[0]["snapshot_id"]
            snapshot = dict(
                db.execute(
                    "SELECT id,sha256,raw_path FROM snapshots WHERE id=?", (snapshot_id,)
                ).fetchone()
            )
        raw_path = tmp_path / snapshot["raw_path"]
        raw_before = raw_path.read_bytes()
        os.kill(child.pid, signal.SIGKILL)
        assert child.wait(timeout=5) == -signal.SIGKILL

        calls = []

        def resume_transport(url, headers, timeout, max_bytes):
            value = parse_qs(urlparse(url).query)["date"][0]
            day = f"{value[:4]}-{value[4:6]}-{value[6:8]}"
            calls.append(day)
            return 200, response("twse", day)

        workflow = make(tmp_path, resume_transport)
        resumed = workflow.resume()
        assert resumed["state"] == "completed"
        assert calls == [tasks[1]["expected_date"]]
        assert completed_date not in calls
        assert raw_path.read_bytes() == raw_before
        with workflow.store.connect() as db:
            assert (
                db.execute("SELECT state FROM runs WHERE id=?", (run_id,)).fetchone()[0]
                == "interrupted"
            )
            saved = db.execute(
                "SELECT state,snapshot_id FROM tasks WHERE expected_date=?", (completed_date,)
            ).fetchone()
            assert saved["state"] == "completed" and saved["snapshot_id"] == snapshot_id
            assert (
                db.execute(
                    "SELECT sha256,raw_path FROM snapshots WHERE id=?", (snapshot_id,)
                ).fetchone()["sha256"]
                == snapshot["sha256"]
            )
            assert (
                db.execute("SELECT COUNT(*) FROM tasks WHERE state='completed'").fetchone()[0] == 2
            )
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=5)
        if child.stdout is not None:
            child.stdout.close()
        if child.stderr is not None:
            child.stderr.close()
