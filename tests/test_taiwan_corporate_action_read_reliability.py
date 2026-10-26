from __future__ import annotations

import builtins
import hashlib
import io
import json
import socket
import sqlite3
import urllib.request
from contextlib import nullcontext
from pathlib import Path
from typing import Any

import pytest

from quant_research.taiwan import official
from quant_research.taiwan.cli import main as cli_main
from quant_research.taiwan.sources import RawBatch
from quant_research.taiwan.storage import SQLiteStore, TaiwanData

REQUEST = {"start_date": "2022-10-01", "end_date": "2022-10-31"}
TASK_PARAMS = {"pipeline": "official-v1", "lane": "events", "request": REQUEST}
FETCHED = "2022-11-01T00:00:00+00:00"


def _body(symbol: str = "2330", cash: str = "8", reference: str = "92") -> bytes:
    return json.dumps(
        {
            "stat": "ok",
            "date": "111/10/01~111/10/31",
            "fields": [
                "除權息日期",
                "代號",
                "股票名稱",
                "除權息前收盤價",
                "除權息參考價",
                "現金股利",
                "每仟股無償配股",
            ],
            "data": [["111/10/03", symbol, "測試", "100", reference, cash, "0"]],
        },
        ensure_ascii=False,
    ).encode()


def _add_edition(
    store: SQLiteStore,
    *,
    edition: int,
    symbol: str = "2330",
    cash: str = "8",
    reference: str = "92",
    state: str = "completed",
    validation: str = "valid",
    snapshot_source: str = "tpex",
    snapshot_dataset: str = "exDailyQ",
    snapshot_params: dict[str, object] | str | None = None,
    task_params: dict[str, object] | None = None,
    scope: str = "opaque-logical-scope-v1",
    connection: sqlite3.Connection | None = None,
) -> tuple[str | None, bytes | None]:
    params = {"start_date": REQUEST["start_date"], "end_date": REQUEST["end_date"]}
    body = _body(symbol, cash, reference)
    parsed = official.parse(
        RawBatch(
            "tpex",
            "exDailyQ",
            "https://www.tpex.org.tw/www/zh-tw/bulletin/exDailyQ",
            FETCHED,
            200,
            body,
            hashlib.sha256(body).hexdigest(),
            0.0,
        ),
        params,
    )
    with nullcontext(connection) if connection is not None else store.connect() as db:
        snapshot_id: str | None = None
        raw_path = ""
        if state != "failed":
            raw_path = store.save_raw("tpex", body, FETCHED)
            snapshot_values = TASK_PARAMS if snapshot_params is None else snapshot_params
            encoded = (
                snapshot_values if isinstance(snapshot_values, str) else json.dumps(snapshot_values)
            )
            snapshot_id = f"snapshot-{edition}-{scope}"
            db.execute(
                "INSERT INTO snapshots VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    snapshot_id,
                    snapshot_source,
                    snapshot_dataset,
                    encoded,
                    "https://www.tpex.org.tw/www/zh-tw/bulletin/exDailyQ",
                    scope,
                    FETCHED,
                    200,
                    hashlib.sha256(body).hexdigest(),
                    raw_path,
                    validation,
                    None,
                ),
            )
            if state == "completed" and validation == "valid":
                for record in parsed.records:
                    db.execute(
                        "INSERT INTO records(snapshot_id,kind,symbol,record_date,record_key,"
                        "payload,quality_flags) "
                        "VALUES(?,?,?,?,?,?,?)",
                        (
                            snapshot_id,
                            "event",
                            record.get("symbol"),
                            record.get("event_date"),
                            f"{record.get('symbol')}:{record.get('event_date')}",
                            json.dumps(record, ensure_ascii=False),
                            json.dumps(record.get("quality_flags", [])),
                        ),
                    )
        task_json = TASK_PARAMS if task_params is None else task_params
        db.execute(
            "INSERT INTO tasks(id,logical_key,edition,source,dataset,params,kind,state,"
            "snapshot_id) VALUES(?,?,?,?,?,?,?,?,?)",
            (
                f"task-{edition}-{scope}",
                scope,
                edition,
                "tpex",
                "exDailyQ",
                json.dumps(task_json, ensure_ascii=False),
                "official_event_summary",
                "completed" if state == "empty" else state,
                snapshot_id,
            ),
        )
    return snapshot_id, body if snapshot_id else None


@pytest.fixture
def seeded(tmp_path: Path) -> tuple[SQLiteStore, Path, str, bytes]:
    root = tmp_path / "data"
    store = SQLiteStore(root)
    store.initialize()
    snapshot_id, body = _add_edition(store, edition=1)
    assert snapshot_id is not None and body is not None
    return store, root, snapshot_id, body


def _action(report: dict[str, Any], symbol: str = "2330") -> dict[str, Any]:
    return next(action for action in report["actions"] if action["symbol"] == symbol)


def test_refresh_keeps_versions_and_source_scope_conflicts(seeded) -> None:
    store, root, old_snapshot, old_body = seeded
    with store.connect() as db:
        old_raw_path, old_hash = db.execute(
            "SELECT raw_path,sha256 FROM snapshots WHERE id=?", (old_snapshot,)
        ).fetchone()
        before = {
            table: db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("tasks", "snapshots", "records")
        }
    _add_edition(store, edition=2, cash="9", reference="91")
    report = TaiwanData(root).corporate_actions()
    action = _action(report)
    assert action["cash_dividend_per_share"] == "9"
    assert action["reference_price"] == "91"
    assert {ref["task_id"] for ref in action["field_provenance"]["cash_dividend_per_share"]} == {
        "task-2-opaque-logical-scope-v1"
    }
    with store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == before["tasks"] + 1
        assert db.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0] == before["snapshots"] + 1
        assert db.execute("SELECT COUNT(*) FROM records").fetchone()[0] == before["records"] + 1
        assert tuple(
            db.execute(
                "SELECT sha256,raw_path FROM snapshots WHERE id=?", (old_snapshot,)
            ).fetchone()
        ) == (old_hash, old_raw_path)
        assert (
            db.execute(
                "SELECT COUNT(*) FROM tasks WHERE id='task-1-opaque-logical-scope-v1'"
            ).fetchone()[0]
            == 1
        )
    assert (root / old_raw_path).read_bytes() == old_body
    assert hashlib.sha256(old_body).hexdigest() == old_hash
    assert action["input_state"] == "complete"
    assert action["reference_check"]["status"] == "match"
    assert action["reference_price"] == "91"
    versions = {v["snapshot_id"]: v["current"] for v in action["scope_versions"]}
    assert versions == {old_snapshot: False, "snapshot-2-opaque-logical-scope-v1": True}
    old_history = [h for h in action["field_history"] if h["snapshot_id"] == old_snapshot]
    assert old_history and all(not h["current"] for h in old_history)
    assert all(h["sha256"] == hashlib.sha256(old_body).hexdigest() for h in old_history)

    # The same source/symbol/date in a different current logical scope is a conflict.
    _add_edition(store, edition=1, cash="10", reference="90", scope="second-scope")
    conflict = _action(TaiwanData(root).corporate_actions())
    assert conflict["cash_dividend_per_share"] is None
    assert "conflicting_source_fields" in conflict["quality_flags"]


def test_latest_invalid_or_empty_edition_never_revives_old_data(seeded) -> None:
    store, root, _, _ = seeded
    _add_edition(store, edition=2, symbol="2317")
    _add_edition(store, edition=3, state="failed")
    report = TaiwanData(root).corporate_actions(symbol="2330")
    assert report["actions"] == []
    assert report["diagnostics"]["stale_scopes"]

    for latest_state, latest_validation in (
        ("pending", "valid"),
        ("empty", "empty_unverified"),
        ("failed", "valid"),
    ):
        isolated = SQLiteStore(root.parent / f"only-{latest_state}")
        isolated.initialize()
        _add_edition(isolated, edition=1)
        _add_edition(
            isolated,
            edition=2,
            state=latest_state,
            validation=latest_validation,
        )
        stale = TaiwanData(isolated.root).corporate_actions()
        assert _action(stale)["input_state"] == "stale"

    no_history = SQLiteStore(root.parent / "no-history")
    no_history.initialize()
    _add_edition(no_history, edition=1, state="failed")
    assert TaiwanData(no_history.root).corporate_actions()["actions"] == []

    other = SQLiteStore(root.parent / "only-other")
    other.initialize()
    _add_edition(other, edition=1)
    _add_edition(other, edition=2, symbol="2317")
    no_match = TaiwanData(other.root).corporate_actions(symbol="2330")
    assert no_match["actions"] == []
    assert no_match["diagnostics"]["superseded_observations"]


def test_finmind_quotes_and_official_other_source_cannot_pollute_reader(
    seeded, monkeypatch
) -> None:
    store, root, snapshot_id, body = seeded
    with store.connect() as db:
        for source, dataset, task_id, snap_id, record_id, params in (
            (
                "finmind",
                "TaiwanStockDividend",
                "finmind-task",
                "finmind-snapshot",
                999,
                TASK_PARAMS,
            ),
            ("tpex", "dailyQuotes", "quote-task", "quote-snapshot", 888, TASK_PARAMS),
        ):
            raw_path = store.save_raw(source, body, FETCHED)
            db.execute(
                "INSERT INTO snapshots VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    snap_id,
                    source,
                    dataset,
                    json.dumps(params),
                    "safe",
                    "rogue",
                    FETCHED,
                    200,
                    hashlib.sha256(body).hexdigest(),
                    raw_path,
                    "valid",
                    None,
                ),
            )
            db.execute(
                "INSERT INTO tasks(id,logical_key,edition,source,dataset,params,kind,state,"
                "snapshot_id) VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    task_id,
                    task_id,
                    1,
                    source,
                    dataset,
                    json.dumps(params),
                    "official_event_summary",
                    "completed",
                    snap_id,
                ),
            )
            db.execute(
                "INSERT INTO records(id,snapshot_id,kind,symbol,record_date,payload) "
                "VALUES(?,?,?,?,?,?)",
                (
                    record_id,
                    snap_id,
                    "event",
                    "2330",
                    "2022-10-03",
                    json.dumps({"cash_dividend_per_share": "999"}),
                ),
            )
        # These valid TPEX records differ only in logical namespace metadata.
        for suffix, rogue_params in (
            ("wrong-pipeline", {"pipeline": "wrong", "lane": "events", "request": REQUEST}),
            ("wrong-lane", {"pipeline": "official-v1", "lane": "quotes", "request": REQUEST}),
        ):
            rogue_body = _body(cash="999", reference="1")
            parsed = official.parse(
                RawBatch(
                    "tpex",
                    "exDailyQ",
                    "https://www.tpex.org.tw/www/zh-tw/bulletin/exDailyQ",
                    FETCHED,
                    200,
                    rogue_body,
                    hashlib.sha256(rogue_body).hexdigest(),
                    0.0,
                ),
                REQUEST,
            )
            snap_id = f"{suffix}-snapshot"
            task_id = f"{suffix}-task"
            raw_path = store.save_raw("tpex", rogue_body, FETCHED)
            db.execute(
                "INSERT INTO snapshots VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    snap_id,
                    "tpex",
                    "exDailyQ",
                    json.dumps(rogue_params),
                    "safe",
                    suffix,
                    FETCHED,
                    200,
                    hashlib.sha256(rogue_body).hexdigest(),
                    raw_path,
                    "valid",
                    None,
                ),
            )
            db.execute(
                "INSERT INTO tasks(id,logical_key,edition,source,dataset,params,kind,state,"
                "snapshot_id) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    task_id,
                    suffix,
                    1,
                    "tpex",
                    "exDailyQ",
                    json.dumps(rogue_params),
                    "official_event_summary",
                    "completed",
                    snap_id,
                ),
            )
            for record in parsed.records:
                db.execute(
                    "INSERT INTO records(snapshot_id,kind,symbol,record_date,record_key,"
                    "payload,quality_flags) "
                    "VALUES(?,?,?,?,?,?,?)",
                    (
                        snap_id,
                        "event",
                        record.get("symbol"),
                        record.get("event_date"),
                        f"{record.get('symbol')}:{record.get('event_date')}",
                        json.dumps(record, ensure_ascii=False),
                        json.dumps(record.get("quality_flags", [])),
                    ),
                )
        # Official is not a permitted task source; similarly-shaped data must remain isolated.
        db.execute(
            "INSERT INTO snapshots VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "official-wrong",
                "official",
                "exDailyQ",
                json.dumps(TASK_PARAMS),
                "safe",
                "rogue",
                FETCHED,
                200,
                "x",
                "missing.bin",
                "valid",
                None,
            ),
        )
        db.execute(
            "INSERT INTO tasks(id,logical_key,edition,source,dataset,params,kind,state,"
            "snapshot_id) VALUES(?,?,?,?,?,?,?,?,?)",
            (
                "official-task",
                "official-scope",
                1,
                "tpex",
                "exDailyQ",
                json.dumps(TASK_PARAMS),
                "official_event_summary",
                "completed",
                "official-wrong",
            ),
        )
    monkeypatch.setenv("FINMIND_TOKEN", "toxic-secret-must-never-be-persisted")

    def forbidden(*args: object, **kwargs: object) -> Any:
        raise AssertionError("reader attempted network or raw-file access")

    monkeypatch.setattr("urllib.request.urlopen", forbidden)
    monkeypatch.setattr(Path, "read_bytes", forbidden)
    report = TaiwanData(root).corporate_actions()
    assert _action(report)["cash_dividend_per_share"] == "8"
    assert _action(report)["input_state"] == "complete"
    assert len(report["actions"]) == 1
    assert (
        _action(TaiwanData(root).corporate_actions(source="tpex"))["cash_dividend_per_share"] == "8"
    )
    assert TaiwanData(root).corporate_actions(source="twse")["actions"] == []
    with store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM records WHERE id IN (888,999)").fetchone()[0] == 2
        assert (
            db.execute(
                "SELECT COUNT(*) FROM tasks WHERE id IN "
                "('finmind-task','quote-task','wrong-pipeline-task','wrong-lane-task','official-task')"
            ).fetchone()[0]
            == 5
        )
        assert (
            db.execute("SELECT COUNT(*) FROM tasks WHERE params LIKE '%toxic-secret%'").fetchone()[
                0
            ]
            == 0
        )
    assert body == _body()


@pytest.mark.parametrize(
    ("bad_params", "snapshot_source", "snapshot_dataset"),
    [
        ({"pipeline": "wrong", "lane": "events", "request": REQUEST}, "tpex", "exDailyQ"),
        (
            {
                "pipeline": "official-v1",
                "lane": "events",
                "request": {"start_date": "2022-09-01", "end_date": "2022-10-31"},
            },
            "tpex",
            "exDailyQ",
        ),
        ("{bad json", "tpex", "exDailyQ"),
        (TASK_PARAMS, "twse", "exDailyQ"),
        (TASK_PARAMS, "tpex", "TWT49U"),
    ],
)
def test_latest_snapshot_lineage_mismatch_cannot_fall_back(
    seeded, bad_params, snapshot_source, snapshot_dataset
) -> None:
    store, root, _, _ = seeded
    _add_edition(
        store,
        edition=2,
        snapshot_params=bad_params,
        snapshot_source=snapshot_source,
        snapshot_dataset=snapshot_dataset,
    )
    report = TaiwanData(root).corporate_actions()
    action = _action(report)
    assert action["input_state"] == "stale"
    assert action["cash_dividend_per_share"] == "8"
    assert "snapshot_provenance_mismatch" in json.dumps(report["diagnostics"])
    assert "task-2-opaque-logical-scope-v1" not in json.dumps(action["field_provenance"])


def test_snapshot_context_and_json_key_order_do_not_break_lineage(seeded) -> None:
    store, root, _, _ = seeded
    snapshot_params = {
        "context": {"run": "new"},
        "request": {"end_date": "2022-10-31", "start_date": "2022-10-01"},
        "lane": "events",
        "pipeline": "official-v1",
    }
    _add_edition(store, edition=2, cash="9", snapshot_params=snapshot_params)
    report = TaiwanData(root).corporate_actions()
    assert _action(report)["cash_dividend_per_share"] == "9"
    assert "snapshot_provenance_mismatch" not in json.dumps(report["diagnostics"])


def test_read_is_single_readonly_transaction_during_concurrent_wal_refresh(
    seeded, monkeypatch
) -> None:
    store, root, _, _ = seeded
    original_connect = sqlite3.connect
    trace: list[str] = []
    reader_connections: list[sqlite3.Connection] = []
    readonly_uris: list[str] = []
    writer_added = False

    class TrackedConnection(sqlite3.Connection):
        def execute(self, sql: str, parameters: object = (), /):
            nonlocal writer_added
            if "FROM tasks t LEFT JOIN snapshots" in sql and not writer_added:
                writer_added = True
                _add_edition(store, edition=2, cash="9", reference="91")
            return super().execute(sql, parameters)

    def connect(database: object, *args: object, **kwargs: object) -> sqlite3.Connection:
        if kwargs.get("uri"):
            connection = original_connect(database, *args, factory=TrackedConnection, **kwargs)
            connection.set_trace_callback(trace.append)
            reader_connections.append(connection)
            readonly_uris.append(str(database))
            return connection
        return original_connect(database, *args, **kwargs)

    monkeypatch.setattr("quant_research.taiwan.storage.sqlite3.connect", connect)
    first = TaiwanData(root).corporate_actions()
    assert _action(first)["cash_dividend_per_share"] == "8"
    assert writer_added
    assert len(reader_connections) == 1
    assert any(statement == "BEGIN" for statement in trace)
    assert len(set(map(id, reader_connections))) == 1
    assert "mode=ro" in readonly_uris[0]
    second = TaiwanData(root).corporate_actions()
    assert _action(second)["cash_dividend_per_share"] == "9"
    assert all(
        ref["task_id"] == "task-2-opaque-logical-scope-v1"
        for ref in _action(second)["field_provenance"]["cash_dividend_per_share"]
    )


def _business_fingerprint_from_connection(
    db: sqlite3.Connection,
) -> tuple[tuple[str, tuple[tuple[object, ...], ...]], ...]:
    tables = [
        str(row[0])
        for row in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' "
            "ORDER BY name"
        )
    ]
    return tuple(
        (table, tuple(tuple(row) for row in db.execute(f'SELECT * FROM "{table}" ORDER BY rowid')))
        for table in tables
    )


def _main_business_fingerprint(
    root: Path,
) -> tuple[tuple[str, tuple[tuple[object, ...], ...]], ...]:
    """Read the closed, checkpointed main DB into memory without opening its WAL sidecars."""
    memory = sqlite3.connect(":memory:")
    try:
        rollback_image = bytearray((root / "state.sqlite3").read_bytes())
        # Open a normalized in-memory copy in rollback mode, without touching target sidecars.
        rollback_image[18:20] = b"\x01\x01"
        memory.deserialize(rollback_image)
        return _business_fingerprint_from_connection(memory)
    finally:
        memory.close()


def _files_except_sqlite_sidecars(root: Path) -> dict[str, str]:
    excluded = {"state.sqlite3-wal", "state.sqlite3-shm"}
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file() and path.relative_to(root).as_posix() not in excluded
    }


def _install_reader_guards(monkeypatch: pytest.MonkeyPatch, root: Path):
    original_connect = sqlite3.connect
    original_io_open = io.open
    connections: list[sqlite3.Connection] = []
    closed: list[bool] = []
    traces: list[list[str]] = []
    uris: list[str] = []

    class ReaderConnection(sqlite3.Connection):
        def close(self) -> None:
            closed[connections.index(self)] = True
            super().close()

    def guarded_connect(database: object, *args: object, **kwargs: object) -> sqlite3.Connection:
        uri = str(database)
        if not kwargs.get("uri") or "mode=ro" not in uri or "immutable=1" in uri:
            raise AssertionError(f"reader used a non-readonly or immutable SQLite URI: {uri}")
        trace: list[str] = []
        connection = original_connect(database, *args, factory=ReaderConnection, **kwargs)
        connection.set_trace_callback(trace.append)
        connections.append(connection)
        closed.append(False)
        traces.append(trace)
        uris.append(uri)
        return connection

    def forbidden(*args: object, **kwargs: object) -> Any:
        raise AssertionError("corporate-action reader attempted a forbidden operation")

    monkeypatch.setattr("quant_research.taiwan.storage.sqlite3.connect", guarded_connect)
    for name in ("initialize", "writer_lock", "connect", "save_raw", "save_snapshot"):
        monkeypatch.setattr(SQLiteStore, name, forbidden)
    monkeypatch.setattr("urllib.request.urlopen", forbidden)
    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)

    def guarded_path_open(path: Path, mode: str = "r", *args: object, **kwargs: object):
        if path.is_relative_to(root) and any(flag in mode for flag in "wax+"):
            raise AssertionError(f"reader attempted direct root write: {path}")
        if path.is_relative_to(root / "raw"):
            raise AssertionError(f"reader attempted raw-file access: {path}")
        return original_path_open(path, mode, *args, **kwargs)

    original_path_open = Path.open
    original_read_bytes = Path.read_bytes
    original_read_text = Path.read_text
    original_builtin_open = builtins.open
    original_mkdir = Path.mkdir

    def guarded_read(path: Path, *args: object, **kwargs: object):
        if path.is_relative_to(root / "raw"):
            raise AssertionError(f"reader attempted raw-file read: {path}")
        return original_read_bytes(path, *args, **kwargs)

    def guarded_read_text(path: Path, *args: object, **kwargs: object):
        if path.is_relative_to(root / "raw"):
            raise AssertionError(f"reader attempted raw-file read: {path}")
        return original_read_text(path, *args, **kwargs)

    def guard_file_access(file: object, mode: str) -> None:
        if isinstance(file, (str, Path)):
            path = Path(file)
            if path.is_relative_to(root) and any(flag in mode for flag in "wax+"):
                raise AssertionError(f"reader attempted direct root write: {path}")
            if path.is_relative_to(root / "raw"):
                raise AssertionError(f"reader attempted raw-file access: {path}")

    def guarded_builtin_open(file: object, mode: str = "r", *args: object, **kwargs: object):
        guard_file_access(file, mode)
        return original_builtin_open(file, mode, *args, **kwargs)

    def guarded_io_open(file: object, mode: str = "r", *args: object, **kwargs: object):
        guard_file_access(file, mode)
        return original_io_open(file, mode, *args, **kwargs)

    def guarded_opener_open(
        self: urllib.request.OpenerDirector, fullurl: object, *args: object, **kwargs: object
    ):
        raise AssertionError("corporate-action reader attempted network access")

    def guarded_mkdir(path: Path, *args: object, **kwargs: object) -> None:
        if path.is_relative_to(root):
            raise AssertionError(f"reader attempted root directory creation: {path}")
        original_mkdir(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", guarded_path_open)
    monkeypatch.setattr(Path, "read_bytes", guarded_read)
    monkeypatch.setattr(Path, "read_text", guarded_read_text)
    monkeypatch.setattr(Path, "mkdir", guarded_mkdir)
    monkeypatch.setattr(builtins, "open", guarded_builtin_open)
    monkeypatch.setattr(io, "open", guarded_io_open)
    monkeypatch.setattr(urllib.request.OpenerDirector, "open", guarded_opener_open)
    return connections, closed, traces, uris


def test_fresh_wal_bootstrap_read_keeps_business_state_and_files(
    seeded, monkeypatch, capsys
) -> None:
    store, root, snapshot_id, body = seeded
    db_path = root / "state.sqlite3"
    main_before = db_path.read_bytes()
    assert main_before[18:20] == b"\x02\x02"  # SQLite header: WAL read/write versions.
    assert not (db_path.parent / "state.sqlite3-wal").exists()
    assert not (db_path.parent / "state.sqlite3-shm").exists()
    before_files = _files_except_sqlite_sidecars(root)
    assert set(before_files) == {
        "state.sqlite3",
        *[
            path.relative_to(root).as_posix()
            for path in (root / "raw").rglob("*")
            if path.is_file()
        ],
    }
    with store.connect() as db:
        expected_raw_path = db.execute(
            "SELECT raw_path FROM snapshots WHERE id=?", (snapshot_id,)
        ).fetchone()[0]
    assert expected_raw_path
    before_business = _main_business_fingerprint(root)
    assert before_business
    assert not (db_path.parent / "state.sqlite3-wal").exists()
    assert not (db_path.parent / "state.sqlite3-shm").exists()
    connections, closed, traces, uris = _install_reader_guards(monkeypatch, root)
    report = TaiwanData(root).corporate_actions()
    action = _action(report)
    assert (
        action["cash_dividend_per_share"],
        action["bonus_share_ratio"],
        action["share_multiplier"],
    ) == (
        "8",
        "0",
        "1",
    )
    assert (action["before_price"], action["reference_price"]) == ("100", "92")
    assert action["input_state"] == "complete"
    assert action["reference_check"]["status"] == "match"
    for field in (
        "cash_dividend_per_share",
        "bonus_share_ratio",
        "before_price",
        "reference_price",
    ):
        refs = action["field_provenance"][field]
        assert refs
        assert all(
            ref["task_id"] == "task-1-opaque-logical-scope-v1"
            and ref["snapshot_id"] == snapshot_id
            and ref["sha256"] == hashlib.sha256(body).hexdigest()
            and ref["raw_path"] == expected_raw_path
            and ref["source"] == "tpex"
            and ref["dataset"] == "exDailyQ"
            for ref in refs
        )
    assert report["research_ready"] is False and report["coverage_complete"] is False
    assert cli_main(["--root", str(root), "corporate-actions"]) == 0
    assert json.loads(capsys.readouterr().out) == report
    assert len(connections) == 2
    assert all("mode=ro" in uri and "immutable=1" not in uri for uri in uris)
    assert all(
        sum(statement.strip().upper() == "BEGIN" for statement in trace) == 1 for trace in traces
    )
    assert all(closed)
    monkeypatch.undo()

    after_files = _files_except_sqlite_sidecars(root)
    assert after_files == before_files
    assert db_path.read_bytes() == main_before
    after_connection = sqlite3.connect(db_path.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        assert _business_fingerprint_from_connection(after_connection) == before_business
    finally:
        after_connection.close()
    sidecars = {path.name for path in db_path.parent.glob("state.sqlite3-*")}
    assert sidecars <= {"state.sqlite3-wal", "state.sqlite3-shm"}
    wal_path = db_path.parent / "state.sqlite3-wal"
    if wal_path.exists():
        assert wal_path.stat().st_size == 0


def test_live_wal_reader_sees_committed_edition_without_checkpoint(
    seeded, monkeypatch, capsys
) -> None:
    store, root, _, _ = seeded
    db_path = root / "state.sqlite3"
    writer = sqlite3.connect(db_path, timeout=5)
    writer.execute("PRAGMA wal_autocheckpoint=0")
    wal_autocheckpoint = writer.execute("PRAGMA wal_autocheckpoint").fetchone()[0]
    assert wal_autocheckpoint == 0
    latest_snapshot, latest_body = _add_edition(
        store, edition=2, cash="9", reference="91", connection=writer
    )
    expected_raw_path = writer.execute(
        "SELECT raw_path FROM snapshots WHERE id=?", (latest_snapshot,)
    ).fetchone()[0]
    assert latest_snapshot is not None and latest_body is not None
    writer.commit()
    wal_path = db_path.parent / "state.sqlite3-wal"
    assert wal_path.is_file() and wal_path.stat().st_size > 0

    # Explicit negative control: the immutable main-file view must still show edition 1.
    immutable = sqlite3.connect(db_path.resolve().as_uri() + "?mode=ro&immutable=1", uri=True)
    try:
        assert immutable.execute("SELECT MAX(edition) FROM tasks").fetchone()[0] == 1
        assert (
            immutable.execute(
                "SELECT COUNT(*) FROM tasks WHERE id='task-2-opaque-logical-scope-v1'"
            ).fetchone()[0]
            == 0
        )
    finally:
        immutable.close()

    before_files = _files_except_sqlite_sidecars(root)
    main_before = db_path.read_bytes()
    raw_before = {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in (root / "raw").rglob("*")
        if path.is_file()
    }
    wal_before = wal_path.read_bytes()
    business_before = _business_fingerprint_from_connection(writer)
    connections, closed, traces, uris = _install_reader_guards(monkeypatch, root)
    report = TaiwanData(root).corporate_actions()
    action = _action(report)
    assert action["cash_dividend_per_share"] == "9"
    assert action["reference_price"] == "91"
    assert action["input_state"] == "complete"
    assert action["reference_check"]["status"] == "match"
    refs = [ref for values in action["field_provenance"].values() for ref in values]
    assert refs and all(
        ref["task_id"] == "task-2-opaque-logical-scope-v1"
        and ref["snapshot_id"] == latest_snapshot
        and ref["sha256"] == hashlib.sha256(latest_body).hexdigest()
        and ref["raw_path"] == expected_raw_path
        and ref["source"] == "tpex"
        and ref["dataset"] == "exDailyQ"
        for ref in refs
    )
    assert report["research_ready"] is False and report["coverage_complete"] is False
    assert cli_main(["--root", str(root), "corporate-actions"]) == 0
    assert json.loads(capsys.readouterr().out) == report
    assert len(connections) == 2
    assert all("mode=ro" in uri and "immutable=1" not in uri for uri in uris)
    assert all(
        sum(statement.strip().upper() == "BEGIN" for statement in trace) == 1 for trace in traces
    )
    assert all(closed)
    monkeypatch.undo()
    assert db_path.read_bytes() == main_before
    assert wal_path.read_bytes() == wal_before
    assert (
        hashlib.sha256(wal_path.read_bytes()).hexdigest() == hashlib.sha256(wal_before).hexdigest()
    )
    assert _business_fingerprint_from_connection(writer) == business_before
    assert {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in (root / "raw").rglob("*")
        if path.is_file()
    } == raw_before
    assert _files_except_sqlite_sidecars(root) == before_files
    writer.close()
