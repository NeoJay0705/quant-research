# Dataset 本機資料盤點與實測設計

## 1. 狀態與範圍

本設計依[已確認需求](dataset-local-validation-requirements.md)，由使用者於 2026-10-30 確認。執行順序為：局部選案例 → 小範圍真實實測 → 主助手核對接受 → 全量盤點。全量盤點不等於全量 Dataset 計算驗證。

新增內部開發腳本及必要測試；不加入 `quant-data`、不公開 API、不改既有 Dataset 核心、官方下載器、報告或排程。不恢復 bundle／版本 manifest／coverage／正式匯出功能。

## 2. 程式與操作介面

- 開發腳本 `scripts/dataset_local_validation.py` 提供分開執行的 `discover`（局部來源查詢）、`verify`（單一案例實測）與後續 `inventory`（全量盤點）操作；不是安裝後的正式命令或穩定資料格式。
- 測試位於 `tests/test_dataset_local_validation.py`，可依實際範圍拆分；不新增第三方依賴或修改 lockfile。
- 資料根目錄明確傳入，實際授權目錄為 `~/.local/share/quant-research/taiwan/`。
- `discover`／`verify` 明確要求起訖日期，單次不超過 62 個曆日；`verify` 限定一個來源市場與一個代碼。候選數量有上限，不自動擴大日期或代碼範圍。
- 第一輪局部查詢採 2022-09-01 至 2022-10-31（61 日），根據既有文件中的 2022 年事件背景尋找真實案例；文件提及案例不是存在或可用證明，以查詢結果為準。無適合案例則記錄缺項，需擴大時先回報。
- 工具將診斷結果以 JSON 印至 stdout，金融值使用十進位字串；這是開發驗證證據，不是正式 Dataset 匯出。來源不存在或必要資料錯誤時明確非零退出。
- 執行證據由操作者保存至 `artifacts/` 下新的獨立目錄；此目錄既有 git ignore，只存本機證據，不寫入 production、不提交大量逐列行情或個人 home 路徑。版控文件保留必要摘要、案例核對與命令範圍。

## 3. 局部來源查詢與案例接受

1. 以 `mode=ro`／`query_only` 讀取 SQLite，不使用 `immutable=1`。
2. 沿用 `SourceReader` 的最新 task edition／snapshot 選取，明示不可用 scope，不回退舊成功資料。
3. 僅查指定日期內的候選行情／事件與必要摘要明細連結。允許為選取規則讀 task／snapshot metadata，但不提前統計全部 records、不掃全部 raw。
4. 優先尋找 TWSE／TPEx 一般行情、減資、面額變更、股利／配股／退還款案例；同一案例可涵蓋多個核對項目。日期窗口需含首筆行情與事件後行情，否則不算驗證到生效邊界。
5. 輸出市場、代碼、生效日、來源識別與可用性；主助手檢查實際來源資料及必要明細後，才接受為實測輸入。
6. 非普通股／unknown／缺價沒有真實案例時明示未驗證，不補造資料。

## 4. 實際計算與獨立核對

- 實測必須呼叫工作區當前實際的 `dataset.build.iter_series(BuildSpec(...))`，不得 mock 或用另一個簡化調價器代替。
- 預期輸入從所選來源 DB 的原始解析 payload、分類證據及精確連結的事件明細取得，不從 `QuoteSeries` 的計算結果反推答案。
- 可沿用來源選取及既有分類器；證據須揭露此共用範圍。分類以輸入條件及分類依據的保留核對為主，不能宣稱獨立認證完整分類規則。
- 預期倍率、順序累乘與 OHLC 必須由獨立核對邏輯產生，不呼叫 `event_multiplier`、`process_quotes`、`calculate_adjustments` 或調整結果產生答案。對測試中的已知例子另用明確固定預期值／有理數核對，避免兩份相同錯誤互相驗證。
- 使用 Decimal，50 位有效數字、ROUND_HALF_EVEN，每步運算依既有政策捨入。減資直接 m；面額優先 m、否則 P/Q；首筆行情 C=1，只累計 d0 < effective_date <= quote_date 的事件一次；現金、配股與退還款不改倍率。
- 逐列比較市場、代碼、日期、原始 OHLC、量、金額、缺值、倍率及調整 OHLC；事件的分配值與來源參照亦需對照原始採用值。非普通股／unknown 不得冒出調整列。
- 保留適量逐列預期／實際值及來源 task／snapshot／record 參照；結論區分通過、失敗、無真實案例。未遇到缺值等情況不能以零次檢查當成已驗證該行為。
- 偵測驗證期間來源資料變動；不能將不同時間來源與結果混作同一證據。發生變動時使案例無效並回報，不自動寫來源或忽略差異。
- 如發現既有核心錯誤，保留具體失敗輸入／預期／實際結果並回報；本次實作不自行修改核心。

## 5. 實測接受後的全量盤點

- 全量限指定根目錄中已保存、官方命名空間的 TWSE／TPEx 行情、摘要與明細；舊 FinMind 或 legacy 非官方 pipeline 不混入。
- 以索引及分批聚合處理，不將全部 records 載入記憶體；必要全量統計仍有磁碟 I/O 成本，不保證秒級。不中途新增來源索引。
- 分開呈現歷史保存量、最新任務可用 scope／snapshot 筆數、去重後事件數。跨版本、跨重疊查詢的列數不得冒充唯一事件／有效行情筆數。
- 明確區分請求區間、實際行情／事件日期與下載時間。缺少適當日期欄位時揭露，不以下載日期代替。
- 檢查摘要連結之必要明細可用性，不只按代碼尋找任意明細。
- 列出最新任務失敗、未完成、snapshot／必要明細不可用等事實；不新增研究就緒、全市場覆蓋或來源不存在判定，不改既有報告標記。
- 不呼叫全量 `iter_series`、不保存全量調整結果、不稱為全量計算驗證。

## 6. 非功能性與資料安全

- Python 3.13，沿用鎖定環境；不新增外部服務、下載或依賴。
- 所有 production 操作只讀；允許 SQLite 正常管理 WAL／SHM，不刪除 sidecar，不宣稱位元組完全不變（例如 access-time）。
- 當前來源 DB 約 26.4 GB；分批查詢與有限案例是必要的記憶體控制，不引入分散式／高併發設計。
- 不全掃 raw 或計算全庫 fingerprint；只記錄採用資料的必要識別，驗證證據不是 Dataset 版本管理產品。
- 分類證據只使用明確可信本機輸入；無 manifest 時沿用核心的有限規則分類，揭露限制，不偽稱歷史官方分類認證。不自動尋找或下載新證據。

## 7. 分段工作與接受鏈

### A. 小範圍工具與測試

限實作 discover／verify 及必要測試，不執行 production 實測或全量盤點。測試以現有 schema／官方 parser 建立小型來源，驗證已知價格、倍率、分配與可用性；必要時注入錯誤結果確認 verifier 能偵測，不僅測正常通過。包含缺 DB 不建立、日期限制、最新失敗不回退、未知分類及來源變動失敗。

主助手檢查程式、比較固定預期值與實際證據，確認實際腳本能執行及其金融 oracle 未呼叫原調價函式，才進入 B。

### B. 局部 production 查詢與實測

先執行 discover，主助手接受候選來源，再委派執行選定 verify。逐項查看實際採用來源、預期／實際及未驗證項目。失敗不自動修核心，不繼續依賴失敗結果進行全量工作。

### C. 全量盤點工具與執行

B 接受後才實作／接受 inventory 的必要部分與測試，再執行 production 全量盤點。以已知 fixture 核對版本重複、實際日期、latest unavailable、明細與去重事件數，並用獨立來源查詢抽核 production 摘要。

### D. 專案檢查與歸檔

執行專案標準檢查：uv sync --locked --group dev、Ruff check／format、mypy、pytest、uv build --no-sources、git diff --check。腳本不在 wheel 內，另直接驗證真正執行的腳本／核心路徑；建置成功不能替代行為核對。

文件由主助手維護。每段結果經主助手檢查後才能成為下一段的輸入。

## 8. 執行紀錄

### A. 小範圍工具接受（2026-10-30）

初版及第一次補正的驗收矩陣不足，主助手拒絕接受，未據此讀取 production 案例。後續按金融案例、錯誤偵測、證據完整性拆小補正。

主助手讀取實作及測試，實際重跑 `uv run --locked pytest -q tests/test_dataset_local_validation*.py`：**28 passed**；變更檔 Ruff check／format 及 `git diff --check` 通過。

- 真實 schema／官方 parser 建立的測試資料，走實際 iter_series，五筆固定倍率為 `1,1,2,2,1`、調整收盤為 `100,102,102,106,106`；與固定答案相符。
- 減資精確連結最新明細倍率 `.94`；缺省現金為零及股利 `.1`／退還款 `.6` 兩條路徑均核對，非從摘要價格比推倍率。
- TPEx inline 直接倍率 `3`、TWSE P/Q 倍率 `2` 分開核對；無行情日事件、首日排除與未來事件排除有固定答案。
- 上市股利明細股利 `2`、配股率 `.125` 保留，兩筆倍率仍 `1,1`。
- 缺價、非普通股／unknown、最新失敗不回退、缺 DB 不建立、日期限制、來源第二連線 commit、hostile Decimal context 均有測試。
- 注入錯誤倍率、價格、事件值、來源 ref、重複／多餘結果及分類筆數，verifier 均拒絕接受。

上述是工具／測試資料接受，不是 production 實測。金融 expected 路徑獨立解析 payload 與計算；來源選擇與分類器仍共用，證據揭露此限制。

### B. 小範圍 production 實測接受（2026-10-30）

局部 discover 範圍為 2022-09-01 至 2022-10-31，每市場／事件種類候選最多 2，行情樣本每市場最多 2。回報 134 個選用 scope、0 個不可用；這不是全量盤點。主助手直接唯讀核對採用的摘要、精確明細及最新行情來源，再選定案例。首批部分股票因停牌而缺少窗口內事件前行情，不誤稱已驗證生效邊界，改用同一窗口內其他已發現候選，不擴大日期。

7 個實際腳本 verify 均 exit 0，來源 data_version 前後相同，各案例沒有 failed check。主助手另外將全部 **67 筆原始行情**與 DB record payload／snapshot 參照逐筆比對，並以已核對來源倍率重新算 **63 筆調整 OHLC**；4 筆 ETF 無調整列。實際數值一致。

| 市場／代碼 | 範圍 | 原始行情數 | 實際核對結果 |
|---|---|---:|---|
| TWSE 2368 | 2022-09-01～09-30 | 14 | 9/19 前 C=1，當日起 C=.9；9/19 原始收盤 88.10 → 79.29。精確明細每千股換900、退還款1、股利0；退還款不扣價。 |
| TPEx 3288 | 2022-09-01～09-30 | 14 | 9/26 前 C=1，當日起 C=.66195765；9/26 原始收盤20.40 → 13.50393606；現金缺省零，包含真實缺價列。 |
| TPEx 5536 | 2022-09-01～09-30 | 14 | 9/19 面額直接倍率2；當日原始收盤106 → 212。 |
| TWSE 3356 | 2022-09-26～10-07 | 10 | 10/4 股利1.2、配股0及精確明細保留；全部C=1。 |
| TPEx 1586 | 2022-09-01～09-09 | 6 | 股利.65877051保留；全部C=1。 |
| TWSE 0050 | 2022-10-11～10-14 | 4 | excluded_non_ordinary，原價保留，不產生調整列。 |
| TWSE 2375 | 2022-09-01～09-16 | 5 | 首筆9/12即事件日，保留m=.8、退還款2，但不重新套用，全部C=1。這是首日排除，不是事件前後邊界。 |

採用資料關鍵參照：2368摘要record8983571／明細record8985528（snapshot968d1c7eeff44fe9bd3f8955801f1551）；3288摘要8983653；5536摘要8983657；3356摘要8983413／明細8985441；1586摘要8983575；2375摘要8983570／明細8985524。完整逐列ref、預期／實際及命令時間存於本機 `artifacts/dataset-local-validation-20261030T123825Z-27992/`，不加入版控。

未驗證的真實情境包括非零股票股利、unknown分類、TWSE面額價格比路徑及換股日無行情但窗口內有前後行情；不從單元測試或其他類別推稱真實案例已通過。分類未提供manifest，普通股採 ordinary_rule_limited，並非歷史普通股分類認證。數值比較採Decimal語意，0與0E-8的字串差異不構成金融值不一致。

本次小範圍實測接受，允許進入 C 的全量**盤點**工具實作與驗證；不授權全量Dataset計算驗證。

### C1. 全量盤點工具接受（2026-10-30）

新增 `scripts/dataset_local_inventory.py`，由開發腳本的獨立 inventory 操作呼叫。初版有最新異常參數被篩掉、明細先篩身分再選版本的問題，主助手拒絕後要求修正；現以曾有官方命名空間的 logical family 為錨，保留最新失敗／損壞身分，不回退舊成功版，也不混入純 legacy family。

主助手讀取程式與固定筆數測試，重跑本次所有專屬測試：**34 passed**，Ruff check／format、`git diff --check` 通過。已核對歷史重複保存量、最新失敗及異常params不回退、成功空scope、明細null日期、重疊摘要3列但1個唯一事件（含1列malformed另報）、必要明細正常／缺少／失敗、明細最新身分變動與legacy排除、缺DB不建立及實際subprocess入口。

盤點只聚合 records 日期／數量及檢查摘要身分／必要明細連結，不執行全量 iter_series、金融正規化或調價。每snapshot聚合結果共用，避免歷史及最新日期重複掃描；記憶體只留snapshot聚合、task metadata與事件識別，不載入全部行情payload。現在接受工具並允許執行指定production根目錄的全量盤點。

### C2. 全量 production 盤點接受（2026-10-30）

實際執行獨立inventory操作，exit0，耗時141.757秒；詳細輸出約3.8MB，保留於本機證據目錄。主助手以另一個SQL join聚合查詢核對每source/dataset歷史snapshot／record數，與工具結果全部一致；另逐筆查核15個不可用明細的真實task／snapshot／零record狀態。

- 歷史官方snapshot16,912個、record20,123,831列。
- 最新官方行情snapshot列：TWSE2,531,265，TPEx17,563,927，合計20,095,192；不是普通股專屬或唯一有效股池數量。行情日期端點2018-01-02～2026-10-22，不推稱區間完整。
- 去重後最新事件18,832：TWSE股利9,604、減資218、面額10；TPEx股利8,811、減資175、面額14。malformed摘要0。
- 最新行情／摘要scope7,070個，來源scope層不可用0；兩市場行情各包含1,075個empty_unverified成功空scope，不能把available當作有行情或完整性認證。
- 上市必要明細連結9,822：可用9,807、不可用15、缺連結0。15個task雖completed，snapshot為empty_unverified且零record，涉及2887F、2887Z1、2887G/H/I等帶尾碼代碼；不是本次已驗證的普通股案例。沒有重新下載、relabel或把空明細當成可用。
- 股利明細9,589列、減資明細218列record_date為null，事件日由摘要及link提供，不當成行情歷史缺漏。

本次接受的是盤點事實與統計，不是全量事件金融解析／全量調價接受。完整分類明細、日期、問題列表及操作見[使用與結果](dataset-local-validation-usage.md)。

### D. 全專案檢查與實際執行元件

主助手檢查完整執行log，以下皆exit0：`uv sync --locked --group dev`、`uv run --locked ruff check .`、`uv run --locked ruff format --check .`（76 Python檔）、`uv run --locked mypy`（23 source檔）、`uv run --locked pytest -q`（**390 passed in 30.32s**）、`uv build --no-sources`、`git diff --check`。

實際開發腳本help含discover／verify／inventory；真正quant-data入口不含build-dataset／export-dataset或新增正式inventory／verify。實際載入的是工作區src內的dataset/build.py；腳本不在wheel中，建置成功不代替腳本行為驗證。標準build更新本機dist產物，沒有修改production或lockfile。

全專案log位於本機證據目錄objective-D-checks.log。主助手完成文件與連結後，另實際重跑Ruff check／format（77 Python檔）、mypy、全套pytest（**390 passed in 49.53s**）、build及git diff --check，全部通過；新／修改文件的本機連結檢查亦通過。最終標準檢查未重新執行production。

程式變更限2個開發腳本與4個測試檔；既有src、pyproject.toml、uv.lock、production DB／raw及正式CLI未修改。需求、設計、使用與結果文件已歸檔；沒有執行commit。
