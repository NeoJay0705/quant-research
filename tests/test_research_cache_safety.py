from __future__ import annotations

import hashlib
import importlib.util
import sqlite3
import time
from pathlib import Path
from typing import Any

import pytest

import quant_research.research.cache as cache_module
from quant_research.research import (
    CacheError,
    CacheIncompatibleError,
    build_cache,
    cache_status,
    list_symbols,
    load_cached,
)


def _sequence_fixture() -> Any:
    path = Path(__file__).with_name("test_taiwan_dataset_pipeline_sequence.py")
    spec = importlib.util.spec_from_file_location("cache_safety_sequence", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _tpex_fixture() -> Any:
    path = Path(__file__).with_name("test_taiwan_dataset_pipeline_tpex.py")
    spec = importlib.util.spec_from_file_location("cache_safety_tpex", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _dump(path: Path) -> tuple[tuple[str, tuple[tuple[Any, ...], ...]], ...]:
    db = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        names = [
            row[0]
            for row in db.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
        ]
        return tuple(
            (name, tuple(sorted(db.execute(f'SELECT * FROM "{name}"').fetchall(), key=repr)))
            for name in names
        )
    finally:
        db.close()


def _twse(tmp_path: Path, name: str = "source") -> tuple[Path, str, str]:
    days = ("2024-01-01", "2024-01-02")
    root = tmp_path / name
    _sequence_fixture()._install(root, days, (10, 11), ())
    return root, days[0], days[-1]


def _two_markets(tmp_path: Path) -> tuple[Path, str, str]:
    _tpex_fixture()._install(tmp_path)
    return tmp_path / "source", "2024-01-02", "2024-01-04"


def _inject_insert_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    real_connect = cache_module._connect_write

    class ConnectionProxy:
        def __init__(self, connection: sqlite3.Connection) -> None:
            self._connection = connection

        def __getattr__(self, name: str) -> Any:
            return getattr(self._connection, name)

        def executemany(self, sql: str, values: Any) -> Any:
            if sql.startswith("INSERT INTO bars"):
                raise RuntimeError("injected build interruption")
            return self._connection.executemany(sql, values)

    monkeypatch.setattr(
        cache_module,
        "_connect_write",
        lambda path: ConnectionProxy(real_connect(path)),
    )


@pytest.mark.parametrize("interrupt", [False, True], ids=["exception", "keyboard-interrupt"])
def test_failed_first_build_removes_database_and_sidecars(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, interrupt: bool
) -> None:
    root, start, end = _twse(tmp_path)
    cache = tmp_path / "cache.sqlite3"
    error: type[BaseException] = KeyboardInterrupt if interrupt else RuntimeError
    if interrupt:

        def fail(*args: Any, **kwargs: Any) -> Any:
            raise KeyboardInterrupt("injected build interruption")

        monkeypatch.setattr(cache_module, "calculate_adjustments", fail)
    else:
        _inject_insert_failure(monkeypatch)
    with pytest.raises(error, match="injected .* interruption"):
        build_cache(cache, root, "twse", start, end)
    assert not cache.exists()
    assert not Path(f"{cache}-wal").exists()
    assert not Path(f"{cache}-shm").exists()


@pytest.mark.parametrize("interrupt", [False, True], ids=["exception", "keyboard-interrupt"])
def test_failed_second_market_is_atomic_and_not_covered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, interrupt: bool
) -> None:
    root, start, end = _two_markets(tmp_path)
    cache = tmp_path / "cache.sqlite3"
    build_cache(cache, root, "twse", start, end)
    before = _dump(cache)
    expected = load_cached(cache, "twse", ("2330",))
    error: type[BaseException] = KeyboardInterrupt if interrupt else RuntimeError
    if interrupt:

        def fail(*args: Any, **kwargs: Any) -> Any:
            raise KeyboardInterrupt("injected second-market interruption")

        monkeypatch.setattr(cache_module, "calculate_adjustments", fail)
    else:
        _inject_insert_failure(monkeypatch)
    with pytest.raises(error, match="injected .* interruption"):
        build_cache(cache, root, "tpex", start, end)
    assert _dump(cache) == before
    assert load_cached(cache, "twse", ("2330",)) == expected
    assert all(status.market != "tpex" for status in cache_status(cache))


def test_writer_exclusion_is_immediate_and_retry_succeeds(tmp_path: Path) -> None:
    root, start, end = _two_markets(tmp_path)
    cache = tmp_path / "cache.sqlite3"
    build_cache(cache, root, "twse", start, end)
    before = _dump(cache)
    lock = sqlite3.connect(cache, timeout=0)
    lock.execute("BEGIN IMMEDIATE")
    started = time.monotonic()
    try:
        with pytest.raises(sqlite3.OperationalError):
            build_cache(cache, root, "tpex", start, end)
        assert time.monotonic() - started < 1
        assert _dump(cache) == before
    finally:
        lock.rollback()
        lock.close()
    build_cache(cache, root, "tpex", start, end)
    assert {status.market for status in cache_status(cache)} == {"twse", "tpex"}


def _fingerprint(root: Path) -> tuple[tuple[str, int, int, str], ...]:
    result = []
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        stat = path.stat()
        result.append(
            (
                path.relative_to(root).as_posix(),
                stat.st_size,
                stat.st_mtime_ns,
                hashlib.sha256(path.read_bytes()).hexdigest(),
            )
        )
    return tuple(result)


def test_build_and_reads_leave_source_unchanged(tmp_path: Path) -> None:
    root, start, end = _twse(tmp_path)
    before = tuple(row for row in _fingerprint(root) if not row[0].endswith(("-wal", "-shm")))
    cache = tmp_path / "cache.sqlite3"
    build_cache(cache, root, "twse", start, end)
    load_cached(cache, "twse", ("2330",))
    cache_status(cache)
    list_symbols(cache, "twse")
    # Read-only SQLite access may create transient WAL coordination sidecars.
    after = tuple(row for row in _fingerprint(root) if not row[0].endswith(("-wal", "-shm")))
    assert after == before


def test_cache_path_protection_and_missing_parent(tmp_path: Path) -> None:
    root, start, end = _twse(tmp_path)
    inside = root / "nested" / "cache.sqlite3"
    with pytest.raises(CacheError):
        build_cache(inside, root, "twse", start, end)
    assert not inside.parent.exists()
    alias = tmp_path / "alias"
    alias.symlink_to(root, target_is_directory=True)
    with pytest.raises(CacheError):
        build_cache(alias / "alias-cache.sqlite3", root, "twse", start, end)
    missing = tmp_path / "never-created" / "cache.sqlite3"
    with pytest.raises(CacheError):
        build_cache(missing, root, "twse", start, end)
    assert not missing.parent.exists()
    assert not Path(f"{missing}-wal").exists()
    assert not Path(f"{missing}-shm").exists()


def test_meta_tampering_is_rejected_by_all_operations_without_repair(
    tmp_path: Path,
) -> None:
    root, start, end = _two_markets(tmp_path)
    cache = tmp_path / "cache.sqlite3"
    build_cache(cache, root, "twse", start, end)
    for tamper in ("schema_version", "adjustment_rule", "drop"):
        if tamper == "drop":
            db = sqlite3.connect(cache)
            db.execute("DROP TABLE meta")
        else:
            db = sqlite3.connect(cache)
            db.execute("UPDATE meta SET value=? WHERE key=?", ("bad", tamper))
        db.commit()
        db.close()
        before = _dump(cache)
        for operation in (
            lambda: load_cached(cache, "twse"),
            lambda: cache_status(cache),
            lambda: list_symbols(cache, "twse"),
            lambda: build_cache(cache, root, "tpex", start, end),
        ):
            with pytest.raises(CacheIncompatibleError):
                operation()
        assert _dump(cache) == before
        # Restore a clean cache for the next independent tamper scenario.
        cache.unlink()
        build_cache(cache, root, "twse", start, end)


def test_protected_inputs_include_source_evidence_and_current_cache_parent(
    tmp_path: Path,
) -> None:
    root, start, end = _twse(tmp_path)
    evidence_dir = tmp_path / "evidence"
    evidence_dir.mkdir()
    classification_fixture = importlib.util.spec_from_file_location(
        "cache_safety_classification",
        Path(__file__).with_name("test_taiwan_dataset_classification.py"),
    )
    assert classification_fixture and classification_fixture.loader
    module = importlib.util.module_from_spec(classification_fixture)
    classification_fixture.loader.exec_module(module)
    module._evidence(evidence_dir, listed=[("2330", "台積電", "ESVUFR")])
    evidence_manifest = evidence_dir / "manifest.json"
    evidence_before = _fingerprint(evidence_dir)
    protected_cache = evidence_dir / "refused.sqlite3"
    with pytest.raises(CacheError):
        build_cache(protected_cache, root, "twse", start, end, evidence_manifest)
    assert not protected_cache.exists()
    cache = tmp_path / "one" / "cache.sqlite3"
    cache.parent.mkdir()
    build_cache(cache, root, "twse", start, end, evidence_manifest)
    loaded = load_cached(cache, "twse", ("2330",))
    assert root.resolve() in loaded.protected_input_dirs
    assert evidence_dir.resolve() in loaded.protected_input_dirs
    assert cache.parent.resolve() in loaded.protected_input_dirs
    cache_status(cache)
    list_symbols(cache, "twse")
    assert _fingerprint(evidence_dir) == evidence_before
    moved_parent = tmp_path / "two"
    moved_parent.mkdir()
    moved = moved_parent / cache.name
    cache.rename(moved)
    assert moved_parent.resolve() in load_cached(moved, "twse", ("2330",)).protected_input_dirs


def test_duplicate_market_build_preserves_logical_database(tmp_path: Path) -> None:
    root, start, end = _twse(tmp_path)
    cache = tmp_path / "cache.sqlite3"
    build_cache(cache, root, "twse", start, end)
    before = _dump(cache)
    with pytest.raises(CacheError, match="already built"):
        build_cache(cache, root, "twse", start, end)
    assert _dump(cache) == before
