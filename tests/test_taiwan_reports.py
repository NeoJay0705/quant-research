# ruff: noqa: E501

import json
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlparse

import pytest

from quant_research.taiwan import official, reports, sources
from quant_research.taiwan.official_workflow import OfficialWorkflow
from quant_research.taiwan.storage import SQLiteStore, TaiwanData


def test_missing_database_and_validation_before_path_access(tmp_path):
    missing = tmp_path / "absent"
    api = TaiwanData(missing)
    with pytest.raises(ValueError):
        api.observation_universe("2026-02-30")
    with pytest.raises(ValueError):
        api.availability("2026-01-01", "2027-01-02")
    assert not missing.exists()
    report = api.availability("2026-01-01", "2026-01-02")
    assert report["schema_version"] == "data-availability-v1"
    assert len(report["quote_days"]) == 4
    assert all(day["state"] == "unplanned" for day in report["quote_days"])


def _official_quote_body(source, day):
    if source == "twse":
        table = {
            "title": "每日收盤行情",
            "fields": [
                "證券代號",
                "證券名稱",
                "開盤價",
                "最高價",
                "最低價",
                "收盤價",
                "成交股數",
                "成交金額",
            ],
            "data": [["2330", "測試公司", "10", "11", "9", "10", "100", "1000"]],
        }
        return {"stat": "OK", "date": day.replace("-", ""), "tables": [table]}
    y, m, d = map(int, day.split("-"))
    return {
        "stat": "ok",
        "date": f"{y - 1911:03d}/{m:02d}/{d:02d}",
        "tables": [
            {
                "title": "一般",
                "fields": ["代號", "名稱", "開盤", "最高", "最低", "收盤", "成交股數", "成交金額"],
                "data": [["6488", "測試TPEx fixture", "10", "11", "9", "10", "100", "1000"]],
            },
            {
                "title": "管理股票",
                "fields": ["代號", "名稱", "開盤", "最高", "最低", "收盤", "成交股數", "成交金額"],
                "data": [["A123", "管理股票 fixture", "10", "11", "9", "10", "100", "1000"]],
            },
        ],
    }


def _workflow_reports(tmp_path):
    clock = [datetime(2026, 10, 24, tzinfo=UTC)]

    def transport(url, headers, timeout, max_bytes):
        source = "twse" if "twse.com" in url else "tpex"
        val = parse_qs(urlparse(url).query)["date"][0]
        day = val.replace("/", "-")
        if source == "twse":
            day = f"{val[:4]}-{val[4:6]}-{val[6:8]}"
        return 200, json.dumps(_official_quote_body(source, day), ensure_ascii=False).encode()

    def sleep(seconds):
        clock[0] += timedelta(seconds=seconds)

    workflow = OfficialWorkflow(tmp_path, transport=transport, now=lambda: clock[0], sleep=sleep)
    result = workflow.backfill("2018-01-02", "2018-01-03")
    assert result["state"] == "completed", result
    return TaiwanData(tmp_path)


def test_official_workflow_shared_quote_reports_cover_both_sources(tmp_path):
    api = _workflow_reports(tmp_path)
    for day in ("2018-01-02", "2018-01-03"):
        universe = api.observation_universe(date=day)
        assert {m["source"] for m in universe["members"]} == {"twse", "tpex"}
        assert all(m["classification"] == "unknown" for m in universe["members"])
        assert all(m["provenance"][0]["task_id"] for m in universe["members"])
        store = SQLiteStore(tmp_path)
        with store.connect() as db:
            for member in universe["members"]:
                for ref in member["provenance"]:
                    row = db.execute(
                        "SELECT t.id task_id,t.source,s.dataset,s.id snapshot_id,s.sha256,s.raw_path "
                        "FROM tasks t JOIN snapshots s ON s.id=t.snapshot_id WHERE t.id=?",
                        (ref["task_id"],),
                    ).fetchone()
                    assert row is not None
                    assert {
                        key: ref[key]
                        for key in (
                            "task_id",
                            "source",
                            "dataset",
                            "snapshot_id",
                            "sha256",
                            "raw_path",
                        )
                    } == dict(row)
        report = api.availability(day, day)
        assert {q["source"] for q in report["quote_days"] if q["state"] == "available"} == {
            "twse",
            "tpex",
        }
        scopes = {
            q["source"]: q["scopes"][0]["record_count"]
            for q in report["quote_days"]
            if q["state"] == "available"
        }
        assert scopes == {"twse": 1, "tpex": 2}
        members = universe["members"]
        assert any(m["source"] == "twse" for m in members)
        assert any(m["source"] == "tpex" for m in members)
        assert all(m["classification"] == "unknown" for m in members)
        management_member = next(
            m
            for m in members
            if m["venue_id"] == "management" and m["source"] == "tpex" and m["symbol"] == "A123"
        )
        assert management_member["venue"] == "管理股票"
        assert management_member["stored_venue_id"] == "management"
        assert management_member["venue_basis"] == "stored_official_quote_table_title"
        store = SQLiteStore(tmp_path)
        with store.connect() as db:
            payload = db.execute(
                "SELECT r.payload FROM records r JOIN snapshots s ON s.id=r.snapshot_id WHERE s.source='tpex' AND r.symbol='A123'"
            ).fetchone()["payload"]
        assert json.loads(payload)["venue"] == "管理股票"
    first = api.observation_universe("2018-01-02", limit=1)
    second = api.observation_universe("2018-01-02", limit=1, offset=1)
    assert first["snapshot_fingerprint"] == second["snapshot_fingerprint"]


def test_quote_queries_bound_record_stats_to_requested_day(tmp_path, monkeypatch):
    api = _workflow_reports(tmp_path)
    calls = []
    real_record_stats = reports.record_stats

    def recording_record_stats(db, wanted):
        calls.append([dict(scope) for scope in wanted])
        return real_record_stats(db, wanted)

    monkeypatch.setattr(reports, "record_stats", recording_record_stats)
    api.observation_universe("2018-01-02")
    api.availability("2018-01-02", "2018-01-02")
    quote_calls = [
        invocation
        for invocation in calls
        if invocation and all(scope.get("kind") == "quote" for scope in invocation)
    ]
    assert len(quote_calls) == 2
    assert all(
        scope["start"] == scope["end"] == "2018-01-02"
        for invocation in quote_calls
        for scope in invocation
    )
    store = SQLiteStore(tmp_path)
    with store.connect() as db:
        expected_snapshot_ids = {
            row[0]
            for row in db.execute(
                "SELECT DISTINCT s.id FROM tasks t JOIN snapshots s ON s.id=t.snapshot_id "
                "WHERE t.kind='official_quote' AND t.expected_date='2018-01-02'"
            )
        }
    assert expected_snapshot_ids
    assert all(
        {scope["snapshot_id"] for scope in invocation} == expected_snapshot_ids
        for invocation in quote_calls
    )


def test_latest_pending_and_failed_quote_tasks_do_not_fallback(tmp_path):
    api = _workflow_reports(tmp_path)
    store = SQLiteStore(tmp_path)
    with store.connect() as db:
        old = db.execute(
            "SELECT * FROM tasks WHERE source='twse' AND expected_date='2018-01-02'"
        ).fetchone()
    for edition_offset, state in enumerate(("pending", "failed"), start=1):
        with store.connect() as db:
            db.execute(
                "INSERT INTO tasks(id,logical_key,edition,source,dataset,params,kind,state,expected_date) VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    f"new-{state}",
                    old["logical_key"],
                    old["edition"] + edition_offset,
                    old["source"],
                    old["dataset"],
                    json.dumps(
                        {
                            "pipeline": "official-v1",
                            "lane": "quotes",
                            "request": {"date": "2018-01-02"},
                        }
                    ),
                    "official_quote",
                    state,
                    "2018-01-02",
                ),
            )
        # Exercise each committed latest state independently without deleting tasks.
        universe = api.observation_universe("2018-01-02", source="twse")
        availability = api.availability("2018-01-02", "2018-01-02", source="twse")
        assert universe["total_count"] == 0
        assert availability["quote_days"][0]["state"] == state


def test_invalid_quote_request_is_diagnosed_in_its_expected_date_slot(tmp_path):
    api = _workflow_reports(tmp_path)
    store = SQLiteStore(tmp_path)
    with store.connect() as db:
        task = db.execute(
            "SELECT id FROM tasks WHERE source='twse' AND expected_date='2018-01-02' "
            "ORDER BY edition DESC LIMIT 1"
        ).fetchone()
        db.execute("UPDATE tasks SET params=? WHERE id=?", ("{invalid", task["id"]))

    assert api.observation_universe("2018-01-02", source="twse")["total_count"] == 0
    report = api.availability("2018-01-02", "2018-01-02", source="twse")
    quote_day = report["quote_days"][0]
    assert quote_day["state"] == "invalid"
    assert quote_day["scopes"][0]["task_id"] == task["id"]
    assert quote_day["scopes"][0]["request_date"] is None
    assert quote_day["scopes"][0]["planned_date"] == "2018-01-02"
    assert report["quote_diagnostics"][0]["task_id"] == task["id"]
    assert report["quote_diagnostics"][0]["reason"] == "latest_scope_not_eligible"


def test_quote_metadata_corruption_invalidates_only_affected_source(tmp_path):
    api = _workflow_reports(tmp_path)
    store = SQLiteStore(tmp_path)
    with store.connect() as db:
        row = db.execute(
            "SELECT r.id,r.payload FROM records r JOIN snapshots s ON s.id=r.snapshot_id "
            "WHERE s.source='twse' AND r.record_date='2018-01-02' LIMIT 1"
        ).fetchone()
        payload = json.loads(row["payload"])
        payload["trade_date"] = "2018-01-03"
        db.execute("UPDATE records SET payload=? WHERE id=?", (json.dumps(payload), row["id"]))
    universe = api.observation_universe("2018-01-02")
    assert universe["total_count"] == 2
    assert {member["source"] for member in universe["members"]} == {"tpex"}
    availability = api.availability("2018-01-02", "2018-01-02")
    by_source = {day["source"]: day["state"] for day in availability["quote_days"]}
    assert by_source == {"twse": "invalid", "tpex": "available"}


def test_observation_universe_uses_real_parser_and_pages_stably(tmp_path):
    store = SQLiteStore(tmp_path)
    store.initialize()
    params = {
        "pipeline": "official-v1",
        "lane": "quotes",
        "request": {"date": "2018-01-02"},
        "context": {},
    }
    parsed = official.parse(
        sources.RawBatch(
            "twse",
            "MI_INDEX",
            "offline",
            "",
            200,
            json.dumps(
                {
                    "stat": "OK",
                    "date": "20180102",
                    "tables": [
                        {
                            "fields": [
                                "證券代號",
                                "證券名稱",
                                "開盤價",
                                "最高價",
                                "最低價",
                                "收盤價",
                                "成交股數",
                                "成交金額",
                            ],
                            "data": [
                                ["006201", "ETF fixture", "10", "11", "9", "10", "100", "1000"],
                                ["A123", "管理股 fixture", "10", "11", "9", "10", "100", "1000"],
                            ],
                            "title": "每日收盤行情",
                        }
                    ],
                }
            ).encode(),
            "",
            0,
        ),
        {"date": "2018-01-02"},
    )
    canonical = json.dumps(params, sort_keys=True)
    sid = store.save_snapshot(
        source="twse",
        dataset="MI_INDEX",
        params=params,
        safe_url="offline",
        scope_key="2018-01-02",
        fetched_at="2026-10-24T00:00:00+00:00",
        http_status=200,
        sha256="fixture",
        raw_path="raw/fixture",
        validation_state="valid",
        records=tuple(parsed.records),
    )
    logical = "fixture-scope"
    with store.connect() as db:
        db.execute(
            "INSERT INTO tasks(id,logical_key,edition,source,dataset,params,kind,state,expected_date,snapshot_id) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                "task",
                logical,
                1,
                "twse",
                "MI_INDEX",
                canonical,
                "official_quote",
                "completed",
                "2018-01-02",
                sid,
            ),
        )
    api = TaiwanData(tmp_path)
    first = api.observation_universe("2018-01-02", source="twse", limit=1)
    second = api.observation_universe("2018-01-02", source="twse", limit=1, offset=1)
    assert first["total_count"] == 2
    assert first["snapshot_fingerprint"] == second["snapshot_fingerprint"]
    assert first["members"][0]["classification"] == "unknown"
    assert (
        second["members"][0]["classification_basis"] == "no_dated_official_security_type_evidence"
    )
    assert first["truncated"] is True
    with pytest.raises(ValueError):
        api.observation_universe("2018-01-02", limit=True)
