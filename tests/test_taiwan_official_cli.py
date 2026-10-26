from __future__ import annotations

import json

import pytest

from quant_research.taiwan import cli
from quant_research.taiwan.storage import SQLiteStore, TaiwanData


class FakeOfficialWorkflow:
    calls: list[tuple[str, object]] = []

    def __init__(self, root):
        self.store = SQLiteStore(root)

    def _save(self, command: str, **kwargs):
        self.calls.append((command, kwargs))
        self.store.initialize()
        self.store.save_snapshot(
            source="twse",
            dataset="MI_INDEX",
            params={},
            safe_url="offline",
            scope_key=command,
            fetched_at=f"2026-01-0{len(self.calls)}T00:00:00Z",
            http_status=200,
            sha256=command,
            raw_path="raw",
            validation_state="valid",
            records=(
                {
                    "kind": "quote",
                    "symbol": "2330",
                    "trade_date": "2026-01-01",
                    "venue_id": "primary",
                    "close": "10",
                },
            ),
        )
        return {"state": "completed", "run_id": command}

    def backfill(self, *args, **kwargs):
        return self._save("backfill", args=args, **kwargs)

    def sync(self, *args, **kwargs):
        return self._save("sync", args=args, **kwargs)

    def resume(self, *args, **kwargs):
        return self._save("resume", args=args, **kwargs)


@pytest.mark.parametrize("command", ["backfill", "sync", "resume"])
def test_cli_workflows_use_official_and_persist(tmp_path, monkeypatch, capsys, command):
    FakeOfficialWorkflow.calls = []
    monkeypatch.setattr(cli, "OfficialWorkflow", FakeOfficialWorkflow)
    monkeypatch.setenv("FINMIND_TOKEN", "poison-secret")
    args = ["--root", str(tmp_path), command]
    if command == "backfill":
        args += ["--start", "2026-01-01", "--end", "2026-01-01", "--max-tasks", "2"]
    assert cli.main(args) == 0
    assert json.loads(capsys.readouterr().out)["state"] == "completed"
    assert FakeOfficialWorkflow.calls[0][0] == command
    assert len(TaiwanData(tmp_path).quotes("2330", source="twse")) == len(
        FakeOfficialWorkflow.calls
    )
    with SQLiteStore(tmp_path).connect() as db:
        assert (
            db.execute("SELECT COUNT(*) FROM snapshots WHERE source='finmind'").fetchone()[0] == 0
        )


@pytest.mark.parametrize(
    "args", [["backfill", "--symbols", "2330"], ["sync", "--finmind-limit", "300"]]
)
def test_legacy_options_rejected_before_workflow_initialization(tmp_path, monkeypatch, args):
    def forbidden(*args, **kwargs):
        raise AssertionError("workflow must not initialize")

    monkeypatch.setattr(cli, "OfficialWorkflow", forbidden)
    root = tmp_path / "uncreated"
    with pytest.raises(SystemExit):
        cli.main(["--root", str(root), *args])
    assert not root.exists()


def test_resume_leaves_legacy_finmind_task_and_raw_untouched(tmp_path, capsys):
    root = tmp_path / "store"
    store = SQLiteStore(root)
    store.initialize()
    raw = root / "raw" / "finmind.bin"
    raw.parent.mkdir(parents=True)
    raw.write_bytes(b"legacy")
    with store.connect() as db:
        db.execute(
            "INSERT INTO tasks(id,logical_key,edition,source,params,kind,state) "
            "VALUES('legacy','legacy-key',1,'finmind','{}','history','pending')"
        )
    assert cli.main(["--root", str(root), "resume"]) == 0
    assert json.loads(capsys.readouterr().out)["legacy_tasks_ignored"] == 1
    with store.connect() as db:
        assert db.execute("SELECT state FROM tasks WHERE id='legacy'").fetchone()[0] == "pending"
    assert raw.read_bytes() == b"legacy"


def test_resume_refuses_identity_lane(tmp_path):
    root = tmp_path / "uncreated"
    assert cli.main(["--root", str(root), "resume", "--lane", "identity"]) == 2
    assert not root.exists()


def test_real_official_workflow_through_cli_with_fake_transport(tmp_path, monkeypatch, capsys):
    from datetime import UTC, datetime
    from urllib.parse import parse_qs, urlparse

    from quant_research.taiwan.official_workflow import OfficialWorkflow

    root = tmp_path / "real"
    urls = []
    running = []

    def transport(url, headers, timeout, max_bytes):
        urls.append(url)
        running.append(TaiwanData(root).status()["running"])
        parsed = urlparse(url)
        if parsed.hostname not in {"www.twse.com.tw", "www.tpex.org.tw"}:
            raise AssertionError(f"non-official URL: {url}")
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
        date_value = parse_qs(parsed.query)["date"][0]
        if "MI_INDEX" in parsed.path:
            return 200, json.dumps(
                {
                    "stat": "OK",
                    "date": date_value,
                    "tables": [
                        {
                            "fields": fields,
                            "data": [["2330", "台積電", "10", "11", "9", "10", "1", "10"]],
                        }
                    ],
                },
                ensure_ascii=False,
            ).encode()
        y, m, d = date_value.split("/")
        roc_date = f"{int(y) - 1911:03d}/{m}/{d}"
        return 200, json.dumps(
            {
                "stat": "ok",
                "date": roc_date,
                "fields": fields,
                "tables": [
                    {
                        "title": "每日收盤行情",
                        "fields": fields,
                        "data": [["6488", "環球晶", "10", "11", "9", "10", "1", "10"]],
                    }
                ],
            },
            ensure_ascii=False,
        ).encode()

    clock = [0]

    def now():
        from datetime import timedelta

        clock[0] += 3
        return datetime(2018, 1, 2, tzinfo=UTC) + timedelta(seconds=clock[0])

    def factory(path):
        return OfficialWorkflow(path, transport=transport, now=now, sleep=lambda _: None)

    monkeypatch.setattr(cli, "OfficialWorkflow", factory)
    monkeypatch.setenv("FINMIND_TOKEN", "FINMIND_POISON_DO_NOT_PERSIST")
    assert (
        cli.main(
            [
                "--root",
                str(root),
                "backfill",
                "--start",
                "2018-01-01",
                "--end",
                "2018-01-02",
                "--max-tasks",
                "1",
            ]
        )
        == 0
    )
    initial_result = json.loads(capsys.readouterr().out)
    assert initial_result["state"] == "paused", initial_result
    assert (
        cli.main(
            [
                "--root",
                str(root),
                "sync",
                "--expected-date",
                "2018-01-02",
                "--markets",
                "twse",
                "--max-tasks",
                "1",
            ]
        )
        == 0
    )
    capsys.readouterr()
    assert cli.main(["--root", str(root), "resume"]) == 0
    capsys.readouterr()
    assert urls and all("finmind" not in url.lower() for url in urls)
    assert running and all(running)
    quotes = TaiwanData(root).quotes("2330", source="twse")
    assert quotes
    with SQLiteStore(root).connect() as db:
        assert (
            db.execute("SELECT COUNT(*) FROM snapshots WHERE source='finmind'").fetchone()[0] == 0
        )
        assert (
            db.execute(
                "SELECT COUNT(*) FROM snapshots WHERE source IN ('twse','tpex') AND raw_path != ''"
            ).fetchone()[0]
            > 0
        )
        assert db.execute("SELECT COUNT(*) FROM tasks WHERE state='pending'").fetchone()[0] == 0
        assert db.execute("SELECT MAX(edition) FROM tasks").fetchone()[0] >= 2
        assert db.execute("SELECT COUNT(*) FROM tasks WHERE state='superseded'").fetchone()[0] > 0
        stored = json.dumps([tuple(row) for row in db.execute("SELECT * FROM snapshots")])
        stored += (
            json.dumps([tuple(row) for row in db.execute("SELECT * FROM raw_files")])
            if db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='raw_files'"
            ).fetchone()
            else ""
        )
        assert "FINMIND_POISON_DO_NOT_PERSIST" not in stored
        assert "FINMIND_POISON_DO_NOT_PERSIST" not in "\\n".join(db.iterdump())
    for path in (root / "raw").rglob("*"):
        if path.is_file():
            assert b"FINMIND_POISON_DO_NOT_PERSIST" not in path.read_bytes()


def test_cli_backfill_crosses_twse_no_date_empty_day(tmp_path, monkeypatch, capsys):
    from datetime import UTC, datetime
    from urllib.parse import parse_qs, urlparse

    from quant_research.taiwan.official_workflow import OfficialWorkflow

    root = tmp_path / "empty-day"
    urls = []

    def transport(url, headers, timeout, max_bytes):
        urls.append(url)
        date_arg = parse_qs(urlparse(url).query)["date"][0]
        if date_arg == "20180102":
            return 200, '{"stat":"很抱歉，沒有符合條件的資料!","type":"ALLBUT0999"}'.encode()
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
        return 200, json.dumps(
            {
                "stat": "OK",
                "date": date_arg,
                "tables": [
                    {
                        "fields": fields,
                        "data": [["2330", "台積電", "10", "11", "9", "10", "1", "10"]],
                    }
                ],
            },
            ensure_ascii=False,
        ).encode()

    clock = [0]

    def now():
        from datetime import timedelta

        clock[0] += 3
        return datetime(2018, 1, 3, tzinfo=UTC) + timedelta(seconds=clock[0])

    monkeypatch.setattr(
        cli,
        "OfficialWorkflow",
        lambda path: OfficialWorkflow(path, transport=transport, now=now, sleep=lambda _: None),
    )
    result = cli.main(
        [
            "--root",
            str(root),
            "backfill",
            "--start",
            "2018-01-01",
            "--end",
            "2018-01-03",
            "--markets",
            "twse",
        ]
    )
    assert result == 1
    output = json.loads(capsys.readouterr().out)
    assert output["state"] == "partial"
    assert result == 1
    assert [parse_qs(urlparse(url).query)["date"][0] for url in urls] == [
        "20180103",
        "20180102",
        "20180101",
    ]
    assert len(urls) == 3
    assert all("twse.com.tw" in url for url in urls)
    assert output["counts"]["completed"] == 3
    assert output["counts"]["empty_unverified"] == 1
    with SQLiteStore(root).connect() as db:
        empty = db.execute(
            "SELECT validation_state,error FROM snapshots WHERE scope_key LIKE '%2018-01-02'"
        ).fetchone()
        assert tuple(empty) == ("empty_unverified", "empty_unverified")
        for day in ("2018-01-01", "2018-01-03"):
            assert (
                db.execute(
                    "SELECT COUNT(*) FROM records WHERE kind='quote' AND record_date=?", (day,)
                ).fetchone()[0]
                == 1
            )
            assert (
                db.execute(
                    "SELECT COUNT(*) FROM snapshots WHERE validation_state='valid' "
                    "AND scope_key LIKE ?",
                    (f"%{day}",),
                ).fetchone()[0]
                == 1
            )
        assert (
            db.execute(
                "SELECT COUNT(*) FROM records WHERE kind='quote' AND record_date='2018-01-02'"
            ).fetchone()[0]
            == 0
        )
        assert (
            db.execute(
                "SELECT COUNT(*) FROM snapshots WHERE validation_state='valid' "
                "AND scope_key LIKE '%2018-01-02'"
            ).fetchone()[0]
            == 0
        )
