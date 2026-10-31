from __future__ import annotations

from decimal import ROUND_DOWN, Context, Decimal, Inexact, getcontext, setcontext

import pytest

from quant_research.research import Bar, ResearchData, ResearchInputError
from quant_research.research.features import (
    FeatureSeries,
    FeatureValue,
    ForwardReturnLabel,
    LabelSeries,
    analyze_feature,
    compute_feature,
    forward_returns,
)


def data(
    rows: dict[str, tuple[Decimal | None, ...]], sessions: tuple[str, ...] | None = None
) -> ResearchData:
    days = sessions or ("2024-01-01", "2024-01-02", "2024-01-03", "2024-01-04")
    bars = tuple(
        Bar("m", symbol, day, adjusted_close=price)
        for symbol in sorted(rows)
        for day, price in zip(days, rows[symbol], strict=True)
        if price is not None
    )
    return ResearchData("m", tuple(rows), days[0], days[-1], days, bars)


def test_hand_computed_groups_and_immutable_reusable_feature() -> None:
    market = data(
        {
            "A": (Decimal(10), Decimal(11), Decimal(12), Decimal(13)),
            "B": (Decimal(10), Decimal(12), Decimal(14), Decimal(16)),
        }
    )
    feature = compute_feature(
        market,
        lambda view: {symbol: Decimal(i + 1) for i, symbol in enumerate(view.symbols)},
        name="ranking",
        params={"window": [1, 2]},
    )
    result = analyze_feature(feature, forward_returns(market, (1,)), 2, "2024-01-01", "2024-01-02")
    selected = [row for row in result.daily if row.date == "2024-01-01"]
    assert [(r.group, r.sample_count, r.mean_label) for r in selected] == [
        (1, 1, Decimal("0.1")),
        (2, 1, Decimal("0.2")),
    ]
    assert feature.values[0].value == Decimal(1)
    assert feature.params == (("window", (1, 2)),)


def test_warmup_ties_constant_and_missing_endpoints() -> None:
    market = data(
        {
            "A": (Decimal(10), None, Decimal(12), Decimal(13)),
            "B": (Decimal(10), Decimal(11), Decimal(12), Decimal(13)),
            "C": (Decimal(10), Decimal(11), Decimal(12), Decimal(13)),
        }
    )
    feature = compute_feature(
        market,
        lambda view: {
            s: None if view.as_of == market.sessions[0] else Decimal(1) for s in view.symbols
        },
        name="constant",
    )
    assert feature.values[0].reason == "returned_none"
    labels = forward_returns(market, (1,))
    assert (
        next(
            label for label in labels.labels if label.date == "2024-01-01" and label.symbol == "A"
        ).reason
        == "missing_end_price"
    )
    analysis = analyze_feature(feature, labels, 4, "2024-01-01", "2024-01-03")
    assert any(row.group == 0 and row.missing_count == 3 for row in analysis.daily)
    assert all(row.group in {0, 1} for row in analysis.daily)


def test_halted_symbol_uses_market_sessions_and_oos_labels_do_not_cross_end() -> None:
    market = data(
        {
            "A": (Decimal(10), None, None, Decimal(20)),
            "B": (Decimal(10), Decimal(11), Decimal(12), Decimal(13)),
        }
    )
    labels = forward_returns(market, (2,))
    a = next(label for label in labels.labels if label.date == "2024-01-01" and label.symbol == "A")
    assert a.end_date == "2024-01-03" and a.reason == "missing_end_price"
    feature = compute_feature(market, lambda view: {s: Decimal(1) for s in view.symbols}, name="x")
    analysis = analyze_feature(feature, labels, 1, "2024-01-01", "2024-01-02")
    assert all(row.date != "2024-01-01" or row.sample_count == 0 for row in analysis.daily)


def test_prefix_is_stable_after_future_extension_and_context_is_unchanged() -> None:
    original = data({"A": (Decimal(10), Decimal(11), Decimal(12), Decimal(13))})
    extended = data({"A": (Decimal(10), Decimal(11), Decimal(12), Decimal(99))})

    def fn(view):
        history = view.history("A")
        return {"A": history[-1].adjusted_close} if history else {"A": None}

    one = compute_feature(original, fn, name="close")
    two = compute_feature(extended, fn, name="close")
    assert one.values[:3] == two.values[:3]
    before = getcontext().copy()
    setcontext(Context(prec=2, rounding=ROUND_DOWN))
    try:
        result = forward_returns(original, (1,))
        assert next(x.value for x in result.labels if x.date == "2024-01-01") == Decimal("0.1")
        assert getcontext().prec == 2 and getcontext().rounding == ROUND_DOWN
    finally:
        setcontext(before)


def test_custom_callback_uses_isolated_decimal_context() -> None:
    market = data({"A": (Decimal(1), Decimal(2), Decimal(3), Decimal(4))})
    before = getcontext().copy()
    hostile = Context(prec=2, rounding=ROUND_DOWN, Emax=2, Emin=-2)
    hostile.traps[Inexact] = True
    hostile.flags[Inexact] = True
    outer_flags = hostile.flags.copy()
    setcontext(hostile)
    try:
        feature = compute_feature(
            market, lambda _view: {"A": Decimal(1) / Decimal(3)}, name="ratio"
        )
        assert feature.values[0].value == Decimal(
            "0.33333333333333333333333333333333333333333333333333"
        )
        assert getcontext().prec == 2
        assert getcontext().rounding == ROUND_DOWN
        assert getcontext().Emax == 2
        assert getcontext().traps[Inexact]
        assert getcontext().flags == outer_flags
    finally:
        setcontext(before)


def test_large_small_analysis_has_one_daily_row_per_date_and_horizon() -> None:
    sessions = tuple(f"2024-01-{day:02}" for day in range(1, 29))
    market = data(
        {f"S{i:02}": tuple(Decimal(i + day) for day, _ in enumerate(sessions)) for i in range(12)},
        sessions,
    )
    feature = compute_feature(
        market, lambda view: {symbol: Decimal(i) for i, symbol in enumerate(view.symbols)}, name="x"
    )
    result = analyze_feature(feature, forward_returns(market, (1, 2)), 4, sessions[0], sessions[-1])
    assert len(result.daily) == len(sessions) * 2 * 4
    assert {(row.date, row.horizon) for row in result.daily} == {
        (day, horizon) for day in sessions for horizon in (1, 2)
    }


def test_manually_constructed_series_are_validated_at_analysis_boundary() -> None:
    feature = compute_feature(
        data({"A": (Decimal(1), Decimal(2), Decimal(3), Decimal(4))}),
        lambda _view: {"A": Decimal(1)},
        name="x",
    )
    labels = forward_returns(data({"A": (Decimal(1), Decimal(2), Decimal(3), Decimal(4))}), (1,))
    bad_features = (
        FeatureSeries(feature.input_digest, feature.name, [], feature.values),  # type: ignore[arg-type]
        FeatureSeries(feature.input_digest, feature.name, feature.params, (object(),)),  # type: ignore[arg-type]
        FeatureSeries(
            feature.input_digest,
            feature.name,
            feature.params,
            (FeatureValue("2024-1-1", "A", Decimal(1)),),
        ),
        FeatureSeries(
            feature.input_digest,
            feature.name,
            feature.params,
            (FeatureValue("2024-01-01", "A", 1.0),),
        ),  # type: ignore[arg-type]
    )
    bad_labels = (
        LabelSeries(labels.input_digest, (object(),)),  # type: ignore[arg-type]
        LabelSeries(
            labels.input_digest,
            (ForwardReturnLabel("2024-01-02", "A", 1, "2024-01-01", Decimal(1)),),
        ),
        LabelSeries(
            labels.input_digest, (ForwardReturnLabel("2024-01-01", "A", True, None, Decimal(1)),)
        ),  # type: ignore[arg-type]
        LabelSeries(labels.input_digest, (ForwardReturnLabel("2024-01-01", "A", 1, None, 1.0),)),  # type: ignore[arg-type]
    )
    for invalid in bad_features:
        with pytest.raises(ResearchInputError):
            analyze_feature(invalid, labels, 1, "2024-01-01", "2024-01-04")
    for invalid in bad_labels:
        with pytest.raises(ResearchInputError):
            analyze_feature(feature, invalid, 1, "2024-01-01", "2024-01-04")


def test_rejects_bad_horizons_values_symbols_and_digest_mismatch() -> None:
    market = data({"A": (Decimal(1), Decimal(2), Decimal(3), Decimal(4))})
    for bad in ((0,), (True,), (1, 1)):
        with pytest.raises(ResearchInputError):
            forward_returns(market, bad)
    with pytest.raises(ResearchInputError):
        compute_feature(market, lambda view: {"A": 1.0}, name="bad")  # type: ignore[dict-item]
    with pytest.raises(ResearchInputError):
        compute_feature(market, lambda view: {"A": Decimal("NaN")}, name="bad")
    with pytest.raises(ResearchInputError):
        compute_feature(market, lambda view: {"Z": Decimal(1)}, name="bad")
    with pytest.raises(ResearchInputError):
        analyze_feature(
            compute_feature(market, lambda view: {"A": Decimal(1)}, name="x"),
            forward_returns(
                data(
                    {"A": (Decimal(1), Decimal(2), Decimal(3), Decimal(4))},
                    ("2024-01-01", "2024-01-02", "2024-01-03", "2024-01-05"),
                ),
                (1,),
            ),
            2,
            "2024-01-01",
            "2024-01-03",
        )


def test_duplicate_symbols_rejected_at_data_boundary() -> None:
    with pytest.raises(ResearchInputError):
        ResearchData("m", ("A", "A"), "2024-01-01", "2024-01-01", ("2024-01-01",), ())
