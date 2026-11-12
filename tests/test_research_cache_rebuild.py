from __future__ import annotations

import importlib.util
import json
import sqlite3
from pathlib import Path

import pytest

from quant_research.research import build_cache, rebuild_cache


def _update_helpers():
    path = Path(__file__).with_name("test_research_cache_update.py")
    spec = importlib.util.spec_from_file_location("cache_rebuild_shared", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _fixture():
    path = Path(__file__).with_name("test_taiwan_dataset_pipeline_sequence.py")
    spec = importlib.util.spec_from_file_location("cache_rebuild_sequence_fixture", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _revise_source(root: Path, day: str) -> None:
    db = sqlite3.connect(root / "state.sqlite3")
    try:
        for snapshot, record_id, payload in db.execute(
            "SELECT snapshot_id,id,payload FROM records WHERE record_date=?", (day,)
        ).fetchall():
            value = json.loads(payload)
            if value["kind"] == "quote":
                value["close"] = "777"
                value["open"] = value["high"] = value["low"] = "777"
            elif value["kind"] == "event":
                value["before_price"] = "12"
            db.execute(
                "UPDATE records SET payload=? WHERE snapshot_id=? AND id=?",
                (json.dumps(value, ensure_ascii=False), snapshot, record_id),
            )
        db.commit()
    finally:
        db.close()


def _add_capital_reduction(root: Path, day: str) -> None:
    from tests.test_taiwan_dataset_events import add_twse_parent, twse_summary

    compact = day.replace("-", "")
    summary = twse_summary(
        "TWTAUU",
        ["恢復買賣日期", "股票代號", "停止買賣前收盤價格", "恢復買賣參考價", "詳細資料"],
        [day, "2330", "10", "9", f"2330,{compact}"],
    )
    summary["strDate"] = summary["endDate"] = day
    add_twse_parent(
        root,
        dataset="TWTAUU",
        summary=summary,
        logical=f"capital-{day}",
        edition=1,
        fetched="2025-06-20",
        request={"start_date": day, "end_date": day},
        detail={
            "stat": "OK",
            "fields": ["股票代號", "每壹仟股換發新股票"],
            "data": [["2330", "800 股"]],
        },
    )


def test_market_wide_rebuild_from_coverage_start_matches_fresh_build(tmp_path: Path) -> None:
    days = tuple(f"2024-08-{n:02d}" for n in range(1, 5))
    root = tmp_path / "source"
    _fixture()._install(root, days, (100, 102, 104, 106), ((days[2], 10, 3),))
    rebuilt, fresh = tmp_path / "rebuilt.sqlite3", tmp_path / "fresh.sqlite3"
    build_cache(rebuilt, root, "twse", days[0], days[-1])
    before = _update_helpers()._content(rebuilt)
    _revise_source(root, days[2])
    result = rebuild_cache(rebuilt, root, "twse", days[0])
    build_cache(fresh, root, "twse", days[0], days[-1])
    assert result.coverage_start == days[0]
    assert result.built_through == days[-1]
    assert _update_helpers()._content(rebuilt) == _update_helpers()._content(fresh)
    assert before != _update_helpers()._content(rebuilt)


def test_market_wide_mid_rebuild_matches_fresh_and_preserves_prefix(tmp_path: Path) -> None:
    days = tuple(f"2024-09-{n:02d}" for n in range(1, 6))
    root = tmp_path / "source"
    _fixture()._install(root, days, (100, 102, 104, 106, 108), ((days[2], 10, 3),))
    rebuilt, fresh = tmp_path / "rebuilt.sqlite3", tmp_path / "fresh.sqlite3"
    build_cache(rebuilt, root, "twse", days[0], days[-1])
    before = _update_helpers()._prefix(rebuilt, days[1])
    rebuild_cache(rebuilt, root, "twse", days[2])
    build_cache(fresh, root, "twse", days[0], days[-1])
    assert _update_helpers()._prefix(rebuilt, days[1]) == before
    assert _update_helpers()._content(rebuilt) == _update_helpers()._content(fresh)


def test_targeted_mid_rebuild_preserves_other_symbols_and_coverage(tmp_path: Path) -> None:
    days = tuple(f"2024-10-{n:02d}" for n in range(1, 5))
    root = tmp_path / "source"
    _fixture()._install(root, days, (100, 102, 104, 106), ())
    cache = tmp_path / "cache.sqlite3"
    build_cache(cache, root, "twse", days[0], days[-1])
    db = sqlite3.connect(cache)
    try:
        other_before = tuple(db.execute("SELECT * FROM bars WHERE symbol!='2330'"))
        market_before = tuple(db.execute("SELECT coverage_start,built_through FROM markets"))
    finally:
        db.close()
    # The fixture's market contains one symbol; targeted operation still validates API and coverage.
    summary = rebuild_cache(cache, root, "twse", days[2], symbols=("2330",))
    db = sqlite3.connect(cache)
    try:
        assert tuple(db.execute("SELECT * FROM bars WHERE symbol!='2330'")) == other_before
        assert (
            tuple(db.execute("SELECT coverage_start,built_through FROM markets")) == market_before
        )
    finally:
        db.close()
    assert summary.coverage_start == days[0] and summary.built_through == days[-1]


def test_mid_rebuild_picks_up_source_revisions_after_T(tmp_path: Path) -> None:
    days = tuple(f"2024-12-{n:02d}" for n in range(1, 7))
    root = tmp_path / "source"
    _fixture()._install(
        root,
        days,
        (100, 102, 104, 106, 108, 110),
        ((days[2], 10, 3), (days[3], 10, 3), (days[4], 10, 5)),
    )
    # Put the first event in a quote gap; its 10/3 ratio exercises recurring decimals.
    db = sqlite3.connect(root / "state.sqlite3")
    try:
        rows = db.execute(
            "SELECT snapshot_id,id,payload FROM records WHERE kind='quote' AND record_date=?",
            (days[2],),
        ).fetchall()
        for snapshot, record_id, _payload in rows:
            db.execute("DELETE FROM records WHERE snapshot_id=? AND id=?", (snapshot, record_id))
    finally:
        db.commit()
        db.close()
    cache, fresh = tmp_path / "cache.sqlite3", tmp_path / "fresh.sqlite3"
    build_cache(cache, root, "twse", days[0], days[-1])
    helpers = _update_helpers()
    prefix_before = helpers._prefix(cache, days[1])
    _revise_source(root, days[3])  # revise quote and event multiplier after T
    with sqlite3.connect(root / "state.sqlite3") as db:
        for snapshot, record_id in db.execute(
            "SELECT snapshot_id,id FROM records WHERE kind='event' AND record_date=?", (days[4],)
        ).fetchall():
            db.execute("DELETE FROM records WHERE snapshot_id=? AND id=?", (snapshot, record_id))
    rebuild_cache(cache, root, "twse", days[2])
    build_cache(fresh, root, "twse", days[0], days[-1])
    assert helpers._prefix(cache, days[1]) == prefix_before
    assert helpers._content(cache) == helpers._content(fresh)
    with sqlite3.connect(cache) as db:
        assert db.execute("SELECT 1 FROM bars WHERE factor!='1' LIMIT 1").fetchone()
        multiplier = db.execute(
            "SELECT multiplier FROM events WHERE multiplier IS NOT NULL LIMIT 1"
        ).fetchone()
        assert multiplier is not None and len(multiplier[0]) > 10


def test_previously_unavailable_symbol_is_recovered_only_from_coverage_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import quant_research.research.cache as cache_module
    from quant_research.research import CacheError

    days = tuple(f"2025-02-{n:02d}" for n in range(1, 5))
    root = tmp_path / "source"
    _fixture()._install(root, days, (100, 102, 104, 106), ())
    targeted, market_wide, fresh = (
        tmp_path / "targeted.sqlite3",
        tmp_path / "market-wide.sqlite3",
        tmp_path / "fresh.sqlite3",
    )
    original = cache_module._build_bars

    def fail_once(*args, **kwargs):
        monkeypatch.setattr(cache_module, "_build_bars", original)
        raise RuntimeError("transient fixture failure")

    monkeypatch.setattr(cache_module, "_build_bars", fail_once)
    build_cache(targeted, root, "twse", days[0], days[-1])
    monkeypatch.setattr(cache_module, "_build_bars", fail_once)
    build_cache(market_wide, root, "twse", days[0], days[-1])
    monkeypatch.setattr(cache_module, "_build_bars", original)
    before = _update_helpers()._content(targeted)
    with pytest.raises(CacheError):
        rebuild_cache(targeted, root, "twse", days[2], symbols=("2330",))
    assert _update_helpers()._content(targeted) == before
    market_before = _update_helpers()._content(market_wide)
    rebuild_cache(market_wide, root, "twse", days[2])
    assert _update_helpers()._content(market_wide) == market_before
    rebuild_cache(targeted, root, "twse", days[0], symbols=("2330",))
    build_cache(fresh, root, "twse", days[0], days[-1])
    assert _update_helpers()._content(targeted) == _update_helpers()._content(fresh)
    rebuild_cache(market_wide, root, "twse", days[0])
    assert _update_helpers()._content(market_wide) == _update_helpers()._content(fresh)


def test_targeted_rebuild_changes_only_target_symbol(tmp_path: Path) -> None:
    days = tuple(f"2025-03-{n:02d}" for n in range(1, 5))
    root = tmp_path / "source"
    _fixture()._install(root, days, (100, 102, 104, 106), ())
    with sqlite3.connect(root / "state.sqlite3") as db:
        source_rows = db.execute(
            "SELECT snapshot_id,record_date,payload,quality_flags FROM records WHERE kind='quote'"
        ).fetchall()
        for symbol in ("2331", "2332"):
            for snapshot, day, raw, flags in source_rows:
                payload = json.loads(raw)
                payload["symbol"] = symbol
                payload["name"] = f"fixture-{symbol}"
                db.execute(
                    "INSERT INTO records(snapshot_id,kind,symbol,record_date,record_key,payload,"
                    "quality_flags) VALUES(?,?,?,?,?,?,?)",
                    (snapshot, "quote", symbol, day, f"{symbol}-{day}", json.dumps(payload), flags),
                )
    cache = tmp_path / "cache.sqlite3"
    build_cache(cache, root, "twse", days[0], days[-1])
    _revise_source(root, days[2])
    with sqlite3.connect(cache) as db:
        before = {
            table: tuple(db.execute(f"SELECT * FROM {table} WHERE symbol!='2330' ORDER BY rowid"))
            for table in ("bars", "events", "symbols")
        }
        market_before = tuple(db.execute("SELECT coverage_start,built_through FROM markets"))
    rebuild_cache(cache, root, "twse", days[2], symbols=("2330",))
    with sqlite3.connect(cache) as db:
        after = {
            table: tuple(db.execute(f"SELECT * FROM {table} WHERE symbol!='2330' ORDER BY rowid"))
            for table in ("bars", "events", "symbols")
        }
        assert after == before
        assert (
            tuple(db.execute("SELECT coverage_start,built_through FROM markets")) == market_before
        )
        assert db.execute(
            "SELECT 1 FROM bars WHERE symbol='2330' AND close='777' LIMIT 1"
        ).fetchone()


def test_rebuild_from_coverage_start_removes_symbol_row_with_no_remaining_bars(
    tmp_path: Path,
) -> None:
    days = tuple(f"2025-04-{n:02d}" for n in range(1, 5))
    root = tmp_path / "source"
    _fixture()._install(root, days, (100, 102, 104, 106), ())
    with sqlite3.connect(root / "state.sqlite3") as db:
        rows = db.execute(
            "SELECT snapshot_id,record_date,payload,quality_flags FROM records WHERE kind='quote'"
        ).fetchall()
        for symbol, selected in (("2331", days), ("2332", days[2:])):
            for snapshot, day, raw, flags in rows:
                if day not in selected:
                    continue
                payload = json.loads(raw)
                payload["symbol"] = symbol
                db.execute(
                    "INSERT INTO records(snapshot_id,kind,symbol,record_date,record_key,payload,"
                    "quality_flags) VALUES(?,?,?,?,?,?,?)",
                    (snapshot, "quote", symbol, day, f"{symbol}-{day}", json.dumps(payload), flags),
                )
    full, partial, fresh = (
        tmp_path / "full.sqlite3",
        tmp_path / "partial.sqlite3",
        tmp_path / "fresh.sqlite3",
    )
    build_cache(full, root, "twse", days[0], days[-1])
    build_cache(partial, root, "twse", days[0], days[-1])
    with sqlite3.connect(root / "state.sqlite3") as db:
        db.execute("DELETE FROM records WHERE symbol='2331'")
        db.execute("DELETE FROM records WHERE symbol='2332' AND record_date>=?", (days[2],))
    rebuild_cache(full, root, "twse", days[0])
    rebuild_cache(partial, root, "twse", days[2])
    build_cache(fresh, root, "twse", days[0], days[-1])
    assert _update_helpers()._content(full) == _update_helpers()._content(fresh)
    with sqlite3.connect(full) as db:
        assert db.execute("SELECT 1 FROM symbols WHERE symbol='2331'").fetchone() is None
        assert db.execute("SELECT 1 FROM symbols WHERE symbol='2332'").fetchone() is None
    with sqlite3.connect(partial) as db:
        assert db.execute("SELECT 1 FROM symbols WHERE symbol='2332'").fetchone() is None


def test_rebuild_does_not_extend_coverage_then_update_catches_up(tmp_path: Path) -> None:
    from quant_research.research import CacheCoverageError, load_cached, update_cache

    days = tuple(f"2025-01-{n:02d}" for n in range(1, 6))
    root = tmp_path / "source"
    _fixture()._install(root, days, (100, 102, 104, 106, 108), ())
    cache, fresh = tmp_path / "cache.sqlite3", tmp_path / "fresh.sqlite3"
    build_cache(cache, root, "twse", days[0], days[2])
    rebuild_cache(cache, root, "twse", days[1])
    with pytest.raises(CacheCoverageError):
        load_cached(cache, "twse", end_date=days[-1])
    update_cache(cache, root, "twse", days[-1])
    build_cache(fresh, root, "twse", days[0], days[-1])
    assert _update_helpers()._content(cache) == _update_helpers()._content(fresh)


def _add_quote_symbols(
    root: Path, symbols: tuple[str, ...], *, etf_after: str | None = None
) -> None:
    with sqlite3.connect(root / "state.sqlite3") as db:
        rows = db.execute(
            "SELECT r.snapshot_id,r.record_date,r.record_key,r.payload,r.quality_flags "
            "FROM records r JOIN snapshots s ON s.id=r.snapshot_id "
            "WHERE s.dataset='MI_INDEX' ORDER BY r.record_date"
        ).fetchall()
        for symbol in symbols:
            for snapshot, day, key, raw, flags in rows:
                payload = json.loads(raw)
                payload["symbol"] = symbol
                payload["name"] = "ETF" if etf_after is not None and day >= etf_after else "普通股"
                db.execute(
                    "INSERT INTO records(snapshot_id,kind,symbol,record_date,record_key,payload,"
                    "quality_flags) VALUES(?,?,?,?,?,?,?)",
                    (snapshot, "quote", symbol, day, f"{key}-{symbol}", json.dumps(payload), flags),
                )
        db.commit()


def test_partial_rebuild_classification_union(tmp_path: Path) -> None:
    days = tuple(f"2025-05-{n:02d}" for n in range(1, 6))
    split = days[2]
    root = tmp_path / "source"
    _fixture()._install(root, days, (100, 102, 104, 106, 108), ())
    _add_quote_symbols(root, ("2887", "2888"))
    market_wide, targeted, fresh = (
        tmp_path / "market-wide.sqlite3",
        tmp_path / "targeted.sqlite3",
        tmp_path / "fresh.sqlite3",
    )
    build_cache(market_wide, root, "twse", days[0], days[-1])
    build_cache(targeted, root, "twse", days[0], days[-1])
    # Revise only 2887's later quotes to the ETF-like name; leave all other symbols intact.
    with sqlite3.connect(root / "state.sqlite3") as db:
        for snapshot, record_id, raw in db.execute(
            "SELECT snapshot_id,id,payload FROM records WHERE symbol='2887' AND record_date>=?",
            (split,),
        ).fetchall():
            payload = json.loads(raw)
            payload["name"] = "ETF"
            db.execute(
                "UPDATE records SET payload=? WHERE snapshot_id=? AND id=?",
                (json.dumps(payload, ensure_ascii=False), snapshot, record_id),
            )
        db.commit()

    rebuild_cache(market_wide, root, "twse", split)
    rebuild_cache(targeted, root, "twse", split, symbols=("2887",))
    build_cache(fresh, root, "twse", days[0], days[-1])
    expected_reason = "classification:mixed:ordinary_rule_limited,unknown"
    for cache in (market_wide, targeted):
        with sqlite3.connect(cache) as db:
            assert db.execute(
                "SELECT status,reason FROM symbols WHERE symbol='2887'"
            ).fetchone() == ("unavailable", expected_reason)
            assert db.execute("SELECT 1 FROM bars WHERE symbol='2887'").fetchone() is None
            assert db.execute("SELECT 1 FROM events WHERE symbol='2887'").fetchone() is None
    helpers = _update_helpers()
    assert helpers._content(market_wide) == helpers._content(fresh)
    # Targeted rebuild leaves other symbols intact and matches the same fresh content.
    assert helpers._content(targeted) == helpers._content(fresh)


def test_mid_rebuild_continues_from_nonunit_retained_factor(tmp_path: Path) -> None:
    days = tuple(f"2025-06-{n:02d}" for n in range(1, 8))
    root = tmp_path / "source"
    _fixture()._install(root, days, (100, 102, 104, 106, 108, 110, 112), ((days[1], 10, 3),))
    _add_capital_reduction(root, days[2])
    cache, fresh = tmp_path / "cache.sqlite3", tmp_path / "fresh.sqlite3"
    build_cache(cache, root, "twse", days[0], days[-1])
    helpers = _update_helpers()
    retained_before = helpers._prefix(cache, days[3])
    _revise_source(root, days[5])
    rebuild_cache(cache, root, "twse", days[4])
    build_cache(fresh, root, "twse", days[0], days[-1])
    assert helpers._prefix(cache, days[3]) == retained_before
    assert helpers._content(cache) == helpers._content(fresh)
    with sqlite3.connect(cache) as db:
        assert db.execute(
            "SELECT 1 FROM bars WHERE date<=? AND factor!='1' LIMIT 1", (days[3],)
        ).fetchone()
        assert db.execute(
            "SELECT 1 FROM bars WHERE date>? AND factor!='1' LIMIT 1", (days[4],)
        ).fetchone()


def test_mid_rebuild_applies_stored_event_in_gap_before_T(tmp_path: Path) -> None:
    days = tuple(f"2025-07-{n:02d}" for n in range(1, 8))
    root = tmp_path / "source"
    event_day = days[4]
    _fixture()._install(root, days, (100, 102, 104, 106, 108, 110, 112), ((event_day, 10, 3),))
    with sqlite3.connect(root / "state.sqlite3") as db:
        db.execute("DELETE FROM records WHERE kind='quote' AND record_date=?", (event_day,))
    cache, fresh = tmp_path / "cache.sqlite3", tmp_path / "fresh.sqlite3"
    build_cache(cache, root, "twse", days[0], days[-1])
    helpers = _update_helpers()
    rebuild_cache(cache, root, "twse", days[5])
    build_cache(fresh, root, "twse", days[0], days[-1])
    assert helpers._content(cache) == helpers._content(fresh)
    with sqlite3.connect(cache) as db:
        assert db.execute(
            "SELECT 1 FROM bars WHERE date>? AND factor!='1' LIMIT 1", (days[3],)
        ).fetchone()


def test_rebuild_rejects_start_outside_coverage_without_changes(tmp_path: Path) -> None:
    from quant_research.research import CacheError

    days = ("2024-11-01", "2024-11-02")
    root = tmp_path / "source"
    _fixture()._install(root, days, (100, 101), ())
    cache = tmp_path / "cache.sqlite3"
    build_cache(cache, root, "twse", days[0], days[-1])
    helpers = _update_helpers()
    before = helpers._content(cache)
    import pytest

    with pytest.raises(CacheError):
        rebuild_cache(cache, root, "twse", "2024-10-31")
    assert helpers._content(cache) == before
