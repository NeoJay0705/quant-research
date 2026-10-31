"""Prefix-safe custom features, forward price labels, and grouped analysis."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from decimal import ROUND_HALF_EVEN, Context, Decimal, localcontext

from .data import ResearchData, TimeView
from .models import ResearchInputError, iso_date


@dataclass(frozen=True, slots=True)
class FeatureValue:
    date: str
    symbol: str
    value: Decimal | None
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class FeatureSeries:
    input_digest: str
    name: str
    params: tuple[tuple[str, object], ...]
    values: tuple[FeatureValue, ...]


@dataclass(frozen=True, slots=True)
class ForwardReturnLabel:
    date: str
    symbol: str
    horizon: int
    end_date: str | None
    value: Decimal | None
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class LabelSeries:
    input_digest: str
    labels: tuple[ForwardReturnLabel, ...]
    semantics: str = (
        "adjusted-close price return; excludes dividends; not next-open portfolio return"
    )


@dataclass(frozen=True, slots=True)
class DailyGroupAnalysis:
    date: str
    horizon: int
    group: int
    sample_count: int
    mean_label: Decimal | None
    missing_count: int


@dataclass(frozen=True, slots=True)
class GroupAnalysis:
    horizon: int
    group: int
    date_count: int
    sample_count: int
    mean_label: Decimal | None
    missing_count: int


@dataclass(frozen=True, slots=True)
class FeatureAnalysis:
    input_digest: str
    start_date: str
    end_date: str
    daily: tuple[DailyGroupAnalysis, ...]
    summary: tuple[GroupAnalysis, ...]


def _freeze(value: object, field: str) -> object:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ResearchInputError(f"{field} must not contain non-finite Decimal values")
        return value
    if isinstance(value, float):
        raise ResearchInputError(f"{field} must not contain float values")
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise ResearchInputError(f"{field} mapping keys must be strings")
        return tuple(sorted((key, _freeze(item, field)) for key, item in value.items()))
    if isinstance(value, (tuple, list)):
        return tuple(_freeze(item, field) for item in value)
    raise ResearchInputError(f"{field} contains unsupported mutable or non-serializable value")


def _validate_value(value: object, field: str) -> Decimal | None:
    if value is None:
        return None
    if not isinstance(value, Decimal) or not value.is_finite():
        raise ResearchInputError(f"{field} values must be finite Decimal or None")
    return value


def compute_feature(
    data: ResearchData,
    function: Callable[[TimeView], Mapping[str, Decimal | None]],
    *,
    name: str,
    params: Mapping[str, object] | None = None,
) -> FeatureSeries:
    """Call function once per market session with data visible only through that session."""
    if not isinstance(data, ResearchData):
        raise ResearchInputError("data must be ResearchData")
    if not callable(function):
        raise ResearchInputError("function must be callable")
    if not isinstance(name, str) or not name.strip():
        raise ResearchInputError("name must be a non-empty string")
    if params is None:
        params = {}
    if not isinstance(params, Mapping) or any(not isinstance(key, str) for key in params):
        raise ResearchInputError("params must be a mapping with string keys")
    frozen_params = tuple(sorted((key, _freeze(value, "params")) for key, value in params.items()))
    out: list[FeatureValue] = []
    known = set(data.symbols)
    for day in data.sessions:
        with localcontext(Context(prec=50, rounding=ROUND_HALF_EVEN)):
            result = function(data.at(day))
        if not isinstance(result, Mapping):
            raise ResearchInputError("feature function must return a symbol-to-value mapping")
        if any(not isinstance(symbol, str) for symbol in result):
            raise ResearchInputError("feature mapping keys must be symbol strings")
        unknown = set(result) - known
        if unknown:
            raise ResearchInputError(f"feature returned unknown symbols: {sorted(unknown)}")
        for symbol in data.symbols:
            if symbol not in result:
                out.append(FeatureValue(day, symbol, None, "not_returned"))
            else:
                value = _validate_value(result[symbol], "feature")
                out.append(
                    FeatureValue(day, symbol, value, "returned_none" if value is None else None)
                )
    return FeatureSeries(data.input_digest, name, frozen_params, tuple(out))


def _horizons(values: Sequence[int]) -> tuple[int, ...]:
    if isinstance(values, (str, bytes)) or not isinstance(values, Sequence) or not values:
        raise ResearchInputError("horizons must be a non-empty sequence of positive integers")
    result = tuple(values)
    if any(isinstance(item, bool) or not isinstance(item, int) or item <= 0 for item in result):
        raise ResearchInputError("horizons must contain positive integers (not bool)")
    if len(set(result)) != len(result):
        raise ResearchInputError("horizons must be unique")
    return result


def forward_returns(data: ResearchData, horizons: Sequence[int]) -> LabelSeries:
    """Create adjusted-close price-return labels at market-session horizons."""
    if not isinstance(data, ResearchData):
        raise ResearchInputError("data must be ResearchData")
    hs = _horizons(horizons)
    session_index = {day: i for i, day in enumerate(data.sessions)}
    bars = {(bar.symbol, bar.date): bar for bar in data.bars}
    labels: list[ForwardReturnLabel] = []
    for start in data.sessions:
        i = session_index[start]
        for symbol in data.symbols:
            start_bar = bars.get((symbol, start))
            for horizon in hs:
                end_i = i + horizon
                end = data.sessions[end_i] if end_i < len(data.sessions) else None
                if (
                    start_bar is None
                    or start_bar.adjusted_close is None
                    or start_bar.adjusted_close <= 0
                ):
                    labels.append(
                        ForwardReturnLabel(start, symbol, horizon, end, None, "missing_start_price")
                    )
                    continue
                if end is None:
                    labels.append(
                        ForwardReturnLabel(
                            start, symbol, horizon, None, None, "horizon_out_of_range"
                        )
                    )
                    continue
                end_bar = bars.get((symbol, end))
                if end_bar is None or end_bar.adjusted_close is None or end_bar.adjusted_close <= 0:
                    labels.append(
                        ForwardReturnLabel(start, symbol, horizon, end, None, "missing_end_price")
                    )
                    continue
                with localcontext(Context(prec=50, rounding=ROUND_HALF_EVEN)):
                    value = end_bar.adjusted_close / start_bar.adjusted_close - Decimal(1)
                labels.append(ForwardReturnLabel(start, symbol, horizon, end, value))
    return LabelSeries(data.input_digest, tuple(labels))


def _midrank_groups(values: Mapping[str, Decimal], groups: int) -> dict[str, int]:
    ordered = sorted(values.items(), key=lambda item: (item[1], item[0]))
    if len({value for _, value in ordered}) == 1:
        return {symbol: 1 for symbol, _ in ordered}
    result: dict[str, int] = {}
    position = 0
    n = len(ordered)
    while position < n:
        stop = position + 1
        while stop < n and ordered[stop][1] == ordered[position][1]:
            stop += 1
        group = min(groups, groups * (position + stop - 1) // (2 * n) + 1)
        for index in range(position, stop):
            result[ordered[index][0]] = group
        position = stop
    return result


def _validate_feature_series(feature: FeatureSeries) -> None:
    if not isinstance(feature.input_digest, str) or not feature.input_digest:
        raise ResearchInputError("feature input_digest must be a non-empty string")
    if not isinstance(feature.name, str) or not feature.name.strip():
        raise ResearchInputError("feature name must be a non-empty string")
    if not isinstance(feature.params, tuple):
        raise ResearchInputError("feature params must be an immutable tuple")
    for pair in feature.params:
        if not isinstance(pair, tuple) or len(pair) != 2 or not isinstance(pair[0], str):
            raise ResearchInputError("feature params must contain string-keyed pairs")
        try:
            if _freeze(pair[1], "params") != pair[1]:
                raise ResearchInputError("feature params must contain frozen values")
        except (TypeError, ValueError) as exc:
            raise ResearchInputError("feature params must contain frozen values") from exc
    if len({pair[0] for pair in feature.params}) != len(feature.params):
        raise ResearchInputError("feature params keys must be unique")
    if not isinstance(feature.values, tuple):
        raise ResearchInputError("feature values must be an immutable tuple")
    for item in feature.values:
        if not isinstance(item, FeatureValue):
            raise ResearchInputError("feature values must contain FeatureValue records")
        iso_date(item.date)
        if not isinstance(item.symbol, str) or not item.symbol:
            raise ResearchInputError("feature symbols must be non-empty strings")
        _validate_value(item.value, "feature")
        if item.reason is not None and not isinstance(item.reason, str):
            raise ResearchInputError("feature reason must be a string or None")


def _validate_label_series(labels: LabelSeries) -> None:
    if not isinstance(labels.input_digest, str) or not labels.input_digest:
        raise ResearchInputError("label input_digest must be a non-empty string")
    if not isinstance(labels.semantics, str):
        raise ResearchInputError("label semantics must be a string")
    if not isinstance(labels.labels, tuple):
        raise ResearchInputError("labels must be an immutable tuple")
    for item in labels.labels:
        if not isinstance(item, ForwardReturnLabel):
            raise ResearchInputError("labels must contain ForwardReturnLabel records")
        iso_date(item.date)
        if not isinstance(item.symbol, str) or not item.symbol:
            raise ResearchInputError("label symbols must be non-empty strings")
        if isinstance(item.horizon, bool) or not isinstance(item.horizon, int) or item.horizon <= 0:
            raise ResearchInputError("label horizons must be positive integers")
        if item.end_date is not None:
            iso_date(item.end_date, "end_date")
            if item.end_date < item.date:
                raise ResearchInputError("label end_date must not precede date")
        _validate_value(item.value, "label")
        if item.reason is not None and not isinstance(item.reason, str):
            raise ResearchInputError("label reason must be a string or None")


def analyze_feature(
    feature: FeatureSeries,
    labels: LabelSeries,
    groups: int,
    start_date: str,
    end_date: str,
    symbols: Sequence[str] | None = None,
) -> FeatureAnalysis:
    """Group each date independently; summaries average available daily means equally."""
    if not isinstance(feature, FeatureSeries) or not isinstance(labels, LabelSeries):
        raise ResearchInputError("feature and labels must be their immutable series records")
    _validate_feature_series(feature)
    _validate_label_series(labels)
    if feature.input_digest != labels.input_digest:
        raise ResearchInputError("feature and label input_digest must agree")
    if isinstance(groups, bool) or not isinstance(groups, int) or groups < 1:
        raise ResearchInputError("groups must be a positive integer")
    start, end = iso_date(start_date, "start_date"), iso_date(end_date, "end_date")
    if start > end:
        raise ResearchInputError("start_date must not be after end_date")
    selected = None if symbols is None else tuple(symbols)
    if selected is not None and (
        any(not isinstance(symbol, str) or not symbol for symbol in selected)
        or len(set(selected)) != len(selected)
    ):
        raise ResearchInputError("symbols must be unique non-empty strings")
    feature_by_date: dict[str, dict[str, FeatureValue]] = {}
    for item in feature.values:
        day_entries = feature_by_date.setdefault(item.date, {})
        if item.symbol in day_entries:
            raise ResearchInputError("feature contains duplicate date/symbol records")
        day_entries[item.symbol] = item
    label_map: dict[tuple[str, str, int], ForwardReturnLabel] = {}
    for label_item in labels.labels:
        key = (label_item.date, label_item.symbol, label_item.horizon)
        if key in label_map:
            raise ResearchInputError("labels contain duplicate date/symbol/horizon records")
        label_map[key] = label_item
    dates = sorted(day for day in feature_by_date if start <= day <= end)
    horizons = sorted({item.horizon for item in labels.labels})
    daily: list[DailyGroupAnalysis] = []
    for horizon in horizons:
        for day in dates:
            eligible: dict[str, Decimal] = {}
            missing: dict[str, int] = {}
            for symbol, item in feature_by_date[day].items():
                if selected is not None and symbol not in selected:
                    continue
                if item.value is None:
                    missing[symbol] = 1
                if item.value is not None:
                    eligible[symbol] = item.value
            assignments = _midrank_groups(eligible, groups) if eligible else {}
            if missing:
                daily.append(DailyGroupAnalysis(day, horizon, 0, 0, None, len(missing)))
            for group in sorted(set(assignments.values())):
                members = [symbol for symbol, assigned in assignments.items() if assigned == group]
                valid: list[Decimal] = []
                for symbol in members:
                    label = label_map.get((day, symbol, horizon))
                    if (
                        label is not None
                        and label.value is not None
                        and label.end_date is not None
                        and start <= label.end_date <= end
                    ):
                        valid.append(label.value)
                with localcontext(Context(prec=50, rounding=ROUND_HALF_EVEN)):
                    mean = sum(valid, Decimal(0)) / len(valid) if valid else None
                daily.append(
                    DailyGroupAnalysis(
                        day, horizon, group, len(valid), mean, len(members) - len(valid)
                    )
                )
    summary: list[GroupAnalysis] = []
    for horizon in horizons:
        for group in sorted({row.group for row in daily if row.horizon == horizon}):
            rows = [row for row in daily if row.horizon == horizon and row.group == group]
            valid_means = [row.mean_label for row in rows if row.mean_label is not None]
            with localcontext(Context(prec=50, rounding=ROUND_HALF_EVEN)):
                mean = sum(valid_means, Decimal(0)) / len(valid_means) if valid_means else None
            summary.append(
                GroupAnalysis(
                    horizon,
                    group,
                    len(valid_means),
                    sum(row.sample_count for row in rows),
                    mean,
                    sum(row.missing_count for row in rows),
                )
            )
    return FeatureAnalysis(feature.input_digest, start, end, tuple(daily), tuple(summary))
