from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from quant_research.taiwan import official, sources
from quant_research.taiwan.dataset.models import BuildSpec, SourceError
from quant_research.taiwan.dataset.source import SourceReader
from quant_research.taiwan.storage import SCHEMA


def spec(root: Path, symbols=None, start="2018-01-01", end="2018-12-31", sources_filter=("twse",)):
    return BuildSpec(root, start, end, symbols=symbols, sources=sources_filter)


def parsed_batch(source, dataset, payload):
    return sources.RawBatch(
        source,
        dataset,
        "offline",
        "https://example.invalid/",
        200,
        json.dumps(payload, ensure_ascii=False).encode(),
        "",
        0,
    )


def quote_records(day, rows):
    payload = {
        "stat": "OK",
        "date": day.replace("-", ""),
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
                "data": rows,
            }
        ],
    }
    return official.parse(parsed_batch("twse", "MI_INDEX", payload), {"date": day})


def empty_event_records():
    parsed = official.parse(
        parsed_batch("twse", "TWT49U", {"stat": "很抱歉，沒有符合條件的資料!"}),
        {"start_date": "2018-01-01", "end_date": "2018-12-31"},
    )
    assert parsed.records == () and parsed.coverage_state == "empty_unverified"
    return parsed


def event_records(*, envelope=True):
    payload = {
        "stat": "OK",
        "fields": ["資料日期", "股票代號", "除權息前收盤價", "除權息參考價", "詳細資料"],
        "data": [["2018-01-11", "2330", "100", "92", "2330,20180111"]],
    }
    if envelope:
        payload.update(strDate="107年01月01日", endDate="107年12月31日")
    parsed = official.parse(
        parsed_batch("twse", "TWT49U", payload),
        {"start_date": "2018-01-01", "end_date": "2018-12-31"},
    )
    return parsed


def init_db(root):
    root.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(root / "state.sqlite3")
    db.executescript(SCHEMA)
    assert db.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    return db


def store_task(
    db,
    *,
    task_id,
    logical_key,
    edition,
    source,
    dataset,
    lane,
    request,
    parsed=None,
    state="completed",
    validation=None,
):
    params = {"pipeline": "official-v1", "lane": lane, "request": request}
    encoded = json.dumps(params, sort_keys=True)
    snapshot_id = f"snapshot-{task_id}" if state == "completed" else None
    if snapshot_id:
        validation = validation or (
            "empty_unverified"
            if parsed.coverage_state == "empty_unverified"
            else "period_envelope_unverified"
            if "period_envelope_unverified" in parsed.warnings
            else "valid"
        )
        db.execute(
            "INSERT INTO snapshots VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                snapshot_id,
                source,
                dataset,
                encoded,
                "https://example.invalid/",
                f"{lane}:{logical_key}",
                "2018-01-01T00:00:00+00:00",
                200,
                hashlib.sha256(task_id.encode()).hexdigest(),
                f"raw/{task_id}.json",
                validation,
                None,
            ),
        )
        for index, record in enumerate(parsed.records):
            record = dict(record)
            db.execute(
                "INSERT INTO records(snapshot_id,kind,symbol,record_date,record_key,"
                "payload,quality_flags) "
                "VALUES(?,?,?,?,?,?,?)",
                (
                    snapshot_id,
                    record["kind"],
                    record.get("symbol"),
                    record.get("trade_date", record.get("event_date", record.get("date"))),
                    str(index),
                    json.dumps(record, ensure_ascii=False, sort_keys=True),
                    json.dumps(record.get("quality_flags", [])),
                ),
            )
    db.execute(
        "INSERT INTO tasks(id,logical_key,edition,source,dataset,params,kind,state,snapshot_id) "
        "VALUES(?,?,?,?,?,?,?,?,?)",
        (
            task_id,
            logical_key,
            edition,
            source,
            dataset,
            encoded,
            "official_quote" if lane == "quotes" else "official_event_summary",
            state,
            snapshot_id,
        ),
    )
    return snapshot_id


def test_parser_quote_records_are_valid_and_date_source_symbol_filters_apply(tmp_path):
    db = init_db(tmp_path)
    first = quote_records(
        "2018-01-02",
        [
            ["2330", "台積電", "100", "105", "99", "104", "1000", "104000"],
            ["2317", "鴻海", "80", "82", "79", "81", "2000", "162000"],
        ],
    )
    later = quote_records(
        "2018-01-03", [["2330", "台積電", "104", "106", "103", "105", "900", "94500"]]
    )
    store_task(
        db,
        task_id="quote-a",
        logical_key="quote/2018-01-02",
        edition=1,
        source="twse",
        dataset="MI_INDEX",
        lane="quotes",
        request={"date": "2018-01-02"},
        parsed=first,
    )
    store_task(
        db,
        task_id="quote-b",
        logical_key="quote/2018-01-03",
        edition=1,
        source="twse",
        dataset="MI_INDEX",
        lane="quotes",
        request={"date": "2018-01-03"},
        parsed=later,
    )
    db.commit()
    db.close()

    with SourceReader(spec(tmp_path, ("2330",), "2018-01-02", "2018-01-02")) as reader:
        (scope,) = reader.scopes()
        (result,) = reader.iter_all_records(scope)
        assert result.diagnostic is None
        assert (result.payload["open"], result.payload["close"]) == ("100", "104")
        assert (
            result.payload["trade_date"],
            result.payload["source"],
            result.payload["symbol"],
        ) == ("2018-01-02", "twse", "2330")
        assert reader.selected_record_count(scope) == 1
    with SourceReader(spec(tmp_path, None, "2018-01-03", "2018-01-03")) as reader:
        (scope,) = reader.scopes()
        results = list(reader.iter_all_records(scope))
        assert [(r.payload["trade_date"], r.payload["symbol"]) for r in results] == [
            ("2018-01-03", "2330")
        ]
    with SourceReader(spec(tmp_path, None, "2018-01-02", "2018-01-02", ("tpex",))) as reader:
        assert reader.scopes() == ()


def test_newest_failed_quote_and_event_tasks_win_without_fallback(tmp_path):
    db = init_db(tmp_path)
    quote = quote_records(
        "2018-01-02", [["2330", "台積電", "100", "105", "99", "104", "1000", "104000"]]
    )
    event = event_records()
    store_task(
        db,
        task_id="quote-old",
        logical_key="quote/stable",
        edition=1,
        source="twse",
        dataset="MI_INDEX",
        lane="quotes",
        request={"date": "2018-01-02"},
        parsed=quote,
    )
    store_task(
        db,
        task_id="quote-new",
        logical_key="quote/stable",
        edition=2,
        source="twse",
        dataset="MI_INDEX",
        lane="quotes",
        request={"date": "2018-01-02"},
        state="failed",
    )
    store_task(
        db,
        task_id="event-old",
        logical_key="event/stable",
        edition=1,
        source="twse",
        dataset="TWT49U",
        lane="events",
        request={"start_date": "2018-01-01", "end_date": "2018-12-31"},
        parsed=event,
    )
    store_task(
        db,
        task_id="event-new",
        logical_key="event/stable",
        edition=2,
        source="twse",
        dataset="TWT49U",
        lane="events",
        request={"start_date": "2018-01-01", "end_date": "2018-12-31"},
        state="failed",
    )
    db.commit()
    db.close()
    with SourceReader(spec(tmp_path)) as reader:
        selected = {scope.lane: scope for scope in reader.scopes()}
        assert selected["quotes"].task_id == "quote-new"
        assert selected["events"].task_id == "event-new"
        for scope in selected.values():
            assert not scope.usable and scope.diagnostics
            with pytest.raises(SourceError, match="unavailable"):
                list(reader.iter_all_records(scope))


def test_parser_empty_event_scope_reads_zero_without_relabel_or_mutation(tmp_path):
    db = init_db(tmp_path)
    parsed = empty_event_records()
    store_task(
        db,
        task_id="empty-event",
        logical_key="event/empty",
        edition=1,
        source="twse",
        dataset="TWT49U",
        lane="events",
        request={"start_date": "2018-01-01", "end_date": "2018-12-31"},
        parsed=parsed,
        validation="empty_unverified",
    )
    db.commit()
    db.close()
    with SourceReader(spec(tmp_path)) as reader:
        (scope,) = reader.scopes()
        assert scope.usable and scope.snapshot_validation == "empty_unverified"
        assert scope.source_record_count == 0
        assert list(reader.iter_all_records(scope)) == []
    db = sqlite3.connect(tmp_path / "state.sqlite3")
    assert db.execute("SELECT validation_state FROM snapshots").fetchone()[0] == "empty_unverified"
    db.close()


def test_nonempty_event_period_envelope_unverified_is_usable(tmp_path):
    db = init_db(tmp_path)
    parsed = event_records(envelope=False)
    assert parsed.records and "period_envelope_unverified" in parsed.warnings
    store_task(
        db,
        task_id="event-unverified",
        logical_key="event/unverified",
        edition=1,
        source="twse",
        dataset="TWT49U",
        lane="events",
        request={"start_date": "2018-01-01", "end_date": "2018-12-31"},
        parsed=parsed,
    )
    db.commit()
    db.close()
    with SourceReader(spec(tmp_path)) as reader:
        (scope,) = reader.scopes()
        (result,) = reader.iter_all_records(scope)
        assert scope.usable and scope.snapshot_validation == "period_envelope_unverified"
        assert result.diagnostic is None and result.payload["symbol"] == "2330"


def test_reader_pins_wal_snapshot_with_open_writer(tmp_path):
    writer = init_db(tmp_path)
    parsed = quote_records(
        "2018-01-02", [["2330", "台積電", "100", "105", "99", "104", "1000", "104000"]]
    )
    store_task(
        writer,
        task_id="wal-one",
        logical_key="wal/stable",
        edition=1,
        source="twse",
        dataset="MI_INDEX",
        lane="quotes",
        request={"date": "2018-01-02"},
        parsed=parsed,
    )
    writer.commit()
    assert writer.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"

    with SourceReader(spec(tmp_path)) as reader:
        (scope,) = reader.scopes()
        (initial,) = reader.iter_all_records(scope)
        assert initial.payload["close"] == "104"
        # A committed change after the reader's first read must not alter its pinned view.
        changed = json.loads(
            writer.execute(
                "SELECT payload FROM records WHERE snapshot_id=?", (scope.snapshot_id,)
            ).fetchone()[0]
        )
        changed["close"] = "999"
        writer.execute(
            "UPDATE records SET payload=? WHERE snapshot_id=?",
            (json.dumps(changed), scope.snapshot_id),
        )
        writer.commit()
        (still_old,) = reader.iter_all_records(scope)
        assert still_old.payload["close"] == "104"
    writer.close()


def _logical_state(path):
    db = sqlite3.connect(path)
    try:
        schema = db.execute(
            "SELECT type,name,tbl_name,sql FROM sqlite_master "
            "WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name"
        ).fetchall()
        tables = {}
        for kind, name, _table, _sql in schema:
            if kind == "table":
                tables[name] = db.execute(f'SELECT * FROM "{name}" ORDER BY rowid').fetchall()
        return schema, tables
    finally:
        db.close()


def test_reader_is_readonly_preserves_schema_rows_and_main_db_bytes(tmp_path):
    db = init_db(tmp_path)
    parsed = quote_records(
        "2018-01-02", [["2330", "台積電", "100", "105", "99", "104", "1000", "104000"]]
    )
    store_task(
        db,
        task_id="readonly",
        logical_key="readonly/stable",
        edition=1,
        source="twse",
        dataset="MI_INDEX",
        lane="quotes",
        request={"date": "2018-01-02"},
        parsed=parsed,
    )
    db.commit()
    db.close()
    path = tmp_path / "state.sqlite3"
    before_state, before_bytes = _logical_state(path), path.read_bytes()
    with SourceReader(spec(tmp_path)) as reader:
        (scope,) = reader.scopes()
        assert list(reader.iter_all_records(scope))[0].diagnostic is None
        with pytest.raises(sqlite3.OperationalError):
            reader._connection().execute("CREATE TABLE forbidden(x)")
    after_state, after_bytes = _logical_state(path), path.read_bytes()
    assert after_state == before_state
    assert after_bytes == before_bytes


def test_reader_does_not_create_missing_root_database(tmp_path):
    missing = tmp_path / "missing"
    with pytest.raises(SourceError, match="does not exist"):
        with SourceReader(spec(missing)):
            pass
    assert not missing.exists()
