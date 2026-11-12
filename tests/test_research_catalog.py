from __future__ import annotations

import inspect
import json
import subprocess
import sys
from pathlib import Path

import pytest

import quant_research.research as research

ROOT = Path(__file__).resolve().parents[1]
_EXPECTED = {
    "compute_feature",
    "forward_returns",
    "analyze_feature",
    "backtest",
    "summarize",
    "compare_runs",
    "save_run",
    "target_weights",
    "daily_returns",
    "load_taiwan",
    "build_cache",
    "update_cache",
    "rebuild_cache",
    "load_cached",
    "cache_status",
    "list_symbols",
}


def test_capability_catalog_matches_public_api_signatures_and_scope() -> None:
    assert callable(research.target_weights)
    assert callable(research.daily_returns)
    entries = research.capabilities()
    assert isinstance(entries, tuple)
    assert {entry.name for entry in entries} == _EXPECTED
    assert all(entry.__class__.__name__ == "Capability" for entry in entries)
    for entry in entries:
        assert entry.import_path == f"quant_research.research.{entry.name}"
        function = getattr(research, entry.name)
        assert entry.signature == str(inspect.signature(function))
        assert entry.purpose and entry.output and entry.example
        assert "quant_research.research." in entry.import_path
    assert "both endpoints" in " ".join(
        next(x for x in entries if x.name == "analyze_feature").limitations
    )
    assert "not guarantee" in " ".join(
        next(x for x in entries if x.name == "compute_feature").limitations
    )
    assert "next-open" in " ".join(next(x for x in entries if x.name == "backtest").limitations)
    assert "new destination" in " ".join(
        next(x for x in entries if x.name == "save_run").limitations
    )
    assert "not guaranteed" in " ".join(
        next(x for x in entries if x.name == "compare_runs").limitations
    )
    target = next(x for x in entries if x.name == "target_weights")
    target_scope = " ".join((target.purpose, target.output, *target.limitations))
    assert "cash_reserve" in target_scope
    assert "known closes" in target_scope
    assert "pending rights" in target_scope and "not sellable" in target_scope
    assert "rejection" in target_scope
    returns = next(x for x in entries if x.name == "daily_returns")
    returns_scope = " ".join((returns.output, *returns.limitations))
    assert "initial_cash" in returns_scope
    assert "zero NAV" in returns_scope and "None" in returns_scope
    assert "not annualized" in returns_scope
    save = next(x for x in entries if x.name == "save_run")
    assert "from pathlib import Path" in save.example


def test_import_and_catalog_are_free_of_market_data_io() -> None:
    guard = """
import socket, sqlite3
from quant_research.taiwan.dataset.source import SourceReader

def blocked(*args, **kwargs):
    raise AssertionError("unexpected market-data I/O")
socket.socket = blocked
sqlite3.connect = blocked
SourceReader.__enter__ = blocked
import quant_research.research as research
assert len(research.capabilities()) == 16
"""
    subprocess.run(
        [sys.executable, "-c", guard], cwd=ROOT, check=True, capture_output=True, text=True
    )


@pytest.mark.parametrize("filename", ["features.py", "single_asset.py", "ranking.py"])
def test_teaching_example_runs_in_fresh_process(filename: str) -> None:
    completed = subprocess.run(
        [sys.executable, str(ROOT / "examples" / "research" / filename)],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    output = json.loads(completed.stdout)
    assert output["data"] == "synthetic; not real market data"
    if filename == "features.py":
        assert [(row["symbol"], row["value"]) for row in output["first_day_h1_labels"]] == [
            ("A", "0.1"),
            ("B", "-0.05"),
        ]
        assert output["research_slice"]["start"] == "2024-01-01"
        assert output["out_of_sample_slice"]["start"] == "2024-01-04"
    elif filename == "single_asset.py":
        assert output["buy_10"]["final_nav"] == "1020"
        assert output["buy_10"]["fills"] == 2
        assert output["buy_10"]["fill_dates"] == ["2024-01-02", "2024-01-04"]
        assert output["buy_5"]["final_nav"] == "1010"
        assert set(output["comparison"]["summaries"].values()) == {"1010", "1020"}
        assert "params" in output["comparison"]["differences"]
        assert "not establish" in output["comparison"]["interpretation"]
    else:
        assert output["final_nav"] == "1047.916667"
        assert output["fills"] == 4
