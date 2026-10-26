import json
from urllib.parse import parse_qs, urlparse

import pytest

from quant_research.taiwan import official, sources


def batch(source, dataset, payload):
    body = (
        payload if isinstance(payload, bytes) else json.dumps(payload, ensure_ascii=False).encode()
    )
    return sources.RawBatch(source, dataset, "offline", "", 200, body, "", 0)


def fake_transport(sink):
    def transport(url, headers, timeout, max_bytes):
        sink.append(url)
        return 200, b'{"stat":"OK","tables":[]}'

    return transport


def quote_payload(source, fields=None, rows=None):
    fields = fields or [
        "證券代號",
        "證券名稱",
        "開盤價",
        "最高價",
        "最低價",
        "收盤價",
        "成交股數",
        "成交金額",
    ]
    rows = rows or [["1037", "台灣", "12", "13", "11", "12", "1", "12"]]
    table = {"fields": fields, "data": rows, "title": "每日收盤行情"}
    return {
        "stat": "OK" if source == "twse" else "ok",
        "date": "20180102" if source == "twse" else "107/01/02",
        "tables": [table],
    }


def test_tpex_management_title_precedes_source_primary_default():
    fields = [
        "證券代號",
        "證券名稱",
        "開盤價",
        "最高價",
        "最低價",
        "收盤價",
        "成交股數",
        "成交金額",
    ]
    regular = {
        "title": "一般",
        "fields": fields,
        "data": [["1234", "一般股", "10", "11", "9", "10", "100", "1000"]],
    }
    management = {
        "title": "管理股票",
        "fields": fields,
        "data": [["5678", "管理股", "10", "11", "9", "10", "100", "1000"]],
    }
    payload = {
        "stat": "ok",
        "date": "107/01/02",
        "tables": [management, regular],
    }
    parsed = official.parse(batch("tpex", "dailyQuotes", payload), {"date": "2018-01-02"})
    assert {record["symbol"]: record["venue_id"] for record in parsed.records} == {
        "5678": "management",
        "1234": "primary",
    }


def test_market_contracts_and_exact_normalization():
    row = ["1037", "台灣", "12.00", "13.00", "11.00", "12.50", "1037", "12962"]
    parsed = official.parse(
        batch("twse", "MI_INDEX", quote_payload("twse", rows=[row])), {"date": "2018-01-02"}
    )
    record = parsed.records[0]
    assert (
        record["kind"],
        record["market"],
        record["dataset"],
        record["record_date"],
        record["price_basis"],
        record["research_ready"],
    ) == ("quote", "twse", "MI_INDEX", "2018-01-02", "raw", False)
    assert record["symbol"] == "1037" and record["name"] == "台灣"
    assert (
        record["open"],
        record["high"],
        record["low"],
        record["close"],
        record["volume"],
        record["amount"],
    ) == ("12.00", "13.00", "11.00", "12.50", "1037", "12962")
    for symbol, volume in (("4415", "2012"), ("006201", "90000")):
        otc_row = [symbol, "ETF/管理", "10", "11", "9", "10", volume, "20000"]
        otc = official.parse(
            batch("tpex", "dailyQuotes", quote_payload("tpex", rows=[otc_row])),
            {"date": "2018-01-02"},
        )
        assert otc.records[0]["symbol"] == symbol and otc.records[0]["volume"] == volume
        assert otc.records[0]["name"] == "ETF/管理" and otc.records[0]["venue"] == "每日收盤行情"


def test_all_dataset_urls_use_fake_transport_and_exact_contracts():
    cases = [
        (
            "twse",
            "MI_INDEX",
            {"date": "2018-01-02"},
            {"date": ["20180102"], "response": ["json"], "type": ["ALLBUT0999"]},
        ),
        (
            "tpex",
            "dailyQuotes",
            {"date": "2018-01-02"},
            {"date": ["2018/01/02"], "id": ["EW"], "response": ["json"]},
        ),
        (
            "twse",
            "TWT49U",
            {"start_date": "2018-01-01", "end_date": "2018-01-31"},
            {"startDate": ["20180101"], "endDate": ["20180131"], "response": ["json"]},
        ),
        (
            "twse",
            "TWTAUU",
            {"start_date": "2018-01-01", "end_date": "2018-01-31"},
            {"startDate": ["20180101"], "endDate": ["20180131"], "response": ["json"]},
        ),
        (
            "twse",
            "TWTB8U",
            {"start_date": "2018-01-01", "end_date": "2018-01-31"},
            {"startDate": ["20180101"], "endDate": ["20180131"], "response": ["json"]},
        ),
        (
            "tpex",
            "exDailyQ",
            {"start_date": "2018-01-01", "end_date": "2018-01-31"},
            {"startDate": ["2018/01/01"], "endDate": ["2018/01/31"], "response": ["json"]},
        ),
        (
            "tpex",
            "revivt",
            {"start_date": "2018-01-01", "end_date": "2018-01-31"},
            {"startDate": ["2018/01/01"], "endDate": ["2018/01/31"], "response": ["json"]},
        ),
        (
            "tpex",
            "pvChgRslt",
            {"start_date": "2018-01-01", "end_date": "2018-01-31"},
            {"startDate": ["2018/01/01"], "endDate": ["2018/01/31"], "response": ["json"]},
        ),
        (
            "twse",
            "TWT49UDetail",
            {"symbol": "4415", "date": "2018-01-02"},
            {"STK_NO": ["4415"], "T1": ["20180102"], "response": ["json"]},
        ),
        (
            "twse",
            "TWTAVUDetail",
            {"symbol": "4415", "file_date": "2018-01-02"},
            {"STK_NO": ["4415"], "FILE_DATE": ["20180102"], "response": ["json"]},
        ),
        (
            "twse",
            "TWTB8UDetail",
            {"symbol": "4415", "stop_date": "2018-01-02", "resume_date": "2018-01-03"},
            {
                "STK_NO": ["4415"],
                "STOP_DATE": ["20180102"],
                "RESUME_DATE": ["20180103"],
                "response": ["json"],
            },
        ),
        (
            "twse",
            "TWTB7UDetail",
            {"symbol": "4415", "file_date": "2018-01-02"},
            {"STK_NO": ["4415"], "FILE_DATE": ["20180102"], "response": ["json"]},
        ),
    ]
    for source, dataset, params, expected in cases:
        called = []
        raw = official.request(source, dataset, params, transport=fake_transport(called))
        assert called == [raw.url] and parse_qs(urlparse(raw.url).query) == expected
    for source, dataset, params, _ in cases:
        with pytest.raises(ValueError):
            official._params(source, dataset, {**params, "unexpected": "x"})
        if len(params) > 1:
            with pytest.raises(ValueError):
                official._params(source, dataset, {next(iter(params)): next(iter(params.values()))})
    for params in ({"date": "2018-1-02"}, {"date": "2018-02-30"}):
        with pytest.raises(ValueError):
            official._params("twse", "MI_INDEX", params)
    with pytest.raises(ValueError):
        official._params("twse", "TWT49U", {"start_date": "2018-02-01", "end_date": "2018-01-01"})


def test_actual_date_and_quote_schema_validation():
    payload = quote_payload("twse")
    payload["date"] = "20180103"
    with pytest.raises(sources.ContractError, match="date mismatch"):
        official.parse(batch("twse", "MI_INDEX", payload), {"date": "2018-01-02"})
    payload = quote_payload("tpex")
    payload["date"] = "107/01/03"
    with pytest.raises(sources.ContractError, match="date mismatch"):
        official.parse(batch("tpex", "dailyQuotes", payload), {"date": "2018-01-02"})
    for placeholder in ("-", "--", "---", "----"):
        assert official._decimal(placeholder, []) is None
    assert official._decimal("-1", []) == "-1"
    with pytest.raises(sources.ContractError, match="recognized quote table"):
        official.parse(
            batch(
                "twse",
                "MI_INDEX",
                quote_payload("twse", fields=["證券代號", "開盤價"], rows=[["4415", "10"]]),
            ),
            {"date": "2018-01-02"},
        )


def test_twse_known_empty_response_is_unverified_and_fails_closed():
    base = {"stat": "很抱歉，沒有符合條件的資料!", "type": "ALLBUT0999"}
    parsed = official.parse(batch("twse", "MI_INDEX", base), {"date": "2018-01-01"})
    assert parsed.records == () and parsed.coverage_state == "empty_unverified"
    assert parsed.actual_date is None and parsed.warnings == ("response_date_unverified",)
    for bad in (
        {**base, "type": "wrong"},
        {**base, "data": [["x"]]},
        {**base, "tables": [{"data": [["x"]]}]},
        {**base, "stat": "other"},
        {**base, "date": "20180102"},
    ):
        with pytest.raises(sources.ContractError):
            official.parse(batch("twse", "MI_INDEX", bad), {"date": "2018-01-01"})
    tpex = {
        "stat": "ok",
        "date": "20180101",
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
                "data": [],
                "totalCount": 0,
            }
        ],
    }
    parsed = official.parse(batch("tpex", "dailyQuotes", tpex), {"date": "2018-01-01"})
    assert parsed.coverage_state == "empty_unverified" and parsed.actual_date == "2018-01-01"
    with pytest.raises(sources.ContractError, match="date mismatch"):
        official.parse(
            batch("tpex", "dailyQuotes", {**tpex, "date": "20180102"}), {"date": "2018-01-01"}
        )


def test_envelope_fail_closed_and_finmind_transport_guard(monkeypatch):
    for envelope in (
        {"date": "20180102", "tables": []},
        {"stat": "mystery", "date": "20180102", "tables": []},
    ):
        with pytest.raises(sources.ContractError):
            official.parse(batch("twse", "MI_INDEX", envelope), {"date": "2018-01-02"})
    calls = []

    def opener(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("network opener called")

    monkeypatch.setattr("urllib.request.build_opener", opener)
    with pytest.raises(sources.AccessBlockedError):
        sources.request("finmind", dataset="TaiwanStockInfo")
    assert calls == []


def test_http_status_partial_and_captcha_contracts():
    from quant_research.taiwan.sources import _PartialTransportError

    def partial(url, headers, timeout, max_bytes):
        raise _PartialTransportError(429, b"partial", "read interrupted", "120")

    with pytest.raises(sources.QuotaError) as caught:
        official.request("twse", "MI_INDEX", {"date": "2018-01-02"}, transport=partial)
    assert (
        caught.value.raw is not None
        and caught.value.raw.body == b"partial"
        and caught.value.raw.retry_after == "120"
    )

    def captcha(url, headers, timeout, max_bytes):
        return 200, b"captcha"

    with pytest.raises(sources.CaptchaError):
        official.request("twse", "MI_INDEX", {"date": "2018-01-02"}, transport=captcha)
