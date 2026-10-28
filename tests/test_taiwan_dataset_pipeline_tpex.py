from __future__ import annotations

import json
import sqlite3
from decimal import Decimal
from pathlib import Path

from quant_research.taiwan import official, sources
from quant_research.taiwan.dataset.build import iter_series
from quant_research.taiwan.dataset.events import load_events
from quant_research.taiwan.dataset.models import BuildSpec
from quant_research.taiwan.dataset.source import SourceReader
from quant_research.taiwan.storage import SCHEMA

START = "2024-01-01"
END = "2024-01-05"
EVENT_DAY = "2024-01-03"


def _parse(source: str, dataset: str, payload: dict, request: dict):
    raw = json.dumps(payload, ensure_ascii=False).encode()
    batch = sources.RawBatch(source, dataset, "offline", "https://example.invalid", 200, raw, "", 0)
    return official.parse(batch, request)


def _store_scope(db, *, source: str, dataset: str, lane: str, request: dict, payload: dict):
    params = {"pipeline": "official-v1", "lane": lane, "request": request}
    parsed = _parse(source, dataset, payload, request)
    request_key = request.get("date", request.get("start_date", "scope"))
    task_id = f"{source}-{dataset}-{request_key}"
    snapshot_id = f"snapshot-{task_id}"
    db.execute(
        "INSERT INTO snapshots VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            snapshot_id,
            source,
            dataset,
            json.dumps(params, sort_keys=True),
            "url",
            task_id,
            "2024-01-06",
            200,
            f"hash-{task_id}",
            "raw",
            "valid",
            None,
        ),
    )
    db.execute(
        "INSERT INTO tasks(id,logical_key,edition,source,dataset,params,kind,state,snapshot_id) "
        "VALUES(?,?,?,?,?,?,?,?,?)",
        (
            task_id,
            task_id,
            1,
            source,
            dataset,
            json.dumps(params, sort_keys=True),
            "official_quote" if lane == "quotes" else "official_event_summary",
            "completed",
            snapshot_id,
        ),
    )
    record_kind = "quote" if lane == "quotes" else "event"
    for index, record in enumerate(parsed.records):
        record_date = record.get("trade_date", record.get("event_date"))
        db.execute(
            "INSERT INTO records(snapshot_id,kind,symbol,record_date,record_key,payload,"
            "quality_flags) VALUES(?,?,?,?,?,?,?)",
            (
                snapshot_id,
                record_kind,
                record["symbol"],
                record_date,
                str(index),
                json.dumps(record, ensure_ascii=False),
                "[]",
            ),
        )


def _quote_payload(source: str, day: str) -> dict:
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
    rows = [["2330", "台積電", "100", "100", "100", "100", "100", "10000"]]
    if source == "tpex":
        rows.append(["2317", "鴻海", "100", "100", "100", "100", "100", "10000"])
    return {
        "stat": "OK" if source == "twse" else "ok",
        "date": day.replace("-", "") if source == "twse" else "113/01/" + day[-2:],
        "tables": [{"title": "每日收盤行情", "fields": fields, "data": rows}],
    }


def _install(tmp_path: Path) -> BuildSpec:
    root = tmp_path / "source"
    root.mkdir()
    db = sqlite3.connect(root / "state.sqlite3")
    db.executescript(SCHEMA)
    for source in ("tpex", "twse"):
        dataset = "dailyQuotes" if source == "tpex" else "MI_INDEX"
        for day in ("2024-01-02", "2024-01-04"):
            _store_scope(
                db,
                source=source,
                dataset=dataset,
                lane="quotes",
                request={"date": day},
                payload=_quote_payload(source, day),
            )

    _store_scope(
        db,
        source="tpex",
        dataset="pvChgRslt",
        lane="events",
        request={"start_date": START, "end_date": END},
        payload={
            "stat": "ok",
            "fields": [
                "恢復買賣日期",
                "證券代號",
                "最後交易日之收盤價格",
                "恢復買賣開始參考價",
                "詳細資料",
            ],
            "data": [
                [
                    "113/01/03",
                    "2330",
                    "10",
                    "5",
                    "<table><tr><td>股票代號/股票名稱</td><td>2330/台積電</td>"
                    "<td>每股換發新股票</td><td>3</td><td>變更前面額</td><td>10</td>"
                    "<td>變更後面額</td><td>5</td></tr></table>",
                ]
            ],
            "date": "113/01/01~113/01/05",
        },
    )
    _store_scope(
        db,
        source="tpex",
        dataset="pvChgRslt",
        lane="events",
        request={"start_date": "2024-01-02", "end_date": END},
        payload={
            "stat": "ok",
            "fields": [
                "恢復買賣日期",
                "證券代號",
                "最後交易日之收盤價格",
                "恢復買賣開始參考價",
                "詳細資料",
            ],
            "data": [
                [
                    "113/01/03",
                    "2317",
                    "10",
                    "2.5",
                    "<table><tr><td>股票代號/股票名稱</td><td>2317/鴻海</td>"
                    "<td>每股換發新股票</td><td>4</td><td>變更前面額</td><td>10</td>"
                    "<td>變更後面額</td><td>2.5</td></tr></table>",
                ]
            ],
            "date": "113/01/02~113/01/05",
        },
    )
    _store_scope(
        db,
        source="twse",
        dataset="TWTB8U",
        lane="events",
        request={"start_date": START, "end_date": END},
        payload={
            "stat": "OK",
            "fields": [
                "恢復買賣日期",
                "股票代號",
                "停止買賣前收盤價格",
                "恢復買賣參考價",
                "詳細資料",
            ],
            "data": [["2024-01-03", "2330", "10", "5", "2330,20240102,20240103"]],
            "strDate": "2024-01-01",
            "endDate": "2024-01-05",
        },
    )
    db.commit()
    db.close()
    return BuildSpec(root, START, END)


def test_integrated_tpex_inline_multiplier_is_not_price_ratio_and_source_isolated(tmp_path):
    spec = _install(tmp_path)
    series = {(item.source, item.symbol): item for item in iter_series(spec)}
    tpex = series[("tpex", "2330")]
    twse = series[("twse", "2330")]

    assert [quote.date for quote in tpex.raw_quotes] == ["2024-01-02", "2024-01-04"]
    assert [quote.factor for quote in tpex.quotes] == [Decimal("1"), Decimal("3")]
    assert [quote.factor for quote in twse.quotes] == [Decimal("1"), Decimal("2")]
    assert len(tpex.events) == len(twse.events) == 1
    tpex_event = tpex.events[0]
    twse_event = twse.events[0]
    assert (tpex_event.source, tpex_event.share_multiplier) == ("tpex", Decimal("3"))
    assert (tpex_event.before_price, tpex_event.reference_price) == (Decimal("10"), Decimal("5"))
    assert tpex_event.cash_return_per_share == Decimal("0")
    assert tpex_event.ref.dataset == "pvChgRslt"
    assert tpex_event.ref.detail_dataset is None
    assert twse_event.source == "twse"
    assert (twse_event.before_price, twse_event.reference_price) == (Decimal("10"), Decimal("5"))
    assert twse_event.share_multiplier is None


def test_default_event_loading_keeps_distinct_symbol_scopes_and_pipeline_events(tmp_path):
    spec = _install(tmp_path)
    with SourceReader(spec) as reader:
        events = load_events(reader)
        filtered_events = load_events(reader, symbol="2317")
    expected_identities = {("tpex", "2330"), ("tpex", "2317"), ("twse", "2330")}
    assert {(event.source, event.symbol) for event in events} == expected_identities
    assert {(event.source, event.symbol) for event in filtered_events} == {("tpex", "2317")}
    by_source = {event.source: event for event in events if event.symbol == "2330"}
    assert by_source["tpex"].share_multiplier == Decimal("3")
    assert by_source["twse"].share_multiplier is None
    assert (by_source["twse"].before_price, by_source["twse"].reference_price) == (
        Decimal("10"),
        Decimal("5"),
    )

    series = {(item.source, item.symbol): item for item in iter_series(spec)}
    assert set(series) == expected_identities
    tpex_2317 = series[("tpex", "2317")]
    assert len(tpex_2317.events) == 1
    assert tpex_2317.events[0].symbol == "2317"
    assert tpex_2317.events[0].share_multiplier == Decimal("4")
    assert [quote.factor for quote in tpex_2317.quotes] == [Decimal("1"), Decimal("4")]
    for source, multiplier in (("tpex", Decimal("3")), ("twse", Decimal("2"))):
        own = series[(source, "2330")]
        assert len(own.events) == 1
        assert own.events[0].source == source
        if source == "tpex":
            assert own.events[0].share_multiplier == multiplier
        else:
            assert own.events[0].share_multiplier is None
            assert own.events[0].before_price / own.events[0].reference_price == multiplier
        assert [quote.factor for quote in own.quotes] == [Decimal("1"), multiplier]
