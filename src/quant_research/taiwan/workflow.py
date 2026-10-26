"""Persistent, resumable Taiwan data workflows."""

from __future__ import annotations

import hashlib
import json
import os
import signal
import sqlite3
import time
import uuid
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from email.utils import parsedate_to_datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from . import sources
from .storage import DEFAULT_ROOT, SQLiteStore, TaiwanData

DATASETS = (
    "TaiwanStockPrice",
    "TaiwanStockDividend",
    "TaiwanStockDividendResult",
    "TaiwanStockCapitalReductionReferencePrice",
)
EVENTS = DATASETS[1:]
TAIPEI = ZoneInfo("Asia/Taipei")


class WorkflowError(RuntimeError):
    pass


def stamp(now: Callable[[], datetime]) -> str:
    value = now()
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat()


class TaiwanWorkflow:
    def __init__(
        self,
        root: str | Path = DEFAULT_ROOT,
        *,
        transport: sources.Transport = sources.urllib_transport,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        sleep: Callable[[float], None] = time.sleep,
        finmind_limit: int = 300,
        token: str | None = None,
    ) -> None:
        if finmind_limit not in (300, 600) or finmind_limit == 600 and not token:
            raise ValueError("600 requests/hour requires FINMIND_TOKEN")
        self.store = SQLiteStore(root)
        self.transport, self.now, self.sleep = transport, now, sleep
        self.limit, self.token, self.stopping = finmind_limit, token, False

    def _id(self) -> str:
        return uuid.uuid4().hex

    def _today(self) -> date:
        return self.now().astimezone(TAIPEI).date()

    def _fin_task(
        self, dataset: str, params: dict[str, str], kind: str = "history"
    ) -> dict[str, object]:
        return {"source": "finmind", "dataset": dataset, "params": params, "kind": kind}

    def _local(self, name: str, params: dict[str, object]) -> dict[str, object]:
        return {"source": "local", "dataset": name, "params": params, "kind": "plan"}

    def backfill(
        self,
        symbols: list[str] | None = None,
        start: str = "2018-01-01",
        end: str | None = None,
        max_tasks: int | None = None,
        refresh: bool = False,
    ) -> dict[str, object]:
        end = end or self._today().isoformat()
        if date.fromisoformat(start) > date.fromisoformat(end):
            raise ValueError("start must not exceed end")
        self._validate_max(max_tasks)
        with self.store.writer_lock():
            self.store.initialize()
            self.store.recover_stale_runs()
            if symbols is None:
                with self.store.connect() as db:
                    db.execute("INSERT OR REPLACE INTO settings VALUES('track_all','1')")
                    db.execute(
                        "INSERT OR REPLACE INTO settings VALUES('backfill_start',?)", (start,)
                    )
                planned = [
                    self._fin_task("TaiwanStockInfo", {}, "metadata"),
                    self._fin_task(
                        "TaiwanStockDelisting",
                        {"start_date": start, "end_date": end},
                        "metadata",
                    ),
                ]
                planned.append(
                    self._local("plan_history", {"start": start, "end": end, "refresh": refresh})
                )
            else:
                planned = [
                    self._fin_task(ds, {"data_id": symbol, "start_date": start, "end_date": end})
                    for symbol in symbols
                    for ds in DATASETS
                ]
            return self._run_locked("backfill", planned, max_tasks, refresh)

    def sync(
        self, expected_date: str | None = None, max_tasks: int | None = None
    ) -> dict[str, object]:
        self._validate_max(max_tasks)
        end = self._today()
        if expected_date is not None:
            try:
                if date.fromisoformat(expected_date).isoformat() != expected_date:
                    raise ValueError
            except ValueError as exc:
                raise ValueError("expected-date must be a valid ISO date") from exc
        tasks: list[dict[str, object]] = [
            {
                "source": "twse",
                "dataset": None,
                "params": {},
                "kind": "sync",
                "expected_date": expected_date,
            },
            {
                "source": "tpex",
                "dataset": None,
                "params": {},
                "kind": "sync",
                "expected_date": expected_date,
            },
            self._fin_task("TaiwanStockInfo", {}, "metadata"),
            self._fin_task(
                "TaiwanStockDelisting",
                {"start_date": "2018-01-01", "end_date": end.isoformat()},
                "metadata",
            ),
            *[
                self._fin_task(
                    ds, {"start_date": "2018-01-01", "end_date": end.isoformat()}, "metadata"
                )
                for ds in ("TaiwanStockSplitPrice", "TaiwanStockParValueChange")
            ],
            self._local("plan_sync", {"end": end.isoformat()}),
        ]
        with self.store.writer_lock():
            self.store.initialize()
            self.store.recover_stale_runs()
            with self.store.connect() as db:
                if expected_date:
                    db.execute(
                        "INSERT OR REPLACE INTO settings VALUES('sync_expected_date',?)",
                        (expected_date,),
                    )
                else:
                    db.execute("DELETE FROM settings WHERE key='sync_expected_date'")
            return self._run_locked("sync", tasks, max_tasks, True, expected_date)

    def resume(self, max_tasks: int | None = None) -> dict[str, object]:
        self._validate_max(max_tasks)
        with self.store.writer_lock():
            self.store.initialize()
            self.store.recover_stale_runs()
            self._repair_legacy_capital_reduction_tasks()
            with self.store.connect() as db:
                expected = db.execute(
                    "SELECT value FROM settings WHERE key='sync_expected_date'"
                ).fetchone()
                db.execute(
                    "UPDATE tasks SET state='pending',run_id=NULL,error=NULL "
                    "WHERE state IN ('failed','running')"
                )
                db.execute(
                    "UPDATE source_blocks SET state='expired' "
                    "WHERE state='cooldown' AND retry_at<=?",
                    (stamp(self.now),),
                )
            return self._run_locked(
                "resume",
                [],
                max_tasks,
                False,
                expected[0] if expected else None,
                already_scheduled=True,
            )

    @staticmethod
    def _validate_max(max_tasks: int | None) -> None:
        if max_tasks is not None and max_tasks < 1:
            raise ValueError("max-tasks must be positive")

    @staticmethod
    def _logical_key(
        source: object, dataset: object, params: object, expected_date: object = None
    ) -> str:
        return hashlib.sha256(
            json.dumps([source, dataset, params, expected_date], sort_keys=True).encode()
        ).hexdigest()

    def _repair_legacy_capital_reduction_tasks(self) -> None:
        legacy = "TaiwanStockCapitalReduction"
        replacement = "TaiwanStockCapitalReductionReferencePrice"
        with self.store.connect() as db:
            rows = db.execute(
                "SELECT * FROM tasks WHERE source='finmind' AND dataset=? "
                "AND state IN ('pending','failed','running') ORDER BY rowid",
                (legacy,),
            ).fetchall()
            for old in rows:
                params = json.loads(old["params"])
                key = self._logical_key(old["source"], replacement, params, old["expected_date"])
                found = db.execute(
                    "SELECT id FROM tasks WHERE logical_key=? ORDER BY edition DESC LIMIT 1",
                    (key,),
                ).fetchone()
                if found:
                    replacement_id = found["id"]
                else:
                    replacement_id = self._id()
                    db.execute(
                        "INSERT INTO tasks(id,logical_key,edition,source,dataset,params,kind,state,"
                        "expected_date) "
                        "VALUES(?,?,?,?,?,?,?,'pending',?)",
                        (
                            replacement_id,
                            key,
                            1,
                            old["source"],
                            replacement,
                            old["params"],
                            old["kind"],
                            old["expected_date"],
                        ),
                    )
                original_error = old["error"]
                note = json.dumps(
                    {"original_error": original_error, "replacement_task_id": replacement_id},
                    ensure_ascii=False,
                    sort_keys=True,
                )
                db.execute(
                    "UPDATE tasks SET state='superseded',error=? WHERE id=?",
                    (note, old["id"]),
                )

    def _schedule(self, tasks: list[dict[str, object]], refresh: bool, runid: str) -> list[str]:
        claimed: list[str] = []
        with self.store.connect() as db:
            for task in tasks:
                params = task["params"]
                expected_date = task.get("expected_date")
                key = self._logical_key(task["source"], task["dataset"], params, expected_date)
                row = db.execute(
                    "SELECT id,state,edition FROM tasks WHERE logical_key=? "
                    "ORDER BY edition DESC LIMIT 1",
                    (key,),
                ).fetchone()
                if row:
                    if row["state"] == "completed":
                        if not refresh:
                            continue
                        edition = int(row["edition"]) + 1
                    elif row["state"] in ("pending", "failed") and not refresh:
                        db.execute(
                            "UPDATE tasks SET state='pending',run_id=?,error=NULL WHERE id=?",
                            (runid, row["id"]),
                        )
                        claimed.append(row["id"])
                        continue
                    elif row["state"] in ("pending", "failed"):
                        edition = int(row["edition"]) + 1
                    else:
                        continue
                else:
                    edition = 1
                taskid = self._id()
                db.execute(
                    "INSERT INTO tasks(id,logical_key,edition,source,dataset,params,kind,state,"
                    "run_id,expected_date) "
                    "VALUES(?,?,?,?,?,?,?,'pending',?,?)",
                    (
                        taskid,
                        key,
                        edition,
                        task["source"],
                        task["dataset"],
                        json.dumps(params, sort_keys=True),
                        task["kind"],
                        runid,
                        expected_date,
                    ),
                )
                claimed.append(taskid)
        return claimed

    def _run_locked(
        self,
        kind: str,
        tasks: list[dict[str, object]],
        max_tasks: int | None,
        refresh: bool,
        expected_date: str | None = None,
        already_scheduled: bool = False,
    ) -> dict[str, object]:
        self.stopping = False
        self.store.recover_stale_runs()
        runid, started = self._id(), stamp(self.now)
        self.store.insert_row(
            "runs",
            {
                "id": runid,
                "kind": kind,
                "started_at": started,
                "updated_at": started,
                "state": "running",
                "pid": os.getpid(),
                "counts": "{}",
            },
        )
        ids = (
            self._schedule(tasks, refresh, runid)
            if not already_scheduled
            else self._claim_all(runid)
        )
        old = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}

        def stop(signum: int, frame: object) -> None:
            self.stopping = True

        completed_now, terminal, error = 0, "completed", None
        try:
            for sig in old:
                signal.signal(sig, stop)
            while not self.stopping and ids:
                if max_tasks is not None and completed_now >= max_tasks:
                    terminal = "paused"
                    break
                taskid = ids.pop(0)
                with self.store.connect() as db:
                    task = db.execute(
                        "SELECT * FROM tasks WHERE id=? AND run_id=? AND state='pending'",
                        (taskid, runid),
                    ).fetchone()
                if task is None:
                    continue
                block = self._active_block(task["source"])
                if block:
                    terminal, error = "blocked", f"{task['source']} is blocked: {block}"
                    break
                self._task_state(taskid, "running", runid)
                try:
                    if task["source"] == "local":
                        ids.extend(self._execute_local(task, runid))
                    else:
                        self._execute(task, runid, task["expected_date"])
                    completed_now += 1
                except (
                    sources.QuotaError,
                    sources.AccessBlockedError,
                    sources.CaptchaError,
                ) as exc:
                    if not getattr(exc, "raw_saved", False):
                        self._save_error(getattr(exc, "raw", None), task, str(exc))
                    retry = self._cooldown(exc) if isinstance(exc, sources.QuotaError) else None
                    state = "cooldown" if retry else "blocked"
                    with self.store.connect() as db:
                        db.execute(
                            "INSERT OR REPLACE INTO source_blocks VALUES(?,?,?,?,?)",
                            (task["source"], str(exc), state, stamp(self.now), retry),
                        )
                    self._task_state(taskid, "pending", runid, str(exc))
                    terminal, error = ("partial" if retry else "blocked"), str(exc)
                    break
                except _PublicationPending as exc:
                    if self.stopping:
                        self._task_state(taskid, "pending", runid, str(exc))
                        terminal, error = "interrupted", str(exc)
                    else:
                        self._task_state(taskid, "failed", runid, str(exc))
                        terminal, error = "partial", str(exc)
                    break
                except Exception as exc:
                    self._task_state(taskid, "failed", runid, str(exc))
                    terminal, error = "failed", str(exc)
                    break
            if self.stopping:
                terminal = "interrupted"
                with self.store.connect() as db:
                    db.execute(
                        "UPDATE tasks SET state='pending' WHERE run_id=? "
                        "AND state IN ('pending','running')",
                        (runid,),
                    )
            counts = self._counts(runid)
            if terminal == "completed" and counts["pending"]:
                terminal = "partial"
            if terminal == "completed" and counts["failed"]:
                terminal = "partial"
        finally:
            for sig, handler in old.items():
                signal.signal(sig, handler)
        counts = self._counts(runid)
        finished = stamp(self.now)
        with self.store.connect() as db:
            db.execute(
                "UPDATE runs SET updated_at=?,finished_at=?,state=?,counts=?,error=? WHERE id=?",
                (finished, finished, terminal, json.dumps(counts), error, runid),
            )
        return {"run_id": runid, "state": terminal, **counts, "error": error}

    def _claim_all(self, runid: str) -> list[str]:
        with self.store.connect() as db:
            rows = db.execute(
                "SELECT id FROM tasks WHERE state='pending' ORDER BY rowid"
            ).fetchall()
            ids = [row[0] for row in rows]
            db.executemany("UPDATE tasks SET run_id=? WHERE id=?", [(runid, item) for item in ids])
            return ids

    def _counts(self, runid: str) -> dict[str, int]:
        with self.store.connect() as db:
            values = {
                state: db.execute(
                    "SELECT COUNT(*) FROM tasks WHERE run_id=? AND state=?", (runid, state)
                ).fetchone()[0]
                for state in ("pending", "running", "completed", "failed")
            }
            values["planned"] = sum(values.values())
            return values

    def _active_block(self, source: str) -> str | None:
        with self.store.connect() as db:
            row = db.execute(
                "SELECT reason,state,retry_at FROM source_blocks WHERE source=?", (source,)
            ).fetchone()
            if not row:
                return None
            if row["state"] == "blocked":
                return str(row["reason"])
            if row["state"] == "cooldown":
                if row["retry_at"] and row["retry_at"] > stamp(self.now):
                    return str(row["reason"])
                db.execute("UPDATE source_blocks SET state='expired' WHERE source=?", (source,))
            return None

    def _task_state(
        self,
        taskid: str,
        state: str,
        runid: str,
        error: str | None = None,
        snapshot: str | None = None,
    ) -> None:
        with self.store.connect() as db:
            db.execute(
                "UPDATE tasks SET state=?,run_id=?,error=?,snapshot_id=COALESCE(?,snapshot_id) "
                "WHERE id=?",
                (state, runid, error, snapshot, taskid),
            )

    def _execute_local(self, task: sqlite3.Row, runid: str) -> list[str]:
        params = json.loads(task["params"])
        name = task["dataset"]
        if name == "plan_history":
            candidates = TaiwanData(self.store.root).symbols(
                include_excluded=False, source="finmind"
            )
            if not candidates:
                raise WorkflowError("plan_history has no verified provider candidates")
            start, end, refresh = params["start"], params["end"], bool(params.get("refresh"))
            with self.store.connect() as db:
                db.execute("INSERT OR REPLACE INTO settings VALUES('track_all','1')")
                db.execute("INSERT OR REPLACE INTO settings VALUES('backfill_start',?)", (start,))
            planned = [
                self._fin_task(
                    ds, {"data_id": str(row["symbol"]), "start_date": start, "end_date": end}
                )
                for row in candidates
                for ds in DATASETS
            ]
            child_ids = self._schedule(planned, refresh, runid)
        elif name == "plan_sync":
            today = date.fromisoformat(params["end"])
            with self.store.connect() as db:
                prices = db.execute(
                    "SELECT params FROM tasks WHERE dataset='TaiwanStockPrice' "
                    "AND state='completed'"
                ).fetchall()
                track = db.execute("SELECT value FROM settings WHERE key='track_all'").fetchone()
                start_row = db.execute(
                    "SELECT value FROM settings WHERE key='backfill_start'"
                ).fetchone()
                latest_candidates = (
                    [
                        str(row["symbol"])
                        for row in TaiwanData(self.store.root).symbols(
                            include_excluded=False, source="finmind"
                        )
                    ]
                    if track
                    else []
                )
            tracked: dict[str, dict[str, str]] = {}
            for row in prices:
                p = json.loads(row[0])
                symbol = p.get("data_id")
                if symbol:
                    current = tracked.setdefault(symbol, {})
                    current["TaiwanStockPrice"] = min(
                        current.get("TaiwanStockPrice", p["start_date"]), p["start_date"]
                    )
            sync_plan: list[dict[str, object]] = []
            for symbol, history in tracked.items():
                for dataset in EVENTS:
                    # Use the latest completed interval for this symbol/dataset when available.
                    with self.store.connect() as db:
                        found = db.execute(
                            "SELECT params FROM tasks WHERE dataset=? AND state='completed' "
                            "AND json_extract(params,'$.data_id')=? ORDER BY rowid DESC LIMIT 1",
                            (dataset, symbol),
                        ).fetchone()
                    lower = (
                        json.loads(found[0])["end_date"] if found else history["TaiwanStockPrice"]
                    )
                    begin = max(
                        date.fromisoformat(history["TaiwanStockPrice"]),
                        date.fromisoformat(lower) - timedelta(days=30),
                    )
                    sync_plan.append(
                        self._fin_task(
                            dataset,
                            {
                                "data_id": symbol,
                                "start_date": begin.isoformat(),
                                "end_date": today.isoformat(),
                            },
                            "event_tail",
                        )
                    )
            if track:
                start = start_row[0] if start_row else "2018-01-01"
                for symbol in latest_candidates:
                    if symbol not in tracked:
                        sync_plan.extend(
                            self._fin_task(
                                ds,
                                {
                                    "data_id": symbol,
                                    "start_date": start,
                                    "end_date": today.isoformat(),
                                },
                            )
                            for ds in DATASETS
                        )
            child_ids = self._schedule(sync_plan, True, runid)
        else:
            raise WorkflowError(f"unknown local task: {name}")
        self._task_state(task["id"], "completed", runid)
        return child_ids if name in ("plan_history", "plan_sync") else []

    def _execute(self, task: sqlite3.Row, runid: str, expected_date: str | None) -> None:
        params, source, dataset = json.loads(task["params"]), task["source"], task["dataset"]
        attempts = 0
        while True:
            if self.stopping:
                raise _PublicationPending("stopping")
            try:
                if source == "finmind":
                    self._throttle()
                with self.store.connect() as db:
                    db.execute("UPDATE tasks SET attempts=attempts+1 WHERE id=?", (task["id"],))
                if self.stopping:
                    raise _PublicationPending("stopping")
                raw = sources.request(
                    source,
                    dataset=dataset,
                    params=params,
                    token=self.token,
                    transport=self.transport,
                )
                break
            except sources.HttpError as exc:
                if exc.raw and not exc.raw_saved:
                    self._save_error(exc.raw, task, str(exc))
                if attempts >= 2 or exc.raw is not None and exc.raw.status < 500:
                    raise
                attempts += 1
                self._wait(2**attempts)
            except (sources.AccessBlockedError, sources.CaptchaError, sources.QuotaError):
                raise
        raw_path = self.store.save_raw(source, raw.body, raw.fetched_at)
        records: tuple[dict[str, object], ...] = ()
        try:
            parsed = (
                sources.parse_exchange_csv(raw)
                if source in ("twse", "tpex")
                else sources.parse_finmind(raw, params)
            )
            records = tuple(parsed.records)
            if (
                expected_date
                and source in ("twse", "tpex")
                and any(r.get("trade_date") != expected_date for r in records)
            ):
                self.store.save_snapshot(
                    source=source,
                    dataset=dataset,
                    params=params,
                    safe_url=raw.url,
                    fetched_at=raw.fetched_at,
                    http_status=raw.status,
                    sha256=raw.sha256,
                    raw_path=raw_path,
                    scope_key=str(records[0].get("trade_date")) if records else "unknown",
                    validation_state="valid",
                    error="awaiting_publication",
                    records=records,
                )
                raise _PublicationPending("expected-date mismatch")
            scope = (
                str(records[0].get("trade_date"))
                if source in ("twse", "tpex") and records
                else json.dumps(params, sort_keys=True)
            )
            sid = self.store.save_snapshot(
                source=source,
                dataset=dataset,
                params=params,
                safe_url=raw.url,
                fetched_at=raw.fetched_at,
                http_status=raw.status,
                sha256=raw.sha256,
                raw_path=raw_path,
                scope_key=scope,
                validation_state="valid",
                records=records,
            )
            self._task_state(task["id"], "completed", runid, snapshot=sid)
        except _PublicationPending:
            raise
        except Exception as exc:
            self.store.save_snapshot(
                source=source,
                dataset=dataset,
                params=params,
                safe_url=raw.url,
                fetched_at=raw.fetched_at,
                http_status=raw.status,
                sha256=raw.sha256,
                raw_path=raw_path,
                scope_key=json.dumps(params, sort_keys=True),
                validation_state="invalid",
                error=str(exc),
            )
            if isinstance(exc, sources.SourceError):
                exc.raw_saved = True
            raise

    def _save_error(self, raw: sources.RawBatch | None, task: sqlite3.Row, error: str) -> None:
        if raw is None:
            return
        path = self.store.save_raw(raw.source, raw.body, raw.fetched_at)
        self.store.save_snapshot(
            source=raw.source,
            dataset=raw.dataset,
            params=json.loads(task["params"]),
            safe_url=raw.url,
            fetched_at=raw.fetched_at,
            http_status=raw.status,
            sha256=raw.sha256,
            raw_path=path,
            scope_key=json.dumps(json.loads(task["params"]), sort_keys=True),
            validation_state="invalid",
            error=error,
        )

    def _wait(self, seconds: float) -> None:
        remaining = seconds
        while remaining > 0 and not self.stopping:
            step = min(remaining, 10)
            self.sleep(step)
            remaining -= step
            with self.store.connect() as db:
                db.execute("UPDATE runs SET updated_at=? WHERE state='running'", (stamp(self.now),))

    def _throttle(self) -> None:
        interval = 3600 / self.limit + 0.1
        while not self.stopping:
            current = self.now().astimezone(UTC)
            with self.store.connect() as db:
                vals = [
                    datetime.fromisoformat(r[0])
                    for r in db.execute(
                        "SELECT requested_at FROM request_log WHERE source='finmind' "
                        "AND requested_at>=? ORDER BY requested_at",
                        ((current - timedelta(hours=1)).isoformat(),),
                    )
                ]
            waits = [interval - (current - vals[-1]).total_seconds()] if vals else []
            if len(vals) >= self.limit:
                waits.append(3600 - (current - vals[0]).total_seconds() + 0.1)
            delay = max(waits + [0])
            if delay <= 0:
                self.store.log_request("finmind", current.isoformat())
                return
            until = current + timedelta(seconds=delay)
            with self.store.connect() as db:
                db.execute(
                    "UPDATE runs SET updated_at=?,counts=json_set(counts,'$.waiting_until',?) "
                    "WHERE state='running'",
                    (stamp(self.now), until.isoformat()),
                )
            self._wait(min(delay, 10))
        raise _PublicationPending("stopping")

    def _cooldown(self, exc: Exception) -> str:
        raw = getattr(exc, "raw", None)
        value, seconds = getattr(raw, "retry_after", None), 0
        if value:
            try:
                seconds = max(0, int(value))
            except ValueError:
                try:
                    seconds = max(
                        0,
                        int((parsedate_to_datetime(value) - self.now()).total_seconds()),
                    )
                except (ValueError, TypeError, OverflowError):
                    pass
        delay = max(3600, seconds)
        current = self.now()
        try:
            retry_at = current + timedelta(seconds=delay)
        except OverflowError:
            retry_at = datetime.max.replace(tzinfo=UTC)
        return retry_at.isoformat()


class _PublicationPending(Exception):
    pass
