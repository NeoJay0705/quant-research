from __future__ import annotations

import hashlib
import json
import multiprocessing
import os
import signal
import sys
import time
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

from quant_research.taiwan import official
from quant_research.taiwan.official_workflow import OfficialWorkflow

AS_OF = datetime(2026, 1, 3, tzinfo=UTC)


def _clock():
    now = [AS_OF]

    def sleep(seconds: float) -> None:
        now[0] += timedelta(seconds=seconds)

    return now, sleep


def _response(dataset: str, params: dict[str, list[str]]) -> tuple[int, bytes]:
    start = params.get("startDate", ["2026/01/01"])[0].replace("/", "-")
    end = params.get("endDate", ["2026/01/31"])[0].replace("/", "-")
    if dataset == "TWT49U":
        body = {
            "stat": "OK",
            "strDate": start.replace("-", ""),
            "endDate": end.replace("-", ""),
            "fields": ["資料日期", "股票代號", "詳細資料", "現金股利"],
            "data": [["2026-01-02", "2330", "2330,20260102", "2"]],
        }
    elif dataset == "TWT49UDetail":
        body = {"stat": "OK", "fields": ["股票代號", "現金股利"], "data": [["2330", "2"]]}
    else:
        body = {
            "stat": "OK",
            "strDate": start,
            "endDate": end,
            "fields": [
                "恢復買賣日期",
                "股票代號",
                "停止買賣前收盤價格",
                "恢復買賣參考價",
                "詳細資料",
            ],
            "data": [],
        }
    return 200, json.dumps(body, ensure_ascii=False).encode()


def _killed_maintenance(root: str, marker: str, request_log: str) -> None:
    now, sleep = _clock()

    def transport(url, headers, timeout, max_bytes):
        parsed = urlparse(url)
        dataset = parsed.path.rsplit("/", 1)[-1]
        params = parse_qs(parsed.query)
        with open(request_log, "a", encoding="utf-8") as stream:
            stream.write(dataset + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        if dataset == "TWT49UDetail":
            Path(marker).write_text("detail transport entered\n", encoding="utf-8")
            while True:
                time.sleep(0.05)
        return _response(dataset, params)

    real_request = official.request

    def request(*args, **kwargs):
        return replace(real_request(*args, **kwargs), fetched_at=now[0].isoformat())

    official.request = request
    workflow = OfficialWorkflow(root, transport=transport, now=lambda: now[0], sleep=sleep)
    while True:
        workflow.maintain_events(
            expected_date="2026-01-03",
            max_tasks=3,
            markets=["twse"],
            recent_months=1,
            detail_refresh_days=7,
        )


def _insert_pending(
    db, *, task_id: str, logical_key: str, source: str, dataset: str, kind: str, params: dict
):
    db.execute(
        "INSERT INTO tasks(id,logical_key,edition,source,dataset,params,kind,state,expected_date) "
        "VALUES(?,?,1,?,?,?,?,'pending',?)",
        (
            task_id,
            logical_key,
            source,
            dataset,
            json.dumps(params, sort_keys=True),
            kind,
            "2026-06-30",
        ),
    )


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="requires Linux SIGKILL")
def test_maintenance_recovers_real_sigkill_during_detail_fetch(tmp_path, monkeypatch):
    root = tmp_path / "synthetic-root"
    marker = tmp_path / "detail-entered.marker"
    request_log = tmp_path / "requests.log"
    workflow = OfficialWorkflow(root)
    workflow.store.initialize()
    with workflow.store.connect() as db:
        _insert_pending(
            db,
            task_id="unrelated-quote",
            logical_key="unrelated-quote-key",
            source="twse",
            dataset="MI_INDEX",
            kind="official_quote",
            params={
                "pipeline": "official-v1",
                "namespace": "official-v1",
                "lane": "quotes",
                "request": {"date": "2026-06-30"},
            },
        )
        _insert_pending(
            db,
            task_id="future-manual-event",
            logical_key="future-manual-event-key",
            source="twse",
            dataset="TWT49U",
            kind="official_event_summary",
            params={
                "pipeline": "official-v1",
                "lane": "events",
                "request": {"start_date": "2026-06-01", "end_date": "2026-06-30"},
            },
        )
        _insert_pending(
            db,
            task_id="unrelated-finmind",
            logical_key="unrelated-finmind-key",
            source="finmind",
            dataset="TaiwanStockDividend",
            kind="finmind:sync",
            params={"request": {"start_date": "2026-06-01", "end_date": "2026-06-30"}},
        )
        unrelated_before = {
            row["id"]: dict(row)
            for row in db.execute(
                "SELECT id,logical_key,edition,source,dataset,params,kind,state,expected_date,"
                "run_id,snapshot_id FROM tasks WHERE id IN ("
                "'unrelated-quote','future-manual-event','unrelated-finmind')"
            )
        }

    context = multiprocessing.get_context("fork")
    process = context.Process(
        target=_killed_maintenance,
        args=(str(root), str(marker), str(request_log)),
    )
    process.start()
    try:
        deadline = time.monotonic() + 10
        while not marker.exists() and time.monotonic() < deadline:
            if not process.is_alive():
                pytest.fail(
                    f"maintenance worker exited before detail callback; exit={process.exitcode}"
                )
            time.sleep(0.02)
        assert marker.exists(), "detail transport was not entered within the 10-second deadline"
    finally:
        if process.is_alive():
            os.kill(process.pid, signal.SIGKILL)
        process.join(timeout=5)
        if process.is_alive():
            process.kill()
            process.join(timeout=5)
    assert process.exitcode == -signal.SIGKILL

    lines_before = request_log.read_text(encoding="utf-8").splitlines()
    assert lines_before.count("TWT49U") == 1
    assert "TWT49UDetail" in lines_before
    with workflow.store.connect() as db:
        parent_before = dict(
            db.execute(
                "SELECT t.id,t.edition,t.run_id,t.snapshot_id,t.state,s.raw_path,s.sha256 "
                "FROM tasks t JOIN snapshots s ON s.id=t.snapshot_id "
                "WHERE t.dataset='TWT49U' AND t.kind='official_event_summary'"
            ).fetchone()
        )
        child_before = dict(
            db.execute(
                "SELECT id,edition,run_id,snapshot_id,state FROM tasks WHERE dataset='TWT49UDetail'"
            ).fetchone()
        )
        link_before = dict(
            db.execute(
                "SELECT parent_task_id,parent_snapshot_id,child_task_id,context "
                "FROM official_event_links WHERE child_task_id=?",
                (child_before["id"],),
            ).fetchone()
        )
        assert parent_before["state"] == "completed" and parent_before["snapshot_id"]
        assert child_before["state"] == "running" and child_before["snapshot_id"] is None
        assert (root / parent_before["raw_path"]).is_file()
        assert (
            hashlib.sha256((root / parent_before["raw_path"]).read_bytes()).hexdigest()
            == parent_before["sha256"]
        )

    now, sleep = _clock()
    calls: list[str] = []

    def transport(url, headers, timeout, max_bytes):
        parsed = urlparse(url)
        dataset = parsed.path.rsplit("/", 1)[-1]
        params = parse_qs(parsed.query)
        calls.append(dataset)
        with request_log.open("a", encoding="utf-8") as stream:
            stream.write(dataset + "\n")
        return _response(dataset, params)

    real_request = official.request

    def request(*args, **kwargs):
        return replace(real_request(*args, **kwargs), fetched_at=now[0].isoformat())

    monkeypatch.setattr(official, "request", request)
    resumed = OfficialWorkflow(root, transport=transport, now=lambda: now[0], sleep=sleep)
    result = resumed.maintain_events(
        expected_date="2026-01-03",
        markets=["twse"],
        recent_months=1,
        detail_refresh_days=7,
    )
    assert result["cycle_as_of"] == "2026-01-03"
    assert result["active"] is False and result["state"] == "partial"
    assert calls == ["TWT49UDetail"]
    with resumed.store.connect() as db:
        parent_after = dict(
            db.execute(
                "SELECT t.id,t.edition,t.run_id,t.snapshot_id,t.state,s.raw_path,s.sha256 "
                "FROM tasks t JOIN snapshots s ON s.id=t.snapshot_id "
                "WHERE t.dataset='TWT49U' AND t.kind='official_event_summary'"
            ).fetchone()
        )
        child_after = dict(
            db.execute(
                "SELECT id,edition,run_id,snapshot_id,state FROM tasks WHERE dataset='TWT49UDetail'"
            ).fetchone()
        )
        event = db.execute(
            "SELECT payload FROM records WHERE snapshot_id=? AND kind='event'",
            (child_after["snapshot_id"],),
        ).fetchone()
        link_after = dict(
            db.execute(
                "SELECT parent_task_id,parent_snapshot_id,child_task_id,context "
                "FROM official_event_links WHERE child_task_id=?",
                (child_after["id"],),
            ).fetchone()
        )
        unrelated_after = {
            row["id"]: dict(row)
            for row in db.execute(
                "SELECT id,logical_key,edition,source,dataset,params,kind,state,expected_date,"
                "run_id,snapshot_id FROM tasks WHERE id IN ("
                "'unrelated-quote','future-manual-event','unrelated-finmind')"
            )
        }
    assert parent_after == parent_before
    assert child_after["id"] == child_before["id"]
    assert child_after["edition"] == child_before["edition"]
    assert child_after["state"] == "completed" and child_after["snapshot_id"]
    assert link_after == link_before
    assert event is not None
    assert json.loads(event["payload"])["cash_dividend_per_share"] == "2"
    assert unrelated_after == unrelated_before
    assert (root / parent_after["raw_path"]).is_file()
    assert (
        hashlib.sha256((root / parent_after["raw_path"]).read_bytes()).hexdigest()
        == parent_after["sha256"]
    )

    requests_before_noop = request_log.read_text(encoding="utf-8").splitlines()
    noop = resumed.maintain_events(
        expected_date="2026-01-03",
        markets=["twse"],
        recent_months=1,
        detail_refresh_days=7,
    )
    assert noop["noop"] is True
    assert request_log.read_text(encoding="utf-8").splitlines() == requests_before_noop
    assert calls == ["TWT49UDetail"]
