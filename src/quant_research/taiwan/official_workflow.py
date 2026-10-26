"""Persistent, resumable orchestration for official daily quote snapshots."""

from __future__ import annotations

import email.utils
import fcntl
import hashlib
import json
import os
import signal
import sqlite3
import threading
import time
import uuid
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import FrameType

from . import official, sources
from .storage import DEFAULT_ROOT, SQLiteStore, WriterBusyError


class OfficialWorkflow:
    def __init__(
        self,
        root: str | Path = DEFAULT_ROOT,
        *,
        transport: sources.Transport = sources.urllib_transport,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.store = SQLiteStore(root)
        self.transport = transport
        self.now = now
        self.sleep = sleep
        self._stop = threading.Event()
        self._event_children: list[dict[str, object]] = []

    def _validate_max(self, value: int | None) -> None:
        if value is not None and (
            isinstance(value, bool) or not isinstance(value, int) or value <= 0
        ):
            raise ValueError("max_tasks must be a positive integer")

    def sync_events(
        self,
        start: str,
        end: str | None = None,
        *,
        markets: list[str] | tuple[str, ...] | None = None,
        max_tasks: int | None = None,
        refresh: bool = False,
    ) -> dict[str, object]:
        self._validate_max(max_tasks)
        first = date.fromisoformat(self._day(start))
        last = (
            date.fromisoformat(self._day(end))
            if end is not None
            else date.fromisoformat(self._today())
        )
        if first > last:
            raise ValueError("start must not exceed end")
        selected = self._markets(markets)
        months: list[tuple[date, date]] = []
        cursor = first.replace(day=1)
        while cursor <= last:
            month_end = (cursor.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(
                days=1
            )
            months.append((max(first, cursor), min(last, month_end)))
            cursor = month_end + timedelta(days=1)
        datasets = {
            "twse": ("TWT49U", "TWTAUU", "TWTB8U"),
            "tpex": ("exDailyQ", "revivt", "pvChgRslt"),
        }
        with self._runner_lock("events"):
            with self.store.writer_lock():
                self.store.initialize()
                self._recover_events()
                tasks = self._plan_events(months, selected, refresh, datasets)
            return self._execute("events:sync", tasks, max_tasks)

    def maintain_events(
        self,
        expected_date: str | None = None,
        max_tasks: int | None = None,
        *,
        markets: list[str] | tuple[str, ...] | None = None,
        recent_months: int = 2,
        detail_refresh_days: int = 7,
    ) -> dict[str, object]:
        self._validate_max(max_tasks)
        if (
            isinstance(recent_months, bool)
            or not isinstance(recent_months, int)
            or not 1 <= recent_months <= 12
        ):
            raise ValueError("recent_months must be an integer from 1 to 12")
        if (
            isinstance(detail_refresh_days, bool)
            or not isinstance(detail_refresh_days, int)
            or not 1 <= detail_refresh_days <= 365
        ):
            raise ValueError("detail_refresh_days must be an integer from 1 to 365")
        as_of = self._day(expected_date) if expected_date is not None else self._today()
        selected = self._markets(markets)
        policy = {
            "markets": list(selected),
            "recent_months": recent_months,
            "detail_refresh_days": detail_refresh_days,
        }
        datasets = {
            "twse": ("TWT49U", "TWTAUU", "TWTB8U"),
            "tpex": ("exDailyQ", "revivt", "pvChgRslt"),
        }
        with self._runner_lock("events"):
            with self.store.writer_lock():
                self.store.initialize()
                self._recover_events()
                active_text = self._setting("official_events_maintenance_active")
                last_text = self._setting("official_events_maintenance_last")
                if active_text:
                    active = json.loads(active_text)
                    if active.get("policy") != policy:
                        raise ValueError(
                            "active maintenance cycle policy differs; resume with matching policy"
                        )
                    cycle_as_of = str(active["as_of"])
                    logicals = list(active["logical_keys"])
                else:
                    if last_text:
                        last = json.loads(last_text)
                        if last.get("as_of") == as_of and last.get("policy") == policy:
                            return {
                                "state": str(last.get("state", "completed")),
                                "noop": True,
                                "active": False,
                                "requested_as_of": as_of,
                                "cycle_as_of": as_of,
                                "lag_days": 0,
                                "counts": {"claimed": 0, "completed": 0, "failed": 0, "pending": 0},
                            }
                    cycle_as_of = as_of
                    today = date.fromisoformat(as_of)
                    recent_start = today.replace(day=1)
                    for _ in range(recent_months - 1):
                        recent_start = (recent_start.replace(day=1) - timedelta(days=1)).replace(
                            day=1
                        )
                    months = []
                    cursor = recent_start
                    while cursor <= today:
                        month_end = (cursor.replace(day=28) + timedelta(days=4)).replace(
                            day=1
                        ) - timedelta(days=1)
                        months.append((cursor, month_end))
                        cursor = month_end + timedelta(days=1)
                    with self.store.connect() as db:
                        existing = db.execute(
                            "SELECT * FROM tasks WHERE kind='official_event_summary' "
                            "AND source IN ('twse','tpex') ORDER BY logical_key,edition DESC"
                        ).fetchall()
                        cold = []
                        newest: set[str] = set()
                        recent_start_s = recent_start.isoformat()
                        for row in existing:
                            logical_key = str(row["logical_key"])
                            if logical_key in newest:
                                continue
                            newest.add(logical_key)
                            try:
                                env = json.loads(row["params"])
                                req = env["request"]
                                if (
                                    env.get("pipeline") == "official-v1"
                                    and env.get("lane") == "events"
                                    and set(req) == {"start_date", "end_date"}
                                    and self._day(req["start_date"]) == req["start_date"]
                                    and self._day(req["end_date"]) == req["end_date"]
                                    and req["start_date"] <= req["end_date"]
                                    and req["start_date"][:7] == req["end_date"][:7]
                                    and req["end_date"] < recent_start_s
                                    and row["source"] in selected
                                    and row["dataset"] in datasets.get(row["source"], ())
                                ):
                                    cold.append((logical_key, row["source"], row["dataset"], req))
                            except (ValueError, KeyError, TypeError, AttributeError):
                                continue
                    cold.sort(key=lambda item: item[0])
                    cursor_key = (last or {}).get("cold_cursor") if last_text else None
                    selected_cold = next(
                        (c for c in cold if cursor_key is None or c[0] > cursor_key),
                        cold[0] if cold else None,
                    )
                    scopes = [
                        (
                            source,
                            dataset,
                            {"start_date": start.isoformat(), "end_date": end.isoformat()},
                        )
                        for start, end in months
                        for source in selected
                        for dataset in datasets[source]
                    ]
                    if selected_cold and selected_cold[1] in selected:
                        scopes.append((selected_cold[1], selected_cold[2], dict(selected_cold[3])))
                    logicals = []
                    for source, dataset, request in scopes:
                        logicals.append(
                            hashlib.sha256(
                                json.dumps(
                                    ["official-v1", "events", source, dataset, request],
                                    sort_keys=True,
                                    separators=(",", ":"),
                                ).encode()
                            ).hexdigest()
                        )
                    self._plan_events(
                        months,
                        selected,
                        False,
                        datasets,
                        context={
                            "maintenance": {
                                "version": 1,
                                "detail_refresh_days": detail_refresh_days,
                            }
                        },
                        refresh_completed=True,
                    )
                    # Plan the chosen historical request without widening its original interval.
                    if selected_cold and selected_cold[1] in selected:
                        self._plan_events(
                            [
                                (
                                    date.fromisoformat(selected_cold[3]["start_date"]),
                                    date.fromisoformat(selected_cold[3]["end_date"]),
                                )
                            ],
                            (selected_cold[1],),
                            False,
                            {selected_cold[1]: (selected_cold[2],)},
                            context={
                                "maintenance": {
                                    "version": 1,
                                    "detail_refresh_days": detail_refresh_days,
                                }
                            },
                            refresh_completed=True,
                        )
                    active = {
                        "version": 1,
                        "as_of": cycle_as_of,
                        "policy": policy,
                        "logical_keys": logicals,
                        "cold_key": selected_cold[0] if selected_cold else None,
                    }
                    with self.store.connect() as db:
                        db.execute(
                            "INSERT INTO settings(key,value) VALUES(?,?) "
                            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                            (
                                "official_events_maintenance_active",
                                json.dumps(active, sort_keys=True),
                            ),
                        )
                tasks = []
                with self.store.connect() as db:
                    for key in logicals:
                        row = db.execute(
                            "SELECT * FROM tasks WHERE logical_key=? ORDER BY edition DESC LIMIT 1",
                            (key,),
                        ).fetchone()
                        if row and row["state"] in {"pending", "failed"}:
                            tasks.append(dict(row))
                        if row and row["state"] == "completed":
                            children = db.execute(
                                "SELECT DISTINCT c.* FROM official_event_links l "
                                "JOIN tasks c ON c.id=l.child_task_id "
                                "WHERE l.parent_task_id=? AND c.state IN ('pending','failed')",
                                (row["id"],),
                            ).fetchall()
                            tasks.extend(dict(child) for child in children)
            result = self._execute("events:maintain", tasks, max_tasks)
            with self.store.writer_lock():
                unresolved = False
                cycle_empty = cycle_warning = 0
                detail_empty = detail_warning = 0
                seen_child_ids: set[str] = set()
                with self.store.connect() as db:
                    for key in logicals:
                        row = db.execute(
                            "SELECT * FROM tasks WHERE logical_key=? ORDER BY edition DESC LIMIT 1",
                            (key,),
                        ).fetchone()
                        if row is None:
                            unresolved = True
                            continue
                        if row["state"] in {"pending", "failed", "running"}:
                            unresolved = True
                        if row["state"] == "completed":
                            if not row["snapshot_id"]:
                                unresolved = True
                            else:
                                summary_validation = db.execute(
                                    "SELECT validation_state FROM snapshots WHERE id=?",
                                    (row["snapshot_id"],),
                                ).fetchone()
                                if summary_validation is None:
                                    unresolved = True
                                elif summary_validation[0] == "empty_unverified":
                                    cycle_empty += 1
                                elif summary_validation[0] == "period_envelope_unverified":
                                    cycle_warning += 1
                        if row and row["snapshot_id"]:
                            child_rows = db.execute(
                                "SELECT DISTINCT c.id,c.state,c.snapshot_id,s.validation_state "
                                "FROM official_event_links l "
                                "JOIN tasks c ON c.id=l.child_task_id "
                                "LEFT JOIN snapshots s ON s.id=c.snapshot_id "
                                "WHERE l.parent_task_id=? AND l.parent_snapshot_id=?",
                                (row["id"], row["snapshot_id"]),
                            ).fetchall()
                            for child in child_rows:
                                if child["id"] in seen_child_ids:
                                    continue
                                seen_child_ids.add(child["id"])
                                if child["state"] in {"pending", "failed", "running"}:
                                    unresolved = True
                                if child["state"] == "completed":
                                    if child["validation_state"] == "empty_unverified":
                                        detail_empty += 1
                                    elif child["validation_state"] == "period_envelope_unverified":
                                        detail_warning += 1
                    if result.get("state") not in {"blocked", "failed", "interrupted"} and (
                        cycle_empty or cycle_warning or detail_empty or detail_warning
                    ):
                        result["state"] = "partial"
                    result["cycle_counts"] = {
                        "summary_empty_unverified": cycle_empty,
                        "summary_unverified_period": cycle_warning,
                        "detail_empty_unverified": detail_empty,
                        "detail_unverified_period": detail_warning,
                    }
                    if unresolved and result.get("state") == "completed":
                        result["state"] = "paused"
                    if not unresolved:
                        last_value = {
                            "version": 1,
                            "as_of": cycle_as_of,
                            "policy": policy,
                            "cold_cursor": active.get("cold_key"),
                            "state": result.get("state", "completed"),
                        }
                        db.execute(
                            "INSERT INTO settings(key,value) VALUES(?,?) "
                            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                            (
                                "official_events_maintenance_last",
                                json.dumps(last_value, sort_keys=True),
                            ),
                        )
                        db.execute(
                            "DELETE FROM settings WHERE key='official_events_maintenance_active'"
                        )
            result.update(
                {
                    "requested_as_of": as_of,
                    "cycle_as_of": cycle_as_of,
                    "lag_days": (date.fromisoformat(as_of) - date.fromisoformat(cycle_as_of)).days,
                    "active": unresolved,
                }
            )
            return result

    def backfill(
        self,
        start: str = "2018-01-01",
        end: str | None = None,
        *,
        markets: list[str] | tuple[str, ...] | None = None,
        max_tasks: int | None = None,
        refresh: bool = False,
    ) -> dict[str, object]:
        self._validate_max(max_tasks)
        first, last = self._day(start), self._day(end) if end else self._today()
        if first > last:
            raise ValueError("start must not exceed end")
        selected = self._markets(markets)
        with self._runner_lock():
            with self.store.writer_lock():
                self.store.initialize()
                self._recover()
                tasks = self._plan_dates(self._dates(first, last), selected, refresh)
            return self._execute("backfill", tasks, max_tasks)

    def sync(
        self,
        expected_date: str | None = None,
        max_tasks: int | None = None,
        *,
        markets: list[str] | tuple[str, ...] | None = None,
    ) -> dict[str, object]:
        self._validate_max(max_tasks)
        today, selected = (
            self._day(expected_date) if expected_date else self._today(),
            self._markets(markets),
        )
        with self._runner_lock():
            with self.store.writer_lock():
                self.store.initialize()
                start = self._setting("official_quotes_start")
                dates = self._dates(self._day(start), today) if start else [today]
                self._recover()
                tasks = self._plan_dates(dates, selected, False, recent_refresh=True)
            return self._execute("sync", tasks, max_tasks)

    def resume(self, max_tasks: int | None = None, *, lane: str = "quotes") -> dict[str, object]:
        self._validate_max(max_tasks)
        if lane not in {"quotes", "events"}:
            raise ValueError(f"lane {lane!r} is not implemented")
        if lane == "events":
            with self._runner_lock("events"):
                with self.store.writer_lock():
                    self.store.initialize()
                    self._recover_events()
                    with self.store.connect() as db:
                        legacy = db.execute(
                            "SELECT COUNT(*) FROM tasks WHERE source NOT IN ('twse','tpex') "
                            "OR params NOT LIKE '%official-v1%'"
                        ).fetchone()[0]
                        rows = db.execute(
                            "SELECT * FROM tasks WHERE state IN ('pending','failed') "
                            "AND source IN ('twse','tpex') "
                            "ORDER BY expected_date DESC, source, dataset"
                        ).fetchall()
                        tasks = [
                            dict(row)
                            for row in rows
                            if self._envelope_lane(row["params"], "events")
                            and self._latest(db, row)
                        ]
                return self._execute("events:resume", tasks, max_tasks, legacy=int(legacy))
        with self._runner_lock():
            with self.store.writer_lock():
                self.store.initialize()
                self._recover()
                with self.store.connect() as db:
                    all_rows = db.execute(
                        "SELECT * FROM tasks "
                        "WHERE state IN ('pending','failed','running') "
                        "AND source IN ('twse','tpex') "
                        "ORDER BY expected_date DESC, source"
                    ).fetchall()
                    rows = [dict(row) for row in all_rows if self._envelope(row["params"])]
                    tasks = []
                    for row in rows:
                        latest = db.execute(
                            "SELECT id,state FROM tasks WHERE logical_key=? "
                            "ORDER BY edition DESC LIMIT 1",
                            (row["logical_key"],),
                        ).fetchone()
                        if (
                            latest
                            and latest["id"] == row["id"]
                            and latest["state"] in {"pending", "failed"}
                        ):
                            tasks.append(row)
                    legacy = db.execute(
                        "SELECT COUNT(*) FROM tasks "
                        "WHERE source NOT IN ('twse','tpex') "
                        "OR params NOT LIKE '%official-v1%'"
                    ).fetchone()[0]
            return self._execute("resume", tasks, max_tasks, legacy=int(legacy))

    @staticmethod
    def _envelope_lane(encoded: str, lane: str) -> bool:
        try:
            value = json.loads(encoded)
            return (
                isinstance(value, dict)
                and value.get("pipeline") == "official-v1"
                and value.get("lane") == lane
            )
        except (ValueError, AttributeError):
            return False

    @staticmethod
    def _latest(db: sqlite3.Connection, row: sqlite3.Row) -> bool:
        latest = db.execute(
            "SELECT id FROM tasks WHERE logical_key=? ORDER BY edition DESC LIMIT 1",
            (row["logical_key"],),
        ).fetchone()
        return bool(latest is not None and latest["id"] == row["id"])

    def _recover_events(self) -> None:
        stamp = self.now().astimezone(UTC).isoformat()
        with self.store.connect() as db:
            db.execute(
                "UPDATE runs SET state='interrupted',updated_at=?,finished_at=?, "
                "error='recovered stale events run' WHERE state='running' "
                "AND kind LIKE 'official-events:%'",
                (stamp, stamp),
            )
            db.execute(
                "UPDATE tasks SET state='pending',error='recovered stale events task' "
                "WHERE state='running' AND source IN ('twse','tpex') "
                "AND json_extract(params,'$.pipeline')='official-v1' "
                "AND json_extract(params,'$.lane')='events'"
            )

    def _plan_events(
        self,
        months: list[tuple[date, date]],
        markets: tuple[str, ...],
        refresh: bool,
        datasets: Mapping[str, tuple[str, ...]],
        *,
        context: dict[str, object] | None = None,
        refresh_completed: bool = False,
    ) -> list[dict[str, object]]:
        planned = []
        planned_ids: set[str] = set()
        with self.store.connect() as db:
            for start, end in reversed(months):
                for source in markets:
                    for dataset in datasets[source]:
                        request = {"start_date": start.isoformat(), "end_date": end.isoformat()}
                        params = {
                            "pipeline": "official-v1",
                            "lane": "events",
                            "request": request,
                            "context": context or {},
                        }
                        encoded = json.dumps(params, sort_keys=True, separators=(",", ":"))
                        logical = hashlib.sha256(
                            json.dumps(
                                ["official-v1", "events", source, dataset, request],
                                sort_keys=True,
                                separators=(",", ":"),
                            ).encode()
                        ).hexdigest()
                        row = db.execute(
                            "SELECT * FROM tasks WHERE logical_key=? ORDER BY edition DESC LIMIT 1",
                            (logical,),
                        ).fetchone()
                        if (
                            row
                            and row["state"] == "completed"
                            and not refresh
                            and not refresh_completed
                        ):
                            linked = db.execute(
                                "SELECT c.* FROM official_event_links l "
                                "JOIN tasks c ON c.id=l.child_task_id "
                                "WHERE l.parent_task_id=? AND c.source=? "
                                "AND c.state IN ('pending','failed') ORDER BY c.id",
                                (row["id"], source),
                            ).fetchall()
                            for child in linked:
                                if child["id"] not in planned_ids and self._latest(db, child):
                                    planned.append(dict(child))
                                    planned_ids.add(child["id"])
                            continue
                        if row and row["state"] in {"pending", "failed"} and not refresh:
                            if context is not None and row["params"] != encoded:
                                db.execute(
                                    "UPDATE tasks SET params=? WHERE id=?", (encoded, row["id"])
                                )
                                row = db.execute(
                                    "SELECT * FROM tasks WHERE id=?", (row["id"],)
                                ).fetchone()
                            if row["id"] not in planned_ids:
                                planned.append(dict(row))
                                planned_ids.add(row["id"])
                            continue
                        if row and refresh:
                            db.execute(
                                "UPDATE tasks SET state='superseded', "
                                "error=COALESCE(error,'superseded by refresh; new task pending') "
                                "WHERE logical_key=? AND state IN ('pending','failed')",
                                (logical,),
                            )
                        task_id = uuid.uuid4().hex
                        edition = row["edition"] + 1 if row else 1
                        db.execute(
                            "INSERT INTO tasks("
                            "id,logical_key,edition,source,dataset,"
                            "params,kind,state,expected_date) "
                            "VALUES(?,?,?,?,?,?,?,?,?)",
                            (
                                task_id,
                                logical,
                                edition,
                                source,
                                dataset,
                                encoded,
                                "official_event_summary",
                                "pending",
                                start.isoformat(),
                            ),
                        )
                        db.execute(
                            "UPDATE tasks SET error=COALESCE("
                            "error || '; superseded by task ' || ?, "
                            "'superseded by task ' || ?) WHERE logical_key=? AND id<>? "
                            "AND state='superseded' AND error NOT LIKE '%superseded by task %'",
                            (task_id, task_id, logical, task_id),
                        )
                        planned.append(
                            dict(
                                id=task_id,
                                logical_key=logical,
                                edition=edition,
                                source=source,
                                dataset=dataset,
                                params=encoded,
                                kind="official_event_summary",
                                state="pending",
                                expected_date=start.isoformat(),
                            )
                        )
        return planned

    @staticmethod
    def _envelope(encoded: str) -> bool:
        try:
            value = json.loads(encoded)
            return bool(value.get("pipeline") == "official-v1" and value.get("lane") == "quotes")
        except (ValueError, AttributeError):
            return False

    def _today(self) -> str:
        value = self.now()
        if value.tzinfo is None:
            raise ValueError("now() must return timezone-aware datetime")
        from zoneinfo import ZoneInfo

        return value.astimezone(ZoneInfo("Asia/Taipei")).date().isoformat()

    @staticmethod
    def _day(value: str) -> str:
        if not isinstance(value, str) or len(value) != 10:
            raise ValueError("date must be valid YYYY-MM-DD")
        try:
            parsed = date.fromisoformat(value)
        except ValueError:
            raise ValueError("date must be valid YYYY-MM-DD") from None
        if parsed.isoformat() != value:
            raise ValueError("date must be valid YYYY-MM-DD")
        return value

    @staticmethod
    def _dates(start: str, end: str) -> list[str]:
        a, b = date.fromisoformat(start), date.fromisoformat(end)
        return [(a + timedelta(days=i)).isoformat() for i in range((b - a).days + 1)]

    @staticmethod
    def _markets(markets: list[str] | tuple[str, ...] | None) -> tuple[str, ...]:
        if markets is None:
            return ("twse", "tpex")
        result = tuple(dict.fromkeys(markets))
        if not result or any(m not in {"twse", "tpex"} for m in result):
            raise ValueError("markets must be a nonempty selection of twse and tpex")
        return result

    @contextmanager
    def _runner_lock(self, lane: str = "quotes") -> Iterator[None]:
        self.store.root.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.store.root / f"{lane}.runner.lock", os.O_CREAT | os.O_RDWR, 0o600)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise WriterBusyError(f"official {lane} runner is busy") from exc
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def _setting(self, key: str) -> str | None:
        with self.store.connect() as db:
            row = db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
            return str(row[0]) if row else None

    def _recover(self) -> None:
        stamp = self.now().astimezone(UTC).isoformat()
        with self.store.connect() as db:
            rows = db.execute(
                "SELECT id FROM runs WHERE state='running' AND kind LIKE 'official-quotes:%'"
            ).fetchall()
            for row in rows:
                db.execute(
                    "UPDATE runs SET state='interrupted',updated_at=?,finished_at=?, "
                    "error='recovered stale quotes run' WHERE id=?",
                    (stamp, stamp, row[0]),
                )
            db.execute(
                "UPDATE tasks SET state='pending',error='recovered stale quotes task' "
                "WHERE state='running' AND source IN ('twse','tpex') "
                "AND json_extract(params,'$.pipeline')='official-v1' "
                "AND json_extract(params,'$.lane')='quotes'"
            )

    def _plan_dates(
        self,
        dates: list[str],
        markets: tuple[str, ...],
        refresh: bool,
        recent_refresh: bool = False,
    ) -> list[dict[str, object]]:
        self.store.initialize()
        planned: list[dict[str, object]] = []
        with self.store.connect() as db:
            current = db.execute(
                "SELECT value FROM settings WHERE key='official_quotes_start'"
            ).fetchone()
            if dates and (current is None or dates[0] < current[0]):
                db.execute(
                    "INSERT INTO settings(key,value) VALUES('official_quotes_start',?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (dates[0],),
                )
            for day in dates:
                for source in markets:
                    dataset = "MI_INDEX" if source == "twse" else "dailyQuotes"
                    params = {
                        "pipeline": "official-v1",
                        "lane": "quotes",
                        "request": {"date": day},
                        "context": {},
                    }
                    canonical = json.dumps(params, sort_keys=True, separators=(",", ":"))
                    logical = hashlib.sha256(
                        json.dumps(
                            [
                                params["pipeline"],
                                params["lane"],
                                source,
                                dataset,
                                params["request"],
                            ],
                            sort_keys=True,
                            separators=(",", ":"),
                        ).encode()
                    ).hexdigest()
                    existing = db.execute(
                        "SELECT id,edition,state FROM tasks WHERE logical_key=? "
                        "ORDER BY edition DESC LIMIT 1",
                        (logical,),
                    ).fetchone()
                    do_refresh = refresh or (
                        recent_refresh
                        and date.fromisoformat(day)
                        >= date.fromisoformat(dates[-1]) - timedelta(days=6)
                    )
                    if existing and existing["state"] == "completed":
                        if not do_refresh:
                            continue
                    elif existing and do_refresh:
                        db.execute(
                            "UPDATE tasks SET state='superseded', "
                            "error=COALESCE(error,'superseded by refresh') "
                            "WHERE logical_key=? AND state IN ('pending','failed')",
                            (logical,),
                        )
                    elif existing:
                        planned.append(
                            dict(
                                id=existing["id"],
                                source=source,
                                dataset=dataset,
                                params=canonical,
                                expected_date=day,
                            )
                        )
                        continue
                    if do_refresh or not existing:
                        edition = existing["edition"] + 1 if existing else 1
                        task_id = uuid.uuid4().hex
                        db.execute(
                            "INSERT INTO tasks "
                            "(id,logical_key,edition,source,dataset,params,kind,state,"
                            "expected_date) "
                            "VALUES(?,?,?,?,?,?,?,?,?)",
                            (
                                task_id,
                                logical,
                                edition,
                                source,
                                dataset,
                                canonical,
                                "official_quote",
                                "pending",
                                day,
                            ),
                        )
                        planned.append(
                            dict(
                                id=task_id,
                                source=source,
                                dataset=dataset,
                                params=canonical,
                                expected_date=day,
                            )
                        )
        return sorted(
            planned,
            key=lambda task: (str(task["expected_date"]), str(task["source"])),
            reverse=True,
        )

    def _execute(
        self, command: str, tasks: list[dict[str, object]], max_tasks: int | None, legacy: int = 0
    ) -> dict[str, object]:
        self._validate_max(max_tasks)
        run_id = uuid.uuid4().hex
        stamp = self.now().astimezone(UTC).isoformat()
        with self.store.writer_lock():
            self.store.insert_row(
                "runs",
                dict(
                    id=run_id,
                    kind=(
                        "official-events:" if command.startswith("events:") else "official-quotes:"
                    )
                    + command.split(":", 1)[-1],
                    started_at=stamp,
                    updated_at=stamp,
                    state="running",
                    pid=os.getpid(),
                    counts="{}",
                ),
            )
            with self.store.connect() as db:
                for task in tasks:
                    db.execute(
                        "UPDATE tasks SET state='pending',run_id=?,error=NULL "
                        "WHERE id=? AND state IN ('pending','failed')",
                        (run_id, task["id"]),
                    )
                    task["run_id"] = run_id
        self._stop.clear()
        self._event_children = []
        old_handlers: dict[
            signal.Signals,
            signal.Handlers | int | None | Callable[[int, FrameType | None], object],
        ] = {}
        if threading.current_thread() is threading.main_thread():
            for signal_num in (signal.SIGINT, signal.SIGTERM):
                old_handlers[signal_num] = signal.getsignal(signal_num)
                signal.signal(signal_num, lambda _signum, _frame: self._stop.set())
        processed = empty = unverified_period = 0
        errors: list[str] = []
        interrupted = blocked = cooldown = False
        try:
            queue = list(tasks)
            index = 0
            while index < len(queue):
                task = queue[index]
                index += 1
                if self._stop.is_set() or (max_tasks is not None and processed >= max_tasks):
                    break
                with self.store.writer_lock():
                    with self.store.connect() as db:
                        if command.startswith("events:"):
                            latest = db.execute(
                                "SELECT id FROM tasks WHERE logical_key=? "
                                "ORDER BY edition DESC LIMIT 1",
                                (task["logical_key"],),
                            ).fetchone()
                            if latest is None or latest["id"] != task["id"]:
                                continue
                        cursor = db.execute(
                            "UPDATE tasks SET state='running',attempts=attempts+1,error=NULL "
                            "WHERE id=? AND run_id=? AND state='pending'",
                            (task["id"], run_id),
                        )
                        if cursor.rowcount != 1:
                            continue
                processed += 1
                try:
                    result = self._fetch(task)
                    empty += result == "empty"
                    unverified_period += result == "warning"
                    queue.extend(getattr(self, "_event_children", []))
                    self._event_children = []
                except (
                    sources.AccessBlockedError,
                    sources.CaptchaError,
                    sources.QuotaError,
                ) as exc:
                    errors.append(str(exc))
                    blocked = not isinstance(exc, sources.QuotaError)
                    cooldown = isinstance(exc, sources.QuotaError)
                    self._task_state(task, None, "pending", str(exc))
                    break
                except Exception as exc:
                    errors.append(str(exc))
                    if self._stop.is_set() or isinstance(exc, InterruptedError):
                        interrupted = True
                        self._task_state(task, None, "pending", "run interrupted")
                    else:
                        self._task_state(task, None, "failed", str(exc))
                    break
                if self._stop.is_set():
                    interrupted = True
                    self._task_state(task, None, "pending", "run interrupted")
                    break
        finally:
            for signal_num, handler in old_handlers.items():
                signal.signal(signal_num, handler)
        if self._stop.is_set():
            interrupted = True
        with self.store.writer_lock():
            with self.store.connect() as db:
                if interrupted:
                    db.execute(
                        "UPDATE tasks SET state='pending',error='run interrupted' "
                        "WHERE run_id=? AND state='running'",
                        (run_id,),
                    )
                rows = db.execute(
                    "SELECT state,COUNT(*) AS n FROM tasks WHERE run_id=? GROUP BY state",
                    (run_id,),
                ).fetchall()
                empty = db.execute(
                    "SELECT COUNT(*) FROM tasks t JOIN snapshots s ON s.id=t.snapshot_id "
                    "WHERE t.run_id=? AND s.validation_state='empty_unverified'",
                    (run_id,),
                ).fetchone()[0]
                unverified_period = db.execute(
                    "SELECT COUNT(*) FROM tasks t JOIN snapshots s ON s.id=t.snapshot_id "
                    "WHERE t.run_id=? AND s.validation_state='period_envelope_unverified'",
                    (run_id,),
                ).fetchone()[0]
        task_counts = {str(row["state"]): int(row["n"]) for row in rows}
        completed = task_counts.get("completed", 0)
        failed = task_counts.get("failed", 0)
        pending = task_counts.get("pending", 0)
        running = task_counts.get("running", 0)
        state = (
            "interrupted"
            if interrupted
            else "blocked"
            if blocked
            else "failed"
            if failed
            else "partial"
            if empty or cooldown or unverified_period
            else "paused"
            if pending
            else "completed"
        )
        counts = {
            "claimed": sum(task_counts.values()),
            "completed": completed,
            "failed": failed,
            "pending": pending,
            "running": running,
            "empty_unverified": empty,
            "unverified_period": unverified_period,
        }
        final = self.now().astimezone(UTC).isoformat()
        with self.store.writer_lock():
            with self.store.connect() as db:
                db.execute(
                    "UPDATE runs SET state=?,updated_at=?,finished_at=?,counts=?,error=? "
                    "WHERE id=?",
                    (
                        state,
                        final,
                        final,
                        json.dumps(counts),
                        errors[0] if errors else ("empty_unverified" if empty else None),
                        run_id,
                    ),
                )
        return {
            "run_id": run_id,
            "state": state,
            "counts": counts,
            "error": errors[0] if errors else ("empty_unverified" if empty else None),
            "legacy_tasks_ignored": legacy,
        }

    @contextmanager
    def _source_lock(self, source: str) -> Iterator[None]:
        fd = os.open(self.store.root / f"{source}.source.lock", os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def _wait(self, seconds: float) -> None:
        remaining = seconds
        while remaining > 0 and not self._stop.is_set():
            step = min(remaining, 0.2)
            self.sleep(step)
            remaining -= step

    def _fetch(self, task: dict[str, object]) -> str:
        source, dataset = str(task["source"]), str(task["dataset"])
        envelope = json.loads(str(task["params"]))
        request = envelope["request"]
        day = str(request.get("date", request.get("start_date", "")))
        attempt = 0
        while attempt < 3:
            if self._stop.is_set():
                raise InterruptedError("workflow interrupted")
            with self._source_lock(source):
                with self.store.connect() as db:
                    block = db.execute(
                        "SELECT state,retry_at FROM source_blocks WHERE source=?", (source,)
                    ).fetchone()
                    latest = db.execute(
                        "SELECT requested_at FROM request_log WHERE source=? "
                        "ORDER BY id DESC LIMIT 1",
                        (source,),
                    ).fetchone()
                if block and block["state"] in {"blocked", "permanent"}:
                    raise sources.AccessBlockedError(f"{source} is blocked")
                if (
                    block
                    and block["state"] in {"cooldown", "temporary"}
                    and (
                        not block["retry_at"]
                        or block["retry_at"] > self.now().astimezone(UTC).isoformat()
                    )
                ):
                    raise sources.AccessBlockedError(f"{source} is blocked")
                if (
                    block
                    and block["state"] in {"cooldown", "temporary"}
                    and block["retry_at"]
                    and block["retry_at"] <= self.now().astimezone(UTC).isoformat()
                ):
                    with self.store.writer_lock():
                        with self.store.connect() as db:
                            db.execute(
                                "UPDATE source_blocks SET state='expired' WHERE source=?",
                                (source,),
                            )
                if latest:
                    wait = (
                        2
                        - (
                            self.now().astimezone(UTC) - datetime.fromisoformat(latest[0])
                        ).total_seconds()
                    )
                    if wait > 0:
                        # Do not hold the source lock while waiting; recheck after reacquiring.
                        pass
                else:
                    wait = 0
            if wait > 0:
                self._wait(wait)
                if self._stop.is_set():
                    raise InterruptedError("workflow interrupted during throttle")
            with self._source_lock(source):
                with self.store.connect() as db:
                    block = db.execute(
                        "SELECT state,retry_at FROM source_blocks WHERE source=?", (source,)
                    ).fetchone()
                    latest = db.execute(
                        "SELECT requested_at FROM request_log WHERE source=? "
                        "ORDER BY id DESC LIMIT 1",
                        (source,),
                    ).fetchone()
                if block and block["state"] in {"blocked", "permanent"}:
                    raise sources.AccessBlockedError(f"{source} is blocked")
                if (
                    block
                    and block["state"] in {"cooldown", "temporary"}
                    and (
                        not block["retry_at"]
                        or block["retry_at"] > self.now().astimezone(UTC).isoformat()
                    )
                ):
                    raise sources.AccessBlockedError(f"{source} is blocked")
                if (
                    block
                    and block["state"] in {"cooldown", "temporary"}
                    and block["retry_at"]
                    and block["retry_at"] <= self.now().astimezone(UTC).isoformat()
                ):
                    with self.store.writer_lock():
                        with self.store.connect() as db:
                            db.execute(
                                "UPDATE source_blocks SET state='expired' WHERE source=?",
                                (source,),
                            )
                if (
                    latest
                    and (
                        self.now().astimezone(UTC) - datetime.fromisoformat(latest[0])
                    ).total_seconds()
                    < 2
                ):
                    continue
                with self.store.writer_lock():
                    self.store.log_request(source, self.now().astimezone(UTC).isoformat())
                try:
                    raw = official.request(source, dataset, request, transport=self.transport)
                except sources.SourceError as exc:
                    error_raw = exc.raw
                    if error_raw and error_raw.body:
                        path = self.store.save_raw(source, error_raw.body, error_raw.fetched_at)
                        self._save_invalid(task, error_raw, path, str(exc))
                    if isinstance(
                        exc, (sources.AccessBlockedError, sources.CaptchaError, sources.QuotaError)
                    ):
                        self._block(source, exc, error_raw)
                    retryable = isinstance(exc, sources.HttpError) and (
                        error_raw is None or error_raw.status >= 500 or not error_raw.complete
                    )
                    if retryable and attempt < 2:
                        attempt += 1
                    else:
                        self._task_state(task, None, "failed", str(exc))
                        raise
                else:
                    path = self.store.save_raw(source, raw.body, raw.fetched_at) if raw.body else ""
                    try:
                        batch = official.parse(raw, request)
                    except sources.SourceError as exc:
                        if raw.body:
                            self._save_invalid(task, raw, path, str(exc))
                        if isinstance(
                            exc,
                            (sources.AccessBlockedError, sources.CaptchaError, sources.QuotaError),
                        ):
                            self._block(source, exc, raw)
                        self._task_state(task, None, "failed", str(exc))
                        raise
                    if envelope.get("lane") == "events":
                        result_state = self._commit_event_batch(task, envelope, raw, path, batch)
                        self._event_children = getattr(self, "_event_children", [])
                    else:
                        with self.store.writer_lock():
                            snapshot = self.store.save_snapshot(
                                source=source,
                                dataset=dataset,
                                params=envelope,
                                safe_url=raw.url,
                                scope_key=f"official-v1:quotes:{source}:{dataset}:{day}",
                                fetched_at=raw.fetched_at,
                                http_status=raw.status,
                                sha256=raw.sha256,
                                raw_path=path,
                                validation_state="valid"
                                if batch.coverage_state == "ok"
                                else "empty_unverified",
                                error="empty_unverified" if batch.coverage_state != "ok" else None,
                                records=batch.records,
                            )
                        self._task_state(task, snapshot, "completed", None)
                        result_state = "ok" if batch.coverage_state == "ok" else "empty"
                    return result_state
            if attempt < 3 and not self._stop.is_set():
                self._wait(2**attempt)
        raise sources.HttpError("request retries exhausted")

    @staticmethod
    def _detail_change_key(params: dict[str, object], context: object) -> str:
        ctx = context if isinstance(context, dict) else {}
        identity = {
            "params": params,
            "symbol": ctx.get("symbol", params.get("symbol")),
            "date": ctx.get("event_date", params.get("date", params.get("file_date"))),
            "event_type": ctx.get("event_type"),
            "summary_token": ctx.get(
                "summary_token",
                (
                    ctx.get("original", {}).get("詳細資料")
                    if isinstance(ctx.get("original"), dict)
                    else None
                ),
            ),
        }
        return json.dumps(identity, sort_keys=True, separators=(",", ":"))

    def _commit_event_batch(
        self,
        task: dict[str, object],
        envelope: dict[str, object],
        raw: sources.RawBatch,
        path: str,
        batch: official.OfficialBatch,
    ) -> str:
        request = envelope["request"]
        canonical = json.dumps(request, sort_keys=True, separators=(",", ":"))
        digest = hashlib.sha256(canonical.encode()).hexdigest()
        scope = f"official-v1:events:{raw.source}:{raw.dataset}:{digest}"
        warning = "period_envelope_unverified" in batch.warnings
        validation = (
            "empty_unverified"
            if not batch.records
            else "period_envelope_unverified"
            if warning
            else "valid"
        )
        children: list[dict[str, object]] = []
        with self.store.writer_lock():
            with self.store.connect() as db:
                snapshot = self.store.save_snapshot(
                    source=raw.source,
                    dataset=raw.dataset,
                    params=envelope,
                    safe_url=raw.url,
                    scope_key=scope,
                    fetched_at=raw.fetched_at,
                    http_status=raw.status,
                    sha256=raw.sha256,
                    raw_path=path,
                    validation_state=validation,
                    error=";".join(batch.warnings)
                    or ("empty_unverified" if not batch.records else None),
                    records=batch.records,
                    connection=db,
                )
                parent_context = envelope.get("context", {})
                maintenance = (
                    parent_context.get("maintenance") if isinstance(parent_context, dict) else None
                )
                changed_details: set[str] = set()
                edition_value = task.get("edition")
                if isinstance(maintenance, dict):
                    previous = db.execute(
                        "SELECT snapshot_id FROM tasks WHERE logical_key=? AND edition<? "
                        "AND state='completed' ORDER BY edition DESC LIMIT 1",
                        (task["logical_key"], task.get("edition", 1)),
                    ).fetchone()
                    old_by_detail: dict[str, list[str]] = {}
                    if previous and previous["snapshot_id"]:
                        old_rows = db.execute(
                            "SELECT payload FROM records WHERE snapshot_id=?",
                            (previous["snapshot_id"],),
                        ).fetchall()
                        for old_row in old_rows:
                            try:
                                record = json.loads(old_row["payload"])
                                params = record.get("detail_params")
                                if isinstance(params, dict):
                                    key = self._detail_change_key(params, record)
                                    old_by_detail.setdefault(key, []).append(
                                        json.dumps(record, sort_keys=True, separators=(",", ":"))
                                    )
                            except (ValueError, TypeError, AttributeError):
                                continue
                    new_by_detail: dict[str, list[str]] = {}
                    for record in batch.records:
                        params = record.get("detail_params")
                        if isinstance(params, dict):
                            key = self._detail_change_key(params, record)
                            new_by_detail.setdefault(key, []).append(
                                json.dumps(record, sort_keys=True, separators=(",", ":"))
                            )
                    requested = {
                        self._detail_change_key(cast_params, detail.get("context", {}))
                        for detail in batch.detail_tasks
                        if isinstance((cast_params := detail.get("params")), dict)
                    }
                    for key in requested:
                        if (
                            key not in old_by_detail
                            or key not in new_by_detail
                            or sorted(old_by_detail[key]) != sorted(new_by_detail[key])
                        ):
                            changed_details.add(key)
                    if not previous or not previous["snapshot_id"]:
                        changed_details.update(requested)
                for detail in batch.detail_tasks:
                    source, dataset = str(detail["source"]), str(detail["dataset"])
                    req = detail["params"]
                    context = detail.get("context", {})
                    child_context: dict[str, object] = {}
                    if isinstance(maintenance, dict):
                        child_context = {"detail": context, "maintenance": maintenance}
                    child_params = {
                        "pipeline": "official-v1",
                        "lane": "events",
                        "request": req,
                        "context": child_context,
                    }
                    encoded = json.dumps(child_params, sort_keys=True, separators=(",", ":"))
                    logical = hashlib.sha256(
                        json.dumps(
                            ["official-v1", "events", source, dataset, req],
                            sort_keys=True,
                            separators=(",", ":"),
                        ).encode()
                    ).hexdigest()
                    child = db.execute(
                        "SELECT * FROM tasks WHERE logical_key=? ORDER BY edition DESC LIMIT 1",
                        (logical,),
                    ).fetchone()
                    same_run = db.execute(
                        "SELECT * FROM tasks WHERE logical_key=? AND run_id=? "
                        "ORDER BY edition DESC LIMIT 1",
                        (logical, task.get("run_id")),
                    ).fetchone()
                    if same_run:
                        child = same_run
                    else:
                        edition_value = task.get("edition", 1)
                        detail_key = self._detail_change_key(
                            req if isinstance(req, dict) else {}, context
                        )
                        force = (
                            isinstance(edition_value, int)
                            and edition_value > 1
                            and (not isinstance(maintenance, dict) or detail_key in changed_details)
                        )
                        reusable = False
                        if (
                            isinstance(maintenance, dict)
                            and child
                            and child["state"] == "completed"
                            and detail_key not in changed_details
                        ):
                            try:
                                old_env = json.loads(child["params"])
                                snapshot_row = db.execute(
                                    "SELECT source,dataset,params,fetched_at,validation_state "
                                    "FROM snapshots "
                                    "WHERE id=?",
                                    (child["snapshot_id"],),
                                ).fetchone()
                                event_count = db.execute(
                                    "SELECT COUNT(*) FROM records "
                                    "WHERE snapshot_id=? AND kind='event'",
                                    (child["snapshot_id"],),
                                ).fetchone()[0]
                                fetched = datetime.fromisoformat(snapshot_row["fetched_at"])
                                snap_env = json.loads(snapshot_row["params"])
                                now_value = self.now()
                                fresh_stamp = (
                                    fetched.tzinfo is not None
                                    and fetched.utcoffset() is not None
                                    and now_value.tzinfo is not None
                                    and now_value.utcoffset() is not None
                                )
                                if fresh_stamp and snapshot_row["validation_state"] == "valid":
                                    age = now_value.astimezone(UTC) - fetched.astimezone(UTC)
                                    ttl = timedelta(days=int(maintenance["detail_refresh_days"]))
                                    reusable = (
                                        child["source"] == source
                                        and child["dataset"] == dataset
                                        and child["kind"] == "official_event_detail"
                                        and old_env.get("pipeline") == "official-v1"
                                        and old_env.get("lane") == "events"
                                        and old_env.get("request") == req
                                        and old_env.get("context", {}).get("detail") == context
                                        and snapshot_row["source"] == source
                                        and snapshot_row["dataset"] == dataset
                                        and snap_env.get("pipeline") == "official-v1"
                                        and snap_env.get("lane") == "events"
                                        and snap_env.get("request") == req
                                        and event_count > 0
                                        and timedelta(0) <= age < ttl
                                    )
                            except (TypeError, ValueError, KeyError, AttributeError):
                                reusable = False
                        if (
                            child
                            and child["state"] == "completed"
                            and (reusable or (not isinstance(maintenance, dict) and not force))
                        ):
                            pass
                        elif (
                            child
                            and child["state"] in {"pending", "failed"}
                            and (isinstance(maintenance, dict) or not force)
                        ):
                            db.execute(
                                "UPDATE tasks SET run_id=?,state='pending',error=NULL WHERE id=?",
                                (task.get("run_id"), child["id"]),
                            )
                            child = db.execute(
                                "SELECT * FROM tasks WHERE id=?", (child["id"],)
                            ).fetchone()
                        else:
                            if child and child["state"] in {"pending", "failed"}:
                                db.execute(
                                    "UPDATE tasks SET state='superseded', "
                                    "error=COALESCE(error,'superseded; new task id follows') "
                                    "WHERE id=?",
                                    (child["id"],),
                                )
                            edition = child["edition"] + 1 if child else 1
                            child_id = uuid.uuid4().hex
                            request_params = req if isinstance(req, dict) else {}
                            context_params = context if isinstance(context, dict) else {}
                            child_date = (
                                context_params.get("event_date")
                                or request_params.get("date")
                                or request_params.get("file_date")
                                or request_params.get("resume_date")
                            )
                            db.execute(
                                "INSERT INTO tasks("
                                "id,logical_key,edition,source,dataset,params,kind,state,run_id, "
                                "expected_date) VALUES(?,?,?,?,?,?,?,?,?,?)",
                                (
                                    child_id,
                                    logical,
                                    edition,
                                    source,
                                    dataset,
                                    encoded,
                                    "official_event_detail",
                                    "pending",
                                    task.get("run_id"),
                                    child_date,
                                ),
                            )
                            child = db.execute(
                                "SELECT * FROM tasks WHERE id=?", (child_id,)
                            ).fetchone()
                            db.execute(
                                "UPDATE tasks SET error=COALESCE("
                                "error || '; superseded by task ' || ?, "
                                "'superseded by task ' || ?) WHERE logical_key=? AND id<>? "
                                "AND state='superseded' AND error NOT LIKE '%superseded by task %'",
                                (child_id, child_id, logical, child_id),
                            )
                    db.execute(
                        "INSERT OR IGNORE INTO official_event_links "
                        "(parent_task_id,parent_snapshot_id,child_task_id,context) VALUES(?,?,?,?)",
                        (
                            task["id"],
                            snapshot,
                            child["id"],
                            json.dumps(
                                context, ensure_ascii=False, sort_keys=True, separators=(",", ":")
                            ),
                        ),
                    )
                    if child["state"] == "pending" and child["run_id"] == task.get("run_id"):
                        children.append(dict(child))
                db.execute(
                    "UPDATE tasks SET state='completed',snapshot_id=?,error=? WHERE id=?",
                    (
                        snapshot,
                        "period_envelope_unverified"
                        if warning
                        else "empty_unverified"
                        if not batch.records
                        else None,
                        task["id"],
                    ),
                )
        self._event_children = children
        return "empty" if not batch.records else "warning" if warning else "ok"

    def _save_invalid(
        self, task: dict[str, object], raw: sources.RawBatch, path: str, error: str
    ) -> None:
        env = json.loads(str(task["params"]))
        request = env["request"]
        day = request.get("date", request.get("start_date", ""))
        encoded_request = json.dumps(request, sort_keys=True, separators=(",", ":"))
        digest = hashlib.sha256(encoded_request.encode()).hexdigest()
        scope = (
            f"official-v1:events:{raw.source}:{raw.dataset}:{digest}"
            if env.get("lane") == "events"
            else f"official-v1:quotes:{raw.source}:{raw.dataset}:{day}"
        )
        with self.store.writer_lock():
            self.store.save_snapshot(
                source=raw.source,
                dataset=raw.dataset,
                params=env,
                safe_url=raw.url,
                scope_key=scope,
                fetched_at=raw.fetched_at,
                http_status=raw.status,
                sha256=raw.sha256,
                raw_path=path,
                validation_state="invalid",
                error=error,
            )

    def _block(self, source: str, error: Exception, raw: sources.RawBatch | None) -> None:
        now = self.now().astimezone(UTC)
        permanent = isinstance(error, (sources.AccessBlockedError, sources.CaptchaError))
        retry = None
        if not permanent:
            retry = now + timedelta(hours=1)
            if raw and raw.retry_after:
                value = raw.retry_after.strip()
                try:
                    try:
                        parsed = now + timedelta(seconds=max(0, int(value)))
                    except OverflowError:
                        parsed = datetime.max.replace(tzinfo=UTC)
                except ValueError:
                    try:
                        parsed_value = email.utils.parsedate_to_datetime(value)
                        if parsed_value.tzinfo is None:
                            parsed_value = parsed_value.replace(tzinfo=UTC)
                        parsed = parsed_value.astimezone(UTC)
                    except (TypeError, ValueError, OverflowError):
                        parsed = retry
                retry = max(retry, parsed)
        with self.store.writer_lock():
            self.store.put(
                "source_blocks",
                "source",
                dict(
                    source=source,
                    reason=str(error),
                    state="blocked" if permanent else "cooldown",
                    blocked_at=now.isoformat(),
                    retry_at=retry.isoformat() if retry else None,
                ),
            )

    def _task_state(
        self, task: dict[str, object], snapshot: str | None, state: str, error: str | None
    ) -> None:
        with self.store.writer_lock():
            with self.store.connect() as db:
                db.execute(
                    "UPDATE tasks SET state=?,snapshot_id=?,error=? "
                    "WHERE id=? AND state!='completed'",
                    (state, snapshot, error, task["id"]),
                )
