# 台股資料需求與接受界線

本文件只描述台股當前需求、現況與未交付事項；實作細節見[台股設計](taiwan-equity-data-design.md)。它取代舊有分階段計畫與操作／驗收日誌，不是新增執行授權。

## 目的與現況

長期用途是支援個人量化研究：上市及上櫃普通股、歷史退市標的、自 2018 年起日線行情，以及除權息、減資、面額變更等公司行動。理想行情欄位為開高低收、成交股數、成交金額。現行程式取得 TWSE/TPEx 官方資料，保留來源紀錄並提供任務接續、維護、觀察／可用性報告及事件候選整理；不是上述需求的完整研究資料庫。

按既有取得紀錄，planned acquisition 範圍 2018-01-01 至 2026-10-22 已完成。這只是一條既有記錄邊界，不是即時 DB 狀態查詢或歷史流程全面驗收。2019、2020 年歷史執行證據缺口仍在，不能宣稱所有歷史批次證據完整。此處不保留年別數量、hash、run log 或操作日誌。

## 穩定需求與設計／測試映射

| ID | 需求 | 實作對照與實際測試 |
|---|---|---|
| TWD-01 | 官方來源、dataset、參數必須 allowlist；不得接受任意 URL。FinMind 新 live 請求停用，不納入 official path。 | [`official.URLS/_params/request`](../src/quant_research/taiwan/official.py)、[`sources.request`](../src/quant_research/taiwan/sources.py)；[`test_taiwan_official.py`](../tests/test_taiwan_official.py)、[`test_taiwan_official_cli.py`](../tests/test_taiwan_official_cli.py)、[`test_taiwan_limits.py`](../tests/test_taiwan_limits.py)。 |
| TWD-02 | 保留 raw、快照、任務、records 及來源血緣；raw 不因重新取得而覆寫，舊來源資料不自動清除。 | [`SQLiteStore.save_raw/save_snapshot`、`SCHEMA`](../src/quant_research/taiwan/storage.py)；[`test_taiwan_storage.py`](../tests/test_taiwan_storage.py)、[`test_taiwan_recovery.py`](../tests/test_taiwan_recovery.py)。 |
| TWD-03 | 工作可續接、scope 冪等；refresh 建新 edition；錯誤、封鎖、pending、failed 等狀態須保留，不能假報成功。 | [`OfficialWorkflow.sync_events/_execute`](../src/quant_research/taiwan/official_workflow.py)、[`Workflow`](../src/quant_research/taiwan/workflow.py)；[`test_taiwan_official_workflow.py`](../tests/test_taiwan_official_workflow.py)、[`test_taiwan_maintenance_recovery.py`](../tests/test_taiwan_maintenance_recovery.py)。 |
| TWD-04 | 月摘要事件以 parser 提供之精確 detail request 建立子任務；links 固定 parent task/snapshot 到確切 child task，不因刷新偷換關聯。 | [`official_workflow.py`](../src/quant_research/taiwan/official_workflow.py)、[`corporate_actions.build_corporate_actions`](../src/quant_research/taiwan/corporate_actions.py)；[`test_taiwan_official_event_workflow.py`](../tests/test_taiwan_official_event_workflow.py)、[`test_taiwan_corporate_action_links.py`](../tests/test_taiwan_corporate_action_links.py)。 |
| TWD-05 | Parser 只辨識有明確契約的正常空回應；資料庫/API 保留 `empty_unverified` 與警告，不將之當成休市或事件不存在證明。 | [`official.parse`](../src/quant_research/taiwan/official.py)；[`test_taiwan_official.py`](../tests/test_taiwan_official.py)、[`test_taiwan_official_events.py`](../tests/test_taiwan_official_events.py)、[`test_taiwan_official_event_workflow.py`](../tests/test_taiwan_official_event_workflow.py)。 |
| TWD-06 | 文件接受政策允許成功、有效、已規劃之支援日期 query 的明確認可空回應結束該次取得；錯誤／拒絕／截斷／未知或矛盾 envelope／未規劃 scope 不可作 no-data。 | **政策而非實作**：沒有自動 relabel、DB 狀態或遷移，也沒有直接實作此新政策的測試。TWD-05 所列測試只驗 parser 與現存狀態。Envelope 警告仍可能保留；不可補造日期，非空期間警告仍 unresolved。 |
| TWD-07 | 觀察股池與 availability 是已存證據視圖，不代表普通股法律身份、全市場完整、PIT 或研究就緒。 | [`reports.observation_universe/availability`](../src/quant_research/taiwan/reports.py)、[`storage.TaiwanData`](../src/quant_research/taiwan/storage.py)；[`test_taiwan_reports.py`](../tests/test_taiwan_reports.py)、[`test_taiwan_report_events.py`](../tests/test_taiwan_report_events.py)、[`test_taiwan_maintenance_cli.py`](../tests/test_taiwan_maintenance_cli.py)。 |
| TWD-08 | 公司行動輸出為候選，不等於完整事件或報酬序列；缺值不得以零、價格反推或重複套用補足。 | [`corporate_actions.build_corporate_actions`](../src/quant_research/taiwan/corporate_actions.py)；[`test_taiwan_corporate_action_checks.py`](../tests/test_taiwan_corporate_action_checks.py)、[`test_taiwan_corporate_actions.py`](../tests/test_taiwan_corporate_actions.py)。 |
| TWD-09 | 一致唯讀報告 API 需看見 live WAL 中已提交狀態，允許 SQLite 管理 WAL/SHM；不得寫 business data、migration 或用 immutable 忽略 WAL。 | `TaiwanData.corporate_actions` 負責唯讀連線、`BEGIN`、finally close；`reports._open` 負責 availability/universe 連線與 transaction，availability 把同一 db 傳給 `storage._corporate_actions_read(db, ...)`，helper 不連線、不 `BEGIN`；[`test_taiwan_corporate_action_read_reliability.py`](../tests/test_taiwan_corporate_action_read_reliability.py)。簡單／legacy 查詢不作概括。 |
| TWD-10 | Event maintenance 預設最近兩個固定曆月（含本月）、已規劃較舊 scope 輪巡、detail TTL 7 日；同日同政策 no-op，明確 refresh 仍可刷新。 | [`OfficialWorkflow.maintain_events/sync_events`](../src/quant_research/taiwan/official_workflow.py)、[`cli.maintain-events`](../src/quant_research/taiwan/cli.py)；[`test_taiwan_maintenance.py`](../tests/test_taiwan_maintenance.py)、[`test_taiwan_maintenance_cli.py`](../tests/test_taiwan_maintenance_cli.py)、[`test_taiwan_official_event_cli.py`](../tests/test_taiwan_official_event_cli.py)。 |
| TWD-11 | Scheduler 僅產生 quotes/events/all 的 systemd unit，不執行 systemctl、不安裝或啟用。 | [`schedule.write_units/render_units`](../src/quant_research/taiwan/schedule.py)、[`cli.main`](../src/quant_research/taiwan/cli.py)；[`test_taiwan_schedule.py`](../tests/test_taiwan_schedule.py)。 |
| TWD-12 | FinMind live 預設 transport 被拒；舊任務/raw 可保留，official reports/resume 不得把其視為官方證據。 | [`sources.request`](../src/quant_research/taiwan/sources.py)、官方來源篩選 [`reports.py`](../src/quant_research/taiwan/reports.py)；代表回歸覆蓋見下表。 |

## 回歸覆蓋索引

以下為需求對應的既存測試位置；實際驗收須另以執行結果判定，索引本身不是通過證明。

| ID | 代表性測試 |
|---|---|
| TWD-01 | [`test_all_dataset_urls_use_fake_transport_and_exact_contracts`](../tests/test_taiwan_official.py)、[`test_allowlist_token_header_and_safe_url`](../tests/test_taiwan_sources.py) |
| TWD-02 | [`test_explicit_init_raw_append_only_and_read_only_facade`](../tests/test_taiwan_storage.py)、[`test_snapshot_index_supports_report_record_lookups`](../tests/test_taiwan_storage.py) |
| TWD-03 | [`test_backfill_pauses_and_resumes_without_refetching_completed`](../tests/test_taiwan_workflow.py)、[`test_sigkill_recovers_running_task_and_resumes_only_remaining_tasks`](../tests/test_taiwan_recovery.py) |
| TWD-04 | [`test_dynamic_detail_atomic_max_and_resume_does_not_refetch_parent`](../tests/test_taiwan_official_event_workflow.py)、[`test_global_newer_unlinked_child_is_not_substituted_for_linked_child`](../tests/test_taiwan_corporate_action_links.py) |
| TWD-05 | [`test_twse_known_empty_response_is_unverified_and_fails_closed`](../tests/test_taiwan_official.py)、[`test_empty_period_envelope_scope_and_refusal_precedence`](../tests/test_taiwan_official_events.py) |
| TWD-06 | **無政策自動化測試**；僅沿用 TWD-05 parser 測試驗證既存 empty envelope，不驗證文件新接受政策。 |
| TWD-07 | [`test_observation_universe_uses_real_parser_and_pages_stably`](../tests/test_taiwan_reports.py)、[`test_scopes_intersect_query_and_latest_editions_define_state`](../tests/test_taiwan_report_events.py) |
| TWD-08 | [`test_simple_reference_formulae_and_independent_decimal_context`](../tests/test_taiwan_corporate_action_checks.py)、[`test_same_day_compound_actions_are_separate_and_not_double_applied`](../tests/test_taiwan_corporate_actions.py) |
| TWD-09 | [`test_live_wal_reader_sees_committed_edition_without_checkpoint`](../tests/test_taiwan_corporate_action_read_reliability.py)、[`test_availability_uses_one_wal_read_snapshot_for_events_and_financial_helper`](../tests/test_taiwan_report_events.py) |
| TWD-10 | [`test_fixed_calendar_months_cross_year_and_new_root_never_backfills`](../tests/test_taiwan_maintenance.py)、[`test_maintenance_cold_cursor_rotates_planned_historical_scopes`](../tests/test_taiwan_maintenance.py) |
| TWD-11 | [`test_event_and_all_lanes`](../tests/test_taiwan_schedule.py)、[`test_cli_write_schedule`](../tests/test_taiwan_schedule.py) |
| TWD-12 | [`test_envelope_fail_closed_and_finmind_transport_guard`](../tests/test_taiwan_official.py)、[`test_resume_leaves_legacy_finmind_task_and_raw_untouched`](../tests/test_taiwan_official_cli.py) |

## 來源及資料語意

- 支援來源為固定 TWSE、TPEx adapter。行情資料表可能含 ETF、管理股票或其他商品；保存來源原表與欄位，不能僅憑代碼、名稱或今日分類當作普通股。
- TWSE 行情成交金額以元保存；TPEx 明示「成交股數」欄優先按股解讀，不因 `flagField=千股` 任意再乘千。解析器對欄名／日期／回應筆數做契約核驗，未知或截斷回應應拒絕，而非靜默接受。詳見[設計](taiwan-equity-data-design.md)。
- 公司行動觀察中的 NA、placeholder、未知欄位均非零。來源參考價不自動成為投資報酬或可交易價格。公司財報欄即使被來源帶回，也不能宣稱事件當時已知。
- TWSE 總表到 detail 的連結需保留來源提供的精確 token/參數。TWTB7U 明細索引只限明確索引或顯式補充鍵，不可從停牌日普遍推估日期。缺索引表示未知／缺明細，不表示不存在事件。

## 來源阻擋與重試界線

每來源節流與封鎖由 workflow 管理：至少 2 秒間隔；每任務每 run 最多 3 次請求嘗試，只對可重試的連線／HTTP 5xx 類錯誤有限重試並以 2、4 秒退避。403/captcha 持久阻擋，不自動重試；429 cooldown 至少一小時，尊重更長 `Retry-After`。不完整 response 會被拒絕，不得轉成空結果。這些是實際程式邊界，不授權繞過。

`data.gov.tw` 非目前 official pipeline adapter，不做重試、換入口或繞過存取阻擋；不得將其誤列為可用備援。FinMind 新 live request disabled；不要求或保存 token，也不讀取 token。來源使用條款可能變更，這些文件不構成法律意見或全面授權結論。

## 覆蓋與研究限制

`observation_universe` 按已保存行情構成觀察集合，classification 保持 `unknown`；可用性報告描述已規劃／已保存任務，不可推斷未列出的市場日期或證券不存在。報告固定 `latest_stored_not_point_in_time`、`coverage_complete=false`、`research_ready=false`。歷史掛牌期、存活者偏誤、修訂公告時間、事件全期對帳仍待另行資料與驗收。

目前公司行動整理器只生成保守候選及缺欄／衝突檢查，不能產生還原價、含息報酬、持倉或交易帳務。完整財務驗證尚未交付：現金增資、員工增資、複合事件、跨來源修訂、未知參考價與事件缺漏皆可能使公式不可檢查。沒有任何文檔政策可以把未實作的財務判斷視為通過。

## 事件資料的最低語意要求

候選整理須把來源欄位與衍生欄位分開，並保留 null／missing／conflict／來源 provenance。最低研究欄位目標如下；此表是資料語意需求，不宣稱現有歷史來源對所有事件都提供這些值。

| 事件類別 | 目標欄位 | 不能假設 |
|---|---|---|
| 除權息 | 每股現金股利、每股無償配股比例、除權息前價、官方參考價與類別 | NA 為 0、每仟股數直接當每股數、缺配股數為零。 |
| 減資 | 剩餘股數倍率、現金退還、同一條件中的現金股利、減資前價與官方參考價 | 每仟股換發數是減少比例；摘要價格足以反推出倍率。 |
| 面額變更 | 每股換發倍率、變更前後面額、前價與官方參考價 | 面額比例可取代缺失的來源倍率，或與其他同日事件重複套用。 |
| 停復牌條件 | stop/resume 日期、來源條件及其與摘要的確切關聯 | detail 沒有事件日就能由其他行情報價猜出日期。 |

事件識別只在來源、代碼、日期、事件種類的觀察層級成立，不是永久證券 ID。相同 key 的資料版本可能互相衝突或僅為重複摘要；跨來源不可合併為一個確定身份。不同類型同日出現應各自保留並標示複合風險，不能把個別簡式多次套成總報酬。

即使來源參考價與有限公式相容，也只滿足一項診斷；最低必需欄位、來源 reference-price kind、事件時點、公告修訂、同日其他事件及全期缺漏都影響是否可用。對上述條件沒有完整證據時，輸出必須保留 `not_checked`、`incomplete`、`unverified` 或 `conflict` 等限制，而非自動升格成有效金融事件。

## 接受概念不可混用

| 概念 | 可以接受的陳述 | 不可以推論 |
|---|---|---|
| Acquisition/task | 一個指定來源與日期 scope 的請求已完成／已辨識空回應。 | 該日一定是交易日、該市場資料已齊或所有商品已收錄。 |
| Raw/parser | 保留了原始來源回應，且 parser 接受其已知 schema。 | 官方資料永遠不修訂、parser 已證明來源全量無截斷。 |
| Event link | 某 summary 的精確 detail task/snapshot 有可追蹤連結。 | child 欄位齊全、代表唯一法律事件或可任意套用到其他 summary。 |
| Observation universe | 指定儲存資料中曾出現的市場代碼集合。 | 已下市公司全集、普通股全集、可交易集合或無生存者偏誤。 |
| Availability | 已規劃範圍內的最新保存任務狀態。 | 未規劃範圍不存在缺漏，或所有可用性等於 PIT。 |
| Reference check | 有限條件公式與來源參考價相容。 | 已核定調整因子、精確報酬、股東帳務或回測正確性。 |

## 來源、使用限制與保留

現行官方 adapter 僅有 TWSE／TPEx 固定資料集，並不代表每個歷史端點或每種用途均已完成授權審查。資料來源、開放資料授權、交易所網站條款及可接受自動化方式都須以相應來源當前條款為準；此文件不提供法律意見，不將過去一次成功請求當永久授權。任何需登入、token、付費或未核准存取的來源，不得假設已取得使用權。

官方來源拒絕（HTTP 或 envelope 403、captcha）須停止；429 遵循持久 cooldown；不改換代理、User-Agent、URL 或來源繞過。部分 body、非 JSON、未辨識 schema、totalCount 不符等為失敗／警告，不是空資料。`data.gov.tw` 不是目前可重試的官方 adapter；不得建立未核准的替代抓取路徑。FinMind 舊 raw/task 保留只為歷史血緣，不代表新請求已獲准，也不與官方資料合併。

外部 raw/evidence 根目錄不屬文件重整範圍，不列出逐筆路徑、日誌或雜湊，也不移動、刪除或宣稱已備份。Production 預設 root 為 `~/.local/share/quant-research/taiwan/`；root 可由 CLI 覆寫。Git 忽略規則不是留存、備份或清理政策。原始資料保留期限、完整 backup/restore、資料庫 migration 策略仍需另行核准。

## 待完成

普通股歷史身份與法律期間、全市場 completeness、非空期間 envelope 警告核實、事件 revisions/PIT、完整事件財務方法、調整價格與回測皆屬未實作事項。歷史 2019/2020 執行證據缺口仍需獨立處理。不執行回補、不做 DB/API 狀態重標、不遷移資料、不刪外部 raw/evidence。
