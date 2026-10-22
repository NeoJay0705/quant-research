# 專案指引

## 範圍與工具
- 建立可重現的量化研究基礎，研究範圍涵蓋台股、美股與加密貨幣。未經需求確認，不自行選擇首個實作市場、資料商或價格調整語意。
- 使用 Python 3.13、uv，以及 `src/quant_research` typed src layout。
- 不把資料取得、ETL、回測或 CLI 當成已實作能力。

## 開發方式
- 以使用情境設計腳本。參數與介面應簡單直覺。
- Import 不得執行網路請求、讀取市場資料或產生其他副作用。
- 不在程式碼、文件或測試中硬編碼憑證。
- 每次變更都要更新相關文件與測試。
- 只修改任務範圍內的檔案；不要加入未核准的功能或相依套件。

## 研究與資料安全
- 避免未來資訊偏誤與生存者偏誤。
- `data/` 與 `artifacts/` 不納入版本控制。
- 不得未經明確核准刪除原始資料或中間產物。
- 下載的時序資料放在家目錄合適的位置，並更新此文件參考對應目錄

## 標準檢查
```sh
uv sync --locked --group dev
uv run --locked ruff check .
uv run --locked ruff format --check .
uv run --locked mypy
uv run --locked pytest
uv build --no-sources
```
