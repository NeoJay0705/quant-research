"""Offline regression coverage for official event contracts."""

from __future__ import annotations

import json

import pytest

from quant_research.taiwan import official, sources


def batch(source: str, dataset: str, payload: dict[str, object]) -> sources.RawBatch:
    return sources.RawBatch(
        source,
        dataset,
        "offline",
        "",
        200,
        json.dumps(payload, ensure_ascii=False).encode(),
        "",
        0,
    )


def parse(
    source: str,
    dataset: str,
    payload: dict[str, object],
    start: str = "2018-01-01",
    end: str = "2018-12-31",
):
    return official.parse(batch(source, dataset, payload), {"start_date": start, "end_date": end})


@pytest.mark.parametrize("dataset", ["TWT49U", "TWTAUU"])
def test_twse_unverified_empty_period_envelope_exact_contract(dataset: str):
    payload = {"stat": "很抱歉，沒有符合條件的資料!"}
    params = {"end_date": "2018-01-03", "start_date": "2018-01-02"}
    result = official.parse(batch("twse", dataset, payload), params)
    assert result.records == ()
    assert result.coverage_state == "empty_unverified"
    assert result.actual_date is None
    assert result.warnings == ("period_envelope_unverified", "empty_unverified")

    for invalid in (
        {"stat": "很抱歉，沒有符合條件的資料!", "data": []},
        {"stat": "很抱歉，沒有符合條件的資料!", "extra": True},
        {"stat": "很抱歉，沒有符合條件的資料"},
    ):
        with pytest.raises(sources.ContractError):
            official.parse(batch("twse", dataset, invalid), params)
    for invalid_params in (
        {"start_date": "2018-02-30", "end_date": "2018-03-01"},
        {"start_date": "2018-01-04", "end_date": "2018-01-03"},
        {"start_date": "2018-01-02", "end_date": "2018-01-03", "extra": "x"},
    ):
        with pytest.raises(sources.ContractError, match="invalid event request parameters"):
            official.parse(batch("twse", dataset, payload), invalid_params)


def test_empty_period_envelope_scope_and_refusal_precedence():
    exact = {"stat": "很抱歉，沒有符合條件的資料!"}
    params = {"start_date": "2018-01-02", "end_date": "2018-01-03"}
    for source, dataset in (("tpex", "revivt"), ("twse", "TWTB8U")):
        with pytest.raises(sources.ContractError):
            official.parse(batch(source, dataset, exact), params)
    for stat, error in (("403", sources.AccessBlockedError), ("429", sources.QuotaError)):
        with pytest.raises(error):
            official.parse(batch("twse", "TWT49U", {"stat": stat}), params)


def test_twt49u_detail_exact_empty_envelope_is_unverified_and_scoped():
    body = b'{"stat":"\xe7\x84\xa1\xe7\x9b\xb8\xe9\x97\x9c\xe8\xb3\x87\xe6\x96\x99"}'
    assert len(body) == 26
    raw = sources.RawBatch("twse", "TWT49UDetail", "offline", "", 200, body, "", 0)
    params = {"symbol": "2887F", "date": "2019-07-09"}
    result = official.parse(raw, params)
    assert result.records == ()
    assert result.detail_tasks == ()
    assert result.coverage_state == "empty_unverified"
    assert result.actual_date is None
    assert result.warnings == ("period_envelope_unverified", "empty_unverified")

    revised_body = '{"stat":"查無相關資料"}'.encode()
    revised_raw = sources.RawBatch("twse", "TWT49UDetail", "offline", "", 200, revised_body, "", 0)
    revised = official.parse(revised_raw, {"symbol": "2887Z1", "date": "2023-07-05"})
    assert revised.coverage_state == "empty_unverified"
    assert revised.warnings == ("period_envelope_unverified", "empty_unverified")

    for source, dataset, payload in (
        ("tpex", "TWT49UDetail", {"stat": "無相關資料"}),
        ("twse", "TWTAVUDetail", {"stat": "無相關資料"}),
        ("twse", "TWT49U", {"stat": "無相關資料"}),
        ("twse", "TWT49UDetail", {"stat": "無相關資料", "data": []}),
        ("twse", "TWT49UDetail", {"stat": "無相關資料", "extra": True}),
        ("twse", "TWT49UDetail", {"stat": "無相關資料", "data": [["2887F"]]}),
        ("twse", "TWT49UDetail", {"stat": "無相關資料 "}),
        ("twse", "TWT49UDetail", {"stat": "查無相關資料", "extra": True}),
        ("twse", "TWT49UDetail", {"stat": "查無相關資料 "}),
    ):
        with pytest.raises(sources.ContractError):
            official.parse(batch(source, dataset, payload), params)

    for invalid_params in (
        {},
        {"symbol": "", "date": "2019-07-09"},
        {"symbol": "2887F", "date": "2019-02-30"},
        {"symbol": "2887F", "date": "2019-07-09", "extra": "x"},
        None,
    ):
        with pytest.raises(sources.ContractError, match="invalid event request parameters"):
            official.parse(raw, invalid_params)
    for stat, error in (("403", sources.AccessBlockedError), ("429", sources.QuotaError)):
        refused = batch("twse", "TWT49UDetail", {"stat": stat})
        with pytest.raises(error):
            official.parse(refused, {})


def test_date_formats_and_official_event_contracts():
    assert official._date("107年01月11日") == "2018-01-11"
    assert official._date("1110110") == "2022-01-10"
    assert official._date("20180111") == "2018-01-11"

    summaries = [
        (
            "twse",
            "TWT49U",
            ["資料日期", "股票代號", "除權息前收盤價", "除權息參考價", "詳細資料"],
            ["2018-01-11", "2330", "100", "92", "2330,20180111"],
        ),
        (
            "twse",
            "TWTAUU",
            ["恢復買賣日期", "股票代號", "停止買賣前收盤價格", "恢復買賣參考價", "詳細資料"],
            ["2018-01-11", "2330", "10", "12", "2330,20180102"],
        ),
        (
            "twse",
            "TWTB8U",
            ["恢復買賣日期", "股票代號", "停止買賣前收盤價格", "恢復買賣參考價", "詳細資料"],
            ["2018-01-11", "2330", "10", "12", "2330,20180102,20180111"],
        ),
        (
            "tpex",
            "exDailyQ",
            ["除權息日期", "代號", "除權息前收盤價", "除權息參考價", "現金股利", "每仟股無償配股"],
            ["107/01/11", "2330", "100", "92", "8", "125"],
        ),
        (
            "tpex",
            "revivt",
            ["恢復買賣日期", "股票代號", "最後交易日之收盤價格", "減資恢復買賣開始日參考價格"],
            ["107/01/11", "2330", "10", "12"],
        ),
        (
            "tpex",
            "pvChgRslt",
            ["恢復買賣日期", "證券代號", "最後交易日之收盤價格", "恢復買賣開始參考價"],
            ["107/01/11", "2330", "10", "12"],
        ),
    ]
    for source, dataset, fields, row in summaries:
        # Give each summary a matching January-2018 period envelope.
        payload: dict[str, object] = {
            "stat": "OK",
            "fields": fields,
            "data": [row],
            "strDate": "107年01月01日",
            "endDate": "107年12月31日",
        }
        if source == "tpex":
            payload = {"stat": "ok", "fields": fields, "data": [row], "date": "107/01/01~107/12/31"}
        result = parse(source, dataset, payload)
        record = result.records[0]
        expected_kind = (
            "par_value"
            if dataset in {"TWTB8U", "pvChgRslt"}
            else "capital_reduction"
            if dataset in {"TWTAUU", "revivt"}
            else "dividend"
        )
        assert record["event_type"] == expected_kind
        assert record["symbol"] == "2330"
        if dataset == "TWTB8U":
            assert record["detail_params"] == {
                "symbol": "2330",
                "stop_date": "2018-01-02",
                "resume_date": "2018-01-11",
            }
            assert result.detail_tasks == ()
        if dataset == "exDailyQ":
            assert record["cash_dividend_per_share"] == "8"
            assert record["bonus_share_ratio"] == "0.125"
            assert record["share_multiplier"] == "1.125"
        if dataset != "pvChgRslt":
            assert record["before_price"] == ("100" if dataset in {"TWT49U", "exDailyQ"} else "10")
        expected_reference = "92" if dataset in {"TWT49U", "exDailyQ"} else "12"
        assert record["reference_price"] == expected_reference
        assert record["reference_price_kind"] == (
            "除權息參考價"
            if dataset in {"TWT49U", "exDailyQ"}
            else "恢復買賣參考價"
            if dataset in {"TWTAUU", "TWTB8U"}
            else "恢復買賣開始參考價"
            if dataset == "pvChgRslt"
            else "減資恢復買賣開始日參考價格"
        )

    with pytest.raises(sources.ContractError, match="unknown event schema"):
        parse(
            "tpex",
            "pvChgRslt",
            {
                "stat": "OK",
                "fields": ["恢復買賣日期", "股票代號"],
                "data": [["107/01/11", "2330"]],
                "date": "107/01/01~107/12/31",
            },
        )


def test_event_period_date_symbol_and_unknown_numeric_contracts():
    p = {"start_date": "2018-01-01", "end_date": "2018-01-31"}
    mismatched = {
        "stat": "OK",
        "strDate": "20180102",
        "endDate": "20180131",
        "fields": ["除權息日期", "代號"],
        "data": [],
    }
    with pytest.raises(sources.ContractError, match="period envelope mismatch"):
        official.parse(batch("tpex", "exDailyQ", mismatched), p)
    assert official._detail_fields({"現金增資股數": "NA"}).get("quality_flags") == [
        "source_placeholder"
    ]
    assert (
        "cash_capital_increase_present"
        not in official._detail_fields({"現金增資股數": "0 股"})["quality_flags"]
    )
    assert official._detail_fields({"現金增資股數": "5 股"})["quality_flags"] == [
        "cash_capital_increase_present"
    ]
    with pytest.raises(sources.ContractError, match="outside query interval"):
        parse(
            "twse",
            "TWT49U",
            {
                "stat": "OK",
                "fields": ["資料日期", "股票代號", "詳細資料"],
                "data": [["2019-01-01", "2330", "2330,20190101"]],
                "strDate": "2018-01-01",
                "endDate": "2018-12-31",
            },
        )
    with pytest.raises(sources.ContractError, match="outside query interval"):
        official.parse(
            batch(
                "twse",
                "TWT49U",
                {
                    "stat": "OK",
                    "fields": ["資料日期", "股票代號", "詳細資料"],
                    "data": [["2018-02-01", "2330", "2330,20180201"]],
                    "strDate": "2018-01-01",
                    "endDate": "2018-01-31",
                },
            ),
            p,
        )


def test_four_detail_shapes_and_price_only_change_detail():
    cases = [
        (
            "TWT49UDetail",
            ["股票代號", "股票名稱", "(每股配發現金股利)除息"],
            ["2330", "台積電", "8 元／股"],
            {"cash_dividend_per_share": "8"},
        ),
        (
            "TWTAVUDetail",
            ["股票代號", "股票名稱", "每壹仟股換發新股票", "變更前面額", "變更後面額"],
            ["6415", "矽力", "4000 股", "10 元/股", "2.5 元/股"],
            {"share_multiplier": "4", "par_before": "10", "par_after": "2.5"},
        ),
        (
            "TWTB8UDetail",
            ["股票代號", "停止買賣前收盤價格", "恢復買賣參考價"],
            ["2330", "100", "90"],
            {"before_price": "100", "reference_price": "90"},
        ),
        (
            "TWTB7UDetail",
            ["股票代號", "每股換發新股票"],
            ["6613", "0.4"],
            {"share_multiplier": "0.4"},
        ),
    ]
    for dataset, fields, row, expected in cases:
        symbol = row[0]
        result = official.parse(
            batch("twse", dataset, {"stat": "OK", "fields": fields, "data": [row]}),
            {
                "symbol": symbol,
                "date": "2018-01-01",
                "file_date": "2018-01-01",
                "stop_date": "2018-01-01",
                "resume_date": "2018-01-02",
            },
        )
        record = result.records[0]
        for key, value in expected.items():
            assert record[key] == value
        if dataset == "TWTB8UDetail":
            assert record["event_type"] == "par_value"
            assert record["observation_type"] == "prices"
    payload = {"stat": "OK", "fields": ["股票代號", "每股換發新股票"], "data": [["6613", "0.4"]]}
    with pytest.raises(sources.ContractError, match="detail symbol mismatch"):
        official.parse(
            batch("twse", "TWTB7UDetail", payload), {"symbol": "0001", "file_date": "2018-01-01"}
        )


def test_synthetic_official_event_regressions():
    # Keep event-pipeline coverage independent of workstation evidence files.
    reduction = official.parse(
        batch(
            "twse",
            "TWTAVUDetail",
            {
                "stat": "OK",
                "fields": ["股票代號", "每壹仟股換發新股票", "每股退還股款"],
                "data": [["6207", "950 股", "0.5 元/股"]],
            },
        ),
        {"symbol": "6207", "file_date": "2022-01-01"},
    )
    assert reduction.records[0]["share_multiplier"] == "0.95"
    assert reduction.records[0]["cash_return_per_share"] == "0.5"

    twse_reduction = official.parse(
        batch(
            "twse",
            "TWTAVUDetail",
            {
                "stat": "OK",
                "fields": ["股票代號", "每壹仟股換發新股票", "每股退還股款"],
                "data": [["2409", "800 股", "2 元/股"]],
            },
        ),
        {"symbol": "2409", "file_date": "2022-09-28"},
    )
    assert twse_reduction.records[0]["share_multiplier"] == "0.8"
    assert twse_reduction.records[0]["cash_return_per_share"] == "2"

    details = [
        ("6613", "2.00000000", "10.00", "5.00"),
        ("6548", "2.50000000", "1.00", "0.40"),
        ("5536", "2.00000000", "10.00", "5.00"),
        ("3093", "4.00000000", "10.00", "2.50"),
    ]
    for symbol, multiplier, before, after in details:
        result = official.parse(
            batch(
                "tpex",
                "pvChgRslt",
                {
                    "stat": "ok",
                    "fields": [
                        "恢復買賣日期",
                        "證券代號",
                        "最後交易日之收盤價格",
                        "恢復買賣開始參考價",
                        "詳細資料",
                    ],
                    "data": [
                        [
                            "111/01/01",
                            symbol,
                            "10",
                            "5",
                            f"<table><tr><td>股票代號/股票名稱</td><td>{symbol}/測試</td><td>每股換發新股票</td><td>{multiplier}</td><td>變更前面額</td><td>{before}</td><td>變更後面額</td><td>{after}</td></tr></table>",
                        ]
                    ],
                    "date": "111/01/01~111/12/31",
                },
            ),
            {"start_date": "2022-01-01", "end_date": "2022-12-31"},
        )
        assert result.records[0]["share_multiplier"] == multiplier
        assert (result.records[0]["par_before"], result.records[0]["par_after"]) == (before, after)

    four_for_one = official.parse(
        batch(
            "twse",
            "TWTB7UDetail",
            {
                "stat": "OK",
                "fields": ["股票代號", "每股換發新股票", "變更前面額", "變更後面額"],
                "data": [["6415", "4 股", "10 元/股", "2.5 元/股"]],
            },
        ),
        {"symbol": "6415", "file_date": "2022-07-05"},
    )
    assert four_for_one.records[0]["share_multiplier"] == "4"
    assert (four_for_one.records[0]["par_before"], four_for_one.records[0]["par_after"]) == (
        "10",
        "2.5",
    )


def test_event_contract_rejection_cases():
    summary = {
        "stat": "OK",
        "fields": ["資料日期", "股票代號", "詳細資料"],
        "data": [["2018-01-11", "2330", "2330,20180111"]],
        "strDate": "2018-01-01",
        "endDate": "2018-01-31",
    }
    for token in ("2317,20180111", "2330,20180112"):
        malformed = {**summary, "data": [["2018-01-11", "2330", token]]}
        with pytest.raises(sources.ContractError, match="detail token mismatch"):
            parse("twse", "TWT49U", malformed, "2018-01-01", "2018-01-31")

    wrong_html = {
        "stat": "OK",
        "fields": ["股票代號", "詳細資料"],
        "data": [
            [
                "2330",
                "<table><tr><td>股票代號/股票名稱</td><td>2317/錯誤</td><td>現金股利</td><td>1</td></tr></table>",
            ]
        ],
    }
    with pytest.raises(sources.ContractError, match="HTML detail symbol mismatch"):
        official.parse(
            batch("twse", "TWT49UDetail", wrong_html), {"symbol": "2330", "date": "2018-01-11"}
        )

    bad_count = {
        "stat": "OK",
        "fields": ["資料日期", "股票代號", "詳細資料"],
        "data": [["2018-01-11", "2330", "2330,20180111"]],
        "strDate": "2018-01-01",
        "endDate": "2018-01-31",
        "totalCount": 2,
    }
    with pytest.raises(sources.ContractError, match="totalCount mismatch"):
        parse("twse", "TWT49U", bad_count, "2018-01-01", "2018-01-31")

    missing_multiplier = official.parse(
        batch("twse", "TWTB7UDetail", {"stat": "OK", "fields": ["股票代號"], "data": [["6415"]]}),
        {"symbol": "6415", "file_date": "2022-07-05"},
    ).records[0]
    assert missing_multiplier["share_multiplier"] is None
    assert "share_multiplier" in missing_multiplier["missing_fields"]


def test_twt49u_bonus_a_label_is_per_thousand_and_multiplier_is_derived():
    zero = official._detail_fields(
        {
            "A. 按普通股股東持股比例每千股無償配股": "0",
            "B. 員工紅利轉增資": "0",
            "C. (有償) 現金增資": "0",
        },
        event_type="dividend",
    )
    assert zero["bonus_share_ratio"] == "0"
    assert zero["share_multiplier"] == "1"
    assert "employee_bonus_shares_present" not in zero["quality_flags"]
    assert "cash_capital_increase_present" not in zero["quality_flags"]

    positive = official._detail_fields(
        {
            "A. 按普通股股東持股比例每千股無償配股": "50.19058194",
        },
        event_type="dividend",
    )
    assert positive["bonus_share_ratio"] == "0.05019058194"
    assert positive["share_multiplier"] == "1.05019058194"

    reduction = official._detail_fields(
        {
            "每壹仟股換發新股票": "800 股",
            "A. 按普通股股東持股比例每千股無償配股": "0",
        },
        event_type="capital_reduction",
    )
    assert reduction["share_multiplier"] == "0.8"

    dividend_conflict = official._detail_fields(
        {
            "每股換發新股票": "1.3",
            "A. 按普通股股東持股比例每千股無償配股": "200",
        },
        event_type="dividend",
    )
    assert dividend_conflict["share_multiplier"] is None
    assert dividend_conflict["field_conflicts"]["share_multiplier"] == ["1.3", "1.2"]

    existing_conflict = official._detail_fields(
        {
            "每壹仟股換發新股票": "800 股",
            "每股換發新股票": "0.95",
            "A. 按普通股股東持股比例每千股無償配股": "0",
        },
        event_type="dividend",
    )
    assert existing_conflict["share_multiplier"] is None
    assert existing_conflict["field_conflicts"]["share_multiplier"] == ["0.8", "0.95"]


def test_detail_alias_candidates_preserve_conflicts_and_decimal_equivalence():
    conflict = official._detail_fields({"現金股利": "8", "(每股配發現金股利)除息": "9"})
    assert conflict["cash_dividend_per_share"] is None
    assert conflict["field_conflicts"]["cash_dividend_per_share"] == ["8", "9"]
    same = official._detail_fields({"現金股利": "8", "(每股配發現金股利)除息": "8.00"})
    assert same["cash_dividend_per_share"] == "8"
    assert "field_conflicts" not in same

    equal_multiplier = official._detail_fields(
        {"每壹仟股換發新股票": "800 股", "每股換發新股票": "0.8"}
    )
    assert equal_multiplier["share_multiplier"] == "0.8"
    conflict_multiplier = official._detail_fields(
        {"每壹仟股換發新股票": "800 股", "每股換發新股票": "0.95"}
    )
    assert conflict_multiplier["share_multiplier"] is None
    assert conflict_multiplier["field_conflicts"]["share_multiplier"] == ["0.8", "0.95"]


def test_summary_and_html_cash_conflict_and_source_input_is_unchanged():
    payload = {
        "stat": "ok",
        "date": "107/01/01~107/12/31",
        "fields": ["除權息日期", "代號", "現金股利", "詳細資料"],
        "data": [
            [
                "107/01/11",
                "2330",
                "8",
                "<table><tr><td>股票代號/股票名稱</td><td>2330/台積電</td><td>(每股配發現金股利)除息</td><td>9</td></tr></table>",
            ]
        ],
    }
    original = json.loads(json.dumps(payload))
    parsed = official.parse(
        batch("tpex", "exDailyQ", payload), {"start_date": "2018-01-01", "end_date": "2018-12-31"}
    )
    assert payload == original
    record = parsed.records[0]
    assert record["cash_dividend_per_share"] is None
    assert record["field_conflicts"]["cash_dividend_per_share"] == ["8", "9"]


def test_summary_cash_aliases_conflict_and_html_na_preserves_valid_summary():
    payload = {
        "stat": "ok",
        "date": "107/01/01~107/12/31",
        "fields": ["除權息日期", "代號", "現金股利", "(每股配發現金股利)除息", "詳細資料"],
        "data": [
            [
                "107/01/11",
                "2330",
                "8",
                "9",
                "<table><tr><td>股票代號/股票名稱</td><td>2330/台積電</td><td>現金股利</td><td>NA</td></tr></table>",
            ]
        ],
    }
    record = official.parse(
        batch("tpex", "exDailyQ", payload),
        {"start_date": "2018-01-01", "end_date": "2018-12-31"},
    ).records[0]
    assert record["cash_dividend_per_share"] is None
    assert record["field_conflicts"]["cash_dividend_per_share"] == ["8", "9"]

    payload["fields"] = ["除權息日期", "代號", "現金股利", "詳細資料"]
    payload["data"] = [
        [
            "107/01/11",
            "2330",
            "8",
            "<table><tr><td>股票代號/股票名稱</td><td>2330/台積電</td><td>現金股利</td><td>NA</td></tr></table>",
        ]
    ]
    record = official.parse(
        batch("tpex", "exDailyQ", payload),
        {"start_date": "2018-01-01", "end_date": "2018-12-31"},
    ).records[0]
    assert record["cash_dividend_per_share"] == "8"
    assert "field_conflicts" not in record


def test_summary_values_survive_detail_na_and_flags_read_summary_columns():
    payload = {
        "stat": "ok",
        "date": "107/01/01~107/12/31",
        "fields": [
            "除權息日期",
            "代號",
            "現金股利",
            "現金增資股數",
            "B. 員工紅利轉增資",
            "詳細資料",
        ],
        "data": [
            [
                "107/01/11",
                "2330",
                "8",
                "5000000",
                "2",
                "<table><tr><td>股票代號/股票名稱</td><td>2330/台積電</td><td>現金股利</td><td>NA</td></tr></table>",
            ]
        ],
    }
    record = official.parse(
        batch("tpex", "exDailyQ", payload), {"start_date": "2018-01-01", "end_date": "2018-12-31"}
    ).records[0]
    assert record["cash_dividend_per_share"] == "8"
    assert "cash_capital_increase_present" in record["quality_flags"]
    assert "employee_bonus_shares_present" in record["quality_flags"]
