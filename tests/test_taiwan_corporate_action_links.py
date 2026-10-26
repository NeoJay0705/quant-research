from __future__ import annotations

import json
from copy import deepcopy
from decimal import Decimal

import pytest

from quant_research.taiwan import official, sources
from quant_research.taiwan.corporate_actions import _canon, build_corporate_actions


def parsed(source: str, dataset: str, params: dict[str, str], body: dict[str, object]):
    raw = sources.RawBatch(source, dataset, "synthetic", "", 200, json.dumps(body).encode(), "", 0)
    return official.parse(raw, params)


def fixtures(*, event_date="2018-01-11", summary_symbol="2330"):
    params = {"start_date": "2018-01-01", "end_date": "2018-01-31"}
    batch = parsed(
        "twse",
        "TWT49U",
        params,
        {
            "stat": "OK",
            "strDate": "20180101",
            "endDate": "20180131",
            "fields": ["資料日期", "股票代號", "除權息前收盤價", "除權息參考價", "詳細資料"],
            "data": [
                [
                    event_date,
                    summary_symbol,
                    "100",
                    "92",
                    f"{summary_symbol},{event_date.replace('-', '')}",
                ]
            ],
        },
    )
    summary = batch.records[0]
    parent = task(
        "p",
        "ps",
        params,
        dataset="TWT49U",
        validation="valid" if not batch.warnings else "period_envelope_unverified",
    )
    child_params = summary["detail_params"]
    detail_batch = parsed(
        "twse",
        "TWT49UDetail",
        child_params,
        {
            "stat": "OK",
            "fields": ["股票代號", "(每股配發現金股利)除息", "每仟股無償配股"],
            "data": [[summary_symbol, "8 元／股", "0"]],
        },
    )
    detail = detail_batch.records[0]
    detail["cash_dividend_per_share"] = "77"
    child = task(
        "c", "cs", deepcopy(child_params), dataset="TWT49UDetail", kind="official_event_detail"
    )
    context = {
        "symbol": summary_symbol,
        "event_date": event_date,
        "event_type": "dividend",
        "summary_dataset": "TWT49U",
        "summary_token": summary["original"]["詳細資料"],
    }
    rows = [
        {"record_id": "sr", "snapshot_id": "ps", "payload": summary},
        {"record_id": "dr", "snapshot_id": "cs", "payload": detail},
    ]
    link = {
        "parent_task_id": "p",
        "parent_snapshot_id": "ps",
        "child_task_id": "c",
        "context": context,
    }
    return summary, detail, parent, child, rows, link


def task(
    task_id,
    snapshot,
    request,
    *,
    dataset,
    kind="official_event_summary",
    source="twse",
    state="completed",
    validation="valid",
    logical=None,
    edition=1,
):
    return {
        "task_id": task_id,
        "logical_key": logical or task_id,
        "edition": edition,
        "source": source,
        "dataset": dataset,
        "kind": kind,
        "state": state,
        "snapshot_id": snapshot if state == "completed" else None,
        "validation_state": validation,
        "record_count": 1,
        "params": {"pipeline": "official-v1", "lane": "events", "request": request},
    }


def run(rows, links, tasks):
    return build_corporate_actions(rows, links, tasks)


def test_official_parse_link_uses_exact_parent_child_and_keeps_detail_date_null():
    summary, detail, parent, child, rows, link = fixtures()
    action = run(rows, [link], [parent, child])["actions"][0]
    assert (
        action["cash_dividend_per_share"],
        action["bonus_share_ratio"],
        action["share_multiplier"],
    ) == ("77", "0", "1")
    assert action["input_state"] == "complete"
    assert action["date_basis"]["evidence"] == [
        {"record_id": "sr", "snapshot_id": "ps", "task_id": "p"}
    ]
    assert action["field_provenance"]["cash_dividend_per_share"][0]["linked_parent_task_id"] == "p"
    assert detail["event_date"] is None


@pytest.mark.parametrize(
    "key", ["symbol", "event_date", "event_type", "summary_dataset", "summary_token"]
)
def test_each_context_identity_key_must_match(key):
    _, _, parent, child, rows, link = fixtures()
    link["context"][key] = "wrong"
    report = run(rows, [link], [parent, child])
    assert report["actions"][0]["cash_dividend_per_share"] is None
    assert not report["actions"][0]["field_provenance"].get("cash_dividend_per_share")
    assert "missing_required_detail" in report["actions"][0]["quality_flags"]
    assert report["actions"][0]["input_state"] == "incomplete"
    assert {d["reason"] for d in report["diagnostics"]["invalid_links"]} >= {
        "context_mismatch",
        "missing_required_detail",
    }


def test_context_allows_additional_metadata_but_parent_snapshot_must_match():
    _, _, parent, child, rows, link = fixtures()
    link["context"]["trace"] = "metadata"
    link["parent_snapshot_id"] = "wrong"
    report = run(rows, [link], [parent, child])
    action = report["actions"][0]
    assert action["cash_dividend_per_share"] is None
    assert "missing_required_detail" in action["quality_flags"]
    assert report["diagnostics"]["invalid_links"][0].get("reason")


@pytest.mark.parametrize(
    "mutate",
    [
        lambda c: c["params"]["request"].update(date="2018-01-12"),
        lambda c: c["params"]["request"].update(symbol="2317"),
        lambda c: c.update(state="pending", snapshot_id=None),
        lambda c: c.update(validation_state="invalid"),
        lambda c: c.update(source="tpex"),
        lambda c: c["params"].update(lane="quotes"),
        lambda c: c["params"].update(pipeline="other"),
    ],
)
def test_invalid_child_task_or_request_is_not_used(mutate):
    _, _, parent, child, rows, link = fixtures()
    mutate(child)
    report = run(rows, [link], [parent, child])
    action = report["actions"][0]
    assert action["cash_dividend_per_share"] is None
    assert not action["field_provenance"].get("cash_dividend_per_share")
    assert "missing_required_detail" in action["quality_flags"]
    assert report["diagnostics"]["invalid_links"]
    assert any(item.get("reason") for item in report["diagnostics"]["invalid_links"])


@pytest.mark.parametrize(
    "key,value",
    [
        ("source", "tpex"),
        ("dataset", "TWTAVUDetail"),
        ("kind", "quote"),
        ("symbol", "2317"),
        ("event_type", "par_value"),
        ("event_date", "2018-01-12"),
    ],
)
def test_child_payload_identity_mismatch_is_not_used(key, value):
    _, _, parent, child, rows, link = fixtures()
    rows[1]["payload"][key] = value
    report = run(rows, [link], [parent, child])
    assert report["actions"][0]["cash_dividend_per_share"] is None
    assert not report["actions"][0]["field_provenance"].get("cash_dividend_per_share")
    assert report["diagnostics"]["invalid_links"]
    assert report["diagnostics"]["invalid_links"][0]["reason"] in {
        "child_payload_mismatch",
        "child_event_date_mismatch",
    }


def test_only_null_child_event_date_inherits_parent_event_date_without_mutation():
    _, detail, parent, child, rows, link = fixtures()
    before = deepcopy(detail)
    report = run(rows, [link], [parent, child])
    assert report["actions"][0]["cash_dividend_per_share"] == "77"
    assert detail == before and detail["event_date"] is None


def test_global_newer_unlinked_child_is_not_substituted_for_linked_child():
    _, _, parent, child, rows, link = fixtures()
    newer = deepcopy(child)
    newer.update(task_id="c9", snapshot_id="cs9", edition=9)
    rows.append(
        {
            "record_id": "dr9",
            "snapshot_id": "cs9",
            "payload": {**rows[1]["payload"], "cash_dividend_per_share": "99"},
        }
    )
    action = run(rows, [link], [parent, child, newer])["actions"][0]
    assert action["cash_dividend_per_share"] == "77"
    assert all(p["task_id"] != "c9" for p in action["field_provenance"]["cash_dividend_per_share"])


def test_two_active_parents_with_distinct_linked_values_conflict_with_both_lineages():
    _, _, p1, child1, rows, link1 = fixtures()
    p2 = deepcopy(p1)
    p2.update(task_id="p2", snapshot_id="ps2", logical_key="p2")
    summary2 = deepcopy(rows[0]["payload"])
    rows.append({"record_id": "s2", "snapshot_id": "ps2", "payload": summary2})
    c2 = deepcopy(child1)
    c2.update(task_id="c2", snapshot_id="cs2")
    detail2 = deepcopy(rows[1]["payload"])
    detail2["cash_dividend_per_share"] = "9"
    rows.append({"record_id": "d2", "snapshot_id": "cs2", "payload": detail2})
    link2 = deepcopy(link1)
    link2.update(parent_task_id="p2", parent_snapshot_id="ps2", child_task_id="c2")
    action = run(rows, [link1, link2], [p1, p2, child1, c2])["actions"][0]
    assert action["cash_dividend_per_share"] is None
    assert "cash_dividend_per_share" in action["conflicts"]
    evidence = action["conflicts"]["cash_dividend_per_share"]
    lineages = {
        (e.get("snapshot_id"), e.get("task_id"), e.get("linked_parent_task_id")) for e in evidence
    }
    assert {("cs", "c", "p"), ("cs2", "c2", "p2")} <= lineages


def test_two_parents_sharing_child_count_one_duplicate_not_double_value():
    _, _, p1, child, rows, link1 = fixtures()
    p2 = deepcopy(p1)
    p2.update(task_id="p2", snapshot_id="ps2", logical_key="p2")
    rows.append({"record_id": "s2", "snapshot_id": "ps2", "payload": deepcopy(rows[0]["payload"])})
    link2 = deepcopy(link1)
    link2.update(parent_task_id="p2", parent_snapshot_id="ps2")
    action = run(rows, [link1, link2], [p1, p2, child])["actions"][0]
    assert action["cash_dividend_per_share"] == "77"
    assert action["summary_duplicate_count"] == 1
    assert len(action["date_basis"]["evidence"]) == 2


def test_decimal_negative_zero_is_canonical_zero():
    assert _canon(Decimal("-0")) == "0"


def test_refresh_keeps_exact_historical_child_without_current_conflict():
    _, _, p1, c1, rows, link1 = fixtures()
    p1.update(task_id="p1", logical_key="same", edition=1)
    link1["parent_task_id"] = "p1"
    p2 = deepcopy(p1)
    p2.update(task_id="p2", snapshot_id="ps2", edition=2)
    c2 = deepcopy(c1)
    c2.update(task_id="c2", snapshot_id="cs2", edition=2)
    rows.extend(
        [
            {"record_id": "sr2", "snapshot_id": "ps2", "payload": deepcopy(rows[0]["payload"])},
            {
                "record_id": "dr2",
                "snapshot_id": "cs2",
                "payload": {**deepcopy(rows[1]["payload"]), "cash_dividend_per_share": "9"},
            },
        ]
    )
    link2 = deepcopy(link1)
    link2.update(parent_task_id="p2", parent_snapshot_id="ps2", child_task_id="c2")
    report = run(rows, [link1, link2], [p1, c1, p2, c2])
    action = report["actions"][0]
    assert action["cash_dividend_per_share"] == "9"
    assert action["input_state"] == "complete"
    assert action["date_basis"]["evidence"] == [
        {"record_id": "sr2", "snapshot_id": "ps2", "task_id": "p2"}
    ]
    assert {v["task_id"] for v in action["scope_versions"]} >= {"p1", "p2", "c", "c2"}
    assert any(h["record_id"] == "dr" and h["task_id"] == "c" for h in action["field_history"])
    assert any(
        d.get("previous_record_id") == "sr" and "linked_child_record_id" in d["changes"]
        for d in report["diagnostics"]["revision_candidates"]
    )
    assert not action["conflicts"]


def b7_fixture():
    params = {"start_date": "2022-07-01", "end_date": "2022-07-31"}
    batch = parsed(
        "twse",
        "TWTB8U",
        params,
        {
            "stat": "OK",
            "strDate": "20220701",
            "endDate": "20220731",
            "fields": [
                "恢復買賣日期",
                "股票代號",
                "停止買賣前收盤價格",
                "恢復買賣參考價",
                "詳細資料",
            ],
            "data": [["2022-07-13", "6415", "2485", "621.25", "6415,20220706,20220713"]],
        },
    )
    summary = batch.records[0]
    parent = task(
        "bp",
        "bps",
        params,
        dataset="TWTB8U",
        validation="valid" if not batch.warnings else "period_envelope_unverified",
    )
    request = {"symbol": "6415", "file_date": "2022-07-05"}
    detail_batch = parsed(
        "twse",
        "TWTB7UDetail",
        request,
        {
            "stat": "OK",
            "fields": [
                "股票代號",
                "每股換發新股票",
                "變更前股票面額",
                "變更後股票面額",
                "停止買賣日期",
            ],
            "data": [["6415", "4", "10", "2.5", "2022-07-06"]],
        },
    )
    child = task("bc", "bcs", request, dataset="TWTB7UDetail", kind="official_event_detail")
    link = {
        "parent_task_id": "bp",
        "parent_snapshot_id": "bps",
        "child_task_id": "bc",
        "context": {
            "symbol": "6415",
            "event_date": "2022-07-13",
            "event_type": "par_value",
            "summary_dataset": "TWTB8U",
            "summary_token": "6415,20220706,20220713",
        },
    }
    return summary, detail_batch.records[0], parent, child, link


def test_explicit_b7_link_completes_par_value_and_null_detail_date_is_unchanged():
    summary, detail, parent, child, link = b7_fixture()
    rows = [
        {"record_id": "bsr", "snapshot_id": "bps", "payload": summary},
        {"record_id": "bdr", "snapshot_id": "bcs", "payload": detail},
    ]
    before = deepcopy(rows)
    action = run(rows, [link], [parent, child])["actions"][0]
    assert (action["share_multiplier"], action["par_before"], action["par_after"]) == (
        "4",
        "10",
        "2.5",
    )
    assert action["input_state"] == "complete"
    assert detail["event_date"] is None
    assert rows == before


@pytest.mark.parametrize("mutation", ["stop", "file_date"])
def test_b7_bad_identity_or_stop_does_not_supply_values(mutation):
    summary, detail, parent, child, link = b7_fixture()
    rows = [
        {"record_id": "bsr", "snapshot_id": "bps", "payload": summary},
        {"record_id": "bdr", "snapshot_id": "bcs", "payload": detail},
    ]
    if mutation == "stop":
        rows[1]["payload"]["stop_date"] = "2022-07-05"
    else:
        child["params"]["request"]["file_date"] = "20220706"
    action = run(rows, [link], [parent, child])["actions"][0]
    assert action["share_multiplier"] is None
    assert action["input_state"] == "incomplete"
    assert action["field_provenance"].get("share_multiplier") is None


def test_unlinked_details_are_diagnostic_only():
    _, detail, parent, child, rows, _ = fixtures()
    report = run(rows, [], [parent, child])
    assert report["actions"][0]["cash_dividend_per_share"] is None
    assert report["diagnostics"]["orphan_details"] == [
        {
            "record_id": "dr",
            "snapshot_id": "cs",
            "task_id": "c",
            "date_filter_applied": False,
            "reason": "not_linked_to_included_summary",
        }
    ]
