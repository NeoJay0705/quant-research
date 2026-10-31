# 專案指引

## 技術與範圍
- Python 3.13、uv、`src/quant_research` typed src layout；專案設定與鎖定品質檢查以 [`pyproject.toml`](pyproject.toml) 及 [CI](.github/workflows/ci.yml) 為準。
- 長期目標含台股、美股與加密資產；已實作台股官方行情／事件工作流及報告，以及離線 Taiwan Quant Dataset 內部順序計算核心。Dataset 不另指定基準日，換股生效後才調整當日及後續價格；現金／股票股利及退還款只記錄，可選現金缺值為零。舊 bundle、reader/export、版本 manifest、coverage／區間阻擋系統及 dataset CLI 已刪除；對外格式與 API 待確認，不自行恢復。不得宣稱完整普通股歷史、全市場 coverage、PIT、交易所認證價格或已驗證回測；既有報告狀態語意不變。詳見[需求](docs/taiwan-quant-dataset-requirements.md)、[設計](docs/taiwan-quant-dataset-design.md)、[使用界線](docs/taiwan-quant-dataset-usage.md)。其他背景見四份[需求](docs/project-requirements.md)、[專案設計](docs/project-design.md)、[台股需求](docs/taiwan-equity-data-requirements.md)、[台股設計](docs/taiwan-equity-data-design.md)。
- 台股現有取得只用固定 TWSE／TPEx allowlist；FinMind 新 live request 停用，舊任務與 raw 保留且不混入官方報告。空資料 DB/API 保留 `empty_unverified`；文件層有限結案政策不等於程式 relabel。

## 策略研究
- 已確認並實作首版 `quant_research.research` Python介面：`capabilities()`提供功能目錄；共通typed資料、特徵／標籤／切片、下一開盤回測、公司行動帳務、績效／比較及新目錄保存。見[研究需求](docs/robot-first-strategy-research-requirements.md)、[設計／接受紀錄](docs/robot-first-strategy-research-design.md)、[使用方式](docs/robot-first-strategy-research-usage.md)，人工例子在 `examples/research/`。
- 首版只取既有本機TWSE／TPEx明確子集，無UI／下單／新依賴／研究CLI；不恢復舊Dataset發布API。標準函式庫、Decimal50位HALF_EVEN、普通股分類限制與observed-calendar假設不可隱藏。
- 原價帳戶與調整價格分開。股數採直接比例或純面額前後面額比，不用市場股價比猜持股；缺實際付款／新股可用日按已確認事件日模型替代，來源實際日期仍None。手算／真實小樣本只驗收模型與資料串接，不宣稱實盤可實現或完整金融資料認證。
- `save_run`只寫新目錄，POSIX新結果目錄0700／新檔0600，不改全域umask或既有權限；拒production／載入來源／分類輸入及symlink路徑；完整資料寫完才發布COMPLETE。任意Python策略是可信程式，不是沙箱；prefix view不保證任意closure或最新修訂來源的PIT。

## 開發規範
- Import 不可發網路請求、讀市場資料或產生外部副作用；不在程式碼、文件或測試硬編碼憑證。
- 只修改核准範圍，不自行擴大功能／相依套件；修改功能時更新相應測試與文件。不得虛報未執行測試。
- 全部 write CLI 需確認 root/scope/授權；不因 403、captcha、429 或來源未知而換入口、UA、proxy 或繞過限制。`data.gov.tw` 不是官方 pipeline 的 retry/fallback。

## 資料與操作安全
- 預設 production root 為 `~/.local/share/quant-research/taiwan/`，CLI `--root` 可覆寫；raw 不自動刪除。不要未經明確核准刪除 raw、中間產物、DB 或外部 evidence。Git ignore 不是備份或 retention 政策。
- 唯讀報告 API 以 `mode=ro` transaction 讀取；SQLite 可管理 live WAL/SHM。不要用 `immutable=1` 讀 live DB，勿建議刪 WAL/SHM。不要將單一 API 的 transaction 聲明擴張到 legacy simple query。
- `maintain-events` 預設最近兩個固定曆月（含本月）、輪巡已規劃舊 scope、detail TTL 7 日；同日同政策 no-op。`write-schedule --lane quotes|events|all` 僅寫 units：台北 20:30 quotes、20:45 events、最多隨機延遲 5 分鐘；events unit max tasks 500。不得安裝／啟用 timer 或啟動新 production 回補，除非另有明確授權。
- 新增 CLI 或執行建議須清楚標示讀取或寫入行為。不得將取得完成、成功空回應或局部公式核對表述成來源不存在、完整覆蓋或已驗證報酬。

## 標準檢查

```sh
uv sync --locked --group dev
uv run --locked ruff check .
uv run --locked ruff format --check .
uv run --locked mypy
uv run --locked pytest
uv build --no-sources
```

不因 Python 3.13 缺少、依賴解析失敗或檢查錯誤而自行換版本或略過檢查。專案尚未選定開源授權。
