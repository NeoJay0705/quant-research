# quant-research

長期目標是建立可重現的量化研究基礎，研究範圍涵蓋台股、美股與加密貨幣。此專案目前只有可安裝的 Python 套件、開發檢查與初始化文件。
它尚無市場資料、資料來源整合、ETL、回測或 CLI，也不宣稱已驗證任何金融功能。

## 前置需求

- Python 3.13。
- [uv](https://docs.astral.sh/uv/) 套件與專案管理工具。

若本機沒有 Python 3.13，可執行 `uv python install 3.13`。若 uv 尚未安裝，請依 uv 官方安裝文件安裝。
不要因為 Python 3.13 缺失而自動改用或升級到其他 Python 版本。若 Python 下載、網路連線或依賴解析失敗，請保留錯誤訊息，檢查網路與 Python／uv 設定，再明確重試；不要略過檢查或改用未鎖定的依賴。

## 安裝與檢查

在專案根目錄執行：

```sh
uv sync --locked --group dev
uv run --locked ruff check .
uv run --locked ruff format --check .
uv run --locked mypy
uv run --locked pytest
uv build --no-sources
```

這些命令會明確回報安裝、Lint、格式、型別、測試或建置錯誤。不要靜默跳過失敗的檢查。
套件匯入測試只確認套件可匯入，以及安裝版本與專案中繼資料一致；它不是金融功能驗證。

## 目錄

- `src/quant_research/`：typed src layout 套件。
- `tests/`：安裝與中繼資料煙霧測試。
- `docs/project-plan.md`：研究專案階段、門檻與待決策事項。
- `.github/workflows/ci.yml`：Pull request 與 push 的品質檢查。

本機 `data/` 與 `artifacts/` 路徑不納入版本控制。Git 忽略規則只限制版本控制追蹤，不是備份、保留或刪除政策。

## 授權

本專案尚未選定開源授權。請勿假設程式碼已依特定開源授權發布。
