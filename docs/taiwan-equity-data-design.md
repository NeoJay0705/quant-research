# 台股資料設計與實作契約

本文件說明現存實作，不是待辦計畫或操作日誌。需求及穩定 ID 見[台股資料需求](taiwan-equity-data-requirements.md)。所有相對來源／測試連結均由 `docs/` 指向 repository 檔案。

## 1. 實作組成

| 層 | 具體模組／符號 | 職責 |
|---|---|---|
| 來源 | [`official.py`](../src/quant_research/taiwan/official.py)：`URLS`、`_params`、`request`、`parse`、`OfficialBatch` | TWSE／TPEx 固定 URL、精確參數、有限 parser 與來源欄位正規化。 |
| 共用 transport | [`sources.py`](../src/quant_research/taiwan/sources.py)：`fetch_allowed`、`urllib_transport`、request errors | transport 限制、大小／完成性、HTTP refusal 與 FinMind 預設拒絕邊界。 |
| 持久化 | [`storage.py`](../src/quant_research/taiwan/storage.py)：`SCHEMA`、`SQLiteStore`、`save_raw`、`save_snapshot`、`TaiwanData` | SQLite tasks/runs/snapshots/records/links/provenance；append-only raw。 |
| 執行 | [`official_workflow.py`](../src/quant_research/taiwan/official_workflow.py)：`OfficialWorkflow.sync_events`、`maintain_events`、`_execute` | 規劃、runner/source locks、節流、狀態、重試、refresh、續接。 |
| 報告 | [`reports.py`](../src/quant_research/taiwan/reports.py)：`_open`、`observation_universe`、`availability` | 一致唯讀 DB 查詢及已保存證據報告。 |
| 事件候選 | [`corporate_actions.py`](../src/quant_research/taiwan/corporate_actions.py)：`build_corporate_actions` | 對官方 summary/detail links 做欄位、衝突、financial check 的純整理。 |
| CLI／排程 | [`cli.py`](../src/quant_research/taiwan/cli.py)、[`schedule.py`](../src/quant_research/taiwan/schedule.py)：`write_units` | 命令介面及 unit 檔案產生。 |

## 2. 需求對照：來源、資料與工作流

| ID | 實作契約／具體符號 | 對應測試函式／路徑 |
|---|---|---|
| TWD-01 | `official.URLS` 只列兩交易所及固定 dataset；`_params` 精確檢查 required keys，固定 query parameter；`request` 交由 `sources.fetch_allowed`。行情：TWSE `MI_INDEX`、TPEx `dailyQuotes`；事件摘要 TWSE `TWT49U/TWTAUU/TWTB8U`、TPEx `exDailyQ/revivt/pvChgRslt`。詳細明細資料集以 parser 提供的 token 和日期組成精確 query。 | [`test_taiwan_official.py`](../tests/test_taiwan_official.py)、[`test_taiwan_official_cli.py`](../tests/test_taiwan_official_cli.py) |
| TWD-02 | `SQLiteStore.save_raw` 保存原始 bytes，snapshot/records/task 綁來源、參數、raw path/hash 與狀態。raw append-only；raw 先落地，DB 提交失敗可留下孤立 raw，不能清理掉。 | [`test_taiwan_storage.py`](../tests/test_taiwan_storage.py)、[`test_taiwan_recovery.py`](../tests/test_taiwan_recovery.py) |
| TWD-03 | `OfficialWorkflow` 依 official pipeline/lane 與 exact logical request 規劃；resume 只恢復相應未完成任務。refresh 建新 edition，不覆蓋舊快照；SIGINT/中斷與 recover 保留工作狀態。writer lock 僅保護短 DB 規劃／提交，runner lock 阻止同 lane 重入，來源鎖保護單次 request／節流／拒絕。 | [`test_taiwan_official_workflow.py`](../tests/test_taiwan_official_workflow.py)、[`test_taiwan_recovery.py`](../tests/test_taiwan_recovery.py)、[`test_taiwan_maintenance_recovery.py`](../tests/test_taiwan_maintenance_recovery.py) |
| TWD-04 | `OfficialWorkflow.sync_events` 將摘要按月規劃；只消費 parser `detail_tasks`，以 `official_event_links` 保留精確 parent task/snapshot→child task 及 context。child 不因全域新 edition 回接舊 parent。TWTB8U 不猜 TWTB7U 的 FILE_DATE；索引限制見需求。 | [`test_taiwan_official_event_workflow.py`](../tests/test_taiwan_official_event_workflow.py)、[`test_taiwan_corporate_action_links.py`](../tests/test_taiwan_corporate_action_links.py) |
| TWD-05 | `official.parse` 僅承認明確 empty envelope；狀態仍 `empty_unverified`，且保留 warnings。403/429、captcha、schema 錯誤與資料不一致不視為 no-data。 | [`test_taiwan_official.py`](../tests/test_taiwan_official.py)、[`test_taiwan_official_events.py`](../tests/test_taiwan_official_events.py)、[`test_taiwan_official_event_workflow.py`](../tests/test_taiwan_official_event_workflow.py) |
| TWD-06 | 接受政策限成功且有效、支援、已規劃日期 query。沒有對應程式狀態轉換；呼叫端不能把所有 `empty_unverified` 自動統一為確認無資料。父摘要仍有事件時，空 child 不會刪父事件，missing financial fields 保留。 | **政策目前未由程式落實；沒有政策實作測試。** Parser 測試只涵蓋既有 envelope 行為，不能引用作政策自動化驗收。 |
| TWD-12 | `sources.request` 對 FinMind live default transport 拒絕；official workflow／reports 只處理 official-v1 範圍，保留 legacy records 不等於啟用。 | [`test_envelope_fail_closed_and_finmind_transport_guard`](../tests/test_taiwan_official.py)、[`test_resume_leaves_legacy_finmind_task_and_raw_untouched`](../tests/test_taiwan_official_cli.py) |

### 來源拒絕及重試數值

`official.request` timeout 預設 20 秒、最大 body 64 MiB。workflow 每來源最少 2 秒節流；每 task/run 最多 3 次嘗試，僅特定連線／HTTP 5xx 條件可有限重試，退避 2、4 秒。403/captcha 形成永久 source block；429 至少一小時，若 `Retry-After` 較長則採較長時間。部分／不完整 body 不能以空資料完成。`data.gov.tw` 不在 `official.URLS`，無 retry 或繞行實作；不把封鎖當作改用其他 URL 的授權。FinMind live request 不讀 token，亦不屬官方報告。

### 來源契約回歸案例索引

下列為可從契約追查的既存 regression coverage references。

| 契約面向 | 測試函式及檔案 |
|---|---|
| 來源 allowlist、精確 query | [`test_all_dataset_urls_use_fake_transport_and_exact_contracts`](../tests/test_taiwan_official.py)、[`test_market_contracts_and_exact_normalization`](../tests/test_taiwan_official.py) |
| 行情日期／schema 拒絕 | [`test_actual_date_and_quote_schema_validation`](../tests/test_taiwan_official.py) |
| 空 envelope scope/refusal | [`test_twse_unverified_empty_period_envelope_exact_contract`](../tests/test_taiwan_official_events.py)、[`test_empty_period_envelope_scope_and_refusal_precedence`](../tests/test_taiwan_official_events.py) |
| Detail fields、索引與來源衝突 | [`test_four_detail_shapes_and_price_only_change_detail`](../tests/test_taiwan_official_events.py)、[`test_detail_alias_candidates_preserve_conflicts_and_decimal_equivalence`](../tests/test_taiwan_official_events.py) |
| Task 原子關聯與中斷恢復 | [`test_event_transaction_rolls_back_parent_children_and_links_then_resumes`](../tests/test_taiwan_official_event_workflow.py)、[`test_sigkill_after_parent_commit_resume_keeps_child_without_refetch`](../tests/test_taiwan_official_event_workflow.py) |
| 來源重試／secrets／stop | [`test_5xx_retry_saves_each_invalid_attempt_and_stops_after_three`](../tests/test_taiwan_limits.py)、[`test_envelope_quota_block_and_transport_secret_redaction`](../tests/test_taiwan_sources.py) |
| 週期維護及 no-op | [`test_maintenance_resumes_active_cycle_and_same_day_is_zero_http`](../tests/test_taiwan_maintenance.py)、[`test_maintenance_reuses_detail_until_ttl_then_refreshes_changed_and_expired`](../tests/test_taiwan_maintenance.py) |
| Scheduler 拒絕覆寫／lane | [`test_refuse_conflict_without_partial_write`](../tests/test_taiwan_schedule.py)、[`test_event_and_all_lanes`](../tests/test_taiwan_schedule.py) |
| WAL 一致性／readonly | [`test_read_is_single_readonly_transaction_during_concurrent_wal_refresh`](../tests/test_taiwan_corporate_action_read_reliability.py)、[`test_live_wal_reader_sees_committed_edition_without_checkpoint`](../tests/test_taiwan_corporate_action_read_reliability.py) |
| Decimal／精確容差 | [`test_exact_par_tolerance_boundaries`](../tests/test_taiwan_corporate_action_checks.py)、[`test_exact_reference_tolerance_for_large_values_and_decimal_boundary`](../tests/test_taiwan_corporate_action_checks.py)、[`test_external_decimal_traps_and_flags_are_isolated`](../tests/test_taiwan_corporate_action_checks.py) |

## 3. 解析與欄位語意

- 行情日期僅在日期欄存在且契約要求時須與請求一致；recognized quote table 驗證必要欄位、row width、重複 key 與可得筆數；response-date equality 僅於存在且契約要求時檢查。recognized undated empty branch 保留 `actual_date=None`。parser 可略過非 quote tables，並可能保留額外欄位，不是拒絕所有未知欄位。跨 venue 表依來源原表保存，不預先剔除非普通股。
- 價量欄位採來源明示名稱。TPEx `成交股數` 原值按股，`flagField=千股` 不足以覆蓋欄名；金額按元。placeholder 轉 null 並保留品質旗標，未知數字格式拒絕。不得用代碼四位或名稱推 classification。
- 月摘要日期／區間必須與 request 相容；有 totalCount 時核對。detail request 依摘要索引，不透過普遍日期推測製造任務。明細不含事件日期時由確切 parent link 提供上下文，但原始記錄 event_date 不被改寫。
- `NA`／不適用／缺值不是 0；來源最近財報資料不具事件當時可知的 PIT 語意。來源原文、正規化值、raw provenance 和 quality flags 一併保留。

## 4. 任務、狀態與維護

`sync-events` 將指定日期閉區間按月切割，再對兩市場的三類摘要資料集規劃任務；`max-tasks` 計 task，非 HTTP 次數。任務與快照是持久狀態，失敗、阻擋、pending、warning 均可追查；run 完成不代表內容完整。來源鎖不在整批下載期間持有，writer lock 不跨網路等待。

`maintain-events` 程式預設 `recent_months=2`、`detail_refresh_days=7`：兩個固定曆月包括當月（非最近兩個完整月）；較舊 scope 僅從已規劃任務中輪巡；明細 TTL 7 天。相同 as-of 日期與政策已完成時回 no-op；近期 scope 固定月初至月末，可能含未生效的預告事件，不保證事件當時可得。`sync-events --refresh` 是明確 refresh，語意不同。允許輸入 months 1–12 與 TTL 1–365 天，CLI/scheduler 的預設與呼叫參見 [`official_workflow.py`](../src/quant_research/taiwan/official_workflow.py)、[`cli.py`](../src/quant_research/taiwan/cli.py)。

| ID | 設計／符號 | 測試 |
|---|---|---|
| TWD-03 | `OfficialWorkflow` runner recovery、idempotence、source block、resume | [`test_taiwan_official_workflow.py`](../tests/test_taiwan_official_workflow.py)、[`test_taiwan_maintenance_recovery.py`](../tests/test_taiwan_maintenance_recovery.py) |
| TWD-10 | `maintain_events`、`sync_events`、`cli.maintain-events` 固定 defaults/no-op/refresh | [`test_taiwan_maintenance.py`](../tests/test_taiwan_maintenance.py)、[`test_taiwan_maintenance_cli.py`](../tests/test_taiwan_maintenance_cli.py)、[`test_taiwan_official_event_cli.py`](../tests/test_taiwan_official_event_cli.py) |
| TWD-11 | `schedule.render_units/write_units` 產生 units；quotes timer 20:30、events 20:45（Asia/Taipei），Persistent、RandomizedDelaySec=5m；events max tasks 500。 | [`test_taiwan_schedule.py`](../tests/test_taiwan_schedule.py) |

`write_units(directory, lane=quotes|events|all)` 建立 unit 檔且拒絕覆寫內容不同的既有檔；不呼叫 systemctl、不安裝／啟用／啟動。其 unit 執行的命令會寫入指定資料 root，故產生 unit 不是生產操作授權。

## 5. 報告及唯讀一致性

`TaiwanData.corporate_actions` 負責唯讀連線、`BEGIN` 及 finally close；`reports._open` 開啟觀察股池／可用性報告的唯讀交易，由 `availability` 將同一連線傳給 `storage._corporate_actions_read(db, ...)`。該 helper 不自行連線或開始交易。兩者需能讀取 live WAL 已提交內容；SQLite 自行管理 WAL/SHM 合法。這不保證程式目錄沒有任何 OS 層側檔，也不意味所有其他 legacy/simple SQL reader 都使用同一 transaction。不能用 `immutable=1` 取代 live DB 讀取；不能手動刪除 WAL/SHM。

`observation_universe` 是保存行情觀察到的符號集合；`availability` 是已規劃及已保存 scopes 的狀態摘要。它們都不是全市場對帳、交易日曆證明或普通股 classification。固定時間語意為 `latest_stored_not_point_in_time`，`coverage_complete=false`、`research_ready=false`。

| ID | 設計／符號 | 測試 |
|---|---|---|
| TWD-07 | `reports.observation_universe`、`reports.availability`、storage report selectors | [`test_taiwan_reports.py`](../tests/test_taiwan_reports.py)、[`test_taiwan_report_events.py`](../tests/test_taiwan_report_events.py)、[`test_taiwan_maintenance_cli.py`](../tests/test_taiwan_maintenance_cli.py) |
| TWD-09 | `TaiwanData.corporate_actions` 管理自己的 `mode=ro` 連線／交易；`reports._open` 開啟報告交易，`availability` 在同一交易呼叫 `_corporate_actions_read`；helper 不另建連線或執行 BEGIN，允許 SQLite 管理 WAL/SHM。 | [`test_taiwan_corporate_action_read_reliability.py`](../tests/test_taiwan_corporate_action_read_reliability.py)，包含 live WAL 讀取場景。 |

## 6. 公司行動候選整理與保守財務檢查

`corporate_actions.build_corporate_actions` 是純整理器，輸入 observations、精確 links、tasks；候選識別不代表永久證券身份。只接 official-v1/events 資料。summary 至 child 必須由 `parent_task_id`、`parent_snapshot_id`、`child_task_id` 精確連結；校驗 source/dataset/context、request params、狀態及 snapshot。缺 link、失效 child、orphan detail、舊 edition、摘要多版本衝突均作 diagnostics，不能推測補接。不同 parent 的不同 child 結果列衝突。

| ID | 實作約束 | 測試 |
|---|---|---|
| TWD-04 | exact summary-child links；TWT49U→TWT49UDetail、TWTAUU→TWTAVUDetail；B7 index 僅顯式索引，TWTB8U 不推 FILE_DATE。 | [`test_taiwan_corporate_action_links.py`](../tests/test_taiwan_corporate_action_links.py)、[`test_taiwan_official_event_workflow.py`](../tests/test_taiwan_official_event_workflow.py) |
| TWD-08 | Decimal 候選值、conflicts、missing fields、reference check；始終 `research_ready=false`。 | [`test_taiwan_corporate_action_checks.py`](../tests/test_taiwan_corporate_action_checks.py)、[`test_taiwan_corporate_actions.py`](../tests/test_taiwan_corporate_actions.py)、[`test_taiwan_corporate_action_cli.py`](../tests/test_taiwan_corporate_action_cli.py) |

### 金融值與來源欄位

候選可含 `cash_dividend_per_share`、`bonus_share_ratio`、`share_multiplier`、`cash_return_per_share`、`par_before/after`、`before_price`、`reference_price` 及日期／種類。值維持 Decimal 語意，輸出字串；NA 為 null。每欄多個現行觀察不一致則該欄 null 並列 conflict，不以任意版本覆蓋。同來源／代碼／日期／類型的重複觀察只作一個候選，不累加現金或倍率；不同 event type 分開。最近嘗試與最近可用快照分開呈現，舊值若被保留會標示 stale／歷史版本，不將其冒充最新完整值；最新版未再觀察到不代表正式取消。

- 每仟股無償配股數轉為每股比例（除 1000）；每仟股換發數轉為剩餘倍率（除 1000）；每股換發直接為倍率。整理層不重複轉換。
- 缺少原始必需欄位不以行情反推；來源參考價保留 `reference_price_kind`，不當成實際收益或普遍定價規則。
- 參考價簡式僅是 conditional diagnostic：股利 `(P-c)/(1+b)`、減資 `(P-return-c)/m`、面額變更 `P/m`。只有必需欄齊、來源參考價種類相符，且無衝突／stale／unverified／同日多類型等阻礙時才檢查；其他情況 `not_checked` 並列原因。標示 match 只代表簡式數值相容，不代表事件完整或投資報酬正確。
- Decimal 限制：金融數值不以 float 運算；純整理使用獨立 512 位 Decimal context，不繼承或污染呼叫端 context。單一數值係數最多 64 位，十進指數絕對值最多 64；超限標無效，不展開巨大指數。缺值不轉零，負現金／配股比例及非正倍率、面額、價格標 invalid。
- 參考價以理論值 `N/D`、`D>0` 為基礎，使用 `abs(N - observed * D) <= 0.01 * D` 的精確有限乘減判定，不先除法再比較近似值。0.01 元只用於條件式診斷，不是全市場捨入政策。
- 面額比對僅在直接倍率及兩個面額均有值時檢查，使用 `abs(par_before - share_multiplier * par_after) <= abs(par_before) * 1e-8` 的精確有限乘減判定；不以面額比補倍率，也不以除法近似值判斷容差。
- 核對前必須具備該事件類型必需的 source/symbol/event_date、金融值及來源定義相符的官方參考價；缺值、衝突、stale/unverified、同日多事件、非零未建模增資或面額一致性錯誤均阻止正常完成／核對。`NA` 不等於 0；未知嵌入成分不略過。現金增資／員工增資或其他未建模複合成分會阻止簡式檢查，不由缺欄推出零。

## 7. CLI 命令及副作用

CLI entry point 為 `quant-data` / `python -m quant_research.taiwan`。`status`、`observation-universe`、`availability`、`event-links`、`event-coverage`、`corporate-actions` 是報告讀取用途；root 不存在時唯讀報告不應創建生產 DB。`sync`、`backfill`、`sync-events`、`resume`、`maintain-events` 會規劃／寫入 tasks、runs、snapshots 或 raw；須先人工檢查 root、日期與範圍，不能把下列範例當成已授權執行：

```sh
# 範例命令，會寫入所指定資料根目錄；執行前須另有明確授權
uv run --locked quant-data --root ~/.local/share/quant-research/taiwan sync-events --start 2026-10-01 --end 2026-10-22 --markets twse tpex
uv run --locked quant-data --root ~/.local/share/quant-research/taiwan resume --lane events
```

唯讀簡例與開發檢查見 [`README.md`](../README.md)。任何命令都不授權 production 全期回補、開啟 timer、刪檔或改寫外部 evidence。

## 8. 資料集契約索引

以下列出 `official.URLS` 中的實際入口；`_params` 對各項只允許明確列出的輸入欄位，來源 URL 不接受使用者自由指定。所有 response 都先保留 raw，再進入 parser。

| 市場 | Dataset | 用途 | 請求欄位 |
|---|---|---|---|
| TWSE | `MI_INDEX` | 日行情 | `date`；固定 `type=ALLBUT0999`、`response=json` |
| TPEx | `dailyQuotes` | 日行情 | `date`；固定 `id=EW`、`response=json` |
| TWSE | `TWT49U` | 除權息月摘要 | `start_date`, `end_date` |
| TWSE | `TWTAUU` | 減資月摘要 | `start_date`, `end_date` |
| TWSE | `TWTB8U` | 面額／暫停恢復交易月摘要 | `start_date`, `end_date` |
| TPEx | `exDailyQ` | 除權息月摘要 | `start_date`, `end_date` |
| TPEx | `revivt` | 減資月摘要 | `start_date`, `end_date` |
| TPEx | `pvChgRslt` | 面額變更月摘要 | `start_date`, `end_date` |
| TWSE | `TWT49UDetail` | 除權息詳細條件 | `symbol`, `date` |
| TWSE | `TWTAVUDetail` | 減資詳細條件 | `symbol`, `file_date` |
| TWSE | `TWTB8UDetail` | 面額／交易詳細價格 | `symbol`, `stop_date`, `resume_date` |
| TWSE | `TWTB7UDetail` | 面額變更詳細條件 | `symbol`, `file_date`；僅顯式索引可規劃 |

`official._params` 接收嚴格 ISO 日期，再按來源契約格式化：TWSE 一般日期為 YYYYMMDD，TPEx 為 YYYY/MM/DD；不將此驗證契約概括到所有簡單讀取 CLI。事件期間以日曆月規劃並與指定閉區間相交；這是任務分區方式，不代表來源對期間完整性已被證明。

## 9. 任務狀態與 reader 行為

持久任務、run、snapshot、record 及 event link 的主要來源為 `storage.SCHEMA`。典型流程為：先持久 raw，再解析／保存 snapshot 與 records，最後更新 task/run 狀態。無法解析時保留 raw 與無效快照／失敗原因；被拒絕時記錄 source block，不能把 refusal 簡化為 empty。中斷恢復只接手符合 lane/version 的 pending 或可恢復任務；已完成任務不因普通重跑而覆寫，refresh 使用新 edition。

`empty_unverified` 代表解析器依有限契約辨識到空回應，並非來源全面性證明。事件 workflow 對 `period_envelope_unverified` 非空回應保留資料及警告，但 run 仍可呈 partial；成功完成 task 不會移除 period warning。事件父列即使 child 為空仍有 parent record/link；財務候選整理輸出 missing fields 和診斷，不把空 child 解讀成取消事件。

| 任務／證據狀況 | 解讀限制 |
|---|---|
| `completed`、snapshot valid | 代表該 task 已完成解析／保存，不代表整個市場或金融欄位完整。 |
| `empty_unverified` | 有明確空 envelope 證據；不代表假日、無事件或全域不存在。 |
| warning / `period_envelope_unverified` | 保留取得內容與警告；非空期間完整性仍需查證。 |
| `failed`、refused、source blocked | 不可作 no-data；遵守持久封鎖及明確等待邊界。 |
| pending / interrupted / stale | 保留供 resume；不回報為成功完整。 |

## 10. 報告與 CLI 範圍

| CLI | 用途 | I/O／安全語意 |
|---|---|---|
| `status` | runs/tasks/lock 狀態 | 讀取；不代表資料完整。 |
| `observation-universe` | 已觀察行情符號 | 讀取，分類未知，不等於普通股清單。 |
| `availability --start --end` | 已規劃／已保存 scope 的狀態 | 讀取，不能推論未規劃日期。 |
| `event-links` | 父摘要與確切 child lineage | 讀取，不改寫 child 原始日期。 |
| `event-coverage` | 已規劃 events scopes 的 coverage 摘要 | 讀取，只統計規劃範圍，固定非 complete/non-ready。 |
| `corporate-actions` | 保守事件候選與診斷 | 讀取，不是金融事件帳本或報酬資料。 |
| `sync`, `backfill`, `sync-events`, `resume` | 行情／事件取得及續接 | 寫 tasks/runs/raw/snapshots；需核對 root 與授權。 |
| `maintain-events` | 近期事件維護和既有 scope 輪巡 | 寫入；預設與 no-op 語意見第 4 節。 |
| `write-schedule` | 建立 systemd unit 檔 | 寫指定目錄，但不執行 systemctl。 |

僅 `observation_universe`、`availability`、`corporate_actions` 的來源／日期參數在讀取 root 前驗證；不概括至 simple／legacy API/CLI。預設 root 為 `~/.local/share/quant-research/taiwan/`；root 缺失時唯讀報告回空／錯誤結果而不建 DB。不要將 CLI 範例當作新的資料取得授權。

## 11. API、儲存邊界與報告契約

儲存邊界為 `<root>/state.sqlite3`；raw 路徑為 `<root>/raw/<source>/<YYYYMMDD>/<uuid>.bin`，snapshot 保存 raw path/hash 及請求／dataset provenance。主要表包括 `settings`、`source_blocks`、`request_log`、`runs`、`tasks`、`snapshots`、`records` 及事件 links；raw 為 append-only，不自動刪除。此列為主要操作面，不取代 schema 原始定義。

Python 唯讀用法：

```python
from pathlib import Path
from quant_research.taiwan.storage import TaiwanData

data = TaiwanData(Path("~/.local/share/quant-research/taiwan").expanduser())
universe = data.observation_universe("2026-10-22", source="official")
availability = data.availability("2026-10-01", "2026-10-22", source="official")
actions = data.corporate_actions(source="official")
```

availability 僅接受最多 366 日的閉區間；來源為 `official`、`twse`、`tpex`。觀察日期必填，limit 預設 20000、最大 100000、offset 非負；分頁 fingerprint 可檢測頁間資料集變動。對各 logical scope，先由 newest edition 決定 scope 狀態，再做日期篩選；failed、pending、empty 狀態不會復活較舊 edition。acquisition state 僅為 `available`、`empty_unverified`、`invalid`、`unverified`、`pending`、`running`、`failed`；日期彙總 state 為 `unplanned`、上述單一狀態或 `mixed`。報告呈現 unplanned gaps 與 `source_blocks`。stored event JSON 無效時 availability 的 `financial_conditions` 為 null 並附 error；報告 exit 0 不代表完整性成功。

CLI reader 可用 `quotes`、`events`、`symbols`；`quotes` 必填 `--symbol` 與 `--source`，`events --include-unverified` 納入期間未驗證觀察，`event-links --include-history` 顯示歷史關聯。`sync` 若已有 `official_quotes_start` 設定，會從該日起規劃至 `--expected-date`（預設台北今日）；無設定時只規劃目標日。它不是唯讀，也不受過往下載截止日限制；其他寫入命令各依自己的日期／lane 參數規劃。

CLI exit code：failed／blocked／partial／interrupted 回傳 1，無效參數 2，busy 75。此處不表示資料完整或可研究。

## 12. 明確未實作／政策差異

- TWD-06 空回應結案為文件政策，不是 parser/storage/API 自動 relabel，不存在該新政策專用測試或 migration。既有 `empty_unverified` 不變； envelope 不確定性可在窄政策下接受但不新增日期；非空期間警告仍 unresolved。
- 現有工具未證實全市場完整、普通股身份、PIT、合法市場期間或完整公司行動帳本；未交付調整 OHLC、total return、回測、股票退出／再投資／現金持倉模型。
- 2019、2020 的歷史執行證據不充分，既有 acquisition 邊界不可升級成歷史流程 blanket acceptance。
