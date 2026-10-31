from __future__ import annotations

from dataclasses import FrozenInstanceError
from decimal import Decimal
from typing import cast

import pytest

from quant_research.research import Bar, CorporateAction, ResearchData, ResearchInputError


def _data(symbols: tuple[str, ...] = ("A",)) -> ResearchData:
    bars = tuple(
        Bar("market", symbol, day, close=Decimal(str(i + 1)))
        for symbol in sorted(symbols)
        for i, day in enumerate(("2024-01-01", "2024-01-03"))
    )
    return ResearchData(
        "market",
        symbols,
        "2024-01-01",
        "2024-01-03",
        ("2024-01-01", "2024-01-02", "2024-01-03"),
        bars,
    )


def test_prefix_view_preserves_missing_sessions_and_never_exposes_future() -> None:
    data = _data()
    view = data.at("2024-01-02")
    assert view.sessions == ("2024-01-01", "2024-01-02")
    assert view.bar("A") is None
    assert [bar.date for bar in view.history("A")] == ["2024-01-01"]
    assert not hasattr(view, "data")
    assert not hasattr(view, "bars")
    assert callable(view.events)
    assert not hasattr(view, "_bars_by_symbol")
    with pytest.raises(ResearchInputError, match="future"):
        view.bar("A", "2024-01-03")
    with pytest.raises(ResearchInputError, match="within load bounds"):
        type(view)(_data=data, as_of="2024-01-04")
    with pytest.raises(FrozenInstanceError):
        data.market = "other"  # type: ignore[misc]
    assert data.at("2024-01-03").history("A")[-1].close == Decimal("2")


def test_digest_canonical_and_relevant_metadata_sensitive() -> None:
    first = _data(("A", "B"))
    second = _data(("B", "A"))
    assert first.input_digest == second.input_digest
    bar = first.bars[0]
    altered = (
        Bar(**{**bar.__dict__, "classification": "excluded_non_ordinary"})
        if hasattr(bar, "__dict__")
        else Bar(
            "market", bar.symbol, bar.date, close=bar.close, classification="excluded_non_ordinary"
        )
    )
    changed = ResearchData(
        "market",
        first.symbols,
        first.start_date,
        first.end_date,
        first.sessions,
        (altered, *first.bars[1:]),
    )
    assert changed.input_digest != first.input_digest


def test_rejects_mutable_metadata_and_bad_nested_payloads() -> None:
    with pytest.raises(ResearchInputError, match="classification_diagnostics"):
        ResearchData(
            "market",
            ("A",),
            "2024-01-01",
            "2024-01-01",
            ("2024-01-01",),
            (),
            classification_diagnostics=[],
        )
    event = CorporateAction("market", "A", "dividend", "2024-01-02", cash_credit_date="2024-01-01")
    with pytest.raises(ResearchInputError, match="precede"):
        ResearchData(
            "market", ("A",), "2024-01-01", "2024-01-02", ("2024-01-01", "2024-01-02"), (), (event,)
        )
    event = CorporateAction(
        "market", "A", "dividend", "2024-01-01", payload=(("nested", cast(str, [])),)
    )
    with pytest.raises(ResearchInputError, match="payload"):
        ResearchData("market", ("A",), "2024-01-01", "2024-01-01", ("2024-01-01",), (), (event,))


def test_empty_and_reversed_history_slices_are_valid_empty_sequences() -> None:
    history = _data().at("2024-01-03").history("A")
    result = history[2:1]
    assert len(result) == 0
    assert list(result) == []


def test_rejects_float_bool_nonfinite_and_duplicate_or_unsorted_records() -> None:
    with pytest.raises(ResearchInputError):
        invalid = Bar("market", "A", "2024-01-01", close=1.0)  # type: ignore[arg-type]
        ResearchData("market", ("A",), "2024-01-01", "2024-01-01", ("2024-01-01",), (invalid,))
    with pytest.raises(ResearchInputError):
        ResearchData(
            "market",
            ("A",),
            "2024-01-01",
            "2024-01-01",
            ("2024-01-01",),
            (Bar("market", "A", "2024-01-01", volume=cast(Decimal, True)),),
        )
    with pytest.raises(ResearchInputError):
        ResearchData(
            "market",
            ("A",),
            "2024-01-01",
            "2024-01-01",
            ("2024-01-01",),
            (Bar("market", "A", "2024-01-01", close=Decimal("NaN")),),
        )
    action = CorporateAction("market", "A", "dividend", "2024-01-01")
    with pytest.raises(ResearchInputError, match="duplicate"):
        ResearchData(
            "market", ("A",), "2024-01-01", "2024-01-01", ("2024-01-01",), (), (action, action)
        )
