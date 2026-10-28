"""Read-only, snapshot-consistent selectors over an existing state.sqlite3."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from .models import BuildSpec, SourceError

QUOTE_DATASETS = {"twse": "MI_INDEX", "tpex": "dailyQuotes"}
EVENT_DATASETS = {
    "twse": frozenset({"TWT49U", "TWTAUU", "TWTB8U"}),
    "tpex": frozenset({"exDailyQ", "revivt", "pvChgRslt"}),
}
DETAIL_DATASETS = frozenset({"TWT49UDetail", "TWTAVUDetail", "TWTB8UDetail", "TWTB7UDetail"})
EVENT_TYPES = {
    "TWT49U": "dividend",
    "TWTAUU": "capital_reduction",
    "TWTB8U": "par_value",
    "exDailyQ": "dividend",
    "revivt": "capital_reduction",
    "pvChgRslt": "par_value",
    "TWT49UDetail": "dividend",
    "TWTAVUDetail": "capital_reduction",
    "TWTB8UDetail": "par_value",
    "TWTB7UDetail": "par_value",
}


def _iso_date(value: object) -> bool:
    if not isinstance(value, str) or len(value) != 10:
        return False
    try:
        return date.fromisoformat(value).isoformat() == value
    except ValueError:
        return False


def _json_object(value: object) -> dict[str, Any] | None:
    if not isinstance(value, str):
        return None

    def reject_constant(value: str) -> None:
        raise ValueError(f"non-finite JSON constant: {value}")

    def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, item in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON key: {key}")
            result[key] = item
        return result

    try:
        parsed = json.loads(value, parse_constant=reject_constant, object_pairs_hook=unique_object)
    except (ValueError, TypeError):
        return None
    return parsed if isinstance(parsed, dict) else None


@dataclass(slots=True)
class Scope:
    """Latest observed task edition for one stable logical scope."""

    source: str
    lane: str
    logical_key: str
    dataset: str
    task_id: str
    edition: int
    task_state: str
    snapshot_id: str | None
    snapshot_validation: str | None
    request: dict[str, Any] | None
    source_record_count: int
    availability: str
    diagnostics: list[str] = field(default_factory=list)
    fetched_at: str | None = None
    raw_path: str | None = None
    sha256: str | None = None
    raw_task_params: str | None = None
    actual_source: str | None = None
    actual_dataset: str | None = None
    actual_kind: str | None = None
    actual_params: str | None = None

    @property
    def usable(self) -> bool:
        return self.availability == "available"

    def task_dict(self) -> dict[str, Any]:
        """Return the builder-compatible selected task metadata (never old editions)."""
        return {
            "task_id": self.task_id,
            "logical_key": self.logical_key,
            "edition": self.edition,
            "source": self.source,
            "dataset": self.dataset,
            "kind": "official_quote" if self.lane == "quotes" else "official_event_summary",
            "state": self.task_state,
            "snapshot_id": self.snapshot_id,
            "params": {"pipeline": "official-v1", "lane": self.lane, "request": self.request},
            "validation_state": self.snapshot_validation,
            "record_count": self.source_record_count,
            "fetched_at": self.fetched_at,
            "raw_path": self.raw_path,
            "sha256": self.sha256,
            "availability": self.availability,
            "diagnostics": list(self.diagnostics),
            "raw_task_params": self.raw_task_params,
            "actual_source": self.actual_source,
            "actual_dataset": self.actual_dataset,
            "actual_kind": self.actual_kind,
            "actual_params": self.actual_params,
        }


@dataclass(frozen=True, slots=True)
class RecordResult:
    """A bounded record row, or its diagnostic; malformed data remains observable."""

    record_id: int
    snapshot_id: str
    payload: dict[str, Any] | None
    quality_flags: tuple[str, ...]
    diagnostic: str | None = None


class SourceReader:
    """Read-only source view; enter as a context manager to pin one SQLite snapshot."""

    def __init__(self, spec: BuildSpec) -> None:
        self.spec = spec
        self._db: sqlite3.Connection | None = None
        self._scopes: list[Scope] | None = None

    def __enter__(self) -> SourceReader:
        path = self.spec.root.expanduser() / "state.sqlite3"
        if not path.is_file():
            raise SourceError(f"source database does not exist: {path}")
        db: sqlite3.Connection | None = None
        try:
            db = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=5)
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA query_only=ON")
            db.execute("BEGIN")
            self._check_schema(db)
            # Force the first actual read now so all later work uses this WAL view.
            db.execute("SELECT name FROM sqlite_master LIMIT 1").fetchone()
        except BaseException as exc:
            if db is not None:
                try:
                    db.close()
                except sqlite3.Error:
                    pass
            if isinstance(exc, SourceError):
                raise
            if isinstance(exc, (sqlite3.Error, OSError)):
                raise SourceError(f"cannot open source database read-only: {exc}") from exc
            raise
        assert db is not None
        self._db = db
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        if self._db is not None:
            self._db.close()
            self._db = None
        self._scopes = None

    @staticmethod
    def _check_schema(db: sqlite3.Connection) -> None:
        required = {
            "tasks": {
                "id",
                "logical_key",
                "edition",
                "source",
                "dataset",
                "params",
                "kind",
                "state",
                "snapshot_id",
            },
            "snapshots": {
                "id",
                "source",
                "dataset",
                "params",
                "scope_key",
                "fetched_at",
                "sha256",
                "raw_path",
                "validation_state",
            },
            "records": {
                "id",
                "snapshot_id",
                "kind",
                "symbol",
                "record_date",
                "payload",
                "quality_flags",
            },
            "official_event_links": {
                "id",
                "parent_task_id",
                "parent_snapshot_id",
                "child_task_id",
                "context",
            },
        }
        tables = {
            str(row[0]) for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        for table, columns in required.items():
            if table not in tables:
                raise SourceError(f"required source table missing: {table}")
            present = {str(row[1]) for row in db.execute(f'PRAGMA table_info("{table}")')}
            if not columns <= present:
                raise SourceError(
                    f"required columns missing from {table}: {sorted(columns - present)}"
                )
        indexes = {str(row[1]) for row in db.execute('PRAGMA index_list("records")')}
        if "records_snapshot" not in indexes:
            raise SourceError("required source index missing: records_snapshot")

    def _connection(self) -> sqlite3.Connection:
        if self._db is None:
            raise RuntimeError("SourceReader must be used inside a with block")
        return self._db

    def scopes(self) -> tuple[Scope, ...]:
        """Select maximal editions before checking state, params, snapshot, or request dates."""
        if self._scopes is not None:
            return tuple(self._scopes)
        db = self._connection()
        task_rows = db.execute(
            "SELECT t.id,t.logical_key,t.edition,t.source,t.dataset,t.params,t.kind,t.state,"
            "t.snapshot_id,t.rowid task_rowid,s.id joined_snapshot_id,s.source snapshot_source,"
            "s.dataset snapshot_dataset,s.params snapshot_params,s.fetched_at,s.sha256,s.raw_path,"
            "s.validation_state "
            "FROM tasks t LEFT JOIN snapshots s ON s.id=t.snapshot_id "
            "ORDER BY t.rowid"
        ).fetchall()

        def identity(row: sqlite3.Row) -> tuple[str, str, str] | None:
            source, dataset, kind = row["source"], row["dataset"], row["kind"]
            if (
                source in QUOTE_DATASETS
                and kind == "official_quote"
                and dataset == QUOTE_DATASETS[source]
            ):
                return str(source), "quotes", str(dataset)
            if (
                source in EVENT_DATASETS
                and kind == "official_event_summary"
                and dataset in EVENT_DATASETS[source]
            ):
                return str(source), "events", str(dataset)
            return None

        histories: dict[str, set[tuple[str, str, str]]] = {}
        for row in task_rows:
            known = identity(row)
            if known is not None:
                histories.setdefault(str(row["logical_key"]), set()).add(known)
        grouped: dict[str, list[sqlite3.Row]] = {}
        for row in task_rows:
            logical = str(row["logical_key"])
            if logical in histories:
                grouped.setdefault(logical, []).append(row)
        selected: list[Scope] = []
        for logical, versions in sorted(grouped.items()):
            identities = histories[logical]
            expected_source, lane, dataset = sorted(identities)[0]
            if expected_source not in self.spec.sources:
                continue
            row = max(
                versions,
                key=lambda item: (
                    int(item["edition"]),
                    str(item["fetched_at"] or ""),
                    str(item["id"]),
                ),
            )
            params = _json_object(row["params"])
            request = params.get("request") if params else None
            request = request if isinstance(request, dict) else None
            diagnostics: list[str] = []
            if row["source"] != expected_source:
                diagnostics.append("task_source_mismatch")
            if row["dataset"] != dataset:
                diagnostics.append("task_dataset_mismatch")
            expected_kind = "official_quote" if lane == "quotes" else "official_event_summary"
            if row["kind"] != expected_kind:
                diagnostics.append("task_kind_mismatch")
            if len(identities) != 1:
                diagnostics.append("ambiguous_logical_identity")
            if (
                params is None
                or params.get("pipeline") != "official-v1"
                or params.get("lane") != lane
                or request is None
            ):
                diagnostics.append("invalid_task_params")
            expected_fields = {"date"} if lane == "quotes" else {"start_date", "end_date"}
            if request is not None:
                if not expected_fields <= set(request) or any(
                    not isinstance(request.get(k), str) for k in expected_fields
                ):
                    diagnostics.append("invalid_request")
                elif lane == "quotes":
                    d = request["date"]
                    if not _iso_date(d):
                        diagnostics.append("invalid_request_date")
                    elif not diagnostics and not self.spec.start_date <= d <= self.spec.end_date:
                        continue
                else:
                    start, end = request["start_date"], request["end_date"]
                    if not _iso_date(start) or not _iso_date(end):
                        diagnostics.append("invalid_request_date")
                    elif start > end:
                        diagnostics.append("invalid_request_period")
                    elif not diagnostics and (
                        end < self.spec.start_date or start > self.spec.end_date
                    ):
                        continue
            snapshot_id = row["snapshot_id"]
            snapshot_exists = row["joined_snapshot_id"] is not None
            if snapshot_id is None:
                diagnostics.append("latest_task_has_no_snapshot")
            elif not snapshot_exists:
                diagnostics.append("task_snapshot_missing")
            if snapshot_exists and (
                row["snapshot_source"] != row["source"]
                or row["snapshot_dataset"] != row["dataset"]
                or _json_object(row["snapshot_params"]) != params
            ):
                diagnostics.append("snapshot_provenance_mismatch")
            validation = str(row["validation_state"]) if snapshot_exists else None
            if row["state"] != "completed":
                diagnostics.append(f"latest_task_state:{row['state']}")
            count = 0
            if snapshot_exists and snapshot_id is not None:
                count = int(
                    db.execute(
                        "SELECT COUNT(*) FROM records INDEXED BY records_snapshot "
                        "WHERE snapshot_id=?",
                        (snapshot_id,),
                    ).fetchone()[0]
                )
            if validation not in {"valid", "period_envelope_unverified"} and not (
                validation == "empty_unverified" and snapshot_exists and count == 0
            ):
                diagnostics.append(f"snapshot_validation:{validation or 'missing'}")
            ready = not diagnostics and snapshot_exists and count >= 0
            selected.append(
                Scope(
                    source=expected_source,
                    lane=lane,
                    logical_key=logical,
                    dataset=dataset,
                    task_id=str(row["id"]),
                    edition=int(row["edition"]),
                    task_state=str(row["state"]),
                    snapshot_id=str(snapshot_id) if snapshot_id is not None else None,
                    snapshot_validation=validation,
                    request=request,
                    source_record_count=count,
                    availability="available" if ready else "unavailable",
                    diagnostics=diagnostics,
                    fetched_at=str(row["fetched_at"]) if row["fetched_at"] is not None else None,
                    raw_path=str(row["raw_path"]) if row["raw_path"] is not None else None,
                    sha256=str(row["sha256"]) if row["sha256"] is not None else None,
                    raw_task_params=(str(row["params"]) if row["params"] is not None else None),
                    actual_source=str(row["source"]),
                    actual_dataset=str(row["dataset"]),
                    actual_kind=str(row["kind"]),
                    actual_params=str(row["params"]) if row["params"] is not None else None,
                )
            )
        self._scopes = selected
        return tuple(selected)

    def selected_record_count(self, scope: Scope) -> int:
        """Count selected-symbol rows separately from the complete snapshot count."""
        if not scope.usable or scope.snapshot_id is None:
            raise SourceError(
                f"selected scope {scope.source}/{scope.dataset} task={scope.task_id} "
                f"is unavailable: {scope.diagnostics}"
            )
        if self.spec.symbols is None:
            return scope.source_record_count
        if not self.spec.symbols:
            return 0
        placeholders = ",".join("?" for _ in self.spec.symbols)
        row = (
            self._connection()
            .execute(
                f"SELECT COUNT(*) FROM records INDEXED BY records_snapshot "
                f"WHERE snapshot_id=? AND symbol IN ({placeholders})",
                (scope.snapshot_id, *self.spec.symbols),
            )
            .fetchone()
        )
        return int(row[0])

    def iter_records(
        self, scope: Scope, *, limit: int = 500, offset: int = 0, symbol: str | None = None
    ) -> Iterator[RecordResult]:
        """Yield finite indexed pages by snapshot; JSON decimals are parsed as strings."""
        if limit < 1 or offset < 0:
            raise ValueError("limit must be positive and offset nonnegative")
        if not scope.usable or scope.snapshot_id is None:
            raise SourceError(
                f"selected scope {scope.source}/{scope.dataset} task={scope.task_id} "
                f"is unavailable: {scope.diagnostics}"
            )
        db = self._connection()
        where = "snapshot_id=?"
        args: tuple[Any, ...] = (scope.snapshot_id,)
        if self.spec.symbols is not None:
            if not self.spec.symbols:
                return
            placeholders = ",".join("?" for _ in self.spec.symbols)
            where += f" AND symbol IN ({placeholders})"
            args += self.spec.symbols
        if symbol is not None:
            where += " AND symbol=?"
            args += (symbol,)
        cursor = db.execute(
            "SELECT id,snapshot_id,kind,symbol,record_date,payload,quality_flags FROM records "
            f"INDEXED BY records_snapshot WHERE {where} ORDER BY id LIMIT ? OFFSET ?",
            (*args, limit, offset),
        )
        for row in cursor:
            yield self._record_result(
                row,
                scope.source,
                scope.dataset,
                scope.request,
                "quote" if scope.lane == "quotes" else "event",
            )

    def iter_all_records(
        self, scope: Scope, *, page_size: int = 500, symbol: str | None = None
    ) -> Iterator[RecordResult]:
        """Iterate all selected rows in bounded pages, without iter_records' 500-row cap."""
        if page_size < 1:
            raise ValueError("page_size must be positive")
        offset = 0
        while True:
            page = list(self.iter_records(scope, limit=page_size, offset=offset, symbol=symbol))
            yield from page
            if len(page) < page_size:
                break
            offset += page_size

    def _record_result(
        self,
        row: sqlite3.Row,
        source: str,
        dataset: str,
        request: dict[str, Any] | None,
        expected_kind: str,
        *,
        child_event_date: str | None = None,
    ) -> RecordResult:
        diagnostics: list[str] = []
        flags: list[str] = []
        try:
            parsed_flags = json.loads(row["quality_flags"])
            if not isinstance(parsed_flags, list):
                diagnostics.append("invalid_quality_flags")
            else:
                flags.extend(str(x) for x in parsed_flags if isinstance(x, str))
                if any(not isinstance(x, str) for x in parsed_flags):
                    diagnostics.append("invalid_quality_flags")
        except (ValueError, TypeError):
            diagnostics.append("invalid_quality_flags")
        payload = _json_object(row["payload"])
        if payload is None:
            diagnostics.append("invalid_json_or_payload_not_object")
        else:
            if row["kind"] != expected_kind:
                diagnostics.append("record_kind_mismatch")
            if payload.get("kind") != row["kind"]:
                diagnostics.append("payload_kind_mismatch")
            if payload.get("source") != source:
                diagnostics.append("payload_source_mismatch")
            if payload.get("dataset") != dataset:
                diagnostics.append("payload_dataset_mismatch")
            payload_flags = payload.get("quality_flags", [])
            if not isinstance(payload_flags, list) or any(
                not isinstance(x, str) for x in payload_flags
            ):
                diagnostics.append("invalid_quality_flags")
            else:
                flags.extend(payload_flags)
            psymbol = payload.get("symbol")
            if not isinstance(psymbol, str) or not psymbol.strip() or psymbol != row["symbol"]:
                diagnostics.append("payload_symbol_mismatch")
            pdate = payload.get("trade_date", payload.get("event_date", payload.get("date")))
            rdate = row["record_date"]
            if expected_kind == "quote":
                requested = request.get("date") if request else None
                if pdate != requested or rdate != requested or not _iso_date(pdate):
                    diagnostics.append("quote_date_mismatch")
            else:
                if child_event_date is None:
                    if not _iso_date(rdate) or not _iso_date(pdate):
                        diagnostics.append("missing_event_date")
                    for value in (rdate, pdate):
                        if _iso_date(value) and (
                            not request
                            or not request.get("start_date", "")
                            <= value
                            <= request.get("end_date", "")
                        ):
                            diagnostics.append("event_date_outside_request")
                else:
                    for value in (rdate, pdate):
                        if value is not None and value != child_event_date:
                            diagnostics.append("child_event_date_mismatch")
                if rdate is not None and pdate is not None and rdate != pdate:
                    diagnostics.append("event_date_mismatch")
            if expected_kind == "event":
                dataset_type = EVENT_TYPES.get(dataset)
                expected_observation = (
                    "summary"
                    if child_event_date is None
                    else ("prices" if dataset == "TWTB8UDetail" else "conditions")
                )
                if payload.get("observation_type") != expected_observation:
                    diagnostics.append("payload_observation_type_mismatch")
                if payload.get("event_type") != dataset_type:
                    diagnostics.append("payload_event_type_mismatch")
        return RecordResult(
            int(row["id"]),
            str(row["snapshot_id"]),
            payload,
            tuple(
                dict.fromkeys((*flags, *[x for x in diagnostics if x == "invalid_quality_flags"]))
            ),
            ";".join(dict.fromkeys(diagnostics)) or None,
        )

    def links(self, parent: Scope) -> tuple[dict[str, Any], ...]:
        """Return only links attached to the exact selected parent task and snapshot."""
        if parent.lane != "events" or parent.snapshot_id is None:
            return ()
        rows = self._connection().execute(
            "SELECT l.id,l.parent_task_id,l.parent_snapshot_id,l.child_task_id,l.context "
            "FROM official_event_links l WHERE l.parent_task_id=? AND l.parent_snapshot_id=? "
            "ORDER BY l.id",
            (parent.task_id, parent.snapshot_id),
        )
        links = []
        for row in rows:
            links.append(
                {
                    "link_id": int(row["id"]),
                    "parent_task_id": str(row["parent_task_id"]),
                    "parent_snapshot_id": str(row["parent_snapshot_id"]),
                    "child_task_id": str(row["child_task_id"]),
                    "context": _json_object(row["context"]),
                }
            )
        return tuple(links)

    def iter_child_records(
        self, link: dict[str, Any], *, limit: int = 500, offset: int = 0
    ) -> Iterator[RecordResult]:
        """Iterate records only from a validated exact linked child snapshot."""
        child = self.child_scope(link)
        if child is None or not child["available"] or child["snapshot_id"] is None:
            return
        snapshot_id = str(child["snapshot_id"])
        if limit < 1 or offset < 0:
            raise ValueError("limit must be positive and offset nonnegative")
        cursor = self._connection().execute(
            "SELECT id,snapshot_id,kind,symbol,record_date,payload,quality_flags FROM records "
            "INDEXED BY records_snapshot WHERE snapshot_id=? ORDER BY id LIMIT ? OFFSET ?",
            (snapshot_id, limit, offset),
        )
        context = link.get("context")
        event_date = context.get("event_date") if isinstance(context, dict) else None
        for row in cursor:
            result = self._record_result(
                row, child["source"], child["dataset"], None, "event", child_event_date=event_date
            )
            yield result

    def child_scope(self, link: dict[str, Any]) -> dict[str, Any] | None:
        """Fetch and validate the exact linked TWSE detail task and snapshot."""
        db = self._connection()
        row = db.execute(
            "SELECT t.id task_id,t.logical_key,t.edition,t.source,t.dataset,t.params,t.kind,"
            "t.state,t.snapshot_id,s.source snapshot_source,s.dataset snapshot_dataset,"
            "s.params snapshot_params,s.validation_state,s.fetched_at,s.raw_path,s.sha256 "
            "FROM tasks t LEFT JOIN snapshots s ON s.id=t.snapshot_id "
            "WHERE t.id=?",
            (link.get("child_task_id"),),
        ).fetchone()
        if row is None:
            return None
        # A caller-supplied dict is not authority: require the exact persisted link row.
        stored_link = db.execute(
            "SELECT parent_task_id,parent_snapshot_id,child_task_id,context "
            "FROM official_event_links WHERE id=?",
            (link.get("link_id"),),
        ).fetchone()
        if (
            stored_link is None
            or stored_link["parent_task_id"] != link.get("parent_task_id")
            or stored_link["parent_snapshot_id"] != link.get("parent_snapshot_id")
            or stored_link["child_task_id"] != link.get("child_task_id")
            or _json_object(stored_link["context"]) != link.get("context")
        ):
            return None
        params = _json_object(row["params"])
        snapshot_params = _json_object(row["snapshot_params"])
        request = params.get("request") if params else None
        context = link.get("context")
        parent = db.execute(
            "SELECT id,source,dataset,params,snapshot_id FROM tasks WHERE id=?",
            (link.get("parent_task_id"),),
        ).fetchone()
        parent_params = _json_object(parent["params"]) if parent else None
        summary_types = {
            "TWT49U": "dividend",
            "TWTAUU": "capital_reduction",
            "TWTB8U": "par_value",
        }
        child_datasets = {
            "TWT49U": "TWT49UDetail",
            "TWTAUU": "TWTAVUDetail",
            "TWTB8U": "TWTB8UDetail",
        }
        summary_dataset = context.get("summary_dataset") if isinstance(context, dict) else None
        event_date = context.get("event_date") if isinstance(context, dict) else None
        context_valid = (
            isinstance(context, dict)
            and isinstance(context.get("symbol"), str)
            and bool(context["symbol"].strip())
            and _iso_date(event_date)
            and parent is not None
            and parent["source"] == "twse"
            and parent["dataset"] == summary_dataset
            and parent["snapshot_id"] == link.get("parent_snapshot_id")
            and isinstance(parent_params, dict)
            and parent_params.get("pipeline") == "official-v1"
            and parent_params.get("lane") == "events"
            and context.get("event_type") == summary_types.get(summary_dataset)
            and summary_dataset in summary_types
            and isinstance(event_date, str)
            and isinstance(parent_params.get("request"), dict)
            and parent_params["request"].get("start_date", "")
            <= event_date
            <= parent_params["request"].get("end_date", "")
        )
        expected_dataset = child_datasets.get(
            summary_dataset if isinstance(summary_dataset, str) else ""
        )
        parent_payload = None
        if context_valid and isinstance(context, dict):
            # Indexed, symbol-scoped and bounded in Python; event rows are naturally small.
            records = db.execute(
                "SELECT payload FROM records INDEXED BY records_snapshot "
                "WHERE snapshot_id=? AND symbol=? AND kind='event' ORDER BY id LIMIT 1000",
                (link.get("parent_snapshot_id"), context["symbol"]),
            )
            for record in records:
                payload = _json_object(record["payload"])
                if not payload or payload.get("event_date", payload.get("date")) != event_date:
                    continue
                token = payload.get("summary_token")
                if token is None:
                    original = payload.get("original")
                    token = original.get("詳細資料") if isinstance(original, dict) else None
                if (
                    payload.get("symbol") == context["symbol"]
                    and payload.get("kind") == "event"
                    and payload.get("event_type") == context.get("event_type")
                    and token == context.get("summary_token")
                ):
                    parent_payload = payload
                    break
        expected_request = parent_payload.get("detail_params") if parent_payload else None
        dataset = row["dataset"]
        if dataset == "TWTB7UDetail":
            b7_request_valid = (
                summary_dataset == "TWTB8U"
                and isinstance(request, dict)
                and set(request) == {"symbol", "file_date"}
                and request.get("symbol")
                == (context.get("symbol") if isinstance(context, dict) else None)
                and _iso_date(request.get("file_date"))
                and isinstance(parent_payload, dict)
                and isinstance(parent_payload.get("stop_date"), str)
                and _iso_date(parent_payload.get("stop_date"))
            )
            request_valid = b7_request_valid
        else:
            required_keys = {
                "TWT49UDetail": {"symbol", "date"},
                "TWTAVUDetail": {"symbol", "file_date"},
                "TWTB8UDetail": {"symbol", "stop_date", "resume_date"},
            }.get(dataset)
            request_valid = (
                required_keys is not None
                and isinstance(request, dict)
                and set(request) == required_keys
                and request == expected_request
                and request.get("symbol")
                == (context.get("symbol") if isinstance(context, dict) else None)
                and all(_iso_date(request.get(key)) for key in required_keys - {"symbol"})
            )
        b7_payloads_valid = True
        if dataset == "TWTB7UDetail" and parent_payload is not None and row["snapshot_id"]:
            b7_rows = db.execute(
                "SELECT payload FROM records INDEXED BY records_snapshot "
                "WHERE snapshot_id=? ORDER BY id LIMIT 1000",
                (row["snapshot_id"],),
            )
            b7_payloads_valid = any(
                (payload := _json_object(record["payload"])) is not None
                and payload.get("stop_date") == parent_payload.get("stop_date")
                and _iso_date(payload.get("stop_date"))
                for record in b7_rows
            )
        valid = (
            context_valid
            and parent_payload is not None
            and dataset in DETAIL_DATASETS
            and (dataset == expected_dataset or dataset == "TWTB7UDetail")
            and request_valid
            and b7_payloads_valid
            and row["source"] == "twse"
            and row["kind"] == "official_event_detail"
            and params is not None
            and params.get("pipeline") == "official-v1"
            and params.get("lane") == "events"
            and row["state"] == "completed"
            and row["snapshot_id"] is not None
            and row["snapshot_source"] == row["source"]
            and row["snapshot_dataset"] == dataset
            and snapshot_params == params
            and row["validation_state"] == "valid"
        )
        count = (
            int(
                db.execute(
                    "SELECT COUNT(*) FROM records INDEXED BY records_snapshot WHERE snapshot_id=?",
                    (row["snapshot_id"],),
                ).fetchone()[0]
            )
            if row["snapshot_id"] is not None
            else 0
        )
        return {
            "task_id": str(row["task_id"]),
            "logical_key": str(row["logical_key"]),
            "edition": int(row["edition"]),
            "source": str(row["source"]),
            "dataset": str(dataset),
            "params": params,
            "kind": str(row["kind"]),
            "state": str(row["state"]),
            "snapshot_id": str(row["snapshot_id"]) if row["snapshot_id"] is not None else None,
            "validation_state": row["validation_state"],
            "fetched_at": row["fetched_at"],
            "raw_path": row["raw_path"],
            "sha256": row["sha256"],
            "record_count": count,
            "source_record_count": count,
            "available": valid,
            "diagnostic": None if valid else "linked_child_provenance_or_state_invalid",
            "raw_task_params": str(row["params"]) if row["params"] is not None else None,
        }
