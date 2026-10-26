"""Generate systemd user units without installing or enabling them."""

from __future__ import annotations

import os
import sys
from pathlib import Path

from .storage import DEFAULT_ROOT

SERVICE_NAME = "quant-research-taiwan.service"
TIMER_NAME = "quant-research-taiwan.timer"
EVENT_SERVICE_NAME = "quant-research-taiwan-events.service"
EVENT_TIMER_NAME = "quant-research-taiwan-events.timer"


def _validate_path(path: Path) -> str:
    value = str(path.resolve())
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError("unit paths cannot contain newline or control characters")
    return value


def _exec_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("$", "$$").replace("%", "%%")


def _directive_escape(value: str) -> str:
    return (
        value.replace("\\", "\\x5c").replace('"', "\\x22").replace(" ", "\\x20").replace("%", "%%")
    )


def render_units(
    *,
    directory: str | os.PathLike[str],
    root: str | os.PathLike[str] = DEFAULT_ROOT,
    project_dir: str | os.PathLike[str] | None = None,
    python: str | os.PathLike[str] = sys.executable,
    lane: str = "quotes",
) -> dict[str, str]:
    """Return unit filename/content pairs for the selected paths."""
    if lane not in {"quotes", "events", "all"}:
        raise ValueError("lane must be quotes, events, or all")
    data_root = _validate_path(Path(root).expanduser())
    workdir = _validate_path(Path(project_dir or Path.cwd()).expanduser())
    _validate_path(Path(directory).expanduser())
    interpreter = os.fspath(Path(python).expanduser())
    if not Path(interpreter).is_absolute():
        raise ValueError("Python executable path must be absolute")
    if any(ord(char) < 32 or ord(char) == 127 for char in interpreter):
        raise ValueError("unit paths cannot contain newline or control characters")
    command = " ".join(
        f'"{_exec_escape(arg)}"'
        for arg in (interpreter, "-m", "quant_research.taiwan", "--root", data_root, "sync")
    )
    service = (
        "[Unit]\nDescription=Update local Taiwan equity data\n\n"
        "[Service]\nType=oneshot\nUMask=0077\nTimeoutStartSec=infinity\nTimeoutStopSec=90\n"
        f"WorkingDirectory={_directive_escape(workdir)}\n"
        f"ExecStart={command}\n"
    )
    timer = (
        "[Unit]\nDescription=Daily Taiwan equity data update\n\n"
        "[Timer]\nOnCalendar=*-*-* 20:30:00 Asia/Taipei\nPersistent=true\n"
        "RandomizedDelaySec=5m\nUnit=quant-research-taiwan.service\n\n"
        "[Install]\nWantedBy=timers.target\n"
    )
    quote_units = {SERVICE_NAME: service, TIMER_NAME: timer}
    if lane == "quotes":
        return quote_units
    event_command = " ".join(
        f'"{_exec_escape(arg)}"'
        for arg in (
            interpreter,
            "-m",
            "quant_research.taiwan",
            "--root",
            data_root,
            "maintain-events",
            "--max-tasks",
            "500",
        )
    )
    event_service = (
        "[Unit]\nDescription=Maintain local Taiwan equity events\n\n"
        "[Service]\nType=oneshot\nUMask=0077\nTimeoutStartSec=infinity\nTimeoutStopSec=90\n"
        f"WorkingDirectory={_directive_escape(workdir)}\n"
        f"ExecStart={event_command}\n"
    )
    event_timer = (
        "[Unit]\nDescription=Daily Taiwan equity event maintenance\n\n"
        "[Timer]\nOnCalendar=*-*-* 20:45:00 Asia/Taipei\nPersistent=true\n"
        "RandomizedDelaySec=5m\nUnit=quant-research-taiwan-events.service\n\n"
        "[Install]\nWantedBy=timers.target\n"
    )
    event_units = {EVENT_SERVICE_NAME: event_service, EVENT_TIMER_NAME: event_timer}
    return event_units if lane == "events" else quote_units | event_units


def write_units(
    directory: str | os.PathLike[str], *, lane: str = "quotes", **kwargs: object
) -> list[str]:
    """Create selected units exclusively; matching existing files are idempotent."""
    target = Path(directory).expanduser()
    units = render_units(directory=target, lane=lane, **kwargs)  # type: ignore[arg-type]
    target.mkdir(parents=True, exist_ok=True)
    paths = {name: target / name for name in units}
    # Check both before creating either, so known conflicts never cause a partial update.
    for name, path in paths.items():
        try:
            existing = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            continue
        if existing != units[name]:
            raise FileExistsError(f"refusing to overwrite different unit: {path}")
    for name, path in paths.items():
        try:
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            if path.read_text(encoding="utf-8") != units[name]:
                raise FileExistsError(f"refusing to overwrite different unit: {path}") from None
            continue
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(units[name])
    return [str(paths[name]) for name in units]
