from __future__ import annotations

import importlib.util
import json
import sqlite3
from pathlib import Path

from quant_research.research import build_cache, load_cached, update_cache


def _fixture():
    path = Path(__file__).with_name("test_taiwan_dataset_pipeline_sequence.py")
    spec = importlib.util.spec_from_file_location("cache_update_sequence_fixture", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _content(path: Path) -> tuple[tuple[tuple[object, ...], ...], ...]:
    db = sqlite3.connect(path)
    try:
        return tuple(
            tuple(db.execute(query))
            for query in (
                "SELECT * FROM bars ORDER BY market,symbol,date",
                "SELECT * FROM events ORDER BY market,symbol,effective_date,event_type",
                "SELECT market,symbol,status,reason,first_date,last_date "
                "FROM symbols ORDER BY market,symbol",
                "SELECT * FROM sessions ORDER BY market,date",
                "SELECT market,coverage_start,built_through,evidence_hashes "
                "FROM markets ORDER BY market",
            )
        )
    finally:
        db.close()


def _prefix(path: Path, day: str) -> tuple[tuple[tuple[object, ...], ...], ...]:
    db = sqlite3.connect(path)
    try:
        return (
            tuple(
                db.execute("SELECT * FROM bars WHERE date<=? ORDER BY market,symbol,date", (day,))
            ),
            tuple(
                db.execute(
                    "SELECT * FROM events WHERE effective_date<=? "
                    "ORDER BY market,symbol,effective_date,event_type",
                    (day,),
                )
            ),
        )
    finally:
        db.close()


def _assert_nontrivial(path: Path) -> None:
    db = sqlite3.connect(path)
    try:
        assert db.execute(
            "SELECT 1 FROM bars WHERE factor!='1' OR "
            "(adjusted_close IS NOT NULL AND adjusted_close!=close) LIMIT 1"
        ).fetchone()
    finally:
        db.close()


def _run(
    tmp_path: Path,
    days: tuple[str, ...],
    events: tuple[tuple[str, int, int], ...],
    *,
    missing: tuple[str, ...] = (),
    d1: int = 1,
    d2: int = 3,
):
    root = tmp_path / "source"
    _fixture()._install(root, days, tuple(100 for _ in days), events)
    if missing:
        db = sqlite3.connect(root / "state.sqlite3")
        for day in missing:
            rows = db.execute(
                "SELECT snapshot_id,id AS record_id,payload FROM records "
                "WHERE kind='quote' AND record_date=?",
                (day,),
            ).fetchall()
            for snapshot, record_id, payload in rows:
                if json.loads(payload)["symbol"] == "2330":
                    db.execute(
                        "DELETE FROM records WHERE snapshot_id=? AND id=?", (snapshot, record_id)
                    )
        db.commit()
        db.close()
    incremental, direct = tmp_path / "incremental.sqlite3", tmp_path / "direct.sqlite3"
    build_cache(incremental, root, "twse", days[0], days[d1])
    old_prefix = _prefix(incremental, days[d1])
    update_cache(incremental, root, "twse", days[d2])
    build_cache(direct, root, "twse", days[0], days[d2])
    assert _content(incremental) == _content(direct)
    assert _prefix(incremental, days[d1]) == old_prefix
    _assert_nontrivial(incremental)
    return root, incremental, direct


def test_plain_incremental_update_matches_direct_build(tmp_path: Path) -> None:
    days = ("2024-01-01", "2024-01-02", "2024-01-03", "2024-01-04")
    root = tmp_path / "source"
    _fixture()._install(root, days, (100, 102, 104, 106), ((days[2], 10, 5),))
    incremental, direct = tmp_path / "incremental.sqlite3", tmp_path / "direct.sqlite3"
    build_cache(incremental, root, "twse", days[0], days[1])
    old_prefix = _prefix(incremental, days[1])
    result = update_cache(incremental, root, "twse", days[-1])
    build_cache(direct, root, "twse", days[0], days[-1])
    assert result.coverage_start == days[0]
    assert result.built_through == days[-1]
    assert _content(incremental) == _content(direct)
    assert _prefix(incremental, days[1]) == old_prefix
    _assert_nontrivial(incremental)
    assert load_cached(incremental, "twse").bars == load_cached(direct, "twse").bars


def test_share_exchange_event_on_quote_gap_matches_direct_build(tmp_path: Path) -> None:
    days = tuple(f"2024-02-{n:02d}" for n in range(1, 6))
    _run(tmp_path, days, ((days[2], 10, 5),), missing=(days[2],))


def test_event_on_first_new_quote_date_matches_direct_build(tmp_path: Path) -> None:
    days = tuple(f"2024-03-{n:02d}" for n in range(1, 5))
    _run(tmp_path, days, ((days[2], 10, 3),), d1=1, d2=3)


def test_quote_free_update_period_event_is_stored_then_applied_on_resume(tmp_path: Path) -> None:
    days = tuple(f"2024-04-{n:02d}" for n in range(1, 7))
    root = tmp_path / "source"
    _fixture()._install(root, days, (100,) * len(days), ((days[2], 10, 5),))
    db = sqlite3.connect(root / "state.sqlite3")
    for day in days[2:4]:
        for snapshot, record_id, payload in db.execute(
            "SELECT snapshot_id,id AS record_id,payload FROM records "
            "WHERE kind='quote' AND record_date=?",
            (day,),
        ).fetchall():
            if json.loads(payload)["symbol"] == "2330":
                db.execute(
                    "DELETE FROM records WHERE snapshot_id=? AND id=?", (snapshot, record_id)
                )
    db.commit()
    db.close()
    incremental, direct = tmp_path / "incremental.sqlite3", tmp_path / "direct.sqlite3"
    build_cache(incremental, root, "twse", days[0], days[1])
    prefix = _prefix(incremental, days[1])
    update_cache(incremental, root, "twse", days[4])
    assert _prefix(incremental, days[1]) == prefix
    db = sqlite3.connect(incremental)
    try:
        assert (
            db.execute("SELECT count(*) FROM events WHERE effective_date=?", (days[2],)).fetchone()[
                0
            ]
            == 1
        )
    finally:
        db.close()
    update_cache(incremental, root, "twse", days[5])
    build_cache(direct, root, "twse", days[0], days[5])
    assert _content(incremental) == _content(direct)
    _assert_nontrivial(incremental)


def test_recurring_decimal_multiplier_and_events_around_join_match_exactly(tmp_path: Path) -> None:
    days = tuple(f"2024-05-{n:02d}" for n in range(1, 7))
    _run(tmp_path, days, ((days[0], 10, 3), (days[2], 10, 5), (days[4], 10, 3)), d1=1, d2=5)


def test_repeated_incremental_updates_equal_direct_build(tmp_path: Path) -> None:
    days = tuple(f"2024-06-{n:02d}" for n in range(1, 7))
    root = tmp_path / "source"
    _fixture()._install(
        root,
        (days[0], days[1], days[3], days[5]),
        (100, 100, 100, 100),
        ((days[2], 10, 5), (days[4], 10, 3)),
    )
    incremental, direct = tmp_path / "incremental.sqlite3", tmp_path / "direct.sqlite3"
    source_days = (days[0], days[1], days[3], days[5])
    build_cache(incremental, root, "twse", source_days[0], source_days[1])
    prefix = _prefix(incremental, source_days[1])
    update_cache(incremental, root, "twse", source_days[2])
    assert _prefix(incremental, source_days[1]) == prefix
    middle_prefix = _prefix(incremental, source_days[2])
    update_cache(incremental, root, "twse", source_days[3])
    assert _prefix(incremental, source_days[2]) == middle_prefix
    build_cache(direct, root, "twse", source_days[0], source_days[3])
    assert _content(incremental) == _content(direct)
    _assert_nontrivial(incremental)


def test_new_symbol_earlier_event_rows_match_full_build(tmp_path: Path) -> None:
    days = tuple(f"2024-07-{n:02d}" for n in range(1, 5))
    root = tmp_path / "source"
    _fixture()._install(root, days, (100, 100, 100, 100), ((days[2], 10, 3),))
    db = sqlite3.connect(root / "state.sqlite3")
    # Clone the normalized quote and event inputs under a symbol absent from D1.
    for snapshot, _record_id, payload, record_date, record_key, flags in db.execute(
        "SELECT snapshot_id,id AS record_id,payload,record_date,record_key,quality_flags "
        "FROM records WHERE kind='quote' AND record_date>?",
        (days[2],),
    ).fetchall():
        value = json.loads(payload)
        value["symbol"] = "6000"
        db.execute(
            "INSERT INTO records(snapshot_id,kind,symbol,record_date,record_key,"
            "payload,quality_flags) "
            "VALUES(?,?,?,?,?,?,?)",
            (
                snapshot,
                "quote",
                "6000",
                record_date,
                "new-" + record_key,
                json.dumps(value, ensure_ascii=False),
                flags,
            ),
        )
    for snapshot, _record_id, payload, record_date, record_key, flags in db.execute(
        "SELECT snapshot_id,id AS record_id,payload,record_date,record_key,quality_flags "
        "FROM records WHERE kind='event'"
    ).fetchall():
        value = json.loads(payload)
        value["symbol"] = "6000"
        db.execute(
            "INSERT INTO records(snapshot_id,kind,symbol,record_date,record_key,"
            "payload,quality_flags) "
            "VALUES(?,?,?,?,?,?,?)",
            (
                snapshot,
                "event",
                "6000",
                record_date,
                "new-" + record_key,
                json.dumps(value, ensure_ascii=False),
                flags,
            ),
        )
    db.commit()
    db.close()
    incremental, direct = tmp_path / "incremental.sqlite3", tmp_path / "direct.sqlite3"
    build_cache(incremental, root, "twse", days[0], days[1])
    prefix = _prefix(incremental, days[1])
    update_cache(incremental, root, "twse", days[-1])
    assert _prefix(incremental, days[1]) == prefix
    build_cache(direct, root, "twse", days[0], days[-1])
    assert _content(incremental) == _content(direct)
    _assert_nontrivial(incremental)
