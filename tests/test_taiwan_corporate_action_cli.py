from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

from quant_research.taiwan.cli import main, parser
from quant_research.taiwan.official_workflow import OfficialWorkflow
from quant_research.taiwan.storage import SQLiteStore, TaiwanData


def test_missing_root_returns_empty_without_creating_it(tmp_path: Path) -> None:
    root = tmp_path / "missing"
    report = TaiwanData(root).corporate_actions()
    assert report["schema_version"] == "corporate-actions-v1"
    assert report["actions"] == []
    assert not root.exists()


def test_legacy_database_without_links_returns_empty_read_only(tmp_path: Path) -> None:
    root = tmp_path / "legacy"
    SQLiteStore(root).initialize()
    db_path = root / "state.sqlite3"
    body = json.dumps({"dataset": "TaiwanStockDividend", "data": []}).encode()
    raw_path = SQLiteStore(root).save_raw("finmind", body, "2022-11-01T00:00:00+00:00")
    with sqlite3.connect(db_path) as db:
        db.execute(
            "INSERT INTO snapshots VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "legacy-snapshot",
                "finmind",
                "TaiwanStockDividend",
                '{"data_id":"2330"}',
                "safe",
                "legacy-scope",
                "2022-11-01T00:00:00+00:00",
                200,
                hashlib.sha256(body).hexdigest(),
                raw_path,
                "valid",
                None,
            ),
        )
        db.execute(
            "INSERT INTO tasks(id,logical_key,edition,source,dataset,params,kind,state,"
            "snapshot_id) "
            "VALUES('legacy-task','legacy-scope',1,'finmind','TaiwanStockDividend',"
            "'{\"data_id\":\"2330\"}','legacy','completed','legacy-snapshot')"
        )
        db.execute(
            "INSERT INTO records(snapshot_id,kind,symbol,record_date,payload) "
            "VALUES('legacy-snapshot','event','2330','2022-10-03','{}')"
        )
        db.execute("DROP TABLE official_event_links")
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
    before = db_path.read_bytes()
    raw_before = (root / raw_path).read_bytes()
    with sqlite3.connect(db_path) as db:
        tables_before = sorted(
            db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        )
    rootfiles_before = sorted(path.name for path in root.iterdir())
    report = TaiwanData(root).corporate_actions()
    assert report["actions"] == []
    assert db_path.read_bytes() == before
    assert (root / raw_path).read_bytes() == raw_before
    with sqlite3.connect(db_path) as db:
        assert (
            sorted(db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall())
            == tables_before
        )
    assert sorted(path.name for path in root.iterdir()) == rootfiles_before


def test_marker_only_database_is_not_mistaken_for_legacy_schema(tmp_path: Path) -> None:
    root = tmp_path / "marker-only"
    root.mkdir()
    with sqlite3.connect(root / "state.sqlite3") as db:
        db.execute("CREATE TABLE marker (value TEXT)")
    with pytest.raises(sqlite3.DatabaseError, match="schema is incomplete"):
        TaiwanData(root).corporate_actions()


def test_validation_precedes_root_inspection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "absent"

    def forbidden(*args: object, **kwargs: object) -> bool:
        raise AssertionError("root inspected before argument validation")

    monkeypatch.setattr(Path, "exists", forbidden)
    for kwargs in (
        {"source": "finmind"},
        {"start": "2022-02-30"},
        {"start": "2022-02-02", "end": "2022-02-01"},
    ):
        with pytest.raises(ValueError):
            TaiwanData(root).corporate_actions(**kwargs)


def test_cli_has_command_and_rejects_invalid_dates_without_root(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert "corporate-actions" in parser()._subparsers._group_actions[0].choices
    root = tmp_path / "not-created"
    with pytest.raises(SystemExit) as error:
        main(["--root", str(root), "corporate-actions", "--source", "bad"])
    assert error.value.code == 2
    assert not root.exists()
    assert main(["--root", str(root), "corporate-actions", "--start", "2022-13-01"]) == 2
    assert not root.exists()
    captured = capsys.readouterr()
    assert captured.err


def test_cli_missing_database_outputs_report(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "not-created"
    assert main(["--root", str(root), "corporate-actions"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["actions"] == []
    assert not root.exists()


def test_workflow_sqlite_corporate_actions_cli_end_to_end_offline(tmp_path, monkeypatch, capsys):
    root = tmp_path / "real"
    clock = [datetime(2026, 10, 23, tzinfo=UTC)]
    urls = []
    callbacks = []

    def sleep(seconds):
        clock[0] += timedelta(seconds=seconds)

    def html(symbol, title, rows):
        body = "".join(f"<tr><td>{k}</td><td>{v}</td></tr>" for k, v in rows)
        return f"<table><tr><td>股票代號/證券名稱</td><td>{symbol}/{title}</td></tr>{body}</table>"

    def transport(url, headers, timeout, max_bytes):
        callbacks.append(url)
        urls.append(url)
        parsed = urlparse(url)
        dataset = parsed.path.rsplit("/", 1)[-1]
        q = parse_qs(parsed.query)
        if dataset in {"TWT49U", "TWTAUU", "TWTB8U"}:
            start = q["startDate"][0].replace("/", "-")
            end = q["endDate"][0].replace("/", "-")
            date, symbol = {
                "TWT49U": ("2022-10-03", "2330"),
                "TWTAUU": ("2022-10-11", "2409"),
                "TWTB8U": ("2022-10-13", "6415"),
            }[dataset]
            fields = (
                ["資料日期", "股票代號", "詳細資料", "除權息前收盤價", "除權息參考價"]
                if dataset == "TWT49U"
                else [
                    "恢復買賣日期",
                    "股票代號",
                    "停止買賣前收盤價格",
                    "恢復買賣參考價",
                    "詳細資料",
                ]
            )
            row = (
                [
                    date,
                    symbol,
                    "2330,20221003"
                    if dataset == "TWT49U"
                    else ("2409,20220928" if dataset == "TWTAUU" else "6415,20221006,20221013"),
                    "100",
                    "92",
                ]
                if dataset == "TWT49U"
                else [
                    date,
                    symbol,
                    "14.7" if dataset == "TWTAUU" else "2485",
                    "15.87" if dataset == "TWTAUU" else "621.25",
                    "2409,20220928" if dataset == "TWTAUU" else "6415,20221006,20221013",
                ]
            )
            doc = {"stat": "OK", "strDate": start, "endDate": end, "fields": fields, "data": [row]}
        elif dataset == "TWT49UDetail":
            doc = {
                "stat": "OK",
                "fields": [
                    "股票代號",
                    "(每股配發現金股利)除息",
                    "A. 按普通股股東持股比例每千股無償配股",
                    "B. 員工紅利轉增資",
                    "C. (有償) 現金增資",
                ],
                "data": [[q["STK_NO"][0], "8", "0", "0", "0"]],
            }
        elif dataset == "TWTAVUDetail":
            doc = {
                "stat": "OK",
                "fields": [
                    "股票代號",
                    "每壹仟股換發新股票",
                    "每股退還股款",
                    "原股每股配發現金股利",
                    "停止買賣日期",
                ],
                "data": [[q["STK_NO"][0], "800", "2", "0", "2022-09-29"]],
            }
        else:
            rocstart, rocend = "111/10/01", "111/10/31"
            rows = {
                "exDailyQ": (
                    [
                        "除權息日期",
                        "代號",
                        "股票名稱",
                        "除權息前收盤價",
                        "除權息參考價",
                        "現金股利",
                        "每仟股無償配股",
                    ],
                    ["111/10/04", "3105", "穩懋", "217", "210", "7", "0"],
                ),
                "revivt": (
                    [
                        "恢復買賣日期",
                        "股票代號",
                        "證券名稱",
                        "最後交易日之收盤價格",
                        "減資恢復買賣開始日參考價格",
                        "詳細資料",
                    ],
                    [
                        "111/10/15",
                        "6207",
                        "雷科",
                        "25.25",
                        "26.05",
                        html(
                            "6207",
                            "雷科",
                            [
                                ("每壹仟股換發新股票", "950"),
                                ("每股退還股款", "0.5"),
                                ("原股每股配發現金股利", "0"),
                                ("停止買賣日期", "2022-10-05"),
                            ],
                        ),
                    ],
                ),
                "pvChgRslt": (
                    [
                        "恢復買賣日期",
                        "證券代號",
                        "證券名稱",
                        "最後交易日之收盤價格",
                        "恢復買賣開始參考價",
                        "詳細資料",
                    ],
                    [
                        "111/10/17",
                        "6613",
                        "朋億",
                        "169",
                        "84.5",
                        html(
                            "6613",
                            "朋億",
                            [
                                ("每股換發新股票", "2"),
                                ("變更前面額", "10"),
                                ("變更後面額", "5"),
                                ("停止買賣日期", "2022-10-07"),
                            ],
                        ),
                    ],
                ),
            }[dataset]
            doc = {
                "stat": "ok",
                "date": f"{rocstart}~{rocend}",
                "fields": rows[0],
                "data": [rows[1]],
            }
        return 200, json.dumps(doc, ensure_ascii=False).encode()

    monkeypatch.setenv("FINMIND_TOKEN", "poison-token-must-not-be-used")

    def forbidden_http(*args, **kwargs):
        raise AssertionError("real HTTP is forbidden")

    monkeypatch.setattr("urllib.request.urlopen", forbidden_http)
    monkeypatch.setattr("urllib.request.build_opener", forbidden_http)
    workflow = OfficialWorkflow(root, transport=transport, now=lambda: clock[0], sleep=sleep)
    summary = workflow.sync_events("2022-10-01", "2022-10-31")
    assert summary["counts"]["completed"] == 8, summary
    assert len(urls) == len(callbacks) == 8
    with workflow.store.connect() as db:
        assert (
            db.execute(
                "SELECT COUNT(*) FROM tasks WHERE kind='official_event_summary' "
                "AND state='completed'"
            ).fetchone()[0]
            == 6
        )
        assert (
            db.execute(
                "SELECT COUNT(*) FROM tasks WHERE kind='official_event_detail' "
                "AND state='completed'"
            ).fetchone()[0]
            == 2
        )
        snapshots = db.execute("SELECT * FROM snapshots").fetchall()
        assert len(snapshots) == 8
        assert all(row["validation_state"] == "valid" for row in snapshots)
        assert all(json.loads(row["params"]).get("request") for row in snapshots)
    # No HTTP transport or raw-file reads are allowed after acquisition.
    monkeypatch.setattr(
        workflow, "transport", lambda *args: (_ for _ in ()).throw(AssertionError("HTTP"))
    )
    read_bytes_calls = []
    monkeypatch.setattr(
        Path,
        "read_bytes",
        lambda *args: (
            read_bytes_calls.append(args) or (_ for _ in ()).throw(AssertionError("raw read"))
        ),
    )
    api = TaiwanData(root).corporate_actions()
    assert len(api["actions"]) == 6
    assert {a["source"] for a in api["actions"]} == {"twse", "tpex"}
    assert len(TaiwanData(root).corporate_actions(source="twse")["actions"]) == 3
    assert len(TaiwanData(root).corporate_actions(source="tpex")["actions"]) == 3
    assert len(TaiwanData(root).corporate_actions(symbol="2330")["actions"]) == 1
    bounded = TaiwanData(root).corporate_actions(start="2022-10-11", end="2022-10-15")
    assert len(bounded["actions"]) == 3
    twse_bounded = TaiwanData(root).corporate_actions(
        source="twse", start="2022-10-11", end="2022-10-15"
    )
    assert len(twse_bounded["actions"]) == 2
    expected = {
        ("twse", "2330", "2022-10-03"): ("dividend", "8", "1"),
        ("twse", "2409", "2022-10-11"): ("capital_reduction", "0", "0.8"),
        ("twse", "6415", "2022-10-13"): ("par_value", None, None),
        ("tpex", "3105", "2022-10-04"): ("dividend", "7", "1"),
        ("tpex", "6207", "2022-10-15"): ("capital_reduction", "0", "0.95"),
        ("tpex", "6613", "2022-10-17"): ("par_value", None, "2"),
    }
    actual = {(a["source"], a["symbol"], a["event_date"]): a for a in api["actions"]}
    assert set(actual) == set(expected)

    def flatten_refs(refs, result):
        for ref in refs:
            if ref.get("derived"):
                flatten_refs(ref["basis"], result)
            else:
                result.append(ref)

    for key, (kind, cash, multiplier) in expected.items():
        action = actual[key]
        assert action["event_type"] == kind
        assert action["cash_dividend_per_share"] == cash
        assert action["share_multiplier"] == multiplier
        if key != ("twse", "6415", "2022-10-13"):
            assert action["reference_check"]["status"] == "match"
        flattened_refs = []
        for refs in action["field_provenance"].values():
            flatten_refs(refs, flattened_refs)
        assert flattened_refs
        for ref in flattened_refs:
            assert ref["snapshot_id"] and ref["task_id"] and ref["sha256"] and ref["raw_path"]
        with workflow.store.connect() as db:
            stored = {
                (row["task_id"], row["snapshot_id"]): row
                for row in db.execute(
                    "SELECT t.id AS task_id,t.source,t.dataset,t.snapshot_id,"
                    "s.sha256,s.raw_path FROM tasks t "
                    "JOIN snapshots s ON s.id=t.snapshot_id"
                )
            }
        for ref in flattened_refs:
            record = stored[(ref["task_id"], ref["snapshot_id"])]
            assert (ref["source"], ref["dataset"], ref["sha256"], ref["raw_path"]) == (
                record["source"],
                record["dataset"],
                record["sha256"],
                record["raw_path"],
            )
    assert actual[("twse", "6415", "2022-10-13")]["input_state"] == "incomplete"
    assert all(a["input_state"] == "complete" for key, a in actual.items() if key[0] == "tpex")
    assert sum(a["reference_check"]["status"] == "match" for a in api["actions"]) == 5
    callbacks_after_api = len(callbacks)
    reads_after_api = len(read_bytes_calls)
    assert main(["--root", str(root), "corporate-actions"]) == 0
    cli_report = json.loads(capsys.readouterr().out)
    assert cli_report == api
    assert len(callbacks) == callbacks_after_api == 8
    assert len(read_bytes_calls) == reads_after_api == 0
