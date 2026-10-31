from __future__ import annotations

import sys
from decimal import Decimal
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parents[1] / "scripts" / "dataset_local_validation.py"
sys.path.insert(0, str(SCRIPT.parent))
import dataset_local_validation as validation  # noqa: E402
from dataset_local_validation import verify  # noqa: E402

from quant_research.taiwan.dataset.models import BuildSpec  # noqa: E402
from quant_research.taiwan.dataset.source import SourceReader  # noqa: E402
from tests.test_taiwan_dataset_events import add_twse_parent, install  # noqa: E402
from tests.test_taiwan_dataset_pipeline_reduction import (  # noqa: E402
    _quotes,
    _reduction_summary,
)
from tests.test_taiwan_dataset_pipeline_sequence import _install  # noqa: E402
from tests.test_taiwan_dataset_pipeline_tpex import _install as install_tpex  # noqa: E402


def _number_strings(values) -> list[str]:
    return [format(Decimal(value).normalize(), "f") for value in values]


def _rows(evidence: dict) -> tuple[list[str], list[str], list[str], list[str]]:
    rows = evidence["rows"]
    expected_factors = _number_strings(row["expected"]["factor"] for row in rows)
    actual_factors = _number_strings(row["actual"]["factor"] for row in rows)
    expected_closes = _number_strings(row["expected"]["ohlc"][3] for row in rows)
    actual_closes = _number_strings(row["actual"]["close"] for row in rows)
    return expected_factors, actual_factors, expected_closes, actual_closes


def test_twse_sequential_adjustments_through_verifier(tmp_path: Path) -> None:
    days = tuple(f"2024-01-0{day}" for day in range(1, 6))
    root = tmp_path / "sequence"
    _install(
        root,
        days,
        (100, 102, 51, 53, 106),
        ((days[0], 200, 100), (days[2], 100, 50), (days[4], 100, 200), ("2024-01-08", 100, 100)),
    )
    spec = BuildSpec(root, days[0], days[-1], symbols=("2330",), sources=("twse",))

    evidence = verify(spec, "twse", "2330")

    assert evidence["passed"] is True
    expected = (
        ["1", "1", "2", "2", "1"],
        ["1", "1", "2", "2", "1"],
        ["100", "102", "102", "106", "106"],
        ["100", "102", "102", "106", "106"],
    )
    assert _rows(evidence) == expected


def test_event_without_quote_and_future_event_leave_fixed_prefix(tmp_path: Path) -> None:
    root = tmp_path / "no-quote-day"
    days = ("2024-02-01", "2024-02-03")
    _install(root, days, (100, 51), (("2024-02-02", 100, 50),))
    spec = BuildSpec(root, days[0], days[-1], symbols=("2330",), sources=("twse",))

    evidence = verify(spec, "twse", "2330")

    assert evidence["passed"] is True
    assert _rows(evidence) == (["1", "2"], ["1", "2"], ["100", "102"], ["100", "102"])


@pytest.mark.parametrize(
    ("optional_fields", "optional_values", "expected_dividend", "expected_return"),
    [
        (["每股退還股款", "(每股配發現金股利)除息"], ["0.6 元/股", "0.1 元/股"], "0.1", "0.6"),
        ([], [], "0", "0"),
    ],
    ids=("cash-return-and-dividend", "optional-amounts-absent"),
)
def test_twse_reduction_terms_and_linked_refs(
    tmp_path: Path, optional_fields, optional_values, expected_dividend, expected_return
) -> None:
    root = tmp_path / "reduction"
    old_start, old_end = "2024-01-01", "2024-06-30"
    new_start, new_end = "2024-02-01", "2024-12-31"
    old_detail = {
        "stat": "OK",
        "fields": ["股票代號", "每壹仟股換發新股票", *optional_fields],
        "data": [["2330", "800 股", *(["0.7 元/股", "0.2 元/股"] if optional_fields else [])]],
    }
    install(
        root,
        parent_dataset="TWTAUU",
        summary=_reduction_summary(start=old_start, end=old_end, token="2330,20240102"),
        child_dataset="TWTAVUDetail",
        detail=old_detail,
        logical_key="older-window",
        request={"start_date": old_start, "end_date": old_end},
        fetched="2024-04-01",
    )
    newer_detail = {
        "stat": "OK",
        "fields": ["股票代號", "每壹仟股換發新股票", *optional_fields],
        "data": [["2330", "940 股", *optional_values]],
    }
    add_twse_parent(
        root,
        dataset="TWTAUU",
        summary=_reduction_summary(start=new_start, end=new_end, token="2330,20240202"),
        logical="newer-window",
        edition=1,
        fetched="2024-05-01",
        request={"start_date": new_start, "end_date": new_end},
        detail=newer_detail,
    )
    _quotes(root)
    spec = BuildSpec(root, "2024-02-29", "2024-03-01", symbols=("2330",), sources=("twse",))

    evidence = verify(spec, "twse", "2330")

    assert evidence["passed"] is True
    assert _rows(evidence) == (["1", "0.94"], ["1", "0.94"], ["100", "94"], ["100", "94"])
    assert all(
        check["status"] == "passed"
        for check in evidence["checks"]
        if check["check"] == "event_terms_and_refs"
    )
    with SourceReader(spec) as reader:
        _, _, expected_events = validation._raw_inputs(reader, "twse", "2330", spec)
    (expected_event,) = expected_events
    actual_event = next(row for row in evidence["checks"] if row["check"] == "event_terms_and_refs")
    assert actual_event["status"] == "passed"
    assert (
        expected_event.event_type,
        expected_event.effective_date,
        expected_event.share_multiplier,
    ) == ("capital_reduction", "2024-03-01", Decimal("0.94"))
    assert (expected_event.dividend, expected_event.cash_return_per_share) == (
        Decimal(expected_dividend),
        Decimal(expected_return),
    )
    assert expected_event.ref.task_id == "parent-newer-window-1"
    assert expected_event.ref.detail_task_id == "child-newer-window-1"
    assert expected_event.ref.detail_dataset == "TWTAVUDetail"
    assert expected_event.ref.detail_snapshot_id == "snap-child-newer-window-1"
    assert (
        expected_event.ref.detail_record_id is not None and expected_event.ref.detail_record_id > 0
    )


@pytest.mark.parametrize(
    ("market", "expected_factors", "expected_closes"),
    [("tpex", ["1", "3"], ["100", "300"]), ("twse", ["1", "2"], ["100", "200"])],
)
def test_exchange_specific_event_factors(
    tmp_path: Path, market: str, expected_factors: list[str], expected_closes: list[str]
) -> None:
    base_spec = install_tpex(tmp_path)
    spec = BuildSpec(
        base_spec.root,
        "2024-01-01",
        "2024-01-05",
        symbols=("2330",),
        sources=(market,),
    )

    evidence = verify(spec, market, "2330")

    assert evidence["passed"] is True
    assert _rows(evidence) == (expected_factors, expected_factors, expected_closes, expected_closes)


def test_twse_linked_dividend_and_bonus_through_verifier(tmp_path: Path) -> None:
    from tests.test_taiwan_dataset_events import twse_summary

    root = tmp_path / "linked-dividend"
    install(
        root,
        parent_dataset="TWT49U",
        summary=twse_summary(
            "TWT49U",
            ["資料日期", "股票代號", "除權息前收盤價", "除權息參考價", "詳細資料"],
            ["2024-03-01", "2330", "100", "90", "2330,20240301"],
        ),
        child_dataset="TWT49UDetail",
        detail={
            "stat": "OK",
            "fields": [
                "股票代號",
                "(每股配發現金股利)除息",
                "A. 按普通股股東持股比例每千股無償配股",
            ],
            "data": [["2330", "2", "125"]],
        },
    )
    _quotes(root)
    spec = BuildSpec(root, "2024-02-29", "2024-03-01", symbols=("2330",), sources=("twse",))

    evidence = verify(spec, "twse", "2330")

    assert evidence["passed"] is True
    assert _rows(evidence) == (["1", "1"], ["1", "1"], ["100", "100"], ["100", "100"])
    event_terms = next(
        check for check in evidence["checks"] if check["check"] == "event_terms_and_refs"
    )
    assert event_terms["status"] == "passed"
    assert event_terms["expected"] == event_terms["actual"]
    checks = {check["check"]: check["status"] for check in evidence["checks"]}
    assert checks["case:dividend"] == "passed"
    assert checks["case:bonus"] == "passed"
    assert checks["case:cash_return"] == "unverified"

    with SourceReader(spec) as reader:
        _, _, expected_events = validation._raw_inputs(reader, "twse", "2330", spec)
    (event,) = expected_events
    assert event.dividend == Decimal("2")
    assert event.bonus_share_ratio == Decimal("0.125")
    assert event.ref.detail_dataset == "TWT49UDetail"
