from __future__ import annotations

import importlib.util
import json
import sqlite3
from decimal import Decimal
from pathlib import Path
from typing import Any

from quant_research.research import build_cache, load_cached, load_taiwan


def _test_module(filename: str, name: str) -> Any:
    path = Path(__file__).with_name(filename)
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _assert_parity(actual: Any, expected: Any) -> None:
    assert actual.bars == expected.bars
    assert actual.events == expected.events
    assert actual.sessions == expected.sessions
    assert actual.calendar_basis == expected.calendar_basis
    assert actual.classification_diagnostics == expected.classification_diagnostics
    assert actual.symbols == expected.symbols
    assert (actual.start_date, actual.end_date) == (expected.start_date, expected.end_date)


def test_cache_parity_tpex(tmp_path: Path) -> None:
    helper = _test_module("test_taiwan_dataset_pipeline_tpex.py", "cache_parity_tpex")
    spec = helper._install(tmp_path)
    cache = tmp_path / "cache.sqlite3"
    build_cache(cache, spec.root, "tpex", spec.start_date, spec.end_date)

    expected = load_taiwan(spec.root, "tpex", ("2330",), spec.start_date, spec.end_date)
    actual = load_cached(cache, "tpex", ("2330",), spec.start_date, spec.end_date)
    _assert_parity(actual, expected)


def test_cache_parity_classification_evidence_manifest(tmp_path: Path) -> None:
    sequence = _test_module("test_taiwan_dataset_pipeline_sequence.py", "cache_manifest_sequence")
    classification = _test_module(
        "test_taiwan_dataset_classification.py", "cache_manifest_classification"
    )
    days = ("2024-08-01", "2024-08-02")
    root = tmp_path / "source"
    sequence._install(root, days, (100, 101), ())
    classification._evidence(tmp_path, listed=[("2330", "台積電", "ESVUFR")])
    manifest = tmp_path / "manifest.json"
    cache = tmp_path.parent / f"{tmp_path.name}-cache.sqlite3"
    build_cache(cache, root, "twse", days[0], days[-1], manifest)

    expected = load_taiwan(root, "twse", ("2330",), days[0], days[-1], manifest)
    actual = load_cached(cache, "twse", ("2330",), days[0], days[-1])
    _assert_parity(actual, expected)
    expected_inputs = {
        value
        for value in expected.classification_diagnostics
        if value.startswith("classification_input:")
    }
    actual_inputs = {
        value
        for value in actual.classification_diagnostics
        if value.startswith("classification_input:")
    }
    assert expected_inputs
    assert actual_inputs == expected_inputs


def test_cache_parity_sessions_from_unselected_unavailable_symbol(tmp_path: Path) -> None:
    sequence = _test_module("test_taiwan_dataset_pipeline_sequence.py", "cache_unselected_sequence")
    days = ("2024-09-01", "2024-09-02")
    root = tmp_path / "source"
    sequence._install(root, days, (10, 11), ())
    with sqlite3.connect(root / "state.sqlite3") as db:
        row = db.execute("SELECT id,payload FROM records ORDER BY id DESC LIMIT 1").fetchone()
        assert row is not None
        record_id, raw = row
        payload = json.loads(raw)
        payload["symbol"] = "0050"
        db.execute(
            "UPDATE records SET symbol='0050',payload=? WHERE id=?",
            (json.dumps(payload, ensure_ascii=False), record_id),
        )
    cache = tmp_path / "cache.sqlite3"
    build_cache(cache, root, "twse", days[0], days[-1])

    expected = load_taiwan(root, "twse", ("2330",), days[0], days[-1])
    actual = load_cached(cache, "twse", ("2330",), days[0], days[-1])
    _assert_parity(actual, expected)
    assert actual.sessions == days
    assert [bar.date for bar in actual.bars] == [days[0]]


def test_cache_parity_linked_twse_event_details(tmp_path: Path) -> None:
    research = _test_module("test_research_taiwan.py", "cache_linked_twse")
    event_helper = _test_module("test_taiwan_dataset_events.py", "cache_linked_twse_events")
    research._install_linked_twse_research_fixture(
        tmp_path,
        "TWT49U",
        event_helper.twse_summary(
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
    start, end = "2024-02-29", "2024-03-01"
    root = tmp_path
    cache = tmp_path.parent / f"{tmp_path.name}-cache.sqlite3"
    build_cache(cache, root, "twse", start, end)

    expected = load_taiwan(root, "twse", ("2330",), start, end)
    actual = load_cached(cache, "twse", ("2330",), start, end)
    _assert_parity(actual, expected)
    (event,) = actual.events
    assert event.payload == expected.events[0].payload
    assert (event.par_before, event.par_after) == (
        expected.events[0].par_before,
        expected.events[0].par_after,
    )
    assert (event.cash_credit_date, event.share_available_date) == (
        expected.events[0].cash_credit_date,
        expected.events[0].share_available_date,
    )
    assert event.source_refs == expected.events[0].source_refs
    assert event.quality_flags == expected.events[0].quality_flags
    assert (
        event.classification,
        event.classification_reason,
        event.classification_refs,
    ) == (
        expected.events[0].classification,
        expected.events[0].classification_reason,
        expected.events[0].classification_refs,
    )
    assert event.dividend == Decimal("2")
    assert event.bonus_share_ratio == Decimal("0.125")


def test_cache_parity_two_markets_same_symbol_are_isolated(tmp_path: Path) -> None:
    helper = _test_module("test_taiwan_dataset_pipeline_tpex.py", "cache_two_markets")
    spec = helper._install(tmp_path)
    cache = tmp_path / "cache.sqlite3"
    build_cache(cache, spec.root, "twse", spec.start_date, spec.end_date)
    build_cache(cache, spec.root, "tpex", spec.start_date, spec.end_date)

    for market in ("twse", "tpex"):
        expected = load_taiwan(spec.root, market, ("2330",), spec.start_date, spec.end_date)
        actual = load_cached(cache, market, ("2330",), spec.start_date, spec.end_date)
        _assert_parity(actual, expected)
        assert all(bar.market == market for bar in actual.bars)
        assert all(event.market == market for event in actual.events)
