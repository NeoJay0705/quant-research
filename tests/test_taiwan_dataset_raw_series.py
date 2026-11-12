from __future__ import annotations

import importlib.util
import json
import sqlite3
from pathlib import Path

import pytest

from quant_research.taiwan.dataset import build
from quant_research.taiwan.dataset.build import (
    _iter_raw_series,
    _iter_series_from_reader,
)
from quant_research.taiwan.dataset.classification import EvidenceSet, is_four_digit_code
from quant_research.taiwan.dataset.models import BuildSpec, DatasetError
from quant_research.taiwan.dataset.source import SourceReader


def _fixture():
    path = Path(__file__).with_name("test_taiwan_dataset_pipeline_sequence.py")
    spec = importlib.util.spec_from_file_location("raw_sequence_fixture", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _install(root: Path) -> BuildSpec:
    days = ("2024-01-01", "2024-01-02", "2024-01-03")
    _fixture()._install(root, days, (100, 102, 51), ((days[1], 100, 50),))
    with sqlite3.connect(root / "state.sqlite3") as db:
        rows = db.execute(
            "SELECT snapshot_id,record_date,payload FROM records "
            "WHERE symbol='2330' AND kind='quote'"
        ).fetchall()
        for snapshot, day, raw in rows:
            payload = json.loads(raw)
            payload.update(
                symbol="00999",
                name="test fund",
                original={"證券代號": "00999", "證券名稱": "test fund"},
            )
            db.execute(
                "INSERT INTO records(snapshot_id,kind,symbol,record_date,record_key,payload,"
                "quality_flags) "
                "VALUES(?,?,?,?,?,?,?)",
                (snapshot, "quote", "00999", day, "nonordinary", json.dumps(payload), "[]"),
            )
        db.commit()
    return BuildSpec(root, days[0], days[-1], sources=("twse",))


def test_raw_series_skip_reports_once_without_normalizing_skipped_symbols(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = _install(tmp_path / "source")
    original = build.normalize_quote
    normalized: list[str] = []
    skips: list[tuple[str, str, str]] = []

    def spy(scope, record, evidence):
        normalized.append(record.payload["symbol"])
        return original(scope, record, evidence)

    monkeypatch.setattr(build, "normalize_quote", spy)
    with SourceReader(spec) as reader:
        series = list(
            _iter_raw_series(
                reader,
                EvidenceSet.from_manifest(None),
                skip_symbol=lambda symbol: "test skip" if symbol == "2330" else None,
                on_skip=lambda *values: skips.append(values),
            )
        )
    assert skips == [("twse", "2330", "test skip")]
    assert "2330" not in normalized
    assert [row.symbol for row in series] == ["00999"]
    clean_spec = _install(tmp_path / "clean")
    with SourceReader(clean_spec) as reader:
        clean = [
            row
            for row in _iter_raw_series(reader, EvidenceSet.from_manifest(None))
            if row.symbol == "00999"
        ]
    assert series == clean


def test_raw_series_symbol_error_discards_prior_rows_and_callback_is_optional(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = _install(tmp_path / "source")
    with sqlite3.connect(spec.root / "state.sqlite3") as db:
        row = db.execute(
            "SELECT id,payload FROM records WHERE record_date='2024-01-02' AND symbol='2330'"
        ).fetchone()
        assert row
        import json

        payload = json.loads(row[1])
        payload["close"] = "corrupt"
        db.execute("UPDATE records SET payload=? WHERE id=?", (json.dumps(payload), row[0]))
        db.commit()
    reported: list[tuple[str, str, Exception]] = []
    with SourceReader(spec) as reader:
        values = list(
            _iter_raw_series(
                reader,
                EvidenceSet.from_manifest(None),
                on_symbol_error=lambda *args: reported.append(args),
            )
        )
    assert len(reported) == 1 and reported[0][:2] == ("twse", "2330")
    assert all(item.symbol != "2330" for item in values)
    # The independent symbol is not affected by the failed symbol's earlier rows.
    assert len(values) == 1 and values[0].symbol == "00999"
    clean_spec = _install(tmp_path / "clean")
    with SourceReader(clean_spec) as reader:
        clean = [
            row
            for row in _iter_raw_series(reader, EvidenceSet.from_manifest(None))
            if row.symbol == "00999"
        ]
    assert values == clean
    with SourceReader(spec) as reader:
        with pytest.raises(DatasetError):
            list(_iter_raw_series(reader, EvidenceSet.from_manifest(None)))


def test_series_matches_manual_raw_event_and_adjustment_pipeline(tmp_path: Path) -> None:
    from quant_research.taiwan.dataset.events import load_events
    from quant_research.taiwan.dataset.factors import calculate_adjustments

    spec = _install(tmp_path / "source")
    evidence = EvidenceSet.from_manifest(None)
    with SourceReader(spec) as reader:
        actual = list(_iter_series_from_reader(reader, evidence))
    with SourceReader(spec) as reader:
        raw = list(_iter_raw_series(reader, evidence))
        manual = []
        for item in raw:
            events = load_events(reader, item.symbol, source=item.source)
            ordinary = all(
                cls.classification in {"ordinary_evidence_supported", "ordinary_rule_limited"}
                for cls in item.classifications
            )
            adjusted = calculate_adjustments(item.quotes, events) if ordinary else ()
            manual.append(
                build.QuoteSeries(
                    item.source, item.symbol, item.quotes, adjusted, item.classifications, events
                )
            )
    assert actual == manual
    assert {item.symbol for item in actual} == {"00999", "2330"}
    assert any(item.events for item in actual)


@pytest.mark.parametrize(
    "symbol", ["2330", "0050", "123", "12345", "2887F", "２３３０", "2330\n", ""]
)
def test_four_digit_predicate_matches_invalid_shape_reason(symbol: str) -> None:
    evidence = EvidenceSet.from_manifest(None)
    result = evidence.classify(symbol)
    assert is_four_digit_code(symbol) is not (
        result.classification == "unknown" and result.reason == "invalid_symbol_shape"
    )


def test_four_digit_predicate_matches_invalid_shape_with_evidence_rows() -> None:
    evidence = EvidenceSet(
        [{"symbol": "2330", "evidence_id": "fixture", "signal": "ordinary", "name": ""}],
        frozenset(),
        {},
    )
    for symbol in ("2330", "0050", "123", "12345", "2887F", "２３３０", "2330\n", ""):
        result = evidence.classify(symbol)
        assert is_four_digit_code(symbol) is not (
            result.classification == "unknown" and result.reason == "invalid_symbol_shape"
        )
