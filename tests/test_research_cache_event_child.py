from __future__ import annotations

import importlib.util
import json
import sqlite3
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from quant_research.research import (
    UnsupportedDataError,
    build_cache,
    list_symbols,
    load_cached,
    load_taiwan,
)


def _test_module(filename: str, name: str) -> Any:
    path = Path(__file__).with_name(filename)
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_cache_isolates_invalid_linked_event_child_by_symbol(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    events = _test_module("test_taiwan_dataset_events.py", "cache_invalid_child_events")
    research = _test_module("test_research_taiwan.py", "cache_invalid_child_research")
    root = tmp_path / "source"
    research._install_linked_twse_research_fixture(
        root,
        "TWT49U",
        events.twse_summary(
            "TWT49U",
            ["資料日期", "股票代號", "除權息前收盤價", "除權息參考價", "詳細資料"],
            ["2024-03-01", "2331", "100", "90", "2331,20240301"],
        ),
        {
            "stat": "OK",
            "fields": [
                "股票代號",
                "(每股配發現金股利)除息",
                "A. 按普通股股東持股比例每千股無償配股",
            ],
            "data": [["2331", "2", "125"]],
        },
    )
    # The fixture has a normal 2330 quote. Keep it for B and duplicate it as A.
    with sqlite3.connect(root / "state.sqlite3") as db:
        rows = db.execute(
            "SELECT r.snapshot_id,r.record_date,r.record_key,r.payload,r.quality_flags "
            "FROM records r JOIN snapshots s ON s.id=r.snapshot_id "
            "WHERE s.dataset='MI_INDEX' ORDER BY r.record_date"
        ).fetchall()
        for snapshot, day, key, raw, flags in rows:
            payload = json.loads(raw)
            assert payload["symbol"] == "2330"
            payload["symbol"] = "2331"
            db.execute(
                "UPDATE records SET symbol='2331',payload=? WHERE snapshot_id=? AND record_date=?",
                (json.dumps(payload, ensure_ascii=False), snapshot, day),
            )
            payload["symbol"] = "2330"
            db.execute(
                "INSERT INTO records(snapshot_id,kind,symbol,record_date,record_key,payload,"
                "quality_flags) VALUES(?,?,?,?,?,?,?)",
                (snapshot, "quote", "2330", day, f"{key}-2330", json.dumps(payload), flags),
            )
        db.commit()

    import quant_research.research.taiwan as adapter

    original_helper = adapter._iter_series_from_reader

    def alter_event_snapshot(reader: Any, evidence: Any) -> Any:
        series = tuple(original_helper(reader, evidence))
        return tuple(
            replace(
                item,
                events=tuple(
                    replace(
                        event,
                        ref=replace(event.ref, detail_snapshot_id="wrong-child-snapshot"),
                    )
                    if event.symbol == "2331"
                    else event
                    for event in item.events
                ),
            )
            for item in series
        )

    monkeypatch.setattr(adapter, "_iter_series_from_reader", alter_event_snapshot)
    import quant_research.research.cache as cache_module

    original_load_events = cache_module.load_events

    def alter_cached_events(reader: Any, symbol: str, **kwargs: Any) -> Any:
        result = original_load_events(reader, symbol, **kwargs)
        if symbol != "2331":
            return result
        return tuple(
            replace(event, ref=replace(event.ref, detail_snapshot_id="wrong-child-snapshot"))
            for event in result
        )

    monkeypatch.setattr(cache_module, "load_events", alter_cached_events)
    start, end = "2024-02-29", "2024-03-01"
    with pytest.raises(UnsupportedDataError, match="invalid exact event child scope"):
        load_taiwan(root, "twse", ("2331",), start, end)

    cache = tmp_path / "cache.sqlite3"
    build_cache(cache, root, "twse", start, end)
    statuses = {status.symbol: status for status in list_symbols(cache, "twse")}
    assert statuses["2331"].status == "unavailable"
    assert statuses["2331"].reason is not None
    assert statuses["2331"].reason.startswith("UnsupportedDataError:")
    assert "invalid exact event child scope" in statuses["2331"].reason
    assert statuses["2330"].status == "available"
    with sqlite3.connect(f"file:{cache}?mode=ro", uri=True) as db:
        assert db.execute("SELECT count(*) FROM bars WHERE symbol='2331'").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM events WHERE symbol='2331'").fetchone()[0] == 0

    expected = load_taiwan(root, "twse", ("2330",), start, end)
    actual = load_cached(cache, "twse", ("2330",), start, end)
    assert actual.bars == expected.bars
    assert actual.events == expected.events
    assert actual.sessions == expected.sessions
    assert actual.calendar_basis == expected.calendar_basis
    assert actual.classification_diagnostics == expected.classification_diagnostics
    assert actual.symbols == expected.symbols
    assert (actual.start_date, actual.end_date) == (expected.start_date, expected.end_date)
