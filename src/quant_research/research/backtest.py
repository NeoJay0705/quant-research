"""Deterministic single-market, next-session-open backtesting.

Simplifications: fractional shares; all valid daily-open orders fill without liquidity,
queue, intraday-limit or market-impact simulation; sell proceeds fund same-open buys.
Missing event dates use the ledger's effective-date proxy. Python strategies are trusted,
not sandboxed. No performance summary or persistence API is provided here.
"""

from __future__ import annotations

import hashlib
import platform
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from decimal import ROUND_HALF_EVEN, Context, Decimal, localcontext
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING

from .data import ResearchData, TimeView, _canonical
from .features import FeatureSeries, _validate_feature_series
from .ledger import AccountSnapshot, Ledger, LedgerEvent

if TYPE_CHECKING:
    from .performance import PerformanceSummary

from .models import (
    Bar,
    CorporateAction,
    ResearchError,
    ResearchInputError,
    SourceDiagnostic,
    iso_date,
)


@dataclass(frozen=True, slots=True)
class MarketOrder:
    symbol: str
    side: str
    quantity: Decimal


@dataclass(frozen=True, slots=True)
class CostModel:
    fee_rate: Decimal
    minimum_fee: Decimal
    sell_tax_rate: Decimal
    slippage_bps: Decimal

    def __post_init__(self) -> None:
        for name in ("fee_rate", "minimum_fee", "sell_tax_rate", "slippage_bps"):
            v = getattr(self, name)
            if not isinstance(v, Decimal) or not v.is_finite() or v < 0:
                raise ResearchInputError(f"{name} must be a finite non-negative Decimal")
        if self.slippage_bps >= Decimal(10000):
            raise ResearchInputError("slippage_bps must be less than 10000")


@dataclass(frozen=True, slots=True)
class Fill:
    session: str
    order: MarketOrder
    price: Decimal
    notional: Decimal
    fee: Decimal
    sell_tax: Decimal
    slippage_cost: Decimal


@dataclass(frozen=True, slots=True)
class Rejection:
    session: str
    order: MarketOrder
    reason: str


@dataclass(frozen=True, slots=True)
class Decision:
    session: str
    orders: tuple[MarketOrder, ...]


@dataclass(frozen=True, slots=True)
class DecisionView:
    time: TimeView
    account: AccountSnapshot
    features: Mapping[str, Mapping[str, Decimal | None]]

    @property
    def date(self) -> str:
        return self.time.as_of

    def bar(self, symbol: str, day: str | None = None) -> Bar | None:
        return self.time.bar(symbol, day)

    def history(self, symbol: str, *, start_date: str | None = None) -> Sequence[Bar]:
        return self.time.history(symbol, start_date=start_date)

    def events(self, symbol: str) -> tuple[CorporateAction, ...]:
        return self.time.events(symbol)


@dataclass(frozen=True, slots=True)
class RunResult:
    input_digest: str
    market: str
    symbols: tuple[str, ...]
    start_date: str
    end_date: str
    strategy_sha256: str | None
    params: tuple[tuple[str, object], ...]
    params_digest: str
    python_version: str
    random_seed: int | None
    costs: CostModel
    assumptions: tuple[str, ...]
    snapshots: tuple[AccountSnapshot, ...]
    decisions: tuple[Decision, ...]
    fills: tuple[Fill, ...]
    rejections: tuple[Rejection, ...]
    events: tuple[LedgerEvent, ...]
    initial_cash: Decimal
    load_start: str
    load_end: str
    program_path: str | None
    protected_input_dirs: tuple[str, ...]
    source_diagnostics: tuple[SourceDiagnostic, ...]
    classification_diagnostics: tuple[str, ...]
    source_refs: tuple[str, ...]
    classification_refs: tuple[str, ...]
    calendar_basis: str
    feature_inputs: tuple[tuple[str, str, str, tuple[tuple[str, object], ...]], ...]

    def summary(self) -> PerformanceSummary:
        from .performance import summarize

        return summarize(self)


class ResearchBacktestError(ResearchError):
    """Strategy callback failure with date context; original exception is chained."""


def _freeze(value: object, path: str = "params") -> object:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, Decimal) and value.is_finite():
        return ("decimal", str(value))
    if isinstance(value, float):
        raise ResearchInputError(f"{path} cannot contain float")
    if isinstance(value, Mapping):
        if any(not isinstance(k, str) for k in value):
            raise ResearchInputError(f"{path} keys must be strings")
        return ("mapping", tuple(sorted((k, _freeze(v, path)) for k, v in value.items())))
    if isinstance(value, list):
        return ("list", tuple(_freeze(v, path) for v in value))
    if isinstance(value, tuple):
        return ("tuple", tuple(_freeze(v, path) for v in value))
    raise ResearchInputError(f"{path} contains unsupported value")


def _copy_runtime(value: object) -> object:
    if isinstance(value, Mapping):
        return {key: _copy_runtime(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_copy_runtime(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_copy_runtime(item) for item in value)
    return value


def target_weights(
    view: DecisionView, weights: Mapping[str, Decimal], cash_reserve: Decimal
) -> tuple[MarketOrder, ...]:
    """Create long-only close-sized orders; pending rights count as exposure, never sellable."""
    if (
        not isinstance(cash_reserve, Decimal)
        or not cash_reserve.is_finite()
        or not 0 <= cash_reserve <= 1
    ):
        raise ResearchInputError("cash_reserve must be a Decimal fraction in [0,1]")
    if not isinstance(weights, Mapping) or set(weights) - set(view.time.symbols):
        raise ResearchInputError("weights must name known symbols")
    if any(not isinstance(w, Decimal) or not w.is_finite() or w < 0 for w in weights.values()):
        raise ResearchInputError("weights must be nonnegative and sum to at most 1-cash_reserve")
    with localcontext(Context(prec=50, rounding=ROUND_HALF_EVEN)):
        if sum(weights.values(), Decimal(0)) > 1 - cash_reserve:
            raise ResearchInputError(
                "weights must be nonnegative and sum to at most 1-cash_reserve"
            )
        nav = view.account.nav
        holdings = {p.symbol: p.quantity for p in view.account.positions}
        pending = {p.symbol: p.quantity for p in view.account.pending_shares}
        out = []
        for symbol in view.time.symbols:
            bar = view.time.bar(symbol)
            if bar is None or bar.close is None or bar.close <= 0:
                continue
            target = nav * weights.get(symbol, Decimal(0)) / bar.close
            current = holdings.get(symbol, Decimal(0)) + pending.get(symbol, Decimal(0))
            delta = target - current
            if delta > 0:
                out.append(MarketOrder(symbol, "buy", delta))
            elif delta < 0:
                sell = min(-delta, holdings.get(symbol, Decimal(0)))
                if sell:
                    out.append(MarketOrder(symbol, "sell", sell))
        return tuple(out)


def backtest(
    data: ResearchData,
    strategy: Callable[..., object],
    *,
    initial_cash: Decimal,
    costs: CostModel,
    start_date: str,
    end_date: str,
    features: Mapping[str, FeatureSeries] | None = None,
    params: Mapping[str, object] | None = None,
    program_path: str | Path | None = None,
    random_seed: int | None = None,
) -> RunResult:
    if not isinstance(data, ResearchData):
        raise ResearchInputError("data must be ResearchData")
    if not callable(strategy):
        raise ResearchInputError("strategy must be callable")
    if not isinstance(initial_cash, Decimal) or not initial_cash.is_finite() or initial_cash <= 0:
        raise ResearchInputError("initial_cash must be positive finite Decimal")
    start, end = iso_date(start_date, "start_date"), iso_date(end_date, "end_date")
    if start > end or start < data.start_date or end > data.end_date:
        raise ResearchInputError("evaluation bounds must be within loaded data")
    if not isinstance(costs, CostModel):
        raise ResearchInputError("costs must be explicit CostModel")
    eval_sessions = tuple(d for d in data.sessions if start <= d <= end)
    if not eval_sessions:
        raise ResearchInputError("evaluation has no sessions")
    if any(
        not any(b.symbol == s and start <= b.date <= end for b in data.bars) for s in data.symbols
    ):
        raise ResearchInputError("chosen universe contains completely missing symbol")
    for s in data.symbols:
        classifications = {
            b.classification for b in data.bars if b.symbol == s and start <= b.date <= end
        }
        if not classifications or any(
            value not in {"ordinary_evidence_supported", "ordinary_rule_limited"}
            for value in classifications
        ):
            raise ResearchInputError(f"nonordinary or unknown symbol classification: {s}")
    if params is None:
        params = {}
    if not isinstance(params, Mapping) or any(not isinstance(key, str) for key in params):
        raise ResearchInputError("params must be a mapping with string keys")
    runtime_params = _copy_runtime(params)
    if not isinstance(runtime_params, Mapping):
        raise ResearchInputError("params must be a mapping")
    frozen = tuple(sorted((key, _freeze(value)) for key, value in runtime_params.items()))
    params_digest = hashlib.sha256(_canonical(frozen)).hexdigest()
    if random_seed is not None and (
        isinstance(random_seed, bool) or not isinstance(random_seed, int)
    ):
        raise ResearchInputError("random_seed must be int or None")
    resolved_program_path: str | None = None
    digest = None
    if program_path is not None:
        try:
            resolved_program_path = str(Path(program_path).resolve())
            digest = hashlib.sha256(Path(resolved_program_path).read_bytes()).hexdigest()
        except OSError as exc:
            raise ResearchInputError("cannot read strategy program_path") from exc
    if features is not None and not isinstance(features, Mapping):
        raise ResearchInputError("features must be a mapping")
    feature_index: dict[str, dict[tuple[str, str], Decimal | None]] = {}
    feature_inputs: list[tuple[str, str, str, tuple[tuple[str, object], ...]]] = []
    for name, series in (features or {}).items():
        if (
            not isinstance(name, str)
            or not name
            or not isinstance(series, FeatureSeries)
            or series.input_digest != data.input_digest
        ):
            raise ResearchInputError("feature name/series/digest invalid")
        _validate_feature_series(series)
        values: dict[tuple[str, str], Decimal | None] = {}
        for item in series.values:
            if (
                item.date not in data.sessions
                or item.symbol not in data.symbols
                or (item.date, item.symbol) in values
            ):
                raise ResearchInputError("feature contains unknown/duplicate key")
            if item.value is not None and (
                not isinstance(item.value, Decimal) or not item.value.is_finite()
            ):
                raise ResearchInputError("feature values must be finite Decimal")
            if not data.start_date <= item.date <= data.end_date:
                raise ResearchInputError("feature date outside loaded bounds")
            values[item.date, item.symbol] = item.value
        feature_index[name] = values
        feature_digest = hashlib.sha256(
            _canonical((series.name, series.params, series.values))
        ).hexdigest()
        feature_inputs.append((name, series.name, feature_digest, series.params))
    ledger = Ledger(initial_cash, events=data.events)
    bars_lists: dict[str, list[Bar]] = {d: [] for d in data.sessions}
    for input_bar in data.bars:
        bars_lists[input_bar.date].append(input_bar)
    bars_by_day = {day: tuple(rows) for day, rows in bars_lists.items()}
    features_by_day: dict[str, dict[str, dict[str, Decimal | None]]] = {}
    for name, indexed in feature_index.items():
        for (feature_day, symbol), value in indexed.items():
            features_by_day.setdefault(feature_day, {}).setdefault(name, {})[symbol] = value
    stop_intervals: dict[str, list[tuple[str, str]]] = {}
    for event in data.events:
        payload = dict(event.payload)
        if "stop_date" not in payload:
            continue
        if payload["stop_date"] is None:
            continue
        stop_date = iso_date(payload["stop_date"], "stop_date")
        if stop_date > event.effective_date:
            raise ResearchInputError("stop_date must be on or before effective_date")
        stop_intervals.setdefault(event.symbol, []).append((stop_date, event.effective_date))
    for intervals in stop_intervals.values():
        intervals.sort()

    pending: tuple[MarketOrder, ...] = ()
    fills: list[Fill] = []
    rejects: list[Rejection] = []
    decisions: list[Decision] = []
    snapshots: list[AccountSnapshot] = []
    all_sessions = tuple(d for d in data.sessions if d <= end)
    previous_session: str | None = None

    def ledger_failure_context(day: str, phase: str, symbols: tuple[str, ...]) -> str:
        actions = tuple(
            action
            for action in data.events
            if (previous_session is None or action.effective_date > previous_session)
            and action.effective_date <= day
        )
        bars = bars_by_day[day]
        relevant_symbols = tuple(
            sorted(set(symbols) | {a.symbol for a in actions} | {b.symbol for b in bars})
        )
        refs = tuple(
            sorted(
                {ref for action in actions for ref in action.source_refs}
                | {ref for bar in bars for ref in bar.source_refs}
            )
        )
        return (
            f"ledger context: market={data.market}, session={day}, phase={phase}, "
            f"symbols={relevant_symbols}, source_refs={refs}"
        )

    for day in all_sessions:
        if day < start:
            previous_session = day
            continue
        with localcontext(Context(prec=50, rounding=ROUND_HALF_EVEN)):
            try:
                update = ledger.begin_session(day, pending_symbols=tuple(o.symbol for o in pending))
            except ResearchError as exc:
                exc.add_note(
                    ledger_failure_context(
                        day, "corporate_action_terms", tuple(o.symbol for o in pending)
                    )
                )
                raise
            for order in pending:
                if order.symbol in update.changed_symbols:
                    rejects.append(Rejection(day, order, "corporate_action_invalidated_order"))
            pending_orders: list[MarketOrder] = [
                o for o in pending if o.symbol not in update.changed_symbols
            ]
            valid: list[tuple[MarketOrder, Bar]] = []
            for o in pending_orders:
                bar: Bar | None = next((b for b in bars_by_day[day] if b.symbol == o.symbol), None)
                reason = "unknown_symbol" if o.symbol not in data.symbols else "invalid_order"
                if (
                    not isinstance(o, MarketOrder)
                    or o.side not in {"buy", "sell"}
                    or not isinstance(o.quantity, Decimal)
                    or not o.quantity.is_finite()
                    or o.quantity <= 0
                ):
                    rejects.append(Rejection(day, o, reason))
                    continue
                if bar is None or bar.open is None or bar.open <= 0:
                    rejects.append(Rejection(day, o, "missing_or_nonpositive_open"))
                    continue
                if any(
                    stop_date <= day < effective_date
                    for stop_date, effective_date in stop_intervals.get(o.symbol, ())
                ):
                    rejects.append(Rejection(day, o, "known_stop_interval"))
                    continue
                valid.append((o, bar))
            # Validate whole net direction before any posting.
            sides: dict[str, set[str]] = {}
            valid.sort(key=lambda item: item[0].symbol)
            for o, _ in valid:
                sides.setdefault(o.symbol, set()).add(o.side)
            if any(len(v) > 1 for v in sides.values()):
                raise ResearchInputError("duplicate net directions for symbol")
            sells = []
            buys = []
            for o, b in valid:
                open_price = b.open
                if open_price is None:
                    raise ResearchInputError("validated bar open unexpectedly missing")
                if o.side == "sell":
                    qty = dict((p.symbol, p.quantity) for p in ledger.positions).get(
                        o.symbol, Decimal(0)
                    )
                    if o.quantity > qty:
                        rejects.append(Rejection(day, o, "insufficient_available_shares"))
                        continue
                    px = open_price * (1 - costs.slippage_bps / Decimal(10000))
                    n = px * o.quantity
                    fee = max(n * costs.fee_rate, costs.minimum_fee)
                    tax = n * costs.sell_tax_rate
                    sells.append((o, b, px, n, fee, tax))
                else:
                    px = open_price * (1 + costs.slippage_bps / Decimal(10000))
                    n = px * o.quantity
                    fee = max(n * costs.fee_rate, costs.minimum_fee)
                    buys.append((o, b, px, n, fee))
            if sum((n - f - t for _, _, _, n, f, t in sells), Decimal(0)) + ledger.cash < 0:
                rejects.extend(Rejection(day, x[0], "sell_batch_negative_cash") for x in sells)
                sells = []
            else:
                # Post cash-generating trades first so individually negative-net sales can
                # consume proceeds from this same-opening batch without transient financing.
                sells.sort(key=lambda x: (-(x[3] - x[4] - x[5]), x[0].symbol))
            for o, b, px, n, fee, tax in sells:
                ledger.apply_sell(o.symbol, o.quantity, px, fee + tax)
                if b.open is None:
                    raise ResearchInputError("validated bar open unexpectedly missing")
                fills.append(Fill(day, o, px, n, fee, tax, (b.open - px) * o.quantity))
            if sum((n + f for _, _, _, n, f in buys), Decimal(0)) > ledger.cash:
                rejects.extend(Rejection(day, x[0], "buy_batch_insufficient_cash") for x in buys)
            else:
                for o, b, px, n, fee in buys:
                    ledger.apply_buy(o.symbol, o.quantity, px, fee)
                    if b.open is None:
                        raise ResearchInputError("validated bar open unexpectedly missing")
                    fills.append(Fill(day, o, px, n, fee, Decimal(0), (px - b.open) * o.quantity))
            try:
                snapshot = ledger.close_session(day, bars_by_day[day])
            except ResearchError as exc:
                exc.add_note(
                    ledger_failure_context(
                        day, "rawclose_valuation", tuple(b.symbol for b in bars_by_day[day])
                    )
                )
                raise
            snapshots.append(snapshot)
            feature_now = MappingProxyType(
                {
                    name: MappingProxyType(dict(values))
                    for name, values in features_by_day.get(day, {}).items()
                }
            )
            view = DecisionView(data.at(day), snapshot, feature_now)
            try:
                with localcontext(Context(prec=50, rounding=ROUND_HALF_EVEN)):
                    raw = strategy(
                        view,
                        **{key: _copy_runtime(value) for key, value in runtime_params.items()},
                    )
            except Exception as exc:
                raise ResearchBacktestError(f"strategy failed on {day}") from exc
            if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
                raise ResearchInputError(f"strategy orders on {day} must be a sequence")
            strategy_orders = tuple(raw)
            for o in strategy_orders:
                if not isinstance(o, MarketOrder):
                    raise ResearchInputError(f"strategy returned malformed order on {day}")
                if not isinstance(o.symbol, str) or o.symbol not in data.symbols:
                    raise ResearchInputError(
                        f"strategy returned unknown symbol on {day}: {o.symbol}"
                    )
                if o.side not in {"buy", "sell"}:
                    raise ResearchInputError(f"strategy returned invalid side on {day}")
                if (
                    not isinstance(o.quantity, Decimal)
                    or not o.quantity.is_finite()
                    or o.quantity <= 0
                ):
                    raise ResearchInputError(f"strategy returned invalid quantity on {day}")
            if len({o.symbol for o in strategy_orders}) != len(strategy_orders):
                raise ResearchInputError(f"duplicate net directions on {day}")
            decisions.append(Decision(day, strategy_orders))
            pending = strategy_orders
            if day == end:
                break
        previous_session = day
    for o in pending:
        rejects.append(Rejection(end, o, "end_of_run"))
    rejects.sort(key=lambda item: (item.session, item.order.symbol, item.reason))
    return RunResult(
        data.input_digest,
        data.market,
        data.symbols,
        start,
        end,
        digest,
        frozen,
        params_digest,
        platform.python_version(),
        random_seed,
        costs,
        (
            "fractional_shares",
            "effective_date_proxy_when_event_dates_missing",
            "same_open_sell_proceeds_fund_buys",
            "no_liquidity_or_intraday_execution_claim",
            f"calendar:{data.calendar_basis}",
        ),
        tuple(snapshots),
        tuple(decisions),
        tuple(fills),
        tuple(rejects),
        ledger.events,
        initial_cash,
        data.start_date,
        data.end_date,
        resolved_program_path,
        tuple(sorted(str(path) for path in data.protected_input_dirs)),
        data.source_diagnostics,
        data.classification_diagnostics,
        tuple(
            sorted(
                {ref for bar in data.bars for ref in bar.source_refs}
                | {ref for event in data.events for ref in event.source_refs}
            )
        ),
        tuple(
            sorted(
                {ref for bar in data.bars for ref in bar.classification_refs}
                | {ref for event in data.events for ref in event.classification_refs}
            )
        ),
        data.calendar_basis,
        tuple(feature_inputs),
    )
