from dataclasses import replace
from decimal import Decimal as D

import pytest

from quant_research.research.backtest import CostModel, MarketOrder, RunResult, backtest
from quant_research.research.data import ResearchData
from quant_research.research.models import Bar, ResearchInputError
from quant_research.research.performance import compare_runs, daily_returns, summarize


def _run(costs: CostModel | None = None) -> RunResult:
    days = ("2024-01-01", "2024-01-02", "2024-01-03")
    data = ResearchData(
        "m",
        ("A",),
        days[0],
        days[-1],
        days,
        tuple(
            Bar(
                "m",
                "A",
                day,
                open=D(price),
                close=D(price),
                classification="ordinary_evidence_supported",
            )
            for day, price in zip(days, ("10", "10", "12"), strict=True)
        ),
    )
    model = costs or CostModel(D(".001"), D(0), D(".003"), D(0))

    def strategy(view):
        if view.date == days[0]:
            return [MarketOrder("A", "buy", D(10))]
        if view.date == days[1]:
            return [MarketOrder("A", "sell", D(10))]
        return []

    return backtest(
        data,
        strategy,
        initial_cash=D(1000),
        costs=model,
        start_date=days[0],
        end_date=days[-1],
        program_path=__file__,
    )


def test_actual_backtest_fee_summary_hand_calculation():
    result = _run()
    summary = result.summary()
    assert summary.final_nav == D("1019.42")
    assert summary.total_return == D(".01942")
    assert summary.total_cost == D(".58")
    assert summary.before_cost_same_fill_pnl == D(20)
    assert not hasattr(summary, "daily_returns")
    assert summary.fees == D(".22")
    assert summary.tax == D(".36")
    assert summary.slippage == 0


def test_synthetic_nav_return_drawdown_includes_initial_peak():
    run = _run()
    snapshots = tuple(
        replace(item, nav=nav)
        for item, nav in zip(run.snapshots, (D(1000), D(900), D(990)), strict=True)
    )
    rows = daily_returns(replace(run, snapshots=snapshots))
    assert [row.daily_return for row in rows] == [D(0), D("-.1"), D(".1")]
    assert [row.drawdown for row in rows] == [D(0), D(".1"), D(".01")]
    assert rows[-1].cumulative_return == D("-.01")
    assert summarize(replace(run, snapshots=snapshots)).max_drawdown == D(".1")
    first_below = daily_returns(replace(run, snapshots=(replace(snapshots[0], nav=D(900)),)))[0]
    assert first_below.drawdown == D(".1")


def test_zero_previous_nav_has_explicit_undefined_return():
    run = _run()
    snapshots = tuple(
        replace(item, nav=nav) for item, nav in zip(run.snapshots, (D(0), D(10), D(0)), strict=True)
    )
    rows = daily_returns(replace(run, snapshots=snapshots))
    assert rows[0].daily_return == D(-1)
    assert rows[1].daily_return is None
    assert rows[1].undefined_reason == "previous_nav_zero"
    assert rows[2].daily_return == D(-1)


def test_comparison_fields_and_unknown_program_warning():
    base = _run()
    changed = replace(
        base,
        input_digest="different-data",
        params=(("x", 2),),
        start_date="2024-01-02",
        assumptions=base.assumptions + ("different assumption",),
        costs=CostModel(D(0), D(0), D(0), D(0)),
        strategy_sha256=None,
    )
    comparison = compare_runs({"base": base, "scenario": changed})
    assert {d.field for d in comparison.differences} >= {
        "input_digest",
        "params",
        "start_date",
        "assumptions",
        "costs",
        "strategy_sha256",
    }
    assert comparison.warnings and "program identity is incomplete" in comparison.warnings[0]
    assert comparison.summaries["base"].final_nav == D("1019.42")
    with pytest.raises(TypeError):
        comparison.summaries["x"] = comparison.summaries["base"]
    with pytest.raises(ResearchInputError):
        compare_runs({})
