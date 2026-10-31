"""Clearly synthetic, in-memory-only dataset shared by the teaching examples."""

from decimal import Decimal

from quant_research.research import Bar, ResearchData, SourceDiagnostic

SESSIONS = tuple(f"2024-01-0{day}" for day in range(1, 6))
_RAW = {
    "A": ((10, 10), (10, 11), (12, 12), (12, 11), (12, 13)),
    "B": ((20, 20), (20, 19), (19, 18), (18, 20), (20, 22)),
}


def teaching_data() -> ResearchData:
    """Build marked synthetic OHLC data; no local source or network is consulted."""
    bars = tuple(
        Bar(
            market="teaching",
            symbol=symbol,
            date=day,
            open=Decimal(open_price),
            close=Decimal(close_price),
            adjusted_close=Decimal(close_price),
            classification="ordinary_rule_limited",
            classification_reason="synthetic teaching data, not real-source evidence",
        )
        for symbol, rows in sorted(_RAW.items())
        for day, (open_price, close_price) in zip(SESSIONS, rows, strict=True)
    )
    return ResearchData(
        market="teaching",
        symbols=tuple(_RAW),
        start_date=SESSIONS[0],
        end_date=SESSIONS[-1],
        sessions=SESSIONS,
        bars=bars,
        calendar_basis="synthetic_teaching_sessions",
        source_diagnostics=(
            SourceDiagnostic(
                "synthetic",
                "teaching",
                "not-a-real-source",
                None,
                len(bars),
                ("synthetic; not real-source data",),
            ),
        ),
        classification_diagnostics=(
            "synthetic bars use ordinary_rule_limited; not real-source evidence",
        ),
    )
