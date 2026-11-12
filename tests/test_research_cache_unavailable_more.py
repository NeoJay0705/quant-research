from __future__ import annotations

import importlib.util
import json
import sqlite3
from pathlib import Path

import pytest

from quant_research.research import (
    CacheCoverageError,
    NoQuotesError,
    build_cache,
    list_symbols,
    load_cached,
    load_taiwan,
)


def _sequence_fixture():
    path = Path(__file__).with_name("test_taiwan_dataset_pipeline_sequence.py")
    spec = importlib.util.spec_from_file_location("cache_unavailable_more_fixture", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _source(tmp_path: Path, days: tuple[str, ...] = ("2024-01-01", "2024-01-02")) -> Path:
    root = tmp_path / "source"
    _sequence_fixture()._install(root, days, tuple(range(10, 10 + len(days))), ())
    return root


def _add_symbols(root: Path, symbols: tuple[str, ...]) -> None:
    """Copy each fixture quote into extra symbol records for each session."""
    with sqlite3.connect(root / "state.sqlite3") as db:
        rows = db.execute(
            "SELECT r.snapshot_id,r.record_date,r.record_key,r.payload,r.quality_flags "
            "FROM records r JOIN snapshots s ON s.id=r.snapshot_id "
            "WHERE s.dataset='MI_INDEX' ORDER BY r.record_date"
        ).fetchall()
        for snapshot, day, key, raw, flags in rows:
            for symbol in symbols:
                payload = json.loads(raw)
                payload["symbol"] = symbol
                payload["name"] = "ETF" if symbol == "2887" and day.endswith("01") else "普通股"
                db.execute(
                    "INSERT INTO records(snapshot_id,kind,symbol,record_date,record_key,payload,"
                    "quality_flags) "
                    "VALUES(?,?,?,?,?,?,?)",
                    (snapshot, "quote", symbol, day, f"{key}-{symbol}", json.dumps(payload), flags),
                )
        db.commit()


def _rows(cache: Path, symbol: str) -> tuple[int, int]:
    with sqlite3.connect(f"file:{cache}?mode=ro", uri=True) as db:
        bars = db.execute("SELECT count(*) FROM bars WHERE symbol=?", (symbol,)).fetchone()[0]
        events = db.execute("SELECT count(*) FROM events WHERE symbol=?", (symbol,)).fetchone()[0]
    return bars, events


def test_invalid_symbol_shape_skips_quote_and_event_normalization(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import quant_research.research.cache as cache_module
    import quant_research.taiwan.dataset.build as build_module

    root = _source(tmp_path)
    _add_symbols(root, ("2887F", "123456"))
    original_normalize = build_module.normalize_quote

    def reject_invalid(scope, record, classifier):
        payload = record.payload
        if payload is not None and payload.get("symbol") in {"2887F", "123456"}:
            pytest.fail("normalized invalid symbol")
        return original_normalize(scope, record, classifier)

    monkeypatch.setattr(build_module, "normalize_quote", reject_invalid)
    original_events = cache_module.load_events

    def reject_invalid_events(reader, symbol, **kwargs):
        if symbol in {"2887F", "123456"}:
            pytest.fail("loaded events for invalid symbol")
        return original_events(reader, symbol, **kwargs)

    monkeypatch.setattr(cache_module, "load_events", reject_invalid_events)
    monkeypatch.setattr(build_module, "load_events", reject_invalid_events, raising=False)
    cache = tmp_path / "cache.sqlite3"
    build_cache(cache, root, "twse", "2024-01-01", "2024-01-02")
    statuses = {s.symbol: s for s in list_symbols(cache, "twse")}
    for symbol in ("2887F", "123456"):
        assert (statuses[symbol].status, statuses[symbol].reason) == (
            "unavailable",
            "classification:unknown:invalid_symbol_shape",
        )
        assert _rows(cache, symbol) == (0, 0)


def test_zero_zero_symbol_is_excluded(tmp_path: Path) -> None:
    root = _source(tmp_path)
    _add_symbols(root, ("0050",))
    cache = tmp_path / "cache.sqlite3"
    build_cache(cache, root, "twse", "2024-01-01", "2024-01-02")
    status = {s.symbol: s for s in list_symbols(cache, "twse")}["0050"]
    assert status.status == "unavailable"
    assert status.reason is not None and status.reason.startswith(
        "classification:excluded_non_ordinary:"
    )
    assert _rows(cache, "0050") == (0, 0)


def test_per_quote_classifications_must_be_consistent(tmp_path: Path) -> None:
    root = _source(tmp_path)
    _add_symbols(root, ("2887",))
    cache = tmp_path / "cache.sqlite3"
    build_cache(cache, root, "twse", "2024-01-01", "2024-01-02")
    status = {s.symbol: s for s in list_symbols(cache, "twse")}["2887"]
    assert status.status == "unavailable"
    assert status.reason == "classification:mixed:ordinary_rule_limited,unknown"
    assert _rows(cache, "2887") == (0, 0)


def test_corrupt_quote_isolated_and_read_side_ranges(tmp_path: Path) -> None:
    root = _source(tmp_path, ("2024-01-01", "2024-01-02", "2024-01-03"))
    _add_symbols(root, ("2887",))
    with sqlite3.connect(root / "state.sqlite3") as db:
        row = db.execute(
            "SELECT id,payload FROM records WHERE symbol='2887' AND record_date='2024-01-02'"
        ).fetchone()
        payload = json.loads(row[1])
        payload["close"] = "not-a-decimal"
        db.execute("UPDATE records SET payload=? WHERE id=?", (json.dumps(payload), row[0]))
        db.commit()
    cache = tmp_path / "cache.sqlite3"
    build_cache(cache, root, "twse", "2024-01-01", "2024-01-03")
    status = {s.symbol: s for s in list_symbols(cache, "twse")}["2887"]
    assert status.status == "unavailable"
    assert "financial_value_invalid_decimal" in (status.reason or "")
    assert _rows(cache, "2887") == (0, 0)
    assert _rows(cache, "2330")[0] == 3
    with pytest.raises(Exception, match="financial_value_invalid_decimal"):
        load_taiwan(root, "twse", ("2887",), "2024-01-01", "2024-01-03")

    with pytest.raises(CacheCoverageError):
        load_cached(cache, "twse", None, "2024-01-01", "2024-01-04")


def test_bar_validation_failure_is_symbol_local(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import quant_research.research.cache as cache_module

    root = _source(tmp_path)
    _add_symbols(root, ("2888",))
    original = cache_module.validate_bar

    def reject_one_symbol(bar):
        if bar.symbol == "2888":
            raise ValueError("injected bar validation failure")
        original(bar)

    monkeypatch.setattr(cache_module, "validate_bar", reject_one_symbol)
    cache = tmp_path / "cache.sqlite3"
    build_cache(cache, root, "twse", "2024-01-01", "2024-01-02")
    statuses = {s.symbol: s for s in list_symbols(cache, "twse")}
    assert statuses["2888"].status == "unavailable"
    assert "injected bar validation failure" in (statuses["2888"].reason or "")
    assert _rows(cache, "2888") == (0, 0)
    assert statuses["2330"].status == "available"
    assert _rows(cache, "2330")[0] == 2


def test_read_all_symbols_uses_quotes_in_requested_range(tmp_path: Path) -> None:
    root = _source(tmp_path, ("2024-01-01", "2024-01-02"))
    _add_symbols(root, ("2887",))
    with sqlite3.connect(root / "state.sqlite3") as db:
        db.execute("DELETE FROM records WHERE symbol='2887' AND record_date='2024-01-01'")
        db.commit()
    cache = tmp_path / "cache.sqlite3"
    build_cache(cache, root, "twse", "2024-01-01", "2024-01-02")
    assert load_cached(cache, "twse", None, "2024-01-01", "2024-01-01").symbols == ("2330",)
    assert load_cached(cache, "twse", None, "2024-01-02", "2024-01-02").symbols == ("2330", "2887")
    with pytest.raises(NoQuotesError, match="no quotes"):
        load_cached(cache, "twse", ("2887",), "2024-01-01", "2024-01-01")
    with pytest.raises(CacheCoverageError):
        load_cached(cache, "twse", None, "2024-01-01", "2024-01-03")
