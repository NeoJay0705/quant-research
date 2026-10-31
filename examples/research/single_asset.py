"""Synthetic next-open single-asset round trip and explicit scenario comparison."""

import json
from collections.abc import Callable, Mapping
from decimal import Decimal

from _teaching_data import SESSIONS, teaching_data

from quant_research.research import (
    CostModel,
    DecisionView,
    MarketOrder,
    RunResult,
    TimeView,
    backtest,
    compare_runs,
    compute_feature,
    summarize,
)

ZERO_COST = CostModel(Decimal(0), Decimal(0), Decimal(0), Decimal(0))


def strategy(quantity: Decimal) -> Callable[..., object]:
    def decide(view: DecisionView, **_params: object) -> tuple[MarketOrder, ...]:
        if view.date == SESSIONS[0]:
            return (MarketOrder("A", "buy", quantity),)
        if view.date == SESSIONS[2]:
            return (MarketOrder("A", "sell", quantity),)
        return ()

    return decide


def _close_values(view: TimeView) -> Mapping[str, Decimal | None]:
    values = {}
    for symbol in view.symbols:
        bar = view.bar(symbol)
        values[symbol] = bar.close if bar is not None else None
    return values


def main() -> None:
    data = teaching_data()
    close = compute_feature(data, _close_values, name="close")

    def run(quantity: int) -> RunResult:
        return backtest(
            data,
            strategy(Decimal(quantity)),
            initial_cash=Decimal(1000),
            costs=ZERO_COST,
            start_date=SESSIONS[0],
            end_date=SESSIONS[-1],
            features={"close": close},
            params={"quantity": quantity},
        )

    baseline, smaller = run(10), run(5)
    comparison = compare_runs({"buy_10": baseline, "buy_5": smaller})
    print(
        json.dumps(
            {
                "data": "synthetic; not real market data",
                "buy_10": {
                    "final_nav": str(summarize(baseline).final_nav),
                    "fills": len(baseline.fills),
                    "fill_dates": [fill.session for fill in baseline.fills],
                },
                "buy_5": {
                    "final_nav": str(summarize(smaller).final_nav),
                    "fills": len(smaller.fills),
                },
                "comparison": {
                    "summaries": {
                        name: str(item.final_nav) for name, item in comparison.summaries.items()
                    },
                    "differences": [item.field for item in comparison.differences],
                    "interpretation": comparison.interpretation,
                },
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
