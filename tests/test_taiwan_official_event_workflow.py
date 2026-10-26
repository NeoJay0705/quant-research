from __future__ import annotations

import hashlib
import json
import signal
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlparse

import pytest

from quant_research.taiwan.official_workflow import OfficialWorkflow


def workflow(tmp_path, callback=None):
    clock = [datetime(2026, 10, 23, tzinfo=UTC)]
    calls = []

    def sleep(seconds):
        clock[0] += timedelta(seconds=seconds)

    def transport(url, headers, timeout, max_bytes):
        parsed = urlparse(url)
        dataset = parsed.path.rsplit("/", 1)[-1]
        calls.append(url)
        params = parse_qs(parsed.query)
        body = callback(dataset, params) if callback else None
        if body is not None:
            return 200, body
        start = params.get("startDate", ["2026-10-01"])[0].replace("/", "-")
        end = params.get("endDate", ["2026-10-31"])[0].replace("/", "-")
        if dataset == "TWT49UDetail":
            payload = {"stat": "OK", "fields": ["股票代號", "現金股利"], "data": [["2330", "1"]]}
        elif dataset == "TWT49U":
            payload = {
                "stat": "OK",
                "strDate": start.replace("-", ""),
                "endDate": end.replace("-", ""),
                "fields": ["資料日期", "股票代號", "詳細資料"],
                "data": [[start, "2330", f"2330,{start.replace('-', '')}"]],
            }
        else:
            payload = {
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
        return 200, json.dumps(payload, ensure_ascii=False).encode()

    return OfficialWorkflow(tmp_path, transport=transport, now=lambda: clock[0], sleep=sleep), calls


def test_month_intersections_cross_year_and_invalid_dates_no_io(tmp_path):
    w, calls = workflow(tmp_path)
    with pytest.raises(ValueError):
        w.sync_events("2026-02-30", markets=["twse"])
    with pytest.raises(ValueError):
        w.sync_events("2026-02-01", "2026-01-31", markets=["twse"])
    assert calls == [] and not (tmp_path / "state.sqlite3").exists()
    w.sync_events("2025-12-20", "2026-01-03", markets=["twse"], max_tasks=1)
    with w.store.connect() as db:
        ranges = [
            json.loads(r[0])["request"]
            for r in db.execute(
                "SELECT params FROM tasks WHERE kind='official_event_summary' "
                "ORDER BY expected_date DESC"
            )
        ]
    assert {(r["start_date"], r["end_date"]) for r in ranges} == {
        ("2025-12-20", "2025-12-31"),
        ("2026-01-01", "2026-01-03"),
    }


def test_dynamic_detail_atomic_max_and_resume_does_not_refetch_parent(tmp_path):
    w, calls = workflow(tmp_path)
    result = w.sync_events("2026-10-10", "2026-10-10", markets=["twse"], max_tasks=1)
    assert result["state"] == "paused"
    with w.store.connect() as db:
        assert (
            db.execute("SELECT state FROM tasks WHERE dataset='TWT49U'").fetchone()[0]
            == "completed"
        )
        assert db.execute("SELECT COUNT(*) FROM official_event_links").fetchone()[0] == 1
        assert (
            db.execute(
                "SELECT COUNT(*) FROM tasks WHERE dataset='TWT49UDetail' AND state='pending'"
            ).fetchone()[0]
            == 1
        )
    summary_calls = [u for u in calls if "TWT49U?" in u]
    resumed = w.resume(lane="events")
    assert resumed["counts"]["completed"] >= 1
    assert len([u for u in calls if "TWT49U?" in u]) == len(summary_calls)
    assert any("TWT49UDetail" in u for u in calls)


def test_refresh_creates_new_summary_and_child_edition_links_keep_history(tmp_path):
    w, calls = workflow(tmp_path)
    w.sync_events("2026-10-10", "2026-10-10", markets=["twse"])
    w.sync_events("2026-10-10", "2026-10-10", markets=["twse"], refresh=True)
    with w.store.connect() as db:
        rows = db.execute(
            "SELECT dataset,edition,state FROM tasks "
            "WHERE dataset IN ('TWT49U','TWT49UDetail') ORDER BY dataset,edition"
        ).fetchall()
        links = db.execute("SELECT COUNT(*) FROM official_event_links").fetchone()[0]
    assert [(r["dataset"], r["edition"]) for r in rows] == [
        ("TWT49U", 1),
        ("TWT49U", 2),
        ("TWT49UDetail", 1),
        ("TWT49UDetail", 2),
    ]
    assert links == 2
    assert len([u for u in calls if "TWT49UDetail" in u]) == 2


def event_callback(dataset, params):
    if dataset == "TWT49UDetail":
        return json.dumps(
            {
                "stat": "OK",
                "fields": ["股票代號", "(每股配發現金股利)除息"],
                "data": [[params["STK_NO"][0], "8 元/股"]],
            }
        ).encode()
    if dataset == "TWTAVUDetail":
        return json.dumps(
            {
                "stat": "OK",
                "fields": ["股票代號", "每壹仟股換發新股票", "變更前面額", "變更後面額"],
                "data": [[params["STK_NO"][0], "950 股", "10 元/股", "5 元/股"]],
            }
        ).encode()
    start = params["startDate"][0].replace("/", "-")
    end = params["endDate"][0].replace("/", "-")
    if dataset == "TWT49U":
        return json.dumps(
            {
                "stat": "OK",
                "strDate": start,
                "endDate": end,
                "fields": ["資料日期", "股票代號", "詳細資料"],
                "data": [["2022-10-11", "2330", "2330,20221011"]],
            }
        ).encode()
    if dataset == "TWTAUU":
        return json.dumps(
            {
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
                "data": [["2022-10-11", "2330", "10", "12", "2330,20221010"]],
            }
        ).encode()
    if dataset == "TWTB8U":
        return json.dumps(
            {
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
                "data": [["2022-10-11", "2330", "10", "12", "2330,20221001,20221011"]],
            }
        ).encode()
    roc_start, roc_end = f"111/{start[5:7]}/{start[8:10]}", f"111/{end[5:7]}/{end[8:10]}"
    if dataset == "exDailyQ":
        fields, row = (
            ["除權息日期", "代號", "除權息前收盤價", "除權息參考價", "現金股利", "每仟股無償配股"],
            ["111/10/11", "6488", "100", "92", "8", "125"],
        )
    elif dataset == "revivt":
        fields, row = (
            ["恢復買賣日期", "股票代號", "最後交易日之收盤價格", "減資恢復買賣開始日參考價格"],
            ["111/10/11", "6488", "10", "12"],
        )
    else:
        fields, row = (
            ["恢復買賣日期", "證券代號", "最後交易日之收盤價格", "恢復買賣開始參考價"],
            ["111/10/11", "6488", "10", "12"],
        )
    return json.dumps(
        {"stat": "ok", "date": f"{roc_start}~{roc_end}", "fields": fields, "data": [row]}
    ).encode()


def test_duplicate_event_rows_and_refresh_reuse_one_child_per_run(tmp_path):
    def duplicate(dataset, params):
        payload = event_callback(dataset, params)
        if dataset != "TWT49U":
            return payload
        doc = json.loads(payload)
        doc["data"].append(doc["data"][0])
        return json.dumps(doc).encode()

    w, calls = workflow(tmp_path, duplicate)
    w.sync_events("2022-10-01", "2022-10-31", markets=["twse"])
    w.sync_events("2022-10-01", "2022-10-31", markets=["twse"], refresh=True)
    with w.store.connect() as db:
        children = db.execute(
            "SELECT edition,state FROM tasks WHERE dataset='TWT49UDetail' ORDER BY edition"
        ).fetchall()
        superseded = db.execute(
            "SELECT COUNT(*) FROM tasks WHERE dataset='TWT49UDetail' AND state='superseded'"
        ).fetchone()[0]
    assert [(row["edition"], row["state"]) for row in children] == [
        (1, "completed"),
        (2, "completed"),
    ]
    assert superseded == 0
    assert len([url for url in calls if "TWT49UDetail" in url]) == 2


def test_overlapping_parent_ranges_share_completed_child_and_keep_context(tmp_path):
    w, calls = workflow(tmp_path, event_callback)
    w.sync_events("2022-10-01", "2022-10-20", markets=["twse"])
    w.sync_events("2022-10-05", "2022-10-31", markets=["twse"])
    with w.store.connect() as db:
        n = db.execute("SELECT COUNT(*) FROM tasks WHERE dataset='TWT49UDetail'").fetchone()[0]
        links = db.execute(
            "SELECT l.context,s.params FROM official_event_links l "
            "JOIN snapshots s ON s.id=l.parent_snapshot_id WHERE s.dataset='TWT49U'"
        ).fetchall()
    assert n == 1 and len(links) == 2
    assert len([u for u in calls if "TWT49UDetail" in u]) == 1
    assert {json.loads(r[1])["request"]["start_date"] for r in links} == {
        "2022-10-01",
        "2022-10-05",
    }
    assert all(json.loads(r[0])["summary_dataset"] == "TWT49U" for r in links)
    assert all("context" not in u for u in calls)


def test_overlapping_sync_claims_pending_child_and_completed_parent(tmp_path):
    w, calls = workflow(tmp_path, event_callback)
    assert (
        w.sync_events("2022-10-01", "2022-10-20", markets=["twse"], max_tasks=1)["state"]
        == "paused"
    )
    w.sync_events("2022-10-05", "2022-10-31", markets=["twse"])
    with w.store.connect() as db:
        child = db.execute("SELECT state,error FROM tasks WHERE dataset='TWT49UDetail'").fetchone()
        parents = db.execute(
            "SELECT COUNT(*) FROM tasks WHERE dataset='TWT49U' AND state='completed'"
        ).fetchone()[0]
    assert child["state"] == "completed" and child["error"] is None and parents == 2
    assert len([u for u in calls if "TWT49U?" in u]) == 2
    assert len([u for u in calls if "TWT49UDetail" in u]) == 1


def test_same_window_sync_replans_pending_summaries_and_children(tmp_path):
    w, calls = workflow(tmp_path, event_callback)
    assert (
        w.sync_events("2022-10-01", "2022-10-31", markets=["twse"], max_tasks=1)["state"]
        == "paused"
    )
    w.sync_events("2022-10-01", "2022-10-31", markets=["twse"])
    with w.store.connect() as db:
        pending = db.execute(
            "SELECT COUNT(*) FROM tasks WHERE state IN ('pending','failed') "
            "AND source='twse' AND json_extract(params,'$.lane')='events'"
        ).fetchone()[0]
        parents = db.execute(
            "SELECT COUNT(*) FROM tasks WHERE dataset='TWT49U' AND state='completed'"
        ).fetchone()[0]
    assert pending == 0 and parents == 1
    assert len([u for u in calls if "TWT49U?" in u]) == 1
    assert len([u for u in calls if "TWTAUU?" in u]) == 1
    assert len([u for u in calls if "TWTB8U?" in u]) == 1


def test_legacy_lanes_survive_events_recovery_and_edition_guard_skips_old_queue(tmp_path):
    w, calls = workflow(tmp_path)
    w.store.initialize()
    params = json.dumps(
        {
            "pipeline": "official-v1",
            "lane": "quotes",
            "request": {"date": "2026-10-23"},
            "context": {},
        }
    )
    with w.store.connect() as db:
        db.execute(
            "INSERT INTO runs(id,kind,started_at,updated_at,state,counts) "
            "VALUES('quote-run','official-quotes:sync','x','x','running','{}')"
        )
        db.execute(
            "INSERT INTO tasks(id,logical_key,edition,source,dataset,params,kind,state,"
            "expected_date,run_id) VALUES('quote-task','quote-key',1,'twse','MI_INDEX',?,"
            "'official_quote','running','2026-10-23','quote-run')",
            (params,),
        )
        db.execute(
            "INSERT INTO tasks(id,logical_key,edition,source,dataset,params,kind,state) "
            "VALUES('legacy-task','legacy-key',1,'finmind','x','{}','legacy','running')"
        )
        old = json.dumps(
            {
                "pipeline": "official-v1",
                "lane": "events",
                "request": {"start_date": "2022-10-01", "end_date": "2022-10-31"},
                "context": {},
            }
        )
        db.execute(
            "INSERT INTO tasks(id,logical_key,edition,source,dataset,params,kind,state,"
            "expected_date) VALUES('old-event','logical',1,'twse','TWT49U',?,"
            "'official_event_summary','pending','2022-10-01')",
            (old,),
        )
        db.execute(
            "INSERT INTO tasks(id,logical_key,edition,source,dataset,params,kind,state,"
            "expected_date) VALUES('new-event','logical',2,'twse','TWT49U',?,"
            "'official_event_summary','superseded','2022-10-01')",
            (old,),
        )
    result = w.resume(lane="events")
    assert result["legacy_tasks_ignored"] == 1
    with w.store.connect() as db:
        assert (
            db.execute("SELECT state FROM tasks WHERE id='quote-task'").fetchone()[0] == "running"
        )
        assert db.execute("SELECT state FROM runs WHERE id='quote-run'").fetchone()[0] == "running"
        assert db.execute("SELECT state FROM tasks WHERE id='old-event'").fetchone()[0] == "pending"
    assert calls == []
    stale = {
        "id": "old-event",
        "logical_key": "logical",
        "run_id": None,
        "params": old,
        "source": "twse",
        "dataset": "TWT49U",
        "expected_date": "2022-10-01",
    }
    result = w._execute("events:resume", [stale], None)
    assert result["counts"]["claimed"] == 1
    with w.store.connect() as db:
        guarded = db.execute("SELECT state,attempts FROM tasks WHERE id='old-event'").fetchone()
    assert guarded["state"] == "pending" and guarded["attempts"] == 0
    assert calls == []


def test_sigkill_after_parent_commit_resume_keeps_child_without_refetch(tmp_path):
    script = r"""
import json, os, signal, sys
from datetime import UTC, datetime
from urllib.parse import parse_qs, urlparse
from quant_research.taiwan.official_workflow import OfficialWorkflow

root = sys.argv[1]
def transport(url, headers, timeout, max_bytes):
    parsed = urlparse(url)
    dataset = parsed.path.rsplit("/", 1)[-1]
    if dataset == "TWT49UDetail":
        payload = {"stat":"OK", "fields":["股票代號","現金股利"], "data":[["2330","1"]]}
    else:
        payload = {"stat":"OK", "strDate":"20261010", "endDate":"20261010",
                   "fields":["資料日期","股票代號","詳細資料"],
                   "data":[["2026-10-10","2330","2330,20261010"]]}
    return 200, json.dumps(payload, ensure_ascii=False).encode()
w = OfficialWorkflow(root, transport=transport)
commit = w._commit_event_batch
def crash_after_commit(*args, **kwargs):
    result = commit(*args, **kwargs)
    os.kill(os.getpid(), signal.SIGKILL)
w._commit_event_batch = crash_after_commit
w.sync_events("2026-10-10", "2026-10-10", markets=["twse"], max_tasks=1)
"""
    child = subprocess.run([sys.executable, "-c", script, str(tmp_path)], timeout=10, check=False)
    assert child.returncode == -signal.SIGKILL
    with workflow(tmp_path)[0].store.connect() as db:
        parent = db.execute("SELECT state,snapshot_id FROM tasks WHERE dataset='TWT49U'").fetchone()
        child_state = db.execute("SELECT state FROM tasks WHERE dataset='TWT49UDetail'").fetchone()[
            0
        ]
        parent_snapshot = db.execute(
            "SELECT * FROM snapshots WHERE id=?", (parent["snapshot_id"],)
        ).fetchone()
        original = dict(parent_snapshot)
        original_records = [
            tuple(r)
            for r in db.execute(
                "SELECT kind,symbol,record_date,record_key,payload "
                "FROM records WHERE snapshot_id=?",
                (parent["snapshot_id"],),
            )
        ]
    assert parent["state"] == "completed" and child_state == "pending"
    assert original_records
    raw_path = tmp_path / str(original["raw_path"])
    raw_bytes = raw_path.read_bytes()
    raw_hash = hashlib.sha256(raw_bytes).hexdigest()
    resumed, calls = workflow(tmp_path)
    result = resumed.resume(lane="events", max_tasks=1)
    assert result["counts"]["completed"] == 1
    assert len(calls) == 1 and "TWT49UDetail" in calls[0]
    with resumed.store.connect() as db:
        current = db.execute(
            "SELECT * FROM snapshots WHERE id=?", (parent["snapshot_id"],)
        ).fetchone()
        current_records = [
            tuple(r)
            for r in db.execute(
                "SELECT kind,symbol,record_date,record_key,payload "
                "FROM records WHERE snapshot_id=?",
                (parent["snapshot_id"],),
            )
        ]
        assert db.execute("SELECT state FROM runs WHERE state='interrupted'").fetchone()
    assert dict(current) == original and current_records == original_records
    assert raw_path.is_file()
    assert raw_path.read_bytes() == raw_bytes
    assert hashlib.sha256(raw_path.read_bytes()).hexdigest() == raw_hash


def test_context_never_sent_and_detail_event_date_remains_null(tmp_path):
    w, calls = workflow(tmp_path)
    w.sync_events("2026-10-10", "2026-10-10", markets=["twse"])
    detail_urls = [url for url in calls if "TWT49UDetail" in url]
    assert detail_urls
    assert all(
        not {"context", "event_date", "summary_token"} & set(parse_qs(urlparse(url).query))
        for url in detail_urls
    )
    with w.store.connect() as db:
        details = db.execute(
            "SELECT r.record_date,r.payload,l.context FROM records r "
            "JOIN snapshots s ON s.id=r.snapshot_id JOIN official_event_links l "
            "ON l.child_task_id=(SELECT id FROM tasks WHERE snapshot_id=s.id) "
            "WHERE s.dataset='TWT49UDetail'"
        ).fetchall()
    assert details and all(row[0] is None for row in details)
    assert all(
        all(json.loads(row[1])[key] is None for key in ("event_date", "date", "record_date"))
        for row in details
    )
    assert all(json.loads(row[2])["summary_dataset"] == "TWT49U" for row in details)
    assert all(
        json.loads(row[2])["event_date"] == "2026-10-10" and json.loads(row[2])["symbol"] == "2330"
        for row in details
    )


def test_period_warning_and_empty_event_are_partial(tmp_path):
    def malformed(dataset, params):
        if dataset == "TWT49U":
            return json.dumps(
                {
                    "stat": "OK",
                    "fields": ["資料日期", "股票代號", "詳細資料"],
                    "data": [["2026-10-10", "2330", "2330,20261010"]],
                },
                ensure_ascii=False,
            ).encode()
        if dataset == "TWT49UDetail":
            return None
        start = params["startDate"][0].replace("/", "-")
        end = params["endDate"][0].replace("/", "-")
        return json.dumps(
            {
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
            },
            ensure_ascii=False,
        ).encode()

    w, _ = workflow(tmp_path, malformed)
    result = w.sync_events("2026-10-10", "2026-10-10", markets=["twse"], max_tasks=3)
    assert result["state"] == "partial"
    assert result["counts"]["unverified_period"] >= 1
    assert result["counts"]["empty_unverified"] >= 1
    with w.store.connect() as db:
        warnings = db.execute(
            "SELECT validation_state,error FROM snapshots WHERE dataset='TWT49U'"
        ).fetchone()
        assert warnings["validation_state"] == "period_envelope_unverified"
        assert warnings["error"]
        assert (
            db.execute(
                "SELECT COUNT(*) FROM records r JOIN snapshots s ON s.id=r.snapshot_id "
                "WHERE s.dataset='TWT49U'"
            ).fetchone()[0]
            == 1
        )


def test_event_transaction_rolls_back_parent_children_and_links_then_resumes(tmp_path):
    w, calls = workflow(tmp_path)
    w.store.initialize()
    with w.store.connect() as db:
        db.execute(
            "CREATE TRIGGER reject_parent_complete BEFORE UPDATE OF state ON tasks "
            "WHEN OLD.dataset='TWT49U' AND NEW.state='completed' "
            "BEGIN SELECT RAISE(ABORT,'injected commit failure'); END"
        )
    result = w.sync_events("2026-10-10", "2026-10-10", markets=["twse"], max_tasks=1)
    assert result["state"] == "failed"
    with w.store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM records").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM official_event_links").fetchone()[0] == 0
        assert (
            db.execute("SELECT COUNT(*) FROM tasks WHERE dataset='TWT49UDetail'").fetchone()[0] == 0
        )
        assert (
            db.execute("SELECT state FROM tasks WHERE dataset='TWT49U'").fetchone()[0] == "failed"
        )
        db.execute("DROP TRIGGER reject_parent_complete")
    raw_files = sorted((tmp_path / "raw").rglob("*.bin"))
    assert raw_files
    raw_before = {
        path: (path.read_bytes(), hashlib.sha256(path.read_bytes()).hexdigest())
        for path in raw_files
    }
    resumed = w.resume(lane="events", max_tasks=1)
    assert resumed["counts"]["completed"] == 1
    for path, (contents, digest) in raw_before.items():
        assert path.is_file()
        assert path.read_bytes() == contents
        assert hashlib.sha256(path.read_bytes()).hexdigest() == digest


@pytest.mark.parametrize(
    ("status", "body", "event_state"),
    [
        (403, b"denied", "blocked"),
        (200, b"captcha verification", "blocked"),
        (200, b'{"stat":"429"}', "partial"),
    ],
)
def test_event_source_rejection_blocks_quotes_lane_without_refetch(
    tmp_path, status, body, event_state
):
    calls = []
    clock = [datetime(2026, 10, 23, tzinfo=UTC)]

    def sleep(seconds):
        clock[0] += timedelta(seconds=seconds)

    def transport(*args):
        calls.append(args[0])
        return status, body

    w = OfficialWorkflow(tmp_path, transport=transport, now=lambda: clock[0], sleep=sleep)
    event = w.sync_events("2026-10-10", "2026-10-10", markets=["twse"], max_tasks=1)
    assert event["state"] == event_state
    assert len(calls) == 1
    assert len(list((tmp_path / "raw" / "twse").rglob("*.bin"))) == 1
    quotes = w.backfill("2026-10-10", "2026-10-10", markets=["twse"])
    assert quotes["state"] == "blocked"
    assert quotes["counts"]["pending"] == 1
    assert calls == [calls[0]]
    assert len(list((tmp_path / "raw" / "twse").rglob("*.bin"))) == 1
    with w.store.connect() as db:
        assert (
            db.execute("SELECT COUNT(*) FROM source_blocks WHERE source='twse'").fetchone()[0] == 1
        )
        assert (
            db.execute(
                "SELECT COUNT(*) FROM tasks WHERE state='pending' "
                "AND json_extract(params,'$.lane')='quotes'"
            ).fetchone()[0]
            == 1
        )


def test_exact_empty_detail_keeps_parent_event_link_and_rerun_uses_no_http(tmp_path):
    calls = []
    clock = [datetime(2026, 10, 23, tzinfo=UTC)]

    def sleep(seconds):
        clock[0] += timedelta(seconds=seconds)

    def transport(url, headers, timeout, max_bytes):
        calls.append(url)
        parsed = urlparse(url)
        dataset = parsed.path.rsplit("/", 1)[-1]
        query = parse_qs(parsed.query)
        if dataset == "TWT49UDetail":
            return 200, '{"stat":"查無相關資料"}'.encode()
        start = query["startDate"][0]
        end = query["endDate"][0]
        payload = {
            "stat": "OK",
            "strDate": start,
            "endDate": end,
            "fields": ["資料日期", "股票代號", "詳細資料"]
            if dataset == "TWT49U"
            else ["恢復買賣日期", "股票代號", "停止買賣前收盤價格", "恢復買賣參考價", "詳細資料"],
            "data": [["2019-07-09", "2887F", "2887F,20190709"]] if dataset == "TWT49U" else [],
        }
        return 200, json.dumps(payload, ensure_ascii=False).encode()

    w = OfficialWorkflow(tmp_path, transport=transport, now=lambda: clock[0], sleep=sleep)
    first = w.sync_events("2019-07-09", "2019-07-09", markets=["twse"])
    assert first["state"] == "partial"
    assert first["counts"]["empty_unverified"] == 3
    assert len(calls) == 4
    first_call_count = len(calls)
    with w.store.connect() as db:
        parent = db.execute("SELECT state,snapshot_id FROM tasks WHERE dataset='TWT49U'").fetchone()
        child = db.execute(
            "SELECT state,snapshot_id FROM tasks WHERE dataset='TWT49UDetail'"
        ).fetchone()
        parent_event = db.execute(
            "SELECT r.symbol,r.record_date FROM records r WHERE r.snapshot_id=?",
            (parent["snapshot_id"],),
        ).fetchone()
        child_snapshot = db.execute(
            "SELECT validation_state,error FROM snapshots WHERE id=?",
            (child["snapshot_id"],),
        ).fetchone()
        child_records = db.execute(
            "SELECT COUNT(*) FROM records WHERE snapshot_id=?", (child["snapshot_id"],)
        ).fetchone()[0]
        link = db.execute(
            "SELECT l.context,l.parent_snapshot_id FROM official_event_links l "
            "WHERE l.child_task_id=(SELECT id FROM tasks WHERE dataset='TWT49UDetail')"
        ).fetchone()
    assert parent["state"] == "completed" and child["state"] == "completed"
    assert parent_event["symbol"] == "2887F" and parent_event["record_date"] == "2019-07-09"
    assert child_snapshot["validation_state"] == "empty_unverified"
    assert child_snapshot["error"] == "period_envelope_unverified;empty_unverified"
    assert child_records == 0
    assert link is not None and link["parent_snapshot_id"] == parent["snapshot_id"]
    context = json.loads(link["context"])
    assert context["symbol"] == "2887F" and context["event_date"] == "2019-07-09"

    second = w.sync_events("2019-07-09", "2019-07-09", markets=["twse"])
    assert second["state"] == "completed"
    assert len(calls) == first_call_count


def test_two_market_six_datasets_save_all_summaries_and_required_details(tmp_path):
    w, calls = workflow(tmp_path, event_callback)
    result = w.sync_events("2022-10-01", "2022-10-31")
    assert result["counts"]["completed"] == 8
    with w.store.connect() as db:
        saved = db.execute(
            "SELECT s.dataset,COUNT(r.id) records FROM snapshots s "
            "LEFT JOIN records r ON r.snapshot_id=s.id "
            "WHERE s.params LIKE '%official-v1%' GROUP BY s.dataset"
        ).fetchall()
        assert {r["dataset"] for r in saved} == {
            "TWT49U",
            "TWTAUU",
            "TWTB8U",
            "exDailyQ",
            "revivt",
            "pvChgRslt",
            "TWT49UDetail",
            "TWTAVUDetail",
        }
        assert all(r["records"] > 0 for r in saved)
        assert (
            db.execute("SELECT COUNT(*) FROM tasks WHERE dataset='TWTB8UDetail'").fetchone()[0] == 0
        )
    assert len(calls) == 8
