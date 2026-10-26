from __future__ import annotations

from copy import deepcopy

import pytest

from quant_research.taiwan.corporate_actions import build_corporate_actions


def task(
    task_id="task",
    *,
    source="twse",
    dataset="TWT49U",
    logical="scope/2024-01",
    edition=1,
    state="completed",
    validation="valid",
    count=1,
    request=None,
):
    return {
        "task_id": task_id,
        "logical_key": logical,
        "edition": edition,
        "source": source,
        "dataset": dataset,
        "kind": "official_event_summary",
        "state": state,
        "snapshot_id": f"snapshot-{task_id}" if state == "completed" else None,
        "validation_state": validation,
        "record_count": count,
        "fetched_at": f"2024-01-0{edition}T00:00:00Z",
        "sha256": f"hash-{task_id}",
        "raw_path": f"raw/{task_id}",
        "params": {
            "pipeline": "official-v1",
            "lane": "events",
            "request": request or {"start_date": "2024-01-01", "end_date": "2024-12-31"},
        },
    }


def payload(**kw):
    return {
        "kind": "event",
        "observation_type": "summary",
        "source": "twse",
        "dataset": "TWT49U",
        "event_type": "dividend",
        "symbol": "2330",
        "event_date": "2024-01-02",
        "cash_dividend_per_share": "8",
        "bonus_share_ratio": "0",
        "before_price": "100",
        "reference_price": "92",
        "reference_price_kind": "除權息參考價",
        **kw,
    }


def obs(t, rid, p):
    p = dict(p)
    p["source"] = t["source"]
    p["dataset"] = t["dataset"]
    p["event_type"] = {
        "TWT49U": "dividend",
        "TWTAUU": "capital_reduction",
        "TWTB8U": "par_value",
        "exDailyQ": "dividend",
        "revivt": "capital_reduction",
        "pvChgRslt": "par_value",
    }[t["dataset"]]
    return {"record_id": rid, "snapshot_id": t["snapshot_id"], "payload": p}


def build(ts, os, **kw):
    return build_corporate_actions(os, [], ts, **kw)


def test_filters_are_validated_before_processing_and_input_is_unchanged():
    obs_rows = [{"bad": "value"}]
    before = deepcopy(obs_rows)
    with pytest.raises(ValueError):
        build_corporate_actions(obs_rows, [], [], source="finmind")
    with pytest.raises(ValueError):
        build_corporate_actions(obs_rows, [], [], start="2024-02-30")
    assert obs_rows == before


def test_overlapping_current_scopes_conflict_and_decimal_values_are_not_duplicates():
    a, b = task("a"), task("b", logical="scope/overlap")
    report = build(
        [a, b], [obs(a, "ra", payload()), obs(b, "rb", payload(cash_dividend_per_share="9"))]
    )
    action = report["actions"][0]
    assert action["cash_dividend_per_share"] is None
    assert "cash_dividend_per_share" in action["conflicts"]
    assert action["input_state"] == "conflict"
    same = build(
        [a, b], [obs(a, "ra", payload()), obs(b, "rb", payload(cash_dividend_per_share="8.00"))]
    )["actions"][0]
    assert same["cash_dividend_per_share"] == "8"
    assert same["summary_duplicate_count"] == 1
    assert same["field_provenance"]["cash_dividend_per_share"]


def test_new_snapshot_with_same_financial_values_is_not_a_revision():
    old, new = task("old", edition=1), task("new", edition=2)
    report = build(
        [old, new],
        [obs(old, "oldrow", payload()), obs(new, "newrow", payload())],
    )
    action = report["actions"][0]
    assert action["input_state"] == "complete"
    assert {version["current"] for version in action["scope_versions"]} == {False, True}
    assert not report["diagnostics"]["revision_candidates"]


def test_newest_scope_value_supersedes_history_without_conflict():
    old, new = task("old", edition=1), task("new", edition=2)
    result = build(
        [old, new],
        [obs(old, "oldrow", payload()), obs(new, "newrow", payload(cash_dividend_per_share="9"))],
    )["actions"][0]
    assert result["cash_dividend_per_share"] == "9"
    assert result["conflicts"] == {}
    assert result["field_history"][0]["values"]["cash_dividend_per_share"] == "8"
    assert result["field_history"][0]["current"] is False


@pytest.mark.parametrize(
    "state,validation,count",
    [("pending", None, 0), ("failed", "invalid", 0), ("completed", "empty_unverified", 0)],
)
def test_unusable_latest_attempt_falls_back_to_old_snapshot_as_stale(state, validation, count):
    old, latest = (
        task("old", edition=1),
        task("latest", edition=2, state=state, validation=validation, count=count),
    )
    result = build([old, latest], [obs(old, "r", payload())])
    assert len(result["actions"]) == 1
    assert result["actions"][0]["input_state"] == "stale"
    assert result["diagnostics"]["stale_scopes"]


def test_fallback_uses_highest_usable_edition_not_all_old_editions():
    old, newest, failed = (
        task("old", edition=8),
        task("new", edition=9),
        task("failed", edition=10, state="failed", validation="invalid", count=0),
    )
    result = build(
        [old, newest, failed],
        [obs(old, "r8", payload()), obs(newest, "r9", payload(cash_dividend_per_share="9"))],
    )
    assert len(result["actions"]) == 1
    assert result["actions"][0]["cash_dividend_per_share"] == "9"
    assert result["actions"][0]["input_state"] == "stale"
    assert result["actions"][0]["input_state"] != "conflict"
    assert {v["edition"] for v in result["actions"][0]["scope_versions"]} == {8, 9}


def test_filtered_empty_selected_snapshot_does_not_revive_older_observation():
    old, selected, failed = (
        task("old", edition=1),
        task("selected", edition=2),
        task("failed", edition=3, state="failed", validation="invalid", count=0),
    )
    result = build(
        [old, selected, failed],
        [obs(old, "old", payload()), obs(selected, "other", payload(symbol="2317"))],
        symbol="2330",
    )
    assert result["actions"] == []


def test_payload_metadata_mismatch_is_rejected():
    t = task()
    for key, wrong in (
        ("source", "tpex"),
        ("dataset", "TWTAUU"),
        ("event_type", "par_value"),
        ("kind", "quote"),
    ):
        row = obs(t, "r", payload())
        row["payload"][key] = wrong
        result = build([t], [row])
        assert result["actions"] == []
        assert result["diagnostics"]["invalid_observations"]


def test_overlapping_multiplier_conflict_stays_null_despite_bonus_derivation():
    a, b = task("a"), task("b", logical="scope/other")
    rows = [
        obs(a, "a", payload(bonus_share_ratio="0.2", share_multiplier="1.2")),
        obs(b, "b", payload(bonus_share_ratio="0.2", share_multiplier="1.3")),
    ]
    result = build([a, b], rows)["actions"][0]
    assert result["share_multiplier"] is None
    assert {e["value"] for e in result["conflicts"]["share_multiplier"] if "value" in e} == {
        "1.2",
        "1.3",
    }


def test_no_usable_history_produces_no_action():
    t = task("bad", state="completed", validation="invalid")
    assert build([t], [obs(t, "r", payload())])["actions"] == []


def test_period_quality_flag_keeps_valid_snapshot_unverified():
    t = task(validation="valid")
    result = build([t], [obs(t, "r", payload(quality_flags=["period_envelope_unverified"]))])[
        "actions"
    ][0]
    assert result["input_state"] == "unverified"


def test_warning_snapshot_is_unverified_even_without_payload_flag():
    t = task(validation="period_envelope_unverified")
    result = build([t], [obs(t, "r", payload())])["actions"][0]
    assert result["input_state"] == "unverified"


def test_valid_nonempty_filtered_snapshot_does_not_resurrect_old_symbol():
    old, new = task("old", edition=1), task("new", edition=2)
    result = build(
        [old, new],
        [obs(old, "old", payload()), obs(new, "new", payload(symbol="2317"))],
        symbol="2330",
    )
    assert result["actions"] == []
    assert result["diagnostics"]["superseded_observations"]


@pytest.mark.parametrize("value", ["NA", None, "0", 0.5, True, "NaN", "Infinity", "-1"])
def test_invalid_or_missing_financial_values_never_complete(value):
    t = task()
    action = build([t], [obs(t, "r", payload(cash_dividend_per_share=value))])["actions"][0]
    if value in ("NA", None):
        assert action["cash_dividend_per_share"] is None
        assert "cash_dividend_per_share" in action["missing_fields"]
    elif value == "0":
        assert action["cash_dividend_per_share"] == "0"
        assert action["input_state"] == "complete"
    else:
        assert action["cash_dividend_per_share"] is None
        assert "invalid_financial_value" in action["quality_flags"]
        assert action["input_state"] == "incomplete"


@pytest.mark.parametrize(
    "field,value,dataset",
    [
        ("share_multiplier", "0", "TWTAUU"),
        ("share_multiplier", "-0.8", "TWTAUU"),
        ("par_before", "0", "TWTB8U"),
        ("before_price", "0", "TWT49U"),
        ("reference_price", "-1", "TWT49U"),
        ("before_price", float("inf"), "TWT49U"),
    ],
)
def test_nonpositive_multipliers_par_and_prices_are_invalid(field, value, dataset):
    t = task(dataset=dataset)
    action = build([t], [obs(t, "r", payload(**{field: value}))])["actions"][0]
    assert "invalid_financial_value" in action["quality_flags"]
    assert action["input_state"] == "incomplete"
    assert action[field] is None


def test_task_source_kind_lane_and_dataset_are_isolated():
    valid = task()
    wrong = task("wrong", source="tpex", dataset="TWT49U")
    legacy = task("legacy")
    legacy["kind"] = "official_quote"
    legacy["params"]["lane"] = "quotes"
    assert len(build([valid, wrong, legacy], [obs(valid, "r", payload())])["actions"]) == 1
    assert build([valid], [obs(valid, "r", payload())], source="tpex")["actions"] == []


def test_source_separation_financial_decimal_types_and_sorting():
    tw = task("tw", dataset="TWT49U")
    tp = task("tp", source="tpex", dataset="exDailyQ", logical="tpex/scope")
    later = payload(event_date="2024-02-02")
    report = build(
        [tp, tw], [obs(tp, "tp1", payload()), obs(tw, "tw1", later), obs(tw, "tw2", payload())]
    )
    assert [(a["event_date"], a["source"]) for a in report["actions"]] == [
        ("2024-01-02", "tpex"),
        ("2024-01-02", "twse"),
        ("2024-02-02", "twse"),
    ]
    assert report["actions"][0]["cash_dividend_per_share"] == "8"
    before = deepcopy([tw, tp])
    build([tw, tp], [obs(tw, "tw1", payload()), obs(tp, "tp1", payload())])
    assert [tw, tp] == before


def test_invalid_request_period_and_event_date_are_diagnostics():
    t = task(request={"start_date": "2024-02-01", "end_date": "2024-02-28"})
    result = build([t], [obs(t, "r", payload())])
    assert result["actions"] == []
    assert result["diagnostics"]["invalid_observations"]
    t2 = task("invalid-date")
    result = build([t2], [obs(t2, "r2", payload(event_date="2024-02-30"))])
    assert result["actions"] == []
    assert result["diagnostics"]["invalid_observations"]


def test_same_day_compound_actions_are_separate_and_not_double_applied():
    dividend = task("dividend", dataset="TWT49U", logical="dividend")
    reduction = task("reduction", dataset="TWTAUU", logical="reduction")
    reduction_payload = {
        "kind": "event",
        "observation_type": "summary",
        "source": "twse",
        "dataset": "TWTAUU",
        "event_type": "capital_reduction",
        "symbol": "2330",
        "event_date": "2024-01-02",
        "share_multiplier": "0.8",
        "cash_return_per_share": "2",
        "cash_dividend_per_share": "0",
        "before_price": "100",
        "reference_price": "122.5",
        "reference_price_kind": "恢復買賣參考價",
    }
    actions = build(
        [dividend, reduction],
        [obs(dividend, "d", payload()), obs(reduction, "r", reduction_payload)],
    )["actions"]
    assert len(actions) == 2
    assert {a["input_state"] for a in actions} == {"incomplete"}
    assert all(a["reference_check"]["status"] == "not_checked" for a in actions)
    assert actions[0]["compound_group_id"] == actions[1]["compound_group_id"]
    assert all(a["related_event_ids"] for a in actions)


def test_dividend_multiplier_is_derived_and_conflicting_explicit_value_is_not_overwritten():
    t = task()
    result = build([t], [obs(t, "r", payload(bonus_share_ratio="0.2"))])["actions"][0]
    assert result["share_multiplier"] == "1.2"
    assert result["field_provenance"]["share_multiplier"][0]["derived"] is True
    conflict = build([t], [obs(t, "r", payload(bonus_share_ratio="0.2", share_multiplier="1.3"))])[
        "actions"
    ][0]
    assert conflict["share_multiplier"] is None
    assert "share_multiplier" in conflict["conflicts"]
