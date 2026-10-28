from __future__ import annotations

import hashlib
import json
import sqlite3
from decimal import Decimal
from pathlib import Path

import pytest

from quant_research.taiwan import official, sources
from quant_research.taiwan.dataset.events import load_events
from quant_research.taiwan.dataset.models import BuildSpec, DatasetError
from quant_research.taiwan.dataset.source import SourceReader
from quant_research.taiwan.storage import SCHEMA


def batch(source, dataset, payload):
    return sources.RawBatch(
        source,
        dataset,
        "offline",
        "https://example.invalid/",
        200,
        json.dumps(payload, ensure_ascii=False).encode(),
        "",
        0,
    )


def parsed(source, dataset, payload, request):
    return official.parse(batch(source, dataset, payload), request)


def install(
    root: Path,
    *,
    parent_dataset: str,
    summary: dict,
    child_dataset: str | None = None,
    detail: dict | None = None,
    edition: int = 1,
    fetched: str = "2024-03-01",
    child_available: bool = True,
    mutate=None,
    logical_key: str = "parent",
    request: dict | None = None,
):
    root.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(root / "state.sqlite3")
    db.executescript(SCHEMA)
    parent_params = {
        "pipeline": "official-v1",
        "lane": "events",
        "request": request or {"start_date": "2024-01-01", "end_date": "2024-12-31"},
    }
    p = parsed("twse", parent_dataset, summary, parent_params["request"])

    def store(task, logical, ed, dataset, params, result, stamp, state="completed"):
        sid = f"snap-{task}" if state == "completed" else None
        if sid:
            db.execute(
                "INSERT INTO snapshots VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    sid,
                    "twse",
                    dataset,
                    json.dumps(params, sort_keys=True),
                    "url",
                    logical,
                    stamp,
                    200,
                    hashlib.sha256(task.encode()).hexdigest(),
                    "raw",
                    "valid",
                    None,
                ),
            )
            for i, rec in enumerate(result.records):
                rec = dict(rec)
                if mutate:
                    rec = mutate(rec)
                db.execute(
                    "INSERT INTO records(snapshot_id,kind,symbol,record_date,record_key,payload,"
                    "quality_flags) VALUES(?,?,?,?,?,?,?)",
                    (
                        sid,
                        "event",
                        rec.get("symbol"),
                        rec.get("event_date"),
                        str(i),
                        json.dumps(rec, ensure_ascii=False),
                        json.dumps(rec.get("quality_flags", [])),
                    ),
                )
        db.execute(
            "INSERT INTO tasks(id,logical_key,edition,source,dataset,params,kind,state,"
            "snapshot_id) VALUES(?,?,?,?,?,?,?,?,?)",
            (
                task,
                logical,
                ed,
                "twse",
                dataset,
                json.dumps(params, sort_keys=True),
                "official_event_summary"
                if dataset in {"TWT49U", "TWTAUU", "TWTB8U"}
                else "official_event_detail",
                state,
                sid,
            ),
        )
        return sid

    parent_sid = store(
        f"parent-{logical_key}-{edition}",
        logical_key,
        edition,
        parent_dataset,
        parent_params,
        p,
        fetched,
    )
    if child_dataset and detail is not None:
        parent_rec = p.records[0]
        req = dict(parent_rec["detail_params"])
        context = {
            "symbol": parent_rec["symbol"],
            "event_date": parent_rec["event_date"],
            "event_type": parent_rec["event_type"],
            "summary_dataset": parent_dataset,
            "summary_token": parent_rec["original"]["詳細資料"],
        }
        cparams = {"pipeline": "official-v1", "lane": "events", "request": req, "context": {}}
        cp = parsed("twse", child_dataset, detail, req)
        store(
            f"child-{logical_key}-{edition}",
            f"child-{logical_key}",
            edition,
            child_dataset,
            cparams,
            cp,
            fetched,
            "completed" if child_available else "failed",
        )
        db.execute(
            "INSERT INTO official_event_links(parent_task_id,parent_snapshot_id,child_task_id,"
            "context) VALUES(?,?,?,?)",
            (
                f"parent-{logical_key}-{edition}",
                parent_sid,
                f"child-{logical_key}-{edition}",
                json.dumps(context, sort_keys=True),
            ),
        )
    db.commit()
    db.close()
    return BuildSpec(root, "2024-01-01", "2024-12-31")


def add_twse_parent(
    root: Path,
    *,
    dataset: str,
    summary: dict,
    logical: str,
    edition: int,
    fetched: str,
    detail: dict | None = None,
    link: bool = True,
    child_available: bool = True,
    request: dict | None = None,
):
    """Append another real parsed summary scope (and its exact child, when requested)."""
    db = sqlite3.connect(root / "state.sqlite3")
    params = {
        "pipeline": "official-v1",
        "lane": "events",
        "request": request or {"start_date": "2024-01-01", "end_date": "2024-12-31"},
    }
    result = parsed("twse", dataset, summary, params["request"])
    task = f"parent-{logical}-{edition}"
    snapshot = f"snap-{task}"
    db.execute(
        "INSERT INTO snapshots VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            snapshot,
            "twse",
            dataset,
            json.dumps(params, sort_keys=True),
            "url",
            logical,
            fetched,
            200,
            hashlib.sha256(task.encode()).hexdigest(),
            "raw",
            "valid",
            None,
        ),
    )
    for index, record in enumerate(result.records):
        db.execute(
            "INSERT INTO records(snapshot_id,kind,symbol,record_date,record_key,payload,"
            "quality_flags) "
            "VALUES(?,?,?,?,?,?,?)",
            (
                snapshot,
                "event",
                record["symbol"],
                record["event_date"],
                str(index),
                json.dumps(record, ensure_ascii=False),
                "[]",
            ),
        )
    db.execute(
        "INSERT INTO tasks(id,logical_key,edition,source,dataset,params,kind,state,snapshot_id) "
        "VALUES(?,?,?,?,?,?,?,?,?)",
        (
            task,
            logical,
            edition,
            "twse",
            dataset,
            json.dumps(params, sort_keys=True),
            "official_event_summary",
            "completed",
            snapshot,
        ),
    )
    if detail is not None:
        parent = result.records[0]
        child_dataset = "TWTAVUDetail" if dataset == "TWTAUU" else "TWT49UDetail"
        child_task = f"child-{logical}-{edition}"
        child_params = {
            "pipeline": "official-v1",
            "lane": "events",
            "request": parent["detail_params"],
            "context": {},
        }
        child = parsed("twse", child_dataset, detail, parent["detail_params"])
        child_snapshot = f"snap-{child_task}" if child_available else None
        if child_snapshot:
            db.execute(
                "INSERT INTO snapshots VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    child_snapshot,
                    "twse",
                    child_dataset,
                    json.dumps(child_params, sort_keys=True),
                    "url",
                    f"child-{logical}",
                    fetched,
                    200,
                    hashlib.sha256(child_task.encode()).hexdigest(),
                    "raw",
                    "valid",
                    None,
                ),
            )
            for index, record in enumerate(child.records):
                db.execute(
                    "INSERT INTO records(snapshot_id,kind,symbol,record_date,record_key,payload,"
                    "quality_flags) "
                    "VALUES(?,?,?,?,?,?,?)",
                    (
                        child_snapshot,
                        "event",
                        record["symbol"],
                        record.get("event_date"),
                        str(index),
                        json.dumps(record, ensure_ascii=False),
                        "[]",
                    ),
                )
        db.execute(
            "INSERT INTO tasks(id,logical_key,edition,source,dataset,params,kind,state,"
            "snapshot_id) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (
                child_task,
                f"child-{logical}",
                edition,
                "twse",
                child_dataset,
                json.dumps(child_params, sort_keys=True),
                "official_event_detail",
                "completed" if child_available else "failed",
                child_snapshot,
            ),
        )
        if link:
            db.execute(
                "INSERT INTO official_event_links(parent_task_id,parent_snapshot_id,child_task_id,"
                "context) "
                "VALUES(?,?,?,?)",
                (
                    task,
                    snapshot,
                    child_task,
                    json.dumps(
                        {
                            "symbol": parent["symbol"],
                            "event_date": parent["event_date"],
                            "event_type": parent["event_type"],
                            "summary_dataset": dataset,
                            "summary_token": parent["original"]["詳細資料"],
                        },
                        sort_keys=True,
                    ),
                ),
            )
    db.commit()
    db.close()


def twse_summary(dataset, fields, row):
    return {
        "stat": "OK",
        "fields": fields,
        "data": [row],
        "strDate": "113年01月01日",
        "endDate": "113年12月31日",
    }


def test_actual_twse_dividend_summary_and_linked_child_exact_ratios(tmp_path):
    spec = install(
        tmp_path,
        parent_dataset="TWT49U",
        summary=twse_summary(
            "TWT49U",
            ["資料日期", "股票代號", "除權息前收盤價", "除權息參考價", "詳細資料"],
            ["2024-03-01", "2330", "100", "90", "2330,20240301"],
        ),
        child_dataset="TWT49UDetail",
        detail={
            "stat": "OK",
            "fields": [
                "股票代號",
                "(每股配發現金股利)除息",
                "A. 按普通股股東持股比例每千股無償配股",
            ],
            "data": [["2330", "2", "125"]],
        },
    )
    with SourceReader(spec) as reader:
        (event,) = load_events(reader)
    assert event.dividend == Decimal("2") and event.bonus_share_ratio == Decimal("0.125")


def test_actual_twse_reduction_child_ratio_return_and_dividend(tmp_path):
    spec = install(
        tmp_path,
        parent_dataset="TWTAUU",
        summary=twse_summary(
            "TWTAUU",
            ["恢復買賣日期", "股票代號", "停止買賣前收盤價格", "恢復買賣參考價", "詳細資料"],
            ["2024-03-01", "2330", "10", "9", "2330,20240102"],
        ),
        child_dataset="TWTAVUDetail",
        detail={
            "stat": "OK",
            "fields": ["股票代號", "每壹仟股換發新股票", "每股退還股款", "(每股配發現金股利)除息"],
            "data": [["2330", "940 股", "0.6 元/股", "0.1 元/股"]],
        },
    )
    with SourceReader(spec) as reader:
        (event,) = load_events(reader)
    assert event.share_multiplier == Decimal("0.94")
    assert event.cash_return_per_share == Decimal("0.6") and event.dividend == Decimal("0.1")


def test_actual_inline_tpex_missing_optional_cash_and_bonus_none(tmp_path):
    payload = {
        "stat": "ok",
        "fields": ["除權息日期", "代號", "除權息前收盤價", "除權息參考價", "每仟股無償配股"],
        "data": [["113/03/01", "2330", "100", "90", "NA"]],
        "date": "113/01/01~113/12/31",
    }
    # Separate root avoids conflating source scopes.
    root = tmp_path / "tpex"
    install_tpex(root, payload)
    with SourceReader(BuildSpec(root, "2024-01-01", "2024-12-31")) as reader:
        (event,) = load_events(reader)
    assert event.dividend == 0 and event.bonus_share_ratio is None


def install_tpex(root, payload):
    root.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(root / "state.sqlite3")
    db.executescript(SCHEMA)
    params = {
        "pipeline": "official-v1",
        "lane": "events",
        "request": {"start_date": "2024-01-01", "end_date": "2024-12-31"},
    }
    result = parsed("tpex", "exDailyQ", payload, params["request"])
    db.execute(
        "INSERT INTO snapshots VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "s",
            "tpex",
            "exDailyQ",
            json.dumps(params, sort_keys=True),
            "url",
            "q",
            "2024-03-01",
            200,
            "h",
            "raw",
            "valid",
            None,
        ),
    )
    for i, r in enumerate(result.records):
        db.execute(
            "INSERT INTO records(snapshot_id,kind,symbol,record_date,record_key,payload,"
            "quality_flags) VALUES(?,?,?,?,?,?,?)",
            ("s", "event", r["symbol"], r["event_date"], str(i), json.dumps(r), "[]"),
        )
    db.execute(
        "INSERT INTO tasks(id,logical_key,edition,source,dataset,params,kind,state,"
        "snapshot_id) VALUES(?,?,?,?,?,?,?,?,?)",
        (
            "t",
            "q",
            1,
            "tpex",
            "exDailyQ",
            json.dumps(params, sort_keys=True),
            "official_event_summary",
            "completed",
            "s",
        ),
    )
    db.commit()
    db.close()


def test_actual_inline_tpex_rev_and_pv_and_twse_b8_need_no_child(tmp_path):
    for source, dataset, fields, row in [
        (
            "tpex",
            "revivt",
            [
                "恢復買賣日期",
                "股票代號",
                "最後交易日之收盤價格",
                "減資恢復買賣開始日參考價格",
                "詳細資料",
            ],
            [
                "113/03/01",
                "2330",
                "10",
                "12",
                "<table><tr><td>股票代號/股票名稱</td><td>2330/測</td><td>每壹仟股換發新股票</td><td>900</td></tr></table>",
            ],
        ),
        (
            "tpex",
            "pvChgRslt",
            ["恢復買賣日期", "證券代號", "最後交易日之收盤價格", "恢復買賣開始參考價", "詳細資料"],
            [
                "113/03/01",
                "2330",
                "10",
                "12",
                "<table><tr><td>股票代號/股票名稱</td><td>2330/測</td><td>每股換發新股票</td><td>0.5</td></tr></table>",
            ],
        ),
        (
            "twse",
            "TWTB8U",
            ["恢復買賣日期", "股票代號", "停止買賣前收盤價格", "恢復買賣參考價", "詳細資料"],
            ["2024-03-01", "2330", "10", "12", "2330,20240102,20240301"],
        ),
    ]:
        root = tmp_path / dataset
        root.mkdir()
        req = {"start_date": "2024-01-01", "end_date": "2024-12-31"}
        payload = {
            "stat": "OK",
            "fields": fields,
            "data": [row],
            "strDate": "113年01月01日",
            "endDate": "113年12月31日",
        }
        if source == "tpex":
            payload = {"stat": "ok", "fields": fields, "data": [row], "date": "113/01/01~113/12/31"}
        if source == "twse":
            install(root, parent_dataset=dataset, summary=payload)
        else:
            install_inline(root, source, dataset, payload, req)
        with SourceReader(BuildSpec(root, "2024-01-01", "2024-12-31")) as reader:
            (e,) = load_events(reader)
        assert e.event_type in {"capital_reduction", "par_value"}


def install_inline(root, source, dataset, payload, request):
    root.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(root / "state.sqlite3")
    db.executescript(SCHEMA)
    params = {"pipeline": "official-v1", "lane": "events", "request": request}
    result = parsed(source, dataset, payload, request)
    db.execute(
        "INSERT INTO snapshots VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "s",
            source,
            dataset,
            json.dumps(params, sort_keys=True),
            "url",
            "q",
            "2024-03-01",
            200,
            "h",
            "raw",
            "valid",
            None,
        ),
    )
    for i, r in enumerate(result.records):
        db.execute(
            "INSERT INTO records(snapshot_id,kind,symbol,record_date,record_key,payload,"
            "quality_flags) VALUES(?,?,?,?,?,?,?)",
            ("s", "event", r["symbol"], r["event_date"], str(i), json.dumps(r), "[]"),
        )
    db.execute(
        "INSERT INTO tasks(id,logical_key,edition,source,dataset,params,kind,state,"
        "snapshot_id) VALUES(?,?,?,?,?,?,?,?,?)",
        (
            "t",
            "q",
            1,
            source,
            dataset,
            json.dumps(params, sort_keys=True),
            "official_event_summary",
            "completed",
            "s",
        ),
    )
    db.commit()
    db.close()


def test_malformed_optional_and_required_values_fail_with_context(tmp_path):
    # Real inline summary values are parsed by official.parse; mutate stored records only to
    # exercise JSON type/finite-value rejection that the official parser correctly prevents.
    for index, val in enumerate((True, 1.25, float("nan"), "nonnumeric")):
        root = tmp_path / str(index)
        payload = {
            "stat": "ok",
            "fields": ["除權息日期", "代號", "現金股利"],
            "data": [["113/03/01", "2330", "1"]],
            "date": "113/01/01~113/12/31",
        }
        install_inline(
            root,
            "tpex",
            "exDailyQ",
            payload,
            {"start_date": "2024-01-01", "end_date": "2024-12-31"},
        )
        db = sqlite3.connect(root / "state.sqlite3")
        raw = db.execute("select payload from records").fetchone()[0]
        rec = json.loads(raw)
        rec["cash_dividend_per_share"] = val
        rec["field_conflicts"] = {}
        db.execute("update records set payload=?", (json.dumps(rec),))
        db.commit()
        db.close()
        match = (
            "tpex/exDailyQ task=t record=1"
            if isinstance(val, float) and val != val
            else "tpex.*2330.*2024-03-01"
        )
        with (
            SourceReader(BuildSpec(root, "2024-01-01", "2024-12-31")) as reader,
            pytest.raises(DatasetError, match=match),
        ):
            load_events(reader)


def test_newest_overlapping_parent_uses_only_its_exact_child(tmp_path):
    summary = twse_summary(
        "TWTAUU",
        ["恢復買賣日期", "股票代號", "停止買賣前收盤價格", "恢復買賣參考價", "詳細資料"],
        ["2024-03-01", "2330", "10", "9", "2330,20240102"],
    )
    summary["strDate"] = "2024-01-01"
    summary["endDate"] = "2024-06-30"
    spec = install(
        tmp_path,
        parent_dataset="TWTAUU",
        summary=summary,
        logical_key="older-window",
        request={"start_date": "2024-01-01", "end_date": "2024-06-30"},
    )
    newer_summary = twse_summary(
        "TWTAUU",
        ["恢復買賣日期", "股票代號", "停止買賣前收盤價格", "恢復買賣參考價", "詳細資料"],
        ["2024-03-01", "2330", "10", "9", "2330,20240202"],
    )
    newer_summary["strDate"] = "2024-02-01"
    newer_summary["endDate"] = "2024-12-31"
    add_twse_parent(
        tmp_path,
        dataset="TWTAUU",
        summary=newer_summary,
        logical="newer-window",
        edition=1,
        fetched="2024-04-01",
        request={"start_date": "2024-02-01", "end_date": "2024-12-31"},
        detail={
            "stat": "OK",
            "fields": ["股票代號", "每壹仟股換發新股票", "每股退還股款"],
            "data": [["2330", "800 股", "0.7 元/股"]],
        },
    )
    with SourceReader(spec) as reader:
        (event,) = load_events(reader)
    assert event.share_multiplier == Decimal("0.8")
    assert event.cash_return_per_share == Decimal("0.7")
    assert event.ref.task_id == "parent-newer-window-1"
    assert event.ref.detail_task_id == "child-newer-window-1"


def test_older_selected_summary_uses_linked_detail_not_newer_unlinked_detail(tmp_path):
    spec = install(
        tmp_path,
        parent_dataset="TWTAUU",
        summary=twse_summary(
            "TWTAUU",
            ["恢復買賣日期", "股票代號", "停止買賣前收盤價格", "恢復買賣參考價", "詳細資料"],
            ["2024-03-01", "2330", "10", "9", "2330,20240102"],
        ),
        child_dataset="TWTAVUDetail",
        detail={
            "stat": "OK",
            "fields": ["股票代號", "每壹仟股換發新股票", "每股退還股款"],
            "data": [["2330", "900 股", "0.2 元/股"]],
        },
    )
    db = sqlite3.connect(tmp_path / "state.sqlite3")
    params = {
        "pipeline": "official-v1",
        "lane": "events",
        "request": {"symbol": "2330", "file_date": "2024-03-01"},
        "context": {},
    }
    payload = {
        "stat": "OK",
        "fields": ["股票代號", "每壹仟股換發新股票", "每股退還股款"],
        "data": [["2330", "500 股", "0.9 元/股"]],
    }
    result = parsed("twse", "TWTAVUDetail", payload, params["request"])
    db.execute(
        "INSERT INTO snapshots VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "unlinked-new",
            "twse",
            "TWTAVUDetail",
            json.dumps(params, sort_keys=True),
            "url",
            "unlinked",
            "2025-01-01",
            200,
            "unlinked-hash",
            "raw",
            "valid",
            None,
        ),
    )
    for index, record in enumerate(result.records):
        db.execute(
            "INSERT INTO records(snapshot_id,kind,symbol,record_date,record_key,payload,"
            "quality_flags) "
            "VALUES(?,?,?,?,?,?,?)",
            (
                "unlinked-new",
                "event",
                record["symbol"],
                record.get("event_date"),
                str(index),
                json.dumps(record, ensure_ascii=False),
                "[]",
            ),
        )
    db.execute(
        "INSERT INTO tasks(id,logical_key,edition,source,dataset,params,kind,state,snapshot_id) "
        "VALUES(?,?,?,?,?,?,?,?,?)",
        (
            "unlinked-new-task",
            "unlinked",
            1,
            "twse",
            "TWTAVUDetail",
            json.dumps(params, sort_keys=True),
            "official_event_detail",
            "completed",
            "unlinked-new",
        ),
    )
    db.commit()
    db.close()
    with SourceReader(spec) as reader:
        (event,) = load_events(reader)
    assert event.share_multiplier == Decimal("0.9")
    assert event.cash_return_per_share == Decimal("0.2")
    assert event.ref.detail_task_id == "child-parent-1"


@pytest.mark.parametrize("failure", ["missing", "failed", "tampered_context"])
def test_selected_required_child_failure_never_substitutes(tmp_path, failure):
    spec = install(
        tmp_path,
        parent_dataset="TWTAUU",
        summary=twse_summary(
            "TWTAUU",
            ["恢復買賣日期", "股票代號", "停止買賣前收盤價格", "恢復買賣參考價", "詳細資料"],
            ["2024-03-01", "2330", "10", "9", "2330,20240102"],
        ),
        child_dataset="TWTAVUDetail",
        detail={
            "stat": "OK",
            "fields": ["股票代號", "每壹仟股換發新股票"],
            "data": [["2330", "900 股"]],
        },
        child_available=failure != "failed",
    )
    db = sqlite3.connect(tmp_path / "state.sqlite3")
    if failure == "missing":
        db.execute("DELETE FROM official_event_links")
    elif failure == "tampered_context":
        db.execute("UPDATE official_event_links SET context='{}'")
    db.commit()
    db.close()
    with (
        SourceReader(spec) as reader,
        pytest.raises(DatasetError, match="required .*detail|child unavailable"),
    ):
        load_events(reader)


def test_out_of_spec_event_is_ignored_without_its_required_detail(tmp_path):
    payload = twse_summary(
        "TWTAUU",
        ["恢復買賣日期", "股票代號", "停止買賣前收盤價格", "恢復買賣參考價", "詳細資料"],
        ["2024-03-01", "2330", "10", "9", "2330,20240201"],
    )
    payload["data"].append(["2024-01-02", "2330", "10", "9", "2330,20231201"])
    install(
        tmp_path,
        parent_dataset="TWTAUU",
        summary=payload,
        child_dataset="TWTAVUDetail",
        detail={
            "stat": "OK",
            "fields": ["股票代號", "每壹仟股換發新股票"],
            "data": [["2330", "900 股"]],
        },
    )
    narrowed = BuildSpec(tmp_path, "2024-02-01", "2024-06-30")
    with SourceReader(narrowed) as reader:
        (event,) = load_events(reader)
    assert event.effective_date == "2024-03-01"


@pytest.mark.parametrize("value", [None, "0", "-0.1"])
def test_capital_reduction_requires_positive_share_multiplier(tmp_path, value):
    spec = install(
        tmp_path,
        parent_dataset="TWTAUU",
        summary=twse_summary(
            "TWTAUU",
            ["恢復買賣日期", "股票代號", "停止買賣前收盤價格", "恢復買賣參考價", "詳細資料"],
            ["2024-03-01", "2330", "10", "9", "2330,20240102"],
        ),
        child_dataset="TWTAVUDetail",
        detail={
            "stat": "OK",
            "fields": ["股票代號", "每股退還股款"],
            "data": [["2330", "0.2 元/股"]],
        },
    )
    db = sqlite3.connect(tmp_path / "state.sqlite3")
    row = db.execute(
        "SELECT id,payload FROM records WHERE snapshot_id='snap-child-parent-1'"
    ).fetchone()
    rec = json.loads(row[1])
    rec["share_multiplier"] = value
    db.execute("UPDATE records SET payload=? WHERE id=?", (json.dumps(rec), row[0]))
    db.commit()
    db.close()
    with (
        SourceReader(spec) as reader,
        pytest.raises(DatasetError, match="positive share_multiplier"),
    ):
        load_events(reader)


@pytest.mark.parametrize("price", [None, "0", "-1"])
def test_par_value_fallback_requires_positive_prices(tmp_path, price):
    row = ["2024-03-01", "2330", "10", "9", "2330,20240102,20240301"]
    spec = install(
        tmp_path,
        parent_dataset="TWTB8U",
        summary=twse_summary(
            "TWTB8U",
            ["恢復買賣日期", "股票代號", "停止買賣前收盤價格", "恢復買賣參考價", "詳細資料"],
            row,
        ),
    )
    db = sqlite3.connect(tmp_path / "state.sqlite3")
    record_id, raw = db.execute("SELECT id,payload FROM records").fetchone()
    rec = json.loads(raw)
    rec["share_multiplier"] = None
    rec["before_price"] = price
    db.execute("UPDATE records SET payload=? WHERE id=?", (json.dumps(rec), record_id))
    db.commit()
    db.close()
    with SourceReader(spec) as reader, pytest.raises(DatasetError, match="positive before_price"):
        load_events(reader)


def test_actual_source_conflict_fails_but_unused_conflict_is_ignored(tmp_path):
    payload = {
        "stat": "ok",
        "fields": ["除權息日期", "代號", "現金股利", "每仟股無償配股"],
        "data": [["113/03/01", "2330", "8", "0"]],
        "date": "113/01/01~113/12/31",
    }
    for conflict, fails in [("cash_dividend_per_share", True), ("cash_return_per_share", False)]:
        root = tmp_path / conflict
        install_inline(
            root,
            "tpex",
            "exDailyQ",
            payload,
            {"start_date": "2024-01-01", "end_date": "2024-12-31"},
        )
        db = sqlite3.connect(root / "state.sqlite3")
        raw = db.execute("select payload from records").fetchone()[0]
        rec = json.loads(raw)
        rec["field_conflicts"] = {conflict: ["1", "2"]}
        db.execute("update records set payload=?", (json.dumps(rec),))
        db.commit()
        db.close()
        with SourceReader(BuildSpec(root, "2024-01-01", "2024-12-31")) as reader:
            if fails:
                with pytest.raises(DatasetError, match="cash_dividend_per_share"):
                    load_events(reader)
            else:
                assert load_events(reader)
