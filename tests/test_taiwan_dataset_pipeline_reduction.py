from __future__ import annotations

import json
import sqlite3
from decimal import Decimal

import pytest

from quant_research.taiwan.dataset.build import iter_series
from quant_research.taiwan.dataset.models import BuildSpec
from tests.test_taiwan_dataset_events import add_twse_parent, install, twse_summary
from tests.test_taiwan_dataset_source import quote_records


def _reduction_summary(*, start: str, end: str, token: str) -> dict:
    summary = twse_summary(
        "TWTAUU",
        ["恢復買賣日期", "股票代號", "停止買賣前收盤價格", "恢復買賣參考價", "詳細資料"],
        ["2024-03-01", "2330", "10", "9", token],
    )
    summary["strDate"] = start
    summary["endDate"] = end
    return summary


def _quotes(root) -> None:
    db = sqlite3.connect(root / "state.sqlite3")
    for index, day in enumerate(("2024-02-29", "2024-03-01")):
        parsed = quote_records(
            day,
            [["2330", "台積電", "100", "100", "100", "100", "1000", "100000"]],
        )
        params = {
            "pipeline": "official-v1",
            "lane": "quotes",
            "request": {"date": day},
        }
        encoded = json.dumps(params, sort_keys=True)
        snapshot = f"quote-snapshot-{index}"
        task = f"quote-task-{index}"
        db.execute(
            "INSERT INTO snapshots VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                snapshot,
                "twse",
                "MI_INDEX",
                encoded,
                "url",
                f"quote/{day}",
                f"{day}T00:00:00+00:00",
                200,
                f"hash-{index}",
                "raw",
                "valid",
                None,
            ),
        )
        db.execute(
            "INSERT INTO tasks(id,logical_key,edition,source,dataset,params,kind,state,"
            "snapshot_id) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (
                task,
                f"quote/{day}",
                1,
                "twse",
                "MI_INDEX",
                encoded,
                "official_quote",
                "completed",
                snapshot,
            ),
        )
        for record_index, record in enumerate(parsed.records):
            db.execute(
                "INSERT INTO records(snapshot_id,kind,symbol,record_date,record_key,payload,"
                "quality_flags) VALUES(?,?,?,?,?,?,?)",
                (
                    snapshot,
                    "quote",
                    record["symbol"],
                    record["record_date"],
                    str(record_index),
                    json.dumps(record, ensure_ascii=False),
                    "[]",
                ),
            )
    db.commit()
    db.close()


@pytest.mark.parametrize(
    ("optional_fields", "optional_values", "expected_dividend", "expected_return"),
    [
        (
            ["每股退還股款", "(每股配發現金股利)除息"],
            ["0.6 元/股", "0.1 元/股"],
            Decimal("0.1"),
            Decimal("0.6"),
        ),
        ([], [], Decimal(0), Decimal(0)),
    ],
    ids=("cash-return-and-dividend", "optional-amounts-absent"),
)
def test_twse_reduction_pipeline_uses_newest_linked_ratio_once(
    tmp_path, optional_fields, optional_values, expected_dividend, expected_return
):
    root = tmp_path / "state"
    old_start, old_end = "2024-01-01", "2024-06-30"
    new_start, new_end = "2024-02-01", "2024-12-31"
    old_detail = {
        "stat": "OK",
        "fields": ["股票代號", "每壹仟股換發新股票", *optional_fields],
        "data": [["2330", "800 股", *(["0.7 元/股", "0.2 元/股"] if optional_fields else [])]],
    }
    install(
        root,
        parent_dataset="TWTAUU",
        summary=_reduction_summary(start=old_start, end=old_end, token="2330,20240102"),
        child_dataset="TWTAVUDetail",
        detail=old_detail,
        logical_key="older-window",
        request={"start_date": old_start, "end_date": old_end},
        fetched="2024-04-01",
    )
    newer_detail = {
        "stat": "OK",
        "fields": ["股票代號", "每壹仟股換發新股票", *optional_fields],
        "data": [["2330", "940 股", *optional_values]],
    }
    add_twse_parent(
        root,
        dataset="TWTAUU",
        summary=_reduction_summary(start=new_start, end=new_end, token="2330,20240202"),
        logical="newer-window",
        edition=1,
        fetched="2024-05-01",
        request={"start_date": new_start, "end_date": new_end},
        detail=newer_detail,
    )
    _quotes(root)

    series_list = list(iter_series(BuildSpec(root, "2024-01-01", "2024-12-31")))
    (series,) = series_list
    assert [(quote.quote.date, quote.factor) for quote in series.quotes] == [
        ("2024-02-29", Decimal(1)),
        ("2024-03-01", Decimal("0.94")),
    ]
    (event,) = series.events
    assert (event.event_type, event.effective_date, event.share_multiplier) == (
        "capital_reduction",
        "2024-03-01",
        Decimal("0.94"),
    )
    assert (event.dividend, event.cash_return_per_share) == (expected_dividend, expected_return)
    assert event.ref.task_id == "parent-newer-window-1"
    assert event.ref.detail_task_id == "child-newer-window-1"
    assert event.ref.detail_dataset == "TWTAVUDetail"
    assert event.ref.detail_snapshot_id == "snap-child-newer-window-1"
    assert event.ref.detail_record_id is not None and event.ref.detail_record_id > 0
