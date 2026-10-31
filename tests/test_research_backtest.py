import hashlib
from dataclasses import replace
from decimal import Decimal as D
from decimal import Inexact, localcontext

import pytest

from quant_research.research.backtest import CostModel, MarketOrder, backtest, target_weights
from quant_research.research.data import ResearchData
from quant_research.research.features import compute_feature
from quant_research.research.models import (
    Bar,
    CorporateAction,
    ResearchInputError,
    UnsupportedDataError,
)


def data(rows):
    calendar = ("2024-01-01", "2024-01-02", "2024-01-03", "2024-01-04")
    days = calendar[: len(next(iter(rows.values())))]
    bars = tuple(
        Bar(
            "m",
            s,
            day,
            open=D(op) if op else None,
            close=D(cl),
            classification="ordinary_evidence_supported",
        )
        for s, vals in sorted(rows.items())
        for day, (op, cl) in zip(days, vals, strict=True)
    )
    return ResearchData("m", tuple(rows), days[0], days[-1], days, bars)


ZERO = CostModel(D(0), D(0), D(0), D(0))


def run(d, strategy, costs=ZERO, cash=D(1000)):
    return backtest(
        d, strategy, initial_cash=cash, costs=costs, start_date="2024-01-01", end_date=d.end_date
    )


def test_next_open_hand_calculation_and_fees():
    d = data({"A": (("10", "10"), ("10", "11"), ("12", "12"))})

    def strat(v):
        return (
            [MarketOrder("A", "buy", D(10))]
            if v.date.endswith("01")
            else [MarketOrder("A", "sell", D(10))]
            if v.date.endswith("02")
            else []
        )

    r = run(d, strat, CostModel(D(".001"), D(0), D(".003"), D(0)))
    assert [(s.cash, s.nav) for s in r.snapshots] == [
        (D(1000), D(1000)),
        (D("899.9"), D("1009.9")),
        (D("1019.42"), D("1019.42")),
    ]
    assert sum((f.fee + f.sell_tax for f in r.fills), D(0)) == D(".58")
    assert [f.price for f in r.fills] == [D(10), D(12)]


def test_missing_open_not_carried_and_end_intent_rejected():
    d = data({"A": (("10", "10"), (None, "11"), ("12", "12"))})
    r = run(
        d,
        lambda v: [MarketOrder("A", "buy", D(1))] if v.date == "2024-01-01" else [],
    )
    assert [(x.session, x.reason) for x in r.rejections] == [
        ("2024-01-02", "missing_or_nonpositive_open")
    ]
    assert not r.fills


def test_overbudget_buy_batch_is_all_or_nothing_and_order_independent():
    d = data({"A": (("1", "1"), ("1", "1"), ("1", "1")), "B": (("1", "1"), ("1", "1"), ("1", "1"))})
    results = []
    for sequence in (("A", "B"), ("B", "A")):
        r = run(d, lambda v, seq=sequence: [MarketOrder(s, "buy", D(600)) for s in seq])
        results.append([(x.order.symbol, x.reason) for x in r.rejections])
        assert r.snapshots[1].cash == D(1000)
    assert results[0] == results[1]


def test_slippage_and_bad_inputs():
    d = data({"A": (("10", "10"), ("10", "11"), ("12", "12"))})
    r = run(
        d,
        lambda v: [MarketOrder("A", "buy", D(1))] if v.date.endswith("01") else [],
        CostModel(D(0), D(".25"), D(0), D(10)),
    )
    assert r.fills[0].price == D("10.01") and r.fills[0].fee == D(".25")
    assert r.fills[0].slippage_cost == D(".01")
    with pytest.raises(ResearchInputError):
        CostModel(D(-1), D(0), D(0), D(0))
    with pytest.raises(ResearchInputError):
        run(d, lambda v: [], cash=D(0))


def test_dividend_payload_does_not_create_stop_interval():
    d = data({"A": (("10", "10"), ("10", "10"), ("10", "10"))})
    event = CorporateAction(
        "m", "A", "dividend", "2024-01-02", bonus_share_ratio=D(0), payload=(("original", "x"),)
    )
    d = ResearchData(d.market, d.symbols, d.start_date, d.end_date, d.sessions, d.bars, (event,))
    r = run(d, lambda v: [MarketOrder("A", "buy", D(1))] if v.date == "2024-01-01" else [])
    assert [(f.session, f.order.side) for f in r.fills] == [("2024-01-02", "buy")]
    assert not r.rejections


def test_explicit_stop_interval_excludes_effective_date():
    d = data({"A": (("10", "10"),) * 4})
    event = CorporateAction(
        "m",
        "A",
        "dividend",
        "2024-01-04",
        bonus_share_ratio=D(0),
        payload=(("stop_date", "2024-01-02"),),
    )
    d = ResearchData(d.market, d.symbols, d.start_date, d.end_date, d.sessions, d.bars, (event,))
    r = run(d, lambda v: [MarketOrder("A", "buy", D(1))] if v.date != "2024-01-04" else [])
    assert [(x.session, x.reason) for x in r.rejections] == [
        ("2024-01-02", "known_stop_interval"),
        ("2024-01-03", "known_stop_interval"),
    ]
    assert [(f.session, f.order.side) for f in r.fills] == [("2024-01-04", "buy")]


def test_explicit_missing_stop_date_is_not_an_interval():
    d = data({"A": (("10", "10"),) * 3})
    event = CorporateAction(
        "m", "A", "dividend", "2024-01-03", bonus_share_ratio=D(0), payload=(("stop_date", None),)
    )
    d = ResearchData(d.market, d.symbols, d.start_date, d.end_date, d.sessions, d.bars, (event,))
    r = run(d, lambda v: [MarketOrder("A", "buy", D(1))] if v.date == "2024-01-01" else [])
    assert [(f.session, f.order.side) for f in r.fills] == [("2024-01-02", "buy")]
    assert not r.rejections


def test_malformed_stop_date_is_not_ignored():
    d = data({"A": (("10", "10"),) * 3})
    event = CorporateAction(
        "m", "A", "dividend", "2024-01-03", payload=(("stop_date", "20240102"),)
    )
    with pytest.raises(ResearchInputError, match="canonical"):
        ResearchData(d.market, d.symbols, d.start_date, d.end_date, d.sessions, d.bars, (event,))


def test_negative_net_sale_is_funded_by_positive_net_sale_order_independently():
    d = data(
        {
            "A": (("10", "10"), ("10", "10"), ("1", "1")),
            "B": (("10", "10"), ("10", "10"), ("10", "10")),
        }
    )
    costs = CostModel(D(0), D(5), D(0), D(0))
    results = []
    for order in (("A", "B"), ("B", "A")):
        r = run(
            d,
            lambda v, seq=order: (
                [MarketOrder(s, "buy", D(1)) for s in ("A", "B")]
                if v.date == "2024-01-01"
                else [MarketOrder(s, "sell", D(1)) for s in seq]
                if v.date == "2024-01-02"
                else []
            ),
            costs,
            D(30),
        )
        results.append((r.snapshots[2].cash, tuple((f.order.symbol, f.fee) for f in r.fills[-2:])))
    assert results == [(D(1), (("B", D(5)), ("A", D(5))))] * 2


def test_same_open_sale_proceeds_fund_buy_and_slippage_is_positive_not_double_debited():
    d = data({"A": (("10", "10"), ("10", "10"), ("10", "10")), "B": (("10", "10"),) * 3})
    r = run(
        d,
        lambda v: (
            [MarketOrder("A", "buy", D(1))]
            if v.date == "2024-01-01"
            else [MarketOrder("A", "sell", D(1)), MarketOrder("B", "buy", D(1))]
            if v.date == "2024-01-02"
            else []
        ),
        CostModel(D(0), D(0), D(0), D(10)),
        D(20),
    )
    assert [(f.order.side, f.price, f.slippage_cost) for f in r.fills] == [
        ("buy", D("10.01"), D(".01")),
        ("sell", D("9.99"), D(".01")),
        ("buy", D("10.01"), D(".01")),
    ]
    assert r.snapshots[2].cash == D("9.970")
    assert r.snapshots[2].nav == D("19.970")


def test_slippage_is_positive_cost_separate_from_fee_and_tax():
    d = data({"A": (("10", "10"), ("10", "10"), ("10", "10"))})
    r = run(
        d,
        lambda v: (
            [MarketOrder("A", "buy", D(1))]
            if v.date == "2024-01-01"
            else [MarketOrder("A", "sell", D(1))]
            if v.date == "2024-01-02"
            else []
        ),
        CostModel(D(".001"), D(".25"), D(".02"), D(10)),
        D(100),
    )
    buy, sell = r.fills
    assert (buy.price, buy.notional, buy.fee, buy.sell_tax, buy.slippage_cost) == (
        D("10.01"),
        D("10.01"),
        D(".25"),
        D(0),
        D(".01"),
    )
    assert (sell.price, sell.notional, sell.fee, sell.sell_tax, sell.slippage_cost) == (
        D("9.99"),
        D("9.99"),
        D(".25"),
        D(".1998"),
        D(".01"),
    )
    assert buy.slippage_cost + sell.slippage_cost == D(".02")
    assert r.snapshots[1].nav == D("99.74")
    assert r.snapshots[2].cash == r.snapshots[2].nav == D("99.2802")


def test_zero_cost_buy_hold_sell_nav_hand_calculation():
    d = data({"A": (("10", "10"), ("10", "11"), ("12", "12"))})
    r = run(
        d,
        lambda v: (
            [MarketOrder("A", "buy", D(10))]
            if v.date == "2024-01-01"
            else [MarketOrder("A", "sell", D(10))]
            if v.date == "2024-01-02"
            else []
        ),
        cash=D(1000),
    )
    assert [(s.cash, s.nav) for s in r.snapshots] == [
        (D(1000), D(1000)),
        (D(900), D(1010)),
        (D(1020), D(1020)),
    ]


def test_params_runtime_copies_and_frozen_initial_values():
    d = data({"A": (("10", "10"), ("10", "10"), ("10", "10"))})
    caller = {"empty_tuple": (), "empty_list": [], "nested": {"items": [1, {"x": 2}]}}
    observed = []

    def strategy(view, **runtime):
        observed.append((type(runtime["empty_tuple"]), type(runtime["empty_list"])))
        runtime["empty_list"].append("callback")
        runtime["nested"]["items"][1]["x"] = 99
        runtime["new"] = "callback"
        return []

    result = backtest(
        d,
        strategy,
        initial_cash=D(10),
        costs=ZERO,
        start_date="2024-01-01",
        end_date=d.end_date,
        params=caller,
    )
    assert caller == {"empty_tuple": (), "empty_list": [], "nested": {"items": [1, {"x": 2}]}}
    assert observed == [(tuple, list)] * 3
    assert result.params == (
        ("empty_list", ("list", ())),
        ("empty_tuple", ("tuple", ())),
        ("nested", ("mapping", (("items", ("list", (1, ("mapping", (("x", 2),))))),))),
    )
    with pytest.raises(ResearchInputError, match="params"):
        backtest(
            d,
            lambda view: [],
            initial_cash=D(10),
            costs=ZERO,
            start_date="2024-01-01",
            end_date=d.end_date,
            params=[],
        )


def test_frozen_parameter_digest_preserves_runtime_types():
    d = data({"A": (("10", "10"),) * 3})
    values = ([], (), {}, D("1"), "1", True, 1)
    results = [
        backtest(
            d,
            lambda view, **kwargs: [],
            initial_cash=D(10),
            costs=ZERO,
            start_date="2024-01-01",
            end_date=d.end_date,
            params={"value": value},
        )
        for value in values
    ]
    assert len({result.params_digest for result in results}) == len(values)


def test_program_hash_precedes_callback_and_feature_values_are_hashed(tmp_path):
    d = data({"A": (("10", "10"), ("10", "10"), ("10", "10"))})
    program = tmp_path / "strategy.py"
    original = b"initial program bytes"
    program.write_bytes(original)
    prior_hash = hashlib.sha256(original).hexdigest()

    def rewrite(view):
        program.write_bytes(b"rewritten by callback")
        return []

    result = backtest(
        d,
        rewrite,
        initial_cash=D(100),
        costs=ZERO,
        start_date="2024-01-01",
        end_date=d.end_date,
        program_path=program,
    )
    assert result.strategy_sha256 == prior_hash
    assert result.program_path == str(program.resolve())
    assert program.read_bytes() == b"rewritten by callback"

    def feature(value):
        return compute_feature(d, lambda view: {"A": D(value)}, name="signal")

    low = feature("1")
    high = feature("2")

    def run_feature(series):
        return backtest(
            d,
            lambda view: [],
            initial_cash=D(100),
            costs=ZERO,
            start_date="2024-01-01",
            end_date=d.end_date,
            features={"signal": series},
        )

    low_result, high_result = run_feature(low), run_feature(high)
    assert low.input_digest == high.input_digest == d.input_digest
    assert low_result.feature_inputs[0][2] != high_result.feature_inputs[0][2]


def test_ledger_begin_failure_has_action_context_and_preserves_exception():
    d = data({"A": (("10", "10"),) * 4})
    bars = tuple(replace(bar, source_refs=(f"bar:{bar.date}",)) for bar in d.bars)
    event = CorporateAction(
        "m",
        "A",
        "capital_reduction",
        "2024-01-03",
        source_refs=("action:par",),
    )
    d = ResearchData(d.market, d.symbols, d.start_date, d.end_date, d.sessions, bars, (event,))
    with pytest.raises(UnsupportedDataError) as caught:
        run(d, lambda view: [MarketOrder("A", "buy", D(1))] if view.date == "2024-01-01" else [])
    error = caught.value
    assert error.__cause__ is None
    assert len(error.__notes__) == 1
    assert "market=m, session=2024-01-03, phase=corporate_action_terms" in error.__notes__[0]
    assert "symbols=('A',)" in error.__notes__[0]
    assert "action:par" in error.__notes__[0] and "bar:2024-01-03" in error.__notes__[0]


def test_ledger_close_failure_has_action_context_and_preserves_exception():
    d = data({"A": (("10", "10"),) * 4})
    bars = tuple(
        replace(bar, close=None, source_refs=("quote:missing-close",))
        if bar.date == "2024-01-03"
        else replace(bar, source_refs=(f"bar:{bar.date}",))
        for bar in d.bars
    )
    event = CorporateAction(
        "m",
        "A",
        "capital_reduction",
        "2024-01-03",
        share_multiplier=D(2),
        source_refs=("action:share-change",),
    )
    d = ResearchData(d.market, d.symbols, d.start_date, d.end_date, d.sessions, bars, (event,))
    with pytest.raises(UnsupportedDataError) as caught:
        run(d, lambda view: [MarketOrder("A", "buy", D(1))] if view.date == "2024-01-01" else [])
    error = caught.value
    assert error.__cause__ is None
    assert len(error.__notes__) == 1
    assert "market=m, session=2024-01-03, phase=rawclose_valuation" in error.__notes__[0]
    assert "symbols=('A',)" in error.__notes__[0]
    assert "action:share-change" in error.__notes__[0]
    assert "quote:missing-close" in error.__notes__[0]


def test_engine_invalidates_pending_buy_on_zero_holdings_par_change():
    d = data({"A": (("10", "10"),) * 4})
    event = CorporateAction(
        "m",
        "A",
        "par_value",
        "2024-01-02",
        par_before=D(10),
        par_after=D(5),
    )
    d = ResearchData(d.market, d.symbols, d.start_date, d.end_date, d.sessions, d.bars, (event,))
    result = run(d, lambda v: [MarketOrder("A", "buy", D(1))] if v.date == "2024-01-01" else [])
    assert not result.fills
    assert [(item.session, item.reason) for item in result.rejections] == [
        ("2024-01-02", "corporate_action_invalidated_order")
    ]
    assert [snapshot.cash for snapshot in result.snapshots] == [D(1000)] * 4


def test_engine_dividend_entitlement_depends_on_prior_session_holding():
    d = data({"A": (("10", "10"), ("10", "10"), ("9", "9"), ("9", "9"))})
    event = CorporateAction(
        "m",
        "A",
        "dividend",
        "2024-01-03",
        dividend=D(1),
        bonus_share_ratio=D(0),
        before_price=D(10),
        reference_price=D(9),
    )
    d = ResearchData(d.market, d.symbols, d.start_date, d.end_date, d.sessions, d.bars, (event,))
    held = run(
        d,
        lambda v: [MarketOrder("A", "buy", D(1000))] if v.date == "2024-01-01" else [],
        cash=D(11000),
    )
    assert [(fill.session, fill.order.side, fill.order.quantity) for fill in held.fills] == [
        ("2024-01-02", "buy", D(1000))
    ]
    assert held.events[0].eligible_quantity == D(1000)
    assert held.events[0].cash_amount == D(1000)
    assert held.events[0].shares_amount == D(0)
    assert held.snapshots[2].cash == D(2000)
    assert held.snapshots[2].positions[0].quantity == D(1000)
    assert not any(item.reason == "corporate_action_invalidated_order" for item in held.rejections)

    exdate_buy = run(
        d,
        lambda v: [MarketOrder("A", "buy", D(1000))] if v.date == "2024-01-02" else [],
        cash=D(11000),
    )
    assert [(fill.session, fill.order.side) for fill in exdate_buy.fills] == [("2024-01-03", "buy")]
    assert not exdate_buy.events
    assert exdate_buy.snapshots[2].cash == D(2000)
    assert exdate_buy.snapshots[2].positions[0].quantity == D(1000)


def test_validation_order_classification_and_last_session_intents():
    d = data({"A": (("10", "10"), ("10", "10"), ("10", "10"))})
    nonordinary = replace(d.bars[0], classification="nonordinary")
    mixed = ResearchData(
        d.market, d.symbols, d.start_date, d.end_date, d.sessions, (nonordinary, *d.bars[1:])
    )
    with pytest.raises(ResearchInputError, match="classification"):
        run(mixed, lambda view: [])

    for bad_quantity in (D("-1"), 1.5):

        def invalid_on_last(view, quantity=bad_quantity):
            return [MarketOrder("A", "buy", quantity)] if view.date == d.end_date else []

        with pytest.raises(ResearchInputError, match="invalid quantity"):
            run(d, invalid_on_last)

    missing = data({"A": (("10", "10"), (None, "10"), ("10", "10"))})

    def duplicates(view):
        return [MarketOrder("A", "buy", D(1)), MarketOrder("A", "sell", D(1))]

    with pytest.raises(ResearchInputError, match="duplicate net directions"):
        run(missing, duplicates)

    final_order = run(d, lambda view: [MarketOrder("A", "buy", D(1))])
    assert [(item.session, item.reason) for item in final_order.rejections] == [
        (d.end_date, "end_of_run")
    ]


def test_warmup_prefix_isolation_future_independence_and_decimal_context():
    d = data({"A": (("10", "10"), ("10", "3"), ("4", "4"), ("5", "5"))})
    modified_bars = tuple(
        replace(bar, close=D("500"), open=D("500")) if bar.date == "2024-01-04" else bar
        for bar in d.bars
    )
    changed = ResearchData(d.market, d.symbols, d.start_date, d.end_date, d.sessions, modified_bars)
    expected_quantity = D("166.66666666666666666666666666666666666666666666667")

    def exercise(dataset):
        seen = []

        def feature_fn(view):
            if view.as_of < "2024-01-04":
                with pytest.raises(ResearchInputError, match="future"):
                    view.bar("A", "2024-01-04")
            return {"A": D(len(view.history("A")))}

        feature = compute_feature(dataset, feature_fn, name="prefix")

        def strategy(view):
            assert view.date >= "2024-01-02"
            assert view.features["prefix"]["A"] == D(len(view.history("A")))
            assert "2024-01-04" not in view.features
            assert all(
                item.date >= "2024-01-02" for item in view.history("A", start_date="2024-01-02")
            )
            if view.date < "2024-01-04":
                with pytest.raises(ResearchInputError, match="future"):
                    view.bar("A", "2024-01-04")
            if view.date == "2024-01-02":
                with pytest.raises(ResearchInputError, match="future"):
                    view.history("A", start_date="2024-01-04")
                orders = target_weights(view, {"A": D("0.5")}, D(0))
                seen.extend(orders)
                return orders
            return []

        with localcontext() as hostile:
            hostile.prec = 2
            hostile.Emax = 9
            hostile.traps[Inexact] = True
            before = (hostile.prec, hostile.Emax, hostile.traps[Inexact])
            result = backtest(
                dataset,
                strategy,
                initial_cash=D(1000),
                costs=ZERO,
                start_date="2024-01-02",
                end_date=dataset.end_date,
                features={"prefix": feature},
            )
            assert (hostile.prec, hostile.Emax, hostile.traps[Inexact]) == before
        assert seen[0].quantity == expected_quantity
        assert result.decisions[0].session == "2024-01-02"
        assert all(x.session >= "2024-01-02" for x in result.decisions)
        assert all(x.session >= "2024-01-02" for x in result.fills)
        return result

    original, future_changed = exercise(d), exercise(changed)
    assert original.decisions[:2] == future_changed.decisions[:2]
    assert original.fills[:1] == future_changed.fills[:1]
