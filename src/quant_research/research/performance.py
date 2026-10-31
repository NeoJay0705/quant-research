"""Auditable performance summaries and explicit run-to-run scenario comparisons."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import ROUND_HALF_EVEN, Context, Decimal, localcontext
from types import MappingProxyType

from .backtest import RunResult
from .models import ResearchInputError


@dataclass(frozen=True, slots=True)
class DailyReturn:
    date: str
    nav: Decimal
    daily_return: Decimal | None
    cumulative_return: Decimal
    drawdown: Decimal
    undefined_reason: str | None = None


@dataclass(frozen=True, slots=True)
class PerformanceSummary:
    initial_cash: Decimal
    final_nav: Decimal
    net_pnl: Decimal
    total_return: Decimal
    max_drawdown: Decimal
    fees: Decimal
    tax: Decimal
    slippage: Decimal
    total_cost: Decimal
    before_cost_same_fill_pnl: Decimal


@dataclass(frozen=True, slots=True)
class RunDifference:
    field: str
    baseline_name: str
    run_name: str
    baseline_value: object
    value: object


@dataclass(frozen=True, slots=True)
class RunComparison:
    summaries: Mapping[str, PerformanceSummary]
    differences: tuple[RunDifference, ...]
    warnings: tuple[str, ...]
    interpretation: str


_FIELDS: tuple[str, ...] = (
    "input_digest",
    "market",
    "symbols",
    "load_start",
    "load_end",
    "start_date",
    "end_date",
    "initial_cash",
    "params_digest",
    "params",
    "strategy_sha256",
    "program_path",
    "random_seed",
    "costs",
    "feature_inputs",
    "calendar_basis",
    "assumptions",
    "classification_diagnostics",
    "classification_refs",
    "source_diagnostics",
    "source_refs",
    "python_version",
)


def _decimal(value: Decimal, field: str) -> Decimal:
    if not isinstance(value, Decimal) or not value.is_finite():
        raise ResearchInputError(f"{field} must be a finite Decimal")
    return value


def daily_returns(run: RunResult) -> tuple[DailyReturn, ...]:
    """Compute daily returns against initial_cash and snapshots; no duplicate account ledger."""
    if not isinstance(run, RunResult):
        raise ResearchInputError("run must be RunResult")
    with localcontext(Context(prec=50, rounding=ROUND_HALF_EVEN)):
        initial = _decimal(run.initial_cash, "initial_cash")
        if initial <= 0:
            raise ResearchInputError("initial_cash must be positive")
        peak = initial
        previous = initial
        result: list[DailyReturn] = []
        for snapshot in run.snapshots:
            nav = _decimal(snapshot.nav, "snapshot NAV")
            if nav < 0:
                raise ResearchInputError("snapshot NAV cannot be negative")
            reason = "previous_nav_zero" if previous == 0 else None
            daily = None if reason else nav / previous - 1
            peak = max(peak, nav)
            result.append(
                DailyReturn(snapshot.session, nav, daily, nav / initial - 1, 1 - nav / peak, reason)
            )
            previous = nav
        return tuple(result)


def summarize(run: RunResult) -> PerformanceSummary:
    """Summarize final equity and explicit cost attribution at 50-digit precision."""
    if not isinstance(run, RunResult):
        raise ResearchInputError("run must be RunResult")
    with localcontext(Context(prec=50, rounding=ROUND_HALF_EVEN)):
        initial = _decimal(run.initial_cash, "initial_cash")
        if initial <= 0:
            raise ResearchInputError("initial_cash must be positive")
        final = _decimal(run.snapshots[-1].nav if run.snapshots else initial, "final NAV")
        fees = sum((fill.fee for fill in run.fills), Decimal(0))
        tax = sum((fill.sell_tax for fill in run.fills), Decimal(0))
        slippage = sum((fill.slippage_cost for fill in run.fills), Decimal(0))
        for name, amount in (("fees", fees), ("tax", tax), ("slippage", slippage)):
            _decimal(amount, name)
        costs = fees + tax + slippage
        net = final - initial
        series = daily_returns(run)
        drawdown = max((item.drawdown for item in series), default=Decimal(0))
        return PerformanceSummary(
            initial,
            final,
            net,
            final / initial - 1,
            drawdown,
            fees,
            tax,
            slippage,
            costs,
            net + costs,
        )


def compare_runs(namedruns: Mapping[str, RunResult]) -> RunComparison:
    """Compare named runs against the first as scenarios, never as certified parameter-only."""
    if not isinstance(namedruns, Mapping) or not namedruns:
        raise ResearchInputError("namedruns must be a non-empty mapping")
    items = tuple(namedruns.items())
    if any(not isinstance(name, str) or not name.strip() for name, _ in items):
        raise ResearchInputError("run names must be non-empty strings")
    if len({name for name, _ in items}) != len(items):
        raise ResearchInputError("run names must be unique")
    if any(not isinstance(run, RunResult) for _, run in items):
        raise ResearchInputError("all values must be RunResult")
    baseline_name, baseline = items[0]
    differences: list[RunDifference] = []
    for name, run in items[1:]:
        for field in _FIELDS:
            left, right = getattr(baseline, field), getattr(run, field)
            if left != right:
                differences.append(RunDifference(field, baseline_name, name, left, right))
    warnings: tuple[str, ...] = ()
    if any(run.strategy_sha256 is None or run.program_path is None for _, run in items):
        warnings = (
            "program identity is incomplete (missing program hash/path); "
            "arbitrary closure state is unknown",
        )
    summaries = MappingProxyType({name: summarize(run) for name, run in items})
    return RunComparison(
        summaries,
        tuple(differences),
        warnings,
        "Runs are compared as scenarios; differences do not establish identical inputs "
        "or isolate parameters.",
    )
