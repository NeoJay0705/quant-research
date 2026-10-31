from __future__ import annotations

import importlib.util
import sqlite3
from decimal import Decimal
from pathlib import Path

from quant_research.taiwan.dataset import build
from quant_research.taiwan.dataset.build import _iter_series_from_reader, iter_series
from quant_research.taiwan.dataset.classification import EvidenceSet
from quant_research.taiwan.dataset.models import BuildSpec
from quant_research.taiwan.dataset.source import SourceReader
from quant_research.taiwan.storage import SCHEMA


def _fixture_helper(filename: str):
    path = Path(__file__).with_name(filename)
    module_spec = importlib.util.spec_from_file_location(path.stem, path)
    assert module_spec is not None and module_spec.loader is not None
    module = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(module)
    return module


def _spec(root: Path) -> BuildSpec:
    return BuildSpec(root, "2024-01-01", "2024-01-05", sources=("twse",))


def test_shared_reader_matches_wrapper_and_preserves_reader_and_first_day_event(
    tmp_path: Path,
) -> None:
    root = tmp_path / "source"
    days = tuple(f"2024-01-0{day}" for day in range(1, 6))
    _fixture_helper("test_taiwan_dataset_pipeline_sequence.py")._install(
        root,
        days,
        (100, 102, 51, 53, 106),
        ((days[0], 200, 100), (days[2], 100, 50), (days[4], 100, 200)),
    )
    spec = _spec(root)
    expected = list(iter_series(spec))
    reader = SourceReader(spec)
    with reader:
        actual = list(_iter_series_from_reader(reader, EvidenceSet.from_manifest(None)))
        assert actual == expected
        (series,) = actual
        # Independent hand-calculated oracle: the Jan 1 event establishes the
        # starting scale only; Jan 3 doubles and Jan 5 halves that scale.
        assert [quote.factor for quote in series.quotes] == list(
            map(Decimal, ("1", "1", "2", "2", "1"))
        )
        assert [quote.close for quote in series.quotes] == list(
            map(Decimal, ("100", "102", "102", "106", "106"))
        )
        assert reader.scopes()
        assert list(_iter_series_from_reader(reader, EvidenceSet.from_manifest(None))) == expected
    assert reader._db is None


def test_partial_generator_close_cleans_temporary_workspace_and_keeps_reader_open(
    tmp_path: Path, monkeypatch
) -> None:
    root = tmp_path / "source"
    days = ("2024-02-01", "2024-02-02")
    # Two quote symbols ensure the helper yields incrementally.
    _store_parsed = _fixture_helper("test_taiwan_dataset_pipeline_sequence.py")._store_parsed
    root.mkdir()
    db = sqlite3.connect(root / "state.sqlite3")
    db.executescript(SCHEMA)
    for index, day in enumerate(days):
        payload = {
            "stat": "OK",
            "date": day.replace("-", ""),
            "tables": [
                {
                    "title": "每日收盤行情",
                    "fields": [
                        "證券代號",
                        "證券名稱",
                        "開盤價",
                        "最高價",
                        "最低價",
                        "收盤價",
                        "成交股數",
                        "成交金額",
                    ],
                    "data": [
                        [symbol, "測試", "10", "10", "10", "10", "1", "10"]
                        for symbol in ("2330", "00999")
                    ],
                }
            ],
        }
        _store_parsed(
            db,
            source="twse",
            dataset="MI_INDEX",
            request={"date": day},
            payload=payload,
            index=index,
        )
    db.commit()
    db.close()

    temporary_paths: list[Path] = []
    original = build.tempfile.TemporaryDirectory

    def tracked_temporary_directory(*args, **kwargs):
        directory = original(*args, **kwargs)
        temporary_paths.append(Path(directory.name))
        return directory

    monkeypatch.setattr(build.tempfile, "TemporaryDirectory", tracked_temporary_directory)
    reader = SourceReader(BuildSpec(root, days[0], days[-1], sources=("twse",)))
    with reader:
        generator = _iter_series_from_reader(reader, EvidenceSet.from_manifest(None))
        assert next(generator).symbol == "00999"
        assert temporary_paths and temporary_paths[0].exists()
        generator.close()
        assert not temporary_paths[0].exists()
        assert reader.scopes()
        assert list(reader.iter_all_records(reader.scopes()[0]))
    assert reader._db is None


def test_shared_reader_uses_its_pinned_wal_snapshot_without_fallback(tmp_path: Path) -> None:
    root = tmp_path / "source"
    days = ("2024-03-01",)
    _fixture_helper("test_taiwan_dataset_pipeline_sequence.py")._install(root, days, (100,), ())
    spec = BuildSpec(root, days[0], days[0], sources=("twse",))
    with SourceReader(spec) as reader:
        old_scope = reader.scopes()[0]
        writer = sqlite3.connect(root / "state.sqlite3")
        writer.execute(
            "INSERT INTO snapshots VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "new-snapshot",
                "twse",
                "MI_INDEX",
                old_scope.actual_params,
                "url",
                "scope",
                "2024-03-02",
                200,
                "hash",
                "raw",
                "valid",
                None,
            ),
        )
        writer.execute(
            "INSERT INTO tasks(id,logical_key,edition,source,dataset,params,kind,state,"
            "snapshot_id) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (
                "new-task",
                old_scope.logical_key,
                old_scope.edition + 1,
                "twse",
                "MI_INDEX",
                old_scope.actual_params,
                "official_quote",
                "completed",
                "new-snapshot",
            ),
        )
        writer.execute(
            "INSERT INTO records(snapshot_id,kind,symbol,record_date,record_key,payload,"
            "quality_flags) "
            "SELECT 'new-snapshot',kind,symbol,record_date,record_key,payload,quality_flags "
            "FROM records WHERE snapshot_id=?",
            (old_scope.snapshot_id,),
        )
        writer.commit()
        writer.close()

        (old_series,) = list(_iter_series_from_reader(reader, EvidenceSet.from_manifest(None)))
        assert old_series.raw_quotes[0].close == Decimal("100")
        assert reader.scopes()[0].task_id == old_scope.task_id

    (new_series,) = list(iter_series(spec))
    assert new_series.raw_quotes[0].close == Decimal("100")
    assert new_series.raw_quotes[0].refs[0].startswith("MI_INDEX:new-snapshot:")
