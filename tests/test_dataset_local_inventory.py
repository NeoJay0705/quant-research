from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from quant_research.taiwan import official, sources
from quant_research.taiwan.storage import SCHEMA

SCRIPT = Path(__file__).parents[1] / "scripts" / "dataset_local_validation.py"


def _fixture(root: Path) -> None:
    root.mkdir()
    db = sqlite3.connect(root / "state.sqlite3")
    db.executescript(SCHEMA)

    def snapshot(
        task: str,
        logical: str,
        edition: int,
        source: str,
        dataset: str,
        lane: str,
        state: str,
        validation: str,
        params: dict,
        records: list[dict],
        fetched: str,
        kind: str | None = None,
        record_dates: list[str | None] | None = None,
    ):
        sid = f"s-{task}"
        encoded = json.dumps(params, sort_keys=True)
        db.execute(
            "INSERT INTO snapshots VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                sid,
                source,
                dataset,
                encoded,
                "url",
                logical,
                fetched,
                200,
                "hash",
                "raw",
                validation,
                None,
            ),
        )
        db.execute(
            "INSERT INTO tasks(id,logical_key,edition,source,dataset,params,kind,state,"
            "snapshot_id) VALUES(?,?,?,?,?,?,?,?,?)",
            (
                task,
                logical,
                edition,
                source,
                dataset,
                encoded,
                kind or ("official_quote" if lane == "quotes" else "official_event_summary"),
                state,
                sid,
            ),
        )
        for i, payload in enumerate(records):
            db.execute(
                "INSERT INTO records(snapshot_id,kind,symbol,record_date,record_key,payload,"
                "quality_flags) VALUES(?,?,?,?,?,?,?)",
                (
                    sid,
                    "quote" if lane == "quotes" else "event",
                    payload.get("symbol"),
                    (
                        record_dates
                        or [payload.get("trade_date", payload.get("event_date"))] * len(records)
                    )[i],
                    str(i),
                    json.dumps(payload),
                    "[]",
                ),
            )
        return sid

    qparams = {"pipeline": "official-v1", "lane": "quotes", "request": {"date": "2024-01-02"}}
    raw = json.dumps(
        {
            "stat": "OK",
            "date": "20240102",
            "tables": [
                {
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
                    "data": [["2330", "台積電", "100", "101", "99", "100", "10", "1000"]],
                }
            ],
        },
        ensure_ascii=False,
    ).encode()
    parsed = official.parse(
        sources.RawBatch("twse", "MI_INDEX", "offline", "https://example.invalid", 200, raw, "", 0),
        {"date": "2024-01-02"},
    )
    qrow = parsed.records[0]
    snapshot(
        "q-old",
        "q",
        1,
        "twse",
        "MI_INDEX",
        "quotes",
        "completed",
        "valid",
        qparams,
        [qrow],
        "2024-01-03",
    )
    snapshot(
        "q-new",
        "q",
        2,
        "twse",
        "MI_INDEX",
        "quotes",
        "failed",
        "invalid",
        qparams,
        [],
        "2024-01-04",
    )
    # Corrupt newest official params must remain selected, not fall back to q-old.
    db.execute(
        "UPDATE tasks SET params=? WHERE id='q-new'",
        ('{"pipeline":"official-v1","pipeline":"legacy","lane":"quotes"}',),
    )
    # Successful empty scope remains usable under SourceReader's explicit empty_unverified rule.
    eparams = {
        "pipeline": "official-v1",
        "lane": "events",
        "request": {"start_date": "2024-01-01", "end_date": "2024-12-31"},
    }
    snapshot(
        "empty",
        "empty",
        1,
        "tpex",
        "exDailyQ",
        "events",
        "completed",
        "empty_unverified",
        eparams,
        [],
        "2024-01-05",
    )
    summary = {
        "kind": "event",
        "source": "twse",
        "dataset": "TWT49U",
        "symbol": "2330",
        "event_date": "2024-01-02",
        "event_type": "dividend",
        "observation_type": "summary",
        "quality_flags": [],
        "summary_token": "d1",
    }
    sparams = {
        "pipeline": "official-v1",
        "lane": "events",
        "request": {"start_date": "2024-01-01", "end_date": "2024-12-31"},
    }
    snapshot(
        "sum1",
        "sum",
        1,
        "twse",
        "TWT49U",
        "events",
        "completed",
        "valid",
        sparams,
        [summary],
        "2024-02-01",
    )
    snapshot(
        "sum2",
        "sum2",
        1,
        "twse",
        "TWT49U",
        "events",
        "completed",
        "valid",
        sparams,
        [summary],
        "2024-02-02",
    )
    malformed = dict(summary, event_date=None)
    snapshot(
        "bad",
        "bad",
        1,
        "twse",
        "TWT49U",
        "events",
        "completed",
        "valid",
        sparams,
        [malformed],
        "2024-02-03",
        record_dates=[None],
    )
    # Official detail record with a null record_date; legacy / FinMind intentionally excluded.
    dparams = {
        "pipeline": "official-v1",
        "lane": "events",
        "request": {"symbol": "2330", "date": "2024-01-02"},
    }
    snapshot(
        "detail",
        "detail",
        1,
        "twse",
        "TWT49UDetail",
        "events",
        "completed",
        "valid",
        dparams,
        [
            {
                "kind": "event",
                "source": "twse",
                "dataset": "TWT49UDetail",
                "symbol": "2330",
                "event_date": "2024-01-02",
                "event_type": "dividend",
                "observation_type": "conditions",
                "quality_flags": [],
            }
        ],
        "2024-02-04",
        kind="official_event_detail",
        record_dates=[None],
    )
    legacy = dict(qparams, pipeline="legacy")
    snapshot(
        "legacy",
        "legacy",
        1,
        "twse",
        "MI_INDEX",
        "quotes",
        "completed",
        "valid",
        legacy,
        [qrow],
        "2024-02-05",
    )
    fin = dict(qparams, pipeline="official-v1")
    snapshot(
        "fin",
        "fin",
        1,
        "finmind",
        "MI_INDEX",
        "quotes",
        "completed",
        "valid",
        fin,
        [qrow],
        "2024-02-06",
    )
    db.commit()
    db.close()


def test_inventory_fixed_counts_and_latest_selection(tmp_path: Path) -> None:
    root = tmp_path / "root"
    _fixture(root)
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--root", str(root), "inventory"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    by_key = {(x["source"], x["dataset"], x["lane"]): x for x in data["historical_saved"]}
    q = by_key[("twse", "MI_INDEX", "quotes")]
    assert (q["snapshot_count"], q["snapshot_row_count"]) == (2, 1)
    latest = {(x["source"], x["dataset"], x["lane"]): x for x in data["latest"]}
    failed = latest[("twse", "MI_INDEX", "quotes")]
    assert (
        failed["usable_scope_count"],
        failed["unavailable_scope_count"],
        failed["source_record_rows"],
    ) == (0, 1, 0)
    assert failed["unavailable_scopes"][0]["task_id"] == "q-new"
    assert "invalid_task_params" in failed["unavailable_scopes"][0]["diagnostics"]
    empty = latest[("tpex", "exDailyQ", "events")]
    assert (empty["usable_scope_count"], empty["source_record_rows"]) == (1, 0)
    summary = latest[("twse", "TWT49U", "events")]
    assert (summary["raw_usable_summary_rows"], summary["unique_latest_event_identities"]) == (3, 1)
    historical_summary = by_key[("twse", "TWT49U", "events")]
    assert (
        historical_summary["snapshot_count"],
        historical_summary["snapshot_row_count"],
    ) == (3, 3)
    assert len(summary["malformed_event_rows"]) == 1
    links = data["required_twse_summary_detail_links"]
    assert (links["required"], links["available"], links["unavailable"], links["missing"]) == (
        1,
        0,
        1,
        1,
    )
    details = next(x for x in data["historical_saved"] if x["dataset"] == "TWT49UDetail")
    assert (details["snapshot_row_count"], details["missing_record_date_snapshot_rows"]) == (1, 1)
    assert all(
        not (x["dataset"] == "MI_INDEX" and x["source"] == "finmind")
        for x in data["historical_saved"]
    )


@pytest.mark.parametrize(
    ("link_mode", "expected"),
    [("available", (1, 0, 0)), ("missing", (0, 1, 1)), ("failed", (0, 1, 0))],
)
def test_required_detail_link_availability_fixed_counts(
    tmp_path: Path, link_mode: str, expected: tuple[int, int, int]
) -> None:
    from tests.test_taiwan_dataset_events import install, twse_summary

    root = tmp_path / link_mode
    child = link_mode != "missing"
    install(
        root,
        parent_dataset="TWT49U",
        summary=twse_summary(
            "TWT49U",
            ["資料日期", "股票代號", "除權息前收盤價", "除權息參考價", "詳細資料"],
            ["2024-03-01", "2330", "100", "90", "2330,20240301"],
        ),
        child_dataset="TWT49UDetail" if child else None,
        detail={
            "stat": "OK",
            "fields": ["股票代號", "(每股配發現金股利)除息"],
            "data": [["2330", "2"]],
        }
        if child
        else None,
        child_available=link_mode != "failed",
    )
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--root", str(root), "inventory"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    counts = json.loads(result.stdout)["required_twse_summary_detail_links"]
    assert (counts["available"], counts["unavailable"], counts["missing"]) == expected
    reason = counts["reasons"][0]["reason"] if counts["reasons"] else None
    assert (
        reason
        == {
            "available": None,
            "missing": "required_link_missing",
            "failed": "linked_child_provenance_or_state_invalid",
        }[link_mode]
    )


def test_latest_detail_identity_change_and_legacy_exclusion(tmp_path: Path) -> None:
    root = tmp_path / "root"
    _fixture(root)
    db = sqlite3.connect(root / "state.sqlite3")
    db.execute(
        "INSERT INTO tasks(id,logical_key,edition,source,dataset,params,kind,state,snapshot_id) "
        "VALUES(?,?,?,?,?,?,?,?,?)",
        (
            "detail-new",
            "detail",
            2,
            "twse",
            "TWTAVUDetail",
            "{}",
            "official_event_detail",
            "failed",
            None,
        ),
    )
    db.execute(
        "INSERT INTO tasks(id,logical_key,edition,source,dataset,params,kind,state,snapshot_id) "
        "VALUES(?,?,?,?,?,?,?,?,?)",
        (
            "legacy-detail",
            "legacy-detail",
            1,
            "twse",
            "TWT49UDetail",
            '{"pipeline":"legacy"}',
            "official_event_detail",
            "completed",
            None,
        ),
    )
    db.commit()
    db.close()
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--root", str(root), "inventory"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    detail = next(x for x in data["latest_detail_tasks"] if x["dataset"] == "TWT49UDetail")
    assert detail["tasks"][0]["task_id"] == "detail-new"
    assert detail["tasks"][0]["identity_changed"] is True
    assert detail["tasks"][0]["actual_dataset"] == "TWTAVUDetail"
    assert detail["latest_snapshot_row_count"] == 0
    assert all(x["dataset"] != "legacy-detail" for x in data["latest_detail_tasks"])


def test_inventory_missing_database_does_not_create_it(tmp_path: Path) -> None:
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--root", str(tmp_path), "inventory"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert not (tmp_path / "state.sqlite3").exists()
