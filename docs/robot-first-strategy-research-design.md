# 機器人優先策略研究環境：首版設計

## 1. 狀態、依據與非目標

依[已確認需求](robot-first-strategy-research-requirements.md)設計。**使用者於2026-10-31確認純面額推算修訂，並以「其他 ok keep going」同意其餘設計；進入分段實作與驗證。** 上市現有資料缺前後面額的事實仍保留；未授權下載／補抓或以股價冒充面額。

目標是讓機器人發現功能、編寫研究程式、組合資料／特徵／分析／回測工具，檢查並比較結果。先提供本機Python研究能力，不提供Web／Notebook UI、不建立agent編排、不接券商、不新增quant-data子命令。不接其他市場、不發布Dataset bundle、不做全量production回測。

首版不新增第三方依賴，使用Python3.13標準函式庫與既有Dataset。共通資料以typed records及明確key表示，不自建DataFrame／查詢語言；研究者可用一般Python處理，也可在自己的研究環境轉成其他分析工具輸入。首版提供必要的分組與統計，不重建龐大指標庫。若後續需要pandas／NumPy等加速或互通，另驗證與核准，不暗中加相依。

## 2. 已查核的既有能力與必要串接調整

主助手讀碼，並委派唯讀事件欄位查核後逐項核對：

- `dataset/build.py` 的 `iter_series` 按來源／代碼回傳原價、調整價、分類及`EventValue`；本次設計前尚無研究／回測模組。
- `SourceReader` 已提供固定唯讀交易、最新版本不回退、symbol/date篩選、精確明細與來源diagnostics。
- `EventValue` 沒有付款／新股可交易日期。現有資料查核見需求§4.2；日期缺失依已確認RSR-19處理。
- `EventValue` 沒有保留停止買賣日、完整quality_flags或有償認購／員工配股條件。這些不能只憑數值模型猜測；帳務適配須讀取其精確引用的summary/detail payload。
- 減資必須有直接換股率。面額調價可採before/reference價格比；[既有需求](taiwan-quant-dataset-requirements.md)已明說價格比不宣稱精確換股率，不能直接拿它當確定的持倉比例。
- 2026-10-31因使用者詢問是否真的可能缺比例，主助手以SourceReader唯讀查核2018-01-01～2026-10-22選用的全部24筆面額事件：上櫃14筆均有直接share_multiplier；上市10筆均沒有直接share_multiplier或par_before/par_after，只有摘要價格。例：2327／2025-08-25（record17745635）546/136.50，6415／2022-07-13（record8984441）2485/621.25，8476／2024-11-11（record14625757）58.80/29.40。這是現有取用資料缺直接條件，不是官方沒有換股條件的結論。已確認的Dataset設計本來就未要求下載上市面額明細。
- 上櫃3093／2022-12-12（record8983344）直接比例4，但摘要價格109.50/27.38不等於4，為價格比不必精確等於股數比例的實際例子；不能因多數事件價格比恰為整數就把來源層級混為一談。此查核只讀24筆事件及既有metadata，未下載、未做全量調價或新回測。
- 順序調價首筆factor=1且不重套首日前事件；這是價格尺度，不是帳戶持股。回測首版從現金、零持股開始，不從factor反推既有庫存。

唯一計畫內的既有核心重構：將`iter_series`的處理本體抽成可使用既有`SourceReader`的私有helper；原`iter_series(spec)`仍建立reader並委派同一本體。研究適配器可在**同一固定唯讀交易**內先檢查資料量、取得行情／事件，再補讀精確來源flags及市場觀察日期，避免另開交易拼接不同時點。不得改變分類、來源選取、調價或可選現金零值語意。先以舊入口／新helper的輸出等價與現有測試接受此重構，才接下游功能。

## 3. 共通資料與台股適配分開

```text
既有本機官方資料（唯讀）
  → Taiwan adapter：既有Dataset＋必要來源帳務依據
  → ResearchData：單一市場的bars、events、sessions、分類與來源
       ├─ 自訂特徵／事後標籤／分組分析
       └─ 逐日策略決策 → 模擬帳務 → 績效／診斷
```

### 3.1 共通輸入

`ResearchData`含：

- 一個market、一組明確symbols、load起訖日、排序唯一的市場session日期。
- `Bar`：symbol/date、原始OHLC、volume/amount、可選的研究調整OHLC、分類／來源參照。金額／價格／股數用Decimal，缺值用None，不用NaN。
- `CorporateAction`：symbol/type、effective_date、直接股數條件、par_before/par_after及推算依據、現金／配股條件、可選cash_credit_date及share_available_date、來源flags與refs。實際日期與模擬日期分開；目前Taiwan adapter實際日期為None，不自行辨認一個名字含「日期」的欄位就當付款日。
- 調價價值和股數條件分開：價格比路徑可產生研究調整價。持倉換股可用直接share ratio，或在純面額變更時以可靠par_before/par_after推算m，無須另外存在直接比例欄位；不能把before_price/reference_price當作面額。
- 持倉帳務按可明確解讀的每原股口徑，遇未知／衝突／超出模型條件保存原因，不靜默改成沒有事件。共通資料與時間view採唯讀結構，避免正常使用路徑意外改寫已載資料；這不是對抗任意Python反射的沙箱。

市場共通部分不import台股模組。台股適配器負責source/dataset對應、分類、股數／金額單位與事件日期語意；不承諾現在已通用於所有市場。

### 3.2 讀取範圍與普通股

`load_taiwan`必須明確提供root、單一market、非空symbols、load起訖日。預設資料量上限100,000筆選用行情，可顯式調整；先以同一reader的選用筆數做預檢，再載入選定研究子集。不以未指定symbols表示全市場，也不讀取全庫payload或掃raw。上限是避免意外的大載入，不是執行任意研究程式的資源沙箱。

保留`ordinary_evidence_supported`／`ordinary_rule_limited`差異。非普通股及unknown仍可檢視原始資料及排除原因，但不得悄悄成為策略可交易股池；若顯式指定的策略universe含不可用類型，回測先報明確輸入錯誤，不默默改變股池。若整檔沒有行情亦明確報告，不回傳看似成功的空績效。

sessions由同市場、同時間範圍的最新行情scope中有來源record的日期建立，**不是只取選定股票有行情的日期**。因此某股票停牌，不會讓它的「下一交易日」被延後到恢復日。這是已保存市場行情的observed-session近似，不是完整官方日曆；來源失敗仍依核心報錯，成功空回應不升格成休市證明。報告揭露calendar_basis；若研究者提供明確可信的session序列，須在範圍與market上驗證並記錄該輸入，不新增日曆下載。

### 3.3 固定輸入與暖身

load區間與evaluation區間分開。前段資料可供指標暖身，但不交易、不憑空建立歷史持股；evaluation開始時只有指定初始現金。調整價的起始尺度沿用load範圍首筆，不重新發明基準日。

以規格化輸入的摘要hash、來源task/snapshot/record refs、分類依據、calendar_basis與load/evaluation範圍識別本次資料。不建立Dataset版本服務或bundle。重現條件是相同輸入、程式及參數；來源最新版本已修訂時hash不同，必須揭露，不能宣稱自動回放舊版本。

## 4. 機器人使用介面：Python優先

下列為已確認的首版契約；實作狀態見§11，不因設計確認就聲稱功能已交付：

| 功能 | 輸入 → 輸出 |
|---|---|
| `capabilities()` | 無市場讀取 → 功能名稱、用途、import路徑、參數／結果說明、例子與限制的結構化目錄 |
| `load_taiwan(...)` | 明確本機資料範圍 → `ResearchData` |
| `compute_feature(data, function, ...)` | 自訂函式、名稱及參數 → 按date/symbol對齊的特徵值與缺值原因 |
| `forward_returns(data, horizons, ...)` | 明確區間／期間 → 事後價格報酬標籤，含起訖日與語意 |
| `analyze_feature(feature, labels, ...)` | 特徵、標籤、分組／切片 → 分組樣本數、平均報酬及缺漏診斷 |
| `backtest(data, strategy, ...)` | 自訂決策函式、特徵、現金與成本 → `RunResult` |
| `compare_runs(...)` | 多個結果 → 對齊的指標、參數／資料／假設差異及可比性提示 |
| `save_run(result, directory, ...)` | 結果及明確新目錄 → 本機實驗紀錄，非Dataset匯出產品 |

不要求策略繼承基底類別。一般Python函式接收受限的當時資料view並回傳交易意圖；可以自行組合其他函式。支援function有自己的狀態，但新實驗由呼叫者建立新狀態，保存必要參數；不承諾任意未記錄外部狀態可重現。

共同view提供截至當日的bar/history/features、當時持倉／現金等；不提供未來bar或事後labels。Python是可信本機研究程式，不是沙箱，無法防止研究者透過closure或自己讀檔故意看未來；文件、時間對齊與prefix測試保證內建路徑不引入未來值，不宣稱消滅任意使用者程式的前視偏誤。

使用形態（省略號為需提供的輸入；完整可執行例子見[使用方式](robot-first-strategy-research-usage.md)）：

```python
from quant_research import research

research.capabilities()                  # 先了解功能與限制
data = research.load_taiwan(...)         # 一次載入明確研究子集
feature = research.compute_feature(data, my_feature, name="idea", params={...})
labels = research.forward_returns(data, horizons=(1, 5))
study = research.analyze_feature(feature, labels, groups=5, ...)
run = research.backtest(data, my_strategy, features={"idea": feature}, ...)
run.summary()                            # 先看摘要
run.trades                               # 需要時看詳細紀錄
```

不建立固定順序工作流；各步可分開使用。完整用法、參數型別、欄位、金融語意與可執行例子寫入usage文件及docstrings，不讓機器人只靠閱讀內部程式推測用法。

## 5. 特徵分析與時間界線

- `compute_feature`按session呼叫自訂函式；view只可取得當日收盤及更早資料，可一次回傳多個symbol的值。結果缺值保留，不自動填補暖身不足或缺價。可重用結果進行多種分析／回測，修改策略不必重新載入資料。
- 共通查詢是依symbol及session索引取窗口，不為每個日期複製整份歷史。可自行用輸出records做額外批次分析，不強迫使用內建feature函式。
- 首版forward label使用指定h個**市場sessions**後的調整收盤／當日調整收盤−1，兩端須有有效正價格；中間停牌不改h的意義，端點缺值就缺值，不跳到下一筆有效股票行情。
- 明確命名為換股調整價格報酬：不含股利／退還款，不是從訊號形成後下一開盤可實現的淨投資報酬。h必須正整數，不用此label假冒回測。
- 每日按feature排序分組，同值不拆散；使用同值區間的平均名次決定分組，可出現空組。輸出各日／各組樣本數及平均label；跨日摘要採各日組平均等權，並提供日數。常數特徵／樣本太少明示，不捏造完整分位組。
- 切片明確包含時間及可選symbols。研究區間／樣本外區間分開輸出；納入某區間時，label起日與終日都必須在區間內，避免研究尾端標籤跨入樣本外。允許暖身使用區間之前資料。
- 不自動選參數、挑最佳策略或聲稱多次查看樣本外仍無過擬合；這是研究者的研究流程責任。

## 6. 回測模型：單一逐session引擎

### 6.1 首版明示假設

1. 單一市場、現金、多頭，初始只有現金。不融資、不放空、不外加存提款。
2. **股數採Decimal理論可分割股數。** 不把換股後的小數股數捨掉或猜零碎股補償；不模擬整股／零股盤、最小交易單位或券商逐筆元捨入。這是使用者已接受的研究簡化，不是台股實際下單規則。
3. 策略收盤後回傳當日決策，基礎意圖為`MarketOrder(symbol, side, quantity)`。同日同股只允許一個淨方向，重複／矛盾指令報錯，不暗中抵銷。也提供便利的`target_weights(view, weights, cash_reserve)`，以當時淨值與原始收盤換算成交易意圖，方便排名配置研究。weights非負且總和不得超過可配置比例；準備金必須明示。
4. 意圖在下一個市場session開盤處理；不能用下一開盤價回頭決定前一日股數。價格跳空造成超預算，可能被拒單。當日有效，未成交不自動延續。最後一日決策沒有區間內下一session，明列end_of_run未執行。
5. **同一開盤批次先賣後買，賣出淨款當批可用。** 這是即時可用款的簡化，不模擬交割／券商額度。沒有可賣庫存就拒該意圖，不形成負持倉。
6. 缺開盤／非正價格／明確不可交易時拒單。只因日線有開盤價的其餘委託按模型全數成交；不以事後當日成交量／高低價當成開盤已知條件，不宣稱模擬排隊、盤中流動性、漲跌停成交機率或市場衝擊。
7. 多檔買單先檢查總成本：可成交買單總需求超過賣後可用現金，**整批買單拒絕**，不依代碼排序挑單、不暗中縮量。賣單若合計淨款會造成負現金，亦拒絕該賣批次。各個無效意圖先明列原因，不能因刪去無效單而隱藏策略錯誤。
8. 每次執行必須提供`CostModel(fee_rate, minimum_fee, sell_tax_rate, slippage_bps)`，全部明示，零值亦可。首版為該次研究的固定費率假設，不自動宣稱歷史法定／個人實際費率；需要變更費率時另跑明確情境比較。

買進模擬價=`open*(1+slippage_bps/10000)`；賣出模擬價=`open*(1-slippage_bps/10000)`。成交價須正值，slippage_bps限[0,10000)。手續費=`max(abs(notional)*fee_rate, minimum_fee)`，賣出另計notional*sell_tax_rate。費率／最低費用非負、有限；未成交不收費。不以當日高低價夾限模擬滑價，避免用事後資料製造開盤成交能力。

金融帳務使用獨立Decimal precision50／ROUND_HALF_EVEN，不受呼叫者context影響；不把二進float偷偷轉成金融值。首版不擬真券商金額／稅費捨入，報告揭露。輸入價格、成本、股數、權重、日期、重複key及非有限值先驗證。

### 6.2 逐日處理順序

```text
處理上個session後至本session開盤前的事件／到期分配
  → 確定事件前持倉資格，建立應收與換股
  → 將當日到期／事件日替代分配轉成可用現金／股數
  → 處理前一session意圖：有效賣批次，再有效買批次
  → 使用當日收盤估值，記錄現金／持倉／應收／成本／淨值
  → 將截至本日資料與當時帳戶交給策略
  → 保存意圖，等待下一session
```

事件發生於非session日期也按真實effective/credit日處理並記錄，到下一session時已反映，不擅改事件日。事件依時間游標只處理一次。僅對事件前有權持股計分配，當日新買不獲本次分配；事件前持股當日賣出仍保有既有分配權利。

股數換發／配股跨越一個尚待執行意圖時，首版將該股意圖明列`corporate_action_invalidated_order`並取消，避免把舊股單位當新股股數成交；不替研究者猜自動改單。純現金分配不改委託股數。

### 6.3 公司行動帳務

以事件前符合資格股數q為基礎，所有金額／比例均須有可解讀依據：

- 現金分配q*d及退還款q*r：effective日建立應收；實際入帳日已知時到日才轉可用現金，否則依RSR-19事件日立即可用。應收本身計入資產，但不可拿來買進。
- 股票股利：以q*b建立新增股數權利，已知可交易日到日才加到可用庫存，未知依事件日替代。待交付股數依同股當日估值計資產，不重複加到庫存。尚有未交付股數又遇股數轉換時若權利口徑無法確定，回報不支援，不猜複合條件。
- 減資：庫存換發q*m，退還款／併案股利分別按其原股口徑記帳，不從含現金的價格比反推m。
- 純面額換股：優先直接且語意明確m；沒有直接m但有可靠變更前後面額時，計算m=par_before/par_after，再換發q*m。面額須有限且正值；兩類依據同時存在時須一致，衝突不可任選。純面額變更不產生退還款或額外現金；其他獨立分配仍另外記帳。只有市場價格比、沒有直接比例及面額時，研究調整價仍可用，但受影響持倉報`unsupported_holdings_ratio`，不以價格比猜整數。
- 2026-10-31新增查核：目前DB的tasks及snapshots均沒有TWTB8UDetail／TWTB7UDetail，上市10筆摘要亦沒有前後面額。純面額比例在計算上可推得，但現有上市輸入尚缺這兩個面額；這是來源欄位問題，不是以此擴張下載或把市場價格冒充面額。
- 有償認購／員工配股、來源衝突或未知併案條件：保留來源flags，檢查是否影響當時持倉及所需條件。首版不替持有人決定認購或推導其他人的配股；無法確定帳務時明示受影響回測不支援。不改Dataset對這些事件的調價規則。
- 同股同日多事件若不能確定分配是否重複或股數基準，不能靠固定事件排序猜答案；回報受影響帳務不支援。單一減資事件內明確的退還款與股利可各自記帳。
- 沒有持倉也沒有待執行受影響意圖時，不憑來源警告生成持有人的現金或股數，也不把其他無關股票研究一併當成失敗。

實際日期缺失仍為None；derived credit日期、policy、權利基礎q、條件、金額／股數及來源refs均寫入事件帳務紀錄。

### 6.4 估值、缺價與失敗

淨值=`可用現金 + 可用股數按原始收盤估值 + 應收現金 + 待交付股數價值`。

一般缺收盤但有持倉時，允許最後已知價格估值，逐股／逐日記錄price_date與stale標記；不作成交價、不修改來源。若涉及本次分配或股數改變而當日無有效收盤，不能沿用未轉換口徑的舊價再加股利／改股數，避免假損益；首版此時明確失敗並指出估值不足，不猜理論價。這不阻擋獨立特徵研究。

輸入／使用者策略例外／必要帳務或估值不足採typed error，附market/symbol/date/field/source及可用診斷；不吞掉錯誤繼續輸出正常全期績效。正常拒單不是整次回測例外，而是結果中的明確狀態。

## 7. 績效、比較與結果保存

`RunResult`分開提供：summary、daily equity/cash/receivables、positions、decisions/orders、fills/rejections、corporate-action ledger、assumptions/input refs。摘要不塞全部行情或交易，機器人可只讀需要的表格並自行篩選。

- 初始淨值為initial_cash。每日報酬NAV_t/NAV_previous−1，第一日previous為initial_cash。
- 累積報酬NAV_last/initial_cash−1。最大回撤=max(1−NAV_t/running_peak)，peak包含initial_cash。
- NAV=0後無法計算後續百分比時回報明確狀態，不用除以零／NaN充數。
- 成本分手續費、交易稅及滑價歸因。滑價已在成交價反映，不能再扣一次。
- 提供同一成交路徑的成本前損益歸因：淨損益加回明確成本；不能把它宣稱為另跑零成本、可能改變可買股數的反事實策略績效。
- 首版不默認年化、Sharpe、無風險利率、基準指數、持倉績效配對或勝率定義。必要淨值／報酬／回撤與可核對紀錄先正確。
- `compare_runs`除指標外列出資料hash、區間、symbols、程式／參數、成本、日期替代、calendar及其他假設差異；不同資料／規則可比較情境，但不得偽稱只有策略參數不同。

`save_run`只在明確指定的新目錄寫研究結果；拒絕覆寫既有目錄，拒絕寫入production root或可信分類輸入目錄及其子目錄，檢查resolved路徑。研究程式為可信本機程式，此保護不是任意Python程式的沙箱。

公開提交前經使用者確認RSR-21私有權限修訂：最終新目錄以mode0700建立；資料檔以os.open的O_CREAT|O_EXCL|O_WRONLY及mode0600排他建立，再由owned fd寫入，stream包裝失敗須關閉fd。COMPLETE仍由0600暫存檔完整寫入／關閉後原子發布；不使用程序全域umask變更，不chmod任何既有路徑。更嚴格umask仍生效；不能將POSIX mode視為其他平台ACL或特權使用者隔離保證。驗收需涵蓋umask000／022／077、既有權限／內容不變、部分寫入與marker失敗及正常金融結果。

以UTF-8 JSON摘要／設定與按表分開JSONL保存詳細紀錄；Decimal用字串、None用null，不序列化NaN/Infinity。這是首版研究診斷格式，不是恢復Dataset發布格式／bundle。先建立新目錄，最後才寫完成標記；失敗不得讓部分產物看似成功，不自動刪除既有使用者資料。

執行開始時固定本次研究程式路徑／內容hash、顯式參數與random seed（若使用）、Python版本、資料範圍／來源refs／hash及假設；結果保存只序列化已固定的紀錄，不在研究完成後重讀已修改的程式或參數冒充本次輸入。任意closure、外部模型或未提供程式的狀態不可能自動完整擷取，需明示由研究者提供重現資訊；不承諾無條件重現任意程式。

## 8. 最小程式範圍

建議新增`src/quant_research/research/`：

| 模組 | 單一責任 |
|---|---|
| `models.py`／`data.py` | typed共通資料、索引、時間受限view、輸入檢查 |
| `taiwan.py` | 唯一台股適配層、來源／分類／觀察session／事件依據 |
| `features.py` | 自訂特徵、價格標籤、分組與切片 |
| `ledger.py` | 現金／股數／應收與公司行動的確定性記帳 |
| `backtest.py` | session順序、策略呼叫、下一開盤批次處理 |
| `performance.py` | 績效與結果比較 |
| `results.py`／`catalog.py` | 明確研究結果保存、功能目錄 |
| `__init__.py` | 顯式研究入口，不做import-time I/O |

實際模組可在不改責任與契約前提下合併小檔案。既有核心只修改build.py的必要私有抽取；來源／下載器／分類器／價格算法／CLI／lockfile不改。若發現必須超出此界線，先回報，不在子任務自行擴張。

新增少量可非互動執行的feature、single-asset、ranking例子，清楚區分人工教學資料與本機真實資料。main agent維護需求／設計／usage與README／AGENT連結；功能與測試由worker執行，不並行改同一檔案。

## 9. 分段實作與接受鏈（設計確認後才執行）

每段都需main讀碼、比較實際值與明確預期後接受；有失敗即停止依賴步驟，不以build或測試總數取代行為驗證。

| 階段 | 唯一主要目標 | 必要證據 |
|---|---|---|
| A1 | 共用同一唯讀reader而不改Dataset行為 | 舊iter_series／私有helper輸出等價；既有Dataset tests、首日事件／多股／最新失敗／WAL交易檢查；來源schema／資料不變 |
| A2 | 建立共通研究資料及台股適配 | 明確symbol／市場範圍、資料量上限、類型排除、來源refs與flags、observed-session及非選定股票行情維持日曆；單一交易證據 |
| B | 自訂特徵及事後分析正確且時間對齊 | 下列手算分組例、缺值／暖身／同值、跨區間label排除、未來資料不改既有prefix、未來label不進策略view |
| C | 公司行動與現金／股數帳務正確 | 下列股利／配股／減資／換股手算；實際／替代日期、事件日新買無分配、未交付權利、未知條件與無持倉分開；獨立答案不呼叫被測記帳函式 |
| D | 意圖在正確時間按資金與成本成交 | 下列下一開盤／費用手算、缺價拒單不延續、多檔超預算順序不變、股數事件取消舊意圖、當批賣後買、最後一日意圖 |
| E | 績效、比較、結果保存與發現功能可用 | 手算淨值／回撤／成本、讀回JSON／JSONL值及完成標記、拒寫來源／覆寫、capabilities及3類研究例子在新程序無UI執行 |
| F | 實際交付元件在少量真實資料上運行 | main先接受來源與範圍，再以少數TWSE股票及另一次TPEx研究跑完整流程；已知缺案例誠實標示。實際呼叫新research入口，不以舊verifier替代；不全量回測 |
| G | 全專案及安裝後入口接受 | 標準sync／Ruff／mypy／pytest／build；獨立安裝wheel實際import及fixture研究；quant-data help保持既有範圍，無production副作用 |

### 手算接受例子

1. 特徵分析：同日A特徵1、B特徵2，下一session調整價報酬分別0.1／0.2，分兩組應分別得到0.1／0.2及各1筆；樣本切片尾日若label終日在區間外不得納入。全同值不偽造兩組差異。
2. 下一開盤：初始1000，第一日收盤決策買10股；第二日open10、close11，零成本明示，cash900、持股10、NAV1010；第二日收盤決策賣10，第三日open12，cash/NAV1020、持股0，累積2%。不可第一日提前成交。
3. 上例手續費買賣各0.001、賣稅0.003、最低費0、滑價0：買費0.1，賣費0.12、稅0.36，最終1019.42，累積1.942%，成本0.58；獨立硬值核對。
4. 純換股：100股、原價10，m=2後200股、原價5，股數與單價抵銷，NAV不憑空變動；不使用調整價再乘200股。另驗證沒有直接m但面額10改2.5時，推得m=4，股數100變400且現金不變；不得因缺直接比例拒絕，亦不得把股價比當面額。直接m與面額比矛盾時明確報錯。
5. 減資：事件前1000股、原價10；m=0.9、每原股退1、事後原價10，應為900股＋1000現金，NAV10000。
6. 股利：1000股、原價10；每股現金1、除息後原價9，NAV10000。已知較晚付款時為股票9000＋應收1000、可用現金0；事件日替代則現金1000。事件日新買不獲這筆分配。
7. 配股：1000股、原價10；配股率0.25、事後原價8，立即可用時1250股、NAV10000；已知較晚可交易時1000可用股＋250待交付股，共同按8估值，NAV仍10000。
8. 多股超預算：現金1000，A/B各需600的有效買單，兩筆皆拒絕；反轉輸入順序結果相同。不能只成交排序較前的一筆。

人工例子是邏輯驗證，不稱為已取得真實來源案例。計算context與已知小數結果按50位規則核對；不以同一函式計算expected。

## 10. 非功能性界線與確認門檻

- 本機單程序、受限研究子集；不假設高併發，不引入distributed execution、服務或SLA。
- 建索引重用已載資料，避免每改策略重掃來源及複製全部歷史；不以多執行緒提早解決尚未證明的效能問題。不承諾全市場速度。
- 來源交易固定、只讀mode=ro／query_only，允許SQLite正常WAL／SHM管理；不用immutable、不改schema／來源索引、不動分類檔、不清理來源。不因模擬缺少入帳日期而新增下載。
- 程式碼及來源refs／分類限制可追查；日期替代、理論分割股數、同批賣款可用、observed calendar、費率／滑價及未模擬市場限制要進結果，不只藏在文件。
- 任意Python研究程式為可信輸入，不做惡意程式隔離／權限沙箱；這與核心不使用未來資料的驗證不同。

使用者已確認的主要設計選擇：Python標準函式庫優先、一般函式介面、單一日線引擎；理論可分割股數；同開盤先賣後買且賣款可用；超預算整批買單拒絕；純面額換股可由前後面額推算；其餘必要持倉條件／事件估值無法確定時明確失敗，不從市場調價倍率猜帳務。按§9逐段委派並由main核對，不跳過接受門檻。

## 11. 實作與接受紀錄

2026-10-31開始。沿用既有未提交的Dataset本機驗證檔案，不覆寫或清除它們；本次不自動commit。A1已由主助手讀碼及實際重跑74項Dataset測試接受。舊入口與私有helper得到相同結果，獨立手算factor為1/1/2/2/1、調整收盤100/102/102/106/106；部分generator關閉清理暫存但不關reader，固定WAL視圖不因另一連線commit改變。A2初版被主助手拒絕：公開view洩漏未來資料、metadata可變、事件分類遺失及串接證據不足。修正後主助手再次讀碼、實際重跑Research＋Dataset測試 **92 passed**、mypy通過，並核對真實parser/schema fixtures的TWSE雙來源flags、TPEx面額10/2.5與直接比例4、同一load_taiwan中的WAL commit仍取舊flags、錯誤child snapshot明確失敗、最新失敗不回退、非普通股保留及資料量預檢。A2接受；adapter純格式整理併入下一步，不影響接受的行為。現在允許依賴A2的B特徵及C帳務分別實作，兩者只修改不重疊新檔並使用獨立測試快取；D需等待兩者各自通過主助手接受。

B經主助手讀碼與實際重跑9項專屬測試接受：自訂callback使用獨立50位context、日期索引避免D²掃描、分組手算0.1/0.2、缺值／同值、session horizon、樣本外尾端排除與prefix均核對。B初版的callback受外部Decimal context影響已修正。

C初版拒絕：主助手實際呼叫普通現金股利被錯當價格比換股而失敗；1000股配股0.25並含來源m=1.25時被算成1500股而非1250。已要求依真實event_type分流，且尚需開盤前事件／收盤後估值的兩階段API、跨非session日期的時序及費稅支援。C初版當時未接受，依賴步驟被停止。其後分段修正兩階段begin/close、真實事件type、零配股、不重套來源m、日曆時序與實際釋放紀錄、費稅現金守恆及獨立Decimal context。主助手再次讀碼並重跑17項ledger測試接受C；另攔下「可用股數已全賣，但待交付股數遇換股被忽略」並要求修正後重跑。核對1250股而非1500、面額100→400且現金不變、fractional減資661.95765、費用後1019.42、跨週末應收／交付及無持倉舊意圖取消。Research＋Dataset較早聯合重跑117項通過，Ruff/format/mypy29source均通過；最後ledger新增的pending回歸亦通過。B/C接受後才進入D；未有真實回測績效。

D初版因型別錯誤、停牌區間方向、賣滑價成本符號、同批負淨款賣單順序及輸入紀錄不足被拒絕。分段修正後，主助手讀碼並重跑Research＋Dataset **137 passed**、Ruff lint/format及mypy30source接受。核對零成本1000→1010→1020，費用後1019.42／成本0.58；雙600買單全拒、同批賣後買、負淨款先由同批正淨款支應、滑價不重扣、停牌[stop,effective)、零持倉換股取消舊買單、股利資格、尾日非法單、暖身與future-prefix。params型別標記及digest區分list/tuple/mapping/Decimal/string/bool/int，program/feature hash在執行前固定。D接受後才允許E分開實作。

E經讀碼及主助手Research＋Dataset **152 passed**、Ruff/format、mypy33source，以及最新E專屬15項重跑接受。summary為小型紀錄、daily_returns另取；手算含初始peak／零NAV狀態與同路徑成本歸因核對。保存攔截既有／來源／classification／symlink目錄，部分寫入失敗不發布marker；主助手另要求修正marker半寫入與發布後暫存清理失敗的commit-point語意。10項功能可發現，3個例子各於新程序運行，1020／1010及排名1047.916667符合預期。

F先以load_taiwan預檢，主助手讀取完整選定行情／事件及來源條件後才授權研究。2022-09兩市場各21sessions／35bars，TWSE2368/3356及TPEx5536/1586；明細與普通股規則限制保留。完成特徵、train/OOS、每市場3情境、6份保存及重跑一致性。主助手另外唯讀查10筆DB records，核對初次成交與期末原價、減資m=.9退1與面額10→5直接m2。基準最終100048.5865／99836.471，費用1.1135／2.529；事件日帳戶與獨立硬值一致。第一版F證據因保存program hash與現存driver不同被拒絕；最終保留driver與6份metadata hash一致，所有完成清單檔案hash由主助手重讀驗證後接受F。詳見usage§8的最終證據路徑。研究功能沒有把research_ready=False升格、沒有全量回測或來源寫入；實際日期／非零配股等未取得真實帳戶驗收的情況仍只列人工證據。

G完成。最後另補兩個錯誤診斷回歸：ledger失敗保留原typed exception/cause，透過__notes__附market、session、條件／估值phase、symbols及來源refs；不改正常金融路徑。主助手讀碼並重跑21項engine測試及mypy接受。

最終G-final完整標準sync／Ruff lint／format（103檔）／mypy（33source）／pytest（473 passed；worker31.35秒、main最後重跑31.29秒）／build／diff-check通過。第一輪僅usage的Python code-fence格式失敗，由main整理後重新完成全套。獨立Python3.13.13 venv以no-index/no-deps安裝最終wheel，隔離PYTHONPATH及checkout來源，34個套件檔案與wheel逐byte一致；禁止網路／SQLite／市場檔讀取下通過10項capabilities及完整人工研究。單股1020/1010、排名1047.916667、費用後1019.42／成本.58及保存hash清單均核對。實際installed quant-data help沒有研究或恢復Dataset子命令；34個本機文件連結無缺失。證據位於artifacts/robot-first-research-20261031/G-final/；wheel SHA256為2290cd7b76b83c41cc249198ba15e213334c9c7437bbf32a0177a38827473fe3。主助手另在最終獨立venv的-I新程序重查research實際import路徑、34個wheel／installed檔案逐byte相同，並實際重跑1019.42／成本.58及10項capabilities後接受G。該階段未新增依賴、未改來源／排程，且尚未commit；後續公開提交審查見§12。

## 12. 公開提交前安全檢查（2026-10-31）

使用者同意公開前最小私有權限修訂RSR-21後，主助手讀碼並實際重跑8項結果保存測試接受；umask000／022／077下，新結果目錄均0700，資料／COMPLETE／殘留暫存檔均0600。既有父目錄、既有目標及sentinel內容／權限不變，函式不改全域umask。檔案採排他建立；stream包裝失敗時關閉owned fd，部分寫入與marker失敗仍不得發布不完整結果。

修訂後完整標準檢查475項測試通過（32.71秒），Ruff lint／format、mypy、sdist／wheel及diff-check通過。另將實際wheel以offline/no-index/no-deps安裝至新Python3.13.13環境；在隔離checkout的-I程序重跑費用後淨值1019.42／成本.58，對三種umask逐檔stat核對0700／0600及完成清單hash。這是人工fixture安全驗收，不冒充新增真實市場案例；本輪沒有production下載／改寫或新production研究。

公開檢查使用獨立本機工具／臨時目錄，不新增專案依賴、不改lockfile；報告不加入commit。Gitleaks8.30.1掃描包含未追蹤新檔的候選樹及全部可達Git歷史，採完整redaction；提交index另逐筆重掃。候選清單不含env、DB、raw、本機證據、build/dist、虛擬環境或快取。沒有發現需隱藏的個人home絕對路徑；Git作者身分沿用既有設定，不自行更改或重寫歷史。尚未選定開源授權，不因公開repository而自動新增LICENSE。

uv audit --locked查核13個鎖定第三方套件（含開發依賴）沒有已知漏洞或adverse status；另從本次實際build解析紀錄取出Hatchling1.33.0及其5個相依的確切版本，向OSV querybatch查核，沒有已知漏洞。外部查詢僅送公開套件名稱／版本，不上傳程式或本機證據。建置相依不在專案lockfile內，此結果不認證未來任意重新解析版本或主機／Python本身的安全。

Bandit1.8.6掃描src、scripts與examples無解析錯誤，得到24項MEDIUM SQL組字串、3項LOW assert及2項LOW cleanup exception警示，無HIGH。人工檢視識別字／placeholder來源與呼叫端，未確認有外部不可信值注入SQL的可達路徑；既有storage conflict key仍僅供可信內部固定值呼叫，不可直接開放外部輸入。assert不是授權檢查。2項cleanup suppression用於保留原始失敗或在COMPLETE已發布後避免把成功誤報為失敗，已有回歸測試。原始警示不是零筆，亦不把未發現可利用漏洞當作無漏洞保證。

另審查完整inventory的資源成長：其metadata、事件去重、診斷與輸出不是固定記憶體／輸出上限；這是操作者明確要求的可信本機全庫工具，不是未信任上傳或多租戶服務，限制已補入使用文件。本機策略callback及分類manifest同樣不是沙箱；POSIX權限不隔離root／同UID或取代其他平台ACL。
