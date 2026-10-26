from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from urllib.parse import parse_qs, urlparse

import pytest

from quant_research.taiwan import cli
from quant_research.taiwan.official_workflow import OfficialWorkflow
from quant_research.taiwan.storage import SQLiteStore, TaiwanData


def event_transport(url, headers, timeout, max_bytes):
    parsed = urlparse(url)
    dataset = parsed.path.rsplit("/", 1)[-1]
    params = parse_qs(parsed.query)
    start = params.get("startDate", ["2026-10-01"])[0].replace("/", "-")
    end = params.get("endDate", ["2026-10-31"])[0].replace("/", "-")
    if dataset == "TWT49UDetail":
        value = {"stat": "OK", "fields": ["股票代號", "現金股利"], "data": [["2330", "1"]]}
    elif dataset == "TWT49U":
        value = {
            "stat": "OK",
            "strDate": start.replace("-", ""),
            "endDate": end.replace("-", ""),
            "fields": ["資料日期", "股票代號", "詳細資料"],
            "data": [[start, "2330", f"2330,{start.replace('-', '')}"]],
        }
    elif dataset in {"TWTAUU", "TWTB8U"}:
        fields = ["恢復買賣日期", "股票代號", "停止買賣前收盤價格", "恢復買賣參考價", "詳細資料"]
        token = "2330,20261010" if dataset == "TWTAUU" else "2330,20261009,20261010"
        payload = ["2026-10-10", "2330", "10", "12", token]
        value = {
            "stat": "OK",
            "strDate": start,
            "endDate": end,
            "fields": fields,
            "data": [payload],
        }
    else:
        value = {
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
    return 200, json.dumps(value, ensure_ascii=False).encode()


def factory(root):
    from datetime import timedelta

    clock = [datetime(2026, 10, 23, tzinfo=UTC)]

    def sleep(seconds):
        clock[0] += timedelta(seconds=seconds)

    return OfficialWorkflow(root, transport=event_transport, now=lambda: clock[0], sleep=sleep)


def test_cli_sync_resume_links_coverage_and_unverified_read(tmp_path, monkeypatch, capsys):
    root = tmp_path / "isolated"
    monkeypatch.setattr(cli, "OfficialWorkflow", factory)
    monkeypatch.setenv("FINMIND_TOKEN", "POISON_TOKEN_MUST_NOT_PERSIST")
    args = [
        "--root",
        str(root),
        "sync-events",
        "--start",
        "2026-10-10",
        "--end",
        "2026-10-10",
        "--markets",
        "twse",
        "--max-tasks",
        "1",
    ]
    assert cli.main(args) == 0
    assert json.loads(capsys.readouterr().out)["state"] == "paused"
    assert cli.main(["--root", str(root), "resume", "--lane", "events"]) == 1
    assert json.loads(capsys.readouterr().out)["state"] == "partial"
    links = TaiwanData(root).event_links("2330")
    assert links and links[0]["parent"]["dataset"] == "TWT49U"
    assert links[0]["child"]["dataset"] == "TWT49UDetail"
    assert links[0]["context"]["event_date"] == "2026-10-10"
    coverage = TaiwanData(root).event_coverage()
    assert coverage["scope"] == "planned_tasks_only"
    assert coverage["coverage_complete"] is False and coverage["research_ready"] is False
    assert coverage["summary_periods"]
    with SQLiteStore(root).connect() as db:
        assert db.execute("SELECT COUNT(*) FROM tasks WHERE source='finmind'").fetchone()[0] == 0
        assert "POISON_TOKEN_MUST_NOT_PERSIST" not in "\n".join(db.iterdump())
        assert (
            db.execute(
                "SELECT COUNT(*) FROM tasks "
                "WHERE kind='official_event_detail' AND state='completed'"
            ).fetchone()[0]
            >= 1
        )


def test_cli_real_two_market_events_resume_and_preserve_legacy_finmind(
    tmp_path, monkeypatch, capsys
):
    import importlib.util
    from pathlib import Path

    spec = importlib.util.spec_from_file_location(
        "event_workflow_fixtures",
        Path(__file__).with_name("test_taiwan_official_event_workflow.py"),
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    event_callback = module.event_callback

    root = tmp_path / "all-events"
    calls = []
    clock = [datetime(2022, 11, 1, tzinfo=UTC)]
    from datetime import timedelta

    def transport(url, headers, timeout, max_bytes):
        calls.append(url)
        return 200, event_callback(
            urlparse(url).path.rsplit("/", 1)[-1], parse_qs(urlparse(url).query)
        )

    def make_workflow(path):
        def sleep(seconds):
            clock[0] += timedelta(seconds=seconds)

        return OfficialWorkflow(path, transport=transport, now=lambda: clock[0], sleep=sleep)

    monkeypatch.setattr(cli, "OfficialWorkflow", make_workflow)
    monkeypatch.setenv("FINMIND_TOKEN", "POISON_TOKEN_MUST_NOT_PERSIST")
    store = SQLiteStore(root)
    store.initialize()
    raw_path = store.save_raw("finmind", b"legacy-finmind-raw-bytes")
    with store.connect() as db:
        db.execute(
            "INSERT INTO tasks(id,logical_key,edition,source,dataset,params,kind,state) "
            "VALUES('legacy','legacy-key',1,'finmind','legacy','{}','legacy','running')"
        )
    args = [
        "--root",
        str(root),
        "sync-events",
        "--start",
        "2022-10-01",
        "--end",
        "2022-10-31",
        "--max-tasks",
        "1",
    ]
    assert cli.main(args) == 0
    assert json.loads(capsys.readouterr().out)["state"] == "paused"
    assert cli.main(["--root", str(root), "resume", "--lane", "events"]) == 0
    assert json.loads(capsys.readouterr().out)["state"] == "completed"
    with store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0] == 8
        assert db.execute("SELECT COUNT(*) FROM records").fetchone()[0] == 8
        assert db.execute("SELECT COUNT(*) FROM official_event_links").fetchone()[0] == 2
        assert tuple(db.execute("SELECT source,state FROM tasks WHERE id='legacy'").fetchone()) == (
            "finmind",
            "running",
        )
        assert "POISON_TOKEN_MUST_NOT_PERSIST" not in "\\n".join(db.iterdump())
    assert (root / raw_path).read_bytes() == b"legacy-finmind-raw-bytes"
    assert len(calls) == 8


def test_read_only_empty_old_database_and_cli_invalid_inputs(tmp_path, capsys):
    missing = tmp_path / "missing"
    data = TaiwanData(missing)
    assert data.event_links() == []
    assert data.event_coverage()["summary_periods"] == []
    assert not missing.exists()
    old = tmp_path / "old"
    old.mkdir()
    import sqlite3

    SQLiteStore(old).initialize()
    db = sqlite3.connect(old / "state.sqlite3")
    db.execute("DROP TABLE official_event_links")
    db.commit()
    db.close()
    assert TaiwanData(old).event_links() == []
    assert TaiwanData(old).event_coverage()["summary_periods"] == []
    root = tmp_path / "invalid"
    assert cli.main(["--root", str(root), "sync-events", "--start", "bad-date"]) == 2
    capsys.readouterr()
    assert (
        cli.main(["--root", str(root), "sync-events", "--start", "2026-10-01", "--max-tasks", "0"])
        == 2
    )
    capsys.readouterr()
    with pytest.raises(SystemExit) as invalid_source:
        cli.main(["--root", str(root), "event-links", "--source", "finmind"])
    assert invalid_source.value.code == 2
    assert not root.exists()
    missing_start = tmp_path / "missing-start"
    with pytest.raises(SystemExit) as missing_argument:
        cli.main(["--root", str(missing_start), "sync-events"])
    assert missing_argument.value.code == 2
    assert not missing_start.exists()


def test_coverage_counts_only_verified_empty_state_and_latest_unique_details(tmp_path):
    store = SQLiteStore(tmp_path)
    store.initialize()
    with store.connect() as db:

        def task(task_id, key, dataset, kind, state, edition=1, snapshot_id=None):
            params = json.dumps({"pipeline": "official-v1", "lane": "events", "request": {}})
            db.execute(
                "INSERT INTO tasks(id,logical_key,edition,source,dataset,params,kind,state,"
                "snapshot_id) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (task_id, key, edition, "twse", dataset, params, kind, state, snapshot_id),
            )

        def snapshot(snapshot_id, dataset, validation, records=()):
            db.execute(
                "INSERT INTO snapshots VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    snapshot_id,
                    "twse",
                    dataset,
                    json.dumps({"pipeline": "official-v1", "lane": "events"}),
                    "offline",
                    snapshot_id,
                    "2026-10-01T00:00:00Z",
                    200,
                    "hash",
                    "raw/file.bin",
                    validation,
                    None,
                ),
            )
            for record in records:
                db.execute(
                    "INSERT INTO records(snapshot_id,kind,payload) VALUES(?,?,?)",
                    (snapshot_id, "event", json.dumps(record)),
                )

        snapshot("empty", "TWT49U", "empty_unverified")
        snapshot("warn", "TWTAUU", "period_envelope_unverified", ({"missing_fields": ["price"]},))
        snapshot(
            "detail",
            "TWT49UDetail",
            "valid",
            ({"observation_type": "dividend", "missing_fields": ["amount"]},),
        )
        task(
            "empty-task",
            "empty-key",
            "TWT49U",
            "official_event_summary",
            "completed",
            snapshot_id="empty",
        )
        task(
            "warn-task",
            "warn-key",
            "TWTAUU",
            "official_event_summary",
            "completed",
            snapshot_id="warn",
        )
        task("pending-summary", "pending-key", "TWTB8U", "official_event_summary", "pending")
        task("failed-summary", "failed-key", "pvChgRslt", "official_event_summary", "failed")
        task(
            "child",
            "child-key",
            "TWT49UDetail",
            "official_event_detail",
            "completed",
            snapshot_id="detail",
        )
        task(
            "pending-child", "pending-child-key", "TWTAVUDetail", "official_event_detail", "pending"
        )
        db.execute(
            "INSERT INTO official_event_links(parent_task_id,parent_snapshot_id,"
            "child_task_id,context) VALUES('empty-task','empty','child','{}')"
        )
        db.execute(
            "INSERT INTO official_event_links(parent_task_id,parent_snapshot_id,"
            "child_task_id,context) VALUES('warn-task','warn','child','{}')"
        )
        db.execute(
            "INSERT INTO official_event_links(parent_task_id,parent_snapshot_id,"
            "child_task_id,context) VALUES('empty-task','empty','pending-child','{}')"
        )
    coverage = TaiwanData(tmp_path).event_coverage()
    assert coverage["empty_periods"] == 1
    assert coverage["period_warnings"] == 1
    assert coverage["summary_states"] == {"completed": 2, "pending": 1, "failed": 1}
    assert coverage["detail_states"] == {"completed": 1, "pending": 1}
    assert coverage["latest_parent_links"] == 3
    assert coverage["distinct_required_details"] == 2
    assert coverage["detail_links_not_completed_and_valid"] == 1
    assert {
        (row["dataset"], row["observation_type"], row["field"], row["count"])
        for row in coverage["reported_missing_fields"]
    } == {
        ("TWTAUU", None, "price", 1),
        ("TWT49UDetail", "dividend", "amount", 1),
    }


def test_status_uses_events_runner_lock_during_http_without_writer(tmp_path):
    root = tmp_path / "status"
    observations = []
    lock_observations = []

    def transport(url, headers, timeout, max_bytes):
        state = TaiwanData(root).status()
        observations.append(state)
        import fcntl
        import os

        fd = os.open(root / "writer.lock", os.O_RDONLY)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            lock_observations.append("available")
            fcntl.flock(fd, fcntl.LOCK_UN)
        except BlockingIOError:
            lock_observations.append("held")
        finally:
            os.close(fd)
        parsed = urlparse(url)
        dataset = parsed.path.rsplit("/", 1)[-1]
        params = parse_qs(parsed.query)
        start = params.get("startDate", ["2026-10-10"])[0].replace("/", "-")
        if dataset == "TWT49U":
            body = {
                "stat": "OK",
                "strDate": start,
                "endDate": start,
                "fields": ["資料日期", "股票代號", "詳細資料"],
                "data": [],
            }
        else:
            body = {
                "stat": "OK",
                "strDate": start,
                "endDate": start,
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

    workflow = OfficialWorkflow(
        root,
        transport=transport,
        now=lambda: datetime(2026, 10, 23, tzinfo=UTC),
        sleep=lambda _: None,
    )
    result = workflow.sync_events("2026-10-10", "2026-10-10", markets=["twse"], max_tasks=1)
    assert result["state"] != "failed"
    assert observations
    with SQLiteStore(root).connect() as db:
        assert db.execute("SELECT COUNT(*) FROM snapshots WHERE http_status=200").fetchone()[0] == 1
    assert lock_observations and set(lock_observations) == {"available"}
    assert all(item["running"] for item in observations)
    assert all(item["lanes"]["events_running"] for item in observations)
    assert all(not item["lanes"]["official_quotes_running"] for item in observations)
    assert all(not item["stale_run_ids"] for item in observations)


def test_events_explicitly_include_unverified_period_warning(tmp_path):
    store = SQLiteStore(tmp_path)
    store.initialize()
    store.save_snapshot(
        source="twse",
        dataset="TWT49U",
        params={},
        safe_url="offline",
        scope_key="unverified",
        fetched_at="2026-01-01T00:00:00Z",
        http_status=200,
        sha256="x",
        raw_path="raw",
        validation_state="period_envelope_unverified",
        records=(
            {
                "kind": "event",
                "symbol": "2330",
                "date": None,
                "quality_flags": ["period_envelope_unverified"],
            },
        ),
    )
    data = TaiwanData(tmp_path)
    assert data.events() == []
    observations = data.events(include_unverified=True)
    assert len(observations) == 1
    assert observations[0]["quality_flags"] == ["period_envelope_unverified"]


def test_cli_sync_plans_quotes_only_with_real_workflow_transport(tmp_path, monkeypatch, capsys):
    from datetime import timedelta

    root = tmp_path / "quotes-only"
    calls = []
    clock = [datetime(2022, 10, 11, tzinfo=UTC)]

    def transport(url, headers, timeout, max_bytes):
        calls.append(url)
        parsed = urlparse(url)
        date_value = parse_qs(parsed.query)["date"][0]
        fields = [
            "證券代號",
            "證券名稱",
            "開盤價",
            "最高價",
            "最低價",
            "收盤價",
            "成交股數",
            "成交金額",
        ]
        if "MI_INDEX" in parsed.path:
            body = {
                "stat": "OK",
                "date": date_value,
                "tables": [
                    {
                        "fields": fields,
                        "data": [["2330", "台積電", "10", "11", "9", "10", "1", "10"]],
                    }
                ],
            }
        else:
            year, month, day = date_value.split("/")
            body = {
                "stat": "ok",
                "date": f"{int(year) - 1911:03d}/{month}/{day}",
                "fields": fields,
                "tables": [{"title": "每日收盤行情", "fields": fields, "data": []}],
            }
        return 200, json.dumps(body, ensure_ascii=False).encode()

    def make_workflow(path):
        def sleep(seconds):
            clock[0] += timedelta(seconds=seconds)

        return OfficialWorkflow(path, transport=transport, now=lambda: clock[0], sleep=sleep)

    monkeypatch.setattr(cli, "OfficialWorkflow", make_workflow)
    assert (
        cli.main(
            [
                "--root",
                str(root),
                "sync",
                "--expected-date",
                "2022-10-11",
                "--markets",
                "twse",
                "--max-tasks",
                "1",
            ]
        )
        == 0
    )
    result = json.loads(capsys.readouterr().out)
    assert result["state"] == "completed"
    assert calls and all("TWT49U" not in call and "TWTAUU" not in call for call in calls)
    with SQLiteStore(root).connect() as db:
        assert (
            db.execute(
                "SELECT COUNT(*) FROM tasks WHERE kind='official_quote' AND state='completed'"
            ).fetchone()[0]
            > 0
        )
        assert (
            db.execute("SELECT COUNT(*) FROM tasks WHERE kind LIKE 'official_event_%'").fetchone()[
                0
            ]
            == 0
        )
        assert db.execute("SELECT COUNT(*) FROM official_event_links").fetchone()[0] == 0


def test_cli_events_include_unverified_warning_only_when_requested(tmp_path, capsys):
    store = SQLiteStore(tmp_path)
    store.initialize()
    store.save_snapshot(
        source="twse",
        dataset="TWT49U",
        params={},
        safe_url="offline",
        scope_key="cli-warning",
        fetched_at="2026-01-01T00:00:00Z",
        http_status=200,
        sha256="warning-hash",
        raw_path="raw/warning.bin",
        validation_state="period_envelope_unverified",
        records=(
            {
                "kind": "event",
                "symbol": "2330",
                "date": None,
                "quality_flags": ["period_envelope_unverified"],
            },
        ),
    )
    assert cli.main(["--root", str(tmp_path), "events", "--symbol", "2330"]) == 0
    assert json.loads(capsys.readouterr().out) == []
    assert (
        cli.main(["--root", str(tmp_path), "events", "--symbol", "2330", "--include-unverified"])
        == 0
    )
    result = json.loads(capsys.readouterr().out)
    assert len(result) == 1
    assert result[0]["quality_flags"] == ["period_envelope_unverified"]


def test_event_links_keep_latest_parent_history_and_exact_child_snapshots(tmp_path):
    import importlib.util
    from pathlib import Path

    spec = importlib.util.spec_from_file_location(
        "event_workflow_fixtures",
        Path(__file__).with_name("test_taiwan_official_event_workflow.py"),
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    detail_value = ["8 元/股"]

    def callback(dataset, params):
        if dataset == "TWT49UDetail":
            return json.dumps(
                {
                    "stat": "OK",
                    "fields": ["股票代號", "(每股配發現金股利)除息"],
                    "data": [[params["STK_NO"][0], detail_value[0]]],
                }
            ).encode()
        return module.event_callback(dataset, params)

    w, _ = module.workflow(tmp_path, callback)
    w.sync_events("2022-10-01", "2022-10-20", markets=["twse"])
    w.sync_events("2022-10-05", "2022-10-31", markets=["twse"])
    detail_value[0] = "9 元/股"
    w.sync_events("2022-10-01", "2022-10-20", markets=["twse"], refresh=True)

    data = TaiwanData(tmp_path)
    current = [link for link in data.event_links("2330") if link["parent"]["dataset"] == "TWT49U"]
    history = [
        link
        for link in data.event_links("2330", include_history=True)
        if link["parent"]["dataset"] == "TWT49U"
    ]
    assert len(current) == 2 and len(history) == 3
    assert data.event_links("9999") == []
    assert {link["parent"]["request"]["start_date"] for link in current} == {
        "2022-10-01",
        "2022-10-05",
    }
    old_a = next(
        link
        for link in history
        if link["parent"]["request"]["start_date"] == "2022-10-01"
        and not link["parent"]["latest_edition"]
    )
    new_a = next(
        link for link in current if link["parent"]["request"]["start_date"] == "2022-10-01"
    )
    b = next(link for link in current if link["parent"]["request"]["start_date"] == "2022-10-05")
    assert old_a["parent"]["latest_edition"] is False
    assert new_a["parent"]["latest_edition"] is True
    assert (
        type(new_a["child"]["latest_edition"]) is bool and new_a["child"]["latest_edition"] is True
    )
    assert type(b["child"]["latest_edition"]) is bool and b["child"]["latest_edition"] is False
    assert b["child"]["snapshot_id"] == old_a["child"]["snapshot_id"]
    assert b["child"]["sha256"] == old_a["child"]["sha256"]
    assert b["child"]["raw_path"] == old_a["child"]["raw_path"]
    assert new_a["child"]["snapshot_id"] != b["child"]["snapshot_id"]
    with SQLiteStore(tmp_path).connect() as db:
        old_raw = db.execute(
            "SELECT raw_path,sha256 FROM snapshots WHERE id=?", (b["child"]["snapshot_id"],)
        ).fetchone()
        old_child_payload = db.execute(
            "SELECT r.payload FROM records r WHERE r.snapshot_id=?", (b["child"]["snapshot_id"],)
        ).fetchone()[0]
        counts_before = data.event_coverage()
        assert db.execute("SELECT COUNT(*) FROM official_event_links").fetchone()[0] > 2
    old_bytes = (tmp_path / old_raw["raw_path"]).read_bytes()
    assert hashlib.sha256(old_bytes).hexdigest() == old_raw["sha256"]
    assert "8 元/股" in old_child_payload
    assert counts_before["detail_states"] == {"completed": 2}
    assert counts_before["latest_parent_links"] == 4
    shared_missing_counts = [
        row["count"]
        for row in counts_before["reported_missing_fields"]
        if row["dataset"] == "TWT49UDetail"
    ]
    assert len(shared_missing_counts) == 6 and set(shared_missing_counts) == {1}


@pytest.mark.parametrize(
    ("source", "expected_periods", "expected_links"),
    [("official", 6, 2), ("twse", 3, 2), ("tpex", 3, 0)],
)
def test_event_reads_filter_source_after_completed_workflow(
    tmp_path, source, expected_periods, expected_links
):
    import importlib.util
    from pathlib import Path

    spec = importlib.util.spec_from_file_location(
        "event_workflow_fixtures",
        Path(__file__).with_name("test_taiwan_official_event_workflow.py"),
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    workflow, _ = module.workflow(tmp_path, module.event_callback)

    result = workflow.sync_events("2022-10-01", "2022-10-31", markets=["twse", "tpex"])
    assert result["state"] == "completed"

    data = TaiwanData(tmp_path)
    links = data.event_links(source=source)
    coverage = data.event_coverage(source=source)
    expected_sources = {"twse", "tpex"} if source == "official" else {source}
    assert len(coverage["summary_periods"]) == expected_periods
    assert {period["source"] for period in coverage["summary_periods"]} == expected_sources
    assert coverage["summary_states"] == {"completed": expected_periods}
    assert {link["parent"]["source"] for link in links} <= expected_sources
    assert {link["child"]["source"] for link in links} <= expected_sources
    assert len(links) == expected_links
    if source in {"official", "twse"}:
        assert {(link["parent"]["dataset"], link["child"]["dataset"]) for link in links} == {
            ("TWT49U", "TWT49UDetail"),
            ("TWTAUU", "TWTAVUDetail"),
        }
    else:
        assert links == []


def test_event_links_and_coverage_exclude_legacy_lane_links_but_keep_finmind(tmp_path):
    import importlib.util
    from pathlib import Path

    spec = importlib.util.spec_from_file_location(
        "event_workflow_fixtures",
        Path(__file__).with_name("test_taiwan_official_event_workflow.py"),
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    w, _ = module.workflow(tmp_path)
    w.sync_events("2022-10-01", "2022-10-20", markets=["twse"])
    store = SQLiteStore(tmp_path)
    baseline = TaiwanData(tmp_path).event_coverage()
    with store.connect() as db:
        parent, child = db.execute(
            "SELECT p.id,c.id FROM official_event_links l JOIN tasks p ON p.id=l.parent_task_id "
            "JOIN tasks c ON c.id=l.child_task_id WHERE p.dataset='TWT49U' LIMIT 1"
        ).fetchone()
        parent_snapshot = db.execute(
            "SELECT parent_snapshot_id FROM official_event_links WHERE parent_task_id=?", (parent,)
        ).fetchone()[0]
        db.execute(
            "INSERT INTO tasks(id,logical_key,edition,source,dataset,params,kind,state,"
            "snapshot_id) "
            "SELECT 'legacy-parent','legacy-parent-key',1,source,dataset,"
            "json_set(params,'$.lane','legacy'),kind,state,snapshot_id FROM tasks WHERE id=?",
            (parent,),
        )
        db.execute(
            "INSERT INTO official_event_links(parent_task_id,parent_snapshot_id,"
            "child_task_id,context) "
            "VALUES('legacy-parent',?,?, '{\"symbol\":\"2330\"}')",
            (parent_snapshot, child),
        )
        legacy_child = "legacy-child"
        db.execute(
            "INSERT INTO tasks(id,logical_key,edition,source,dataset,params,kind,state,"
            "snapshot_id) "
            "SELECT ?, 'legacy-child-key',1,'twse',dataset,json_set(params,'$.lane','quotes'),"
            "kind,state,snapshot_id FROM tasks WHERE id=?",
            (legacy_child, child),
        )
        db.execute(
            "INSERT INTO official_event_links(parent_task_id,parent_snapshot_id,"
            "child_task_id,context) "
            'VALUES(?,?,?,\'{"symbol":"2330"}\')',
            (parent, parent_snapshot, legacy_child),
        )
        finmind_raw = store.save_raw("finmind", b"retained-finmind-event-evidence")
        db.execute(
            "INSERT INTO snapshots VALUES('finmind-snapshot','finmind','TaiwanStockPrice','{}',"
            "'offline','legacy','2022-10-01T00:00:00Z',200,'finmind-hash',?,'valid',NULL)",
            (finmind_raw,),
        )
        db.execute(
            "INSERT INTO tasks(id,logical_key,edition,source,dataset,params,kind,state,"
            "snapshot_id) "
            "VALUES('finmind-event','finmind-key',1,'finmind','TaiwanStockPrice','{}','legacy','done',"
            "'finmind-snapshot')"
        )
    links = TaiwanData(tmp_path).event_links("2330")
    assert links
    assert all(link["parent"]["task_id"] != "legacy-parent" for link in links)
    assert all(link["child"]["task_id"] != "legacy-child" for link in links)
    coverage = TaiwanData(tmp_path).event_coverage()
    assert coverage["latest_parent_links"] == baseline["latest_parent_links"]
    assert coverage["detail_states"] == baseline["detail_states"]
    with store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM official_event_links").fetchone()[0] >= 3
        assert (
            db.execute("SELECT source FROM tasks WHERE id='finmind-event'").fetchone()[0]
            == "finmind"
        )
    assert (tmp_path / finmind_raw).read_bytes() == b"retained-finmind-event-evidence"
