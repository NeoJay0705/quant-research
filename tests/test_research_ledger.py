from decimal import ROUND_DOWN, Decimal, Inexact, getcontext

import pytest

from quant_research.research.ledger import Ledger
from quant_research.research.models import (
    Bar,
    CorporateAction,
    ResearchInputError,
    UnsupportedDataError,
)

D = Decimal


def bar(day: str, close: str | None) -> Bar:
    return Bar("TEST", "A", day, close=D(close) if close is not None else None)


def action(day: str, **kwargs: object) -> CorporateAction:
    event_type = str(kwargs.pop("event_type", "corporate_action"))
    return CorporateAction("TEST", "A", event_type, day, **kwargs)  # type: ignore[arg-type]


def test_reduction_and_dividend_nav_neutral_and_future_credit_once() -> None:
    reduction = action(
        "2024-01-02",
        event_type="capital_reduction",
        share_multiplier=D(".9"),
        cash_return_per_share=D("1"),
    )
    ledger = Ledger(D("0"), {"A": D("1000")}, [reduction])
    snap = ledger.process_session("2024-01-02", [bar("2024-01-02", "10")])
    assert snap.positions == () or snap.positions[0].quantity == D("900")
    assert snap.cash == D("1000")
    assert snap.nav == D("10000")

    dividend = action(
        "2024-01-02",
        event_type="dividend",
        dividend=D("1"),
        bonus_share_ratio=D("0"),
        cash_credit_date="2024-01-04",
    )
    ledger = Ledger(D("0"), {"A": D("1000")}, [dividend])
    one = ledger.process_session("2024-01-02", [bar("2024-01-02", "9")])
    assert (one.cash, one.receivables, one.nav) == (D("0"), D("1000"), D("10000"))
    two = ledger.process_session("2024-01-03", [bar("2024-01-03", "9")])
    assert (two.cash, two.receivables, two.nav) == (D("0"), D("1000"), D("10000"))
    three = ledger.process_session("2024-01-05", [bar("2024-01-05", "9")])
    assert (three.cash, three.receivables, three.nav) == (D("1000"), D("0"), D("10000"))


def test_face_ratio_bonus_stale_and_missing_changed_price() -> None:
    face = action("2024-01-02", event_type="par_value", par_before=D("10"), par_after=D("2.5"))
    ledger = Ledger(D("0"), {"A": D("100")}, [face])
    assert ledger.process_session("2024-01-02", [bar("2024-01-02", "2.5")]).positions[
        0
    ].quantity == D("400")
    conflict = action(
        "2024-01-02",
        event_type="par_value",
        share_multiplier=D("2"),
        par_before=D("10"),
        par_after=D("2.5"),
    )
    with pytest.raises(UnsupportedDataError):
        Ledger(D("0"), {"A": D("1")}, [conflict]).process_session(
            "2024-01-02", [bar("2024-01-02", "5")]
        )
    bonus = action(
        "2024-01-02",
        event_type="dividend",
        bonus_share_ratio=D(".25"),
        share_available_date="2024-01-04",
    )
    ledger = Ledger(D("0"), {"A": D("1000")}, [bonus])
    pending = ledger.process_session("2024-01-02", [bar("2024-01-02", "8")])
    assert pending.pending_shares[0].quantity == D("250") and pending.nav == D("10000")
    missing = Ledger(
        D("0"), {"A": D("10")}, [action("2024-01-02", dividend=D("1"), bonus_share_ratio=D("0"))]
    )
    with pytest.raises(UnsupportedDataError):
        missing.process_session("2024-01-02", [bar("2024-01-02", None)])
    stale = Ledger(D("0"), {"A": D("10")})
    stale.process_session("2024-01-02", [bar("2024-01-02", "5")])
    assert stale.process_session("2024-01-03", [bar("2024-01-03", None)]).valuations[0].stale


def test_actual_typed_dividend_and_bonus_share_multiplier_are_not_double_counted() -> None:
    cash = action(
        "2024-01-02",
        event_type="dividend",
        dividend=D("1"),
        bonus_share_ratio=D("0"),
        before_price=D("10"),
        reference_price=D("9"),
    )
    cash_ledger = Ledger(D("0"), {"A": D("1000")}, [cash])
    assert cash_ledger.process_session("2024-01-02", [bar("2024-01-02", "9")]).cash == D("1000")

    bonus = action(
        "2024-01-02",
        event_type="dividend",
        bonus_share_ratio=D(".25"),
        share_multiplier=D("1.25"),
        share_available_date="2024-01-04",
    )
    ledger = Ledger(D("0"), {"A": D("1000")}, [bonus])
    pending = ledger.process_session("2024-01-02", [bar("2024-01-02", "8")])
    assert pending.pending_shares[0].quantity == D("250")
    assert pending.nav == D("10000")
    released = ledger.process_session("2024-01-04", [bar("2024-01-04", "8")])
    assert released.positions[0].quantity == D("1250")
    assert released.pending_shares == ()


def test_two_phase_trades_and_close_snapshots_include_fills() -> None:
    ledger = Ledger(D("1000"))
    update = ledger.begin_session("2024-01-02")
    assert update.session == "2024-01-02" and update.changed_symbols == ()
    ledger.apply_buy("A", D("10"), D("10"), D(".1"))
    first = ledger.close_session("2024-01-02", [bar("2024-01-02", "11")])
    assert (first.cash, first.positions[0].quantity, first.nav) == (
        D("899.9"),
        D("10"),
        D("1009.9"),
    )
    ledger.begin_session("2024-01-03")
    ledger.apply_sell("A", D("10"), D("12"), D(".48"))
    second = ledger.close_session("2024-01-03", [bar("2024-01-03", "12")])
    assert (second.cash, second.positions, second.nav) == (D("1019.42"), (), D("1019.42"))
    assert ledger.snapshots == (first, second)


def test_hostile_context_does_not_affect_ratio_or_leak() -> None:
    ctx = getcontext()
    old = ctx.copy()
    try:
        ctx.prec = 2
        ctx.Emax = 2
        ctx.traps[Inexact] = True
        face = action("2024-01-02", event_type="par_value", par_before=D("1"), par_after=D("3"))
        snap = Ledger(D("0"), {"A": D("3")}, [face]).process_session(
            "2024-01-02", [bar("2024-01-02", "1")]
        )
        assert snap.positions[0].quantity == D("0." + "9" * 50)
        assert ctx.prec == 2 and ctx.Emax == 2 and ctx.traps[Inexact]
    finally:
        ctx.prec, ctx.Emax = old.prec, old.Emax
        ctx.traps = old.traps.copy()


def test_exact_cash_capital_increase_marker_is_rejected() -> None:
    marked = action(
        "2024-01-02",
        event_type="par_value",
        share_multiplier=D("1"),
        quality_flags=("cash_capital_increase_present",),
    )
    with pytest.raises(UnsupportedDataError):
        Ledger(D("0"), {"A": D("1")}, [marked]).begin_session("2024-01-02")


def test_calendar_order_distinct_actions_and_actual_release_audit() -> None:
    sat = action(
        "2024-01-06",
        event_type="dividend",
        dividend=D("1"),
        bonus_share_ratio=D("0"),
        cash_credit_date="2024-01-08",
    )
    sun = action(
        "2024-01-07",
        event_type="dividend",
        dividend=D("2"),
        bonus_share_ratio=D("0"),
        cash_credit_date="2024-01-08",
    )
    ledger = Ledger(D("0"), {"A": D("1")}, [sun, sat])
    friday = ledger.process_session("2024-01-05", [bar("2024-01-05", "10")])
    monday = ledger.begin_session("2024-01-08")
    assert friday.cash == 0
    assert [(e.kind, e.record_date, e.effective_date) for e in monday.events] == [
        ("action", "2024-01-06", "2024-01-06"),
        ("action", "2024-01-07", "2024-01-07"),
        ("cash_release", "2024-01-08", "2024-01-06"),
        ("cash_release", "2024-01-08", "2024-01-07"),
    ]
    assert ledger.cash == D("3")
    ledger.close_session("2024-01-08", [bar("2024-01-08", "7")])
    assert len([e for e in ledger.events if e.kind == "cash_release"]) == 2


def test_release_is_not_visible_early_and_bonus_waits_until_delivery() -> None:
    dividend = action(
        "2024-01-06",
        event_type="dividend",
        dividend=D("1"),
        bonus_share_ratio=D(".25"),
        cash_credit_date="2024-01-07",
        share_available_date="2024-01-09",
    )
    ledger = Ledger(D("0"), {"A": D("4")}, [dividend])
    ledger.process_session("2024-01-05", [bar("2024-01-05", "10")])
    saturday = ledger.begin_session("2024-01-06")
    assert ledger.cash == 0 and ledger.positions[0].quantity == D("4")
    assert not any(e.kind == "cash_release" for e in saturday.events)
    ledger.close_session("2024-01-06", [bar("2024-01-06", "9")])
    sunday = ledger.process_session("2024-01-07", [bar("2024-01-07", "9")])
    assert sunday.cash == D("4") and sunday.pending_shares[0].quantity == D("1")
    assert any(e.kind == "cash_release" and e.record_date == "2024-01-07" for e in sunday.events)
    release = ledger.process_session("2024-01-09", [bar("2024-01-09", "9")])
    assert release.positions[0].quantity == D("5")
    assert any(e.kind == "share_release" and e.record_date == "2024-01-09" for e in release.events)


def test_pending_bonus_shares_crossing_unit_change_rejected_after_originals_sold() -> None:
    bonus = action(
        "2024-01-02",
        event_type="dividend",
        bonus_share_ratio=D(".25"),
        share_available_date="2024-01-10",
    )
    par_change = action(
        "2024-01-03",
        event_type="par_value",
        par_before=D("2"),
        par_after=D("1"),
    )
    ledger = Ledger(D("0"), {"A": D("1000")}, [bonus, par_change])
    ledger.begin_session("2024-01-02")
    ledger.apply_sell("A", D("1000"), D("8"))
    ledger.close_session("2024-01-02", [bar("2024-01-02", "8")])

    with pytest.raises(UnsupportedDataError, match="pending shares cross another stock change"):
        ledger.begin_session("2024-01-03")


def test_zero_bonus_cash_does_not_flag_units_or_require_fresh_close() -> None:
    cash = action("2024-01-02", event_type="dividend", dividend=D("0"), bonus_share_ratio=D("0"))
    ledger = Ledger(D("0"), {"A": D("1")}, [cash])
    ledger.process_session("2024-01-01", [bar("2024-01-01", "1")])
    update = ledger.begin_session("2024-01-02")
    assert update.changed_symbols == ()
    snapshot = ledger.close_session("2024-01-02", [bar("2024-01-02", None)])
    assert snapshot.valuations[0].stale


def test_record_date_entitlement_precedes_day_trades_and_reduction_requires_ratio() -> None:
    dividend = action(
        "2024-01-02",
        event_type="dividend",
        dividend=D("1"),
        bonus_share_ratio=D("0"),
    )
    ledger = Ledger(D("10"), events=[dividend])
    ledger.begin_session("2024-01-02")
    ledger.apply_buy("A", D("1"), D("5"))
    snap = ledger.close_session("2024-01-02", [bar("2024-01-02", "5")])
    assert snap.cash == D("5")  # Buy on the ex-date receives no dividend.

    ledger = Ledger(D("0"), {"A": D("1")}, [dividend])
    ledger.begin_session("2024-01-02")
    ledger.apply_sell("A", D("1"), D("5"))
    snap = ledger.close_session("2024-01-02", [bar("2024-01-02", "5")])
    assert snap.cash == D("6")  # Sale proceeds plus the already accrued dividend.

    reduction = action("2024-01-02", event_type="capital_reduction")
    with pytest.raises(UnsupportedDataError, match="requires share_multiplier"):
        Ledger(D("0"), {"A": D("1")}, [reduction]).begin_session("2024-01-02")


def test_sell_cost_may_be_funded_by_existing_cash() -> None:
    ledger = Ledger(D("5"), {"A": D("1")})
    ledger.apply_sell("A", D("1"), D("2"), D("4"))
    assert ledger.cash == D("3")
    unfunded = Ledger(D("1"), {"A": D("1")})
    with pytest.raises(ResearchInputError, match="insufficient cash"):
        unfunded.apply_sell("A", D("1"), D("2"), D("4"))


def test_zero_holdings_pending_unit_change_cancels_intent_without_booking_entitlement() -> None:
    events = (
        action("2024-01-02", event_type="par_value", par_before=D("10"), par_after=D("2.5")),
        action(
            "2024-01-02",
            event_type="dividend",
            bonus_share_ratio=D(".25"),
        ),
        action(
            "2024-01-02",
            event_type="dividend",
            dividend=D("1"),
            bonus_share_ratio=D("0"),
        ),
    )
    for event in events:
        ledger = Ledger(D("50"), events=[event])
        update = ledger.begin_session("2024-01-02", pending_symbols=("A",))
        expected = ("A",) if event is not events[2] else ()
        assert update.changed_symbols == expected
        assert ledger.positions == ()
        assert ledger.cash == D("50")
        assert not ledger.events


def test_zero_holdings_without_intent_ignore_unsupported_unit_marker() -> None:
    marked = action(
        "2024-01-02",
        event_type="par_value",
        quality_flags=("cash_capital_increase_present",),
    )
    update = Ledger(D("50"), events=[marked]).begin_session("2024-01-02")
    assert update.changed_symbols == ()


def test_fractional_reduction_changes_quantity_by_exact_multiplier() -> None:
    reduction = action(
        "2024-01-02",
        event_type="capital_reduction",
        share_multiplier=D(".66195765"),
        cash_return_per_share=D("1"),
    )
    snapshot = Ledger(D("0"), {"A": D("1000")}, [reduction]).process_session(
        "2024-01-02", [bar("2024-01-02", "10")]
    )
    assert snapshot.positions[0].quantity == D("661.95765000")
    assert snapshot.cash == D("1000")


def test_ambiguous_same_day_actions_reject_held_symbol() -> None:
    actions = [
        action("2024-01-02", event_type="par_value", par_before=D("10"), par_after=D("5")),
        action("2024-01-02", event_type="dividend", dividend=D("1"), bonus_share_ratio=D("0")),
    ]
    with pytest.raises(UnsupportedDataError, match="ambiguous same-day"):
        Ledger(D("0"), {"A": D("1")}, actions).begin_session("2024-01-02")


def test_fractional_quantity_context_and_helpers() -> None:
    old = getcontext().rounding
    getcontext().rounding = ROUND_DOWN
    try:
        ledger = Ledger(D("10000"))
        ledger.apply_buy("A", D("1"), D("1"))
        assert ledger.cash == D("9999")
    finally:
        getcontext().rounding = old
    dividend = action(
        "2024-01-02", event_type="dividend", dividend=D(".66195765"), bonus_share_ratio=D("0")
    )
    ledger = Ledger(D("0"), {"A": D("1")}, [dividend])
    record = ledger.process_session("2024-01-02", [bar("2024-01-02", "1")]).events[0]
    assert record.cash_amount == D(".66195765")
