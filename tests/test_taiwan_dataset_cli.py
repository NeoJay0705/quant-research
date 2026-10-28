from __future__ import annotations

import pytest

from quant_research.taiwan.cli import parser


def test_retired_dataset_commands_are_absent_and_official_commands_remain() -> None:
    commands = parser()._subparsers._group_actions[0].choices
    assert "build-dataset" not in commands and "export-dataset" not in commands
    assert {
        "sync",
        "sync-events",
        "maintain-events",
        "backfill",
        "resume",
        "quotes",
        "events",
    } <= set(commands)
    parsed = parser().parse_args(
        [
            "--root",
            "/tmp/local",
            "sync-events",
            "--start",
            "2024-01-01",
            "--end",
            "2024-01-31",
            "--markets",
            "twse",
        ]
    )
    assert (
        parsed.command == "sync-events"
        and parsed.start == "2024-01-01"
        and parsed.markets == ["twse"]
    )
    with pytest.raises(SystemExit):
        parser().parse_args(["build-dataset"])
