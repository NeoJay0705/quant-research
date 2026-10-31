from __future__ import annotations

import hashlib
import json
import sqlite3
import subprocess
import sys
from decimal import ROUND_DOWN, Decimal, localcontext
from pathlib import Path

import pytest

from quant_research.taiwan import official, sources
from quant_research.taiwan.dataset.events import EventRef, EventValue
from quant_research.taiwan.dataset.models import QuoteValue
from quant_research.taiwan.storage import SCHEMA

SCRIPT = Path(__file__).parents[1] / "scripts" / "dataset_local_validation.py"
sys.path.insert(0, str(SCRIPT.parent))
import dataset_local_validation as validation  # noqa: E402
from dataset_local_validation import _oracle  # noqa: E402


def test_independent_oracle_fixed_two_exchange_factors_and_date_edges() -> None:
    quotes = [
        QuoteValue(
            "twse",
            "2330",
            f"2024-01-0{i}",
            Decimal(v),
            Decimal(v),
            Decimal(v),
            Decimal(v),
            Decimal("9"),
            Decimal("900"),
        )
        for i, v in enumerate(("100", "102", "51", "53", "106"), start=1)
    ]
    ref = EventRef("twse", "TWTAUU", "task", "snap", 1, "now")
    events = (
        EventValue(
            "twse",
            "2330",
            "capital_reduction",
            "2024-01-03",
            Decimal(0),
            None,
            Decimal(0),
            Decimal(2),
            None,
            None,
            ref,
        ),
        EventValue(
            "twse",
            "2330",
            "par_value",
            "2024-01-05",
            Decimal(0),
            None,
            Decimal(0),
            Decimal("0.5"),
            None,
            None,
            ref,
        ),
    )
    actual = _oracle(quotes, events)
    assert [row["factor"] for row in actual] == list(map(Decimal, ("1", "1", "2", "2", "1")))
    assert [row["ohlc"][0] for row in actual] == list(
        map(Decimal, ("100", "102", "102", "106", "106"))
    )
    # Events on first quote date are intentionally excluded from the baseline.
    at_start = EventValue(
        "twse",
        "2330",
        "capital_reduction",
        "2024-01-01",
        Decimal(0),
        None,
        Decimal(0),
        Decimal(4),
        None,
        None,
        ref,
    )
    assert _oracle(quotes, (at_start, *events))[0]["factor"] == 1


def _quote_db(root: Path) -> None:
    root.mkdir(parents=True)
    db = sqlite3.connect(root / "state.sqlite3")
    db.executescript(SCHEMA)
    params = {"pipeline": "official-v1", "lane": "quotes", "request": {"date": "2024-01-02"}}
    encoded = json.dumps(params, sort_keys=True)
    raw = json.dumps(
        {
            "stat": "OK",
            "date": "20240102",
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
                    "data": [["2330", "台積電", "100", "101", "99", "100", "10", "1000"]],
                }
            ],
        },
        ensure_ascii=False,
    ).encode()
    parsed = official.parse(
        sources.RawBatch("twse", "MI_INDEX", "offline", "https://example.invalid", 200, raw, "", 0),
        {"date": "2024-01-02"},
    )
    db.execute(
        "INSERT INTO snapshots VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "snap",
            "twse",
            "MI_INDEX",
            encoded,
            "url",
            "scope",
            "2024-01-03",
            200,
            hashlib.sha256(raw).hexdigest(),
            "raw",
            "valid",
            None,
        ),
    )
    db.execute(
        "INSERT INTO tasks(id,logical_key,edition,source,dataset,params,kind,state,snapshot_id) "
        "VALUES(?,?,?,?,?,?,?,?,?)",
        ("task", "quote", 1, "twse", "MI_INDEX", encoded, "official_quote", "completed", "snap"),
    )
    for rec in parsed.records:
        db.execute(
            "INSERT INTO records(snapshot_id,kind,symbol,record_date,record_key,payload,"
            "quality_flags) VALUES(?,?,?,?,?,?,?)",
            (
                "snap",
                "quote",
                rec["symbol"],
                rec["trade_date"],
                "1",
                json.dumps(rec, ensure_ascii=False),
                "[]",
            ),
        )
    db.commit()
    db.close()


def test_real_script_subprocess_discover_and_verify(tmp_path: Path) -> None:
    root = tmp_path / "source"
    _quote_db(root)
    base = [sys.executable, str(SCRIPT), "--root", str(root)]
    discover = subprocess.run(
        [*base, "discover", "--start", "2024-01-02", "--end", "2024-01-02"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert discover.returncode == 0, discover.stderr
    assert json.loads(discover.stdout)["operation"] == "discover"
    verify = subprocess.run(
        [
            *base,
            "verify",
            "--start",
            "2024-01-02",
            "--end",
            "2024-01-02",
            "--market",
            "twse",
            "--symbol",
            "2330",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert verify.returncode == 0, verify.stderr
    evidence = json.loads(verify.stdout)
    assert evidence["passed"] is True
    assert evidence["rows"][0]["expected"]["factor"] == "1"
    assert evidence["rows"][0]["actual"]["factor"] == "1"


def test_subprocess_requires_existing_source_and_never_creates_it(tmp_path: Path) -> None:
    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--root",
            str(tmp_path),
            "discover",
            "--start",
            "2024-01-01",
            "--end",
            "2024-01-02",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert not (tmp_path / "state.sqlite3").exists()


@pytest.mark.parametrize("end", ["2024-03-02", "2024-02-31", "2023-12-31"])
def test_subprocess_rejects_invalid_or_overlong_date_range(tmp_path: Path, end: str) -> None:
    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--root",
            str(tmp_path),
            "discover",
            "--start",
            "2024-01-01",
            "--end",
            end,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0


def test_oracle_isolated_from_hostile_caller_context() -> None:
    quote = QuoteValue("twse", "2330", "2024-01-01", *(Decimal("1.23456789"),) * 4, None, None)
    with localcontext() as ctx:
        ctx.prec = 2
        ctx.rounding = ROUND_DOWN
        actual = _oracle([quote], ())
    assert actual[0]["ohlc"] == [Decimal("1.23456789")] * 4


def test_data_version_detects_second_connection_commit(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "source"
    _quote_db(root)
    spec = validation.BuildSpec(root, "2024-01-02", "2024-01-02", ("2330",), ("twse",))
    real_iter = validation.iter_series

    def committing_iter(spec_arg):
        db = sqlite3.connect(root / "state.sqlite3")
        db.execute("UPDATE snapshots SET raw_path='committed-during-verify' WHERE id='snap'")
        db.commit()
        db.close()
        yield from real_iter(spec_arg)

    monkeypatch.setattr(validation, "iter_series", committing_iter)
    evidence = validation.verify(spec, "twse", "2330")
    changed = next(item for item in evidence["checks"] if item["check"] == "source_unchanged")
    assert changed["status"] == "failed"
    assert evidence["passed"] is False


def test_oracle_keeps_cash_and_bonus_event_out_of_price_factor() -> None:
    quote = QuoteValue(
        "tpex", "1234", "2024-01-02", *(Decimal("10"),) * 4, Decimal("7"), Decimal("70")
    )
    ref = EventRef("tpex", "exDailyQ", "task", "snap", 8, "now")
    event = EventValue(
        "tpex",
        "1234",
        "dividend",
        "2024-01-02",
        Decimal("2"),
        Decimal("0.5"),
        Decimal("0"),
        None,
        None,
        None,
        ref,
    )
    row = _oracle([quote], [event])[0]
    assert row["factor"] == 1
    assert row["ohlc"] == [Decimal("10")] * 4
