from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from urllib.parse import parse_qs, urlparse

import pytest

from quant_research.taiwan import official
from quant_research.taiwan.official_workflow import OfficialWorkflow


def make_workflow(tmp_path, as_of="2026-01-03", callback=None):
    now = [datetime.fromisoformat(as_of).replace(tzinfo=UTC)]
    calls = []

    def sleep(seconds):
        now[0] += timedelta(seconds=seconds)

    def transport(url, headers, timeout, max_bytes):
        parsed = urlparse(url)
        dataset = parsed.path.rsplit("/", 1)[-1]
        params = parse_qs(parsed.query)
        calls.append((dataset, params))
        if callback:
            result = callback(dataset, params)
            if result is not None:
                return result
        if dataset.endswith("Detail"):
            body = {"stat": "OK", "fields": ["股票代號", "現金股利"], "data": [["2330", "1"]]}
        else:
            start = params.get("startDate", ["2025/12/01"])[0].replace("/", "-")
            end = params.get("endDate", ["2025/12/31"])[0].replace("/", "-")
            if dataset == "TWT49U":
                body = {
                    "stat": "OK",
                    "strDate": start.replace("-", ""),
                    "endDate": end.replace("-", ""),
                    "fields": ["資料日期", "股票代號", "詳細資料"],
                    "data": [[start, "2330", f"2330,{start.replace('-', '')}"]],
                }
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

    return OfficialWorkflow(tmp_path, transport=transport, now=lambda: now[0], sleep=sleep), calls


def test_maintenance_validation_precedes_root_io(tmp_path):
    workflow, calls = make_workflow(tmp_path)
    for kwargs in ({"recent_months": True}, {"detail_refresh_days": 0}, {"max_tasks": False}):
        with pytest.raises(ValueError):
            workflow.maintain_events(**kwargs)
    with pytest.raises(ValueError):
        workflow.maintain_events("2026-1-03")
    assert calls == []
    assert not (tmp_path / "state.sqlite3").exists()


def test_fixed_calendar_months_cross_year_and_new_root_never_backfills(tmp_path):
    workflow, calls = make_workflow(tmp_path)
    result = workflow.maintain_events(max_tasks=1, markets=["twse"], recent_months=2)
    assert result["state"] == "paused"
    with workflow.store.connect() as db:
        requests = [
            json.loads(r[0])["request"]
            for r in db.execute(
                "SELECT params FROM tasks WHERE kind='official_event_summary' "
                "ORDER BY expected_date"
            )
        ]
    assert {(r["start_date"], r["end_date"]) for r in requests} == {
        ("2025-12-01", "2025-12-31"),
        ("2026-01-01", "2026-01-31"),
    }
    assert all(r["start_date"] >= "2025-12-01" for r in requests)
    assert len(calls) == 1


def test_manual_refresh_supersedes_pending_event_detail_child(tmp_path):
    workflow, _calls = make_workflow(tmp_path)
    first = workflow.sync_events("2026-01-01", "2026-01-31", markets=["twse"], max_tasks=1)
    assert first["state"] == "paused"
    with workflow.store.connect() as db:
        old_child = db.execute(
            "SELECT id,edition,state FROM tasks WHERE dataset='TWT49UDetail'"
        ).fetchone()
        old_parent = db.execute("SELECT id FROM tasks WHERE dataset='TWT49U'").fetchone()[0]
    assert old_child is not None and old_child["state"] == "pending"

    refreshed = workflow.sync_events(
        "2026-01-01", "2026-01-31", markets=["twse"], max_tasks=1, refresh=True
    )
    assert refreshed["state"] == "paused"
    with workflow.store.connect() as db:
        old = db.execute("SELECT state FROM tasks WHERE id=?", (old_child["id"],)).fetchone()
        children = db.execute(
            "SELECT id,edition,state FROM tasks WHERE dataset='TWT49UDetail' ORDER BY edition"
        ).fetchall()
        parents = db.execute(
            "SELECT id,edition,state FROM tasks WHERE dataset='TWT49U' ORDER BY edition"
        ).fetchall()
        links = db.execute(
            "SELECT parent_task_id,child_task_id FROM official_event_links ORDER BY rowid"
        ).fetchall()
    assert old["state"] == "superseded"
    assert [(row["edition"], row["state"]) for row in children] == [
        (1, "superseded"),
        (2, "pending"),
    ]
    assert [(row["edition"], row["state"]) for row in parents] == [
        (1, "completed"),
        (2, "completed"),
    ]
    assert len(parents) == 2 and parents[0]["id"] == old_parent
    assert links[-1]["parent_task_id"] == parents[1]["id"]
    assert links[-1]["child_task_id"] == children[1]["id"]
    assert (parents[0]["id"], children[0]["id"]) in [
        (row["parent_task_id"], row["child_task_id"]) for row in links
    ]


def test_maintenance_resumes_active_cycle_and_same_day_is_zero_http(tmp_path):
    workflow, calls = make_workflow(tmp_path)
    first = workflow.maintain_events(max_tasks=1, markets=["twse"], recent_months=1)
    assert first["state"] == "paused" and first["active"]
    with workflow.store.connect() as db:
        parent_id = db.execute("SELECT id FROM tasks WHERE dataset='TWT49U'").fetchone()[0]
        pending_child_count = db.execute(
            "SELECT count(*) FROM tasks WHERE dataset='TWT49UDetail'"
        ).fetchone()[0]
    assert pending_child_count == 1
    for _ in range(5):
        result = workflow.maintain_events(markets=["twse"], recent_months=1)
        assert result["cycle_as_of"] == "2026-01-03"
        if not result["active"]:
            break
        assert result["state"] in {"paused", "partial"}
    else:
        pytest.fail("maintenance cycle did not finish within five bounded invocations")
    summary_calls = [c for c in calls if c[0] == "TWT49U"]
    count = len(calls)
    noop = workflow.maintain_events(markets=["twse"], recent_months=1)
    assert noop["noop"] is True and noop["active"] is False
    assert len(calls) == count
    with workflow.store.connect() as db:
        assert db.execute("SELECT id FROM tasks WHERE dataset='TWT49U'").fetchone()[0] == parent_id
    assert len(summary_calls) == 1


def test_maintenance_reuses_detail_until_ttl_then_refreshes_changed_and_expired(
    tmp_path, monkeypatch
):
    now = [datetime(2026, 1, 3, tzinfo=UTC)]
    calls = []
    changed = [False]

    def transport(url, headers, timeout, max_bytes):
        parsed = urlparse(url)
        dataset = parsed.path.rsplit("/", 1)[-1]
        params = parse_qs(parsed.query)
        calls.append((dataset, params))
        start = params.get("startDate", [""])[0]
        end = params.get("endDate", [""])[0]
        if dataset == "TWT49U":
            rows = [
                ["2026-01-15", "2330", "2330,20260115", "9" if changed[0] else "8"],
                ["2026-01-16", "2317", "2317,20260116", "7"],
            ]
            body = {
                "stat": "OK",
                "strDate": start,
                "endDate": end,
                "fields": ["資料日期", "股票代號", "詳細資料", "現金股利"],
                "data": rows,
            }
        elif dataset in {"TWTAUU", "TWTB8U"}:
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
        else:
            symbol = params["STK_NO"][0]
            cash = ("9" if changed[0] else "8") if symbol == "2330" else "7"
            body = {"stat": "OK", "fields": ["股票代號", "現金股利"], "data": [[symbol, cash]]}
        return 200, json.dumps(body, ensure_ascii=False).encode()

    real_request = official.request

    def request(*args, **kwargs):
        return replace(real_request(*args, **kwargs), fetched_at=now[0].isoformat())

    monkeypatch.setattr(official, "request", request)

    def sleep(seconds):
        now[0] += timedelta(seconds=seconds)

    workflow = OfficialWorkflow(tmp_path, transport=transport, now=lambda: now[0], sleep=sleep)

    def run():
        return workflow.maintain_events(markets=["twse"], recent_months=1)

    result = run()
    assert result["active"] is False
    assert [
        len([c for c in calls if c[0] == "TWT49UDetail" and c[1]["STK_NO"] == [s]])
        for s in ("2330", "2317")
    ] == [1, 1]

    def linked():
        with workflow.store.connect() as db:
            return [
                dict(row)
                for row in db.execute(
                    "SELECT p.id parent_id,p.edition parent_edition,c.id child_id,"
                    "c.edition child_edition,c.dataset,c.params,c.snapshot_id,c.state,s.raw_path "
                    "FROM official_event_links l JOIN tasks p ON p.id=l.parent_task_id "
                    "JOIN tasks c ON c.id=l.child_task_id "
                    "LEFT JOIN snapshots s ON s.id=c.snapshot_id "
                    "WHERE p.dataset='TWT49U' AND p.edition=("
                    "SELECT max(latest.edition) FROM tasks latest "
                    "WHERE latest.logical_key=p.logical_key) "
                    "ORDER BY json_extract(c.params,'$.request.symbol')"
                )
            ]

    initial = linked()
    assert len(initial) == 2
    old_by_symbol = {json.loads(r["params"])["request"]["symbol"]: r for r in initial}
    initial_parent_id = initial[0]["parent_id"]
    initial_raw_path = old_by_symbol["2330"]["raw_path"]
    initial_raw_bytes = (tmp_path / initial_raw_path).read_bytes()
    initial_raw_hash = hashlib.sha256(initial_raw_bytes).hexdigest()
    now[0] += timedelta(days=1)
    result = run()
    assert result["active"] is False
    assert len([c for c in calls if c[0] == "TWT49UDetail"]) == 2
    refreshed = linked()
    assert len(refreshed) == 2
    assert all(r["parent_id"] != initial_parent_id for r in refreshed)
    assert all(
        r["parent_edition"] == old_by_symbol["2330"]["parent_edition"] + 1 for r in refreshed
    )
    refreshed_by_symbol = {json.loads(r["params"])["request"]["symbol"]: r for r in refreshed}
    assert {symbol: row["child_id"] for symbol, row in refreshed_by_symbol.items()} == {
        symbol: row["child_id"] for symbol, row in old_by_symbol.items()
    }
    refreshed_parent_id = refreshed_by_symbol["2330"]["parent_id"]

    changed[0] = True
    now[0] += timedelta(days=1)
    result = run()
    assert result["active"] is False
    current = linked()
    counts = {
        s: len([c for c in calls if c[0] == "TWT49UDetail" and c[1]["STK_NO"] == [s]])
        for s in ("2330", "2317")
    }
    assert counts == {"2330": 2, "2317": 1}
    current_by_symbol = {json.loads(r["params"])["request"]["symbol"]: r for r in current}
    assert current_by_symbol["2317"]["child_id"] == old_by_symbol["2317"]["child_id"]
    assert current_by_symbol["2330"]["child_id"] != old_by_symbol["2330"]["child_id"]
    assert current_by_symbol["2330"]["child_id"] != refreshed_by_symbol["2330"]["child_id"]
    assert current_by_symbol["2317"]["child_id"] == refreshed_by_symbol["2317"]["child_id"]
    assert current_by_symbol["2330"]["parent_id"] != refreshed_parent_id
    assert (
        old_by_symbol["2330"]["raw_path"]
        and old_by_symbol["2330"]["raw_path"] != current_by_symbol["2330"]["raw_path"]
    )

    now[0] += timedelta(days=8)
    result = run()
    assert result["active"] is False
    counts = {
        s: len([c for c in calls if c[0] == "TWT49UDetail" and c[1]["STK_NO"] == [s]])
        for s in ("2330", "2317")
    }
    assert counts == {"2330": 3, "2317": 2}
    assert all(r["raw_path"] for r in linked())
    with workflow.store.connect() as db:
        historical = [
            dict(row)
            for row in db.execute(
                "SELECT c.id child_id,s.raw_path FROM official_event_links l "
                "JOIN tasks p ON p.id=l.parent_task_id "
                "JOIN tasks c ON c.id=l.child_task_id "
                "LEFT JOIN snapshots s ON s.id=c.snapshot_id "
                "WHERE p.id=? ORDER BY json_extract(c.params,'$.request.symbol')",
                (initial_parent_id,),
            )
        ]
    assert len(historical) == 2
    assert {row["child_id"] for row in historical} == {row["child_id"] for row in initial}
    assert old_by_symbol["2330"]["raw_path"] == initial_raw_path
    final_raw_bytes = (tmp_path / initial_raw_path).read_bytes()
    assert final_raw_bytes == initial_raw_bytes
    assert hashlib.sha256(final_raw_bytes).hexdigest() == initial_raw_hash


@pytest.mark.parametrize(
    ("corruption", "value"),
    [
        ("fetched_at", "2026-01-05T00:00:00+00:00"),
        ("fetched_at", "2026-01-03T00:00:00"),
        ("fetched_at", "not-a-time"),
        ("request", '{"date":"2026-01-01"}'),
        ("source", "tpex"),
        ("records", None),
    ],
)
def test_maintenance_rejects_counterfactually_invalid_detail_cache(
    tmp_path, monkeypatch, corruption, value
):
    now = [datetime(2026, 1, 3, tzinfo=UTC)]
    calls = []

    def transport(url, headers, timeout, max_bytes):
        parsed = urlparse(url)
        dataset = parsed.path.rsplit("/", 1)[-1]
        params = parse_qs(parsed.query)
        calls.append((dataset, params))
        start = params.get("startDate", [""])[0]
        end = params.get("endDate", [""])[0]
        if dataset == "TWT49U":
            body = {
                "stat": "OK",
                "strDate": start,
                "endDate": end,
                "fields": ["資料日期", "股票代號", "詳細資料"],
                "data": [
                    ["2026-01-02", "2330", "2330,20260102"],
                    ["2026-01-03", "2317", "2317,20260103"],
                ],
            }
        elif dataset in {"TWTAUU", "TWTB8U"}:
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
        else:
            symbol = params["STK_NO"][0]
            body = {"stat": "OK", "fields": ["股票代號", "現金股利"], "data": [[symbol, "1"]]}
        return 200, json.dumps(body, ensure_ascii=False).encode()

    real_request = official.request

    def request(*args, **kwargs):
        return replace(real_request(*args, **kwargs), fetched_at=now[0].isoformat())

    monkeypatch.setattr(official, "request", request)

    def sleep(seconds):
        now[0] += timedelta(seconds=seconds)

    workflow = OfficialWorkflow(tmp_path, transport=transport, now=lambda: now[0], sleep=sleep)

    def run():
        return workflow.maintain_events(markets=["twse"], recent_months=1, detail_refresh_days=7)

    assert run()["active"] is False
    with workflow.store.connect() as db:
        child = db.execute(
            "SELECT c.id,c.snapshot_id,s.raw_path FROM tasks c "
            "JOIN snapshots s ON s.id=c.snapshot_id WHERE c.dataset='TWT49UDetail' "
            "AND json_extract(c.params,'$.request.symbol')='2330'"
        ).fetchone()
        other = db.execute(
            "SELECT c.id FROM tasks c WHERE c.dataset='TWT49UDetail' "
            "AND json_extract(c.params,'$.request.symbol')='2317'"
        ).fetchone()
        old_raw = (tmp_path / child["raw_path"]).read_bytes()
        old_raw_hash = hashlib.sha256(old_raw).hexdigest()
        snapshot_id = child["snapshot_id"]
        if corruption == "fetched_at":
            db.execute("UPDATE snapshots SET fetched_at=? WHERE id=?", (value, snapshot_id))
        elif corruption == "request":
            snapshot_params = json.loads(
                db.execute("SELECT params FROM snapshots WHERE id=?", (snapshot_id,)).fetchone()[0]
            )
            snapshot_params["request"]["date"] = "2026-01-01"
            db.execute(
                "UPDATE snapshots SET params=? WHERE id=?",
                (json.dumps(snapshot_params, sort_keys=True), snapshot_id),
            )
        elif corruption == "source":
            db.execute("UPDATE snapshots SET source=? WHERE id=?", (value, snapshot_id))
        else:
            db.execute("DELETE FROM records WHERE snapshot_id=? AND kind='event'", (snapshot_id,))
    now[0] += timedelta(days=1)
    assert run()["active"] is False
    detail_calls = [c for c in calls if c[0] == "TWT49UDetail"]
    assert [params["STK_NO"][0] for _, params in detail_calls].count("2330") == 2
    assert [params["STK_NO"][0] for _, params in detail_calls].count("2317") == 1
    with workflow.store.connect() as db:
        current = db.execute(
            "SELECT c.id,c.edition,c.snapshot_id,c.state,s.raw_path FROM tasks c "
            "JOIN snapshots s ON s.id=c.snapshot_id WHERE c.dataset='TWT49UDetail' "
            "AND json_extract(c.params,'$.request.symbol')='2330' "
            "ORDER BY c.edition DESC LIMIT 1"
        ).fetchone()
        other_current = db.execute(
            "SELECT c.id FROM tasks c WHERE c.dataset='TWT49UDetail' "
            "AND json_extract(c.params,'$.request.symbol')='2317' "
            "ORDER BY c.edition DESC LIMIT 1"
        ).fetchone()
        refs = db.execute(
            "SELECT count(*) FROM official_event_links WHERE child_task_id=?", (current["id"],)
        ).fetchone()[0]
    assert current["id"] != child["id"] and current["edition"] == 2
    assert current["state"] == "completed" and current["snapshot_id"] != snapshot_id
    assert other_current["id"] == other["id"]
    assert refs == 1
    assert (tmp_path / child["raw_path"]).read_bytes() == old_raw
    assert hashlib.sha256((tmp_path / child["raw_path"]).read_bytes()).hexdigest() == old_raw_hash


def test_maintenance_force_resumes_pending_child_in_place(tmp_path, monkeypatch):
    now = [datetime(2026, 1, 3, tzinfo=UTC)]
    changed = [False]
    calls = []

    def transport(url, headers, timeout, max_bytes):
        parsed = urlparse(url)
        dataset = parsed.path.rsplit("/", 1)[-1]
        params = parse_qs(parsed.query)
        calls.append((dataset, params))
        start = params.get("startDate", [""])[0]
        end = params.get("endDate", [""])[0]
        if dataset == "TWT49U":
            fields = ["資料日期", "股票代號", "詳細資料", "現金股利"]
            body = {
                "stat": "OK",
                "strDate": start,
                "endDate": end,
                "fields": fields,
                "data": [["2026-01-02", "2330", "2330,20260102", "2" if changed[0] else "1"]],
            }
        elif dataset in {"TWTAUU", "TWTB8U"}:
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
        else:
            symbol = params["STK_NO"][0]
            cash = "2" if changed[0] else "1"
            body = {"stat": "OK", "fields": ["股票代號", "現金股利"], "data": [[symbol, cash]]}
        return 200, json.dumps(body, ensure_ascii=False).encode()

    real_request = official.request

    def request(*args, **kwargs):
        return replace(real_request(*args, **kwargs), fetched_at=now[0].isoformat())

    monkeypatch.setattr(official, "request", request)

    def sleep(seconds):
        now[0] += timedelta(seconds=seconds)

    workflow = OfficialWorkflow(tmp_path, transport=transport, now=lambda: now[0], sleep=sleep)

    def run():
        return workflow.maintain_events(markets=["twse"], recent_months=1, detail_refresh_days=7)

    first = workflow.sync_events("2026-01-01", "2026-01-31", markets=["twse"], max_tasks=1)
    assert first["state"] == "paused"
    with workflow.store.connect() as db:
        parent_before = db.execute(
            "SELECT t.id,t.edition,t.snapshot_id,t.run_id,s.raw_path FROM tasks t "
            "JOIN snapshots s ON s.id=t.snapshot_id WHERE t.dataset='TWT49U'"
        ).fetchone()
        child_before = db.execute(
            "SELECT id,edition,state,run_id,snapshot_id,params FROM tasks "
            "WHERE dataset='TWT49UDetail'"
        ).fetchone()
        raw = (tmp_path / parent_before["raw_path"]).read_bytes()
        raw_hash = hashlib.sha256(raw).hexdigest()
    assert child_before is not None
    assert child_before["state"] == "pending" and child_before["snapshot_id"] is None
    assert not [c for c in calls if c[0] == "TWT49UDetail"]
    changed[0] = True
    now[0] += timedelta(days=1)
    result = run()
    assert result["active"] is False
    with workflow.store.connect() as db:
        child = db.execute(
            "SELECT c.id,c.edition,c.state,c.run_id,c.snapshot_id,s.raw_path FROM tasks c "
            "LEFT JOIN snapshots s ON s.id=c.snapshot_id WHERE c.dataset='TWT49UDetail'"
        ).fetchone()
        run_row = db.execute(
            "SELECT run_id FROM tasks WHERE dataset='TWT49U' AND edition=2"
        ).fetchone()
        link = db.execute(
            "SELECT p.id parent_id,p.snapshot_id parent_snapshot,l.child_task_id "
            "FROM official_event_links l JOIN tasks p ON p.id=l.parent_task_id "
            "WHERE l.child_task_id=? ORDER BY p.edition DESC LIMIT 1",
            (child_before["id"],),
        ).fetchone()
        editions = db.execute(
            "SELECT count(*) FROM tasks WHERE logical_key=("
            "SELECT logical_key FROM tasks WHERE id=?)",
            (child_before["id"],),
        ).fetchone()[0]
        states = db.execute("SELECT state FROM tasks WHERE id=?", (child_before["id"],)).fetchone()[
            0
        ]
        event_record = db.execute(
            "SELECT payload FROM records WHERE snapshot_id=? AND kind='event'",
            (child["snapshot_id"],),
        ).fetchone()
        parent_after = db.execute(
            "SELECT t.id,t.edition,t.snapshot_id,s.raw_path FROM tasks t "
            "JOIN snapshots s ON s.id=t.snapshot_id WHERE t.dataset='TWT49U' AND t.edition=1"
        ).fetchone()
    assert child["id"] == child_before["id"] and child["edition"] == child_before["edition"] == 1
    assert event_record is not None
    assert json.loads(event_record["payload"])["cash_dividend_per_share"] == "2"
    assert editions == 1 and states == "completed" and child["run_id"] == run_row["run_id"]
    assert child["run_id"] != child_before["run_id"] and child["snapshot_id"]
    assert (
        link["parent_id"]
        and link["parent_snapshot"]
        and link["child_task_id"] == child_before["id"]
    )
    assert len([c for c in calls if c[0] == "TWT49UDetail"]) == 1
    assert (
        parent_after["id"] == parent_before["id"]
        and parent_after["snapshot_id"] == parent_before["snapshot_id"]
    )
    assert (tmp_path / parent_after["raw_path"]).read_bytes() == raw
    assert (
        hashlib.sha256((tmp_path / parent_after["raw_path"]).read_bytes()).hexdigest() == raw_hash
    )


def test_maintenance_partial_counts_empty_detail_after_bounded_runs(tmp_path, monkeypatch):
    now = [datetime(2026, 1, 3, tzinfo=UTC)]
    calls = []

    def transport(url, headers, timeout, max_bytes):
        parsed = urlparse(url)
        dataset = parsed.path.rsplit("/", 1)[-1]
        params = parse_qs(parsed.query)
        calls.append((dataset, params))
        start = params.get("startDate", [""])[0]
        end = params.get("endDate", [""])[0]
        if dataset == "TWT49U":
            body = {
                "stat": "OK",
                "strDate": start,
                "endDate": end,
                "fields": ["資料日期", "股票代號", "詳細資料"],
                "data": [["2026-01-02", "2330", "2330,20260102"]],
            }
        elif dataset == "TWT49UDetail":
            body = {"stat": "OK", "fields": ["股票代號", "現金股利"], "data": []}
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

    real_request = official.request

    def request(*args, **kwargs):
        return replace(real_request(*args, **kwargs), fetched_at=now[0].isoformat())

    monkeypatch.setattr(official, "request", request)

    def sleep(seconds):
        now[0] += timedelta(seconds=seconds)

    workflow = OfficialWorkflow(tmp_path, transport=transport, now=lambda: now[0], sleep=sleep)
    results = []
    for _ in range(8):
        result = workflow.maintain_events(max_tasks=1, markets=["twse"], recent_months=1)
        results.append(result)
        if not result["active"]:
            break
    else:
        pytest.fail("maintenance cycle did not finish within eight bounded invocations")
    last = results[-1]
    assert last["state"] == "partial"
    assert last["cycle_counts"]["detail_empty_unverified"] == 1
    assert last["cycle_counts"]["detail_unverified_period"] == 0
    assert calls[-1][0] != "TWT49UDetail"
    before = len(calls)
    noop = workflow.maintain_events(markets=["twse"], recent_months=1)
    assert noop["noop"] is True and noop["state"] == "partial"
    assert len(calls) == before


def test_blocked_maintenance_does_not_become_partial_and_blocks_source(tmp_path):
    calls = []

    def callback(dataset, params):
        calls.append(dataset)
        if len(calls) == 1:
            start = params["startDate"][0].replace("/", "-")
            end = params["endDate"][0].replace("/", "-")
            body = {
                "stat": "OK",
                "strDate": start,
                "endDate": end,
                "fields": (
                    ["資料日期", "股票代號", "詳細資料"]
                    if dataset == "TWT49U"
                    else [
                        "恢復買賣日期",
                        "股票代號",
                        "停止買賣前收盤價格",
                        "恢復買賣參考價",
                        "詳細資料",
                    ]
                ),
                "data": [],
            }
            return 200, json.dumps(body, ensure_ascii=False).encode()
        return 403, b"forbidden"

    workflow, http_calls = make_workflow(tmp_path, callback=callback)
    first = workflow.maintain_events(max_tasks=1, markets=["twse"], recent_months=1)
    assert first["state"] == "partial"
    blocked = workflow.maintain_events(markets=["twse"], recent_months=1)
    assert blocked["state"] == "blocked" and blocked["active"] is True
    assert blocked["cycle_counts"]["summary_empty_unverified"] >= 1
    with workflow.store.connect() as db:
        assert (
            db.execute("SELECT state FROM source_blocks WHERE source='twse'").fetchone()[0]
            == "blocked"
        )
    request_count = len(http_calls)
    later = workflow.maintain_events(markets=["twse"], recent_months=1)
    quotes = workflow.sync(expected_date="2026-01-03", markets=["twse"])
    assert later["state"] == "blocked" and later["active"] is True
    assert len(http_calls) == request_count
    assert quotes["state"] == "blocked"
    assert len(http_calls) == request_count


def test_maintenance_cold_cursor_rotates_planned_historical_scopes(tmp_path):
    workflow, calls = make_workflow(
        tmp_path,
        callback=lambda dataset, params: (
            200,
            json.dumps(
                {
                    "stat": "OK",
                    "strDate": params["startDate"][0].replace("/", "-"),
                    "endDate": params["endDate"][0].replace("/", "-"),
                    "fields": (
                        ["資料日期", "股票代號", "詳細資料"]
                        if dataset == "TWT49U"
                        else [
                            "恢復買賣日期",
                            "股票代號",
                            "停止買賣前收盤價格",
                            "恢復買賣參考價",
                            "詳細資料",
                        ]
                    ),
                    "data": [],
                },
                ensure_ascii=False,
            ).encode(),
        ),
    )
    workflow.sync_events("2025-10-01", "2025-11-30", markets=["twse"])
    workflow.sync_events("2025-10-01", "2025-10-31", markets=["twse"], refresh=True)
    with workflow.store.connect() as db:
        old = db.execute(
            "SELECT id,logical_key,dataset,params,snapshot_id FROM tasks t "
            "WHERE kind='official_event_summary' AND edition=("
            "SELECT max(x.edition) FROM tasks x WHERE x.logical_key=t.logical_key) "
            "ORDER BY logical_key"
        ).fetchall()
    assert len(old) == 6
    old_ids = {row["id"] for row in old}
    assert len({row["logical_key"] for row in old}) == 6
    old_snapshots = {}
    with workflow.store.connect() as db:
        for row in old:
            if row["snapshot_id"]:
                snapshot = db.execute(
                    "SELECT raw_path FROM snapshots WHERE id=?", (row["snapshot_id"],)
                ).fetchone()
                if snapshot and snapshot["raw_path"]:
                    raw = (tmp_path / snapshot["raw_path"]).read_bytes()
                    old_snapshots[snapshot["raw_path"]] = hashlib.sha256(raw).hexdigest()
    expected = {
        (
            row["dataset"],
            json.loads(row["params"])["request"]["start_date"],
            json.loads(row["params"])["request"]["end_date"],
        )
        for row in old
    }
    seen_cold = []
    for day in range(3, 9):
        before_len = len(calls)
        result = workflow.maintain_events(
            expected_date=f"2026-01-{day:02d}",
            markets=["twse"],
            recent_months=1,
        )
        assert result["active"] is False
        current_calls = calls[before_len:]
        assert len(current_calls) == 4
        normalized = [
            (
                dataset,
                date.fromisoformat(params["startDate"][0].replace("/", "-")).isoformat(),
                date.fromisoformat(params["endDate"][0].replace("/", "-")).isoformat(),
            )
            for dataset, params in current_calls
        ]
        historical_calls = [scope for scope in normalized if scope[1] < "2025-12-01"]
        assert len(historical_calls) == 1
        cold_scope = historical_calls[0]
        seen_cold.append(cold_scope)
        with workflow.store.connect() as db:
            last = db.execute(
                "SELECT value FROM settings WHERE key='official_events_maintenance_last'"
            ).fetchone()
            cycle = json.loads(last[0])
            retained = {row[0] for row in db.execute("SELECT id FROM tasks")}
            scopes = {
                (
                    row["dataset"],
                    json.loads(row["params"])["request"]["start_date"],
                    json.loads(row["params"])["request"]["end_date"],
                )
                for row in db.execute(
                    "SELECT dataset,params FROM tasks WHERE kind='official_event_summary'"
                )
            }
        cold_logical = next(
            row["logical_key"]
            for row in old
            if (
                row["dataset"],
                json.loads(row["params"])["request"]["start_date"],
                json.loads(row["params"])["request"]["end_date"],
            )
            == cold_scope
        )
        assert cycle["cold_cursor"] == cold_logical
        assert old_ids <= retained
        assert scopes == expected | {
            ("TWT49U", "2026-01-01", "2026-01-31"),
            ("TWTAUU", "2026-01-01", "2026-01-31"),
            ("TWTB8U", "2026-01-01", "2026-01-31"),
        }
    assert len(set(seen_cold)) == 6
    assert set(seen_cold) == expected
    # The prior-event task IDs and raw snapshots remain present after every rotation.
    with workflow.store.connect() as db:
        assert (
            db.execute(
                f"SELECT count(*) FROM tasks WHERE id IN ({','.join('?' for _ in old_ids)})",
                tuple(old_ids),
            ).fetchone()[0]
            == 6
        )
        assert db.execute("SELECT count(*) FROM request_log").fetchone()[0] >= 33
        assert db.execute("SELECT count(*) FROM official_event_links").fetchone()[0] == 0
    assert old_snapshots
    assert {
        path: hashlib.sha256((tmp_path / path).read_bytes()).hexdigest() for path in old_snapshots
    } == old_snapshots


def test_maintenance_partial_survives_resumed_cycle_with_empty_summaries(tmp_path):
    workflow, calls = make_workflow(tmp_path, callback=lambda dataset, params: None)
    results = []
    original_parent_ids = None
    for _ in range(8):
        result = workflow.maintain_events(max_tasks=1, markets=["twse"], recent_months=1)
        results.append(result)
        with workflow.store.connect() as db:
            active = db.execute(
                "SELECT value FROM settings WHERE key='official_events_maintenance_active'"
            ).fetchone()
            latest = {
                r["dataset"]: r["id"]
                for r in db.execute(
                    "SELECT t.dataset,t.id FROM tasks t "
                    "WHERE t.kind='official_event_summary' AND t.edition=("
                    "SELECT max(x.edition) FROM tasks x WHERE x.logical_key=t.logical_key)"
                )
            }
        if original_parent_ids is None:
            original_parent_ids = latest
        assert latest == original_parent_ids
        if result["active"] is False:
            break
        assert active is not None
        assert all(latest.values())
    else:
        pytest.fail("maintenance cycle did not finish within eight bounded invocations")
    assert any(
        r["state"] == "partial" or r.get("cycle_counts", {}).get("summary_empty_unverified", 0)
        for r in results
    )
    assert results[-1]["state"] == "partial"
    before = len(calls)
    noop = workflow.maintain_events(markets=["twse"], recent_months=1)
    assert noop["noop"] is True and noop["state"] == "partial"
    assert len(calls) == before
