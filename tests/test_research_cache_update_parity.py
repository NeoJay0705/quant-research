from __future__ import annotations

import importlib.util
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from quant_research.research import (
    CacheCoverageError,
    build_cache,
    load_cached,
    load_taiwan,
    update_cache,
)


def _module(filename: str, name: str) -> Any:
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


def _sequence() -> Any:
    return _module("test_taiwan_dataset_pipeline_sequence.py", "update_parity_sequence")


def test_twse_update_matches_load_taiwan_with_share_exchange(tmp_path: Path) -> None:
    days = ("2024-01-01", "2024-01-02", "2024-01-03", "2024-01-04")
    root, cache = tmp_path / "source", tmp_path / "twse.sqlite3"
    _sequence()._install(root, days, (100, 100, 50, 52), ((days[2], 10, 5),))
    build_cache(cache, root, "twse", days[0], days[1])
    update_cache(cache, root, "twse", days[-1])
    actual = load_cached(cache, "twse", ("2330",), days[0], days[-1])
    expected = load_taiwan(root, "twse", ("2330",), days[0], days[-1])
    _assert_parity(actual, expected)
    assert actual.bars[2].adjusted_close != actual.bars[2].close


def test_tpex_update_matches_load_taiwan(tmp_path: Path) -> None:
    spec = _module("test_taiwan_dataset_pipeline_tpex.py", "update_parity_tpex")._install(tmp_path)
    cache = tmp_path / "tpex.sqlite3"
    build_cache(cache, spec.root, "tpex", spec.start_date, "2024-01-02")
    update_cache(cache, spec.root, "tpex", spec.end_date)
    actual = load_cached(cache, "tpex", ("2330",), spec.start_date, spec.end_date)
    expected = load_taiwan(spec.root, "tpex", ("2330",), spec.start_date, spec.end_date)
    _assert_parity(actual, expected)
    assert actual.bars[-1].adjusted_close != actual.bars[-1].close


def test_update_parity_with_classification_evidence_manifest(tmp_path: Path) -> None:
    sequence = _sequence()
    classification = _module(
        "test_taiwan_dataset_classification.py", "update_parity_classification"
    )
    days = ("2024-08-01", "2024-08-02", "2024-08-03")
    root, cache = tmp_path / "source", tmp_path.parent / f"{tmp_path.name}-cache.sqlite3"
    sequence._install(root, days, (100, 101, 102), ())
    classification._evidence(tmp_path, listed=[("2330", "台積電", "ESVUFR")])
    manifest = tmp_path / "manifest.json"
    build_cache(cache, root, "twse", days[0], days[1], manifest)
    update_cache(cache, root, "twse", days[-1], manifest)
    actual = load_cached(cache, "twse", ("2330",), days[0], days[-1])
    expected = load_taiwan(root, "twse", ("2330",), days[0], days[-1], manifest)
    _assert_parity(actual, expected)


def test_linked_twse_event_detail_in_update_range_matches_load_taiwan(tmp_path: Path) -> None:
    research = _module("test_research_taiwan.py", "update_parity_linked_twse")
    events = _module("test_taiwan_dataset_events.py", "update_parity_linked_events")
    root, cache = tmp_path / "source", tmp_path / "cache.sqlite3"
    research._install_linked_twse_research_fixture(
        root,
        "TWT49U",
        events.twse_summary(
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
    build_cache(cache, root, "twse", "2024-02-29", "2024-02-29")
    update_cache(cache, root, "twse", "2024-03-01")
    actual = load_cached(cache, "twse", ("2330",), "2024-02-29", "2024-03-01")
    expected = load_taiwan(root, "twse", ("2330",), "2024-02-29", "2024-03-01")
    _assert_parity(actual, expected)
    (event,) = actual.events
    expected_event = expected.events[0]
    assert event.payload == expected_event.payload
    assert (event.par_before, event.par_after) == (
        expected_event.par_before,
        expected_event.par_after,
    )
    assert (event.cash_credit_date, event.share_available_date) == (
        expected_event.cash_credit_date,
        expected_event.share_available_date,
    )
    assert event.source_refs == expected_event.source_refs
    assert event.quality_flags == expected_event.quality_flags
    assert (event.classification, event.classification_reason, event.classification_refs) == (
        expected_event.classification,
        expected_event.classification_reason,
        expected_event.classification_refs,
    )


def test_later_updated_slice_preserves_full_range_adjusted_prices(tmp_path: Path) -> None:
    days = ("2024-02-01", "2024-02-02", "2024-02-03", "2024-02-04")
    root, cache = tmp_path / "source", tmp_path / "cache.sqlite3"
    _sequence()._install(root, days, (100, 100, 50, 52), ((days[2], 10, 5),))
    build_cache(cache, root, "twse", days[0], days[1])
    update_cache(cache, root, "twse", days[-1])
    full = load_taiwan(root, "twse", ("2330",), days[0], days[-1])
    later = load_cached(cache, "twse", ("2330",), days[2], days[-1])
    assert later.bars == full.bars[2:]


def test_cache_coverage_extends_only_after_update(tmp_path: Path) -> None:
    days = ("2024-04-01", "2024-04-02", "2024-04-03")
    root, cache = tmp_path / "source", tmp_path / "cache.sqlite3"
    _sequence()._install(root, days, (100, 101, 102), ())
    build_cache(cache, root, "twse", days[0], days[1])
    with pytest.raises(CacheCoverageError):
        load_cached(cache, "twse", ("2330",), days[0], days[-1])
    update_cache(cache, root, "twse", days[-1])
    assert len(load_cached(cache, "twse", ("2330",), days[0], days[-1]).bars) == 3


def test_update_one_market_preserves_other_market_and_both_match_source(tmp_path: Path) -> None:
    spec = _module("test_taiwan_dataset_pipeline_tpex.py", "update_two_market_fixture")._install(
        tmp_path
    )
    root = spec.root
    first, second = "2024-01-02", "2024-01-04"
    cache = tmp_path / "shared.sqlite3"
    build_cache(cache, root, "twse", first, first)
    build_cache(cache, root, "tpex", first, first)
    before = load_cached(cache, "tpex", ("2330",), first, first)
    update_cache(cache, root, "twse", second)
    after = load_cached(cache, "tpex", ("2330",), first, first)
    assert before == after
    twse_actual = load_cached(cache, "twse", ("2330",), first, second)
    twse_expected = load_taiwan(root, "twse", ("2330",), first, second)
    _assert_parity(twse_actual, twse_expected)
    tpex_expected = load_taiwan(root, "tpex", ("2330",), first, first)
    _assert_parity(after, tpex_expected)


def test_wrong_continuation_factor_is_detected_by_full_range_comparison(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    days = ("2024-06-01", "2024-06-02", "2024-06-03", "2024-06-04")
    root, cache = tmp_path / "source", tmp_path / "cache.sqlite3"
    _sequence()._install(root, days, (100, 100, 50, 52), ((days[2], 10, 5),))
    build_cache(cache, root, "twse", days[0], days[1])
    import quant_research.research.cache as cache_module

    cache_module_any: Any = cache_module
    original = cache_module_any.process_quotes

    def wrong_continuation(quotes: Any, events: Any, **kwargs: Any) -> Any:
        return original(quotes, events, initial_factor=Decimal(1), applied_through=None)

    monkeypatch.setattr(cache_module, "process_quotes", wrong_continuation)
    update_cache(cache, root, "twse", days[-1])
    corrupted = load_cached(cache, "twse", ("2330",), days[0], days[-1])
    fresh = tmp_path / "fresh.sqlite3"
    build_cache(fresh, root, "twse", days[0], days[-1])
    direct = load_cached(fresh, "twse", ("2330",), days[0], days[-1])
    with pytest.raises(AssertionError):
        _assert_parity(corrupted, direct)
