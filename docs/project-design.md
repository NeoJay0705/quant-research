# 專案設計

## 專案基礎

專案採 Python 3.13、uv、`src/quant_research` typed src layout；CLI entry point 為 `quant-data`。套件組態與鎖定品質檢查以 [`pyproject.toml`](../pyproject.toml)、[`uv.lock`](../uv.lock)、[CI workflow](../.github/workflows/ci.yml) 為準。此設計只描述當前基礎與已實作台股工具，不把長期跨市場目標當成交付。

### PRJ 需求對應

| ID | 設計／實作位置 | 測試依據／狀態 |
|---|---|---|
| PRJ-01 | `pyproject.toml`：`requires-python >=3.13,<3.14`、`quant-data`、mypy strict、pytest、ruff；uv 鎖定依賴。 | [`tests/test_package.py`](../tests/test_package.py)；README 列出品質檢查。 |
| PRJ-02 | 台股實作位於 [`src/quant_research/taiwan/`](../src/quant_research/taiwan/)；美股及加密資產沒有對應模組。 | 未實作，無功能測試。 |
| PRJ-03 | [`storage.py`](../src/quant_research/taiwan/storage.py) 管理 DB、raw 及任務證據；沒有自動刪除功能或保留期管理器。 | [`test_taiwan_storage.py`](../tests/test_taiwan_storage.py)、[`test_taiwan_recovery.py`](../tests/test_taiwan_recovery.py)。備份政策未實作。 |
| PRJ-04 | 請求只從固定來源設定發出；import 不觸發請求。 | [`test_taiwan_sources.py`](../tests/test_taiwan_sources.py)、[`test_taiwan_limits.py`](../tests/test_taiwan_limits.py)。 |
| PRJ-05 | 報告以 false readiness／coverage 與未知分類保守呈現。 | [`reports.py`](../src/quant_research/taiwan/reports.py)、[`corporate_actions.py`](../src/quant_research/taiwan/corporate_actions.py)；相關測試於台股設計表。 |

## 台股實作邊界

台股資料層使用固定官方來源 adapter、SQLite 持久化 workflow、CLI、觀察／可用性報告及公司行動候選整理。系統本身不建立普通股法律母集合，不對全市場 completeness、PIT、調整價格或投資報酬作承諾。現有紀錄的 planned retrieval 邊界及未完成事項見[台股需求](taiwan-equity-data-requirements.md)。

### 維運及安全規範

- Production 根目錄預設 `~/.local/share/quant-research/taiwan/`，CLI 可用 `--root` 覆寫。raw 不自動刪除；source-validation 外部資料與 production 根目錄不同，文件不移動或改寫那些資料。
- 唯讀交易邊界：`TaiwanData.corporate_actions` 負責唯讀連線、`BEGIN` 及 finally close；`reports._open` 開啟觀察股池／可用性報告的唯讀交易，由 `availability` 將同一連線傳給 `storage._corporate_actions_read(db, ...)`。該 helper 不自行連線或開始交易；不將此契約概括到其他簡單查詢。SQLite 可管理 live WAL/SHM；不要用 `immutable=1` 或手動刪除側檔。
- `write-schedule` 只寫 unit 檔，不安裝、啟用或啟動 timer。Quotes unit 為台北 20:30、events 為 20:45，皆 `Persistent=true`、最多隨機延遲 5 分鐘；events service 固定 `--max-tasks 500`。產生單位不等於核准生產排程。
- 禁止硬編碼 secrets；來源拒絕或封鎖不得透過替換 URL、UA、代理或非核准來源繞過。
- 本文件不授權新 production 回補、排程啟用、DB migration 或資料刪除。

## 安全操作示例

由專案目錄執行唯讀報告前先確定根目錄。簡要命令：

```sh
uv run --locked quant-data --root ~/.local/share/quant-research/taiwan status
uv run --locked quant-data --root ~/.local/share/quant-research/taiwan observation-universe --date 2026-10-22 --source official
uv run --locked quant-data --root ~/.local/share/quant-research/taiwan availability --start 2026-10-01 --end 2026-10-22
uv run --locked quant-data --root ~/.local/share/quant-research/taiwan corporate-actions
```

讀取狀態及報告不代表資料完整。`sync`、`backfill`、`sync-events`、`resume`、`maintain-events` 會寫入任務／資料；執行前須確認 root、scope、來源與執行授權。`write-schedule` 會建立 unit 檔案，可能寫入指定目錄，但不安裝或啟用。CLI 參數詳見 [`cli.py`](../src/quant_research/taiwan/cli.py)；操作語意與數值預設詳見[台股設計](taiwan-equity-data-design.md)。

## 未來範圍

完整市場身分期間、PIT、事件對帳／財務驗證、回測、跨市場資料、SLA、備份及保留策略均需另行設計與驗收。不得以目前候選事件的局部參考價核對或樣本測試代替上述交付。
