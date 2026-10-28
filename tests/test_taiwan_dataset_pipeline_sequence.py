from __future__ import annotations

import json
import sqlite3
from decimal import Decimal
from pathlib import Path

from quant_research.taiwan import official, sources
from quant_research.taiwan.dataset.build import iter_series
from quant_research.taiwan.dataset.models import BuildSpec
from quant_research.taiwan.storage import SCHEMA


def _batch(source: str, dataset: str, payload: dict) -> sources.RawBatch:
    return sources.RawBatch(
        source,
        dataset,
        "offline",
        "https://example.invalid",
        200,
        json.dumps(payload, ensure_ascii=False).encode(),
        "",
        0,
    )


def _store_parsed(
    db: sqlite3.Connection,
    *,
    source: str,
    dataset: str,
    request: dict[str, str],
    payload: dict,
    index: int,
) -> None:
    parsed = official.parse(_batch(source, dataset, payload), request)
    params = {
        "pipeline": "official-v1",
        "lane": "quotes" if dataset == "MI_INDEX" else "events",
        "request": request,
    }
    encoded = json.dumps(params, sort_keys=True)
    snapshot, task = f"snapshot-{index}", f"task-{index}"
    db.execute(
        "INSERT INTO snapshots VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            snapshot,
            source,
            dataset,
            encoded,
            "url",
            task,
            "2024-01-20",
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
            task,
            task,
            1,
            source,
            dataset,
            encoded,
            "official_quote" if dataset == "MI_INDEX" else "official_event_summary",
            "completed",
            snapshot,
        ),
    )
    for row_index, record in enumerate(parsed.records):
        db.execute(
            "INSERT INTO records(snapshot_id,kind,symbol,record_date,record_key,payload,"
            "quality_flags) VALUES(?,?,?,?,?,?,?)",
            (
                snapshot,
                "quote" if dataset == "MI_INDEX" else "event",
                record["symbol"],
                request.get("date", record.get("event_date")),
                str(row_index),
                json.dumps(record, ensure_ascii=False),
                "[]",
            ),
        )


def _install(
    root: Path,
    days: tuple[str, ...],
    closes: tuple[int, ...],
    events: tuple[tuple[str, int, int], ...],
) -> None:
    root.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(root / "state.sqlite3")
    db.executescript(SCHEMA)
    for index, (day, close) in enumerate(zip(days, closes, strict=True)):
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
                            str(close),
                            str(close),
                            str(close),
                            str(close),
                            "10",
                            "1000",
                        ]
                    ],
                }
            ],
        }
        _store_parsed(
            db,
            source="twse",
            dataset="MI_INDEX",
            request={"date": day},
            payload=payload,
            index=index,
        )
    if events:
        # TWTB8U's detail token contains stop date then resume (effective) date.
        rows = [
            [
                day,
                "2330",
                str(before),
                str(after),
                f"2330,{day.replace('-', '')},{day.replace('-', '')}",
            ]
            for day, before, after in events
        ]
        request_start = min(day for day, _, _ in events)
        request_end = max(day for day, _, _ in events)
        payload = {
            "stat": "OK",
            "fields": [
                "恢復買賣日期",
                "股票代號",
                "停止買賣前收盤價格",
                "恢復買賣參考價",
                "詳細資料",
            ],
            "data": rows,
            "strDate": request_start,
            "endDate": request_end,
        }
        _store_parsed(
            db,
            source="twse",
            dataset="TWTB8U",
            request={"start_date": request_start, "end_date": request_end},
            payload=payload,
            index=len(days),
        )
    db.commit()
    db.close()


def test_pipeline_adjusts_sequentially_and_ignores_boundary_outside_events(tmp_path: Path) -> None:
    days = tuple(f"2024-01-0{day}" for day in range(1, 6))
    root = tmp_path / "sequential"
    _install(
        root,
        days,
        (100, 102, 51, 53, 106),
        ((days[0], 200, 100), (days[2], 100, 50), (days[4], 100, 200), ("2024-01-08", 100, 100)),
    )
    (series,) = list(iter_series(BuildSpec(root, days[0], days[-1], sources=("twse",))))
    assert [quote.quote.date for quote in series.quotes] == list(days)
    assert [event.effective_date for event in series.events] == [days[0], days[2], days[4]]
    assert [quote.factor for quote in series.quotes] == list(
        map(Decimal, ("1", "1", "2", "2", "1"))
    )
    assert [quote.close for quote in series.quotes] == list(
        map(Decimal, ("100", "102", "102", "106", "106"))
    )


def test_event_without_quote_applies_to_next_quote_and_append_preserves_prefix(
    tmp_path: Path,
) -> None:
    root = tmp_path / "append"
    initial_days = ("2024-02-01", "2024-02-03")
    _install(root, initial_days, (100, 51), (("2024-02-02", 100, 50),))
    spec = BuildSpec(root, initial_days[0], initial_days[-1], sources=("twse",))
    (before,) = list(iter_series(spec))
    assert [quote.factor for quote in before.quotes] == [Decimal("1"), Decimal("2")]
    assert [quote.close for quote in before.quotes] == [Decimal("100"), Decimal("102")]

    db = sqlite3.connect(root / "state.sqlite3")
    day = "2024-02-04"
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
                "data": [["2330", "台積電", "53", "53", "53", "53", "10", "1000"]],
            }
        ],
    }
    _store_parsed(
        db, source="twse", dataset="MI_INDEX", request={"date": day}, payload=payload, index=4
    )
    _store_parsed(
        db,
        source="twse",
        dataset="TWTB8U",
        request={"start_date": "2024-02-01", "end_date": "2024-02-05"},
        payload={
            "stat": "OK",
            "fields": [
                "恢復買賣日期",
                "股票代號",
                "停止買賣前收盤價格",
                "恢復買賣參考價",
                "詳細資料",
            ],
            "data": [["2024-02-05", "2330", "100", "50", "2330,20240204,20240205"]],
            "strDate": "2024-02-01",
            "endDate": "2024-02-05",
        },
        index=5,
    )
    db.commit()
    db.close()
    (after,) = list(iter_series(BuildSpec(root, initial_days[0], day, sources=("twse",))))
    assert [(q.quote.date, q.factor, q.close) for q in after.quotes[:2]] == [
        (q.quote.date, q.factor, q.close) for q in before.quotes
    ]
    assert after.quotes[2].factor == Decimal("2")
    assert after.quotes[2].close == Decimal("106")
