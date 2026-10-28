# quant-research

建立量化研究基礎的 Python 專案，長期涵蓋台股、美股及加密資產；現有功能包括台股官方行情／公司行動工作流、唯讀報告，以及離線 Taiwan Quant Dataset 內部計算核心（原價與事件後換股調整）。這不是完整普通股歷史資料庫、交易所認證還原價、PIT 資料或已驗證回測。現況與界線見[專案需求](docs/project-requirements.md)及[專案設計](docs/project-design.md)；台股需求與實作映射見[台股需求](docs/taiwan-equity-data-requirements.md)及[台股設計](docs/taiwan-equity-data-design.md)。

按既有記錄，planned acquisition 範圍 2018-01-01 至 2026-10-22 已完成；不是 live DB 查詢或完整性證明，且 2019/2020 歷史執行證據仍有缺口。觀察股池分類未知；報告不承諾全市場 coverage 或 research readiness。Timer 產生器只寫 unit，不安裝或啟用。

## Taiwan Quant Dataset

內部核心唯讀使用既有官方資料，每個來源市場＋股票代碼由第一筆行情以倍率 1 開始；減資／面額換股生效後累乘倍率，只調整當日及後續 OHLC。現金股利、股票股利／無償配股、退還股款另行記錄，不改價格；可選現金金額缺值為零，解析錯誤明確失敗。

已刪除舊版 bundle、reader/export、版本 manifest、coverage／區間阻擋系統與 `build-dataset`／`export-dataset` 入口。對外 Python API、資料格式及匯出方式待使用情境確認；不另提供替代 CLI，不改官方下載流程、來源 DB 或既有報告。修訂範圍、算法及驗收見[需求](docs/taiwan-quant-dataset-requirements.md)、[設計](docs/taiwan-quant-dataset-design.md)與[使用界線](docs/taiwan-quant-dataset-usage.md)。

## 環境與安裝

需要 Python 3.13 及 [uv](https://docs.astral.sh/uv/)。若缺少 Python 可執行 `uv python install 3.13`。不要因下載或依賴解析失敗而切換 Python 版本或略過錯誤。

```sh
uv sync --locked --group dev
uv run --locked ruff check .
uv run --locked ruff format --check .
uv run --locked mypy
uv run --locked pytest
uv build --no-sources
```

套件設定、Python 範圍與 CLI entry point 見 [`pyproject.toml`](pyproject.toml)；CI workflow 見 [`.github/workflows/ci.yml`](.github/workflows/ci.yml)。

## 安全操作

預設台股資料根目錄為 `~/.local/share/quant-research/taiwan/`，可透過 `--root` 覆寫。互動式 CLI 不會自動強制既有資料目錄或 SQLite 檔案的權限；在共用主機上，首次建立資料前應於執行命令的 shell 設定 `umask 077`，並使用僅自己可存取的資料根目錄。此設定不會收緊既有檔案／目錄的權限；既有部署需另行檢查擁有者、權限及 ACL。產生的 systemd service 已設定 `UMask=0077`。下列報告命令為讀取用途；缺少 root/DB 時不應建立 DB：

```sh
uv run --locked quant-data --root ~/.local/share/quant-research/taiwan status
uv run --locked quant-data --root ~/.local/share/quant-research/taiwan observation-universe --date 2026-10-22 --source official
uv run --locked quant-data --root ~/.local/share/quant-research/taiwan availability --start 2026-10-01 --end 2026-10-22
uv run --locked quant-data --root ~/.local/share/quant-research/taiwan corporate-actions
```

`sync`、`backfill`、`sync-events`、`resume`、`maintain-events` 會寫 DB/raw；執行前先檢查 root、scope 並取得明確授權。`write-schedule` 會寫 unit 檔案但不安裝或啟用 systemd timer。raw 不自動刪除；勿手動刪除 live SQLite `-wal`／`-shm`，唯讀 API 允許 SQLite 正常管理這些 sidecar。FinMind live request 預設停用；不要提供或硬編碼 token。沒有選定開源授權。
