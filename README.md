# quant-research

建立可重現量化研究基礎的 Python 專案，長期涵蓋台股、美股及加密資產；目前只有台股官方行情／公司行動取得、維護及唯讀報告工具。它不代表完整普通股歷史資料庫、PIT 資料、調整價格或已驗證回測。現況與界線見[專案需求](docs/project-requirements.md)及[專案設計](docs/project-design.md)；台股需求與實作映射見[台股需求](docs/taiwan-equity-data-requirements.md)及[台股設計](docs/taiwan-equity-data-design.md)。

按既有記錄，planned acquisition 範圍 2018-01-01 至 2026-10-22 已完成；不是 live DB 查詢或完整性證明，且 2019/2020 歷史執行證據仍有缺口。觀察股池分類未知；報告不承諾全市場 coverage 或 research readiness。Timer 產生器只寫 unit，不安裝或啟用。

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
