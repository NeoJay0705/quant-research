# 研究就緒資料快取設計

對應需求：[research-data-cache-requirements.md](research-data-cache-requirements.md)（RDC-01～13，已定案）。任務拆分見[實作任務](research-data-cache-tasks.md)。

**狀態：已實作並驗收。**使用說明見[research-data-cache-usage.md](research-data-cache-usage.md)。

## 1. 設計原則

- 只做需求所需的最小範圍：標準函式庫（`sqlite3`、`json`、`decimal`），不新增相依套件。
- 下載資料根目錄與 Dataset 的既有輸出語意不變。對既有程式的修改只限「行為保持不變的拆分」與「向後相容的選用參數」，由既有測試證明不變。
- 快取是衍生資料，可刪除、可由 records 與分類證據重建。
- 調價公式與四捨五入不重寫，一律重用 `dataset/arithmetic.py`。

## 2. 元件與資料流

```text
下載資料根目錄（唯讀，SourceReader，既有）
   │
   ▼
raw 序列讀取（T1 由現有函式拆出）
   │ 每筆報價：原價、分類、品質旗標、來源參照
   │ 代碼不是 4 位數 → 直接記為不可用（不呼叫分類與事件讀取）
   │ 單一標的的正規化失敗 → 該標的記為不可用，其餘繼續
   ▼
每檔：全部報價分類皆屬範圍？ ── 否 → symbols(unavailable, reason)
   │是
   ▼
讀事件（對所有範圍內標的，與該期間有無報價無關；失敗 → unavailable）
   → 調價（重用 Dataset）→ 驗證該檔 Bar／事件（失敗 → unavailable）
   ▼
研究快取檔（SQLite，獨立檔案）── load_cached ──→ ResearchData
```

新增模組：`src/quant_research/research/cache.py`；公開 API 由 `research/__init__.py` 匯出。首版只提供 Python API，不新增命令列入口。

## 3. 儲存

單一 SQLite 檔，路徑由呼叫者明確傳入。Decimal 存為 TEXT，陣列欄位存為 JSON 文字。

| 表 | 主鍵 | 內容 |
|---|---|---|
| `meta` | key | `schema_version`、`adjustment_rule`（RDC-09 連乘規則版本） |
| `markets` | market | `coverage_start`（首次建立指定的起日）、`built_through`（已建立日期）、`evidence_hashes`、`source_root`、`evidence_dirs` |
| `sessions` | market, date | 觀察到的市場交易日（與 `load_taiwan` 相同口徑：有完整快照報價紀錄的日期，與標的是否納入無關）。只新增、不刪除 |
| `symbols` | market, symbol | `status`（`available`／`unavailable`）、`reason`、`first_date`、`last_date` |
| `bars` | market, symbol, date | 原價 OHLC、volume、amount、調整 OHLC、`factor`（該日累計倍率）、分類／原因／證據參照、來源參照、品質旗標 |
| `events` | market, symbol, effective_date, event_type | `CorporateAction` 全部欄位、`multiplier`（`event_multiplier` 的結果，現金股利為空） |
| `scopes` | market, task_id, snapshot_id | 建立時各來源範圍的 source／dataset／筆數／診斷／請求日期（RDC-10，僅作建立來源紀錄；重複讀入同一範圍時覆寫同一列） |

取捨：調整價整欄儲存並另存每日 `factor`。「只存原價與倍率表、讀取時才乘」較小，但讀取路徑須另寫乘法與四捨五入並證明與 Dataset 一致；整欄儲存直接保留 Dataset 的結果。檔案大小以 T4 實測為準。`bars.factor` 是接續連乘的狀態；`events.multiplier` 讓接續只需（日期，倍率）而不必由 `CorporateAction` 還原 Dataset 的 `EventValue`。

## 4. 範圍與不可用標的（RDC-02、05）

- **非 4 位數代碼**（Dataset 對這類代碼的分類必為 `unknown`、原因 `invalid_symbol_shape`，形狀檢查優先於證據判斷，與是否提供證據無關）：不正規化報價、不讀事件，直接在 `symbols` 記為不可用，原因 `classification:unknown:invalid_symbol_shape`。這既符合「記錄不在範圍的標的」，也避免對約 4.7 萬個 TPEX 代碼逐筆正規化。形狀判斷必須與 Dataset 完全相同（ASCII `[0-9]{4}` 全長比對，不用 `len()` 或 Unicode `isdigit()`），並以測試證明捷徑與 Dataset 對同一代碼的分類與原因一致。
- **4 位數代碼**：以其涵蓋期間內全部報價的分類判斷；全部屬 `ordinary_evidence_supported` 或 `ordinary_rule_limited` 才納入。其他（含日後出現的混合分類）整檔記為不可用，`reason` 如 `classification:excluded_non_ordinary:00_code_rule_v1`、`classification:mixed:{類別集合}`。
- **失敗以單一標的為單位隔離**：報價正規化失敗（發生在整個市場的暫存階段、尚未逐檔輸出之前）、事件讀取失敗、調價失敗、以及 `Bar`／`CorporateAction` 驗證失敗，都把該標的記為不可用，`reason` 為例外類型與訊息，其他標的照常；一個標的在寫入前須通過完整驗證才可標為可用。T1 拆出的讀取函式以選用的錯誤回呼支援此行為，預設維持現行 fail-fast。因為只對範圍內標的讀事件，先前 `2887F` 類失敗不會發生。
- 不可用標的不寫入 `bars`／`events`。更新時，已可用的標的若新日期使其轉換失敗或分類改變，刪除其 `bars`／`events` 並改記為不可用（同一交易內）；**不可用狀態在一般更新中不會自行恢復**：更新不得僅憑新日期的後段資料把標的改回可用；只有以 `coverage_start` 為起始日的 `rebuild_cache`（從頭重新判斷）能恢復，其他起始日對不可用標的明確拒絕。
- 來源範圍不可用（`scope.usable` 為否）、儲存錯誤等**指令層錯誤**使整個指令失敗，不當作單檔略過。

## 5. 建立、更新與重建（RDC-05～09）

| 函式 | T | 標的 |
|---|---|---|
| `build_cache`（首次） | 呼叫者指定的起日（成為 `coverage_start`） | 該市場全部 |
| `update_cache` | `built_through` 的次日 | 該市場全部，含新標的 |
| `rebuild_cache` | 呼叫者指定的起始日（不得早於 `coverage_start`） | 指定標的，或該市場全部；結束日固定為既有 `built_through`，**不延伸涵蓋期間**（要延伸須之後執行 `update_cache`） |

共通規則：

- 每次呼叫處理**一個市場**，在**單一寫入交易**內完成：建立時啟用 WAL，指令開頭即 `BEGIN IMMEDIATE`，鎖逾時為 0（取不到寫入鎖立即報錯）；失敗或中斷整體回復，快取與指令前相同（RDC-05）。首次建立失敗時，若檔案由本次呼叫建立則一併移除，不留下可用的快取。
- `build_cache` 對已有該市場資料的快取拒絕；`update_cache` 的結束日不晚於 `built_through` 時拒絕。
- 來源讀取使用 `SourceReader`，範圍 `[T, end_date]`，`mode=ro`、既有「最新選版」語意。
- `sessions` 只新增該範圍內觀察到的交易日，不刪除；`update_cache` 更新 `built_through`，`rebuild_cache` 不改變它，也不改變市場層級的涵蓋期間。
- **事件讀取範圍：** 重新轉換的標的（含沒有任何新報價的既有可用標的）一律讀取 `[T, end]` 的事件並寫入，與該期間有無報價無關；沒有任何列的新標的（update 時才出現）讀取 `[coverage_start, end]` 的事件，使其事件與一次完整建立相同（含早於首筆報價的事件，這些事件不影響調價）。既有標的在 `T` 之前已存的事件不重讀。
- `markets.evidence_hashes` 記錄分類證據輸入雜湊。與傳入的雜湊不同時拒絕，除非本次是該市場全部標的、起始日不晚於 `coverage_start` 的完整重建，避免同一快取混用兩套分類（RDC-02、07 的前提）。

### 5.1 調整價接續（RDC-06、07、09）

連乘規則不變：還原價 = 原價 × 累計倍率，起點為該檔快取最早報價。對某檔從 T 重新轉換：

1. 刪除該檔 `date >= T` 的 `bars`／`events`（update 時沒有可刪除的列）。
2. X = 該檔保留的最後一筆報價日期，`factor_X` = 該日已存 `factor`；若沒有保留的報價（新標的或以 `coverage_start` 重建），從頭開始（`process_quotes` 預設行為）。
3. 待套用事件 = 已存快取中 `effective_date > X` 且倍率不為空的事件（含報價空窗期內的事件，及 T 之前已讀入者）＋ 來源在 `[T, end]` 新讀入的事件。
4. 以同一個 `process_quotes` 迴圈，從 `factor_X` 起對新報價接續連乘；**已套用截止日 X 取代**現行「略過日期不晚於本次第一筆報價的事件」這條邊界（只初始化倍率而保留原邊界，會遺失空窗期事件與首筆新增報價當日的事件）。保留原有的 50 位精度隔離環境與逐事件四捨五入。

規則沿用現有定義：事件日期須晚於該檔起點，且不晚於報價日，每個事件只套用一次。為此對 `process_quotes` 加兩個選用關鍵字參數（初始倍率、已套用截止日），預設保持現行行為；待套用事件以（日期，倍率）提供。

已知限制（寫入使用說明）：
- 增量只讀 `[T, end]` 的事件；日期早於 T 才出現的事件不被讀入，也不套用，不會在接縫處產生假跳動（RDC-08）。
- 不偵測舊日期報價被修訂。
- 補入更早歷史後該檔起點改變，須以 `rebuild_cache` 從 `coverage_start` 重建該檔。
- `sessions` 只新增；若來源日後移除某交易日，快取仍保留該日，屬 RDC-07 前提（來源未變）之外的情形。
- RDC-07 的一致性只在起始日以前的來源、事件與分類證據未變時成立。

## 6. 讀取（RDC-01、11、12、13）

```python
load_cached(cache_path, market, symbols=None, start_date=None, end_date=None) -> ResearchData
cache_status(cache_path) -> 各市場 coverage_start／built_through、可用與不可用標的數
list_symbols(cache_path, market, status=None) -> (symbol, status, reason, first_date, last_date)
```

- 開啟唯讀連線，一個一致的讀取交易內讀完。
- 起訖日預設為涵蓋期間；超出 `[coverage_start, built_through]` → 明確錯誤。
- 明確指定標的：不可用 → 含原因的錯誤；快取中不存在 → 另一種錯誤；範圍內沒有報價 → 錯誤（同 `load_taiwan`）。`symbols=None` → 該市場在範圍內有報價的**可用標的**（文件須註明這是快取內可用集合，不是完整市場）。
- 回傳 `ResearchData`：`bars`／`events` 欄位語意同 `load_taiwan`，事件按 `[start, end]` 篩選；`sessions` 取範圍內；`calendar_basis` 為 `observed_market_quote_dates`；`source_diagnostics` 為該市場快取的建立來源紀錄（取自 `scopes`，不是依載入範圍切片，與 `load_taiwan` 的 `source_diagnostics` 內容不同，因此兩者的 `input_digest` 不相等）；`classification_diagnostics` 由讀出的 Bar 以相同格式產生並加上分類證據雜湊紀錄；`protected_input_dirs` = `markets` 記錄的來源根目錄與證據目錄，加上**載入當下**解析後的快取檔所在目錄（是 `load_taiwan` 的超集合）。載入時不讀取來源或證據檔。
- `input_digest` 仍由 `ResearchData` 讀出後計算，不用於判斷過期。
- 調整價水準以該檔快取最早報價為起點，與 `load_taiwan` 在較晚起日載入時不同；使用說明須明示並提醒絕對價格條件用原價（RDC-09、12）。

## 7. 非功能性需求（務實判斷）

| 項目 | 判斷 |
|---|---|
| 併發 | 單一寫入者、多讀取者：寫入單一交易（WAL、`BEGIN IMMEDIATE`、鎖逾時 0），讀取唯讀連線不被長交易阻擋。不做分散式。長交易的 WAL 檔會增長，實測時記錄其峰值；複製使用中的快取須用 SQLite backup 或在關閉並檢查點後複製，不能只複製主檔。 |
| 效能 | 範圍內標的約 2,100 檔、約 380 萬列（估計）。量測顯示掃描全部 TPEX 報價約 7.5 分鐘、TWSE 約 2 分鐘；建立耗時、WAL 與暫存空間、檔案大小以 T4 的冒煙量測加比例推估為準，不優化；全量未實跑，單一交易在全量規模下的 WAL 與暫存空間增長是推估風險。 |
| 記憶體 | 建立時逐檔處理。`load_cached` 仍回傳完整 `ResearchData`（記憶體隨載入量成長）；分批待存取方式確定後再決定。 |
| 暫存空間 | 沿用 Dataset 的暫存作業區（系統暫存目錄），實測時記錄。 |
| 安全 | 以解析後的實際路徑檢查：快取路徑不得位於下載資料根目錄內或分類證據的受保護目錄內，違者在開啟可寫入的儲存之前拒絕；不修改來源（以 size／mtime 核對）；快取放在 repo 之外，內容不入版控。 |
| 相容性 | `meta.schema_version`／`adjustment_rule` 不符時拒絕並提示刪除重建；不做遷移。 |
| 外部服務 | 無，不下載、不呼叫網路。 |

## 8. 驗證項目（每項說明為何列出、為何不刪）

**比較基準（V2、V3 用）：** 「正規內容」= `bars`（含 `factor`、調整價）、`events`、`symbols`（狀態與原因）、`sessions`、`markets` 的涵蓋欄位，Decimal 以字串逐字比較；不比較資料庫檔案位元組、`scopes` 的建立紀錄或其他操作紀錄。不使用容差。

| ID | 驗證 | 為何列出／為何不刪 |
|---|---|---|
| V0 | 既有測試套件、ruff、mypy 通過；`load_taiwan` 對既有測試資料的輸出（含 `input_digest`）與修改前相同；預設錯誤行為（fail-fast）、reader 所有權與暫存作業區清理不變 | T1 與 `process_quotes` 的修改動到既有程式，需求要求不改變既有語意。不刪：沒有它無法判斷既有行為是否被改壞（它是回歸防護，不是完整證明） |
| V1 | 快取讀出的 `bars`／`events`／`sessions`／分類診斷／`calendar_basis` 與 `load_taiwan` 對同一範圍一致（起日取標的最早日，使調整價水準相同）；涵蓋兩個市場與有／無分類證據；含只出現在未選標的上的交易日；較晚起日的切片，調整價水準不重設；兩個市場的相同代碼與日期互不干擾；載入時不讀來源或證據檔 | RDC-01、04、09。不刪：是快取等於現有資料的證明；不比較 `source_diagnostics`／`input_digest`（設計 §6 已說明不同） |
| V2 | `build` 到 D1 後 `update` 到 D2，正規內容與直接 `build` 到 D2 相同；案例：事件落在無報價日、事件在首筆新增報價當日、整段更新期間某既有標的完全沒有報價且期間內有事件、之後恢復報價、循環小數倍率（10÷3）、多次事件、重複更新、新標的含早於首筆報價的事件；日期早於 T 才出現的事件不進入更新；舊前段不變 | RDC-06、08。不刪：四捨五入與邊界事件是增量最可能出錯處 |
| V3 | `rebuild` 從 T 起（T 為 `coverage_start`、中段；全部標的與指定標的），在 T 之前輸入未變的條件下，與以最新來源從頭建立的正規內容相同，且 T 之後的來源變動（報價被修訂、事件被修訂或移除）被納入；指定標的重建不改變市場涵蓋期間；不可用標的只能以 `coverage_start` 重建恢復，其他起始日明確拒絕；T 早於 `coverage_start` 被拒絕 | RDC-07、13。不刪：只測「輸入未變」會讓一個什麼都沒重做的實作通過 |
| V4 | 非 4 位數捷徑與 Dataset 分類一致（含 ASCII 以外的數字字元、有分類證據）；混合分類（建立與更新時）、非普通股、正規化失敗、事件讀取失敗、驗證失敗的標的被記錄且其他標的不受影響；更新時既有標的失敗轉為不可用；讀取端：不可用／不存在／無報價／超出涵蓋期間各得到不同的明確錯誤；`symbols=None` 只含範圍內有報價的標的，且沒有任何可用標的時得到明確錯誤 | RDC-02、05、13。不刪：這些是「不得靜默回傳錯資料」的行為 |
| V5 | build、update、rebuild 各在注入例外後（含刪除與狀態變更之後的失敗），重新開啟並以正規內容比對與失敗前相同；首次建立失敗不留下可用的快取 | RDC-05 原子性。不刪：需求直接要求，且 update／rebuild 的刪除再寫入路徑是最可能破壞原子性之處 |
| V6 | 來源資料根目錄與證據輸入 size／mtime 不變；快取路徑（含別名路徑）位於根目錄或證據目錄內被拒；讀出的 `ResearchData.protected_input_dirs` 含來源根目錄、證據目錄與當下快取所在目錄；schema 或連乘規則版本不符時讀寫皆拒絕 | RDC-01、03。不刪：來源保護是專案硬界線；版本不符若不拒絕會靜默讀到錯誤語意的資料 |
| V7 | 證據雜湊不同時拒絕部分重建與更新、允許完整重建；完整重建後先前不可用的標的被重新判斷 | 防止同一快取混用兩套分類（RDC-02、07 前提）。不刪：否則 RDC-07 的前提會被靜默破壞 |
| V8 | 本機真實資料唯讀：少量標的快取與 `load_taiwan` 逐欄一致；以保留尾段的方式實測更新（先建到較早日期，再更新到最新，與一次完整建立相同）；以至少三個不同資料量（N 筆報價）做冒煙量測，記錄建立與 `load_cached` 的耗時、峰值記憶體、暫存空間、WAL 與檔案大小，檢查是否近似線性，再依比例推估全部資料 | 需求§6（使用者決定：冒煙加推估，不設門檻、不要求全量實跑）。不刪：這是唯一提供規模估計的依據；多個 N 是為了判斷比例推估是否成立。推估不是實測，報告須如此標示 |

## 9. 預計修改既有程式

- `taiwan/dataset/build.py`：把 `_iter_series_from_reader` 拆成「讀原價報價＋分類＋品質旗標」與「事件＋調價」兩段，原函式行為與輸出不變。
- `taiwan/dataset/arithmetic.py`：`process_quotes` 新增兩個選用參數（預設行為不變）。
- `research/taiwan.py`：把由每檔序列產生 `Bar`／`CorporateAction` 的邏輯抽成共用函式，`load_taiwan` 對外行為不變。
- 其餘為新增檔案與測試。

## 10. 需求對照

| 需求 | 設計 |
|---|---|
| RDC-01 | §6、§3 `sessions` |
| RDC-02 | §4 |
| RDC-03 | §3 獨立檔案、§7 安全 |
| RDC-04 | §3 `bars`、`events`、`sessions` |
| RDC-05 | §4、§5 共通規則 |
| RDC-06、07、08 | §5、§5.1 |
| RDC-09、12 | §5.1、§6 |
| RDC-10 | §3 `scopes` |
| RDC-11 | §6 |
| RDC-13 | §6 |
