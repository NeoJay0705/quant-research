# 專案指引

## 技術與範圍
- Python 3.13、uv、`src/quant_research` typed src layout；專案設定與鎖定品質檢查以 [`pyproject.toml`](pyproject.toml) 及 [CI](.github/workflows/ci.yml) 為準。
- 長期目標含台股、美股與加密資產；目前僅台股官方行情／事件資料工作流及報告已實作。不要把觀察池／候選事件描述為完整普通股歷史、完整 coverage、PIT、調整價格或研究就緒。詳見四份[需求](docs/project-requirements.md)、[專案設計](docs/project-design.md)、[台股需求](docs/taiwan-equity-data-requirements.md)、[台股設計](docs/taiwan-equity-data-design.md)。
- 台股現有取得只用固定 TWSE／TPEx allowlist；FinMind 新 live request 停用，舊任務與 raw 保留且不混入官方報告。空資料 DB/API 保留 `empty_unverified`；文件層有限結案政策不等於程式 relabel。

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
