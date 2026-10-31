"""Synthetic feature, forward-label, grouping and research/OOS-slice walkthrough."""

import json
from collections.abc import Mapping
from decimal import Decimal

from _teaching_data import SESSIONS, teaching_data

from quant_research.research import TimeView, analyze_feature, compute_feature, forward_returns


def _close_values(view: TimeView) -> Mapping[str, Decimal | None]:
    values: dict[str, Decimal | None] = {}
    for symbol in view.symbols:
        bar = view.bar(symbol)
        values[symbol] = bar.close if bar is not None else None
    return values


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
    close_feature = compute_feature(
        data,
        _close_values,
        name="close",
    )
    momentum = compute_feature(
        data,
        _momentum_values,
        name="one_session_close_momentum",
        params={"lookback_sessions": 1},
    )
    labels = forward_returns(data, (1,))
    research = analyze_feature(close_feature, labels, 2, SESSIONS[0], SESSIONS[2])
    out_of_sample = analyze_feature(close_feature, labels, 2, SESSIONS[3], SESSIONS[4])
    first = [
        {"symbol": item.symbol, "value": str(item.value), "end_date": item.end_date}
        for item in labels.labels
        if item.date == SESSIONS[0]
    ]
    print(
        json.dumps(
            {
                "data": "synthetic; not real market data",
                "feature": close_feature.name,
                "momentum_feature": momentum.name,
                "first_day_h1_labels": first,
                "research_slice": {
                    "start": research.start_date,
                    "end": research.end_date,
                    "groups": len(research.daily),
                },
                "out_of_sample_slice": {
                    "start": out_of_sample.start_date,
                    "end": out_of_sample.end_date,
                    "groups": len(out_of_sample.daily),
                },
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
