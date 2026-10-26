# 專案需求

## 範圍與現況

本專案長期目標是建立可重現的量化研究基礎，涵蓋台股、美股與加密資產。現有產品能力僅為台灣證券交易所（TWSE）及證券櫃檯買賣中心（TPEx）之官方日行情、公司行動事件原始資料取得、SQLite 持久化與維護，以及唯讀狀態／候選整理工具。美股、加密資產、研究與回測尚未實作。台股現有取得範圍不是完整市場資料庫，也不是研究就緒資料集。

## 穩定需求

| ID | 現行需求與界線 | 狀態 |
|---|---|---|
| PRJ-01 | 使用 Python 3.13、uv、`src/quant_research` typed src layout；依鎖定依賴執行 CI 品質檢查。 | 已實作；[pyproject.toml](../pyproject.toml)、[README](../README.md) |
| PRJ-02 | 長期研究範圍涵蓋台股、美股、加密資產；只有台股來源取得／維護工具已交付。 | 美股、加密資產及研究計算未實作，沒有對應交付測試。 |
| PRJ-03 | 保留來源原始證據及可追溯性；不得擅自刪除原始資料或中間產物。 | 程式不提供自動刪除政策；備份、保留期限、災難復原 SLA 尚未制定。 |
| PRJ-04 | 不硬編碼憑證；Import 不發網路請求、不讀市場資料、不產生外部副作用。 | 專案開發約束。 |
| PRJ-05 | 不宣稱現有資料已分類為普通股、完整覆蓋、無生存者偏誤、point-in-time（PIT）、可回測或具報酬語意。 | `research_ready=false`、`coverage_complete=false` 是報告的保守界線。 |

## 台股使用需求與接受邊界

目標使用情境是上市／上櫃普通股日線與公司行動研究，包含歷史退市標的；非興櫃。行情目標欄位為開、高、低、收、成交股數與成交金額；目前來源回應可能涵蓋其他商品，分類維持未知。公司行動目標含除權息、減資、面額變更等事件，但目前事件輸出只是有來源證據的候選與缺漏診斷，不是已核定的金融事件帳本。

依現有記錄，規劃取得範圍 2018-01-01 至 2026-10-22 已完成；這是既有取得邊界，不是即時資料庫查詢，也不代表逐日有效、完整、普通股專屬或研究驗收通過。2019、2020 的歷史執行證據仍有缺口，不能概括宣稱歷史流程全數驗收。各年度批次筆數、雜湊、run 記錄及操作日誌不屬本需求文件。

### 空結果的文件接受政策

對明確支援、參數有效且已規劃的日期查詢，若成功取得來源認可的正常空回應，可在文件驗收政策上視為該 acquisition query「無資料」並結束該項取得；不等同交易所證明休市、事件取消、合法標的不存在或任何全域不存在結論。回應證據與 DB/API 仍保留 `empty_unverified` 及警告。拒絕（含 403/429）、錯誤、截斷／不完整回應、未知或矛盾 envelope、非支援資料集及未規劃 scope 均不得當作無資料。Envelope 不確定性可能符合上述有限結案政策，但不得補造事件日期；非空期間警告仍未解除。

此為文件層接受政策；程式沒有自動將報告狀態重新標記，也沒有相應 DB 狀態或遷移。沒有為此修改程式或測試。已知父事件即使明細為空，仍保留父事件及缺失金融欄位，不因空明細刪除事件。

## 未交付需求與非目標

- 可驗證的歷史普通股母集合、逐日合法市場期間、分類與退市／轉市場身分鏈。
- 全歷史行情／公司行動對帳、完整性證明、資料修訂的完整 PIT 血緣。
- 經核定的事件財務語意、調整 OHLC、股利再投資、持倉現金帳務、含息報酬、交易成本／滑價、回測。
- 美股及加密資產資料管線；生產 SLA、備份與自動清理策略。

觀察股池只是從保存的行情建立的觀察集合；未知分類不以四碼代碼、名稱、今日 CFI 或舊 FinMind 資料推定普通股。行情出現不等於可交易。近期維護不是全期回補。新 production 執行、全期回補、timer 安裝／啟用及原始資料刪除都不由此文件授權。

## 對應設計與證據索引

| 需求 | 實作對照（細節見 [台股需求](taiwan-equity-data-requirements.md)） | 可查測試 |
|---|---|---|
| PRJ-01 | `pyproject.toml` Python 版本、uv 鎖檔與套件設定 | [`tests/test_package.py`](../tests/test_package.py) |
| PRJ-02 | 未有美股／加密實作；台股根目錄模組 `src/quant_research/taiwan/` | 無對應未實作功能測試 |
| PRJ-03 | `SQLiteStore.save_raw`、schema 與 append-only raw：[`storage.py`](../src/quant_research/taiwan/storage.py) | [`test_taiwan_storage.py`](../tests/test_taiwan_storage.py)、[`test_taiwan_recovery.py`](../tests/test_taiwan_recovery.py) |
| PRJ-04 | 套件模組及來源請求邊界；憑證不寫入程式 | [`test_taiwan_sources.py`](../tests/test_taiwan_sources.py)、[`test_taiwan_limits.py`](../tests/test_taiwan_limits.py) |
| PRJ-05 | `reports.observation_universe`、`reports.availability`、`corporate_actions.build_corporate_actions` | [`test_taiwan_reports.py`](../tests/test_taiwan_reports.py)、[`test_taiwan_corporate_actions.py`](../tests/test_taiwan_corporate_actions.py) |
