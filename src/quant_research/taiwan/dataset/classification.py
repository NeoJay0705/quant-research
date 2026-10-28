"""Offline, auditable classification from explicitly supplied official ISIN snapshots."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

from .models import DatasetError

PARSER_VERSION = "twse-isin-html-v1"
RULE_VERSION = "taiwan-equity-classification-v1"
_URLS = {
    "https://isin.twse.com.tw/isin/C_public.jsp?strMode=2": ("listed", "上市"),
    "https://isin.twse.com.tw/isin/C_public.jsp?strMode=4": ("otc", "上櫃"),
}
_CODE = re.compile(r"[0-9]{4}\Z", re.ASCII)
_CFI_ORDINARY = re.compile(r"ES[A-Z]{4}\Z", re.ASCII)
_HEADER_TOKENS = ("有價證券代號及名稱", "CFICode")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _canonical_hash(value: object) -> str:
    data = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return _sha256(data)


@dataclass(frozen=True, slots=True)
class ClassificationResult:
    symbol: str
    classification: str
    reason: str
    evidence_refs: tuple[str, ...] = ()

    @property
    def result(self) -> str:
        return self.classification


@dataclass(frozen=True, slots=True)
class _TableRow:
    cells: tuple[str, ...]
    locator: str


class _OfficialTableParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title_parts: list[str] = []
        self._heading = False
        self._plain_title = False
        self._table_depth = 0
        self._row: list[str] | None = None
        self._cell: list[str] | None = None
        self._rows: list[_TableRow] = []
        self._row_number = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if tag in {"h1", "h2", "h3", "title"} and self._table_depth == 0:
            self._heading = True
        # TWSE's legacy page wraps its official title in <font class="h1"> inside a layout table.
        if (
            tag == "font"
            and (self._table_depth == 0 or "h1" in (attributes.get("class") or "").split())
            and not self.title_parts
        ):
            self._plain_title = True
        if tag == "table":
            self._table_depth += 1
        elif tag == "tr" and self._table_depth:
            self._row = []
            self._row_number += 1
        elif tag in {"td", "th"} and self._row is not None:
            self._cell = []

    def handle_endtag(self, tag: str) -> None:
        if tag in {"h1", "h2", "h3", "title"}:
            self._heading = False
        elif tag == "font":
            self._plain_title = False
        elif tag in {"td", "th"} and self._cell is not None and self._row is not None:
            self._row.append("".join(self._cell).strip())
            self._cell = None
        elif tag == "tr" and self._row is not None:
            self._rows.append(_TableRow(tuple(self._row), f"table-row:{self._row_number}"))
            self._row = None
        elif tag == "table" and self._table_depth:
            self._table_depth -= 1

    def handle_data(self, data: str) -> None:
        if self._heading or self._plain_title:
            self.title_parts.append(data.strip())
        if self._cell is not None:
            self._cell.append(data)


def _normalize(value: str) -> str:
    return "".join(value.split())


def _duplicate_free_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate manifest key: {key}")
        result[key] = value
    return result


class EvidenceSet:
    """Validated local manifest and all parsed row-level evidence; performs no import-time I/O."""

    def __init__(
        self,
        evidence_rows: list[dict[str, Any]],
        protected_input_directories: frozenset[Path],
        stable_input_hashes: dict[str, str],
        *,
        missing: bool = False,
    ) -> None:
        self.evidence_rows = tuple(evidence_rows)
        self._rows_by_symbol: dict[str, list[dict[str, Any]]] = {}
        for row in self.evidence_rows:
            self._rows_by_symbol.setdefault(str(row["symbol"]), []).append(row)
        self.protected_input_directories = protected_input_directories
        self.stable_input_hashes = dict(sorted(stable_input_hashes.items()))
        self.missing = missing

    @classmethod
    def from_manifest(cls, path: Path | None) -> EvidenceSet:
        if path is None:
            return cls([], frozenset(), {}, missing=True)
        try:
            manifest = Path(path).resolve(strict=True)
        except OSError as exc:
            raise DatasetError(f"classification manifest is missing: {exc}") from exc
        if not manifest.is_file() or not manifest.stat().st_mode & 0o444:
            raise DatasetError("classification manifest is not a readable regular file")
        try:
            raw_manifest = manifest.read_bytes()
            doc = json.loads(raw_manifest, object_pairs_hook=_duplicate_free_object)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            raise DatasetError(f"invalid classification manifest: {exc}") from exc
        if not isinstance(doc, dict) or set(doc) != {"schema_version", "entries"}:
            raise DatasetError("manifest must contain exactly schema_version and entries")
        entries = doc["entries"]
        if doc["schema_version"] != 1 or not isinstance(entries, list):
            raise DatasetError("manifest schema_version must be 1 and entries must be an array")
        hashes = {"manifest": _sha256(raw_manifest)}
        protected = {manifest.parent}
        if not entries:
            return cls([], frozenset(protected), hashes, missing=True)
        rows: list[dict[str, Any]] = []
        for index, entry in enumerate(entries):
            if not isinstance(entry, dict) or set(entry) != {
                "source_url",
                "snapshot_asof",
                "encoding",
                "sha256",
                "path",
            }:
                raise DatasetError(f"entry {index} has invalid fields")
            url = entry["source_url"]
            if not isinstance(url, str) or url not in _URLS:
                raise DatasetError(f"entry {index} source_url is not allowlisted")
            mode, expected_title = _URLS[url]
            asof = entry["snapshot_asof"]
            if not isinstance(asof, str):
                raise DatasetError(f"entry {index} snapshot_asof must be ISO date")
            try:
                if (
                    len(asof) != 10
                    or __import__("datetime").date.fromisoformat(asof).isoformat() != asof
                ):
                    raise ValueError
            except ValueError as exc:
                raise DatasetError(f"entry {index} snapshot_asof must be ISO date") from exc
            if entry["encoding"] != "cp950":
                raise DatasetError(f"entry {index} encoding must be cp950")
            expected_hash = entry["sha256"]
            if not isinstance(expected_hash, str) or not re.fullmatch(
                r"[0-9a-f]{64}", expected_hash
            ):
                raise DatasetError(f"entry {index} sha256 must be lowercase hex")
            relpath = entry["path"]
            if not isinstance(relpath, str) or not relpath or Path(relpath).is_absolute():
                raise DatasetError(f"entry {index} path must be relative")
            try:
                file_path = (manifest.parent / relpath).resolve(strict=True)
            except OSError as exc:
                raise DatasetError(f"entry {index} evidence file is missing") from exc
            if not file_path.is_file() or not file_path.stat().st_mode & 0o444:
                raise DatasetError(f"entry {index} path is not a readable regular file")
            try:
                raw_html = file_path.read_bytes()
                actual_hash = _sha256(raw_html)
                if actual_hash != expected_hash:
                    raise DatasetError(f"entry {index} sha256 mismatch")
                html = raw_html.decode("cp950", errors="strict")
            except (OSError, UnicodeDecodeError) as exc:
                raise DatasetError(
                    f"entry {index} evidence file is unreadable or invalid cp950"
                ) from exc
            parser = _OfficialTableParser()
            try:
                parser.feed(html)
                parser.close()
            except (ValueError, AssertionError) as exc:
                raise DatasetError(f"entry {index} HTML parse failure") from exc
            title = _normalize("".join(parser.title_parts))
            if expected_title not in title:
                raise DatasetError(f"entry {index} expected official {expected_title} title")
            header_index = next(
                (
                    i
                    for i, r in enumerate(parser._rows)
                    if r.cells
                    and all(token in _normalize("".join(r.cells)) for token in _HEADER_TOKENS)
                ),
                None,
            )
            if header_index is None:
                raise DatasetError(f"entry {index} ISIN table header/schema missing")
            headers = parser._rows[header_index].cells
            cfi_index = next(
                (i for i, cell in enumerate(headers) if "CFICode" in _normalize(cell)), None
            )
            if cfi_index is None or cfi_index >= len(headers):
                raise DatasetError(f"entry {index} CFI header missing")
            symbol_index = next(
                (i for i, cell in enumerate(headers) if "有價證券代號及名稱" in _normalize(cell)),
                None,
            )
            market_index = next(
                (i for i, cell in enumerate(headers) if "市場別" in _normalize(cell)), None
            )
            if symbol_index is None or market_index is None:
                raise DatasetError(f"entry {index} security header missing")
            protected.add(file_path.parent)
            identity = f"evidence:{url}:{asof}:{actual_hash}"
            hashes[identity] = actual_hash
            section = ""
            for table_row in parser._rows[header_index + 1 :]:
                cells = table_row.cells
                raw = "\t".join(cells)
                item = cells[symbol_index].strip() if len(cells) > symbol_index else ""
                # Official section markers have a category but no code/name separator.
                if item and "　" not in item and " " not in item and not _CODE.fullmatch(item):
                    section = item
                    continue
                if len(cells) <= max(symbol_index, cfi_index) or not item:
                    continue
                parts = re.split(r"[\s　]+", item.strip(), maxsplit=1)
                symbol = parts[0]
                name = parts[1] if len(parts) > 1 else ""
                cfi = cells[cfi_index].strip()
                if len(cells) <= market_index or expected_title not in cells[market_index].strip():
                    raise DatasetError(f"entry {index} row market does not match official URL")
                normalized_section = _normalize(section)
                is_tdr = "存託憑證" in normalized_section
                is_etf = (
                    "ETF" in normalized_section.upper() or "指數股票型基金" in normalized_section
                )
                is_preferred = (
                    "特別股" in normalized_section
                    or bool(re.match(r"[0-9]{4}[A-Wa-w](?:\s|　)", item))
                    or cfi.startswith("EP")
                )
                is_fund_cfi = cfi.startswith("C")
                ordinary_section = section.strip() in {"股票", "創新板"}
                cfi_valid = bool(_CFI_ORDINARY.fullmatch(cfi))
                if is_tdr or is_etf or is_preferred or is_fund_cfi:
                    signal = "nonordinary"
                    signal_reason = (
                        "explicit_non_ordinary_tdr"
                        if is_tdr
                        else "explicit_non_ordinary_etf"
                        if is_etf or is_fund_cfi
                        else "explicit_non_ordinary_preferred"
                    )
                elif ordinary_section and cfi_valid:
                    signal, signal_reason = "ordinary", "official_stock_section_and_cfi"
                elif ordinary_section:
                    signal, signal_reason = "ambiguous", "ordinary_section_invalid_cfi"
                else:
                    signal, signal_reason = "ambiguous", "unrecognized_section_or_schema"
                identity_data = {
                    "url": url,
                    "asof": asof,
                    "file_sha256": actual_hash,
                    "raw_row": raw,
                    "table_title": title,
                    "section": section,
                    "symbol": symbol,
                    "cfi": cfi,
                    "locator": table_row.locator,
                    "parser_version": PARSER_VERSION,
                }
                evidence_id = "twisin:v1:" + _canonical_hash(identity_data)
                rows.append(
                    {
                        "evidence_id": evidence_id,
                        "symbol": symbol,
                        "name": name,
                        "source_url": url,
                        "file_sha256": actual_hash,
                        "snapshot_asof": asof,
                        "raw_row": raw,
                        "table_title": title,
                        "section": section,
                        "cfi": cfi,
                        "locator": table_row.locator,
                        "parser_version": PARSER_VERSION,
                        "signal": signal,
                        "reason": signal_reason,
                    }
                )
        return cls(rows, frozenset(protected), hashes)

    def classify(
        self,
        symbol: str,
        name: str | None = None,
        original_hint: str | None = None,
        source_hint: str | None = None,
        venue_hint: str | None = None,
        **hints: Any,
    ) -> ClassificationResult:
        del source_hint, venue_hint
        evidence = self._rows_by_symbol.get(symbol, [])
        refs = tuple(sorted(row["evidence_id"] for row in evidence))
        if not isinstance(symbol, str) or not _CODE.fullmatch(symbol):
            return ClassificationResult(str(symbol), "unknown", "invalid_symbol_shape", refs)
        signals = {row["signal"] for row in evidence}
        if "ordinary" in signals and ("nonordinary" in signals or "ambiguous" in signals):
            return ClassificationResult(symbol, "unknown", "classification_conflict", refs)
        if "nonordinary" in signals and "ambiguous" in signals:
            return ClassificationResult(symbol, "unknown", "classification_conflict", refs)
        if "nonordinary" in signals:
            return ClassificationResult(
                symbol, "excluded_non_ordinary", "official_non_ordinary_evidence", refs
            )
        ordinary_hint = original_hint
        if ordinary_hint is not None and ordinary_hint not in {
            "ordinary",
            "ordinary_stock",
            "stock",
        }:
            return ClassificationResult(symbol, "unknown", "conflicting_or_unrecognized_hint", refs)
        if symbol.startswith("00"):
            if "ordinary" in signals or ordinary_hint is not None:
                return ClassificationResult(symbol, "unknown", "classification_conflict", refs)
            return ClassificationResult(symbol, "excluded_non_ordinary", "00_code_rule_v1", refs)
        names = [name] if name else []
        names.extend(str(row.get("name", "")) for row in evidence)
        suspicious = any(
            any(token in value.upper() for token in ("-DR", "TDR", "ETF"))
            or "管理" in value
            or "存託" in value
            for value in names
        )
        suspicious |= bool(hints.get("ambiguous") or hints.get("suspicious"))
        if suspicious:
            return ClassificationResult(symbol, "unknown", "suspicious_or_ambiguous_signal", refs)
        if "ambiguous" in signals:
            return ClassificationResult(symbol, "unknown", "ambiguous_official_evidence", refs)
        if "ordinary" in signals:
            return ClassificationResult(
                symbol, "ordinary_evidence_supported", "official_stock_section_and_cfi", refs
            )
        if ordinary_hint is not None:
            return ClassificationResult(symbol, "unknown", "unsupported_ordinary_hint", refs)
        return ClassificationResult(
            symbol, "ordinary_rule_limited", "four_digit_code_rule_v1", refs
        )
