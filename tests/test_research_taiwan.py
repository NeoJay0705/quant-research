from __future__ import annotations

import importlib.util
import json
import sqlite3
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from quant_research.research import ResearchInputError, UnsupportedDataError, load_taiwan


def _sequence_fixture() -> Any:
    path = Path(__file__).with_name("test_taiwan_dataset_pipeline_sequence.py")
    spec = importlib.util.spec_from_file_location("sequence_fixture", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_taiwan_adapter_scope_calendar_and_core_parity(tmp_path: Path) -> None:
    days = ("2024-01-01", "2024-01-02", "2024-01-03")
    # Fixture's parsed source has the selected 2330 plus a second quote identity.
    helper = _sequence_fixture()
    root = tmp_path / "twse"
    helper._install(root, days, (100, 102, 51), ((days[0], 200, 100), (days[2], 100, 50)))
    data = load_taiwan(root, "twse", ("2330",), days[0], days[-1])
    assert data.market == "twse" and data.symbols == ("2330",)
    assert data.sessions == days
    assert [bar.close for bar in data.bars] == list(map(Decimal, ("100", "102", "51")))
    assert [bar.adjusted_close for bar in data.bars] == list(map(Decimal, ("100", "102", "102")))
    assert data.bars[0].source_refs[0].startswith("MI_INDEX:")
    assert data.events[0].effective_date == days[0]
    assert data.events[0].cash_credit_date is None
    assert data.events[0].share_available_date is None
    with pytest.raises(UnsupportedDataError, match="no quote records"):
        load_taiwan(root, "twse", ("9999",), days[0], days[-1])


def test_taiwan_adapter_rejects_missing_database_and_row_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(UnsupportedDataError, match="does not exist"):
        load_taiwan(tmp_path / "missing", "twse", ("2330",), "2024-01-01", "2024-01-02")
    days = ("2024-02-01", "2024-02-02")
    root = tmp_path / "source"
    _sequence_fixture()._install(root, days, (10, 11), ())
    import quant_research.research.taiwan as adapter

    def unexpected_helper(*args: Any, **kwargs: Any) -> Any:
        pytest.fail("core series helper called before max_rows rejection")

    monkeypatch.setattr(adapter, "_iter_series_from_reader", unexpected_helper)
    with pytest.raises(ResearchInputError, match="max_rows"):
        load_taiwan(root, "twse", ("2330",), days[0], days[-1], max_rows=1)


def test_market_session_from_only_unselected_symbol_is_retained(tmp_path: Path) -> None:
    days = ("2024-04-01", "2024-04-02")
    root = tmp_path / "source"
    _sequence_fixture()._install(root, days, (10, 11), ())
    db = sqlite3.connect(root / "state.sqlite3")
    row = db.execute("SELECT id,payload FROM records ORDER BY id DESC LIMIT 1").fetchone()
    assert row is not None
    record_id, raw = row
    payload = json.loads(raw)
    payload["symbol"] = "9999"
    db.execute(
        "UPDATE records SET symbol='9999',payload=? WHERE id=?", (json.dumps(payload), record_id)
    )
    db.commit()
    db.close()
    data = load_taiwan(root, "twse", ("2330",), days[0], days[-1])
    assert data.sessions == days
    assert [bar.date for bar in data.bars] == [days[0]]


def test_explicit_sessions_must_cover_all_requested_bars(tmp_path: Path) -> None:
    days = ("2024-03-01", "2024-03-02")
    root = tmp_path / "source"
    _sequence_fixture()._install(root, days, (10, 11), ())
    with pytest.raises(ResearchInputError, match="omit source bar"):
        load_taiwan(root, "twse", ("2330",), days[0], days[-1], sessions=(days[0],))


def test_taiwan_adapter_event_classification_matches_selected_bar(tmp_path: Path) -> None:
    days = ("2024-05-01", "2024-05-02", "2024-05-03")
    root = tmp_path / "classified-event"
    _sequence_fixture()._install(root, days, (100, 101, 102), ((days[1], 100, 90),))

    data = load_taiwan(root, "twse", ("2330",), days[0], days[-1])
    (event,) = data.events
    selected_bar = next(bar for bar in data.bars if bar.symbol == event.symbol)
    assert (event.classification, event.classification_reason, event.classification_refs) == (
        selected_bar.classification,
        selected_bar.classification_reason,
        selected_bar.classification_refs,
    )
    assert event.classification != "unknown"
    assert event.classification_reason != "no_quote"


def _tpex_fixture() -> Any:
    path = Path(__file__).with_name("test_taiwan_dataset_pipeline_tpex.py")
    spec = importlib.util.spec_from_file_location("tpex_fixture", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _install_linked_twse_research_fixture(
    root: Path, dataset: str, summary: dict[str, Any], detail: dict[str, Any]
) -> None:
    events = importlib.util.spec_from_file_location(
        "events_fixture", Path(__file__).with_name("test_taiwan_dataset_events.py")
    )
    assert events and events.loader
    events_helper = importlib.util.module_from_spec(events)
    events.loader.exec_module(events_helper)
    spec = events_helper.install(
        root,
        parent_dataset=dataset,
        summary=summary,
        child_dataset="TWT49UDetail" if dataset == "TWT49U" else "TWTAVUDetail",
        detail=detail,
    )
    sequence = _sequence_fixture()
    with sqlite3.connect(root / "state.sqlite3") as db:
        for index, (day, close) in enumerate((("2024-02-29", 100), ("2024-03-01", 90))):
            quote = {
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
            sequence._store_parsed(
                db,
                source="twse",
                dataset="MI_INDEX",
                request={"date": day},
                payload=quote,
                index=index,
            )
        db.execute(
            "UPDATE records SET quality_flags=? WHERE snapshot_id='snap-parent-parent-1'",
            (json.dumps(["summary-fixture-flag"]),),
        )
        if dataset == "TWTAUU":
            record_id, raw = db.execute(
                "SELECT id,payload FROM records WHERE snapshot_id='snap-parent-parent-1'"
            ).fetchone()
            record = json.loads(raw)
            record["stop_date"] = "2024-01-02"
            db.execute(
                "UPDATE records SET payload=? WHERE id=?",
                (json.dumps(record, ensure_ascii=False), record_id),
            )
        db.execute(
            "UPDATE records SET quality_flags=? WHERE snapshot_id='snap-child-parent-1'",
            (json.dumps(["child-fixture-flag"]),),
        )
    assert spec.root == root


def test_load_taiwan_preserves_actual_linked_twse_dividend_metadata(tmp_path: Path) -> None:
    events = importlib.util.spec_from_file_location(
        "events_fixture", Path(__file__).with_name("test_taiwan_dataset_events.py")
    )
    assert events and events.loader
    events_helper = importlib.util.module_from_spec(events)
    events.loader.exec_module(events_helper)
    _install_linked_twse_research_fixture(
        tmp_path,
        "TWT49U",
        events_helper.twse_summary(
            "TWT49U",
            ["資料日期", "股票代號", "除權息前收盤價", "除權息參考價", "詳細資料"],
            ["2024-03-01", "2330", "100", "90", "2330,20240301"],
        ),
        {
            "stat": "OK",
            "fields": [
                "股票代號",
                "(每股配發現金股利)除息",
                "A. 按普通股股東持股比例每千股無償配股",
            ],
            "data": [["2330", "2", "125"]],
        },
    )
    data = load_taiwan(tmp_path, "twse", ("2330",), "2024-02-29", "2024-03-01")
    (event,) = data.events
    assert event.event_type == "dividend"
    assert event.dividend == Decimal("2") and event.bonus_share_ratio == Decimal("0.125")
    assert "TWT49U:snap-parent-parent-1:1" in event.source_refs
    assert "TWT49UDetail:snap-child-parent-1:2" in event.source_refs
    assert {"summary-fixture-flag", "child-fixture-flag"} <= set(event.quality_flags)
    quote = next(bar for bar in data.bars if bar.date == event.effective_date)
    assert (event.classification, event.classification_reason, event.classification_refs) == (
        quote.classification,
        quote.classification_reason,
        quote.classification_refs,
    )


def test_load_taiwan_preserves_actual_linked_twse_reduction_metadata(tmp_path: Path) -> None:
    events = importlib.util.spec_from_file_location(
        "events_fixture", Path(__file__).with_name("test_taiwan_dataset_events.py")
    )
    assert events and events.loader
    events_helper = importlib.util.module_from_spec(events)
    events.loader.exec_module(events_helper)
    _install_linked_twse_research_fixture(
        tmp_path,
        "TWTAUU",
        events_helper.twse_summary(
            "TWTAUU",
            ["恢復買賣日期", "股票代號", "停止買賣前收盤價格", "恢復買賣參考價", "詳細資料"],
            ["2024-03-01", "2330", "100", "90", "2330,20240102"],
        ),
        {
            "stat": "OK",
            "fields": [
                "股票代號",
                "每壹仟股換發新股票",
                "每股退還股款",
                "(每股配發現金股利)除息",
            ],
            "data": [["2330", "940 股", "0.6 元/股", "0.1 元/股"]],
        },
    )
    data = load_taiwan(tmp_path, "twse", ("2330",), "2024-02-29", "2024-03-01")
    (event,) = data.events
    assert event.event_type == "capital_reduction"
    assert event.share_multiplier == Decimal("0.94")
    assert event.cash_return_per_share == Decimal("0.6") and event.dividend == Decimal("0.1")
    assert "TWTAUU:snap-parent-parent-1:1" in event.source_refs
    assert "TWTAVUDetail:snap-child-parent-1:2" in event.source_refs
    assert {"summary-fixture-flag", "child-fixture-flag"} <= set(event.quality_flags)
    quote = next(bar for bar in data.bars if bar.date == event.effective_date)
    assert (event.classification, event.classification_reason, event.classification_refs) == (
        quote.classification,
        quote.classification_reason,
        quote.classification_refs,
    )
    assert dict(event.payload)["stop_date"] == "2024-01-02"


def test_load_taiwan_rejects_invalid_exact_linked_twse_child_scope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from dataclasses import replace

    events = importlib.util.spec_from_file_location(
        "events_fixture", Path(__file__).with_name("test_taiwan_dataset_events.py")
    )
    assert events and events.loader
    events_helper = importlib.util.module_from_spec(events)
    events.loader.exec_module(events_helper)
    _install_linked_twse_research_fixture(
        tmp_path,
        "TWT49U",
        events_helper.twse_summary(
            "TWT49U",
            ["資料日期", "股票代號", "除權息前收盤價", "除權息參考價", "詳細資料"],
            ["2024-03-01", "2330", "100", "90", "2330,20240301"],
        ),
        {
            "stat": "OK",
            "fields": [
                "股票代號",
                "(每股配發現金股利)除息",
                "A. 按普通股股東持股比例每千股無償配股",
            ],
            "data": [["2330", "2", "125"]],
        },
    )
    import quant_research.research.taiwan as adapter

    original_helper = adapter._iter_series_from_reader
    observed: list[Any] = []

    def alter_event_snapshot(reader: Any, evidence: Any) -> Any:
        series = tuple(original_helper(reader, evidence))
        observed.extend(series)
        return tuple(
            replace(
                item,
                events=tuple(
                    replace(
                        event,
                        ref=replace(event.ref, detail_snapshot_id="wrong-child-snapshot"),
                    )
                    for event in item.events
                ),
            )
            for item in series
        )

    monkeypatch.setattr(adapter, "_iter_series_from_reader", alter_event_snapshot)
    with pytest.raises(UnsupportedDataError, match="invalid exact event child scope"):
        load_taiwan(tmp_path, "twse", ("2330",), "2024-02-29", "2024-03-01")
    assert observed and any(item.events for item in observed)


def test_load_taiwan_pins_linked_child_metadata_during_wal_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    events = importlib.util.spec_from_file_location(
        "events_fixture", Path(__file__).with_name("test_taiwan_dataset_events.py")
    )
    assert events and events.loader
    events_helper = importlib.util.module_from_spec(events)
    events.loader.exec_module(events_helper)
    root = tmp_path / "pinned-wal"
    _install_linked_twse_research_fixture(
        root,
        "TWT49U",
        events_helper.twse_summary(
            "TWT49U",
            ["資料日期", "股票代號", "除權息前收盤價", "除權息參考價", "詳細資料"],
            ["2024-03-01", "2330", "100", "90", "2330,20240301"],
        ),
        {
            "stat": "OK",
            "fields": [
                "股票代號",
                "(每股配發現金股利)除息",
                "A. 按普通股股東持股比例每千股無償配股",
            ],
            "data": [["2330", "2", "125"]],
        },
    )
    database = root / "state.sqlite3"
    with sqlite3.connect(database) as db:
        assert db.execute("PRAGMA journal_mode=WAL").fetchone()[0].lower() == "wal"
    import quant_research.research.taiwan as adapter

    original_helper = adapter._iter_series_from_reader
    old_flag, new_flag = "child-fixture-flag", "new-commit-flag"
    observed: dict[str, Any] = {}

    def commit_between_series_and_adapter(reader: Any, evidence: Any) -> Any:
        observed["query_only"] = reader._db.execute("PRAGMA query_only").fetchone()[0]
        observed["journal_mode"] = reader._db.execute("PRAGMA journal_mode").fetchone()[0]
        series = tuple(original_helper(reader, evidence))
        with sqlite3.connect(database, timeout=5) as writer:
            writer.execute(
                "UPDATE records SET quality_flags=? WHERE snapshot_id=?",
                (json.dumps([new_flag]), "snap-child-parent-1"),
            )
            assert writer.execute("SELECT changes()").fetchone()[0] == 1
            writer.commit()
        yield from series

    monkeypatch.setattr(adapter, "_iter_series_from_reader", commit_between_series_and_adapter)
    pinned = load_taiwan(root, "twse", ("2330",), "2024-02-29", "2024-03-01")
    (pinned_event,) = pinned.events
    assert observed == {"query_only": 1, "journal_mode": "wal"}
    assert old_flag in pinned_event.quality_flags
    assert new_flag not in pinned_event.quality_flags
    assert pinned_event.dividend == Decimal("2")
    assert pinned_event.bonus_share_ratio == Decimal("0.125")
    assert "TWT49UDetail:snap-child-parent-1:2" in pinned_event.source_refs
    assert all(not diagnostic.diagnostics for diagnostic in pinned.source_diagnostics)
    assert root.resolve() in pinned.protected_input_dirs

    monkeypatch.setattr(adapter, "_iter_series_from_reader", original_helper)
    fresh = load_taiwan(root, "twse", ("2330",), "2024-02-29", "2024-03-01")
    (fresh_event,) = fresh.events
    assert new_flag in fresh_event.quality_flags
    assert old_flag not in fresh_event.quality_flags
    assert fresh_event.dividend == pinned_event.dividend
    assert fresh_event.bonus_share_ratio == pinned_event.bonus_share_ratio
    assert fresh_event.source_refs == pinned_event.source_refs


def _set_tpex_par_metadata(root: Path, before: object, after: object) -> None:
    with sqlite3.connect(root / "state.sqlite3") as db:
        row = db.execute(
            "SELECT id,payload FROM records WHERE snapshot_id IN "
            "(SELECT id FROM snapshots WHERE dataset='pvChgRslt') LIMIT 1"
        ).fetchone()
        assert row is not None
        record_id, raw = row
        payload = json.loads(raw)
        payload["share_multiplier"] = "4"
        payload["par_before"] = before
        payload["par_after"] = after
        db.execute("UPDATE records SET payload=? WHERE id=?", (json.dumps(payload), record_id))


def test_load_taiwan_preserves_tpex_par_value_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from dataclasses import replace

    from quant_research.taiwan import official

    original_parse = official.parse

    def parse_m4_inline_html(batch: Any, request: Any) -> Any:
        if batch.source == "tpex" and batch.dataset == "pvChgRslt":
            body = (
                batch.body.decode()
                .replace(
                    "<td>每股換發新股票</td><td>3</td><td>變更前面額</td><td>10</td>"
                    "<td>變更後面額</td><td>5",
                    "<td>每股換發新股票</td><td>4</td><td>變更前面額</td><td>10</td>"
                    "<td>變更後面額</td><td>2.5",
                )
                .encode()
            )
            batch = replace(batch, body=body)
        return original_parse(batch, request)

    monkeypatch.setattr(official, "parse", parse_m4_inline_html)
    _tpex_fixture()._install(tmp_path)
    data = load_taiwan(tmp_path / "source", "tpex", ("2330",), "2024-01-01", "2024-01-05")
    (event,) = data.events
    assert (event.par_before, event.par_after) == (Decimal("10"), Decimal("2.5"))
    assert (event.share_multiplier, event.before_price, event.reference_price) == (
        Decimal("4"),
        Decimal("10"),
        Decimal("5"),
    )


def test_load_taiwan_rejects_float_par_before_payload(tmp_path: Path) -> None:
    _tpex_fixture()._install(tmp_path)
    _set_tpex_par_metadata(tmp_path / "source", 10.0, "2.5")
    with pytest.raises(UnsupportedDataError, match="invalid par_before"):
        load_taiwan(tmp_path / "source", "tpex", ("2330",), "2024-01-01", "2024-01-05")


def test_latest_failed_quote_edition_does_not_fall_back_to_success(tmp_path: Path) -> None:
    days = ("2024-06-01", "2024-06-02")
    root = tmp_path / "failed-edition"
    _sequence_fixture()._install(root, days, (10, 11), ())
    with sqlite3.connect(root / "state.sqlite3") as db:
        task = db.execute(
            "SELECT id,logical_key,source,dataset,params,kind,snapshot_id FROM tasks "
            "WHERE source='twse' AND kind='official_quote' ORDER BY rowid LIMIT 1"
        ).fetchone()
        assert task is not None
        old_id, logical_key, source, dataset, params, kind, snapshot_id = task
        edition = db.execute("SELECT edition FROM tasks WHERE id=?", (old_id,)).fetchone()[0]
        db.execute(
            "INSERT INTO tasks(id,logical_key,edition,source,dataset,"
            "params,kind,state,snapshot_id) VALUES(?,?,?,?,?,?,?,?,?)",
            (
                "failed-latest",
                logical_key,
                edition + 1,
                source,
                dataset,
                params,
                kind,
                "failed",
                snapshot_id,
            ),
        )
    with pytest.raises(UnsupportedDataError):
        load_taiwan(root, "twse", ("2330",), days[0], days[-1])


def test_parser_derived_twse_0050_raw_bar_is_retained_as_excluded(tmp_path: Path) -> None:
    days = ("2024-07-01",)
    root = tmp_path / "0050"
    _sequence_fixture()._install(root, days, (100,), ())
    with sqlite3.connect(root / "state.sqlite3") as db:
        row = db.execute(
            "SELECT id,payload FROM records WHERE kind='quote' ORDER BY id LIMIT 1"
        ).fetchone()
        assert row is not None
        record_id, raw = row
        payload = json.loads(raw)
        payload["symbol"] = "0050"
        db.execute(
            "UPDATE records SET symbol='0050',payload=? WHERE id=?",
            (json.dumps(payload, ensure_ascii=False), record_id),
        )
    data = load_taiwan(root, "twse", ("0050",), days[0], days[-1])
    (bar,) = data.bars
    assert bar.symbol == "0050"
    assert bar.classification == "excluded_non_ordinary"
    assert bar.adjusted_close is None
