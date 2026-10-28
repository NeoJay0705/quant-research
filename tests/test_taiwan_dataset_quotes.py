from __future__ import annotations

from decimal import Decimal

from quant_research.taiwan.dataset.classification import EvidenceSet
from quant_research.taiwan.dataset.quotes import normalize_quote
from quant_research.taiwan.dataset.source import RecordResult, Scope


def test_strict_normalization_preserves_null_raw_values_and_classification() -> None:
    scope = Scope(
        "twse",
        "quotes",
        "q",
        "MI_INDEX",
        "task",
        1,
        "completed",
        "snap",
        "valid",
        {"date": "2024-03-01"},
        1,
        "available",
    )
    payload = {
        "kind": "quote",
        "source": "twse",
        "dataset": "MI_INDEX",
        "symbol": "2330",
        "trade_date": "2024-03-01",
        "open": "10.0100",
        "high": None,
        "low": "9",
        "close": "10.5",
        "volume": "0",
        "amount": None,
        "venue": "一般",
        "venue_id": "primary",
    }
    record = RecordResult(1, "snap", payload, ())
    quote, classification, _, refs = normalize_quote(scope, record, EvidenceSet.from_manifest(None))
    assert (quote.open, quote.high, quote.volume, quote.amount) == (
        Decimal("10.0100"),
        None,
        0,
        None,
    )
    assert classification == "ordinary_rule_limited"
    assert refs == () and quote.refs == ("MI_INDEX:snap:1",)
