# Dataset 本機盤點與實測：操作與本次結果

這是開發／診斷工具，不是正式 Dataset CLI、公開 API、資料發布格式或全量調價服務。需求與接受流程見[需求](dataset-local-validation-requirements.md)及[設計／驗收紀錄](dataset-local-validation-design.md)。

## 1. 工具與安全邊界

從 repository 根目錄使用 Python 3.13 鎖定環境執行 `scripts/dataset_local_validation.py`。它直接使用此工作區 `src/` 中的核心；不是驗證另一個已安裝版本。正式 `quant-data` 命令未增加 inventory／verify。

- production DB／raw／分類輸入只讀；不下載、不回補、不啟用 timer、不修改來源 schema／標記／索引，不刪 WAL／SHM。
- SQLite 使用 `mode=ro` 及讀取交易，允許正常 WAL／SHM 管理；不使用 immutable。
- 工具輸出 JSON 至 stdout；自行保存時須使用新的本機證據檔案，不要覆寫來源檔案。以下範例只讀來源，不寫 production；shell 重新導向會寫操作者指定的輸出檔。
- 不存在的來源 DB 不會被建立。發現錯誤應先檢查 stderr／退出碼，不要把沒有成功輸出的檔案當成通過證據。
- 詳細輸出含市場資料、來源參照及可能的本機路徑，應當作本機診斷資料管理，不整批加入版控。

## 2. 局部選案例（discover）

```sh
uv run --locked python scripts/dataset_local_validation.py \
  --root ~/.local/share/quant-research/taiwan \
  discover --start 2022-09-01 --end 2022-10-31 --max-candidates 2
```

起訖日期必填、最多62個曆日。事件候選每市場／事件種類各有上限；行情樣本每市場最多 `min(max-candidates, 10)`，不會先用行情耗光事件候選配額。工具仍需讀取 task metadata 及候選 scope，這不是全量 records 統計。

檢查候選事件的摘要與必要明細，以及事件前後有無行情，才決定實測窗口。停牌可能使第一筆行情已是恢復交易日；這種窗口可以驗證首日不重複套用，但不能宣稱驗證到事件前後倍率切換。TPEx 詳細條件是摘要內嵌值，沒有外部detail link不等於缺資料。

## 3. 單一案例實測（verify）

```sh
uv run --locked python scripts/dataset_local_validation.py \
  --root ~/.local/share/quant-research/taiwan \
  verify --market twse --symbol 2368 \
  --start 2022-09-01 --end 2022-09-30
```

一次一個來源市場＋代碼，最多62個曆日。工具比較：

1. 從來源 payload 獨立解析的原始行情、事件／精確連結明細、分類依據及來源參照。
2. 以獨立 Decimal 邏輯算出的倍率／OHLC，對照實際 `iter_series` 結果。
3. 原始量／金額／缺值／來源參照保留，以及非普通股／unknown 不產生調整列。

金融計算不呼叫原調價函式取得答案；來源版本選擇及分類器仍共用並在輸出揭露，不構成獨立來源認證。分類 manifest 未指定時沿用有限規則；目前開發命令不提供新增證據的下載或自動搜尋。

輸出解讀：

- `passed: true`：本次案例實際執行的比較沒有失敗，不表示所有金融情境已涵蓋。
- `checks`：各項 expected／actual 或狀態；`unverified` 列出沒有真實例子的行為。
- 非普通股／unknown 的 `rows[].actual` 為 null；`expected` 中的算術值只供核對邏輯參考，不表示應發布該商品的調整價。
- 財務數值以十進位字串記錄。`0`、`0E-8`、`0.00000000` 可是同一金融值，應依 Decimal 語意比較，不以字串格式認定不同。
- 執行期間來源有 commit，`source_unchanged` 會失敗，不能將不同來源時點的結果當成同一案例接受。
- 比較失敗退出1；輸入／來源／解析錯誤退出2，錯誤訊息在stderr。需修核心或擴大案例時先確認範圍，不自動修production或下載。

## 4. 全量盤點（inventory）

**先接受小範圍實測結果，再使用此操作。**

```sh
uv run --locked python scripts/dataset_local_validation.py \
  --root ~/.local/share/quant-research/taiwan inventory
```

沒有日期參數，盤點指定根目錄全部已保存的官方命名空間資料，不執行全量 `iter_series`。此工具只處理操作者指定的可信本機DB，不是未信任上傳／多租戶服務；snapshot metadata、事件去重、診斷及輸出會隨全庫規模成長，沒有固定記憶體／輸出上限，執行前須評估資源。可能產生較大磁碟 I/O；本次約26.4GB DB的執行時間為142秒，不能據此保證其他主機或未來資料量的耗時。

- `historical_saved`：包含多版本與各驗證狀態的保存量；不是唯一有效行情／事件數。
- `latest`：沿用核心選擇的最新行情／事件摘要scope及其可用性，列出snapshot列數、請求區間、實際record_date及下載時間。不可把有資料的日期端點當作中間每天／每檔完整。
- `unique_latest_event_identities`：以市場、代碼、事件種類、生效日去重的最新事件數，不是全量金融條件解析成功數。
- `latest_detail_tasks`：最新明細任務狀態與其snapshot列數；不等於精確連結後可供Dataset使用的明細數。
- `required_twse_summary_detail_links`：對去重後上市股利／減資事件檢查必要連結與首筆明細，列出不可用原因；TPEx inline與上市面額不要求這種外部明細。
- `malformed_event_rows`：識別／schema診斷失敗的摘要，獨立列出，不算進有效事件身分數。

盤點讀取交易固定一致視圖，但它是診斷報告，不宣稱全量金融正規化或計算驗證通過。退出0表示盤點流程成功，仍可能報出不可用資料。

## 5. 本次實際結果（2026-10-30）

### 小範圍實測

已接受7案例、67筆原始行情、63筆調整行情；4筆ETF只保留原價。案例與具體數值見[設計 §8](dataset-local-validation-design.md#8-執行紀錄)。已核對兩市場減資、上櫃面額直接換股、兩市場現金股利不調價、退還款不扣價、真實缺價、ETF排除及首日換股不重複套用。

未取得本次真實案例的情境包括非零股票股利、unknown分類、上市面額價格比及換股日無行情而窗口有前後行情；沒有將人工fixture通過冒充這些真實情境已驗證。

### 全量盤點

| 類別 | 本次筆數 | 意義 |
|---|---:|---|
| TWSE行情 | 2,531,265 | 最新可用snapshot的行情列，含各種證券，不是普通股專屬數量 |
| TPEx行情 | 17,563,927 | 同上；不把代碼／市場／不同商品混成普通股資料集 |
| 官方行情合計 | **20,095,192** | 不是全量調價驗證筆數 |
| 唯一最新公司事件 | **18,832** | 身分去重後摘要事件；未對全部金融值執行Dataset計算 |
| 已保存官方snapshot | 16,912 | 包含歷史版本及空回應 |
| 已保存官方record列 | 20,123,831 | 含行情、摘要與明細，不等於唯一市場事件數 |

行情實際保存日期端點為2018-01-02至2026-10-22。兩市場各3,217個最新行情scope中，各2,142為valid、1,075為empty_unverified；不把成功空回應說成完整交易日曆或資料不存在。最新行情／摘要scope共7,070個，核心scope判定不可用為0；此判定不涵蓋全部必要明細及金融條件。

上市必要明細連結：9,822筆需要、9,807筆可用、15筆不可用、0筆連結缺少。主助手逐筆查DB確認15筆都是 `completed` 任務對應零record的 `empty_unverified` 回應；不是明細檔案被刪，也沒有改標記或補抓。

| 代碼 | 不可用明細對應事件日 |
|---|---|
| 2887F | 2019-07-09、2020-07-02、2021-08-09、2022-07-05、2023-07-05、2024-07-02、2025-07-08、2026-07-01 |
| 2887Z1 | 2023-07-05、2024-07-02、2025-07-08、2026-07-01 |
| 2887G／2887H／2887I | 各2026-07-01 |

這些帶尾碼證券不應當成四碼普通股。盤點仍如實列出官方事件的必要明細狀態，不據此擴充或改變既有分類／核心處理政策。本次未判定需要重新下載，更未執行任何補抓。

股利明細9,589列、減資明細218列的record_date為null；事件日期由摘要及精確連結提供，不能當作行情歷史缺漏。

### 測試與本機證據

本次新增34項測試，全專案 **390 passed**。Ruff、mypy、鎖定環境同步、sdist／wheel建置皆通過。測試有注入錯誤來證明verifier會拒絕錯誤結果，不只測正常案例。

完整本機證據目錄：`artifacts/dataset-local-validation-20261030T123825Z-27992/`。其中包括discover、7組verify逐列JSON、inventory JSON與全專案檢查log。此目錄不加入版控；被忽略不等於已有備份或保留政策。

**結論：完成小範圍計算實測及全部已保存官方資料盤點；未執行全量調價驗證、不宣稱完整普通股歷史／PIT／交易所認證價格／已驗證回測。**
