from __future__ import annotations

import importlib.util
from pathlib import Path

from quant_research.research.taiwan import _build_actions, _build_bars, load_taiwan
from quant_research.taiwan.dataset.build import _iter_series_from_reader
from quant_research.taiwan.dataset.classification import EvidenceSet
from quant_research.taiwan.dataset.models import BuildSpec
from quant_research.taiwan.dataset.source import RecordResult, SourceReader


def _module(filename: str, name: str):
    path = Path(__file__).with_name(filename)
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


def test_conversion_helpers_match_load_taiwan_for_linked_detail_event(tmp_path: Path) -> None:
    research_tests = _module("test_research_taiwan.py", "helper_research_fixtures")
    event_fixtures = _module("test_taiwan_dataset_events.py", "helper_event_fixtures")
    research_tests._install_linked_twse_research_fixture(
        tmp_path,
        "TWT49U",
        event_fixtures.twse_summary(
            "TWT49U",
            ["資料日期", "股票代號", "除權息前收盤價", "除權息參考價", "詳細資料"],
            ["2024-03-01", "2330", "100", "90", "2330,20240301"],
        ),
        {
            "stat": "OK",
            "fields": [
                "股票代號",
                "(每股配發現金股利)除息",
                "A. 按普通股股東持股比例每千股無償配股",
            ],
            "data": [["2330", "2", "125"]],
        },
    )
    expected = load_taiwan(tmp_path, "twse", ("2330",), "2024-02-29", "2024-03-01")
    spec = BuildSpec(tmp_path, "2024-02-29", "2024-03-01", symbols=("2330",), sources=("twse",))
    with SourceReader(spec) as reader:
        evidence = EvidenceSet.from_manifest(None)
        series = tuple(_iter_series_from_reader(reader, evidence))
        quote_flags: dict[tuple[str, str], tuple[str, ...]] = {}
        scopes = reader.scopes()
        for scope in scopes:
            if scope.lane == "quotes":
                for record in reader.iter_all_records(scope):
                    if record.payload is not None:
                        quote_flags[
                            (
                                str(record.payload.get("symbol", "")),
                                str(record.payload.get("trade_date", "")),
                            )
                        ] = record.quality_flags
        bars = tuple(
            bar
            for item in series
            for bar in _build_bars(
                "twse",
                item.symbol,
                item.raw_quotes,
                {row.quote.date: row for row in item.quotes},
                item.classifications,
                lambda quote: quote_flags.get((quote.symbol, quote.date), ()),
            )
        )
        scope_by_ref = {(scope.task_id, scope.snapshot_id): scope for scope in scopes}
        link_cache = {
            (scope.task_id, scope.snapshot_id): reader.links(scope)
            for scope in scopes
            if scope.lane == "events"
        }
        summaries = {
            (scope.task_id, scope.snapshot_id, record.record_id): record
            for scope in scopes
            if scope.lane == "events"
            for record in reader.iter_all_records(scope)
        }
        child_cache: dict[tuple[int, str, str], tuple[RecordResult, ...]] = {}
        actions = tuple(
            action
            for item in series
            for action in _build_actions(
                "twse",
                item.symbol,
                item.events,
                next((bar for bar in bars if bar.symbol == item.symbol), None),
                reader,
                scope_by_ref,
                link_cache,
                summaries,
                child_cache,
            )
        )
    assert bars == expected.bars
    assert actions == expected.events
    assert actions and actions[0].source_refs == (
        "TWT49U:snap-parent-parent-1:1",
        "TWT49UDetail:snap-child-parent-1:2",
    )
