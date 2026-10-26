from __future__ import annotations

import json

import pytest

from quant_research.taiwan.sources import (
    AccessBlockedError,
    CaptchaError,
    ContractError,
    HttpError,
    QuotaError,
    RawBatch,
    _decimal,
    parse_exchange_csv,
    parse_finmind,
    request,
    urllib_transport,
)

# ruff: noqa: E501


def raw(source: str, body: bytes, dataset: str | None = None) -> RawBatch:
    return RawBatch(source, dataset, "safe", "2026-10-22T00:00:00+00:00", 200, body, "hash", 0.0)


def test_allowlist_token_header_and_safe_url() -> None:
    observed: dict[str, object] = {}

    def transport(
        url: str, headers: dict[str, str], timeout: float, max_bytes: int
    ) -> tuple[int, bytes]:
        observed.update(url=url, headers=headers)
        return 200, b'{"status":"success","data":[]}'

    got = request("finmind", dataset="TaiwanStockInfo", token="secret", transport=transport)
    assert got.body == b'{"status":"success","data":[]}'
    assert "secret" not in str(observed["url"])
    assert observed["headers"]["Authorization"] == "Bearer secret"  # type: ignore[index]
    with pytest.raises(ValueError):
        request("other", transport=transport)
    calls = []

    def fake(url, headers, timeout, max_bytes):
        calls.append(url)
        return 200, b'{"status":"success","data":[]}'

    request(
        "finmind",
        dataset="TaiwanStockCapitalReductionReferencePrice",
        params={"data_id": "2409", "start_date": "2022-10-11", "end_date": "2022-10-11"},
        transport=fake,
    )
    assert "dataset=TaiwanStockCapitalReductionReferencePrice" in calls[0]
    with pytest.raises(ValueError):
        request(
            "finmind",
            dataset="TaiwanStockCapitalReduction",
            params={"data_id": "2409", "start_date": "2022-10-11", "end_date": "2022-10-11"},
            transport=fake,
        )
    assert len(calls) == 1


def test_finmind_envelope_and_csv_decimals_placeholders_dates() -> None:
    parsed = parse_finmind(raw("finmind", b'{"status":"success","data":[]}'))
    assert parsed.records == ()
    with pytest.raises(ContractError):
        parse_finmind(raw("finmind", b'{"status":"error","data":[]}'))
    body = "日期,證券代號,證券名稱,成交股數,成交金額,開盤價,最高價,最低價,收盤價\n1151022,2330,台積電,1,2,3,--,2,3\n".encode()
    parsed_csv = parse_exchange_csv(raw("twse", body))
    assert parsed_csv.records[0]["trade_date"] == "2026-10-22"
    assert parsed_csv.records[0]["high"] is None
    assert "source_placeholder" in parsed_csv.records[0]["quality_flags"]  # type: ignore[operator]


def test_tpex_dash_placeholders_keep_original_and_zero_turnover() -> None:
    header = "資料日期,代號,名稱,成交股數,成交金額,開盤,最高,最低,收盤\n"
    row = "1151022,020001,富邦存股雙十N,0,0,---,----, --- ,28.93\n"
    parsed = parse_exchange_csv(raw("tpex", (header + row).encode()))
    record = parsed.records[0]
    assert (record["open"], record["high"], record["low"], record["close"]) == (
        None,
        None,
        None,
        "28.93",
    )
    assert (record["volume"], record["amount"]) == ("0", "0")
    assert "source_placeholder" in record["quality_flags"]  # type: ignore[operator]
    assert record["original"] == {
        "資料日期": "1151022",
        "代號": "020001",
        "名稱": "富邦存股雙十N",
        "成交股數": "0",
        "成交金額": "0",
        "開盤": "---",
        "最高": "----",
        "最低": " --- ",
        "收盤": "28.93",
    }
    flags: list[str] = []
    assert _decimal("-1", flags) == "-1"
    assert flags == []


def test_csv_contract_html_date_and_duplicates_fail_closed() -> None:
    head = "日期,證券代號,證券名稱,成交股數,成交金額,開盤價,最高價,最低價,收盤價\n"
    with pytest.raises(ContractError):
        parse_exchange_csv(raw("twse", b"<html>blocked"))
    with pytest.raises(ContractError):
        parse_exchange_csv(raw("twse", (head + "bad,2330,x,1,2,3,4,2,3\n").encode()))
    row = "1151022,2330,x,1,2,3,4,2,3\n"
    with pytest.raises(ContractError):
        parse_exchange_csv(raw("twse", (head + row + row).encode()))


def test_403_captcha_and_html_finmind_rejected() -> None:
    def blocked(
        url: str, headers: dict[str, str], timeout: float, max_bytes: int
    ) -> tuple[int, bytes]:
        return 403, b"captcha"

    with pytest.raises(CaptchaError):
        request("twse", transport=blocked)
    with pytest.raises(ContractError):
        parse_finmind(raw("finmind", b"<html>error</html>"))


def test_finmind_dataset_normalization_and_dates() -> None:
    data = {
        "status": "success",
        "data": [
            {
                "date": "2026-01-02",
                "stock_id": "2330",
                "Trading_Volume": "10",
                "Trading_money": "200",
                "open": "20",
                "max": "22",
                "min": "19",
                "close": "21",
            }
        ],
    }
    rec = parse_finmind(
        raw("finmind", json.dumps(data).encode(), "TaiwanStockPrice"),
        {"data_id": "2330", "start_date": "2026-01-01", "end_date": "2026-01-31"},
    ).records[0]
    assert (rec["high"], rec["low"], rec["market"], rec["kind"]) == ("22", "19", None, "quote")
    assert rec["original"] == data["data"][0]
    bad = dict(data)
    bad["data"] = [{**data["data"][0], "date": "2026-02-01"}]
    with pytest.raises(ContractError):
        parse_finmind(
            raw("finmind", json.dumps(bad).encode(), "TaiwanStockPrice"),
            {"data_id": "2330", "start_date": "2026-01-01", "end_date": "2026-01-31"},
        )


def test_envelope_quota_block_and_transport_secret_redaction() -> None:
    for status, exception in ((402, QuotaError), (429, QuotaError), (403, AccessBlockedError)):
        body = json.dumps({"status": status, "data": []}).encode()
        with pytest.raises(exception):
            parse_finmind(raw("finmind", body, "TaiwanStockInfo"))
    secret = "top-secret-token"

    def broken(url, headers, timeout, max_bytes):
        raise RuntimeError(f"failure {secret}")

    with pytest.raises(HttpError) as error:
        request("finmind", dataset="TaiwanStockInfo", token=secret, transport=broken)
    assert secret not in str(error.value)


def test_error_envelopes_without_data_keep_raw() -> None:
    for status, error in ((402, QuotaError), (403, AccessBlockedError), (429, QuotaError)):
        with pytest.raises(error) as caught:
            parse_finmind(
                raw("finmind", json.dumps({"status": status}).encode(), "TaiwanStockInfo")
            )
        assert caught.value.raw is not None


def test_info_events_fail_closed_and_keep_original_precision() -> None:
    info = {
        "stock_id": "2330",
        "type": "公司",
        "industry_category": "半導體",
        "name": "ETF sounding company",
        "date": "source-date",
    }
    rec = parse_finmind(
        raw(
            "finmind", json.dumps({"status": "success", "data": [info]}).encode(), "TaiwanStockInfo"
        )
    ).records[0]
    assert rec["original"] == info
    assert rec["name"] == info["name"]
    assert rec["classification"] == "candidate_unverified"
    assert "ordinary_industry_unverified" in rec["quality_flags"]
    for dataset, event in (
        (
            "TaiwanStockDividend",
            {
                k: "1.25"
                for k in (
                    "CashEarningsDistribution",
                    "CashStatutorySurplus",
                    "StockEarningsDistribution",
                    "StockStatutorySurplus",
                )
            },
        ),
        (
            "TaiwanStockDividendResult",
            {
                "before_price": "1.25",
                "after_price": "1.5",
                "stock_and_cache_dividend": "0.1",
                "stock_or_cache_dividend": "息",
            },
        ),
        (
            "TaiwanStockCapitalReductionReferencePrice",
            {
                "ClosingPriceonTheLastTradingDay": "14.7",
                "PostReductionReferencePrice": "15.87",
                "ReasonforCapitalReduction": "Cash refund",
            },
        ),
        ("TaiwanStockSplitPrice", {"before_price": "10", "after_price": "5", "type": "split"}),
        ("TaiwanStockParValueChange", {"before_close": "10", "after_ref_close": "5"}),
    ):
        row = {"stock_id": "2330", "date": "2026-01-01", **event}
        encoded = json.dumps({"status": "success", "data": [row]}, separators=(",", ":")).encode()
        result = parse_finmind(raw("finmind", encoded, dataset)).records[0]
        assert result["original"] == row
        assert result["reference_fields"]
        assert not result["research_ready"]
        with pytest.raises(ContractError):
            parse_finmind(
                raw(
                    "finmind",
                    json.dumps(
                        {"status": "success", "data": [{"date": "2026-01-01", **event}]}
                    ).encode(),
                    dataset,
                )
            )
    with pytest.raises(ContractError):
        parse_finmind(
            raw(
                "finmind",
                json.dumps({"status": "success", "data": [{"symbol": "1"}]}).encode(),
                "TaiwanStockInfo",
            )
        )


def test_capital_reduction_reference_price_official_dataset_shape() -> None:
    row = {
        "date": "2022-10-11",
        "stock_id": "2409",
        "ClosingPriceonTheLastTradingDay": "14.7",
        "PostReductionReferencePrice": "15.87",
        "ReasonforCapitalReduction": "Cash refund",
    }
    parsed = parse_finmind(
        raw(
            "finmind",
            json.dumps({"status": "success", "data": [row]}).encode(),
            "TaiwanStockCapitalReductionReferencePrice",
        ),
        {"data_id": "2409", "start_date": "2022-10-11", "end_date": "2022-10-11"},
    )
    record = parsed.records[0]
    assert record["date"] == "2022-10-11"
    assert record["reference_fields"] == {
        "ClosingPriceonTheLastTradingDay": "14.7",
        "PostReductionReferencePrice": "15.87",
    }
    assert record["original"] == row


def test_dividend_result_text_type_and_dividend_dates_are_preserved() -> None:
    result_row = {
        "date": "2018-06-25",
        "stock_id": "2330",
        "before_price": "227.5",
        "after_price": "219.5",
        "stock_and_cache_dividend": "8",
        "stock_or_cache_dividend": "息",
    }
    result = parse_finmind(
        raw(
            "finmind",
            json.dumps({"status": "success", "data": [result_row]}).encode(),
            "TaiwanStockDividendResult",
        )
    ).records[0]
    assert result["original"] == result_row
    assert result["record_date"] == "2018-06-25"
    assert result["reference_fields"] == {
        "before_price": "227.5",
        "after_price": "219.5",
        "stock_and_cache_dividend": "8",
    }

    dividend = {
        "date": "2018-06-25",
        "stock_id": "2330",
        "CashEarningsDistribution": "8",
        "CashStatutorySurplus": "0",
        "StockEarningsDistribution": "0",
        "StockStatutorySurplus": "0",
        "AnnouncementDate": "2018-06-07",
        "CashExDividendTradingDate": "2018-06-25",
        "StockExDividendTradingDate": "2018-06-25",
        "CashDividendPaymentDate": "2018-07-19",
        "RecordDate": "2018-07-01",
    }
    parsed = parse_finmind(
        raw(
            "finmind",
            json.dumps({"status": "success", "data": [dividend]}).encode(),
            "TaiwanStockDividend",
        )
    ).records[0]
    assert parsed["AnnouncementDate"] == "2018-06-07"
    assert parsed["RecordDate"] == "2018-07-01"
    assert parsed["CashExDividendTradingDate"] == "2018-06-25"
    assert parsed["StockExDividendTradingDate"] == "2018-06-25"
    assert parsed["CashDividendPaymentDate"] == "2018-07-19"
    assert parsed["original"] == dividend

    dividend["AnnouncementDate"] = "invalid"
    invalid = parse_finmind(
        raw(
            "finmind",
            json.dumps({"status": "success", "data": [dividend]}).encode(),
            "TaiwanStockDividend",
        )
    ).records[0]
    assert invalid["AnnouncementDate"] is None
    assert "invalid_provider_date" in invalid["quality_flags"]


def test_transport_partial_body_is_attached_and_incomplete(monkeypatch) -> None:
    class Response:
        status = 200
        closed = False
        calls = 0

        def read(self, size):
            self.calls += 1
            if self.calls == 1:
                return b"partial"
            raise OSError("secret")

        def close(self):
            self.closed = True

    response = Response()

    class Opener:
        def open(self, request, timeout):
            return response

    monkeypatch.setattr(
        "quant_research.taiwan.sources.urllib.request.build_opener", lambda *args: Opener()
    )
    monkeypatch.setattr("quant_research.taiwan.sources.time.monotonic", lambda: 1.0)
    with pytest.raises(HttpError) as caught:
        request("twse", transport=urllib_transport)
    assert caught.value.raw is not None
    assert caught.value.raw.body == b"partial"
    assert caught.value.raw.complete is False
    assert response.closed
    assert "secret" not in str(caught.value)


@pytest.mark.parametrize(("status", "error"), [(403, AccessBlockedError), (429, QuotaError)])
def test_partial_http_block_or_quota_status_takes_precedence(monkeypatch, status, error) -> None:
    class Response:
        closed = False
        calls = 0

        def __init__(self):
            self.status = status

        def read(self, size):
            self.calls += 1
            if self.calls == 1:
                return b"partial"
            raise OSError("connection interrupted")

        def close(self):
            self.closed = True

    response = Response()

    class Opener:
        def open(self, request, timeout):
            return response

    monkeypatch.setattr(
        "quant_research.taiwan.sources.urllib.request.build_opener", lambda *args: Opener()
    )
    monkeypatch.setattr("quant_research.taiwan.sources.time.monotonic", lambda: 1.0)
    with pytest.raises(error) as caught:
        request("twse", transport=urllib_transport)
    assert caught.value.raw is not None
    assert caught.value.raw.status == status
    assert caught.value.raw.body == b"partial"
    assert caught.value.raw.complete is False
    assert response.closed


def test_transport_deadline_and_byte_limit_use_fake_response(monkeypatch) -> None:
    class Response:
        status = 200
        closed = False

        def __init__(self, payload):
            self.payload = payload

        def read(self, size):
            return self.payload

        def close(self):
            self.closed = True

    class Opener:
        def __init__(self, response):
            self.response = response

        def open(self, request, timeout):
            return self.response

    response = Response(b"")
    monkeypatch.setattr(
        "quant_research.taiwan.sources.urllib.request.build_opener", lambda *args: Opener(response)
    )
    clock = iter((0.0, 0.0, 2.0, 2.0))
    monkeypatch.setattr("quant_research.taiwan.sources.time.monotonic", lambda: next(clock))
    with pytest.raises(HttpError) as caught:
        request("twse", transport=urllib_transport, timeout=1)
    assert caught.value.raw is not None and caught.value.raw.body == b""
    assert caught.value.raw.complete is False and response.closed

    response = Response(b"1234")
    monkeypatch.setattr(
        "quant_research.taiwan.sources.urllib.request.build_opener", lambda *args: Opener(response)
    )
    clock = iter((0.0, 0.0, 0.0, 0.0))
    monkeypatch.setattr("quant_research.taiwan.sources.time.monotonic", lambda: next(clock))
    with pytest.raises(ContractError) as oversized:
        request("twse", transport=urllib_transport, timeout=1, max_bytes=3)
    assert oversized.value.raw is not None and oversized.value.raw.body == b"1234"
    assert response.closed


def test_request_range_and_http_redirect_fail() -> None:
    with pytest.raises(ValueError):
        request(
            "finmind",
            dataset="TaiwanStockPrice",
            params={"data_id": "2330", "start_date": "2026-02-01", "end_date": "2026-01-01"},
            transport=lambda *args: (200, b"{}"),
        )
    with pytest.raises(HttpError):
        request("twse", transport=lambda *args: (302, b"redirect"))
