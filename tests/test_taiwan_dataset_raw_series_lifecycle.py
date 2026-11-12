from __future__ import annotations

import importlib.util
import sqlite3
from pathlib import Path

import pytest

from quant_research.taiwan.dataset import build
from quant_research.taiwan.dataset.build import _iter_raw_series, _iter_series_from_reader
from quant_research.taiwan.dataset.classification import EvidenceSet
from quant_research.taiwan.dataset.models import BuildSpec, DatasetError
from quant_research.taiwan.dataset.source import SourceReader


def _spec(root: Path) -> BuildSpec:
    path = Path(__file__).with_name("test_taiwan_dataset_pipeline_sequence.py")
    spec = importlib.util.spec_from_file_location("lifecycle_sequence_fixture", path)
    assert spec and spec.loader
    fixture = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fixture)
    days = ("2024-02-01", "2024-02-02")
    fixture._install(root, days, (10, 11), ())
    with sqlite3.connect(root / "state.sqlite3") as db:
        db.execute("UPDATE records SET quality_flags='[\"fixture-flag\"]'")
        db.commit()
    return BuildSpec(root, days[0], days[-1], sources=("twse",))


def test_raw_series_quality_flags_are_source_record_flags(tmp_path: Path) -> None:
    spec = _spec(tmp_path / "source")
    with SourceReader(spec) as reader:
        rows = list(_iter_raw_series(reader, EvidenceSet.from_manifest(None)))
    assert rows and all(flags == ("fixture-flag",) for row in rows for flags in row.quality_flags)


@pytest.mark.parametrize("pipeline", ["raw", "series"])
@pytest.mark.parametrize("when", ["normal", "exception", "early"])
def test_pipeline_work_directory_is_removed_deterministically(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pipeline: str, when: str
) -> None:
    spec = _spec(tmp_path / "source")
    directories: list[Path] = []
    original = build.tempfile.TemporaryDirectory

    def tracked(*args, **kwargs):
        temporary = original(*args, **kwargs)
        directories.append(Path(temporary.name))
        return temporary

    monkeypatch.setattr(build.tempfile, "TemporaryDirectory", tracked)
    if when == "exception":

        def fail(*args, **kwargs):
            raise RuntimeError("injected event loader failure")

        monkeypatch.setattr(build, "load_events", fail)
    with SourceReader(spec) as reader:
        generator = (
            _iter_raw_series(reader, EvidenceSet.from_manifest(None))
            if pipeline == "raw"
            else _iter_series_from_reader(reader, EvidenceSet.from_manifest(None))
        )
        if when == "normal":
            list(generator)
        elif when == "exception":
            if pipeline == "raw":
                monkeypatch.setattr(
                    build,
                    "normalize_quote",
                    lambda *args: (_ for _ in ()).throw(DatasetError("injected")),
                )
                with pytest.raises(DatasetError, match="injected"):
                    list(generator)
            else:
                with pytest.raises(DatasetError, match="injected event loader failure"):
                    list(generator)
        else:
            next(generator)
            assert directories and directories[0].exists()
            generator.close()
        assert directories and all(not directory.exists() for directory in directories)
