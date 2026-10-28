from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from quant_research.taiwan.dataset.classification import EvidenceSet
from quant_research.taiwan.dataset.models import DatasetError

LISTED = "https://isin.twse.com.tw/isin/C_public.jsp?strMode=2"
OTC = "https://isin.twse.com.tw/isin/C_public.jsp?strMode=4"


def _html(title: str, securities: list[tuple[str, str, str]]) -> bytes:
    rows = [
        "<tr><th>有價證券代號及名稱</th><th>國際證券辨識號碼(ISIN Code)</th>"
        "<th>上市日</th><th>市場別</th><th>產業別</th><th>CFICode</th></tr>",
        "<tr><td>股票</td></tr>",
    ]
    for symbol, name, cfi in securities:
        section = {"7788": "創新板", "9103": "臺灣存託憑證", "0050": "指數股票型基金"}.get(
            symbol, "股票"
        )
        rows.append(f"<tr><td>{section}</td></tr>")
        isin = "US0000000000" if symbol == "7777" else "TW0000000000"
        rows.append(
            f"<tr><td>{symbol}　{name}</td><td>{isin}</td>"
            f"<td>2020/01/01</td><td>{title}</td><td>半導體</td><td>{cfi}</td></tr>"
        )
    return (
        f"<html><body><table><h2><strong><font class='h1'>"
        f"本國{title}證券國際證券辨識號碼一覽表</font></strong></h2></table><table>"
        + "".join(rows)
        + "</table></body></html>"
    ).encode("cp950")


def _evidence(
    tmp_path: Path,
    *,
    listed: list[tuple[str, str, str]] | None = None,
    otc: list[tuple[str, str, str]] | None = None,
) -> EvidenceSet:
    listed = listed or []
    otc = otc or []
    entries = []
    for name, url, securities in (("listed.html", LISTED, listed), ("otc.html", OTC, otc)):
        if not securities:
            continue
        data = _html("上市" if url == LISTED else "上櫃", securities)
        (tmp_path / name).write_bytes(data)
        entries.append(
            {
                "source_url": url,
                "snapshot_asof": "2026-10-22",
                "encoding": "cp950",
                "sha256": hashlib.sha256(data).hexdigest(),
                "path": name,
            }
        )
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps({"schema_version": 1, "entries": entries}, ensure_ascii=False), encoding="utf-8"
    )
    return EvidenceSet.from_manifest(manifest)


def test_classification_rules_and_evidence_preservation(tmp_path: Path) -> None:
    ev = _evidence(
        tmp_path,
        listed=[
            ("2330", "台積電", "ESVUFR"),
            ("0050", "元大台灣50", "CE..."),
            ("9103", "某公司", "EDXXXX"),
            ("9105", "另一TDR", "ESVUFR"),
            ("2311", "歷史公司", "ESVUFR"),
            ("1234", "未知分類", "ZZZZZZ"),
            ("9999", "疑似公司-DR", "ESVUFR"),
        ],
        otc=[
            ("6488", "環球晶", "ESVUFR"),
            ("7788", "創新公司", "ESVUFR"),
            ("7777", "外國發行人", "ESVUFR"),
        ],
    )
    expected = {
        "2330": "ordinary_evidence_supported",
        "6488": "ordinary_evidence_supported",
        "7788": "ordinary_evidence_supported",
        "7777": "ordinary_evidence_supported",
        "0050": "excluded_non_ordinary",
        "9103": "excluded_non_ordinary",
        "9105": "unknown",
        "2311": "ordinary_evidence_supported",
        "1234": "unknown",
        "9999": "unknown",
        "2312": "ordinary_rule_limited",
        "0051": "excluded_non_ordinary",
    }
    for symbol, result in expected.items():
        assert ev.classify(symbol).classification == result
    assert ev.classify("2330").evidence_refs
    raw = ev.evidence_rows[0]
    assert {
        "raw_row",
        "section",
        "cfi",
        "table_title",
        "locator",
        "file_sha256",
        "snapshot_asof",
        "source_url",
        "parser_version",
    } <= raw.keys()
    assert ev.stable_input_hashes["manifest"]
    assert ev.protected_input_directories == frozenset({tmp_path.resolve()})


def test_no_evidence_and_shape_suspicion_conflicts(tmp_path: Path) -> None:
    empty = EvidenceSet.from_manifest(None)
    assert empty.missing
    assert empty.classify("2311").classification == "ordinary_rule_limited"
    assert empty.classify("9103").classification == "ordinary_rule_limited"
    assert empty.classify("１２３４").classification == "unknown"
    assert empty.classify("2311", "issuer-DR").classification == "unknown"
    conflict = _evidence(tmp_path, listed=[("0012", "ETF", "CE..."), ("0012", "普通股", "ESVUFR")])
    assert conflict.classify("0012").reason == "classification_conflict"
    assert conflict.classify("0012").classification == "unknown"


@pytest.mark.parametrize("alter", ["url", "encoding", "missing", "hash", "title", "schema"])
def test_manifest_contract_errors_are_operational(tmp_path: Path, alter: str) -> None:
    data = _html("上市", [("2330", "台積電", "ESVUFR")])
    path = tmp_path / "listed.html"
    path.write_bytes(data)
    entry = {
        "source_url": LISTED,
        "snapshot_asof": "2026-10-22",
        "encoding": "cp950",
        "sha256": hashlib.sha256(data).hexdigest(),
        "path": path.name,
    }
    if alter == "url":
        entry["source_url"] = "https://example.invalid"
    elif alter == "encoding":
        entry["encoding"] = "utf-8"
    elif alter == "missing":
        entry["path"] = "absent.html"
    elif alter == "hash":
        entry["sha256"] = "0" * 64
    elif alter == "title":
        path.write_bytes(_html("上櫃", [("2330", "台積電", "ESVUFR")]))
        entry["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    elif alter == "schema":
        path.write_bytes(
            "<html><h2>上市股票</h2><table><tr><td>broken</td></tr></table></html>".encode("cp950")
        )
        entry["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"schema_version": 1, "entries": [entry]}), encoding="utf-8")
    with pytest.raises((DatasetError, OSError)):
        EvidenceSet.from_manifest(manifest)


def test_multiple_snapshots_same_url_conflict_without_overwriting(tmp_path: Path) -> None:
    snapshots = [
        _html("上市", [("2330", "台積電", "ESVUFR")]),
        _html("上市", [("2330", "台積電", "ZZZZZZ")]),
    ]
    entries = []
    for index, data in enumerate(snapshots):
        filename = f"snapshot-{index}.html"
        (tmp_path / filename).write_bytes(data)
        entries.append(
            {
                "source_url": LISTED,
                "snapshot_asof": f"2026-10-{22 + index}",
                "encoding": "cp950",
                "sha256": hashlib.sha256(data).hexdigest(),
                "path": filename,
            }
        )
    (tmp_path / "manifest.json").write_text(
        json.dumps({"schema_version": 1, "entries": entries}, ensure_ascii=False), encoding="utf-8"
    )
    evidence = EvidenceSet.from_manifest(tmp_path / "manifest.json")
    assert len(evidence.evidence_rows) == 2
    assert len([key for key in evidence.stable_input_hashes if key.startswith("evidence:")]) == 2
    assert evidence.classify("2330").classification == "unknown"
    assert evidence.classify("2330").reason == "classification_conflict"


def test_stable_hash_identities_ignore_storage_directory(tmp_path: Path) -> None:
    first, second = tmp_path / "a", tmp_path / "b"
    first.mkdir()
    second.mkdir()
    left = _evidence(first, listed=[("2330", "台積電", "ESVUFR")])
    right = _evidence(second, listed=[("2330", "台積電", "ESVUFR")])
    assert left.stable_input_hashes == right.stable_input_hashes
    assert left.evidence_rows[0]["evidence_id"] == right.evidence_rows[0]["evidence_id"]
