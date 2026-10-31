"""Discoverable public research API catalog; importing it performs no market-data I/O."""

from __future__ import annotations

import inspect
from collections.abc import Callable
from dataclasses import dataclass

from .backtest import backtest, target_weights
from .features import analyze_feature, compute_feature, forward_returns
from .performance import compare_runs, daily_returns, summarize
from .results import save_run
from .taiwan import load_taiwan


@dataclass(frozen=True, slots=True)
class Capability:
    """One public operation, its callable signature, usage and scope limitations."""

    name: str
    import_path: str
    signature: str
    purpose: str
    output: str
    example: str
    limitations: tuple[str, ...]


_COMMON_LIMITS = (
    "Trusted callbacks are not sandboxed; prefix views do not guarantee point-in-time correctness.",
)


def _capability(
    function: Callable[..., object],
    purpose: str,
    output: str,
    example: str,
    limitations: tuple[str, ...] = (),
) -> Capability:
    return Capability(
        function.__name__,
        f"quant_research.research.{function.__name__}",
        str(inspect.signature(function)),
        purpose,
        output,
        example,
        limitations,
    )


def capabilities() -> tuple[Capability, ...]:
    """Return immutable capabilities and honest limitations; does not load market data."""
    return (
        _capability(
            compute_feature,
            "Compute custom per-symbol values once per session from prefix-limited TimeView.",
            "FeatureSeries: digest, name, params, and FeatureValue records "
            "(date, symbol, value, reason).",
            "from decimal import Decimal\n"
            "from quant_research.research import compute_feature\n"
            "feature = compute_feature(data,\n"
            "    lambda view: {s: Decimal(1) for s in view.symbols}, name='constant')",
            _COMMON_LIMITS,
        ),
        _capability(
            forward_returns,
            "Create adjusted-close price-return labels at market-session horizons.",
            "LabelSeries of ForwardReturnLabel(date, symbol, horizon, end_date, value, reason).",
            "from quant_research.research import forward_returns\n"
            "labels = forward_returns(data, (1,))",
            (
                "Labels exclude dividends and are not portfolio returns.",
                "Missing endpoints and horizons outside the supplied data are explicit.",
            ),
        ),
        _capability(
            analyze_feature,
            "Group feature values independently each date and summarize forward labels.",
            "FeatureAnalysis: input_digest, bounds, daily and summary tuples of group records.",
            "from quant_research.research import analyze_feature\n"
            "analysis = analyze_feature(feature, labels, 2, data.start_date, data.end_date)",
            (
                "Labels count only when both endpoints are inside the research/OOS slice.",
                "Group summaries equally average available daily means; not portfolio returns.",
            ),
        ),
        _capability(
            backtest,
            "Run a deterministic single-market strategy using next-session open fills.",
            "RunResult: snapshots, decisions, fills, rejections, events and provenance; summary().",
            "from decimal import Decimal\n"
            "from quant_research.research import CostModel, backtest\n"
            "run = backtest(data, lambda view: (), initial_cash=Decimal('1000'),\n"
            "    costs=CostModel(Decimal(0), Decimal(0), Decimal(0), Decimal(0)),\n"
            "    start_date=data.start_date, end_date=data.end_date)",
            _COMMON_LIMITS
            + (
                "Uses next-open fills, explicit costs, fractional shares and supplied calendar.",
                "No liquidity, queue, intraday-limit or market-impact simulation.",
                "Event dates may use the ledger effective-date proxy.",
            ),
        ),
        _capability(
            summarize,
            "Summarize final equity, drawdown and explicit cost attribution.",
            "PerformanceSummary fields include initial_cash, final_nav, net_pnl, return, drawdown, "
            "fees, tax, slippage, total_cost and before_cost_same_fill_pnl.",
            "from quant_research.research import summarize\nsummary = summarize(run)",
        ),
        _capability(
            compare_runs,
            "Compare named runs as scenarios and report differences and summaries.",
            "RunComparison: summaries, differences (RunDifference), warnings and interpretation.",
            "from quant_research.research import compare_runs\n"
            "comparison = compare_runs({'baseline': run_a, 'variant': run_b})",
            (
                "Comparison is not guaranteed to isolate parameter changes; "
                "closure state and inputs may differ.",
            ),
        ),
        _capability(
            save_run,
            "Persist a completed run with provenance, detail tables, hashes and completion marker.",
            "Path to immutable run directory; ResearchSaveError on unsafe or failed persistence.",
            "from pathlib import Path\n"
            "from quant_research.research import save_run\n"
            "saved = save_run(run, Path('artifacts/my-run'))",
            (
                "Writes only to a new destination; refuses protected input storage "
                "and symlink paths.",
            ),
        ),
        _capability(
            target_weights,
            "Convert long-only portfolio weights into close-sized market orders.",
            "Tuple of MarketOrder instructions based on current NAV and known positive closes.",
            "from decimal import Decimal\n"
            "from quant_research.research import target_weights\n"
            "orders = target_weights(view, {'2330': Decimal('0.8')}, Decimal('0.1'))",
            (
                "cash_reserve is a fraction in [0,1]; weights are nonnegative and sum to at most "
                "1-cash_reserve.",
                "Sizing uses known closes; pending rights count as exposure but are not sellable.",
                "Gaps, costs and order rejection may prevent realized weights from matching "
                "targets.",
            ),
        ),
        _capability(
            daily_returns,
            "Compute daily and cumulative returns and drawdown from a completed run.",
            "Tuple of DailyReturn records; daily_return is None with undefined_reason after "
            "zero NAV.",
            "from quant_research.research import daily_returns\nreturns = daily_returns(run)",
            (
                "The first daily return uses initial_cash as its baseline.",
                "A zero previous NAV yields None with reason 'previous_nav_zero'.",
                "Returns are not annualized.",
            ),
        ),
        _capability(
            load_taiwan,
            "Read an explicitly scoped Taiwan dataset into immutable ResearchData.",
            "ResearchData: market, symbols, sessions, bars, events, diagnostics and input_digest.",
            "from quant_research.research import load_taiwan\n"
            "data = load_taiwan(root, 'twse', ('2330',), start_date, end_date)",
            ("Reads local Taiwan data only; explicit universe/bounds; not a market downloader.",),
        ),
    )
