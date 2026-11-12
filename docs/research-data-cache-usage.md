# 研究就緒資料快取使用說明

需求：[research-data-cache-requirements.md](research-data-cache-requirements.md)；設計：[research-data-cache-design.md](research-data-cache-design.md)。

快取是由已下載資料衍生出的單一 SQLite 檔，讓新研究直接載入 `ResearchData`，不必每次重做報價正規化、事件讀取與調價。它不是來源事實，可以刪除並重建；不下載、不改動下載資料根目錄。

```python
from pathlib import Path
from quant_research import research

root = Path("<下載資料根目錄>")  # 唯讀
cache = Path("<快取檔>")  # 必須在資料根目錄與證據目錄之外，父目錄須已存在

research.build_cache(cache, root, "twse", "2018-01-02", "2026-10-22")  # 首次建立一個市場
data = research.load_cached(cache, "twse", symbols=("2330",))  # 直接研究
```

## 函式

| 函式 | 用途 |
|---|---|
| `build_cache(cache_path, root, market, start_date, end_date, classification_evidence_manifest=None)` | 首次建立一個市場（`twse` 或 `tpex`）。起日成為 `coverage_start`。該市場已存在則拒絕 |
| `update_cache(cache_path, root, market, end_date, classification_evidence_manifest=None)` | 增量補建 `built_through` 次日到 `end_date`，接續既有調整價 |
| `rebuild_cache(cache_path, root, market, start_date, symbols=None, classification_evidence_manifest=None)` | 從 `start_date` 重新轉換（全部或指定標的），結束日固定為 `built_through`，不延伸涵蓋期間 |
| `load_cached(cache_path, market, symbols=None, start_date=None, end_date=None)` | 回傳 `ResearchData`；不讀來源 |
| `cache_status(cache_path)` | 各市場 `coverage_start`、`built_through`、可用與不可用標的數 |
| `list_symbols(cache_path, market, status=None)` | 各標的狀態（`available`／`unavailable`）、原因、首末日期 |

建立、更新、重建每次只處理一個市場，在單一寫入交易內完成：失敗或中斷後快取與指令前相同；首次建立失敗不留下檔案。取不到寫入鎖會立即報錯，不等待。所有操作都先檢查 schema 版本與調價規則版本，不符時拒絕（`CacheIncompatibleError`）。

## 範圍與不可用標的

- 納入分類為 `ordinary_evidence_supported` 或 `ordinary_rule_limited` 的整檔標的。非 4 位數代碼、非普通股（例如 `00` 開頭）、混合分類，以及轉換或驗證失敗的標的，記為不可用並保留原因（`list_symbols` 可查），不寫入 bars／events。
- 單一標的失敗不影響其他標的；來源範圍不可用、儲存錯誤等指令層錯誤使整個指令失敗並回復。
- 要求不可用標的得到 `SymbolUnavailableError`（含原因）；不存在得到 `SymbolNotInCacheError`；指定標的在期間內無報價得到 `NoQuotesError`；日期超出 `[coverage_start, built_through]` 得到 `CacheCoverageError`。
- `symbols=None` 只含範圍內有報價的**可用**標的，是快取內可用集合，不是完整市場；沒有任何符合者時得到 `NoQuotesError`。
- 不可用狀態在 `update_cache` 與起始日晚於 `coverage_start` 的重建中不會自行恢復。只有起始日等於 `coverage_start` 的 `rebuild_cache` 會重新判斷。以較晚起始日明確指定不可用標的會被拒絕；較晚起始日的全市場重建則保持它們不變。

## 價格使用說明（RDC-09、12）

- 調整價 = 原價 × 累計倍率，**起點固定為該檔快取最早的報價**（變更前為 1x），之後每個減資或面額變更事件連乘該事件倍率。現金股利與配股不調價，也不含息還原。
- 切片載入（較晚的 `start_date`）時調整價水準**不會**重設，與 `load_taiwan` 以較晚起日載入的結果不同；歷史日期的調整價不因新增日期或新事件被改寫。最新調整價不保證等於市價。
- 調整價用於同一標的的價格變動比率與相對量；**價格門檻、成交、持倉估值用原價**。
- `bars.factor`（每日累計倍率）與 `events.multiplier` 存在快取內，供增量接續。

## 已知限制（RDC-07、08）

- 不自動偵測來源修訂。`update_cache` 只讀 `built_through` 之後的資料與事件；舊日期被修訂、舊日期才出現的事件，不會被讀入也不會套用，不會在接縫處產生假跳動。
- RDC-07 的一致性（重建結果與完整重建一致）只在起始日以前的來源、事件與分類證據未變時成立；輸入已變動時，須選擇涵蓋最早受影響日期的起始日。
- 補入比 `coverage_start` 更早的歷史後，該檔起點改變，須從 `coverage_start` 重建。
- `sessions` 只新增、不刪除；來源日後移除某交易日時，快取仍保留該日。
- 分類證據雜湊記錄於快取。與記錄不同時，`update_cache` 與部分重建被拒絕；只有全市場、起始日等於 `coverage_start` 的重建會換用新證據，避免同一快取混用兩套分類。
- `load_cached` 回傳的 `source_diagnostics` 是建立當時的來源範圍紀錄，不依載入範圍切片，因此與 `load_taiwan` 的 `input_digest` 不相等；`protected_input_dirs` 是 `load_taiwan` 的超集合（多了快取所在目錄）。`input_digest` 不用於判斷快取是否過期。

## 規模（本機真實資料全量實測）

範圍 2018-01-02～2026-10-22，兩個市場各在獨立子程序、記憶體上限 20 GiB 下建立；資料根目錄主檔與 WAL 的 size／mtime 前後不變。

| 項目 | TWSE | TPEX |
|---|---:|---:|
| 建立時間 | 271 秒 | 552 秒 |
| 建立峰值 RSS | 174 MiB | 123 MiB |
| 快取檔大小 | 522 MB | 435 MB |
| 單一交易的 WAL 峰值（取樣） | 525 MB | 438 MB |
| 暫存空間峰值（取樣） | 496 MB | 407 MB |
| bars／events | 2,091,824／7,958 | 1,720,314／5,924 |
| 可用／不可用標的 | 1,139／355 | 968／47,404 |
| `sessions` | 2,142 | 2,142 |
| `load_cached` 全部可用標的 | 141 秒、11.6 GiB | 114 秒、9.5 GiB |
| `load_cached` 25 檔 | 2.5 秒、277 MiB | 2.7 秒、301 MiB |
| 增量更新（建到 2026-09-30 後更新到 10-22） | 5.9 秒 | 12.0 秒 |

不可用原因：TWSE 為 11 檔 `excluded_non_ordinary`（00 開頭）、338 檔 `invalid_symbol_shape`、6 檔 `suspicious_or_ambiguous_signal`；TPEX 為 47,403 檔 `invalid_symbol_shape`、1 檔 `suspicious_or_ambiguous_signal`。

全量驗證：SQLite `integrity_check` 與 `foreign_key_check` 通過；每個市場 25 檔的 `load_cached` 與 `load_taiwan` 在整段期間逐欄一致（`load_taiwan` 25 檔 12.5 秒／34.7 秒）；事件日前後的 `factor` 與以 50 位精度計算的結果相同；以「建到 09-30 再更新到 10-22」與「一次建到 10-22」比較，bars、events（含倍率）、symbols、sessions、markets 的內容逐字相同。

注意：
- WAL 與暫存是 0.5 秒間隔取樣的峰值，可能低估短暫的高值；單一交易下 WAL 約等於快取檔大小。
- 讀取全部標的的記憶體與 `load_taiwan` 同量級，研究時建議以 `symbols` 與日期範圍選取。
- 先前以三個資料量冒煙量測所做的推估與全量結果同量級；規模數字以本節實測為準。

## 使用建議

- 先 `cache_status` 看涵蓋期間，再決定是否 `update_cache`。每日更新是明確指令，不會自動執行。
- 快取檔放在資料根目錄與證據目錄之外；路徑別名（符號連結）也會被檢查。
- 刪除快取檔不影響來源；以 `build_cache` 可重建。
