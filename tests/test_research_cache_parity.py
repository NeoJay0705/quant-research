from __future__ import annotations

import importlib.util
from pathlib import Path

from quant_research.research import build_cache, load_cached, load_taiwan


def _sequence_fixture():
    path = Path(__file__).with_name("test_taiwan_dataset_pipeline_sequence.py")
    spec = importlib.util.spec_from_file_location("cache_parity_fixture", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _assert_parity(actual, expected) -> None:
    assert actual.bars == expected.bars
    assert actual.events == expected.events
    assert actual.sessions == expected.sessions
    assert actual.calendar_basis == expected.calendar_basis
    assert actual.classification_diagnostics == expected.classification_diagnostics
    assert actual.symbols == expected.symbols
    assert (actual.start_date, actual.end_date) == (expected.start_date, expected.end_date)


def test_cache_parity_market_and_later_slice(tmp_path: Path) -> None:
    market = "twse"
    days = ("2024-01-01", "2024-01-02", "2024-01-03")
    root, cache = tmp_path / "source", tmp_path / f"{market}.sqlite3"
    _sequence_fixture()._install(
        root, days, (100, 102, 51), ((days[0], 200, 100), (days[2], 100, 50))
    )
    build_cache(cache, root, market, days[0], days[-1])
    expected = load_taiwan(root, market, ("2330",), days[0], days[-1])
    actual = load_cached(cache, market, ("2330",))
    _assert_parity(actual, expected)
    later = load_cached(cache, market, ("2330",), days[1], days[-1])
    assert later.bars == expected.bars[1:]
    assert later.bars[-1].adjusted_close == expected.bars[-1].adjusted_close
    assert later.bars[-1].close == expected.bars[-1].close
    # Cached diagnostics are the complete stored scope records, not slice-specific.
    assert actual.source_diagnostics
    sliced = load_cached(cache, market, ("2330",), days[1], days[-1])
    assert sliced.source_diagnostics == actual.source_diagnostics


def test_cache_loading_does_not_consult_source_after_build(tmp_path: Path) -> None:
    days = ("2024-02-01", "2024-02-02")
    root, cache = tmp_path / "source", tmp_path / "cache.sqlite3"
    _sequence_fixture()._install(root, days, (10, 11), ())
    build_cache(cache, root, "twse", days[0], days[-1])
    moved = tmp_path / "renamed-source"
    root.rename(moved)
    loaded = load_cached(cache, "twse", ("2330",))
    assert len(loaded.bars) == 2


def test_decimal_text_codec_preserves_exponent_and_scale() -> None:
    from decimal import Decimal

    from quant_research.research.cache import _decimal, _decimal_from

    for value in (Decimal("10.0"), Decimal("1E+1")):
        assert str(_decimal_from(_decimal(value))) == str(value)
