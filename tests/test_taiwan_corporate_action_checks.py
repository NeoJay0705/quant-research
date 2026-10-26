from __future__ import annotations

from decimal import Decimal, Inexact, Rounded, getcontext, localcontext

import pytest

from quant_research.taiwan.corporate_actions import build_corporate_actions


def run(kind, values, source="twse", dataset=None, *, symbol="2330", event_date="2024-01-02"):
    dataset = (
        dataset
        or {
            ("twse", "dividend"): "TWT49U",
            ("twse", "capital_reduction"): "TWTAUU",
            ("twse", "par_value"): "TWTB8U",
            ("tpex", "dividend"): "exDailyQ",
            ("tpex", "capital_reduction"): "revivt",
            ("tpex", "par_value"): "pvChgRslt",
        }[(source, kind)]
    )
    task = {
        "task_id": "t",
        "logical_key": "scope",
        "edition": 1,
        "source": source,
        "dataset": dataset,
        "kind": "official_event_summary",
        "state": "completed",
        "snapshot_id": "s",
        "validation_state": "valid",
        "record_count": 1,
        "params": {
            "pipeline": "official-v1",
            "lane": "events",
            "request": {"start_date": "2020-01-01", "end_date": "2030-01-01"},
        },
    }
    kind_label = {
        ("twse", "dividend"): "除權息參考價",
        ("tpex", "dividend"): "除權息參考價",
        ("twse", "capital_reduction"): "恢復買賣參考價",
        ("twse", "par_value"): "恢復買賣參考價",
        ("tpex", "capital_reduction"): "減資恢復買賣開始日參考價格",
        ("tpex", "par_value"): "恢復買賣開始參考價",
    }[(source, kind)]
    payload = {
        "kind": "event",
        "observation_type": "summary",
        "source": source,
        "dataset": dataset,
        "event_type": kind,
        "symbol": symbol,
        "event_date": event_date,
        "reference_price_kind": kind_label,
        **values,
    }
    report = build_corporate_actions(
        [{"record_id": "r", "snapshot_id": "s", "payload": payload}], [], [task]
    )
    return report["actions"][0]


def test_simple_reference_formulae_and_independent_decimal_context():
    cases = [
        (
            "dividend",
            {
                "cash_dividend_per_share": "8",
                "bonus_share_ratio": "0",
                "before_price": "227.5",
                "reference_price": "219.5",
            },
            "twse",
        ),
        (
            "dividend",
            {
                "cash_dividend_per_share": "7",
                "bonus_share_ratio": "0",
                "before_price": "217",
                "reference_price": "210",
            },
            "twse",
        ),
        (
            "dividend",
            {
                "cash_dividend_per_share": "0",
                "bonus_share_ratio": "0.05019058194",
                "before_price": "19.25",
                "reference_price": "18.33",
            },
            "tpex",
        ),
        (
            "capital_reduction",
            {
                "share_multiplier": "0.8",
                "cash_return_per_share": "2",
                "cash_dividend_per_share": "0",
                "before_price": "14.7",
                "reference_price": "15.87",
            },
            "twse",
        ),
        (
            "capital_reduction",
            {
                "share_multiplier": "0.95",
                "cash_return_per_share": "0.5",
                "cash_dividend_per_share": "0",
                "before_price": "25.25",
                "reference_price": "26.05",
            },
            "twse",
        ),
        (
            "par_value",
            {
                "share_multiplier": "2",
                "par_before": "10",
                "par_after": "5",
                "before_price": "169",
                "reference_price": "84.5",
            },
            "tpex",
        ),
        (
            "par_value",
            {
                "share_multiplier": "2.5",
                "par_before": "1",
                "par_after": "0.4",
                "before_price": "90.6",
                "reference_price": "36.24",
            },
            "tpex",
        ),
        (
            "par_value",
            {
                "share_multiplier": "2",
                "par_before": "10",
                "par_after": "5",
                "before_price": "206",
                "reference_price": "103",
            },
            "tpex",
        ),
        (
            "par_value",
            {
                "share_multiplier": "4",
                "par_before": "10",
                "par_after": "2.5",
                "before_price": "109.5",
                "reference_price": "27.38",
            },
            "tpex",
        ),
        (
            "par_value",
            {
                "share_multiplier": "4",
                "par_before": "10",
                "par_after": "2.5",
                "before_price": "2485",
                "reference_price": "621.25",
            },
            "twse",
        ),
    ]
    original_precision = getcontext().prec
    with localcontext() as ctx:
        ctx.prec = 2
        for kind, values, source in cases:
            action = run(kind, values, source)
            assert action["reference_check"]["status"] == "match"
            assert action["input_state"] == "complete"
            if values.get("bonus_share_ratio") == "0.05019058194":
                assert action["share_multiplier"] == "1.05019058194"
        assert Decimal("1") / Decimal("3") == Decimal("0.33")
    assert getcontext().prec == original_precision


def test_reference_kind_whitelist_missing_fields_and_par_conflict():
    bad_kind = run(
        "par_value",
        {
            "share_multiplier": "4",
            "par_before": "10",
            "par_after": "2.5",
            "before_price": "2485",
            "reference_price": "621",
            "reference_price_kind": "開盤競價基準",
        },
    )
    assert bad_kind["reference_check"]["status"] == "not_checked"
    assert bad_kind["reference_check"]["reasons"] == [
        "input_state_not_complete",
        "unknown_reference_price_kind",
    ]
    assert bad_kind["input_state"] == "incomplete"
    match = run(
        "par_value",
        {
            "share_multiplier": "4",
            "par_before": "10",
            "par_after": "2.5",
            "before_price": "2485",
            "reference_price": "621.25",
        },
    )
    mismatch = run(
        "par_value",
        {
            "share_multiplier": "4",
            "par_before": "10",
            "par_after": "2.5",
            "before_price": "2485",
            "reference_price": "621",
        },
    )
    assert match["reference_check"]["status"] == "match"
    assert mismatch["reference_check"]["status"] == "mismatch"
    no_multiplier = run(
        "par_value",
        {"par_before": "10", "par_after": "2.5", "before_price": "100", "reference_price": "25"},
    )
    assert "share_multiplier" in no_multiplier["missing_fields"]
    assert no_multiplier["reference_check"]["status"] == "not_checked"
    mismatch = run(
        "par_value",
        {
            "share_multiplier": "2.5",
            "par_before": "10",
            "par_after": "5",
            "before_price": "100",
            "reference_price": "40",
        },
    )
    assert mismatch["input_state"] == "conflict"
    assert mismatch["reference_check"]["status"] == "not_checked"
    assert mismatch["share_multiplier"] == "2.5"
    assert mismatch["par_value_check"] == {
        "status": "mismatch",
        "expected": "2",
        "observed": "2.5",
        "relative_tolerance": "1e-8",
    }


def test_exact_par_tolerance_boundaries():
    values = {
        "par_before": "2",
        "par_after": "3",
        "before_price": "100",
        "reference_price": "100",
    }
    boundary = run("par_value", {**values, "share_multiplier": "0.66666666"})
    below = run("par_value", {**values, "share_multiplier": "0.66666665"})
    above = run("par_value", {**values, "share_multiplier": "0.66666667"})

    assert boundary["par_value_check"]["status"] == "match"
    assert boundary["par_value_check"]["expected"] is not None
    assert boundary["par_before"] == "2" and boundary["par_after"] == "3"
    assert boundary["share_multiplier"] == "0.66666666"
    assert boundary["input_state"] == "complete"
    assert below["par_value_check"]["status"] == "mismatch"
    assert below["input_state"] == "conflict"
    assert above["par_value_check"]["status"] == "match"
    assert above["input_state"] == "complete"


def test_exact_reference_tolerance_for_large_values_and_decimal_boundary():
    huge = "1" + "0" * 63 + "e64"
    large = run(
        "dividend",
        {
            "cash_dividend_per_share": "100",
            "bonus_share_ratio": "0",
            "before_price": huge,
            "reference_price": huge,
        },
    )
    assert large["input_state"] == "complete"
    assert large["cash_dividend_per_share"] == "100"
    assert large["bonus_share_ratio"] == "0"
    assert Decimal(large["before_price"]) == Decimal(large["reference_price"]) == Decimal(huge)
    assert large["reference_check"]["status"] == "mismatch"
    assert large["reference_check"]["difference"] == "100"

    exact = run(
        "dividend",
        {
            "cash_dividend_per_share": "0",
            "bonus_share_ratio": "0",
            "before_price": "100",
            "reference_price": "100.01",
        },
    )
    beyond = run(
        "dividend",
        {
            "cash_dividend_per_share": "0",
            "bonus_share_ratio": "0",
            "before_price": "100",
            "reference_price": "100.01" + "0" * 40 + "1",
        },
    )
    assert exact["input_state"] == beyond["input_state"] == "complete"
    assert exact["reference_check"]["status"] == "match"
    assert beyond["reference_check"]["status"] == "mismatch"


def test_external_decimal_traps_and_flags_are_isolated():
    context = getcontext()
    with localcontext() as external:
        external.prec = 2
        external.Emax = 9
        external.Emin = -9
        external.traps[Inexact] = True
        external.traps[Rounded] = True
        external.clear_flags()
        before = external.copy()
        action = run(
            "dividend",
            {
                "cash_dividend_per_share": "not-a-number",
                "bonus_share_ratio": "0",
                "before_price": "10",
                "reference_price": "9",
            },
        )
        par = run(
            "par_value",
            {
                "par_before": "2",
                "par_after": "3",
                "share_multiplier": "0.66666666",
                "before_price": "100",
                "reference_price": "150",
            },
        )
        huge = "1" + "0" * 63 + "e64"
        division = run(
            "dividend",
            {
                "cash_dividend_per_share": "100",
                "bonus_share_ratio": "0",
                "before_price": huge,
                "reference_price": huge,
            },
        )
        after = getcontext()
        assert action["input_state"] == "incomplete"
        assert action["reference_check"]["status"] == "not_checked"
        assert par["par_value_check"]["status"] == "match"
        assert par["input_state"] == "complete"
        assert par["reference_check"]["status"] == "match"
        assert division["input_state"] == "complete"
        assert division["reference_check"]["status"] == "mismatch"
        assert division["reference_check"]["difference"] == "100"
        assert after.prec == before.prec
        assert after.Emax == before.Emax and after.Emin == before.Emin
        assert after.traps == before.traps
        assert after.flags == before.flags
    assert getcontext() is context


def test_large_exponent_is_invalid_and_transaction_wide_context_is_restored():
    action = run(
        "dividend",
        {
            "cash_dividend_per_share": "1e999999999",
            "bonus_share_ratio": "0",
            "before_price": "10",
            "reference_price": "9",
        },
    )
    assert action["cash_dividend_per_share"] is None
    assert "invalid_financial_value" in action["quality_flags"]


def test_quality_flags_block_completion_without_legacy_boolean_fields():
    for flag in ("cash_capital_increase_present", "employee_bonus_shares_present"):
        action = run(
            "dividend",
            {
                "cash_dividend_per_share": "8",
                "bonus_share_ratio": "0",
                "before_price": "100",
                "reference_price": "92",
                "quality_flags": [flag],
            },
        )
        assert action["input_state"] == "incomplete"
        assert flag in action["quality_flags"]
        assert action["reference_check"]["status"] == "not_checked"
        assert flag in action["reference_check"]["reasons"]


def test_invalid_explicit_multiplier_is_not_replaced_by_bonus_derivation():
    action = run(
        "dividend",
        {
            "cash_dividend_per_share": "0",
            "bonus_share_ratio": "0.2",
            "share_multiplier": "0",
            "before_price": "12",
            "reference_price": "10",
        },
    )
    assert action["share_multiplier"] is None
    assert "invalid_financial_value" in action["quality_flags"]


@pytest.mark.parametrize("bonus", ["0", "1000"])
def test_dividend_bonus_thousand_share_counterexamples_do_not_match(bonus):
    action = run(
        "dividend",
        {
            "cash_dividend_per_share": "0",
            "bonus_share_ratio": bonus,
            "before_price": "110",
            "reference_price": "100",
        },
    )
    assert action["reference_check"]["status"] == "mismatch"


def test_inverse_multiplier_and_ignored_cash_return_are_not_accepted():
    inverse = run(
        "par_value",
        {
            "share_multiplier": "0.4",
            "par_before": "10",
            "par_after": "4",
            "before_price": "100",
            "reference_price": "250",
        },
    )
    assert inverse["par_value_check"]["status"] == "mismatch"
    assert inverse["input_state"] == "conflict"
    assert inverse["reference_check"]["status"] == "not_checked"
    ignored_return = run(
        "capital_reduction",
        {
            "share_multiplier": "0.8",
            "cash_return_per_share": "10",
            "cash_dividend_per_share": "0",
            "before_price": "100",
            "reference_price": "125",
        },
    )
    assert ignored_return["reference_check"]["status"] == "mismatch"


def test_nonpositive_theoretical_price_is_not_checked_as_a_normal_reference():
    action = run(
        "dividend",
        {
            "cash_dividend_per_share": "11",
            "bonus_share_ratio": "0",
            "before_price": "10",
            "reference_price": "1",
        },
    )
    assert action["reference_check"]["status"] == "not_checked"
    assert action["reference_check"]["reasons"] == ["nonpositive_theoretical_reference_price"]
    assert action["input_state"] == "incomplete"


@pytest.mark.parametrize(
    ("kind", "values", "preserved"),
    [
        (
            "par_value",
            {
                "share_multiplier": "2",
                "par_before": "10",
                "par_after": "5",
                "cash_dividend_per_share": "5",
                "before_price": "100",
                "reference_price": "50",
            },
            {"cash_dividend_per_share": "5", "share_multiplier": "2"},
        ),
        (
            "dividend",
            {
                "cash_dividend_per_share": "8",
                "bonus_share_ratio": "0",
                "cash_return_per_share": "2",
                "before_price": "100",
                "reference_price": "92",
            },
            {"cash_dividend_per_share": "8", "cash_return_per_share": "2"},
        ),
        (
            "capital_reduction",
            {
                "share_multiplier": "0.8",
                "cash_return_per_share": "2",
                "cash_dividend_per_share": "0",
                "bonus_share_ratio": "0.1",
                "before_price": "100",
                "reference_price": "122.5",
            },
            {"share_multiplier": "0.8", "cash_return_per_share": "2", "bonus_share_ratio": "0.1"},
        ),
        (
            "dividend",
            {
                "cash_dividend_per_share": "0",
                "bonus_share_ratio": "1",
                "share_multiplier": "2",
                "par_before": "10",
                "par_after": "5",
                "before_price": "100",
                "reference_price": "50",
            },
            {
                "bonus_share_ratio": "1",
                "share_multiplier": "2",
                "par_before": "10",
                "par_after": "5",
            },
        ),
    ],
)
def test_nonzero_unmodeled_embedded_financial_components_are_not_ignored(kind, values, preserved):
    action = run(kind, values)
    assert action["input_state"] == "incomplete"
    assert "unmodeled_embedded_component" in action["quality_flags"]
    assert action["reference_check"]["status"] == "not_checked"
    assert "unmodeled_embedded_component" in action["reference_check"]["reasons"]
    for field, value in preserved.items():
        assert action[field] == value


def test_supported_reduction_cash_dividend_still_matches_and_zero_or_unknown_is_not_flagged():
    supported = run(
        "capital_reduction",
        {
            "share_multiplier": "0.8",
            "cash_return_per_share": "2",
            "cash_dividend_per_share": "3",
            "before_price": "100",
            "reference_price": "118.75",
        },
    )
    assert supported["reference_check"]["status"] == "match"
    assert supported["cash_dividend_per_share"] == "3"
    assert "unmodeled_embedded_component" not in supported["quality_flags"]
    for amount in ("0", None):
        neutral = run(
            "capital_reduction",
            {
                "share_multiplier": "0.8",
                "cash_return_per_share": "2",
                "cash_dividend_per_share": amount,
                "before_price": "100",
                "reference_price": "122.5",
            },
        )
        assert "unmodeled_embedded_component" not in neutral["quality_flags"]
