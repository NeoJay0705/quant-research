"""JSON command-line interface for local Taiwan data."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence

from .official_workflow import OfficialWorkflow
from .schedule import write_units
from .storage import DEFAULT_ROOT, TaiwanData, WriterBusyError


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="quant-data")
    p.add_argument("--root", default=str(DEFAULT_ROOT))
    sub = p.add_subparsers(dest="command", required=True)
    s = sub.add_parser("sync")
    s.add_argument("--expected-date")
    s.add_argument("--max-tasks", type=int)
    s.add_argument("--markets", nargs="+", choices=("twse", "tpex"))
    evsync = sub.add_parser("sync-events")
    evsync.add_argument("--start", required=True)
    evsync.add_argument("--end")
    evsync.add_argument("--markets", nargs="+", choices=("twse", "tpex"))
    evsync.add_argument("--max-tasks", type=int)
    evsync.add_argument("--refresh", action="store_true")
    maintain = sub.add_parser("maintain-events")
    maintain.add_argument("--expected-date")
    maintain.add_argument("--max-tasks", type=int)
    maintain.add_argument("--markets", nargs="+", choices=("twse", "tpex"))
    maintain.add_argument("--recent-months", type=int, default=2)
    maintain.add_argument("--detail-refresh-days", type=int, default=7)
    universe = sub.add_parser("observation-universe")
    universe.add_argument("--date", required=True)
    universe.add_argument("--source", choices=("official", "twse", "tpex"), default="official")
    universe.add_argument("--limit", type=int, default=20000)
    universe.add_argument("--offset", type=int, default=0)
    availability = sub.add_parser("availability")
    availability.add_argument("--start", required=True)
    availability.add_argument("--end", required=True)
    availability.add_argument("--source", choices=("official", "twse", "tpex"), default="official")
    b = sub.add_parser("backfill")
    b.add_argument("--start", default="2018-01-01")
    b.add_argument("--end")
    b.add_argument("--max-tasks", type=int)
    b.add_argument("--refresh", action="store_true")
    b.add_argument("--markets", nargs="+", choices=("twse", "tpex"))
    r = sub.add_parser("resume")
    r.add_argument("--max-tasks", type=int)
    r.add_argument("--lane", default="quotes")
    sub.add_parser("status")
    symbols = sub.add_parser("symbols")
    symbols.add_argument(
        "--source", choices=("official", "twse", "tpex", "finmind"), default="official"
    )
    q = sub.add_parser("quotes")
    q.add_argument("--symbol", required=True)
    q.add_argument("--source", required=True)
    q.add_argument("--start")
    q.add_argument("--end")
    e = sub.add_parser("events")
    e.add_argument("--symbol")
    e.add_argument("--dataset")
    e.add_argument("--source", choices=("official", "twse", "tpex", "finmind"), default="official")
    e.add_argument("--include-unverified", action="store_true")
    actions = sub.add_parser("corporate-actions")
    actions.add_argument("--symbol")
    actions.add_argument("--source", choices=("official", "twse", "tpex"), default="official")
    actions.add_argument("--start")
    actions.add_argument("--end")
    links = sub.add_parser("event-links")
    links.add_argument("--symbol")
    links.add_argument("--source", choices=("official", "twse", "tpex"), default="official")
    links.add_argument("--include-history", action="store_true")
    coverage = sub.add_parser("event-coverage")
    coverage.add_argument("--source", choices=("official", "twse", "tpex"), default="official")
    schedule = sub.add_parser("write-schedule")
    schedule.add_argument("--directory", required=True)
    schedule.add_argument("--lane", choices=("quotes", "events", "all"), default="quotes")
    return p


def main(argv: Sequence[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if args.command == "write-schedule":
        try:
            print(
                json.dumps({"created": write_units(args.directory, root=args.root, lane=args.lane)})
            )
            return 0
        except (OSError, ValueError) as exc:
            print(json.dumps({"error": str(exc)}), file=sys.stderr)
            return 2
    try:
        result: object
        if args.command in {
            "status",
            "symbols",
            "quotes",
            "events",
            "corporate-actions",
            "event-links",
            "event-coverage",
            "observation-universe",
            "availability",
        }:
            data = TaiwanData(args.root)
        if args.command == "status":
            result = data.status()
        elif args.command == "symbols":
            result = data.symbols(source=args.source)
        elif args.command == "quotes":
            result = data.quotes(args.symbol, source=args.source, start=args.start, end=args.end)
        elif args.command == "events":
            result = data.events(
                args.symbol,
                dataset=args.dataset,
                source=args.source,
                include_unverified=args.include_unverified,
            )
        elif args.command == "corporate-actions":
            result = data.corporate_actions(
                args.symbol, source=args.source, start=args.start, end=args.end
            )
        elif args.command == "event-links":
            result = data.event_links(
                args.symbol, source=args.source, include_history=args.include_history
            )
        elif args.command == "event-coverage":
            result = data.event_coverage(source=args.source)
        elif args.command == "observation-universe":
            result = data.observation_universe(
                args.date, source=args.source, limit=args.limit, offset=args.offset
            )
        elif args.command == "availability":
            result = data.availability(args.start, args.end, source=args.source)
        else:
            workflow = OfficialWorkflow(args.root)
            if args.command == "sync":
                result = workflow.sync(args.expected_date, args.max_tasks, markets=args.markets)
            elif args.command == "maintain-events":
                result = workflow.maintain_events(
                    args.expected_date,
                    args.max_tasks,
                    markets=args.markets,
                    recent_months=args.recent_months,
                    detail_refresh_days=args.detail_refresh_days,
                )
            elif args.command == "sync-events":
                result = workflow.sync_events(
                    args.start,
                    args.end,
                    markets=args.markets,
                    max_tasks=args.max_tasks,
                    refresh=args.refresh,
                )
            elif args.command == "backfill":
                result = workflow.backfill(
                    args.start,
                    args.end,
                    markets=args.markets,
                    max_tasks=args.max_tasks,
                    refresh=args.refresh,
                )
            else:
                result = workflow.resume(args.max_tasks, lane=args.lane)
        print(json.dumps(result, ensure_ascii=False, default=str))
        if isinstance(result, dict) and result.get("state") in {
            "failed",
            "blocked",
            "partial",
            "interrupted",
        }:
            return 1
        return 0
    except WriterBusyError as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        return 75
    except ValueError as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        return 2
    except Exception as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
