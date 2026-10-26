from __future__ import annotations

import sys
from pathlib import Path

import pytest

from quant_research.taiwan.cli import main
from quant_research.taiwan.schedule import render_units, write_units


def test_units_content_and_idempotence(tmp_path: Path) -> None:
    content = render_units(directory=tmp_path, root=tmp_path / "data", project_dir=tmp_path)
    service = content["quant-research-taiwan.service"]
    timer = content["quant-research-taiwan.timer"]
    assert f'ExecStart="{sys.executable}" "-m" "quant_research.taiwan"' in service
    assert f'--root" "{tmp_path / "data"}"' in service
    assert f"WorkingDirectory={tmp_path}" in service
    assert "Type=oneshot" in service
    assert "UMask=0077" in service
    assert "TimeoutStartSec=infinity" in service
    assert "TimeoutStopSec=90" in service
    assert "EnvironmentFile" not in service
    assert "FINMIND" not in service
    assert "OnCalendar=*-*-* 20:30:00 Asia/Taipei" in timer
    assert "Persistent=true" in timer
    assert "RandomizedDelaySec=5m" in timer
    assert "WantedBy=timers.target" in timer
    assert write_units(tmp_path, root=tmp_path / "data", project_dir=tmp_path)
    assert write_units(tmp_path, root=tmp_path / "data", project_dir=tmp_path)


def test_path_escaping(tmp_path: Path) -> None:
    odd = tmp_path / 'sp ace$percent%"slash\\'
    service = render_units(directory=tmp_path, root=odd, project_dir=odd)[
        "quant-research-taiwan.service"
    ]
    assert 'sp ace$$percent%%\\"slash\\\\' in service
    # $ is not systemd environment expansion in WorkingDirectory.
    workdir = (
        str(odd.resolve())
        .replace("\\", "\\x5c")
        .replace('"', "\\x22")
        .replace(" ", "\\x20")
        .replace("%", "%%")
    )
    assert f"WorkingDirectory={workdir}" in service


def test_refuse_conflict_without_partial_write(tmp_path: Path) -> None:
    (tmp_path / "quant-research-taiwan.timer").write_text("different", encoding="utf-8")
    with pytest.raises(FileExistsError):
        write_units(tmp_path)
    assert not (tmp_path / "quant-research-taiwan.service").exists()


def test_reject_control_path(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="control"):
        render_units(directory=tmp_path, root=str(tmp_path) + "\n")


def test_cli_write_schedule(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["write-schedule", "--directory", str(tmp_path)]) == 0
    assert "quant-research-taiwan.service" in capsys.readouterr().out


def test_event_and_all_lanes(tmp_path: Path) -> None:
    events = render_units(directory=tmp_path, root=tmp_path / "data", lane="events")
    assert set(events) == {
        "quant-research-taiwan-events.service",
        "quant-research-taiwan-events.timer",
    }
    service = events["quant-research-taiwan-events.service"]
    assert f'ExecStart="{sys.executable}" "-m" "quant_research.taiwan"' in service
    assert '"maintain-events" "--max-tasks" "500"' in service
    assert "UMask=0077" in service
    assert "TimeoutStartSec=infinity" in service and "TimeoutStopSec=90" in service
    timer = events["quant-research-taiwan-events.timer"]
    assert "OnCalendar=*-*-* 20:45:00 Asia/Taipei" in timer
    assert "Persistent=true" in timer and "RandomizedDelaySec=5m" in timer
    assert "FINMIND" not in service
    assert len(render_units(directory=tmp_path, lane="all")) == 4
    assert len(render_units(directory=tmp_path)) == 2


def test_lane_validation_and_all_conflicts_are_preflighted(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="lane"):
        render_units(directory=tmp_path / "not-created", lane="bad")
    assert not (tmp_path / "not-created").exists()
    conflict = tmp_path / "conflict"
    conflict.mkdir()
    (conflict / "quant-research-taiwan-events.timer").write_text("different", encoding="utf-8")
    with pytest.raises(FileExistsError):
        write_units(conflict, lane="all")
    assert not (conflict / "quant-research-taiwan.service").exists()
    assert not (conflict / "quant-research-taiwan-events.service").exists()


def test_cli_schedule_lane(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["write-schedule", "--directory", str(tmp_path), "--lane", "events"]) == 0
    assert len(__import__("json").loads(capsys.readouterr().out)["created"]) == 2
