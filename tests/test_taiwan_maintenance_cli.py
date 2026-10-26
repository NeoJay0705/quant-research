from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

from quant_research.taiwan import cli, official
from quant_research.taiwan.official_workflow import OfficialWorkflow
from quant_research.taiwan.storage import TaiwanData


def test_maintenance_cli_forwards_policy_and_exit_state(monkeypatch, tmp_path, capsys):
    calls = []

    class Workflow:
        def __init__(self, root):
            calls.append(("init", root))

        def maintain_events(self, *args, **kwargs):
            calls.append(("maintain", args, kwargs))
            return {"state": "partial", "count": 1}

    monkeypatch.setattr(cli, "OfficialWorkflow", Workflow)
    rc = cli.main(
        [
            "--root",
            str(tmp_path / "db"),
            "maintain-events",
            "--expected-date",
            "2026-10-24",
            "--max-tasks",
            "5",
            "--markets",
            "twse",
            "--recent-months",
            "3",
            "--detail-refresh-days",
            "9",
        ]
    )
    assert rc == 1
    assert json.loads(capsys.readouterr().out) == {"state": "partial", "count": 1}
    assert calls[1] == (
        "maintain",
        ("2026-10-24", 5),
        {"markets": ["twse"], "recent_months": 3, "detail_refresh_days": 9},
    )


def test_new_read_commands_route_through_taiwan_data(monkeypatch, tmp_path, capsys):
    class Data:
        def __init__(self, root):
            pass

        def observation_universe(self, date, **kwargs):
            return {"date": date, **kwargs}

        def availability(self, start, end, **kwargs):
            return {"start": start, "end": end, **kwargs}

    monkeypatch.setattr(cli, "TaiwanData", Data)
    assert cli.main(["--root", str(tmp_path), "observation-universe", "--date", "2026-10-24"]) == 0
    assert json.loads(capsys.readouterr().out) == {
        "date": "2026-10-24",
        "source": "official",
        "limit": 20000,
        "offset": 0,
    }
    assert (
        cli.main(
            [
                "--root",
                str(tmp_path),
                "availability",
                "--start",
                "2026-10-01",
                "--end",
                "2026-10-24",
                "--source",
                "twse",
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out) == {
        "start": "2026-10-01",
        "end": "2026-10-24",
        "source": "twse",
    }


def test_real_read_cli_missing_root_is_empty_success_without_creation(tmp_path, capsys):
    root = tmp_path / "missing-root"
    assert cli.main(["--root", str(root), "observation-universe", "--date", "2026-10-24"]) == 0
    universe = json.loads(capsys.readouterr().out)
    assert universe["members"] == [] and universe["classification"] == "unknown"
    assert (
        cli.main(
            ["--root", str(root), "availability", "--start", "2026-10-01", "--end", "2026-10-02"]
        )
        == 0
    )
    availability = json.loads(capsys.readouterr().out)
    assert availability["source_blocks"] == []
    assert root.exists() is False


def test_real_read_cli_rejects_wrong_schema_database(tmp_path, capsys):
    root = tmp_path / "wrong-schema"
    root.mkdir()
    with sqlite3.connect(root / "state.sqlite3") as db:
        db.execute("CREATE TABLE unrelated(value TEXT)")
    rc = cli.main(
        ["--root", str(root), "availability", "--start", "2026-10-01", "--end", "2026-10-02"]
    )
    captured = capsys.readouterr()
    assert rc == 1
    assert captured.err and "error" in captured.err.lower()


@pytest.mark.parametrize(
    "args",
    [
        ["maintain-events", "--recent-months", "13"],
        ["maintain-events", "--detail-refresh-days", "0"],
        ["observation-universe", "--date", "not-a-date"],
        ["observation-universe", "--date", "2026-10-24", "--source", "finmind"],
        ["availability", "--start", "2026-10-01", "--end", "2026-10-02", "--source", "finmind"],
    ],
)
def test_new_cli_invalid_arguments_do_not_create_root(tmp_path, monkeypatch, args, capsys):
    root = tmp_path / "no-io"

    try:
        result = cli.main(["--root", str(root), *args])
    except SystemExit as exc:
        assert exc.code == 2
    else:
        assert result == 2
    capsys.readouterr()
    assert not root.exists()


def test_maintenance_cli_and_read_reports_use_real_official_workflow(monkeypatch, tmp_path, capsys):
    root = tmp_path / "database"
    clock = [datetime(2018, 1, 3, tzinfo=UTC)]
    http_calls = []

    def callback(dataset, params):
        if dataset == "MI_INDEX":
            day = params["date"][0]
            return {
                "stat": "OK",
                "date": day,
                "tables": [
                    {
                        "title": "每日收盤行情",
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
                        "data": [["2330", "合成2330", "10", "11", "9", "10", "100", "1000"]],
                    }
                ],
            }
        if dataset == "TWT49UDetail":
            return {
                "stat": "OK",
                "fields": ["股票代號", "現金股利"],
                "data": [["2330", "1"]],
            }
        if dataset == "TWT49U":
            start = params["startDate"][0]
            end = params["endDate"][0]
            return {
                "stat": "OK",
                "strDate": start,
                "endDate": end,
                "fields": ["資料日期", "股票代號", "詳細資料", "現金股利"],
                "data": [["2018-01-02", "2330", "2330,20180102", "1"]],
            }
        return None

    def transport(url, headers, timeout, max_bytes):
        parsed = urlparse(url)
        dataset = parsed.path.rsplit("/", 1)[-1]
        params = parse_qs(parsed.query)
        http_calls.append((dataset, params))
        body = callback(dataset, params)
        if body is None:
            start = params.get("startDate", [""])[0].replace("/", "-")
            end = params.get("endDate", [""])[0].replace("/", "-")
            body = {
                "stat": "OK",
                "strDate": start.replace("-", ""),
                "endDate": end.replace("-", ""),
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

    def sleep(seconds):
        clock[0] += timedelta(seconds=seconds)

    workflow = OfficialWorkflow(root, transport=transport, now=lambda: clock[0], sleep=sleep)
    real_request = official.request

    def request(*args, **kwargs):
        return replace(real_request(*args, **kwargs), fetched_at=workflow.now().isoformat())

    monkeypatch.setattr(official, "request", request)
    backfill = workflow.backfill("2018-01-02", "2018-01-03", markets=["twse"])
    assert backfill["state"] == "completed", backfill
    assert len([call for call in http_calls if call[0] == "MI_INDEX"]) == 2

    def workflow_factory(cli_root):
        assert Path(cli_root) == root
        return workflow

    monkeypatch.setattr(cli, "OfficialWorkflow", workflow_factory)
    maintain_rc = cli.main(
        [
            "--root",
            str(root),
            "maintain-events",
            "--expected-date",
            "2018-01-03",
            "--markets",
            "twse",
            "--recent-months",
            "1",
        ]
    )
    maintain_output = json.loads(capsys.readouterr().out)
    assert maintain_rc == 1
    assert maintain_output["state"] == "partial"
    assert maintain_output["counts"]["claimed"] > 0

    with workflow.store.connect() as db:
        parents = db.execute(
            "SELECT tasks.id,tasks.state,tasks.dataset,tasks.snapshot_id FROM tasks "
            "LEFT JOIN snapshots s ON s.id=tasks.snapshot_id "
            "WHERE tasks.kind='official_event_summary' "
            "ORDER BY tasks.dataset"
        ).fetchall()
        children = db.execute(
            "SELECT tasks.id,tasks.state,tasks.dataset,tasks.snapshot_id,s.validation_state "
            "FROM tasks JOIN snapshots s ON s.id=tasks.snapshot_id "
            "WHERE tasks.dataset='TWT49UDetail'"
        ).fetchall()
        links = db.execute(
            "SELECT parent_task_id,parent_snapshot_id,child_task_id FROM official_event_links"
        ).fetchall()
    assert len(parents) == 3 and all(row["state"] == "completed" for row in parents)
    assert len(children) == 1 and children[0]["state"] == "completed"
    assert len(links) == 1
    assert (links[0]["parent_task_id"], links[0]["child_task_id"]) == (
        next(row["id"] for row in parents if row["dataset"] == "TWT49U"),
        children[0]["id"],
    )
    parent = next(row for row in parents if row["dataset"] == "TWT49U")
    assert links[0]["parent_snapshot_id"] == parent["snapshot_id"]
    assert children[0]["validation_state"] == "valid"
    with workflow.store.connect() as db:
        event = db.execute(
            "SELECT payload FROM records WHERE snapshot_id=? AND kind='event'",
            (children[0]["snapshot_id"],),
        ).fetchone()
    assert event is not None
    assert json.loads(event["payload"])["cash_dividend_per_share"] == "1"
    assert len([call for call in http_calls if call[0] == "TWT49UDetail"]) == 1

    calls_after_maintenance = len(http_calls)
    noop_rc = cli.main(
        [
            "--root",
            str(root),
            "maintain-events",
            "--expected-date",
            "2018-01-03",
            "--markets",
            "twse",
            "--recent-months",
            "1",
        ]
    )
    noop_output = json.loads(capsys.readouterr().out)
    assert noop_rc == 1
    assert noop_output["noop"] is True and noop_output["active"] is False
    assert len(http_calls) == calls_after_maintenance

    data = TaiwanData(root)
    expected_universe = data.observation_universe("2018-01-02")
    expected_availability = data.availability("2018-01-02", "2018-01-03")
    members = expected_universe["members"]
    assert members
    assert expected_universe["classification"] == "unknown"
    assert expected_universe["coverage_complete"] is False
    assert expected_universe["research_ready"] is False
    financial = expected_availability["financial_conditions"]
    assert financial["actions"]
    assert expected_availability["coverage_complete"] is False
    assert expected_availability["research_ready"] is False

    with workflow.store.connect() as db:
        for member in members:
            assert member["classification"] == "unknown"
            assert member["classification_evidence"] == []
            for ref in member["provenance"]:
                task = db.execute(
                    "SELECT t.id task_id,t.source,t.dataset,t.state,s.id snapshot_id,s.sha256,"
                    "s.raw_path "
                    "FROM tasks t JOIN snapshots s ON s.id=t.snapshot_id WHERE t.id=?",
                    (ref["task_id"],),
                ).fetchone()
                assert task is not None and task["state"] == "completed"
                expected_ref = {
                    key: ref[key]
                    for key in ("task_id", "source", "dataset", "snapshot_id", "sha256", "raw_path")
                }
                assert {key: task[key] for key in expected_ref} == expected_ref
                record = db.execute(
                    "SELECT id FROM records WHERE id=? AND snapshot_id=?",
                    (ref["record_id"], ref["snapshot_id"]),
                ).fetchone()
                assert record is not None

    universe_rc = cli.main(["--root", str(root), "observation-universe", "--date", "2018-01-02"])
    universe_output = json.loads(capsys.readouterr().out)
    availability_rc = cli.main(
        [
            "--root",
            str(root),
            "availability",
            "--start",
            "2018-01-02",
            "--end",
            "2018-01-03",
        ]
    )
    availability_output = json.loads(capsys.readouterr().out)
    assert universe_rc == availability_rc == 0
    assert universe_output == expected_universe
    assert availability_output == expected_availability
    assert len(http_calls) == calls_after_maintenance
