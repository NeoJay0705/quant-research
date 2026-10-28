from __future__ import annotations

import hashlib
import json
import sqlite3
from decimal import Decimal
from pathlib import Path

from quant_research.taiwan import official, sources
from quant_research.taiwan.dataset.build import iter_series
from quant_research.taiwan.dataset.models import BuildSpec
from quant_research.taiwan.storage import SCHEMA


def test_internal_pipeline_reads_parsed_storage_and_preserves_nulls_classification_and_source(
    tmp_path: Path,
) -> None:
    root = tmp_path / "source"
    root.mkdir()
    db = sqlite3.connect(root / "state.sqlite3")
    db.executescript(SCHEMA)
    for index, day in enumerate(
        ("2024-01-01", "2024-01-02", "2024-01-03", "2024-01-04", "2024-01-05")
    ):
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
                    "data": [
                        [
                            "2330",
                            "台積電",
                            str(100 + index),
                            "－",
                            "90",
                            str(100 + index),
                            "10",
                            "－",
                        ],
                        ["00999", "未知商品", "5", "6", "4", "5", "1", "2"],
                    ],
                }
            ],
        }
        parsed = official.parse(
            sources.RawBatch(
                "twse",
                "MI_INDEX",
                "offline",
                "https://example.invalid",
                200,
                json.dumps(payload, ensure_ascii=False).encode(),
                "",
                0,
            ),
            {"date": day},
        )
        params = json.dumps(
            {"pipeline": "official-v1", "lane": "quotes", "request": {"date": day}}, sort_keys=True
        )
        snap = f"s{index}"
        db.execute(
            "INSERT INTO snapshots VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                snap,
                "twse",
                "MI_INDEX",
                params,
                "url",
                "scope",
                "2024-01-06",
                200,
                "hash",
                "raw",
                "valid",
                None,
            ),
        )
        db.execute(
            "INSERT INTO tasks(id,logical_key,edition,source,dataset,params,kind,state,"
            "snapshot_id) VALUES(?,?,?,?,?,?,?,?,?)",
            (
                f"t{index}",
                f"q{index}",
                1,
                "twse",
                "MI_INDEX",
                params,
                "official_quote",
                "completed",
                snap,
            ),
        )
        for n, record in enumerate(parsed.records):
            db.execute(
                "INSERT INTO records(snapshot_id,kind,symbol,record_date,record_key,payload,"
                "quality_flags) VALUES(?,?,?,?,?,?,?)",
                (
                    snap,
                    "quote",
                    record["symbol"],
                    day,
                    str(n),
                    json.dumps(record, ensure_ascii=False),
                    "[]",
                ),
            )
    event_params = {
        "pipeline": "official-v1",
        "lane": "events",
        "request": {"start_date": "2024-01-01", "end_date": "2024-01-05"},
    }
    event_payload = {
        "stat": "OK",
        "fields": ["恢復買賣日期", "股票代號", "停止買賣前收盤價格", "恢復買賣參考價", "詳細資料"],
        "data": [["2024-01-03", "2330", "100", "50", "2330,20240102,20240103"]],
        "strDate": "113年01月01日",
        "endDate": "113年01月05日",
    }
    parsed_event = official.parse(
        sources.RawBatch(
            "twse",
            "TWTB8U",
            "offline",
            "https://example.invalid",
            200,
            json.dumps(event_payload, ensure_ascii=False).encode(),
            "",
            0,
        ),
        event_params["request"],
    )
    encoded_event_params = json.dumps(event_params, sort_keys=True)
    db.execute(
        "INSERT INTO snapshots VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "event-snap",
            "twse",
            "TWTB8U",
            encoded_event_params,
            "url",
            "event",
            "2024-01-06",
            200,
            "hash",
            "raw",
            "valid",
            None,
        ),
    )
    db.execute(
        "INSERT INTO tasks(id,logical_key,edition,source,dataset,params,kind,state,snapshot_id) "
        "VALUES(?,?,?,?,?,?,?,?,?)",
        (
            "event-task",
            "event-scope",
            1,
            "twse",
            "TWTB8U",
            encoded_event_params,
            "official_event_summary",
            "completed",
            "event-snap",
        ),
    )
    for n, record in enumerate(parsed_event.records):
        db.execute(
            "INSERT INTO records(snapshot_id,kind,symbol,record_date,record_key,payload,"
            "quality_flags) "
            "VALUES(?,?,?,?,?,?,?)",
            (
                "event-snap",
                "event",
                record["symbol"],
                record["event_date"],
                str(n),
                json.dumps(record, ensure_ascii=False),
                "[]",
            ),
        )
    db.commit()
    db.close()
    before = hashlib.sha256((root / "state.sqlite3").read_bytes()).hexdigest()
    spec = BuildSpec(root, "2024-01-01", "2024-01-05", sources=("twse",))
    series = list(iter_series(spec))
    assert [x.symbol for x in series] == ["00999", "2330"]
    ordinary = series[1]
    assert len(ordinary.quotes) == 5
    assert [q.factor for q in ordinary.quotes] == list(map(Decimal, ("1", "1", "2", "2", "2")))
    assert [q.close for q in ordinary.quotes] == list(
        map(Decimal, ("100", "101", "204", "206", "208"))
    )
    assert all(q.high is None for q in ordinary.quotes)
    assert ordinary.quotes[0].quote.amount is None and ordinary.quotes[0].quote.volume == 10
    assert ordinary.classifications[0].classification == "ordinary_rule_limited"
    assert series[0].classifications[0].classification in {"unknown", "excluded_non_ordinary"}
    assert len(series[0].raw_quotes) == 5 and series[0].quotes == ()
    assert len(ordinary.events) == 1
    event = ordinary.events[0]
    assert (event.source, event.symbol, event.event_type, event.effective_date) == (
        "twse",
        "2330",
        "par_value",
        "2024-01-03",
    )
    assert event.ref.task_id == "event-task" and event.ref.snapshot_id == "event-snap"
    assert event.ref.record_id > 0
    assert hashlib.sha256((root / "state.sqlite3").read_bytes()).hexdigest() == before
