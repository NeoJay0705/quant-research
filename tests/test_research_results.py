import hashlib
import json
import os
import stat
from dataclasses import replace
from decimal import Decimal as D
from pathlib import Path

import pytest

from quant_research.research import results
from quant_research.research.backtest import CostModel, MarketOrder, RunResult, backtest
from quant_research.research.data import ResearchData
from quant_research.research.models import Bar
from quant_research.research.results import ResearchSaveError, save_run


def _run(*, protected: tuple[str, ...] = (), program_path: str = __file__) -> RunResult:
    days = ("2024-01-01", "2024-01-02", "2024-01-03")
    data = ResearchData(
        "m",
        ("A",),
        days[0],
        days[-1],
        days,
        tuple(
            Bar(
                "m",
                "A",
                day,
                open=D(price),
                close=D(price),
                classification="ordinary_evidence_supported",
            )
            for day, price in zip(days, ("10", "10", "12"), strict=True)
        ),
    )

    def strategy(view, marker=None):
        if view.date == days[0]:
            return [MarketOrder("A", "buy", D(10))]
        if view.date == days[1]:
            return [MarketOrder("A", "sell", D(10))]
        return []

    result = backtest(
        data,
        strategy,
        initial_cash=D(1000),
        costs=CostModel(D(".001"), D(0), D(".003"), D(0)),
        start_date=days[0],
        end_date=days[-1],
        program_path=program_path,
        params={"marker": "captured"},
    )
    return replace(result, protected_input_dirs=protected)


def test_save_real_run_roundtrip_and_hashes(tmp_path):
    result = _run()
    destination = save_run(result, tmp_path / "saved")
    summary = json.loads((destination / "summary.json").read_text())
    assert D(summary["final_nav"]) == D("1019.42")
    assert D(summary["total_return"]) == D("0.01942")
    assert D(summary["total_cost"]) == D("0.58")
    metadata = json.loads((destination / "metadata.json").read_text())
    assert metadata["strategy_sha256"] == result.strategy_sha256
    assert metadata["program_path"] == result.program_path
    assert metadata["costs"]["fee_rate"] == "0.001"
    assert metadata["params"] == [["marker", "captured"]]
    assert "snapshots" not in metadata
    rows = [json.loads(line) for line in (destination / "snapshots.jsonl").read_text().splitlines()]
    assert D(rows[-1]["nav"]) == D("1019.42")
    assert "positions" in rows[-1] and "valuations" in rows[-1]
    complete = json.loads((destination / "COMPLETE.json").read_text())
    assert complete["status"] == "COMPLETE"
    for name, digest in complete["files"].items():
        assert hashlib.sha256((destination / name).read_bytes()).hexdigest() == digest
    assert not list(destination.glob(".COMPLETE.json.*.pending"))


def test_new_results_have_private_modes_under_caller_umasks(tmp_path, monkeypatch):
    if os.name != "posix":
        pytest.skip("POSIX permission modes are required")
    original_umask = os.umask
    initial_umask = original_umask(0)
    original_umask(initial_umask)
    parent = tmp_path / "existing-parent"
    parent.mkdir(mode=0o755)
    sentinel = parent / "sentinel.txt"
    sentinel.write_bytes(b"unchanged")
    os.chmod(parent, 0o755)
    os.chmod(sentinel, 0o640)
    expected_existing = (stat.S_IMODE(parent.stat().st_mode), stat.S_IMODE(sentinel.stat().st_mode))
    for mask in (0o000, 0o022, 0o077):
        original_umask(mask)
        target = parent / f"saved-{mask:03o}"
        calls = []
        monkeypatch.setattr(results.os, "umask", lambda value, calls=calls: calls.append(value))
        try:
            destination = save_run(_run(), target)
            assert calls == []
            assert stat.S_IMODE(destination.stat().st_mode) == 0o700
            modes = {path.name: stat.S_IMODE(path.stat().st_mode) for path in destination.iterdir()}
            assert modes
            assert all(mode == 0o600 for mode in modes.values()), modes
            assert "COMPLETE.json" in modes
            actual_existing = (
                stat.S_IMODE(parent.stat().st_mode),
                stat.S_IMODE(sentinel.stat().st_mode),
            )
            assert actual_existing == expected_existing
            assert sentinel.read_bytes() == b"unchanged"
        finally:
            monkeypatch.setattr(results.os, "umask", original_umask)
            original_umask(initial_umask)


def test_cleanup_failure_after_publication_keeps_save_committed(tmp_path, monkeypatch):
    target = tmp_path / "cleanup-failure"
    original_link = results.os.link
    original_unlink = Path.unlink

    def link_then_fail_cleanup(source, destination):
        original_link(source, destination)

    def failing_unlink(path, *args, **kwargs):
        if path.name.startswith(".COMPLETE.json.") and path.name.endswith(".pending"):
            raise OSError("injected cleanup failure")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(results.os, "link", link_then_fail_cleanup)
    monkeypatch.setattr(Path, "unlink", failing_unlink)

    destination = save_run(_run(), target)
    complete = json.loads((destination / "COMPLETE.json").read_text())
    assert complete["status"] == "COMPLETE"
    for name, digest in complete["files"].items():
        assert hashlib.sha256((destination / name).read_bytes()).hexdigest() == digest
    pending = list(destination.glob(".COMPLETE.json.*.pending"))
    assert len(pending) == 1
    if os.name == "posix":
        assert stat.S_IMODE(pending[0].stat().st_mode) == 0o600


def test_reject_existing_protected_default_and_symlink_targets(tmp_path, monkeypatch):
    existing = tmp_path / "existing"
    existing.mkdir(mode=0o755)
    sentinel = existing / "keep.txt"
    sentinel.write_bytes(b"unchanged")
    if os.name == "posix":
        os.chmod(existing, 0o755)
        os.chmod(sentinel, 0o640)
    existing_modes = (
        stat.S_IMODE(existing.stat().st_mode),
        stat.S_IMODE(sentinel.stat().st_mode),
    )
    for target, run in (
        (existing, _run()),
        (tmp_path / "protected" / "new", _run(protected=(str(tmp_path / "protected"),))),
    ):
        with pytest.raises(ResearchSaveError):
            save_run(run, target)
    production = Path.home() / ".local/share/quant-research/taiwan" / "unsafe-test-target"
    with pytest.raises(ResearchSaveError):
        save_run(_run(), production)
    parent = tmp_path / "real-parent"
    parent.mkdir()
    symlink = tmp_path / "link"
    symlink.symlink_to(parent, target_is_directory=True)
    with pytest.raises(ResearchSaveError):
        save_run(_run(), symlink / "new")
    assert not (tmp_path / "protected" / "new").exists()
    assert not (parent / "new").exists()
    assert sentinel.read_bytes() == b"unchanged"
    if os.name == "posix":
        actual_modes = (
            stat.S_IMODE(existing.stat().st_mode),
            stat.S_IMODE(sentinel.stat().st_mode),
        )
        assert actual_modes == existing_modes == (0o755, 0o640)


def test_write_failure_leaves_partial_without_complete_and_input_intact(tmp_path, monkeypatch):
    protected = tmp_path / "input"
    protected.mkdir()
    sentinel = protected / "keep.txt"
    sentinel.write_text("unchanged")
    result = _run(protected=(str(protected),))
    target = tmp_path / "partial"
    original_fdopen = results.os.fdopen
    fill_content = results._jsonl(result.fills)

    class PartialWriteFailure:
        def __init__(self, stream):
            self.stream = stream

        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            self.stream.close()

        def write(self, content):
            if content == fill_content:
                self.stream.write(content[:8])
                self.stream.flush()
                raise OSError("injected write failure")
            return self.stream.write(content)

        def flush(self):
            self.stream.flush()

    def failing_fdopen(descriptor, mode):
        return PartialWriteFailure(original_fdopen(descriptor, mode))

    monkeypatch.setattr(results.os, "fdopen", failing_fdopen)
    with pytest.raises(ResearchSaveError):
        save_run(result, target)
    assert target.is_dir() and not (target / "COMPLETE.json").exists()
    assert sentinel.read_text() == "unchanged"
    if os.name == "posix":
        assert stat.S_IMODE(target.stat().st_mode) == 0o700
        assert all(stat.S_IMODE(path.stat().st_mode) == 0o600 for path in target.iterdir())


def test_descriptor_closes_if_stream_wrapping_fails(tmp_path, monkeypatch):
    descriptor = None

    def failing_fdopen(fd, mode):
        nonlocal descriptor
        descriptor = fd
        raise OSError("injected fd wrapping failure")

    monkeypatch.setattr(results.os, "fdopen", failing_fdopen)
    with pytest.raises(ResearchSaveError):
        save_run(_run(), tmp_path / "fdopen-failure")
    assert descriptor is not None
    with pytest.raises(OSError):
        os.fstat(descriptor)


def test_marker_write_failure_never_publishes_partial_complete(tmp_path, monkeypatch):
    protected = tmp_path / "input"
    protected.mkdir()
    sentinel = protected / "keep.txt"
    sentinel.write_text("unchanged")
    target = tmp_path / "partial-marker"
    original_fdopen = results.os.fdopen

    class PartialWriteFailure:
        def __init__(self, stream):
            self.stream = stream

        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            self.stream.close()

        def write(self, content):
            self.stream.write(content[:8])
            self.stream.flush()
            raise OSError("injected marker write failure")

        def flush(self):
            self.stream.flush()

    wrapped = 0

    def failing_fdopen(descriptor, mode):
        nonlocal wrapped
        wrapped += 1
        stream = original_fdopen(descriptor, mode)
        return PartialWriteFailure(stream) if wrapped == 9 else stream

    monkeypatch.setattr(results.os, "fdopen", failing_fdopen)
    with pytest.raises(ResearchSaveError):
        save_run(_run(protected=(str(protected),)), target)

    assert target.is_dir()
    assert (target / "fills.jsonl").is_file()
    assert not (target / "COMPLETE.json").exists()
    assert not list(target.glob(".COMPLETE.json.*.pending"))
    assert sentinel.read_text() == "unchanged"


def test_capture_hash_without_rereading_program_and_reject_nonfinite(tmp_path):
    program = tmp_path / "strategy.py"
    program.write_text("original strategy")
    result = _run(program_path=str(program))
    recorded = result.strategy_sha256
    program.write_text("modified after backtest")
    destination = save_run(result, tmp_path / "captured")
    metadata = json.loads((destination / "metadata.json").read_text())
    assert metadata["strategy_sha256"] == recorded
    bad = replace(result, initial_cash=D("NaN"))
    with pytest.raises(ResearchSaveError):
        save_run(bad, tmp_path / "malformed")
    assert not (tmp_path / "malformed" / "COMPLETE.json").exists()
