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
    ResearchInputError,
    UnsupportedDataError,
    build_cache,
    cache_status,
    load_cached,
    update_cache,
)


def _module(filename: str, name: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(filename))
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _dump(path: Path) -> tuple[tuple[str, tuple[tuple[Any, ...], ...]], ...]:
    with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True) as db:
        tables = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        return tuple(
            (table, tuple(sorted(db.execute(f'SELECT * FROM "{table}"').fetchall(), key=repr)))
            for table in sorted(tables)
        )


def _setup(
    tmp_path: Path, *, evidence: bool = False
) -> tuple[Path, Path, tuple[str, ...], Path | None]:
    days = ("2024-01-01", "2024-01-02", "2024-01-03", "2024-01-04")
    root, cache = tmp_path / "source", tmp_path / "cache.sqlite3"
    _module("test_taiwan_dataset_pipeline_sequence.py", "update_safety_sequence")._install(
        root, days, (100, 101, 102, 103), ()
    )
    manifest = None
    if evidence:
        evidence_dir = tmp_path / "evidence"
        evidence_dir.mkdir()
        _module("test_taiwan_dataset_classification.py", "update_safety_classification")._evidence(
            evidence_dir, listed=[("2330", "台積電", "ESVUFR")]
        )
        manifest = evidence_dir / "manifest.json"
    build_cache(cache, root, "twse", days[0], days[1], manifest)
    return root, cache, days, manifest


def test_preconditions_reject_without_changing_database(tmp_path: Path) -> None:
    root, cache, days, _manifest = _setup(tmp_path)
    before = _dump(cache)
    cases: tuple[tuple[Any, ...], ...] = (
        (cache, root, "twse", days[1], ResearchInputError),
        (cache, root, "twse", days[0], ResearchInputError),
        (cache, root, "tpex", days[-1], CacheError),
        (cache, root, "twse", "not-a-date", ResearchInputError),
        ("cache.sqlite3", root, "twse", days[-1], ResearchInputError),
        (cache, "source", "twse", days[-1], ResearchInputError),
    )
    for args in cases:
        with pytest.raises(args[-1]):
            update_cache(*args[:-1])
        assert _dump(cache) == before
    missing = tmp_path / "missing.sqlite3"
    with pytest.raises(CacheError):
        update_cache(missing, root, "twse", days[-1])
    assert not missing.exists()
    assert _dump(cache) == before


def test_unusable_quote_scope_aborts_update_without_changes(tmp_path: Path) -> None:
    root, cache, days, _manifest = _setup(tmp_path)
    with sqlite3.connect(root / "state.sqlite3") as db:
        rows = db.execute(
            "SELECT id,logical_key,edition,source,dataset,params,kind "
            "FROM tasks WHERE dataset='MI_INDEX'"
        ).fetchall()
        matching = [row for row in rows if '"2024-01-03"' in row[5]]
        assert matching
        task = matching[0]
        db.execute(
            "INSERT INTO tasks(id,logical_key,edition,source,dataset,params,kind,state,snapshot_id)"
            "VALUES(?,?,?,?,?,?,?,'failed',NULL)",
            ("failed-latest", task[1], task[2] + 1, task[3], task[4], task[5], task[6]),
        )
    before = _dump(cache)
    with pytest.raises(UnsupportedDataError):
        update_cache(cache, root, "twse", days[2])
    assert _dump(cache) == before


def test_update_without_observed_session_is_atomic(tmp_path: Path) -> None:
    root, cache, days, _manifest = _setup(tmp_path)
    with sqlite3.connect(root / "state.sqlite3") as db:
        db.execute("DELETE FROM records WHERE kind='quote' AND record_date>?", (days[1],))
    before = _dump(cache)
    with pytest.raises(UnsupportedDataError, match="no observed market sessions"):
        update_cache(cache, root, "twse", days[-1])
    assert _dump(cache) == before


def test_protected_cache_paths_are_refused_without_changes(tmp_path: Path) -> None:
    root, cache, days, _manifest = _setup(tmp_path)
    before = _dump(cache)
    targets = (root / "nested" / "cache.sqlite3",)
    for target in targets:
        with pytest.raises(CacheError):
            update_cache(target, root, "twse", days[-1])
        assert not target.parent.exists()
    alias = tmp_path / "source-alias"
    alias.symlink_to(root, target_is_directory=True)
    with pytest.raises(CacheError):
        update_cache(alias / "alias.sqlite3", root, "twse", days[-1])
    evidence_dir = tmp_path / "protected-evidence"
    evidence_dir.mkdir()
    _module("test_taiwan_dataset_classification.py", "update_safety_protected")._evidence(
        evidence_dir, listed=[("2330", "台積電", "ESVUFR")]
    )
    manifest = evidence_dir / "manifest.json"
    evidence_cache = tmp_path / "evidence-cache.sqlite3"
    build_cache(evidence_cache, root, "twse", days[0], days[1], manifest)
    evidence_before = _dump(evidence_cache)
    with pytest.raises(CacheError):
        update_cache(evidence_dir / "refused.sqlite3", root, "twse", days[-1], manifest)
    assert not (evidence_dir / "refused.sqlite3").exists()
    assert _dump(cache) == before
    assert _dump(evidence_cache) == evidence_before


@pytest.mark.parametrize("tamper", ["schema_version", "adjustment_rule", "drop"])
def test_meta_tampering_is_not_repaired_by_update(tmp_path: Path, tamper: str) -> None:
    root, cache, days, _manifest = _setup(tmp_path)
    with sqlite3.connect(cache) as db:
        if tamper == "drop":
            db.execute("DROP TABLE meta")
        else:
            db.execute("UPDATE meta SET value='bad' WHERE key=?", (tamper,))
    before = _dump(cache)
    with pytest.raises(CacheIncompatibleError):
        update_cache(cache, root, "twse", days[-1])
    assert _dump(cache) == before


def test_evidence_hash_mismatch_rejected_but_same_manifest_updates(tmp_path: Path) -> None:
    root, cache, days, manifest = _setup(tmp_path, evidence=True)
    assert manifest is not None
    before = _dump(cache)
    with pytest.raises(CacheError, match="evidence hashes differ"):
        update_cache(cache, root, "twse", days[2])
    assert _dump(cache) == before
    _module("test_taiwan_dataset_classification.py", "update_safety_changed")._evidence(
        manifest.parent, listed=[("2330", "台積電", "CE...")]
    )
    with pytest.raises(CacheError, match="evidence hashes differ"):
        update_cache(cache, root, "twse", days[2], manifest)
    assert _dump(cache) == before
    # Restore original evidence bytes from a fresh fixture-created manifest.
    _module("test_taiwan_dataset_classification.py", "update_safety_restored")._evidence(
        manifest.parent, listed=[("2330", "台積電", "ESVUFR")]
    )
    update_cache(cache, root, "twse", days[2], manifest)


@pytest.mark.parametrize("interrupt", [False, True], ids=["exception", "keyboard-interrupt"])
def test_update_failure_after_insert_rolls_back_everything(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, interrupt: bool
) -> None:
    root, cache, days, _manifest = _setup(tmp_path)
    before = _dump(cache)
    loaded_before = load_cached(cache, "twse")
    insert = cache_module._insert_bar_rows

    def fail_after_insert(db: sqlite3.Connection, rows: list[tuple[Any, ...]]) -> None:
        insert(db, rows)
        error: type[BaseException] = KeyboardInterrupt if interrupt else RuntimeError
        raise error("injected update interruption")

    monkeypatch.setattr(cache_module, "_insert_bar_rows", fail_after_insert)
    error = KeyboardInterrupt if interrupt else RuntimeError
    with pytest.raises(error, match="injected update interruption"):
        update_cache(cache, root, "twse", days[2])
    assert _dump(cache) == before
    assert load_cached(cache, "twse") == loaded_before


def test_update_writer_exclusion_is_immediate_then_retry_succeeds(tmp_path: Path) -> None:
    root, cache, days, _manifest = _setup(tmp_path)
    lock = sqlite3.connect(cache, timeout=0)
    lock.execute("BEGIN IMMEDIATE")
    started = time.monotonic()
    try:
        with pytest.raises(sqlite3.OperationalError):
            update_cache(cache, root, "twse", days[2])
        assert time.monotonic() - started < 1
    finally:
        lock.rollback()
        lock.close()
    update_cache(cache, root, "twse", days[2])


def _fingerprint(root: Path) -> tuple[tuple[str, int, int, str], ...]:
    rows = []
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
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


def test_successful_update_preserves_source_and_updates_protection_and_status(
    tmp_path: Path,
) -> None:
    root, cache, days, manifest = _setup(tmp_path, evidence=True)
    assert manifest is not None
    before = _fingerprint(root) + _fingerprint(manifest.parent)
    update_cache(cache, root, "twse", days[2], manifest)
    after = _fingerprint(root) + _fingerprint(manifest.parent)
    assert after == before
    loaded = load_cached(cache, "twse")
    assert root.resolve() in loaded.protected_input_dirs
    assert manifest.parent.resolve() in loaded.protected_input_dirs
    assert cache.parent.resolve() in loaded.protected_input_dirs
    (status,) = cache_status(cache)
    assert status.built_through == days[2]
    assert status.coverage_start == days[0]
