# Taiwan Quant Dataset 使用界線

本文件取代舊版 bundle reader、Python 公開 API 與 CSV／JSONL 匯出指南。修訂後目前交付的是可測試的內部離線計算核心，尚未決定正式使用介面與輸出格式。以[需求](taiwan-quant-dataset-requirements.md)及[設計](taiwan-quant-dataset-design.md)為準。

## 已實作的核心行為

- 唯讀使用既有官方行情、事件摘要及其已保存明細，不發網路請求。
- 每個來源市場＋代碼由使用範圍的第一筆行情開始，累積倍率初值為 1。
- 按日期升序，減資／面額換股生效時更新倍率；只調整當日及後續 OHLC，不回改先前行情。
- 減資使用直接換股率；面額變更優先使用直接比例，否則使用事件前價格／恢復買賣參考價。
- 現金股利、股票股利／無償配股、退還股款及來源參照另行保留，不參與調價。可選現金金額缺值為零，必要值或解析錯誤不以零代替。
- 商品類型保留普通股、非普通股、unknown 與分類依據。非普通股／unknown 保留原始資料，但不產生普通股調整結果。
- 原始 OHLC、成交股數、成交金額不被改寫；來源缺價保留缺值，不補價。

## 不再提供的舊功能

以下程式與入口已移除，不應再依舊指南使用：

- `build-dataset`、`export-dataset` CLI。
- `Dataset.open`、公開 `build`／`export_dataset` 等舊套件匯出。
- 版本化 bundle、dataset manifest／fingerprint、CSV／JSONL 匯出。
- coverage 報告、品質狀態及 blocked／unknown 價格區間系統。
- 另行指定未來基準日並回頭調整歷史價格的算法。

分類用的既有本機證據及其輸入 manifest 屬於分類輸入，不是已移除的 dataset 版本管理功能；本次未新增分類下載來源。

## 使用與安全邊界

`dataset/build.py` 的 `iter_series` 及相關資料結構僅為內部串接與測試入口，不是穩定公開 API，也不是新輸出格式。正式讀取方式、儲存及匯出格式待使用情境確認後另定；本次沒有替代 CLI。

來源 DB、分類 manifest 及其引用檔案都是操作者明確選定的可信本機輸入，不適用於接收不可信使用者上傳的服務。manifest 內的相對路徑以其所在目錄解析，但不是目錄沙箱；`..` 或 symlink 可指向目錄外的檔案。SHA-256 只檢查內容與 manifest 是否一致，不證明來源真實性；檔案也沒有不可信輸入的大小限制。不要載入未知來源的 manifest／證據，或把這個內部流程直接包裝成有較高權限的公開上傳服務。

核心使用私有臨時 SQLite 工作區排序，按股處理；正常結束、錯誤或關閉流程時清理自身工作區，不清理來源檔。來源以唯讀交易開啟，允許 SQLite 正常處理 WAL／SHM，不使用 immutable 忽略 WAL。

官方下載器、既有唯讀報告與排程命令不變。既有 production DB、raw、歷史下載結果與分類證據不 migration、不 relabel、不刪除。執行成功只表示依已取得的資料及規則完成處理，不代表完整歷史、官方認證價格或含息總報酬。

驗收案例與實際檢查結果見[設計 §9](taiwan-quant-dataset-design.md#9-本次驗收紀錄)，舊 bundle／匯出驗收不適用本修訂。
