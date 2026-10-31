from __future__ import annotations

import json
import sqlite3
import sys
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest

from quant_research.taiwan.dataset.models import BuildSpec, DatasetError
from tests.test_taiwan_dataset_pipeline_sequence import _install
from tests.test_taiwan_dataset_pipeline_tpex import _install as install_tpex

SCRIPT = Path(__file__).parents[1] / "scripts"
sys.path.insert(0, str(SCRIPT))
import dataset_local_validation as validation  # noqa: E402

DAYS = tuple(f"2024-01-0{day}" for day in range(1, 6))


def _spec(root: Path, symbol: str = "2330") -> BuildSpec:
    return BuildSpec(root, DAYS[0], DAYS[-1], symbols=(symbol,), sources=("twse",))


@pytest.mark.parametrize(
    ("fault", "failed_check"),
    [
        ("factor", "adjustment:2024-01-03"),
        ("adjusted", "adjustment:2024-01-03"),
        ("event", "event_terms_and_refs"),
        ("refs", "raw_and_refs:2024-01-01"),
        ("duplicate", "raw_result_identities"),
        ("extra", "adjusted_result_identities"),
        ("classification_count", "classification_count"),
    ],
)
def test_verify_rejects_injected_pipeline_faults(tmp_path, monkeypatch, fault, failed_check):
    root = tmp_path / "source"
    _install(root, DAYS, (100, 102, 51, 53, 106), ((DAYS[2], 100, 50),))
    real_iter = validation.iter_series

    def faulty(spec):
        for series in real_iter(spec):
            changes = {}
            if fault == "factor":
                changes["quotes"] = (
                    *series.quotes[:2],
                    replace(series.quotes[2], factor=Decimal("9")),
                    *series.quotes[3:],
                )
            elif fault == "adjusted":
                changes["quotes"] = (
                    *series.quotes[:2],
                    replace(series.quotes[2], close=Decimal("999")),
                    *series.quotes[3:],
                )
            elif fault == "event":
                changes["events"] = (replace(series.events[0], dividend=Decimal("7")),)
            elif fault == "refs":
                changes["raw_quotes"] = (
                    replace(series.raw_quotes[0], refs=()),
                    *series.raw_quotes[1:],
                )
            elif fault == "duplicate":
                changes["raw_quotes"] = (*series.raw_quotes, series.raw_quotes[0])
            elif fault == "extra":
                changes["quotes"] = (*series.quotes, series.quotes[0])
            elif fault == "classification_count":
                changes["classifications"] = series.classifications[:-1]
            yield replace(series, **changes)

    monkeypatch.setattr(validation, "iter_series", faulty)
    result = validation.verify(_spec(root), "twse", "2330")
    assert result["passed"] is False
    assert any(c["check"] == failed_check and c["status"] == "failed" for c in result["checks"])


def test_latest_failed_edition_is_not_hidden_by_good_edition_one(tmp_path):
    root = tmp_path / "source"
    _install(root, DAYS, (100, 102, 51, 53, 106), ())
    db = sqlite3.connect(root / "state.sqlite3")
    db.execute(
        "INSERT INTO tasks(id,logical_key,edition,source,dataset,params,kind,state,snapshot_id) "
        "SELECT 'failed-latest',logical_key,2,source,dataset,params,kind,'failed',NULL "
        "FROM tasks WHERE id='task-0'"
    )
    db.commit()
    db.close()
    with pytest.raises(DatasetError):
        validation.verify(_spec(root), "twse", "2330")


def test_missing_ohlc_keeps_nonzero_turnover(tmp_path):
    root = tmp_path / "source"
    _install(root, DAYS[:1], (100,), ())
    db = sqlite3.connect(root / "state.sqlite3")
    row = db.execute("SELECT payload FROM records LIMIT 1").fetchone()
    payload = json.loads(row[0])
    payload["open"] = None
    db.execute("UPDATE records SET payload=?", (json.dumps(payload, ensure_ascii=False),))
    db.commit()
    db.close()
    spec = BuildSpec(root, DAYS[0], DAYS[0], symbols=("2330",), sources=("twse",))
    result = validation.verify(spec, "twse", "2330")
    assert result["passed"] is True
    assert (
        next(c for c in result["checks"] if c["check"] == "case:missing_values")["status"]
        == "passed"
    )
    assert result["rows"][0]["raw"]["volume"] == "10"
    assert result["rows"][0]["raw"]["amount"] == "1000"


@pytest.mark.parametrize(("symbol", "name"), [("0050", "元大台灣50"), ("2330", "台積電")])
def test_nonordinary_or_ambiguous_classification_has_no_adjusted_rows(tmp_path, symbol, name):
    root = tmp_path / "source"
    _install(root, DAYS[:1], (100,), ())
    db = sqlite3.connect(root / "state.sqlite3")
    payload = json.loads(db.execute("SELECT payload FROM records LIMIT 1").fetchone()[0])
    payload["symbol"], payload["name"] = symbol, name
    if symbol == "2330":
        payload["venue_id"] = "ambiguous"
    db.execute(
        "UPDATE records SET symbol=?,payload=?", (symbol, json.dumps(payload, ensure_ascii=False))
    )
    db.commit()
    db.close()
    spec = BuildSpec(root, DAYS[0], DAYS[0], symbols=(symbol,), sources=("twse",))
    result = validation.verify(spec, "twse", symbol)
    assert result["passed"] is True
    assert result["rows"][0]["actual"] is None
    assert (
        next(c for c in result["checks"] if c["check"] == "case:nonordinary_or_unknown")["status"]
        == "passed"
    )
    assert "nonordinary_or_unknown" not in result["unverified"]


def test_discover_limit_one_preserves_quotes_and_both_markets_par_value_events(tmp_path):
    base = install_tpex(tmp_path)
    result = validation.discover(base, 1)
    scopes = result["scopes"]
    assert {s["source"] for s in scopes if s["lane"] == "quotes"} == {"twse", "tpex"}
    for market in ("twse", "tpex"):
        candidates = [
            r for s in scopes if s["source"] == market and s["lane"] == "events" for r in s["rows"]
        ]
        assert any(r["event_type"] == "par_value" for r in candidates)
        for row in candidates:
            assert row["record_id"] is not None and row["task_id"] and row["snapshot_id"]
