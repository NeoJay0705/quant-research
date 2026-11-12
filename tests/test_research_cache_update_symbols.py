from __future__ import annotations

import importlib.util
import json
import sqlite3
from pathlib import Path

import pytest

from quant_research.research import build_cache, list_symbols, update_cache


def _fixture():
    path = Path(__file__).with_name("test_taiwan_dataset_pipeline_sequence.py")
    spec = importlib.util.spec_from_file_location("cache_update_symbols_fixture", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _source(
    tmp_path: Path, *, events: tuple[tuple[str, int, int], ...] = ()
) -> tuple[Path, tuple[str, ...]]:
    days = tuple(f"2024-01-{n:02d}" for n in range(1, 5))
    root = tmp_path / "source"
    _fixture()._install(root, days, (100, 100, 100, 100), events)
    return root, days


def _status(cache: Path, symbol: str = "2330"):
    return {item.symbol: item for item in list_symbols(cache, "twse")}[symbol]


def _rows(
    cache: Path, symbol: str = "2330"
) -> tuple[list[tuple[object, ...]], list[tuple[object, ...]]]:
    with sqlite3.connect(cache) as db:
        bars = db.execute("SELECT * FROM bars WHERE symbol=? ORDER BY date", (symbol,)).fetchall()
        events = db.execute(
            "SELECT * FROM events WHERE symbol=? ORDER BY effective_date,event_type", (symbol,)
        ).fetchall()
    return bars, events


def _add_symbol(
    root: Path, symbol: str, days: tuple[str, ...], *, etf_after: str | None = None
) -> None:
    with sqlite3.connect(root / "state.sqlite3") as db:
        rows = db.execute(
            "SELECT r.snapshot_id,r.record_date,r.record_key,r.payload,r.quality_flags "
            "FROM records r JOIN snapshots s ON s.id=r.snapshot_id "
            "WHERE s.dataset='MI_INDEX' ORDER BY r.record_date"
        ).fetchall()
        for snapshot, day, key, raw, flags in rows:
            payload = json.loads(raw)
            payload["symbol"] = symbol
            payload["name"] = "ETF" if etf_after is not None and day >= etf_after else "普通股"
            db.execute(
                "INSERT INTO records(snapshot_id,kind,symbol,record_date,record_key,payload,"
                "quality_flags) "
                "VALUES(?,?,?,?,?,?,?)",
                (
                    snapshot,
                    "quote",
                    symbol,
                    day,
                    f"{key}-{symbol}",
                    json.dumps(payload, ensure_ascii=False),
                    flags,
                ),
            )
        db.commit()


def _dump(cache: Path) -> tuple[tuple[str, tuple[tuple[object, ...], ...]], ...]:
    with sqlite3.connect(cache) as db:
        tables = [
            row[0]
            for row in db.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
        ]
        return tuple(
            (table, tuple(db.execute(f'SELECT * FROM "{table}" ORDER BY rowid')))
            for table in tables
        )


def test_rdc08_old_event_added_after_build_is_not_read_or_applied(tmp_path: Path) -> None:
    root, days = _source(tmp_path)
    cache, direct = tmp_path / "incremental.sqlite3", tmp_path / "direct.sqlite3"
    build_cache(cache, root, "twse", days[0], days[1])
    helper = _fixture()
    with sqlite3.connect(root / "state.sqlite3") as db:
        helper._store_parsed(
            db,
            source="twse",
            dataset="TWTB8U",
            request={"start_date": days[1], "end_date": days[1]},
            index=20,
            payload={
                "stat": "OK",
                "fields": [
                    "恢復買賣日期",
                    "股票代號",
                    "停止買賣前收盤價格",
                    "恢復買賣參考價",
                    "詳細資料",
                ],
                "data": [
                    [
                        days[1],
                        "2330",
                        "100",
                        "50",
                        f"2330,{days[1].replace('-', '')},{days[1].replace('-', '')}",
                    ]
                ],
                "strDate": days[1],
                "endDate": days[1],
            },
        )
        db.commit()
    update_cache(cache, root, "twse", days[-1])
    build_cache(direct, root, "twse", days[0], days[-1])
    incremental_bars, incremental_events = _rows(cache)
    fresh_bars, fresh_events = _rows(direct)
    assert incremental_events == []
    assert len(fresh_events) == 1
    assert [row[12] for row in incremental_bars] != [row[12] for row in fresh_bars]
    # Documented RDC-08 limitation: late-arriving pre-T events are outside the update read range.


def test_update_preserves_previously_unavailable_symbol(tmp_path: Path) -> None:
    root, days = _source(tmp_path)
    _add_symbol(root, "2887", days, etf_after=days[0])
    cache = tmp_path / "cache.sqlite3"
    build_cache(cache, root, "twse", days[0], days[1])
    before = _status(cache, "2887")
    assert before.status == "unavailable"
    with sqlite3.connect(root / "state.sqlite3") as db:
        db.execute("UPDATE records SET payload=replace(payload,'ETF','普通股') WHERE symbol='2887'")
        db.commit()
    update_cache(cache, root, "twse", days[-1])
    assert _status(cache, "2887") == before
    assert _rows(cache, "2887") == ([], [])


def test_new_mixed_classification_deletes_symbol_rows_and_matches_build(tmp_path: Path) -> None:
    root, days = _source(tmp_path)
    _add_symbol(root, "2887", days, etf_after=days[2])
    incremental, direct = tmp_path / "incremental.sqlite3", tmp_path / "direct.sqlite3"
    build_cache(incremental, root, "twse", days[0], days[1])
    update_cache(incremental, root, "twse", days[-1])
    build_cache(direct, root, "twse", days[0], days[-1])
    status = _status(incremental, "2887")
    assert (status.status, status.reason) == (
        "unavailable",
        "classification:mixed:ordinary_rule_limited,unknown",
    )
    assert _rows(incremental, "2887") == ([], [])
    assert _status(incremental, "2330") == _status(direct, "2330")
    assert _status(incremental, "2887") == _status(direct, "2887")
    assert _rows(incremental, "2330") == _rows(direct, "2330")


def test_update_validation_failure_marks_only_symbol_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import quant_research.research.cache as cache_module

    root, days = _source(tmp_path)
    _add_symbol(root, "2888", days)
    cache, direct = tmp_path / "incremental.sqlite3", tmp_path / "direct.sqlite3"
    build_cache(cache, root, "twse", days[0], days[1])
    original = cache_module.validate_bar
    updating = True

    def fail_new_symbol(bar):
        if updating and bar.symbol == "2888":
            raise ValueError("update conversion injected")
        original(bar)

    monkeypatch.setattr(cache_module, "validate_bar", fail_new_symbol)
    update_cache(cache, root, "twse", days[-1])
    updating = False
    build_cache(direct, root, "twse", days[0], days[-1])
    assert _status(cache, "2888").status == "unavailable"
    assert (_status(cache, "2888").reason or "").startswith("ValueError:")
    assert _rows(cache, "2888") == ([], [])
    assert _status(cache, "2330") == _status(direct, "2330")


def test_update_storage_error_rolls_back_complete_logical_dump(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import quant_research.research.cache as cache_module

    root, days = _source(tmp_path)
    cache = tmp_path / "cache.sqlite3"
    build_cache(cache, root, "twse", days[0], days[1])
    before = _dump(cache)
    original = cache_module._insert_bar_rows

    def fail_insert(db, rows):
        if rows and rows[0][1] == "2330":
            raise sqlite3.OperationalError("injected storage failure")
        original(db, rows)

    monkeypatch.setattr(cache_module, "_insert_bar_rows", fail_insert)
    with pytest.raises(sqlite3.OperationalError, match="injected storage failure"):
        update_cache(cache, root, "twse", days[-1])
    assert _dump(cache) == before


def test_quiet_symbol_event_conversion_failure_becomes_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import quant_research.research.cache as cache_module

    root, days = _source(tmp_path, events=(("2024-01-03", 100, 50),))
    cache = tmp_path / "cache.sqlite3"
    build_cache(cache, root, "twse", days[0], days[1])
    original = cache_module._build_actions

    def fail_actions(market, symbol, *args, **kwargs):
        if symbol == "2330":
            raise ValueError("event conversion injected")
        return original(market, symbol, *args, **kwargs)

    monkeypatch.setattr(cache_module, "_build_actions", fail_actions)
    update_cache(cache, root, "twse", days[-1])
    status = _status(cache)
    assert status.status == "unavailable"
    assert (status.reason or "").startswith("ValueError:")
    assert _rows(cache) == ([], [])
    assert days[2] == "2024-01-03"
