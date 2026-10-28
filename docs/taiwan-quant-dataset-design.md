# 台股量化計算資料層設計（Taiwan Quant Dataset）

## 1. 狀態與範圍

本文件依[修訂需求](taiwan-quant-dataset-requirements.md)記錄使用者已確認的最小設計，取代舊版以指定基準日調整歷史價格、發布版本化 bundle 的設計。使用者並已確認：不需要但已實作的功能須刪除，不只停用。修訂核心、刪除項目及本次有界驗收已完成，實際證據見 §9；舊測試與舊有界驗收不作本次符合性證據。

這次交付內部可驗證的來源適配、分類、事件整理與順序計算核心。不新增穩定對外 Python API、匯出或資料格式；存取方式待使用情境確認。程式內部函式與測試資料結構不構成對外格式契約。

官方下載器、來源 DB／raw、既有事件整理報告、排程與分類證據原檔不變。不 HTTP、不回補、不 migration／relabel、不清理來源資料。不宣稱官方認證調整價、完整歷史或含息報酬。

## 2. 處理流程

```text
既有 state.sqlite3（唯讀交易）
  -> 選取實際保存的官方行情及事件
  -> 每個查詢取最新結果；同一事件不重複使用
  -> 商品類型分類、摘要與精確連結明細組合
  -> 每個來源市場＋代碼按日期升序處理
  -> 換股生效時更新累積倍率
  -> 原始行情、調整值與分配資料供內部流程使用
```

來源資料由現有下載流程解析；新核心不改下載時的金額預設或來源驗證狀態，只在自身使用資料時套用本需求的業務規則。

## 3. 來源選取及重複結果

- 使用 `MI_INDEX`、`dailyQuotes` 行情；事件摘要使用 TWSE `TWT49U/TWTAUU/TWTB8U`、TPEx `exDailyQ/revivt/pvChgRslt`，不混入 legacy FinMind。
- 在來源 DB 以 `mode=ro`、讀取交易取得一致視圖；不使用 `immutable=1`，不修改 WAL／SHM 或新增來源索引。
- 每個相同查詢取最新 task edition；最新一次失敗、pending 或來源資料不能使用時直接報錯，不回退較舊成功結果。
- 成功的空事件結果表示沒有事件列，不建立 coverage／完整性報告，也不改來源的 `empty_unverified` 標記。解析失敗或 API 錯誤不是成功空結果。
- 重疊查詢包含同一來源、代碼、種類、生效日的同一事件時，取最新回應及其已綁定明細，不重複計算，也不從不同快照拼湊金融值。時間相同時用固定的 task/record 識別順序決定，不另設版本選擇功能。
- 保留足以定位採用值的 source/dataset/record/snapshot 參照，不建立 lineage 認證、fingerprint 或品質 manifest。
- 批次讀取及按股處理，必要時使用獨立臨時 SQLite 工作區排序；不把全市場全部 records 載入 RAM。不發布額外資料集或留下版本化 bundle。

## 4. 分類及事件整理

### 4.1 商品類型

沿用現有分類器與明確指定的本機分類證據解析，不新增分類下載來源。保留普通股、非普通股、unknown 及分類依據；unknown 指商品類型，不是市場未知。不產生整批缺證據報告，不把 unknown 當成普通股，也不讓某股的分類 unknown 使其他股票失敗。

行情只保留來源市場、代碼、日期，不做轉上市辨識、跨市場歷史整合或公司沿革串接。

### 4.2 摘要、明細及分配

| 事件來源 | 使用方式 |
|---|---|
| 上市除權息 | 摘要及既有精確連結的 `TWT49UDetail`，保留現金股利、無償配股條件 |
| 上市減資 | 摘要及既有精確連結的 `TWTAVUDetail`，取得換發股數、退還款及併案股利 |
| 上市面額變更 | 摘要價格即可；不要求額外明細下載 |
| 上櫃三類事件 | 使用同份回應及已解析的內嵌詳細資料，不虛構外部明細請求 |

事件日期取來源的生效／恢復交易日；明細依摘要連結組合，不以明細下載日或申報日代替事件日期。

現金股利、股票股利／無償配股條件、退還股款分別保存；依來源保留原股口徑及適用日期，不推定發款日。可選現金金額為 null／來源契約中的缺值時，按零處理；不是把所有缺欄或 malformed 值都轉成零。配股率、換股率、必要價格不套用此預設。

新事件整理器不經過共用 `build_corporate_actions()` 的 complete/reference-match／compound 等舊計算資格判定。該共用函式及既有報告語意不變。

## 5. 順序價格算法

每個來源市場＋代碼獨立維護倍率。使用範圍內第一筆行情日期為 `d0`，初始 `C=1`，首筆價格不變，不另輸入基準日。

### 5.1 單一事件倍率

- 減資：直接換股率 `m=換發後股數/原股數`。每千股換發 `N` 股時 `m=N/1000`；不從含現金分配效果的價格比反推。
- 面額變更：同一回應已有直接換股率時優先使用 `m`；否則使用摘要 `P/Q`，`P` 是事件前價格，`Q` 是恢復買賣參考價。價格比只表示公布價格推得的倍率，不宣稱精確換股率。
- 現金股利、股票股利／無償配股、退還股款不更新 `C`，也不扣入價格。

### 5.2 累乘及日期

```text
C = 1
依日期升序：
    對 d0 < 生效日 <= 當日日期、尚未套用的每個換股事件：
        C = C × 事件倍率
    該日調整後 OHLC = 原始 OHLC × C
```

事件生效日有行情，先更新倍率再計算該日；無行情時不補價，倍率留給後續行情。首筆行情當日或之前已反映的事件不追溯累計。四個 OHLC 使用相同倍率，volume/amount 不變；來源行情缺價不補造價格。

不讀取輸出結束日期後的事件改價，不回改先前行情；加入較晚日期資料不影響既有較早結果。這是使用範圍起始股數尺度的順序換算，不是把歷史價格換到指定未來基準日。

## 6. 金融數值與錯誤

- 使用 `Decimal`，獨立 50 位有效數字、`ROUND_HALF_EVEN` 計算環境；不受呼叫者的 precision/traps/flags 影響。
- 原始金融值不量化；計算按上述有效位精度捨入。不沿用固定小數點後 12 位的舊調整價政策，對外顯示精度不在本次決定。
- 換股倍率與用於除法的價格必須有限且大於零；現金金額依來源契約解析，缺省才為零。
- 已選必要資料缺失、無法解析／計算、來源錯誤或格式改變時，明確失敗並指出來源、代碼、日期、欄位及原因；不猜值，不把失敗當零或無事件。
- 不因併案、現金增資或員工增資等名稱就拒絕換股率；按已確認的來源條件使用，不擴充未要求的事件模型。
- 不建立 incomplete/blocked 區間、coverage、財務衝突分類或研究就緒狀態系統。

## 7. 程式範圍及刪除項目

保留並簡化 `dataset/source.py`、`models.py`、`quotes.py`、`events.py`、`arithmetic.py`、`factors.py`、`financial.py`、`build.py` 作為內部核心；分類器原則上沿用。

刪除已退出需求的 `dataset/coverage.py`、`bundle.py`、`reader.py`、`export.py`、其專屬測試及舊 bundle／品質狀態相依程式。刪除新增的 `build-dataset`／`export-dataset` CLI 入口與穩定對外 Python reader/export 匯出；保留既有官方下載及唯讀報告 CLI。核心測試直接呼叫內部流程，不新增正式使用介面或發布格式。

`__init__.py` 不再匯出舊版 Dataset/Bundle/Export 契約；README、使用說明與專案指引不再宣稱這些能力已交付。現有來源 raw、DB、下載歷史或分類證據不刪除。

## 8. 分段實作及驗證

### A. 來源與事件適配

限定目標：正確讀取實際保存資料，選用單一最新結果、精確配對明細、產出事件條件及分配值。

接受證據：使用真實來源 schema 的小型 SQLite fixtures 與現有官方 parser／storage 建立資料；檢查正常空事件、最新失敗不回退、TPEx inline、TWSE 精確明細、現金缺值零、必要換股條件及解析錯誤。來源資料內容及 schema 不變，WAL 已提交結果可見。

### B. 順序計算及移除舊功能

限定目標：將 A 的實際輸入接入升序核心，刪除退出需求的模組、入口及測試；不改既有官方流程與報告。

接受證據：走讀取→事件→計算的完整小型流程；五筆行情／兩次換股對照需求例子；事件生效日、無行情日、首筆當日事件、較晚事件、重複事件、現金不調價及面額直接／價格比兩條路徑皆有明確預期。未知商品分類保留且不冒充普通股。使用獨立 Fraction 答案核對數值及 50 位捨入，不只以測試通過代替行為比較。

### C. 全專案與實際執行元件

標準檢查：`uv sync --locked --group dev`、Ruff check/format、mypy、全套 pytest、`uv build --no-sources`。另檢查真正 console CLI 不再提供已刪入口、wheel 不含已刪模組，並以已建置套件執行內部有界來源 fixture 驗證；不得假稱 production 全量轉換或使用網路取得資料。

文件由 main agent 直接修改。每段驗證通過後才使用其結果進入下一段；尚未確認的對外格式不因實作方便而新增。

## 9. 本次驗收紀錄

以下為本修訂實際執行及主助手核對的結果。舊版測試數量、bundle／匯出 roundtrip 及指定未來基準日的 production 樣本不作本修訂驗收證據。

### 9.1 已完成實作與追溯

| 需求 | 實作及直接證據 |
|---|---|
| TQD-01、02、13 | `source.py` 以唯讀來源交易選取最新 task；source tests 驗證最新失敗不回退、成功空事件、WAL 已提交內容可見、固定 snapshot、DDL 拒絕及來源內容／schema 不變。 |
| TQD-03、04 | 分類器與 quote normalization；classification/build tests 保留普通股、非普通股及 unknown。整合測試另驗證同碼跨市場分開與多代碼事件不遺失；unknown／非普通股只保留原始資料及分類，不產生普通股調整列。 |
| TQD-05、09 | 原始 `QuoteValue` 與 `AdjustedQuote` 分開；quotes/build/factors tests 核對 OHLC、來源缺價及 volume/amount 不變。 |
| TQD-06、08 | `events.py` 整理摘要與精確連結的明細，保留三類分配及來源參照；events tests 驗證 TWSE dividend/reduction、TPEx inline、可選現金零、malformed／必要條件明確失敗。整合案例核對減資分配可從結果讀得，且不改倍率。 |
| TQD-07、12 | `arithmetic.py`、`factors.py` 按股順序計算；Fraction 獨立答案、50 位捨入、外部 hostile Decimal context、非有限值／下溢錯誤、日期邊界及未來資料 prefix 測試皆通過。 |
| TQD-10、11 | 已刪 `bundle.py`、`coverage.py`、`reader.py`、`export.py` 及其專屬測試，移除舊 dataset CLI／套件匯出及 anchor；原始碼查核及已安裝 wheel 查核均無已刪模組／入口。對外 API 與格式仍未指定，不提供替代發布產品。 |

`build.py` 的 `iter_series` 為內部串接入口，使用獨立、有 `(source,symbol,day)` 索引的臨時 SQLite 工作區排序，按股讀入計算；來源 DB 不加索引。內部結果保留 `raw_quotes`、普通股調整列、與原始列對應的分類、以及正規化事件／分配，不只回傳價格而丟棄分配。

整合驗收曾發現事件讀取的 symbol filter 被列值覆寫，已分離參數與列識別並補上多 scope／多代碼回歸；市場篩選在事件正規化前完成。未通過的中間結果未作最終符合性證據。

### 9.2 核對實際行為

整合案例以真實 `storage.SCHEMA` 與既有 `official.parse` 保存的資料呼叫內部流程，不 mock 計算結果。主要測試檔為 `test_taiwan_dataset_pipeline_sequence.py`、`test_taiwan_dataset_pipeline_reduction.py`、`test_taiwan_dataset_pipeline_tpex.py` 與 `test_taiwan_dataset_build.py`。

| 日期序 | 原始收盤 | 換股倍率 | 實際累積倍率 | 實際調整收盤 |
|---|---:|---:|---:|---:|
| 1 | 100 | 不套用首日事件 | 1 | 100 |
| 2 | 102 | — | 1 | 102 |
| 3 | 51 | 2 | 2 | 102 |
| 4 | 53 | — | 2 | 106 |
| 5 | 106 | 0.5 | 1 | 106 |

結果與需求例一致。另直接比對：

- 換股日沒有行情：下一筆行情才出現新倍率，不新增行情列；追加後續資料／未來事件，既有 prefix 的日期、倍率及價格不變。
- TWSE 減資：較舊重疊查詢提供 `0.8`，最新摘要精確連結明細提供 `0.94`；實際只套用一次 `0.94`。退還款 `0.6`、股利 `0.1` 分別保留，均不扣價；可選金額未提供時兩者為零，倍率仍為 `0.94`。
- TPEx 面額變更：直接倍率 `3`、摘要 `P/Q=2` 時，實際採 `3`；同碼 TWSE 摘要採自己的 `P/Q=2`，不混用市場事件。額外股票代碼的摘要與結果亦保留，明確 symbol filter 不混入別股。
- 原始缺價保持缺值；原始量、金額及分類保留，來源檔內容不變。

### 9.3 全專案與安裝後驗收

主助手實際重跑並確認：

| 檢查 | 結果 |
|---|---|
| `uv sync --locked --group dev` | 通過，無依賴／lockfile 變更 |
| `uv run --locked ruff check .` | 通過 |
| `uv run --locked ruff format --check .` | 通過（68 files） |
| `uv run --locked mypy` | 通過（23 source files）；未以任意擴大到所有 tests 的命令代替專案設定 |
| `uv run --locked pytest -q` | **356 passed** |
| `uv run --locked pytest -q tests/test_taiwan_dataset_*.py` | **71 passed** |
| `uv build --no-sources` | sdist 與 wheel 建置成功 |
| `git diff --check` | 通過 |

另將 wheel 以 `--offline --no-deps` 安裝至獨立臨時 Python 3.13 環境，確認實際載入路徑位於該環境的 `site-packages`，不是工作區 editable source。實際驗收包括：

1. wheel 檔案及安裝後 import lookup 均不含四個已刪模組。
2. 真正安裝的 `quant-data --help` 不含 `build-dataset`／`export-dataset`，仍含既有官方命令。
3. 從既有 schema／官方 parser 建立有界 fixture，使用安裝後核心得到五筆預期價格 `100,102,102,106,106`、倍率 `1,1,2,2,1`，並核對原始成交股數與金額不變。
4. 安裝後核心分別產出 `tpex:2317`、`tpex:2330`、`twse:2330`，同碼不同市場倍率各自為 `3` 與 `2`，可選現金缺值為零。

這是離線有界驗收，不是 production 全量轉換、官方全量資料驗證、歷史完整性或通用價格認證。核心驗收沒有執行市場資料下載、回補、migration／relabel、啟用排程或清理來源資料。

### 9.4 公開提交前安全檢查

公開提交前另行檢查程式、相依套件與候選檔案；安全工具在獨立工具環境執行，沒有新增專案依賴或修改 lockfile。套件漏洞查詢只使用套件名稱／版本，不上傳原始碼或本機證據。

- Gitleaks `v8.24.3`：掃描提交前既有的全部 6 筆 commit，以及包含未追蹤新檔的候選工作樹；未偵測到 secret。報告啟用完整 redaction，留在 repository 外，不加入 commit。
- 候選檔案清單另行確認沒有 `.env`、DB、私鑰、raw、本機 evidence、build／dist 或快取；未發現個人 home 絕對路徑。
- pip-audit `2.10.1`：鎖定的 13 個第三方套件（含開發依賴及其他平台條件套件）未查到已知漏洞；以實際 wheel generator 版本 Hatchling `1.33.0` 建立的獨立建置工具環境共 6 個套件亦未查到已知漏洞。建置工具並非 `uv.lock` 的一部分，此結果不代表未來任意重新解析版本。
- Bandit `1.8.6` 在 Python 3.13 掃描整個 `src`，無解析錯誤，得到 24 筆 SQL 組字串警示及 3 筆 assert 警示，無 HIGH。逐一追查來源與呼叫路徑後，現有操作介面未確認有可利用漏洞；不能把原始掃描結果寫成零警示。新 source reader 的金融資料與查詢值使用參數綁定；assert 不是授權檢查。
- 既有 storage 的 `put` 接受未驗證的 SQL conflict key，但現有呼叫只提供程式內固定值；沒有發現從 CLI 不可信輸入傳入的路徑。這是可信內部呼叫假設，不代表可將該參數開放給外部使用者。
- 分類 manifest／證據同樣是操作者指定的可信本機輸入，不是沙箱或可接收任意使用者上傳的服務；完整限制已列於[使用界線](taiwan-quant-dataset-usage.md)。SHA-256 不是來源真實性證明。

公開提交前再次重跑完整 356 筆測試、Ruff、格式、mypy 及建置，全部通過；獨立的 corporate-action context 變更另核對預設 precision=512、指定 precision=50 且不改變呼叫者 context。上述是指定範圍的安全檢查與已知漏洞資料庫查詢，不是無漏洞保證，也不涵蓋主機／作業系統漏洞。Git 作者資訊依既有設定，公開 push 前仍須確認姓名及信箱可公開。
