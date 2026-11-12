from __future__ import annotations

import importlib.util
from decimal import Decimal
from pathlib import Path

import pytest

from quant_research.research import (
    CacheError,
    CacheStatus,
    SymbolStatus,
    build_cache,
    cache_status,
    list_symbols,
    load_cached,
    load_taiwan,
)


def _sequence_fixture():
    path = Path(__file__).with_name("test_taiwan_dataset_pipeline_sequence.py")
    spec = importlib.util.spec_from_file_location("cache_sequence_fixture", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_research_cache_first_build_and_read_parity_twse(tmp_path: Path) -> None:
    days = ("2024-01-01", "2024-01-02", "2024-01-03")
    root = tmp_path / "source"
    _sequence_fixture()._install(
        root, days, (100, 102, 51), ((days[0], 200, 100), (days[2], 100, 50))
    )
    cache = tmp_path / "derived.sqlite3"
    summary = build_cache(cache, root, "twse", days[0], days[-1])
    assert (summary.available_count, summary.unavailable_count) == (1, 0)
    expected = load_taiwan(root, "twse", ("2330",), days[0], days[-1])
    actual = load_cached(cache, "twse", ("2330",))
    assert actual.bars == expected.bars
    assert actual.events == expected.events
    assert actual.sessions == expected.sessions
    assert actual.symbols == expected.symbols
    assert actual.classification_diagnostics == expected.classification_diagnostics
    assert actual.calendar_basis == expected.calendar_basis
    assert actual.source_diagnostics
    assert cache.parent.resolve() in actual.protected_input_dirs
    assert load_cached(cache, "twse", ("2330",), days[1], days[-1]).bars == expected.bars[1:]
    assert actual.bars[-1].adjusted_close == Decimal("102")
    assert cache_status(cache) == (CacheStatus("twse", days[0], days[-1], 1, 0),)
    assert list_symbols(cache, "twse") == (
        SymbolStatus("2330", "available", None, days[0], days[-1]),
    )


def test_research_cache_refuses_second_build_for_same_market(tmp_path: Path) -> None:
    days = ("2024-01-01", "2024-01-02")
    root = tmp_path / "source"
    _sequence_fixture()._install(root, days, (10, 11), ())
    cache = tmp_path / "derived.sqlite3"
    build_cache(cache, root, "twse", days[0], days[-1])
    with pytest.raises(CacheError, match="already built"):
        build_cache(cache, root, "twse", days[0], days[-1])
