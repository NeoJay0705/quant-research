"""Allowlisted official Taiwan exchange requests and conservative parsers."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from html.parser import HTMLParser
from typing import Any, cast
from urllib.parse import urlencode

from . import sources

URLS = {
    ("twse", "MI_INDEX"): "https://www.twse.com.tw/exchangeReport/MI_INDEX",
    ("tpex", "dailyQuotes"): "https://www.tpex.org.tw/www/zh-tw/afterTrading/dailyQuotes",
    ("twse", "TWT49U"): "https://www.twse.com.tw/rwd/zh/exRight/TWT49U",
    ("twse", "TWTAUU"): "https://www.twse.com.tw/rwd/zh/reducation/TWTAUU",
    ("twse", "TWTB8U"): "https://www.twse.com.tw/rwd/zh/change/TWTB8U",
    ("tpex", "exDailyQ"): "https://www.tpex.org.tw/www/zh-tw/bulletin/exDailyQ",
    ("tpex", "revivt"): "https://www.tpex.org.tw/www/zh-tw/bulletin/revivt",
    ("tpex", "pvChgRslt"): "https://www.tpex.org.tw/www/zh-tw/bulletin/pvChgRslt",
    ("twse", "TWT49UDetail"): "https://www.twse.com.tw/rwd/zh/exRight/TWT49UDetail",
    ("twse", "TWTAVUDetail"): "https://www.twse.com.tw/rwd/zh/reducation/TWTAVUDetail",
    ("twse", "TWTB8UDetail"): "https://www.twse.com.tw/rwd/zh/change/TWTB8UDetail",
    ("twse", "TWTB7UDetail"): "https://www.twse.com.tw/rwd/zh/change/TWTB7UDetail",
}


Transport = sources.Transport
RawBatch = sources.RawBatch


@dataclass(frozen=True)
class OfficialBatch:
    records: tuple[dict[str, object], ...]
    coverage_state: str
    actual_date: str | None
    detail_tasks: tuple[dict[str, object], ...] = ()
    warnings: tuple[str, ...] = ()


def _ymd(s: str) -> str:
    if not isinstance(s, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
        raise ValueError("date must be valid YYYY-MM-DD")
    try:
        return date.fromisoformat(s).isoformat()
    except ValueError:
        raise ValueError("date must be valid YYYY-MM-DD") from None


def _roc(s: str, slash: bool = True) -> str:
    s = s.strip()
    if re.fullmatch(r"\d{8}", s):
        return date(int(s[:4]), int(s[4:6]), int(s[6:])).isoformat()
    m = (
        re.fullmatch(r"(\d{2,3})/(\d{1,2})/(\d{1,2})", s)
        if slash
        else re.fullmatch(r"(\d{2,3})(\d{2})(\d{2})", s)
    )
    if not m:
        raise sources.ContractError("invalid source date")
    return date(int(m[1]) + 1911, int(m[2]), int(m[3])).isoformat()


def _params(source: str, ds: str, p: dict[str, str]) -> dict[str, str]:
    if (source, ds) not in URLS:
        raise ValueError("source/dataset is not allowlisted")
    contracts: dict[str, tuple[set[str], set[str]]] = {
        "MI_INDEX": ({"date"}, {"date"}),
        "dailyQuotes": ({"date"}, {"date"}),
        "TWT49U": ({"start_date", "end_date"}, {"start_date", "end_date"}),
        "TWTAUU": ({"start_date", "end_date"}, {"start_date", "end_date"}),
        "TWTB8U": ({"start_date", "end_date"}, {"start_date", "end_date"}),
        "exDailyQ": ({"start_date", "end_date"}, {"start_date", "end_date"}),
        "revivt": ({"start_date", "end_date"}, {"start_date", "end_date"}),
        "pvChgRslt": ({"start_date", "end_date"}, {"start_date", "end_date"}),
        "TWT49UDetail": ({"symbol", "date"}, {"symbol", "date"}),
        "TWTAVUDetail": ({"symbol", "file_date"}, {"symbol", "file_date"}),
        "TWTB8UDetail": (
            {"symbol", "stop_date", "resume_date"},
            {"symbol", "stop_date", "resume_date"},
        ),
        "TWTB7UDetail": ({"symbol", "file_date"}, {"symbol", "file_date"}),
    }
    required, allowed = contracts[ds]
    if not required <= p.keys() or set(p) != allowed:
        raise ValueError("missing or unsupported query parameters")
    result: dict[str, str] = {}
    if "symbol" in p:
        symbol = p["symbol"].strip()
        if not symbol:
            raise ValueError("symbol is required")
        result["STK_NO"] = symbol
    for k, v in p.items():
        if k == "symbol":
            continue
        d = _ymd(v)
        if k in {"start_date", "end_date"}:
            result[{"start_date": "startDate", "end_date": "endDate"}[k]] = (
                d.replace("-", "") if source == "twse" else d.replace("-", "/")
            )
        elif k == "date":
            result["T1" if ds == "TWT49UDetail" else "date"] = (
                d.replace("-", "")
                if ds == "TWT49UDetail" or source == "twse"
                else d.replace("-", "/")
            )
        else:
            result[
                {"file_date": "FILE_DATE", "stop_date": "STOP_DATE", "resume_date": "RESUME_DATE"}[
                    k
                ]
            ] = d.replace("-", "")
    if "start_date" in p and _ymd(p["start_date"]) > _ymd(p["end_date"]):
        raise ValueError("start_date must not exceed end_date")
    if ds == "MI_INDEX":
        result.update(response="json", type="ALLBUT0999")
    elif ds == "dailyQuotes":
        result.update(id="EW", response="json")
    else:
        result["response"] = "json"
    return result


def request(
    source: str,
    dataset: str,
    params: dict[str, str],
    *,
    transport: Transport = sources.urllib_transport,
    timeout: float = 20,
    max_bytes: int = 64 * 1024 * 1024,
) -> RawBatch:
    q = _params(source, dataset, dict(params))
    url = URLS[(source, dataset)] + "?" + urlencode(q)
    return sources.fetch_allowed(
        url,
        source=source,
        dataset=dataset,
        allowed_urls=URLS,
        transport=transport,
        timeout=timeout,
        max_bytes=max_bytes,
    )


class _Table(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.cells: list[list[str]] = []
        self.cur: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"th", "td"}:
            self.cur = [tag, ""]

    def handle_data(self, data: str) -> None:
        if self.cur is not None:
            self.cur[1] += data

    def handle_endtag(self, tag: str) -> None:
        if self.cur is not None and tag == self.cur[0]:
            self.cells.append(self.cur)
            self.cur = None


def _decimal(value: object, flags: list[str]) -> str | None:
    s = str(value).strip().replace(",", "")
    if s.upper() in {"", "NA", "N/A", "－", "X"} or re.fullmatch(r"-{1,4}", s):
        flags.append("source_placeholder")
        return None
    s = re.sub(r"\s*(股|元/股|元／股|股/股)$", "", s)
    try:
        n = Decimal(s)
    except InvalidOperation:
        raise sources.ContractError("invalid numeric field") from None
    if not n.is_finite():
        raise sources.ContractError("non-finite numeric field")
    return str(n)


def parse(raw: RawBatch, params: dict[str, str]) -> OfficialBatch:
    if (raw.source, raw.dataset) not in URLS:
        raise sources.ContractError("unknown official dataset", raw)
    try:
        doc = json.loads(raw.body, parse_float=str)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise sources.ContractError("invalid official JSON", raw) from None
    if not isinstance(doc, dict):
        raise sources.ContractError("invalid envelope", raw)
    stat = str(doc.get("stat", ""))
    if stat in {"403", "429"}:
        raise (sources.AccessBlockedError if stat == "403" else sources.QuotaError)(
            "official envelope refusal", raw
        )
    if (
        raw.source == "twse"
        and raw.dataset in {"TWT49U", "TWTAUU"}
        and doc == {"stat": "很抱歉，沒有符合條件的資料!"}
    ):
        try:
            _params(raw.source, raw.dataset, dict(params))
        except (TypeError, ValueError):
            raise sources.ContractError("invalid event request parameters", raw) from None
        return OfficialBatch(
            (),
            "empty_unverified",
            None,
            warnings=("period_envelope_unverified", "empty_unverified"),
        )
    if (
        raw.source == "twse"
        and raw.dataset == "TWT49UDetail"
        and doc in ({"stat": "無相關資料"}, {"stat": "查無相關資料"})
    ):
        try:
            _params(raw.source, raw.dataset, dict(params))
        except (AttributeError, TypeError, ValueError):
            raise sources.ContractError("invalid event request parameters", raw) from None
        return OfficialBatch(
            (),
            "empty_unverified",
            None,
            warnings=("period_envelope_unverified", "empty_unverified"),
        )
    success = stat.lower() == "ok"
    no_data = stat in {"查無資料", "查無符合條件資料", "查無符合條件的資料", "沒有符合條件的資料"}
    twse_empty = (
        raw.source == "twse" and raw.dataset == "MI_INDEX" and stat == "很抱歉，沒有符合條件的資料!"
    )
    if not (success or no_data or twse_empty):
        raise sources.ContractError("missing or unknown official response status", raw)
    ds = raw.dataset
    if ds in {"MI_INDEX", "dailyQuotes"}:
        if twse_empty:
            if doc.get("type") != "ALLBUT0999":
                raise sources.ContractError("unexpected empty response type", raw)
            if "date" in doc and _roc(str(doc["date"]), slash=False) != _ymd(params["date"]):
                raise sources.ContractError("response date mismatch", raw)
            tables_value = doc.get("tables")
            if isinstance(doc.get("data"), list) and doc["data"]:
                raise sources.ContractError("empty response contains data", raw)
            if isinstance(tables_value, list) and any(
                isinstance(table, dict) and isinstance(table.get("data"), list) and table["data"]
                for table in tables_value
            ):
                raise sources.ContractError("empty response contains data", raw)
            return OfficialBatch(
                (), "empty_unverified", None, warnings=("response_date_unverified",)
            )
        actual = (
            _roc(str(doc.get("date", "")), slash=True)
            if raw.source == "tpex"
            else _roc(str(doc.get("date", "")), slash=False)
        )
        if actual != _ymd(params["date"]):
            raise sources.ContractError("response date mismatch", raw)
        tables = doc.get("tables")
        if ds == "MI_INDEX":
            tables = [doc] if isinstance(doc.get("fields"), list) else doc.get("tables")
        if not isinstance(tables, list):
            raise sources.ContractError("quote tables missing", raw)
        if isinstance(doc.get("totalCount"), int) and doc["totalCount"] != sum(
            len(t.get("data", []))
            for t in tables
            if isinstance(t, dict) and isinstance(t.get("data"), list)
        ):
            raise sources.ContractError("totalCount mismatch", raw)
        records: list[dict[str, object]] = []
        seen: set[tuple[str, str, str]] = set()
        for t in tables:
            if not isinstance(t, dict) or not t:
                continue
            if not isinstance(t.get("fields"), list) or not isinstance(t.get("data"), list):
                raise sources.ContractError("unknown quote table schema", raw)
            if isinstance(t.get("totalCount"), int) and t["totalCount"] != len(t["data"]):
                raise sources.ContractError("totalCount mismatch", raw)
            f = t["fields"]
            low = {str(x).strip(): i for i, x in enumerate(f)}
            alias = {
                "symbol": ("證券代號", "代號", "股票代號"),
                "name": ("證券名稱", "名稱"),
                "open": ("開盤價", "開盤"),
                "high": ("最高價", "最高"),
                "low": ("最低價", "最低"),
                "close": ("收盤價", "收盤"),
                "volume": ("成交股數",),
                "amount": ("成交金額", "成交金額(元)"),
            }
            ix = {k: next((low[n] for n in names if n in low), None) for k, names in alias.items()}
            required = ("symbol", "open", "high", "low", "close", "volume", "amount")
            if any(ix[k] is None for k in required):
                header_text = " ".join(map(str, f))
                quote_signals = ("成交股數", "開盤價", "開盤", "收盤價", "收盤", "最高價", "最低價")
                if any(x in header_text for x in ("證券代號", "股票代號", "代號")) and any(
                    x in header_text for x in quote_signals
                ):
                    raise sources.ContractError(
                        "recognized quote table missing required columns", raw
                    )
                continue
            for row in t["data"]:
                if not isinstance(row, list) or len(row) != len(f):
                    raise sources.ContractError("quote row width mismatch", raw)
                flags: list[str] = []
                indexes = {key: ix[key] for key in required}
                if any(index is None for index in indexes.values()):
                    raise sources.ContractError("quote columns missing", raw)
                symbol_index = indexes["symbol"]
                assert symbol_index is not None
                symbol = str(row[symbol_index]).strip()
                if not symbol:
                    raise sources.ContractError("empty symbol", raw)
                venue = str(t.get("title") or t.get("subtitle") or "main")
                normalized_title = re.sub(r"\s+", "", venue)
                if "管理股票" in normalized_title:
                    venue_id = "management"
                elif (
                    raw.source == "tpex"
                    or ds == "dailyQuotes"
                    or normalized_title in {"一般", "每日收盤行情"}
                ):
                    venue_id = "primary"
                else:
                    venue_id = "other:" + venue
                rec = {
                    "kind": "quote",
                    "source": raw.source,
                    "market": raw.source,
                    "dataset": ds,
                    "symbol": symbol,
                    "record_date": actual,
                    "trade_date": actual,
                    "price_basis": "raw",
                    "research_ready": False,
                    "venue": venue,
                    "venue_id": venue_id,
                    "name": str(row[ix["name"]]).strip() if ix["name"] is not None else "",
                    "original": dict(zip(f, row, strict=True)),
                    "quality_flags": flags,
                }
                for k in ("open", "high", "low", "close", "volume", "amount"):
                    index = ix[k]
                    if index is None:
                        raise sources.ContractError("quote columns missing", raw)
                    rec[k] = _decimal(row[index], flags)
                sources._validate_quote(rec, raw)
                key = (venue, symbol, actual)
                if key in seen:
                    raise sources.ContractError("duplicate quote key", raw)
                seen.add(key)
                records.append(rec)
        return OfficialBatch(tuple(records), "ok" if records else "empty_unverified", actual)
    return _parse_events(raw, doc, params)


def _norm_label(value: object) -> str:
    return re.sub(r"[\s:：]", "", str(value)).replace("&nbsp", "")


def _date(value: object) -> str:
    text = str(value).strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        return _ymd(text)
    chinese = re.fullmatch(r"(\d{2,3})年(\d{1,2})月(\d{1,2})日", text)
    if chinese:
        try:
            return date(int(chinese[1]) + 1911, int(chinese[2]), int(chinese[3])).isoformat()
        except ValueError:
            raise sources.ContractError("invalid source date") from None
    if re.fullmatch(r"\d{8}", text):
        return _roc(text)
    if re.fullmatch(r"\d{7}", text):
        return _roc(text, slash=False)
    if re.fullmatch(r"\d{2,3}/\d{1,2}/\d{1,2}", text):
        return _roc(text)
    raise sources.ContractError("invalid source date")


def _parse_events(raw: RawBatch, doc: dict[str, object], params: dict[str, str]) -> OfficialBatch:
    ds = raw.dataset
    detail_ds = {"TWT49U": "TWT49UDetail", "TWTAUU": "TWTAVUDetail", "TWTB8U": "TWTB8UDetail"}
    detail_kind = {
        "TWT49UDetail": "dividend",
        "TWTAVUDetail": "capital_reduction",
        "TWTB8UDetail": "par_value",
        "TWTB7UDetail": "par_value",
    }
    event_kind = {
        "TWT49U": "dividend",
        "TWTAUU": "capital_reduction",
        "TWTB8U": "par_value",
        "exDailyQ": "dividend",
        "revivt": "capital_reduction",
        "pvChgRslt": "par_value",
        **detail_kind,
    }
    summary = ds in {"TWT49U", "TWTAUU", "TWTB8U", "exDailyQ", "revivt", "pvChgRslt"}
    start = _ymd(params["start_date"]) if summary else None
    end = _ymd(params["end_date"]) if summary else None
    tables: list[Any] = cast(list[Any], doc.get("tables"))
    if not isinstance(tables, list):
        tables = [doc]
    total = 0
    for table_any in tables:
        if isinstance(table_any, dict) and isinstance(table_any.get("data"), list):
            total += len(table_any["data"])
    if isinstance(doc.get("totalCount"), int) and doc["totalCount"] != total:
        raise sources.ContractError("totalCount mismatch", raw)
    rows: list[tuple[dict[str, str], dict[str, object], object]] = []
    envelope_ok = False
    if summary:
        period_start_key = next(
            (k for k in ("strDate", "startDate", "start_date", "startDateStr") if k in doc),
            None,
        )
        period_end_key = next((k for k in ("endDate", "end_date", "endDateStr") if k in doc), None)
        has_period = period_start_key is not None or period_end_key is not None
        if period_start_key is not None or period_end_key is not None:
            if period_start_key is None or period_end_key is None:
                raise sources.ContractError("incomplete period envelope", raw)
            if _date(doc[period_start_key]) != start or _date(doc[period_end_key]) != end:
                raise sources.ContractError("period envelope mismatch", raw)
            envelope_ok = True
        if raw.source == "tpex" and isinstance(doc.get("date"), str):
            m = re.fullmatch(r"([^~]+)~([^~]+)", str(doc["date"]).strip())
            if m:
                if _date(m[1]) != start or _date(m[2]) != end:
                    raise sources.ContractError("period envelope mismatch", raw)
                envelope_ok = True
                has_period = True
        title = str(doc.get("title", ""))
        dates = re.findall(r"\d{2,3}/\d{1,2}/\d{1,2}|\d{7,8}|\d{2,3}年\d{1,2}月\d{1,2}日", title)
        if len(dates) >= 2:
            if _date(dates[0]) != start or _date(dates[1]) != end:
                raise sources.ContractError("period envelope mismatch", raw)
            envelope_ok = True
            has_period = True
        if not has_period:
            envelope_ok = False
    for table_obj in tables:
        if (
            not isinstance(table_obj, dict)
            or not isinstance(table_obj.get("fields"), list)
            or not isinstance(table_obj.get("data"), list)
        ):
            raise sources.ContractError("unknown event schema", raw)
        table: dict[str, Any] = table_obj
        fields, data = table["fields"], table["data"]
        labels = [_norm_label(x) for x in fields]
        if len(set(labels)) != len(labels):
            raise sources.ContractError("duplicate event header", raw)
        if isinstance(table.get("totalCount"), int) and table["totalCount"] != len(data):
            raise sources.ContractError("totalCount mismatch", raw)
        index = {label: i for i, label in enumerate(labels)}
        required_by_dataset = {
            "TWT49U": ("資料日期", "股票代號", "詳細資料"),
            "TWTAUU": ("恢復買賣日期", "股票代號", "詳細資料"),
            "TWTB8U": ("恢復買賣日期", "股票代號", "詳細資料"),
            "exDailyQ": ("除權息日期", "代號"),
            "revivt": ("恢復買賣日期", "股票代號"),
            "pvChgRslt": ("恢復買賣日期", "證券代號"),
            "TWT49UDetail": ("股票代號",),
            "TWTAVUDetail": ("股票代號",),
            "TWTB8UDetail": ("股票代號",),
            "TWTB7UDetail": ("股票代號",),
        }
        if ds not in required_by_dataset:
            raise sources.ContractError("unknown event dataset", raw)
        required = required_by_dataset[ds]
        if any(k not in index for k in required):
            raise sources.ContractError("unknown event schema", raw)
        if ds in {"exDailyQ", "revivt"} and not any(
            k in index for k in ("代號", "股票代號", "證券代號")
        ):
            raise sources.ContractError("unknown event schema", raw)
        if ds in {"TWT49UDetail", "TWTAVUDetail", "TWTB8UDetail", "TWTB7UDetail"} and not any(
            k in index for k in ("股票代號", "證券代號")
        ):
            raise sources.ContractError("unknown event schema", raw)
        for row in data:
            if not isinstance(row, list) or len(row) != len(fields):
                raise sources.ContractError("event row width mismatch", raw)
            original = dict(zip(fields, row, strict=True))
            vals = {label: row[i] for label, i in index.items()}
            rows.append((vals, original, row))
    records: list[dict[str, object]] = []
    tasks: list[dict[str, object]] = []
    warnings = []
    if summary and not envelope_ok:
        warnings.append("period_envelope_unverified")
    for vals, original, _row in rows:
        symbol_key = next((k for k in ("股票代號", "證券代號", "代號") if k in vals), None)
        if symbol_key is not None:
            symbol = str(vals[cast(str, symbol_key)]).strip()
        else:
            symbol = str(params.get("symbol", "")).strip()
        if not symbol:
            raise sources.ContractError("missing event symbol")
        kind = event_kind[cast(str, ds)]
        event_date = None
        stop = resume = None
        if ds == "TWT49U":
            event_date = _date(vals["資料日期"])
        elif ds == "exDailyQ":
            event_date = _date(vals["除權息日期"])
        if ds in {"TWTAUU", "TWTB8U", "revivt", "pvChgRslt"}:
            resume = _date(vals["恢復買賣日期"])
            event_date = resume
        if ds in {"TWT49UDetail", "TWTAVUDetail", "TWTB8UDetail", "TWTB7UDetail"}:
            if symbol != str(params["symbol"]).strip():
                raise sources.ContractError("detail symbol mismatch", raw)
        if event_date and start and end and not start <= event_date <= end:
            raise sources.ContractError("record date outside query interval", raw)
        rec: dict[str, object] = {
            "kind": "event",
            "source": raw.source,
            "market": raw.source,
            "dataset": ds,
            "symbol": symbol,
            "date": event_date,
            "record_date": event_date,
            "event_date": event_date,
            "event_type": kind,
            "observation_type": "summary"
            if summary
            else "conditions"
            if ds != "TWTB8UDetail"
            else "prices",
            "research_ready": False,
            "original": original,
            "quality_flags": [],
            "missing_fields": [],
            "stop_date": stop,
            "resume_date": resume,
            "cash_dividend_per_share": None,
            "cash_return_per_share": None,
            "share_multiplier": None,
            "par_before": None,
            "par_after": None,
            "before_price": None,
            "reference_price": None,
            "reference_price_kind": None,
            "detail_params": None,
        }
        if ds in {"TWT49U", "TWTAUU", "TWTB8U"}:
            token = str(vals["詳細資料"]).strip()
            bits = [x.strip() for x in token.split(",")]
            try:
                if ds == "TWT49U" and len(bits) == 2:
                    task_params = {"symbol": bits[0], "date": _date(bits[1])}
                    expected = event_date
                elif ds == "TWTAUU" and len(bits) == 2:
                    task_params = {"symbol": bits[0], "file_date": _date(bits[1])}
                    expected = None
                elif ds == "TWTB8U" and len(bits) == 3:
                    task_params = {
                        "symbol": bits[0],
                        "stop_date": _date(bits[1]),
                        "resume_date": _date(bits[2]),
                    }
                    expected = resume
                else:
                    raise ValueError
            except (ValueError, sources.ContractError):
                raise sources.ContractError("unknown detail token format", raw) from None
            if task_params["symbol"] != symbol or (
                expected and (task_params.get("date") or task_params.get("resume_date")) != expected
            ):
                raise sources.ContractError("detail token mismatch", raw)
            rec["detail_params"] = task_params
            if "stop_date" in task_params:
                rec["stop_date"] = task_params["stop_date"]
            if ds != "TWTB8U":
                dd = detail_ds[ds]
                tasks.append(
                    {
                        "source": "twse",
                        "dataset": dd,
                        "params": task_params,
                        "context": {
                            "symbol": symbol,
                            "event_date": event_date,
                            "event_type": kind,
                            "summary_dataset": ds,
                            "summary_token": token,
                        },
                    }
                )
        fields_norm = {_norm_label(k): v for k, v in original.items()}
        if summary:
            for label in ("現金增資股數", "C. (有償) 現金增資", "C.(有償)現金增資"):
                value = fields_norm.get(_norm_label(label))
                if value is not None:
                    amount = _decimal(value, cast(list[str], rec["quality_flags"]))
                    if amount is not None and Decimal(amount) > 0:
                        cast(list[str], rec["quality_flags"]).append(
                            "cash_capital_increase_present"
                        )
            employee_labels = (
                _norm_label("B. 員工紅利轉增資"),
                _norm_label("B.員工紅利轉增資"),
                _norm_label("員工紅利轉增資"),
            )
            for label in employee_labels:
                employee = fields_norm.get(label)
                if employee is not None:
                    amount = _decimal(employee, cast(list[str], rec["quality_flags"]))
                    if amount is not None and Decimal(amount) != 0:
                        cast(list[str], rec["quality_flags"]).append(
                            "employee_bonus_shares_present"
                        )
            cash_candidates: list[object] = [
                value
                for label in (_norm_label("現金股利"), _norm_label("(每股配發現金股利)除息"))
                if label in fields_norm
                and (value := _decimal(fields_norm[label], cast(list[str], rec["quality_flags"])))
                is not None
            ]
            _merge_financial_candidates(rec, "cash_dividend_per_share", cash_candidates)
            price_labels: dict[str, tuple[str, str, str]] = {
                "TWT49U": ("除權息前收盤價", "除權息參考價", "除權息參考價"),
                "TWTAUU": ("停止買賣前收盤價格", "恢復買賣參考價", "恢復買賣參考價"),
                "TWTB8U": ("停止買賣前收盤價格", "恢復買賣參考價", "恢復買賣參考價"),
                "exDailyQ": ("除權息前收盤價", "除權息參考價", "除權息參考價"),
                "revivt": (
                    "最後交易日之收盤價格",
                    "減資恢復買賣開始日參考價格",
                    "減資恢復買賣開始日參考價格",
                ),
                "pvChgRslt": (
                    "最後交易日之收盤價格",
                    "恢復買賣開始參考價",
                    "恢復買賣開始參考價",
                ),
            }
            prices = price_labels.get(cast(str, ds))
            if prices is not None:
                before_label, reference_label, reference_kind = prices
                before = fields_norm.get(_norm_label(before_label))
                reference = fields_norm.get(_norm_label(reference_label))
                if before is not None:
                    rec["before_price"] = _decimal(before, cast(list[str], rec["quality_flags"]))
                if reference is not None:
                    rec["reference_price"] = _decimal(
                        reference, cast(list[str], rec["quality_flags"])
                    )
                    rec["reference_price_kind"] = reference_kind
                if before is None:
                    cast(list[str], rec["missing_fields"]).append("before_price")
                if reference is None:
                    cast(list[str], rec["missing_fields"]).append("reference_price")
            if ds == "exDailyQ":
                bonus = fields_norm.get("每仟股無償配股")
                if rec["cash_dividend_per_share"] is None and not any(
                    fields_norm.get(_norm_label(label))
                    for label in ("現金股利", "(每股配發現金股利)除息")
                ):
                    cast(list[str], rec["missing_fields"]).append("cash_dividend_per_share")
                if bonus is not None:
                    bonus_value = _decimal(bonus, cast(list[str], rec["quality_flags"]))
                    if bonus_value is not None:
                        rec["bonus_share_ratio"] = str(Decimal(bonus_value) / 1000)
                        rec["share_multiplier"] = str(Decimal(1) + Decimal(bonus_value) / 1000)
                    else:
                        cast(list[str], rec["missing_fields"]).append("share_multiplier")
                else:
                    cast(list[str], rec["missing_fields"]).append("share_multiplier")
        html = str(fields_norm.get("詳細資料", ""))
        detail_values: dict[str, str] = {}
        if html.startswith("<table"):
            parser = _Table()
            parser.feed(html)
            pairs = {
                _norm_label(parser.cells[i][1]): parser.cells[i + 1][1].strip()
                for i in range(0, len(parser.cells) - 1, 2)
            }
            if not pairs:
                raise sources.ContractError("unknown detail HTML", raw)
            detail_values = pairs
            for key in ("股票代號/證券名稱", "股票代號/股票名稱", "證券代號/證券名稱"):
                if key in detail_values and detail_values[key].split("/")[0].strip() != symbol:
                    raise sources.ContractError("HTML detail symbol mismatch", raw)
        if not detail_values and not html.startswith("<table") and not summary:
            detail_values = {str(k): str(v) for k, v in original.items()}
        if detail_values:
            extracted = _detail_fields(detail_values, event_type=kind)
            extracted_flags = cast(list[str], extracted.pop("quality_flags", []))
            extracted_missing = cast(list[str], extracted.pop("missing_fields", []))
            cast(list[str], rec["quality_flags"]).extend(extracted_flags)
            cast(list[str], rec["missing_fields"]).extend(extracted_missing)
            financial_fields = {
                "cash_dividend_per_share",
                "cash_return_per_share",
                "bonus_share_ratio",
                "share_multiplier",
                "par_before",
                "par_after",
                "before_price",
                "reference_price",
            }
            extracted_conflicts = cast(
                dict[str, list[object]], extracted.pop("field_conflicts", {})
            )
            for field in financial_fields:
                incoming = extracted_conflicts.get(field, [])
                if not incoming and extracted.get(field) is not None:
                    incoming = [extracted[field]]
                _merge_financial_candidates(rec, field, incoming)
            for field, value in extracted.items():
                if field not in financial_fields and field in {
                    "employee_bonus_shares_present",
                    "cash_capital_increase_present",
                }:
                    continue
                if field not in financial_fields and field not in {
                    "missing_fields",
                    "quality_flags",
                }:
                    rec[field] = value
            if ds in {"TWTB8UDetail", "TWTB7UDetail"}:
                before = next(
                    (
                        v
                        for k, v in detail_values.items()
                        if _norm_label(k) in {"停止買賣前收盤價格", "變更前收盤價"}
                    ),
                    None,
                )
                reference = next(
                    (
                        v
                        for k, v in detail_values.items()
                        if _norm_label(k) in {"恢復買賣參考價", "變更後參考價"}
                    ),
                    None,
                )
                if before is not None:
                    rec["before_price"] = _decimal(before, cast(list[str], rec["quality_flags"]))
                if reference is not None:
                    rec["reference_price"] = _decimal(
                        reference, cast(list[str], rec["quality_flags"])
                    )
                    rec["reference_price_kind"] = "恢復買賣參考價"
            stop_value = next(
                (v for k, v in detail_values.items() if _norm_label(k) == "停止買賣日期"), None
            )
            if stop_value is not None:
                rec["stop_date"] = _date(stop_value)
            else:
                cast(list[str], rec["missing_fields"]).append("stop_date")
        if ds in {"TWT49UDetail", "TWTAVUDetail", "TWTB8UDetail", "TWTB7UDetail"}:
            rec["event_date"] = rec["date"] = rec["record_date"] = None
            cast(list[str], rec["missing_fields"]).append("event_date")
        if summary and event_date and not envelope_ok:
            cast(list[str], rec["quality_flags"]).append("period_envelope_unverified")
        records.append(rec)
    if summary and not records:
        warnings.append("empty_unverified")
    return OfficialBatch(
        tuple(records),
        "ok" if records else "empty_unverified",
        None,
        tuple(tasks),
        tuple(dict.fromkeys(warnings)),
    )


def _merge_financial_candidates(
    record: dict[str, object], field: str, incoming: list[object]
) -> None:
    conflicts = cast(dict[str, object], record.get("field_conflicts", {}))
    prior = conflicts.get(field)
    candidates = list(prior) if isinstance(prior, list) else []
    if prior is None and record.get(field) is not None:
        candidates.append(record[field])
    candidates.extend(candidate for candidate in incoming if candidate is not None)
    unique: list[object] = []
    for candidate in candidates:
        try:
            duplicate = any(Decimal(str(candidate)) == Decimal(str(item)) for item in unique)
        except InvalidOperation:
            duplicate = candidate in unique
        if not duplicate:
            unique.append(candidate)
    if len(unique) > 1:
        cast(dict[str, object], record.setdefault("field_conflicts", {}))[field] = unique
        record[field] = None
        flags = cast(list[str], record["quality_flags"])
        if "conflicting_source_fields" not in flags:
            flags.append("conflicting_source_fields")
    elif unique:
        record[field] = unique[0]


def _detail_fields(detail: dict[str, str], *, event_type: str | None = None) -> dict[str, object]:
    d = {_norm_label(k): str(v).strip() for k, v in detail.items()}
    flags: list[str] = []
    out: dict[str, object] = {}
    missing: list[str] = []

    def numbers(*labels: str) -> list[str]:
        values: list[str] = []
        for label in labels:
            normalized = _norm_label(label)
            if normalized not in d:
                continue
            value = _decimal(d[normalized], flags)
            if value is not None and not any(Decimal(value) == Decimal(v) for v in values):
                values.append(value)
        return values

    def add_candidates(field: str, candidates: list[str]) -> None:
        if not candidates:
            return
        if len(candidates) == 1:
            out[field] = candidates[0]
        else:
            out[field] = None
            cast(dict[str, list[object]], out.setdefault("field_conflicts", {}))[field] = cast(
                list[object], candidates
            )
            flags.append("conflicting_source_fields")

    thousand_multiplier = numbers("每壹仟股換發新股票")
    direct_multiplier = numbers("每股換發新股票", "變更股票面額換股率")
    multiplier_candidates = [str(Decimal(value) / 1000) for value in thousand_multiplier]
    multiplier_candidates.extend(direct_multiplier)
    unique_multiplier: list[str] = []
    for value in multiplier_candidates:
        if not any(Decimal(value) == Decimal(existing) for existing in unique_multiplier):
            unique_multiplier.append(value)
    add_candidates("share_multiplier", unique_multiplier)

    bonus = numbers(
        "A. 按普通股股東持股比例每千股無償配股",
        "按普通股股東持股比例每千股無償配股",
        "每仟股無償配股",
    )
    add_candidates("bonus_share_ratio", [str(Decimal(value) / 1000) for value in bonus])
    if (
        event_type == "dividend"
        and out.get("bonus_share_ratio") is not None
        and "share_multiplier" not in cast(dict[str, list[object]], out.get("field_conflicts", {}))
    ):
        dividend_multiplier_candidates: list[str] = []
        if out.get("share_multiplier") is not None:
            dividend_multiplier_candidates.append(str(out["share_multiplier"]))
        derived = str(Decimal(1) + Decimal(str(out["bonus_share_ratio"])))
        if not any(Decimal(value) == Decimal(derived) for value in dividend_multiplier_candidates):
            dividend_multiplier_candidates.append(derived)
        add_candidates("share_multiplier", dividend_multiplier_candidates)

    employee_bonus = numbers("B. 員工紅利轉增資", "B.員工紅利轉增資", "員工紅利轉增資")
    if any(Decimal(value) != 0 for value in employee_bonus):
        flags.append("employee_bonus_shares_present")
    for key, labels in (
        ("cash_return_per_share", ("每股退還股款",)),
        ("cash_dividend_per_share", ("現金股利", "原股每股配發現金股利", "(每股配發現金股利)除息")),
        ("par_before", ("變更前股票面額", "變更前面額")),
        ("par_after", ("變更後股票面額", "變更後面額")),
    ):
        candidates = numbers(*labels)
        add_candidates(key, candidates)
        if not candidates:
            missing.append(key)
    if "share_multiplier" not in out:
        missing.append("share_multiplier")

    for label, value in d.items():
        if "有償" in label or "現金增資" in label:
            amount = _decimal(value, flags)
            if amount is not None and Decimal(amount) > 0:
                flags.append("cash_capital_increase_present")
                break
    out["missing_fields"] = missing
    out["quality_flags"] = list(dict.fromkeys(flags))
    return out
