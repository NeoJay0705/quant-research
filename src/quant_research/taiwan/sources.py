"""Allowlisted Taiwan market data sources and fail-closed parsers."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from typing import Protocol
from urllib.parse import urlencode

TWSE_URL = "https://www.twse.com.tw/exchangeReport/STOCK_DAY_ALL?response=open_data"
TPEX_URL = "https://www.tpex.org.tw/web/stock/aftertrading/DAILY_CLOSE_quotes/stk_quote_result.php?l=zh-tw&o=data"
FINMIND_URL = "https://api.finmindtrade.com/api/v4/data"
DATASETS: dict[str, frozenset[str]] = {
    name: frozenset()
    for name in (
        "TaiwanStockInfo",
        "TaiwanStockDelisting",
        "TaiwanStockSplitPrice",
        "TaiwanStockParValueChange",
        "TaiwanStockPrice",
        "TaiwanStockDividend",
        "TaiwanStockDividendResult",
        "TaiwanStockCapitalReductionReferencePrice",
    )
}
EVENTS = {
    "TaiwanStockDividend",
    "TaiwanStockDividendResult",
    "TaiwanStockCapitalReductionReferencePrice",
    "TaiwanStockSplitPrice",
    "TaiwanStockParValueChange",
}


class SourceError(Exception):
    def __init__(
        self, message: str, raw: RawBatch | None = None, *, raw_saved: bool = False
    ) -> None:
        super().__init__(message)
        self.raw = raw
        self.raw_saved = raw_saved


class ContractError(SourceError):
    pass


class CaptchaError(SourceError):
    pass


class AccessBlockedError(SourceError):
    pass


class QuotaError(SourceError):
    pass


class HttpError(SourceError):
    pass


class Transport(Protocol):
    def __call__(
        self, url: str, headers: dict[str, str], timeout: float, max_bytes: int
    ) -> tuple[int, bytes] | tuple[int, bytes, dict[str, str]]: ...


class _PartialTransportError(Exception):
    def __init__(
        self, status: int, body: bytes, reason: str, retry_after: str | None = None
    ) -> None:
        super().__init__(reason)
        self.status = status
        self.body = body
        self.retry_after = retry_after


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self, req: object, fp: object, code: int, msg: str, headers: object, newurl: str
    ) -> None:
        return None


def _retry_after(response: object) -> str | None:
    value = getattr(response, "headers", {}).get("Retry-After", "")
    return value if value and len(value) <= 128 else None


def urllib_transport(
    url: str, headers: dict[str, str], timeout: float, max_bytes: int
) -> tuple[int, bytes] | tuple[int, bytes, dict[str, str]]:
    opener = urllib.request.build_opener(_NoRedirect)
    request = urllib.request.Request(url, headers=headers)
    started = time.monotonic()
    try:
        response = opener.open(request, timeout=timeout)
    except urllib.error.HTTPError as exc:
        response = exc
    except Exception as exc:
        # Do not expose arbitrary transport exception text (it can contain Authorization).
        raise HttpError(f"transport failure ({type(exc).__name__})") from None
    chunks: list[bytes] = []
    size = 0
    try:
        while True:
            if time.monotonic() - started > timeout:
                raise _PartialTransportError(
                    response.status,
                    b"".join(chunks),
                    "response deadline exceeded",
                    _retry_after(response),
                )
            try:
                chunk = response.read(min(65536, max_bytes + 1 - size))
            except Exception as exc:
                raise _PartialTransportError(
                    response.status, b"".join(chunks), type(exc).__name__, _retry_after(response)
                ) from None
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
            if size > max_bytes:
                break
        retry_after = _retry_after(response)
        safe_headers = {"Retry-After": retry_after} if retry_after else {}
        return response.status, b"".join(chunks), safe_headers
    finally:
        response.close()


@dataclass(frozen=True)
class RawBatch:
    source: str
    dataset: str | None
    url: str
    fetched_at: str
    status: int
    body: bytes = field(repr=False)
    sha256: str
    duration_seconds: float
    complete: bool = True
    retry_after: str | None = None


@dataclass(frozen=True)
class ParsedBatch:
    raw: RawBatch
    records: tuple[dict[str, object], ...]
    quality_flags: tuple[str, ...] = ("history_coverage_unverified",)
    valid: bool = True


def _day(value: str) -> str:
    try:
        return date.fromisoformat(value).isoformat()
    except (ValueError, TypeError) as exc:
        raise ValueError("dates must use valid YYYY-MM-DD") from exc


def request(
    source: str,
    *,
    dataset: str | None = None,
    params: dict[str, str] | None = None,
    token: str | None = None,
    transport: Transport = urllib_transport,
    timeout: float = 20.0,
    max_bytes: int = 64 * 1024 * 1024,
) -> RawBatch:
    params = dict(params or {})
    if source == "finmind" and transport is urllib_transport:
        raise AccessBlockedError("live FinMind requests are disabled")
    if source in {"twse", "tpex"}:
        if dataset is not None or params:
            raise ValueError(f"{source} accepts no query parameters")
        url = TWSE_URL if source == "twse" else TPEX_URL
    elif source == "finmind":
        if dataset not in DATASETS:
            raise ValueError("FinMind dataset is not allowlisted")
        allowed = {
            "TaiwanStockInfo": set(),
            "TaiwanStockDelisting": {"start_date", "end_date"},
            "TaiwanStockSplitPrice": {"start_date", "end_date"},
            "TaiwanStockParValueChange": {"start_date", "end_date"},
            "TaiwanStockPrice": {"data_id", "start_date", "end_date"},
            "TaiwanStockDividend": {"data_id", "start_date", "end_date"},
            "TaiwanStockDividendResult": {"data_id", "start_date", "end_date"},
            "TaiwanStockCapitalReductionReferencePrice": {"data_id", "start_date", "end_date"},
        }[dataset]
        if set(params) - allowed:
            raise ValueError("unsupported FinMind query parameters")
        per_symbol = dataset in {
            "TaiwanStockPrice",
            "TaiwanStockDividend",
            "TaiwanStockDividendResult",
            "TaiwanStockCapitalReductionReferencePrice",
        }
        if per_symbol and not {"data_id", "start_date", "end_date"} <= params.keys():
            raise ValueError("single-symbol queries require data_id and date range")
        if per_symbol and not params.get("data_id", "").strip():
            raise ValueError("data_id must be nonempty")
        if "start_date" in params and "end_date" in params:
            start, end = _day(params["start_date"]), _day(params["end_date"])
            if start > end:
                raise ValueError("start_date must not exceed end_date")
        for key in ("start_date", "end_date"):
            if key in params:
                params[key] = _day(params[key])
        url = FINMIND_URL + "?" + urlencode({"dataset": dataset, **params})
    else:
        raise ValueError("source is not allowlisted")
    headers = {"User-Agent": "quant-research/0.1"}
    if source == "finmind" and token:
        headers["Authorization"] = f"Bearer {token}"
    started = time.monotonic()
    complete = True
    try:
        response = transport(url, headers, timeout, max_bytes)
        status, body = response[:2]
        response_headers = response[2] if len(response) == 3 else {}
        retry_after = (
            response_headers.get("Retry-After") if isinstance(response_headers, dict) else None
        )
    except _PartialTransportError as exc:
        status, body, complete, retry_after = exc.status, exc.body, False, exc.retry_after
    except Exception as exc:
        raise HttpError(f"transport failure ({type(exc).__name__})") from None
    duration = time.monotonic() - started
    raw = RawBatch(
        source,
        dataset,
        url,
        datetime.now(UTC).isoformat(),
        status,
        body,
        hashlib.sha256(body).hexdigest(),
        duration,
        complete,
        retry_after,
    )
    lower = body[:4096].lower()
    if status == 403 and (b"captcha" in lower or "驗證碼".encode() in lower):
        raise CaptchaError(f"{source} captcha response; response sha256={raw.sha256}", raw)
    if status == 403:
        raise AccessBlockedError(f"{source} returned HTTP 403; response sha256={raw.sha256}", raw)
    if status in (402, 429):
        raise QuotaError(f"{source} returned HTTP {status}; response sha256={raw.sha256}", raw)
    if not 200 <= status < 300:
        raise HttpError(f"{source} returned HTTP {status}; response sha256={raw.sha256}", raw)
    if not complete:
        raise HttpError("incomplete response due to transport failure", raw)
    if len(body) > max_bytes:
        raise ContractError("response exceeds size limit", raw)
    if b"captcha" in lower or "驗證碼".encode() in lower:
        raise CaptchaError(f"{source} captcha response; response sha256={raw.sha256}", raw)
    return raw


def fetch_allowed(
    url: str,
    *,
    source: str,
    dataset: str,
    allowed_urls: dict[tuple[str, str], str],
    transport: Transport = urllib_transport,
    timeout: float = 20.0,
    max_bytes: int = 64 * 1024 * 1024,
) -> RawBatch:
    """Fetch a URL whose origin/path is fixed by a caller-owned allowlist."""
    base = allowed_urls.get((source, dataset))
    if base is None or not url.startswith(base + "?"):
        raise ValueError("URL is not in the source allowlist")
    started = time.monotonic()
    complete = True
    try:
        response = transport(url, {"User-Agent": "quant-research/0.1"}, timeout, max_bytes)
        status, body = response[:2]
        response_headers = response[2] if len(response) == 3 else {}
        retry_after = (
            response_headers.get("Retry-After") if isinstance(response_headers, dict) else None
        )
    except _PartialTransportError as exc:
        status, body, complete, retry_after = exc.status, exc.body, False, exc.retry_after
    except Exception as exc:
        raise HttpError(f"transport failure ({type(exc).__name__})") from None
    raw = RawBatch(
        source,
        dataset,
        url,
        datetime.now(UTC).isoformat(),
        status,
        body,
        hashlib.sha256(body).hexdigest(),
        time.monotonic() - started,
        complete,
        retry_after,
    )
    lower = body[:4096].lower()
    if status == 403 and (b"captcha" in lower or "驗證碼".encode() in lower):
        raise CaptchaError(f"{source} captcha response; response sha256={raw.sha256}", raw)
    if status == 403:
        raise AccessBlockedError(f"{source} returned HTTP 403; response sha256={raw.sha256}", raw)
    if status in (402, 429):
        raise QuotaError(f"{source} returned HTTP {status}; response sha256={raw.sha256}", raw)
    if not 200 <= status < 300:
        raise HttpError(f"{source} returned HTTP {status}; response sha256={raw.sha256}", raw)
    if not complete:
        raise HttpError("incomplete response due to transport failure", raw)
    if len(body) > max_bytes:
        raise ContractError("response exceeds size limit", raw)
    if b"captcha" in lower or "驗證碼".encode() in lower:
        raise CaptchaError(f"{source} captcha response; response sha256={raw.sha256}", raw)
    return raw


def _decimal(value: object, flags: list[str]) -> str | None:
    text = str(value).strip().replace(",", "")
    if text in {"", "X", "N/A", "null", "None"} or re.fullmatch(r"-{1,4}", text):
        flags.append("source_placeholder")
        return None
    try:
        number = Decimal(text)
    except (InvalidOperation, ValueError) as exc:
        raise ContractError("invalid numeric field") from exc
    if not number.is_finite():
        raise ContractError("non-finite numeric field")
    return str(number)


def _iso(value: object) -> str:
    try:
        return date.fromisoformat(str(value)).isoformat()
    except ValueError as exc:
        raise ContractError("invalid ISO provider date") from exc


def parse_finmind(raw: RawBatch, params: dict[str, str] | None = None) -> ParsedBatch:
    if raw.source != "finmind":
        raise ValueError("expected FinMind response")
    if not raw.dataset:
        try:
            env = json.loads(raw.body)
            if (
                isinstance(env, dict)
                and env.get("status") in ("成功", "success", 200, "200")
                and isinstance(env.get("data"), list)
                and not env["data"]
            ):
                return ParsedBatch(raw, ())
        except (UnicodeDecodeError, json.JSONDecodeError):
            pass
        raise ContractError("dataset is required to normalize FinMind rows", raw)
    try:
        envelope = json.loads(raw.body, parse_float=str)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ContractError("invalid FinMind JSON", raw) from exc
    if not isinstance(envelope, dict):
        raise ContractError("invalid FinMind response envelope", raw)
    status = envelope.get("status")
    if status in (402, "402", "429", 429):
        raise QuotaError(f"FinMind envelope quota status {status}", raw)
    if status in (403, "403"):
        raise AccessBlockedError("FinMind envelope access blocked", raw)
    if status not in ("成功", "success", 200, "200"):
        raise ContractError("invalid FinMind response envelope", raw)
    if not isinstance(envelope.get("data"), list):
        raise ContractError("invalid FinMind response envelope", raw)
    rows = envelope["data"]
    if not all(isinstance(row, dict) for row in rows):
        raise ContractError("FinMind data rows must be objects", raw)
    ds = raw.dataset
    query = params or {}
    records = []
    keys = set()
    for row in rows:
        flags = ["history_coverage_unverified"]
        dateval = row.get("date")
        record_date: str | None
        if ds in {
            "TaiwanStockPrice",
            "TaiwanStockDividend",
            "TaiwanStockDividendResult",
            "TaiwanStockCapitalReductionReferencePrice",
            "TaiwanStockSplitPrice",
            "TaiwanStockParValueChange",
            "TaiwanStockDelisting",
        }:
            if dateval is None:
                raise ContractError(f"{ds} row missing date", raw)
            record_date = _iso(dateval)
            if (
                query.get("start_date")
                and record_date < query["start_date"]
                or query.get("end_date")
                and record_date > query["end_date"]
            ):
                raise ContractError("record date outside query interval", raw)
        else:
            try:
                record_date = _iso(dateval) if dateval else None
            except ContractError:
                record_date = None
        symbol = row.get("stock_id") or row.get("data_id") or row.get("symbol")
        if ds in {"TaiwanStockInfo", "TaiwanStockDelisting", *EVENTS}:
            symbol = row.get("stock_id")
            if not isinstance(symbol, str) or not symbol.strip():
                raise ContractError(f"{ds} row missing stock_id", raw)
        if ds in {
            "TaiwanStockPrice",
            "TaiwanStockDividend",
            "TaiwanStockDividendResult",
            "TaiwanStockCapitalReductionReferencePrice",
        }:
            symbol = row.get("stock_id") or row.get("data_id")
            if not symbol:
                raise ContractError(f"{ds} row missing symbol", raw)
            if query.get("data_id") and str(symbol) != query["data_id"]:
                raise ContractError("provider symbol does not match data_id", raw)
        kind = (
            "quote"
            if ds == "TaiwanStockPrice"
            else "symbol"
            if ds in {"TaiwanStockInfo", "TaiwanStockDelisting"}
            else "event"
        )
        rec: dict[str, object] = {
            "kind": kind,
            "symbol": str(symbol) if symbol is not None else None,
            "record_date": record_date,
            "date": record_date,
            "market": None,
            "original": dict(row),
            "research_ready": False,
            "quality_flags": flags + (["classification_unverified"] if kind == "symbol" else []),
        }
        if ds == "TaiwanStockPrice":
            aliases = {
                "open": ["open"],
                "high": ["max", "high"],
                "low": ["min", "low"],
                "close": ["close"],
                "volume": [" Trading_Volume", "Trading_Volume"],
                "amount": ["Trading_Money", "Trading_money"],
            }
            for key, names in aliases.items():
                val = next((row[n] for n in names if n in row), None)
                if val is None:
                    raise ContractError(f"TaiwanStockPrice missing {key}", raw)
                rec[key] = _decimal(val, flags)
            if any(
                rec[k] is not None and Decimal(str(rec[k])) <= 0
                for k in ("open", "high", "low", "close")
            ):
                flags.append("nonpositive_price")
            flags.append("classification_unverified")
            rec["trade_date"] = record_date
            rec["price_basis"] = "raw"
            if (symbol, record_date) in keys:
                raise ContractError("duplicate quote key", raw)
            keys.add((symbol, record_date))
            _validate_quote(rec, raw)
        elif kind == "event":
            flags.extend(["cross_dataset_duplicates_unresolved", "revision_history_unverified"])
            required = {
                "TaiwanStockDividend": {
                    "CashEarningsDistribution",
                    "CashStatutorySurplus",
                    "StockEarningsDistribution",
                    "StockStatutorySurplus",
                },
                "TaiwanStockDividendResult": {
                    "before_price",
                    "after_price",
                    "stock_and_cache_dividend",
                    "stock_or_cache_dividend",
                },
                "TaiwanStockCapitalReductionReferencePrice": {
                    "ClosingPriceonTheLastTradingDay",
                    "PostReductionReferencePrice",
                    "ReasonforCapitalReduction",
                },
                "TaiwanStockSplitPrice": {"before_price", "after_price", "type"},
                "TaiwanStockParValueChange": {"before_close", "after_ref_close"},
            }[ds]
            if not required <= row.keys():
                raise ContractError(f"{ds} row missing required event fields", raw)
            if ds == "TaiwanStockCapitalReductionReferencePrice":
                flags.extend(
                    ["capital_reduction_cash_ratio_unknown", "capital_reduction_payment_unknown"]
                )
            if ds == "TaiwanStockDividend":
                for key in (
                    "AnnouncementDate",
                    "RecordDate",
                    "CashExDividendTradingDate",
                    "StockExDividendTradingDate",
                    "CashDividendPaymentDate",
                ):
                    value = row.get(key)
                    try:
                        rec[key] = _iso(value) if value not in (None, "") else None
                    except ContractError:
                        rec[key] = None
                        flags.append("invalid_provider_date")
            reference_keys = required - {"ReasonforCapitalReduction", "type"}
            if ds == "TaiwanStockDividend":
                reference_keys = required
            elif ds == "TaiwanStockDividendResult":
                reference_keys = {"before_price", "after_price", "stock_and_cache_dividend"}
            rec["reference_fields"] = {key: _decimal(row[key], flags) for key in reference_keys}
        else:
            for key in ("type", "industry", "industry_category", "stock_name", "name", "date"):
                if key in row:
                    rec[key] = row[key]
            category = " ".join(str(row.get(k, "")) for k in ("industry_category", "type")).upper()
            if any(x in category for x in ("ETF", "ETN", "TDR", "存託憑證", "指數股票型基金")):
                rec["classification"] = "excluded_by_provider_category"
            else:
                rec["classification"] = "candidate_unverified"
                if row.get("industry_category"):
                    flags.append("ordinary_industry_unverified")
        rec["quality_flags"] = flags + (["classification_unverified"] if kind == "symbol" else [])
        records.append(rec)
    return ParsedBatch(raw, tuple(records), ("history_coverage_unverified",))


def _validate_quote(record: dict[str, object], raw: RawBatch | None = None) -> None:
    nums = {
        key: Decimal(str(record[key]))
        for key in ("open", "high", "low", "close")
        if record.get(key) is not None
    }
    if nums and (
        nums.get("high", Decimal("Infinity")) < max(nums.values())
        or nums.get("low", Decimal("-Infinity")) > min(nums.values())
    ):
        raise ContractError("OHLC range is inconsistent", raw)
    for key in ("volume", "amount"):
        value = record.get(key)
        if value is not None and Decimal(str(value)) < 0:
            raise ContractError(f"{key} must be nonnegative", raw)
    volume = record.get("volume")
    if volume is not None and Decimal(str(volume)) != Decimal(str(volume)).to_integral_value():
        raise ContractError("volume must be an integer", raw)


def _iso_date(value: str) -> str:
    value = value.strip()
    if re.fullmatch(r"\d{6,7}", value):
        y, m, d = int(value[:-4]), int(value[-4:-2]), int(value[-2:])
        value = f"{y + 1911:04d}-{m:02d}-{d:02d}"
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError as exc:
        raise ContractError("invalid CSV date") from exc


def parse_exchange_csv(raw: RawBatch) -> ParsedBatch:
    if raw.source not in {"twse", "tpex"}:
        raise ValueError("expected exchange CSV response")
    try:
        text = raw.body.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ContractError("unsupported CSV encoding", raw) from exc
    if "<html" in text[:1024].lower() or "<!doctype html" in text[:1024].lower():
        raise ContractError("HTML response is not CSV", raw)
    reader = csv.DictReader(io.StringIO(text))
    header = reader.fieldnames or []
    if not header or len(set(header)) != len(header):
        raise ContractError("empty or duplicate CSV headers", raw)
    m = (
        {
            "date": "日期",
            "symbol": "證券代號",
            "name": "證券名稱",
            "open": "開盤價",
            "high": "最高價",
            "low": "最低價",
            "close": "收盤價",
            "volume": "成交股數",
            "amount": "成交金額",
        }
        if raw.source == "twse"
        else {
            "date": "資料日期",
            "symbol": "代號",
            "name": "名稱",
            "open": "開盤",
            "high": "最高",
            "low": "最低",
            "close": "收盤",
            "volume": "成交股數",
            "amount": "成交金額",
        }
    )
    if not set(m.values()) <= set(header):
        raise ContractError("CSV required columns are missing", raw)
    rows = []
    dates = set()
    seen = set()
    for row in reader:
        if None in row or any(v is None for v in row.values()):
            raise ContractError("CSV row width mismatch", raw)
        day = _iso_date(row[m["date"]])
        dates.add(day)
        flags = ["classification_unverified", "history_coverage_unverified"]
        rec: dict[str, object] = {
            "kind": "quote",
            "symbol": row[m["symbol"]].strip(),
            "name": row[m["name"]].strip(),
            "trade_date": day,
            "record_date": day,
            "market": raw.source,
            "price_basis": "raw",
            "research_ready": False,
        }
        if not rec["symbol"]:
            raise ContractError("empty symbol", raw)
        for key in ("open", "high", "low", "close", "volume", "amount"):
            rec[key] = _decimal(row[m[key]], flags)
        if any(
            rec[k] is not None and Decimal(str(rec[k])) <= 0
            for k in ("open", "high", "low", "close")
        ):
            flags.append("nonpositive_price")
        _validate_quote(rec, raw)
        rec["quality_flags"] = flags
        rec["original"] = {k: v for k, v in row.items()}
        quote_key = (rec["symbol"], day)
        if quote_key in seen:
            raise ContractError("duplicate quote key", raw)
        seen.add(quote_key)
        rows.append(rec)
    if not dates:
        raise ContractError("empty CSV has no determinable source date", raw)
    if len(dates) > 1:
        raise ContractError("CSV rows contain inconsistent dates", raw)
    return ParsedBatch(raw, tuple(rows))
