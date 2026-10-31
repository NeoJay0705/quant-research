"""Synthetic momentum ranking with top-one target weight and a cash reserve."""

import json
from collections.abc import Mapping
from decimal import Decimal

from _teaching_data import SESSIONS, teaching_data

from quant_research.research import (
    CostModel,
    DecisionView,
    MarketOrder,
    TimeView,
    backtest,
    compute_feature,
    target_weights,
)


def _momentum_values(view: TimeView) -> Mapping[str, Decimal | None]:
    values: dict[str, Decimal | None] = {}
    for symbol in view.symbols:
        history = view.history(symbol)
        if len(history) < 2 or history[-1].close is None or history[-2].close is None:
            values[symbol] = None
        else:
            values[symbol] = history[-1].close / history[-2].close - Decimal(1)
    return values


def main() -> None:
    data = teaching_data()
    momentum = compute_feature(
        data,
        _momentum_values,
        name="one_session_close_momentum",
        params={"lookback_sessions": 1},
    )

    def strategy(view: DecisionView) -> tuple[MarketOrder, ...]:
        scores = {symbol: view.features["momentum"][symbol] for symbol in view.time.symbols}
        eligible = [(score, symbol) for symbol, score in scores.items() if score is not None]
        if not eligible:
            return ()
        winner = max(eligible)[1]
        return target_weights(view, {winner: Decimal("0.5")}, Decimal("0.5"))

    run = backtest(
        data,
        strategy,
        initial_cash=Decimal(1000),
        costs=CostModel(Decimal(0), Decimal(0), Decimal(0), Decimal(0)),
        start_date=SESSIONS[0],
        end_date=SESSIONS[-1],
        features={"momentum": momentum},
    )
    print(
        json.dumps(
            {
                "data": "synthetic; not real market data",
                "selection": (
                    "top one by known-past close momentum; 0.5 stock weight, 0.5 cash reserve"
                ),
                "final_nav": str(run.snapshots[-1].nav.quantize(Decimal("0.000001"))),
                "fills": len(run.fills),
                "fill_sides": [fill.order.side for fill in run.fills],
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
