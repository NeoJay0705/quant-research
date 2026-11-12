from __future__ import annotations

import hashlib
import importlib.util
import json
import sqlite3
import time
from pathlib import Path
from typing import Any

import pytest

import quant_research.research.cache as cache_module
from quant_research.research import (
    CacheError,
    CacheIncompatibleError,
    ResearchInputError,
    SymbolNotInCacheError,
    build_cache,
    cache_status,
    load_cached,
    rebuild_cache,
)


def _module(filename: str, name: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(filename))
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _dump(path: Path) -> tuple[tuple[str, tuple[tuple[Any, ...], ...]], ...]:
    with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True) as db:
        tables = [row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        return tuple(
            (table, tuple(sorted(db.execute(f'SELECT * FROM "{table}"').fetchall(), key=repr)))
            for table in sorted(tables)
        )


def _setup(
    tmp_path: Path, *, evidence: bool = False
) -> tuple[Path, Path, tuple[str, ...], Path | None]:
    days = ("2024-01-01", "2024-01-02", "2024-01-03", "2024-01-04")
    root, cache = tmp_path / "source", tmp_path / "cache.sqlite3"
    _module("test_taiwan_dataset_pipeline_sequence.py", "rebuild_safety_sequence")._install(
        root, days, (100, 101, 102, 103), ()
    )
    manifest = None
    if evidence:
        evidence_dir = tmp_path / "evidence"
        evidence_dir.mkdir()
        _module("test_taiwan_dataset_classification.py", "rebuild_safety_classification")._evidence(
            evidence_dir, listed=[("2330", "台積電", "ESVUFR")]
        )
        manifest = evidence_dir / "manifest.json"
    build_cache(cache, root, "twse", days[0], days[-1], manifest)
    return root, cache, days, manifest


def _different_evidence(manifest: Path) -> None:
    _module("test_taiwan_dataset_classification.py", "rebuild_safety_changed")._evidence(
        manifest.parent, listed=[("2330", "台積電", "CE...")]
    )


def test_rebuild_argument_validation_preserves_all_tables(tmp_path: Path) -> None:
    root, cache, days, _manifest = _setup(tmp_path)
    before = _dump(cache)
    bad_args = [
        ((cache, root, "twse", "2023-12-31"), CacheError),
        ((cache, root, "twse", "2024-01-05"), CacheError),
        ((cache, root, "twse", "not-a-date"), ResearchInputError),
        (("cache.sqlite3", root, "twse", days[1]), ResearchInputError),
        ((cache, "source", "twse", days[1]), ResearchInputError),
        ((cache, root, "bad-market", days[1]), ResearchInputError),
        ((cache, root, "tpex", days[1]), CacheError),
        ((cache, root, "twse", days[1], ("9999",)), SymbolNotInCacheError),
        ((cache, root, "twse", days[1], ()), ResearchInputError),
        ((cache, root, "twse", days[1], ("2330", "2330")), ResearchInputError),
        ((cache, root, "twse", days[1], ("2330", 2)), ResearchInputError),
        ((cache, root, "twse", days[1], ("",)), ResearchInputError),
    ]
    invoke: Any = rebuild_cache
    for args, error in bad_args:
        with pytest.raises(error):
            invoke(*args)
        assert _dump(cache) == before
    missing = tmp_path / "missing.sqlite3"
    with pytest.raises(CacheError):
        rebuild_cache(missing, root, "twse", days[1])
    assert not missing.exists()
    assert _dump(cache) == before


def test_evidence_hash_policy_and_full_replacement(tmp_path: Path) -> None:
    root, cache, days, manifest = _setup(tmp_path, evidence=True)
    assert manifest is not None
    before = _dump(cache)
    _different_evidence(manifest)
    for args in (
        (cache, root, "twse", days[1], None, manifest),
        (cache, root, "twse", days[0], ("2330",), manifest),
        (cache, root, "twse", days[1]),
    ):
        with pytest.raises(CacheError):
            rebuild_cache(*args)
        assert _dump(cache) == before

    rebuilt, fresh = tmp_path / "rebuilt.sqlite3", tmp_path / "fresh.sqlite3"
    # Fresh cache must use the changed manifest; create the first cache at identical coverage.
    build_cache(rebuilt, root, "twse", days[0], days[-1], manifest)
    result = rebuild_cache(rebuilt, root, "twse", days[0], None, manifest)
    build_cache(fresh, root, "twse", days[0], days[-1], manifest)
    assert result.coverage_start == days[0] and result.built_through == days[-1]
    queries = (
        "SELECT * FROM bars ORDER BY market,symbol,date",
        "SELECT * FROM events ORDER BY market,symbol,effective_date,event_type",
        "SELECT * FROM symbols ORDER BY market,symbol",
        "SELECT * FROM sessions ORDER BY market,date",
        "SELECT market,coverage_start,built_through,evidence_hashes FROM markets ORDER BY market",
    )

    def content(path: Path) -> tuple[tuple[tuple[Any, ...], ...], ...]:
        with sqlite3.connect(path) as db:
            return tuple(tuple(db.execute(query)) for query in queries)

    assert content(rebuilt) == content(fresh)
    changed = _dump(rebuilt)
    with sqlite3.connect(rebuilt) as db:
        hashes = db.execute("SELECT evidence_hashes FROM markets WHERE market='twse'").fetchone()[0]
    assert hashes != dict((table, rows) for table, rows in before)["markets"][0][3]
    assert changed != before


def test_identical_evidence_allows_partial_rebuild(tmp_path: Path) -> None:
    root, cache, days, manifest = _setup(tmp_path, evidence=True)
    assert manifest is not None
    before = _dump(cache)
    result = rebuild_cache(cache, root, "twse", days[2], None, manifest)
    assert result.coverage_start == days[0] and result.built_through == days[-1]
    assert _dump(cache) == before


@pytest.mark.parametrize("location", ["root", "symlink", "evidence"])
def test_protected_cache_locations_are_refused(tmp_path: Path, location: str) -> None:
    root, cache, days, _manifest = _setup(tmp_path)
    before = _dump(cache)
    if location == "root":
        target = root / "nested" / "cache.sqlite3"
        target.parent.mkdir()
        # Existing cache-file validation is intentionally reached before any writes.
        target.write_bytes(cache.read_bytes())
    elif location == "symlink":
        alias = tmp_path / "source-alias"
        alias.symlink_to(root, target_is_directory=True)
        target = alias / "cache.sqlite3"
        target.write_bytes(cache.read_bytes())
    else:
        evidence_dir = tmp_path / "evidence"
        evidence_dir.mkdir()
        _module("test_taiwan_dataset_classification.py", "rebuild_safety_protected")._evidence(
            evidence_dir, listed=[("2330", "台積電", "ESVUFR")]
        )
        manifest = evidence_dir / "manifest.json"
        evidence_cache = tmp_path / "evidence-cache.sqlite3"
        build_cache(evidence_cache, root, "twse", days[0], days[-1], manifest)
        before = _dump(cache)
        target = evidence_dir / "cache.sqlite3"
        target.write_bytes(cache.read_bytes())
    target_before = target.read_bytes()
    manifest_arg = manifest if location == "evidence" else None
    with pytest.raises(CacheError):
        rebuild_cache(target, root, "twse", days[1], None, manifest_arg)
    assert target.read_bytes() == target_before
    assert _dump(cache) == before


@pytest.mark.parametrize("tamper", ["schema_version", "adjustment_rule", "drop"])
def test_meta_tampering_is_not_repaired_by_rebuild(tmp_path: Path, tamper: str) -> None:
    root, cache, days, _manifest = _setup(tmp_path)
    with sqlite3.connect(cache) as db:
        if tamper == "drop":
            db.execute("DROP TABLE meta")
        else:
            db.execute("UPDATE meta SET value='bad' WHERE key=?", (tamper,))
    before = _dump(cache)
    with pytest.raises(CacheIncompatibleError):
        rebuild_cache(cache, root, "twse", days[1])
    assert _dump(cache) == before


@pytest.mark.parametrize("start_index", [0, 1], ids=["coverage-start", "mid-coverage"])
@pytest.mark.parametrize("failure", ["before-insert", "after-insert", "keyboard-interrupt"])
def test_rebuild_failure_rolls_back_full_dump_and_loaded_data(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    start_index: int,
    failure: str,
) -> None:
    root, cache, days, _manifest = _setup(tmp_path)
    before, loaded_before = _dump(cache), load_cached(cache, "twse")
    insert = cache_module._insert_bar_rows

    def inject(db: sqlite3.Connection, rows: list[tuple[Any, ...]]) -> None:
        if failure == "before-insert":
            raise sqlite3.OperationalError("injected before insert")
        insert(db, rows)
        error: type[BaseException] = (
            KeyboardInterrupt if failure == "keyboard-interrupt" else sqlite3.OperationalError
        )
        raise error("injected after insert")

    monkeypatch.setattr(cache_module, "_insert_bar_rows", inject)
    error = KeyboardInterrupt if failure == "keyboard-interrupt" else sqlite3.OperationalError
    with pytest.raises(error, match="injected"):
        rebuild_cache(cache, root, "twse", days[start_index])
    assert _dump(cache) == before
    assert load_cached(cache, "twse") == loaded_before


def _add_second_quote_symbol(root: Path, symbol: str = "2331") -> None:
    with sqlite3.connect(root / "state.sqlite3") as db:
        rows = db.execute(
            "SELECT r.snapshot_id,r.record_date,r.record_key,r.payload,r.quality_flags "
            "FROM records r JOIN snapshots s ON s.id=r.snapshot_id "
            "WHERE s.dataset='MI_INDEX' ORDER BY r.record_date"
        ).fetchall()
        for snapshot, day, key, raw, flags in rows:
            payload = json.loads(raw)
            payload["symbol"] = symbol
            payload["name"] = "fixture-2331"
            db.execute(
                "INSERT INTO records(snapshot_id,kind,symbol,record_date,record_key,payload,"
                "quality_flags) VALUES(?,?,?,?,?,?,?)",
                (snapshot, "quote", symbol, day, f"{key}-{symbol}", json.dumps(payload), flags),
            )
        db.commit()


@pytest.mark.parametrize("start_index", [0, 1], ids=["coverage-start", "mid-coverage"])
@pytest.mark.parametrize(
    "failure", [RuntimeError, KeyboardInterrupt], ids=["runtime-error", "keyboard-interrupt"]
)
def test_rebuild_atomicity_after_symbol_became_unavailable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    start_index: int,
    failure: type[BaseException],
) -> None:
    root, _single_cache, days, _manifest = _setup(tmp_path)
    _add_second_quote_symbol(root)
    cache = tmp_path / "multi-symbol-cache.sqlite3"
    build_cache(cache, root, "twse", days[0], days[-1])
    before = _dump(cache)
    loaded_before = load_cached(cache, "twse")
    original_validate = cache_module.validate_bar
    original_status = cache_module._write_symbol_status

    def fail_first(bar: Any) -> None:
        if bar.symbol == "2330":
            raise ValueError("first symbol validation injected")
        original_validate(bar)

    def fail_next(db: sqlite3.Connection, market: str, symbol: str, *args: Any) -> None:
        if symbol == "2331" and args and args[0] == "available":
            raise failure("next symbol status injected")
        original_status(db, market, symbol, *args)

    monkeypatch.setattr(cache_module, "validate_bar", fail_first)
    monkeypatch.setattr(cache_module, "_write_symbol_status", fail_next)
    with pytest.raises(failure, match="next symbol status injected"):
        rebuild_cache(cache, root, "twse", days[start_index])
    assert _dump(cache) == before
    assert load_cached(cache, "twse") == loaded_before


def test_rebuild_writer_exclusion_is_immediate_then_retry_succeeds(tmp_path: Path) -> None:
    root, cache, days, _manifest = _setup(tmp_path)
    lock = sqlite3.connect(cache, timeout=0)
    lock.execute("BEGIN IMMEDIATE")
    started = time.monotonic()
    try:
        with pytest.raises(sqlite3.OperationalError):
            rebuild_cache(cache, root, "twse", days[1])
        assert time.monotonic() - started < 1
    finally:
        lock.rollback()
        lock.close()
    rebuild_cache(cache, root, "twse", days[1])


def _fingerprint(root: Path) -> tuple[tuple[str, int, int, str], ...]:
    rows = []
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        if path.name.endswith(("-wal", "-shm")):
            continue
        stat = path.stat()
        rows.append(
            (
                path.relative_to(root).as_posix(),
                stat.st_size,
                stat.st_mtime_ns,
                hashlib.sha256(path.read_bytes()).hexdigest(),
            )
        )
    return tuple(rows)


def test_successful_rebuild_preserves_source_and_records_protected_inputs(tmp_path: Path) -> None:
    root, cache, days, manifest = _setup(tmp_path, evidence=True)
    assert manifest is not None
    before = _fingerprint(root) + _fingerprint(manifest.parent)
    rebuild_cache(cache, root, "twse", days[2], None, manifest)
    assert _fingerprint(root) + _fingerprint(manifest.parent) == before
    loaded = load_cached(cache, "twse")
    assert root.resolve() in loaded.protected_input_dirs
    assert manifest.parent.resolve() in loaded.protected_input_dirs
    assert cache.parent.resolve() in loaded.protected_input_dirs
    (status,) = cache_status(cache)
    assert status.coverage_start == days[0] and status.built_through == days[-1]
