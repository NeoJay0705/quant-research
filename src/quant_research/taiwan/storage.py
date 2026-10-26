"""Local append-only raw storage and read-only TaiwanData access."""

# ruff: noqa: E501

from __future__ import annotations

import fcntl
import json
import os
import sqlite3
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

DEFAULT_ROOT = Path.home() / ".local/share/quant-research/taiwan"
SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS runs (id TEXT PRIMARY KEY, kind TEXT NOT NULL, started_at TEXT NOT NULL, updated_at TEXT NOT NULL, finished_at TEXT, state TEXT NOT NULL, pid INTEGER, counts TEXT NOT NULL DEFAULT '{}', error TEXT); -- noqa: E501
CREATE TABLE IF NOT EXISTS tasks (id TEXT PRIMARY KEY, logical_key TEXT NOT NULL, edition INTEGER NOT NULL, source TEXT NOT NULL, dataset TEXT, params TEXT NOT NULL, kind TEXT NOT NULL, state TEXT NOT NULL, expected_date TEXT, attempts INTEGER NOT NULL DEFAULT 0, run_id TEXT REFERENCES runs(id), snapshot_id TEXT, error TEXT, UNIQUE(logical_key, edition)); -- noqa: E501
CREATE TABLE IF NOT EXISTS snapshots (id TEXT PRIMARY KEY, source TEXT NOT NULL, dataset TEXT, params TEXT NOT NULL, safe_url TEXT NOT NULL, scope_key TEXT NOT NULL, fetched_at TEXT NOT NULL, http_status INTEGER NOT NULL, sha256 TEXT NOT NULL, raw_path TEXT NOT NULL, validation_state TEXT NOT NULL, error TEXT); -- noqa: E501
CREATE TABLE IF NOT EXISTS records (id INTEGER PRIMARY KEY, snapshot_id TEXT NOT NULL REFERENCES snapshots(id), kind TEXT NOT NULL, symbol TEXT, record_date TEXT, record_key TEXT, payload TEXT NOT NULL, quality_flags TEXT NOT NULL DEFAULT '[]'); -- noqa: E501
CREATE TABLE IF NOT EXISTS official_event_links (id INTEGER PRIMARY KEY, parent_task_id TEXT NOT NULL REFERENCES tasks(id), parent_snapshot_id TEXT NOT NULL REFERENCES snapshots(id), child_task_id TEXT NOT NULL REFERENCES tasks(id), context TEXT NOT NULL, UNIQUE(parent_task_id,parent_snapshot_id,child_task_id,context));
CREATE INDEX IF NOT EXISTS records_lookup ON records(kind, symbol, record_date);
CREATE INDEX IF NOT EXISTS records_snapshot ON records(snapshot_id);
CREATE INDEX IF NOT EXISTS snapshots_scope ON snapshots(source, dataset, scope_key, fetched_at);
CREATE TABLE IF NOT EXISTS source_blocks (source TEXT PRIMARY KEY, reason TEXT NOT NULL, state TEXT NOT NULL, blocked_at TEXT NOT NULL, retry_at TEXT); -- noqa: E501
CREATE TABLE IF NOT EXISTS request_log (id INTEGER PRIMARY KEY, source TEXT NOT NULL, requested_at TEXT NOT NULL); -- noqa: E501
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _decode_record(row: dict[str, object]) -> dict[str, object]:
    payload = json.loads(str(row.pop("payload")))
    flags = json.loads(str(row.pop("quality_flags", "[]")))
    provenance = {
        key: row.pop(key, None)
        for key in (
            "source",
            "dataset",
            "snapshot_id",
            "fetched_at",
            "sha256",
            "raw_path",
            "params",
            "scope_key",
        )
    }
    if provenance.get("params") is not None:
        provenance["params"] = json.loads(str(provenance["params"]))
    payload["provenance"] = provenance
    payload["quality_flags"] = flags
    return {**payload, **provenance}


class WriterBusyError(RuntimeError):
    """Another writer holds the non-blocking lock."""


class RawStorageError(RuntimeError):
    pass


class SQLiteStore:
    """Explicitly initialized writable store. Construction has no filesystem effects."""

    def __init__(self, root: str | Path = DEFAULT_ROOT) -> None:
        self.root = Path(root).expanduser()

    def initialize(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.root / "state.sqlite3", timeout=10)
        try:
            db.executescript(SCHEMA)
            columns = {row[1] for row in db.execute("PRAGMA table_info(tasks)")}
            if "expected_date" not in columns:
                db.execute("ALTER TABLE tasks ADD COLUMN expected_date TEXT")
            db.commit()
        finally:
            db.close()

    @contextmanager
    def writer_lock(self) -> Iterator[None]:
        self.root.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.root / "writer.lock", os.O_CREAT | os.O_RDWR, 0o600)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise WriterBusyError("Taiwan data writer is busy") from exc
            yield
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.root / "state.sqlite3", timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA busy_timeout=10000")
        try:
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def save_raw(self, source: str, raw: bytes, fetched_at: str | None = None) -> str:
        """Create an exclusive immutable raw file; never replace an existing path."""
        if source not in {"twse", "tpex", "finmind"}:
            raise ValueError("raw source is not allowlisted")
        stamp = (fetched_at or _now()).replace(":", "").replace("-", "")[:8]
        directory = self.root / "raw" / source / stamp
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{uuid.uuid4().hex}.bin"
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
        except BaseException:
            # Preserve partial raw evidence rather than unlinking it.
            raise
        return str(path.relative_to(self.root))

    def save_snapshot(
        self,
        *,
        source: str,
        dataset: str | None,
        params: dict[str, object],
        safe_url: str,
        scope_key: str,
        fetched_at: str,
        http_status: int,
        sha256: str,
        raw_path: str,
        validation_state: str,
        error: str | None = None,
        records: tuple[dict[str, object], ...] = (),
        connection: sqlite3.Connection | None = None,
    ) -> str:
        snapshot_id = uuid.uuid4().hex
        if connection is not None:
            return self._insert_snapshot(
                connection,
                snapshot_id,
                source,
                dataset,
                params,
                safe_url,
                scope_key,
                fetched_at,
                http_status,
                sha256,
                raw_path,
                validation_state,
                error,
                records,
            )
        with self.connect() as db:
            return self._insert_snapshot(
                db,
                snapshot_id,
                source,
                dataset,
                params,
                safe_url,
                scope_key,
                fetched_at,
                http_status,
                sha256,
                raw_path,
                validation_state,
                error,
                records,
            )

    @staticmethod
    def _insert_snapshot(
        db: sqlite3.Connection,
        snapshot_id: str,
        source: str,
        dataset: str | None,
        params: dict[str, object],
        safe_url: str,
        scope_key: str,
        fetched_at: str,
        http_status: int,
        sha256: str,
        raw_path: str,
        validation_state: str,
        error: str | None,
        records: tuple[dict[str, object], ...],
    ) -> str:
        db.execute(
            "INSERT INTO snapshots VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                snapshot_id,
                source,
                dataset,
                json.dumps(params, sort_keys=True),
                safe_url,
                scope_key,
                fetched_at,
                http_status,
                sha256,
                raw_path,
                validation_state,
                error,
            ),
        )
        for index, record in enumerate(records):
            kind = str(record.get("kind", "quote" if "trade_date" in record else "event"))
            flags = record.get("quality_flags", [])
            db.execute(
                "INSERT INTO records(snapshot_id, kind, symbol, record_date, record_key, payload, quality_flags) VALUES (?, ?, ?, ?, ?, ?, ?)",  # noqa: E501
                (
                    snapshot_id,
                    kind,
                    record.get("symbol"),
                    record.get("trade_date", record.get("date")),
                    str(record.get("record_key", index)),
                    json.dumps(record, ensure_ascii=False, sort_keys=True),
                    json.dumps(flags),
                ),
            )
        return snapshot_id

    def put(self, table: str, key: str, values: dict[str, object]) -> None:
        """Small typed-schema CRUD upsert for settings, tasks, runs, blocks, request log."""
        permitted = {
            "settings": {"key", "value"},
            "source_blocks": {"source", "reason", "state", "blocked_at", "retry_at"},
            "runs": {
                "id",
                "kind",
                "started_at",
                "updated_at",
                "finished_at",
                "state",
                "pid",
                "counts",
                "error",
            },
            "tasks": {
                "id",
                "logical_key",
                "edition",
                "source",
                "dataset",
                "params",
                "kind",
                "state",
                "attempts",
                "run_id",
                "snapshot_id",
                "error",
            },
        }
        if table not in permitted or set(values) - permitted[table]:
            raise ValueError("unsupported CRUD table or columns")
        cols = list(values)
        placeholders = ",".join("?" for _ in cols)
        update = ",".join(f"{col}=excluded.{col}" for col in cols if col != key)
        with self.connect() as db:
            db.execute(
                f"INSERT INTO {table} ({','.join(cols)}) VALUES ({placeholders}) ON CONFLICT({key}) DO UPDATE SET {update}",  # noqa: E501
                tuple(values[c] for c in cols),
            )

    def insert_row(self, table: str, values: dict[str, object]) -> None:
        """Insert one row into a core operational table using a strict table allowlist."""
        allowed = {
            "runs": {
                "id",
                "kind",
                "started_at",
                "updated_at",
                "finished_at",
                "state",
                "pid",
                "counts",
                "error",
            },
            "tasks": {
                "id",
                "logical_key",
                "edition",
                "source",
                "dataset",
                "params",
                "kind",
                "state",
                "attempts",
                "run_id",
                "snapshot_id",
                "error",
            },
            "source_blocks": {"source", "reason", "state", "blocked_at", "retry_at"},
            "request_log": {"source", "requested_at"},
            "settings": {"key", "value"},
        }
        if table not in allowed or not values or set(values) - allowed[table]:
            raise ValueError("unsupported CRUD table or columns")
        columns = list(values)
        sql = f"INSERT INTO {table} ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})"
        with self.connect() as db:
            db.execute(sql, tuple(values[column] for column in columns))

    def log_request(self, source: str, requested_at: str | None = None) -> None:
        with self.connect() as db:
            db.execute(
                "INSERT INTO request_log(source, requested_at) VALUES (?, ?)",
                (source, requested_at or _now()),
            )

    def recover_stale_runs(self) -> None:
        """Mark running work interrupted and return running tasks to pending."""
        with self.connect() as db:
            db.execute(
                "UPDATE runs SET state='interrupted', finished_at=?, updated_at=? WHERE state='running'",  # noqa: E501
                (_now(), _now()),
            )
            db.execute(
                "UPDATE tasks SET state='pending', error='recovered stale running task' WHERE state='running'"  # noqa: E501
            )


def _corporate_actions_read(
    db: sqlite3.Connection,
    symbol: str | None = None,
    *,
    source: str,
    start: str | None,
    end: str | None,
) -> dict[str, object]:
    """Read corporate-action evidence from an existing read transaction."""
    from .corporate_actions import build_corporate_actions

    empty = build_corporate_actions([], [], [], symbol=symbol, source=source, start=start, end=end)
    tables = {
        str(row[0]) for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    required = {"tasks", "snapshots", "records"}
    if not required <= tables:
        raise sqlite3.DatabaseError("corporate-action schema is incomplete")
    if "official_event_links" not in tables:
        return empty

    task_rows = [
        dict(row)
        for row in db.execute(
            "SELECT t.rowid AS task_rowid,t.id task_id,t.logical_key,t.edition,t.source,t.dataset,"
            "t.kind,t.state,t.snapshot_id,t.params task_params,s.id AS joined_snapshot_id,"
            "s.source snapshot_source,s.dataset snapshot_dataset,s.params snapshot_params,"
            "s.rowid snapshot_rowid,s.fetched_at,s.sha256,s.raw_path,s.validation_state "
            "FROM tasks t LEFT JOIN snapshots s ON s.id=t.snapshot_id "
            "WHERE t.source IN ('twse','tpex') AND t.kind IN ('official_event_summary','official_event_detail') "
            "AND t.params IS NOT NULL "
            + ("AND t.source=? " if source != "official" else "")
            + "ORDER BY t.rowid",
            (source,) if source != "official" else (),
        )
    ]
    tasks: list[dict[str, object]] = []
    eligible_snapshots: set[str] = set()
    for row in task_rows:
        # Task params are operational state and must decode cleanly. Snapshot
        # metadata is untrusted provenance, so decode it independently.
        params = json.loads(str(row["task_params"]))
        try:
            snapshot_params = (
                json.loads(str(row["snapshot_params"]))
                if row["snapshot_params"] is not None
                else None
            )
        except (ValueError, TypeError):
            snapshot_params = None
        if (
            not isinstance(params, dict)
            or params.get("pipeline") != "official-v1"
            or params.get("lane") != "events"
        ):
            continue
        lineage_bad = row["joined_snapshot_id"] is not None and (
            row["snapshot_source"] != row["source"]
            or row["snapshot_dataset"] != row["dataset"]
            or not isinstance(snapshot_params, dict)
            or snapshot_params.get("pipeline") != params.get("pipeline")
            or snapshot_params.get("lane") != params.get("lane")
            or snapshot_params.get("request") != params.get("request")
        )
        # A mismatch is carried to the pure validator as an invalid validation state.
        validation = "snapshot_provenance_mismatch" if lineage_bad else row["validation_state"]
        task = {
            key: row[key]
            for key in (
                "task_id",
                "logical_key",
                "edition",
                "source",
                "dataset",
                "kind",
                "state",
                "snapshot_id",
                "snapshot_rowid",
                "fetched_at",
                "sha256",
                "raw_path",
            )
        }
        task.update({"params": params, "validation_state": validation, "record_count": 0})
        if row["joined_snapshot_id"] is not None:
            if not lineage_bad and row["snapshot_id"] == row["joined_snapshot_id"]:
                eligible_snapshots.add(str(row["joined_snapshot_id"]))
        tasks.append(task)

    record_counts = {
        str(row["snapshot_id"]): int(row["record_count"])
        for row in db.execute(
            "SELECT r.snapshot_id,COUNT(*) record_count FROM records r "
            "JOIN snapshots s ON s.id=r.snapshot_id "
            "WHERE r.kind='event' AND s.source IN ('twse','tpex') "
            "GROUP BY r.snapshot_id"
        )
    }
    for task in tasks:
        task["record_count"] = record_counts.get(str(task["snapshot_id"]), 0)

    observations: list[dict[str, object]] = []
    if eligible_snapshots:
        clauses = [
            "r.snapshot_id IN (SELECT value FROM json_each(?))",
            "r.kind='event'",
            "s.source IN ('twse','tpex')",
        ]
        args: list[object] = [json.dumps(sorted(eligible_snapshots))]
        if symbol is not None:
            clauses.append("r.symbol=?")
            args.append(symbol)
        if start is not None:
            clauses.append("(r.record_date IS NULL OR r.record_date>=?)")
            args.append(start)
        if end is not None:
            clauses.append("(r.record_date IS NULL OR r.record_date<=?)")
            args.append(end)
        sql = (
            "SELECT r.id record_id,r.snapshot_id,r.payload,r.quality_flags FROM records r "
            "JOIN snapshots s ON s.id=r.snapshot_id WHERE "
            + " AND ".join(clauses)
            + " ORDER BY r.id"
        )
        for row in db.execute(sql, tuple(args)):
            payload = json.loads(str(row["payload"]))
            flags = json.loads(str(row["quality_flags"]))
            if isinstance(payload, dict):
                payload["quality_flags"] = sorted(
                    set(payload.get("quality_flags", [])) | set(flags)
                )
            observations.append(
                {
                    "record_id": row["record_id"],
                    "snapshot_id": row["snapshot_id"],
                    "payload": payload,
                }
            )

    link_sql = (
        "SELECT l.parent_task_id,l.parent_snapshot_id,l.child_task_id,l.context "
        "FROM official_event_links l "
        "JOIN tasks p ON p.id=l.parent_task_id "
        "JOIN snapshots ps ON ps.id=l.parent_snapshot_id "
        "JOIN tasks c ON c.id=l.child_task_id "
        "WHERE p.source IN ('twse','tpex') AND p.kind='official_event_summary' "
        "AND p.snapshot_id=l.parent_snapshot_id "
        "AND json_type(p.params)='object' "
        "AND json_extract(p.params,'$.pipeline')='official-v1' "
        "AND json_extract(p.params,'$.lane')='events' "
        "AND ps.source=p.source AND ps.dataset=p.dataset "
        "AND c.source IN ('twse','tpex') AND c.kind='official_event_detail' "
        "AND json_type(c.params)='object' "
        "AND json_extract(c.params,'$.pipeline')='official-v1' "
        "AND json_extract(c.params,'$.lane')='events' "
        + ("AND p.source=? " if source != "official" else "")
        + "ORDER BY l.id"
    )
    link_rows = db.execute(link_sql, (source,) if source != "official" else ())
    links = []
    for row in link_rows:
        links.append(
            {
                "parent_task_id": row["parent_task_id"],
                "parent_snapshot_id": row["parent_snapshot_id"],
                "child_task_id": row["child_task_id"],
                "context": json.loads(str(row["context"])),
            }
        )
    return build_corporate_actions(
        observations, links, tasks, symbol=symbol, source=source, start=start, end=end
    )


class TaiwanData:
    """Read-only facade. Construction does not open, create, or inspect the root."""

    def __init__(self, root: str | Path = DEFAULT_ROOT) -> None:
        self.root = Path(root).expanduser()

    def observation_universe(
        self, date: str, *, source: str = "official", limit: int = 20000, offset: int = 0
    ) -> dict[str, object]:
        from .reports import observation_universe

        return observation_universe(self.root, date, source=source, limit=limit, offset=offset)

    def availability(self, start: str, end: str, *, source: str = "official") -> dict[str, object]:
        from .reports import availability

        return availability(self.root, start, end, source=source)

    def corporate_actions(
        self,
        symbol: str | None = None,
        *,
        source: str = "official",
        start: str | None = None,
        end: str | None = None,
    ) -> dict[str, object]:
        """Build a corporate-action report from one consistent, read-only DB view."""
        from .corporate_actions import build_corporate_actions

        # The pure builder owns validation. Run it before inspecting the root path.
        empty = build_corporate_actions(
            [], [], [], symbol=symbol, source=source, start=start, end=end
        )
        path = self.root / "state.sqlite3"
        if not path.exists():
            return empty
        uri = path.resolve().as_uri() + "?mode=ro"
        db = sqlite3.connect(uri, uri=True, timeout=5)
        db.row_factory = sqlite3.Row
        try:
            db.execute("BEGIN")
            return _corporate_actions_read(db, symbol=symbol, source=source, start=start, end=end)
        finally:
            db.close()

    def _read(self, sql: str, params: tuple[object, ...] = ()) -> list[dict[str, object]]:
        path = self.root / "state.sqlite3"
        if not path.exists():
            return []
        uri = path.resolve().as_uri() + "?mode=ro"
        db = sqlite3.connect(uri, uri=True, timeout=5)
        db.row_factory = sqlite3.Row
        try:
            return [dict(row) for row in db.execute(sql, params)]
        finally:
            db.close()

    def quotes(
        self, symbol: str, *, source: str, start: str | None = None, end: str | None = None
    ) -> list[dict[str, object]]:
        if not source:
            raise ValueError("quotes requires an explicit source")
        clauses = ["r.kind='quote'", "r.symbol=?", "s.source=?"]
        args: list[object] = [symbol, source]
        if start:
            clauses.append("r.record_date>=?")
            args.append(start)
        if end:
            clauses.append("r.record_date<=?")
            args.append(end)
        rows = self._read(
            "WITH active_snapshots AS (SELECT s.*, s.rowid AS snapshot_rowid, ROW_NUMBER() OVER (PARTITION BY source, dataset, scope_key ORDER BY fetched_at DESC, rowid DESC) AS snapshot_rank FROM snapshots s WHERE validation_state='valid'), ranked_quotes AS (SELECT r.*, s.id AS active_snapshot_id, s.source, s.dataset, s.fetched_at, s.sha256, s.raw_path, s.params, s.scope_key, ROW_NUMBER() OVER (PARTITION BY s.source, r.symbol, r.record_date, COALESCE(json_extract(r.payload,'$.venue_id'), CASE WHEN json_extract(r.payload,'$.venue')='管理股票' THEN 'management' ELSE 'primary' END) ORDER BY s.fetched_at DESC, s.snapshot_rowid DESC, r.id DESC) AS quote_rank FROM records r JOIN active_snapshots s ON s.id=r.snapshot_id WHERE s.snapshot_rank=1 AND "
            + " AND ".join(clauses)
            + ") SELECT payload, quality_flags, active_snapshot_id AS snapshot_id, source, dataset, fetched_at, sha256, raw_path, params, scope_key FROM ranked_quotes WHERE quote_rank=1 ORDER BY record_date",
            tuple(args),
        )
        return [_decode_record(row) for row in rows]

    def events(
        self,
        symbol: str | None = None,
        *,
        dataset: str | None = None,
        source: str = "official",
        include_unverified: bool = False,
    ) -> list[dict[str, object]]:
        if source not in {"official", "twse", "tpex", "finmind"}:
            raise ValueError("unknown event source")
        clauses = ["s.snapshot_rank=1", "r.kind='event'"]
        args: list[object] = []
        if source == "official":
            clauses.append("s.source IN ('twse','tpex')")
        else:
            clauses.append("s.source=?")
            args = [source]
        if symbol is not None:
            clauses.append("r.symbol=?")
            args.append(symbol)
        if dataset is not None:
            clauses.append("s.dataset=?")
            args.append(dataset)
        validation = "s.validation_state='valid'"
        if include_unverified:
            validation = "s.validation_state IN ('valid','period_envelope_unverified')"
        rows = self._read(
            "WITH active_snapshots AS (SELECT s.*, ROW_NUMBER() OVER (PARTITION BY source, dataset, scope_key ORDER BY fetched_at DESC, rowid DESC) AS snapshot_rank FROM snapshots s WHERE "
            + validation
            + ") SELECT r.payload, r.quality_flags, s.id snapshot_id, s.source, s.dataset, s.fetched_at, s.sha256, s.raw_path, s.params, s.scope_key FROM records r JOIN active_snapshots s ON s.id=r.snapshot_id WHERE "
            + " AND ".join(clauses)
            + " ORDER BY r.record_date, r.id",
            tuple(args),
        )
        return [_decode_record(row) for row in rows]

    def event_links(
        self, symbol: str | None = None, *, source: str = "official", include_history: bool = False
    ) -> list[dict[str, object]]:
        if source not in {"official", "twse", "tpex"}:
            raise ValueError("unknown event link source")
        path = self.root / "state.sqlite3"
        if not path.exists():
            return []
        params: tuple[object, ...] = ()
        source_clause = "p.source IN ('twse','tpex')" if source == "official" else "p.source=?"
        if source != "official":
            params = (source,)
        pipeline_clause = (
            " AND p.kind='official_event_summary' "
            "AND json_extract(p.params,'$.pipeline')='official-v1' "
            "AND json_extract(p.params,'$.lane')='events' "
            "AND c.source IN ('twse','tpex') AND c.kind='official_event_detail' "
            "AND json_extract(c.params,'$.pipeline')='official-v1' "
            "AND json_extract(c.params,'$.lane')='events'"
            if source == "official"
            else " AND p.kind='official_event_summary' "
            "AND json_extract(p.params,'$.pipeline')='official-v1' "
            "AND json_extract(p.params,'$.lane')='events' "
            "AND c.source=? AND c.kind='official_event_detail' "
            "AND json_extract(c.params,'$.pipeline')='official-v1' "
            "AND json_extract(c.params,'$.lane')='events'"
        )
        if source != "official":
            params += (source,)
        symbol_clause = ""
        if symbol is not None:
            symbol_clause = " AND (json_extract(l.context,'$.symbol')=? OR json_extract(c.params,'$.request.symbol')=?)"
            params += (symbol, symbol)
        latest = (
            ""
            if include_history
            else " AND p.edition=(SELECT MAX(p2.edition) FROM tasks p2 WHERE p2.logical_key=p.logical_key)"
        )
        try:
            rows = self._read(
                "SELECT json_object('task_id',p.id,'edition',p.edition,'source',p.source,'dataset',p.dataset,'state',p.state,'snapshot_id',l.parent_snapshot_id,'validation_state',ps.validation_state,'request',json_extract(p.params,'$.request'),'sha256',ps.sha256,'raw_path',ps.raw_path,'latest_edition',CASE WHEN p.edition=(SELECT MAX(p2.edition) FROM tasks p2 WHERE p2.logical_key=p.logical_key) THEN 1 ELSE 0 END) parent, json_object('task_id',c.id,'edition',c.edition,'source',c.source,'dataset',c.dataset,'state',c.state,'snapshot_id',c.snapshot_id,'validation_state',cs.validation_state,'request',json_extract(c.params,'$.request'),'sha256',cs.sha256,'raw_path',cs.raw_path,'latest_edition',CASE WHEN c.edition=(SELECT MAX(c2.edition) FROM tasks c2 WHERE c2.logical_key=c.logical_key) THEN 1 ELSE 0 END) child,l.context context FROM official_event_links l JOIN tasks p ON p.id=l.parent_task_id JOIN tasks c ON c.id=l.child_task_id LEFT JOIN snapshots ps ON ps.id=l.parent_snapshot_id LEFT JOIN snapshots cs ON cs.id=c.snapshot_id WHERE "
                + source_clause
                + pipeline_clause
                + latest
                + symbol_clause
                + " ORDER BY p.expected_date,p.edition,c.dataset",
                params,
            )
            result = []
            for row in rows:
                parent = json.loads(str(row["parent"]))
                child = json.loads(str(row["child"]))
                parent["latest_edition"] = bool(parent["latest_edition"])
                child["latest_edition"] = bool(child["latest_edition"])
                result.append(
                    {
                        "parent": parent,
                        "child": child,
                        "context": json.loads(str(row["context"])),
                    }
                )
            return result
        except sqlite3.OperationalError as exc:
            if "official_event_links" in str(exc):
                return []
            raise

    def event_coverage(self, *, source: str = "official") -> dict[str, object]:
        if source not in {"official", "twse", "tpex"}:
            raise ValueError("unknown event coverage source")
        source_clause = "t.source IN ('twse','tpex')" if source == "official" else "t.source=?"
        source_args: tuple[object, ...] = () if source == "official" else (source,)
        periods = self._read(
            "WITH ranked AS (SELECT t.*, ROW_NUMBER() OVER (PARTITION BY t.logical_key "
            "ORDER BY t.edition DESC, t.rowid DESC) AS rn FROM tasks t WHERE "
            + source_clause
            + " AND t.kind='official_event_summary' AND "
            "json_extract(t.params,'$.pipeline')='official-v1' AND "
            "json_extract(t.params,'$.lane')='events') "
            "SELECT t.id,t.logical_key,t.edition,t.source,t.dataset,t.state,t.snapshot_id,t.params,"
            "s.validation_state,COUNT(r.id) record_count FROM ranked t "
            "LEFT JOIN snapshots s ON s.id=t.snapshot_id "
            "LEFT JOIN records r ON r.snapshot_id=s.id AND r.kind='event' "
            "WHERE t.rn=1 GROUP BY t.id ORDER BY t.logical_key",
            source_args,
        )
        summary_states: dict[str, int] = {}
        summary_periods = []
        for row in periods:
            params = json.loads(str(row["params"]))
            request = cast(dict[str, object], params.get("request", {}))
            state = str(row["state"])
            summary_states[state] = summary_states.get(state, 0) + 1
            summary_periods.append(
                {
                    "source": row["source"],
                    "dataset": row["dataset"],
                    "start": request.get("start_date"),
                    "end": request.get("end_date"),
                    "state": state,
                    "edition": row["edition"],
                    "snapshot_id": row["snapshot_id"],
                    "validation_state": row["validation_state"],
                    "record_count": int(str(row["record_count"])),
                }
            )

        detail_states_rows = self._read(
            "WITH ranked AS (SELECT t.state,ROW_NUMBER() OVER (PARTITION BY t.logical_key "
            "ORDER BY t.edition DESC,t.rowid DESC) rn FROM tasks t WHERE "
            + source_clause
            + " AND t.kind='official_event_detail' AND "
            "json_extract(t.params,'$.pipeline')='official-v1' AND "
            "json_extract(t.params,'$.lane')='events') "
            "SELECT state,COUNT(*) count FROM ranked WHERE rn=1 GROUP BY state",
            source_args,
        )
        detail_states = {str(row["state"]): int(str(row["count"])) for row in detail_states_rows}

        link_counts = {
            "latest_parent_links": 0,
            "detail_links_not_completed_and_valid": 0,
            "distinct_required_details": 0,
        }
        link_sql = (
            "WITH parents AS (SELECT t.* FROM tasks t WHERE "
            + ("t.source IN ('twse','tpex')" if source == "official" else "t.source=?")
            + " AND t.kind='official_event_summary' AND "
            "json_extract(t.params,'$.pipeline')='official-v1' AND "
            "json_extract(t.params,'$.lane')='events' AND t.edition=(SELECT MAX(x.edition) "
            "FROM tasks x WHERE x.logical_key=t.logical_key)), edges AS ("
            "SELECT l.parent_task_id,l.child_task_id,c.state child_state,cs.validation_state "
            "FROM official_event_links l JOIN parents p ON p.id=l.parent_task_id "
            "JOIN tasks c ON c.id=l.child_task_id AND c.source IN ('twse','tpex') "
            "AND c.kind='official_event_detail' AND "
            "json_extract(c.params,'$.pipeline')='official-v1' AND "
            "json_extract(c.params,'$.lane')='events' LEFT JOIN snapshots cs ON cs.id=c.snapshot_id) "
            "SELECT COUNT(*) latest_parent_links,"
            "SUM(CASE WHEN child_state!='completed' OR validation_state IS NULL "
            "OR validation_state!='valid' THEN 1 ELSE 0 END) detail_links_not_completed_and_valid,"
            "COUNT(DISTINCT child_task_id) distinct_required_details FROM edges"
        )
        try:
            link_rows = self._read(link_sql, source_args)
            if link_rows:
                link_counts = {key: int(str(link_rows[0][key] or 0)) for key in link_counts}
        except sqlite3.OperationalError as exc:
            if "official_event_links" not in str(exc):
                raise

        missing_rows = self._read(
            "WITH latest AS (SELECT t.source,t.dataset,t.snapshot_id,t.logical_key,"
            "ROW_NUMBER() OVER (PARTITION BY t.logical_key ORDER BY t.edition DESC,t.rowid DESC) rn "
            "FROM tasks t WHERE "
            + source_clause
            + " AND t.kind IN ('official_event_summary','official_event_detail') AND "
            "json_extract(t.params,'$.pipeline')='official-v1' AND "
            "json_extract(t.params,'$.lane')='events') "
            "SELECT l.dataset,json_extract(r.payload,'$.observation_type') observation_type,"
            "j.value field,COUNT(*) count FROM latest l JOIN records r ON r.snapshot_id=l.snapshot_id "
            "JOIN json_each(r.payload,'$.missing_fields') j WHERE l.rn=1 "
            "GROUP BY l.dataset,observation_type,j.value ORDER BY l.dataset,observation_type,j.value",
            source_args,
        )
        return {
            "scope": "planned_tasks_only",
            "coverage_complete": False,
            "research_ready": False,
            "warnings": [
                "unplanned_intervals_not_covered",
                "historical_identity_not_validated",
                "financial_required_fields_not_fully_validated",
                "parser_missing_fields_are_not_financial_requirements",
                "empty_periods_are_not_completeness_evidence",
            ],
            "summary_states": summary_states,
            "detail_states": detail_states,
            "empty_periods": sum(row["validation_state"] == "empty_unverified" for row in periods),
            "period_warnings": sum(
                row["validation_state"] == "period_envelope_unverified" for row in periods
            ),
            **link_counts,
            "summary_periods": summary_periods,
            "reported_missing_fields": [
                {
                    "dataset": row["dataset"],
                    "observation_type": row["observation_type"],
                    "field": row["field"],
                    "count": int(str(row["count"])),
                }
                for row in missing_rows
            ],
        }

    def symbols(
        self, include_excluded: bool = True, *, source: str = "official"
    ) -> list[dict[str, object]]:
        if source not in {"official", "twse", "tpex", "finmind"}:
            raise ValueError("unknown symbol source")
        if source != "finmind":
            clauses = "s.source IN ('twse','tpex')" if source == "official" else "s.source=?"
            args: tuple[object, ...] = () if source == "official" else (source,)
            rows = self._read(
                "WITH active_snapshots AS (SELECT s.*,ROW_NUMBER() OVER(PARTITION BY source,dataset,scope_key ORDER BY fetched_at DESC,rowid DESC) snapshot_rank FROM snapshots s WHERE validation_state='valid'), observed AS (SELECT r.symbol,s.source,MIN(r.record_date) first_observed,MAX(r.record_date) last_observed,COUNT(*) quote_count FROM records r JOIN active_snapshots s ON s.id=r.snapshot_id WHERE r.kind='quote' AND s.snapshot_rank=1 AND "
                + clauses
                + " GROUP BY s.source,r.symbol) SELECT * FROM observed ORDER BY source,symbol",
                args,
            )
            return [
                {
                    "symbol": row["symbol"],
                    "source": row["source"],
                    "classification": "candidate_unverified",
                    "first_observed": row["first_observed"],
                    "last_observed": row["last_observed"],
                    "quote_presence": {"count": row["quote_count"]},
                    "classification_basis": "quote_presence_only",
                    "listing_period": "unknown",
                    "delisting_period": "unknown",
                    "research_ready": False,
                }
                for row in rows
            ]
        rows = self._read(
            "SELECT r.payload, r.quality_flags, s.id snapshot_id, s.source, s.dataset, s.fetched_at FROM records r JOIN snapshots s ON s.id=r.snapshot_id WHERE r.kind='symbol' AND s.validation_state='valid' AND ((s.dataset='TaiwanStockInfo' AND lower(COALESCE(json_extract(r.payload, '$.type'), '')) IN ('twse','tpex')) OR s.dataset='TaiwanStockDelisting') ORDER BY r.symbol, s.fetched_at"  # noqa: E501
        )
        merged: dict[str, dict[str, object]] = {}
        for row in rows:
            original = json.loads(str(row["payload"]))
            code = original.get("symbol")
            if not code:
                continue
            item = merged.setdefault(
                str(code),
                {
                    "symbol": str(code),
                    "classification": "candidate_unverified",
                    "evidence": [],
                    "research_ready": False,
                },
            )
            evidence = item["evidence"]
            if isinstance(evidence, list):
                evidence.append(
                    {
                        "record": original,
                        "source": row.get("source"),
                        "dataset": row.get("dataset"),
                        "snapshot_id": row.get("snapshot_id"),
                    }
                )
            if row.get("dataset") == "TaiwanStockInfo":
                category = " ".join(
                    str(original.get(key, "")) for key in ("industry_category", "type")
                ).upper()
                excluded = any(
                    term in category for term in ("ETF", "ETN", "TDR", "存託憑證", "指數股票型基金")
                )
                general = bool(original.get("industry_category")) and not excluded
                item["_excluded_evidence"] = bool(item.get("_excluded_evidence")) or excluded
                item["_general_evidence"] = bool(item.get("_general_evidence")) or general
        values = list(merged.values())
        for item in values:
            excluded = bool(item.pop("_excluded_evidence", False))
            general = bool(item.pop("_general_evidence", False))
            if excluded and general:
                item["classification"] = "candidate_unverified"
                item["quality_flags"] = ["classification_conflict"]
            elif excluded:
                item["classification"] = "excluded_by_provider_category"
            else:
                item["classification"] = "candidate_unverified"
        return (
            values
            if include_excluded
            else [r for r in values if r["classification"] != "excluded_by_provider_category"]
        )

    def status(self) -> dict[str, object]:
        path = self.root / "state.sqlite3"
        if not path.exists():
            return {"initialized": False, "research_ready": False, "coverage": "unverified"}

        def lock_held(name: str) -> bool:
            lock_path = self.root / name
            if not lock_path.exists():
                return False
            fd = os.open(lock_path, os.O_RDONLY)
            try:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    fcntl.flock(fd, fcntl.LOCK_UN)
                    return False
                except BlockingIOError:
                    return True
            finally:
                os.close(fd)

        runs = self._read(
            "SELECT id, kind, state, started_at, updated_at, finished_at, error, counts, pid FROM runs ORDER BY started_at"
        )
        locks = {
            "official_quotes": lock_held("quotes.runner.lock"),
            "events": lock_held("events.runner.lock"),
            "legacy": lock_held("writer.lock"),
        }
        effective_runs = [dict(run) for run in runs]
        for run in effective_runs:
            if run["state"] == "running":
                kind = str(run.get("kind", ""))
                is_locked = (
                    locks["official_quotes"]
                    if kind.startswith("official-quotes:")
                    else locks["events"]
                    if kind.startswith("official-events:")
                    else locks["legacy"]
                )
                if not is_locked:
                    run["stored_state"] = "running"
                    run["state"] = "interrupted"
        effective_last_run = effective_runs[-1] if effective_runs else None
        return {
            "initialized": True,
            "running": any(locks.values()),
            "lanes": {
                "official_quotes_running": locks["official_quotes"],
                "events_running": locks["events"],
            },
            "legacy_finmind_tasks": self._read(
                "SELECT state, COUNT(*) count FROM tasks WHERE source='finmind' GROUP BY state"
            ),
            "stored_running_without_lock": any(
                run["state"] == "running" and effective.get("state") == "interrupted"
                for run, effective in zip(runs, effective_runs, strict=True)
            ),
            "stale_run_ids": [
                run["id"] for run in effective_runs if run.get("stored_state") == "running"
            ],
            "runs": effective_runs,
            "tasks": {
                state: len(self._read("SELECT id FROM tasks WHERE state=?", (state,)))
                for state in ("pending", "running", "completed", "failed", "superseded")
            },
            "last_run": effective_last_run,
            "last_success": next(
                (run for run in reversed(runs) if run["state"] == "completed"), None
            ),
            "latest_quote_date": {
                row["source"]: row["latest"]
                for row in self._read(
                    "WITH active_snapshots AS (SELECT s.*, ROW_NUMBER() OVER (PARTITION BY source, dataset, scope_key ORDER BY fetched_at DESC, rowid DESC) AS snapshot_rank FROM snapshots s WHERE validation_state='valid') SELECT s.source, MAX(r.record_date) latest FROM records r JOIN active_snapshots s ON s.id=r.snapshot_id WHERE r.kind='quote' AND s.snapshot_rank=1 GROUP BY s.source"
                )
            },
            "coverage_warnings": ["history_coverage_unverified", "freshness_unverified"],
            "blocks": self._read(
                "SELECT source, reason, state, blocked_at, retry_at FROM source_blocks"
            ),
            "research_ready": False,
            "coverage": "unverified",
        }
