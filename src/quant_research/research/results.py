"""Immutable, provenance-preserving persistence for completed research runs."""

from __future__ import annotations

import hashlib
import json
import math
import os
import tempfile
from collections.abc import Mapping
from dataclasses import fields, is_dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, BinaryIO

from .backtest import RunResult
from .models import ResearchError
from .performance import summarize


class ResearchSaveError(ResearchError):
    """A run could not be safely or completely persisted."""


_TABLES = {
    "snapshots": "snapshots.jsonl",
    "daily_returns": "daily_returns.jsonl",
    "decisions": "decisions.jsonl",
    "fills": "fills.jsonl",
    "rejections": "rejections.jsonl",
    "events": "events.jsonl",
}
_TABLE_FIELDS = frozenset(_TABLES) - {"daily_returns"}


def _json_value(value: Any) -> Any:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError("non-finite Decimal")
        return str(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("non-finite float")
        return value
    if isinstance(value, Path):
        return str(value)
    if is_dataclass(value) and not isinstance(value, type):
        return {field.name: _json_value(getattr(value, field.name)) for field in fields(value)}
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise ValueError("JSON object keys must be strings")
        return {key: _json_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json_value(item) for item in value]
    raise ValueError(f"unsupported JSON value: {type(value).__name__}")


def _encoded(value: Any) -> bytes:
    return (
        json.dumps(_json_value(value), ensure_ascii=False, allow_nan=False, indent=2) + "\n"
    ).encode("utf-8", errors="strict")


def _jsonl(values: Any) -> bytes:
    return b"".join(
        (json.dumps(_json_value(value), ensure_ascii=False, allow_nan=False) + "\n").encode(
            "utf-8", errors="strict"
        )
        for value in values
    )


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _validate_destination(run: RunResult, requested: Path) -> Path:
    absolute = Path.cwd() / requested if not requested.is_absolute() else requested
    # Reject symlinked path components, including the immediate parent, before resolve().
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        current /= part
        if current.is_symlink():
            raise ResearchSaveError(f"destination path contains symlink: {current}")
    if absolute.exists():
        raise ResearchSaveError(f"destination already exists: {absolute}")
    destination = absolute.resolve(strict=False)
    protected = [Path(value).expanduser().resolve() for value in run.protected_input_dirs]
    protected.append((Path.home() / ".local/share/quant-research/taiwan").resolve())
    if any(_is_within(destination, root) for root in protected):
        raise ResearchSaveError(f"destination is within protected input storage: {destination}")
    return destination


def _open_new_file(path: Path) -> BinaryIO:
    """Open a new private file exclusively, closing its descriptor on wrap failure."""
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        return os.fdopen(descriptor, "wb")
    except BaseException:
        os.close(descriptor)
        raise


def _publish_complete(destination: Path, content: bytes) -> None:
    """Publish the completion marker only after its complete contents are closed."""
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".COMPLETE.json.", suffix=".pending", dir=destination
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
        # A hard link is an atomic, no-overwrite publication in the same directory.
        os.link(temporary, destination / "COMPLETE.json")
    except BaseException:
        # Before publication, preserve the primary failure even if cleanup fails.
        try:
            temporary.unlink(missing_ok=True)
        except Exception:
            pass
        raise
    else:
        # The link is the commit point: cleanup is best-effort after it succeeds.
        try:
            temporary.unlink(missing_ok=True)
        except Exception:
            pass


def save_run(run: RunResult, directory: str | Path) -> Path:
    """Save captured run provenance and detail tables; never access source files or program."""
    if not isinstance(run, RunResult):
        raise ResearchSaveError("run must be RunResult")
    try:
        requested = Path(directory)
        destination = _validate_destination(run, requested)
        details: dict[str, bytes] = {}
        for field in _TABLE_FIELDS:
            details[_TABLES[field]] = _jsonl(getattr(run, field))
        from .performance import daily_returns

        details[_TABLES["daily_returns"]] = _jsonl(daily_returns(run))
        excluded = _TABLE_FIELDS
        metadata = {
            field.name: getattr(run, field.name)
            for field in fields(run)
            if field.name not in excluded
        }
        files: dict[str, bytes] = {
            "metadata.json": _encoded(metadata),
            "summary.json": _encoded(summarize(run)),
        }
        files.update(details)
    except ResearchSaveError:
        raise
    except (OSError, TypeError, ValueError, OverflowError) as exc:
        raise ResearchSaveError(
            f"run serialization or destination validation failed: {exc}"
        ) from exc

    try:
        destination.mkdir(mode=0o700, parents=True, exist_ok=False)
        hashes: dict[str, str] = {}
        for name, content in files.items():
            path = destination / name
            with _open_new_file(path) as stream:
                stream.write(content)
            hashes[name] = hashlib.sha256(content).hexdigest()
        complete = {
            "schema": "quant-research-run-v1",
            "status": "COMPLETE",
            "files": dict(sorted(hashes.items())),
        }
        _publish_complete(destination, _encoded(complete))
        return destination
    except (OSError, TypeError, ValueError) as exc:
        raise ResearchSaveError(f"failed to save run to {destination}: {exc}") from exc
