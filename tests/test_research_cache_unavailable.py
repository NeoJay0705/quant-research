from __future__ import annotations

import importlib.util
import sqlite3
from pathlib import Path

import pytest

from quant_research.research import (
    CacheCoverageError,
    NoQuotesError,
    ResearchInputError,
    SymbolNotInCacheError,
    SymbolUnavailableError,
    build_cache,
    cache_status,
    list_symbols,
    load_cached,
)


def _sequence_fixture():
    path = Path(__file__).with_name("test_taiwan_dataset_pipeline_sequence.py")
    spec = importlib.util.spec_from_file_location("cache_unavailable_fixture", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_cache_read_errors_and_symbol_validation(tmp_path: Path) -> None:
    days = ("2024-03-01", "2024-03-02")
    root, cache = tmp_path / "source", tmp_path / "cache.sqlite3"
    _sequence_fixture()._install(root, days, (10, 11), ())
    build_cache(cache, root, "twse", days[0], days[-1])
    with pytest.raises(SymbolNotInCacheError, match="symbol not in cache"):
        load_cached(cache, "twse", ("9999",))
    db = sqlite3.connect(cache)
    db.execute(
        "INSERT INTO symbols VALUES(?,?,?,?,?,?)",
        ("twse", "9998", "unavailable", "ExampleError: fixture reason", None, None),
    )
    db.commit()
    db.close()
    with pytest.raises(SymbolUnavailableError, match="ExampleError: fixture reason"):
        load_cached(cache, "twse", ("9998",))
    db = sqlite3.connect(cache)
    db.execute("DELETE FROM bars WHERE market='twse' AND symbol='2330' AND date=?", (days[-1],))
    db.commit()
    db.close()
    with pytest.raises(NoQuotesError, match="no quotes"):
        load_cached(cache, "twse", ("2330",), days[-1], days[-1])
    with pytest.raises(CacheCoverageError):
        load_cached(cache, "twse", ("2330",), "2024-02-29", days[-1])
    for symbols in ((), ("2330", "2330"), ("2330", 123)):
        with pytest.raises(ResearchInputError):
            load_cached(cache, "twse", symbols)  # type: ignore[arg-type]


def test_symbol_status_filters_and_status_validation(tmp_path: Path) -> None:
    days = ("2024-04-01", "2024-04-02")
    root, cache = tmp_path / "source", tmp_path / "cache.sqlite3"
    _sequence_fixture()._install(root, days, (10, 11), ())
    build_cache(cache, root, "twse", days[0], days[-1])
    assert [s.symbol for s in list_symbols(cache, "twse", "available")] == ["2330"]
    assert list_symbols(cache, "twse", "unavailable") == ()
    assert cache_status(cache)[0].available_count == 1
    with pytest.raises(ResearchInputError, match="status"):
        list_symbols(cache, "twse", "bad")


def test_sqlite_insert_failure_propagates_and_removes_first_build_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import quant_research.research.cache as cache_module

    days = ("2024-05-01", "2024-05-02")
    root, cache = tmp_path / "source", tmp_path / "cache.sqlite3"
    _sequence_fixture()._install(root, days, (10, 11), ())
    real_connect = cache_module._connect_write

    class ConnectionProxy:
        def __init__(self, conn):
            self._conn = conn

        def __getattr__(self, name):
            return getattr(self._conn, name)

        def executemany(self, sql, values):
            if sql.startswith("INSERT INTO bars"):
                raise sqlite3.OperationalError("injected storage failure")
            return self._conn.executemany(sql, values)

    monkeypatch.setattr(
        cache_module, "_connect_write", lambda path: ConnectionProxy(real_connect(path))
    )
    with pytest.raises(sqlite3.OperationalError, match="injected storage failure"):
        build_cache(cache, root, "twse", days[0], days[-1])
    assert not cache.exists()
    assert not Path(f"{cache}-wal").exists()
    assert not Path(f"{cache}-shm").exists()
