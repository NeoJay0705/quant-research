# 機器人優先策略研究：使用方式

## 1. 入口與界線

這是首版、本機、非互動式 Python 研究工具，不是策略白名單、agent 平台或下單框架。研究者可自由組合一般 Python 函式、自訂特徵、事後分析及回測；不必使用固定流程。參見[需求](robot-first-strategy-research-requirements.md)與[設計／驗收紀錄](robot-first-strategy-research-design.md)。

```python
from quant_research import research

for item in research.capabilities():
    print(item.name, item.import_path, item.signature)
    print(item.purpose, item.output, item.example, item.limitations)
```

`capabilities()` 回傳唯讀 `tuple[Capability, ...]`，涵蓋10個操作；import 與功能目錄不讀市場資料、不連網。型別與精確 signature 以目錄／docstrings 為準。沒有新增 `quant-data` 研究子命令，也沒有恢復 Dataset bundle／reader／export。

**本工具不保證完整普通股歷史、全市場 coverage、PIT、來源價格認證或實盤可成交績效。** 既有來源採最新選版，舊版不自動回放；修訂後 input digest 可改變。日線開盤有價不證明排隊、流動性或漲跌停一定能成交。

## 2. 可直接執行的人工例子

在專案根目錄執行，不需要市場 DB、UI、網路或憑證：

```sh
uv run --locked python examples/research/features.py
uv run --locked python examples/research/single_asset.py
uv run --locked python examples/research/ranking.py
```

三者都明示 `synthetic; not real market data`，不是官方資料驗收：

- 特徵：一般函式、h=1 labels、每日分組、研究／樣本外切片。首日 A/B 未來價格報酬為 `0.1`／`-0.05`。
- 單股：下一開盤買10股、之後賣出，明示零成本，1000變1020；改成5股為1010。比較會列出參數差異。
- 多股排名：以已知收盤動量選一檔、目標權重0.5／準備金0.5；4筆成交，最終淨值顯示至6位為1047.916667。

這些例子證明研究能力與時間／金融邏輯，不是推薦策略或績效優勢。

## 3. 明確載入本機台股資料（唯讀）

```python
from pathlib import Path
from quant_research import research

data = research.load_taiwan(
    root=Path.home() / ".local/share/quant-research/taiwan",
    market="twse",  # 或 tpex；一次一個市場
    symbols=("2368", "3356"),  # 必須明確，不以空值表示全市場
    start_date="2022-09-01",  # load範圍，可含暖身
    end_date="2022-09-30",
    max_rows=1000,  # 預設100000；選用行情筆數預檢
)
print(data.input_digest, data.calendar_basis)
print(data.classification_diagnostics, data.source_diagnostics)
```

`ResearchData` 包含 market、symbols、load起訖日、sessions、bars、events、分類／來源診斷、input_digest、protected_input_dirs；records／metadata 唯讀。`Bar` 包含原價 OHLC、volume/amount、研究調整 OHLC、分類及refs。`CorporateAction` 保存股利／配股／退還款／股數比例／前後面額／實際日期及來源條件；金額、股數、比例一律 `Decimal`，缺值 `None`。

- 同一固定唯讀交易完成核心資料與補充來源取用，不下載、不改 DB／raw／索引。
- observed sessions 來自**同市場**已保存行情日期，不只選定股票有價日；這不是完整官方交易日曆。可傳 `sessions=tuple(...)`，由呼叫者負責其來源與市場語意；程式檢查日期範圍、順序及來源行情日不得被遺漏。
- 可傳 `classification_evidence_manifest=Path(...)` 使用既有可信分類線索；不把目前名冊當完整歷史證明。`ordinary_rule_limited` 是規則推斷，非官方普通股歷史認證。
- 非普通股／unknown 原始資料可檢視，但回測拒絕此類明確選定股池，不默默換股票。整檔沒有行情、超出筆數上限、最新來源失敗或必要明細不符會明確失敗，不回退較舊成功版本。
- 此 loader 載入明確子集供記憶體重用，不是全市場大資料服務。source refs 可追查原始 task／snapshot／record；不用任意其他同代碼明細代替精確連結。

## 4. 自訂特徵、標籤及切片

```python
from decimal import Decimal


def last_quote_momentum(view):
    values = {}
    for symbol in view.symbols:
        history = view.history(symbol)
        previous = history[-2].adjusted_close if len(history) >= 2 else None
        current = history[-1].adjusted_close if history else None
        values[symbol] = (
            current / previous - Decimal(1)
            if current is not None and previous is not None and previous > 0
            else None
        )
    return values


feature = research.compute_feature(
    data, last_quote_momentum, name="last_two_quotes", params={"quotes": 2}
)
labels = research.forward_returns(data, horizons=(1, 5))
train = research.analyze_feature(feature, labels, 2, "2022-09-01", "2022-09-16")
oos = research.analyze_feature(feature, labels, 2, "2022-09-19", "2022-09-30")
```

這個特徵使用最後兩筆行情；不是保證相鄰市場session的報酬。研究者可自行改成精確 session 窗口。`compute_feature` 每個 session 呼叫一次 callback，收到 `TimeView`，回傳 symbol→`Decimal | None`；漏回傳及缺值保留原因。`params` 是此特徵的紀錄，不自動傳成 callback kwargs。結果為 `FeatureSeries.values` 的 date/symbol records，可重用、不必重掃來源。

`TimeView.as_of`／symbols／`bar(symbol, day=None)`／`history(symbol, start_date=None)`／`events(symbol)` 只能正常查詢當日與過去。window 透過索引，不逐日複製全部歷史。超過 as_of 的查詢報錯；Python callback 是可信程式，無法防止 closure、自行讀檔或手寫特徵故意偷看未來。

Labels 為 **h個市場sessions後的調整收盤／本日調整收盤−1**。端點缺價就是缺值，不跳到下一有效價；不含股利、退還款，也不是訊號後下一開盤可交易的淨報酬。`h` 必須唯一正整數。分組同值不拆、可能有空組；常數全放同組。缺特徵列在group0；各組記錄 sample_count／missing_count，跨日摘要對可用每日均值等權。切片要求 label 起訖日都在區間內，不把研究尾端跨進樣本外的報酬納入。

## 5. 自訂策略與回測

```python
from decimal import Decimal


def strategy(view, shares):
    # view.features只含當日特徵；view.account是當日交易後帳戶。
    if view.date == "2022-09-01":
        return (research.MarketOrder("2368", "buy", shares),)
    return ()


run = research.backtest(
    data,
    strategy,
    initial_cash=Decimal("10000"),
    costs=research.CostModel(
        fee_rate=Decimal("0.001"),
        minimum_fee=Decimal("0"),
        sell_tax_rate=Decimal("0.003"),
        slippage_bps=Decimal("0"),
    ),
    start_date="2022-09-01",
    end_date="2022-09-30",
    features={"momentum": feature},
    params={"shares": Decimal("10")},
    # 自存研究程式時提供program_path=Path(...): 開始時計算內容hash
)
print(run.summary())
print(run.fills, run.rejections)
```

策略收到 `DecisionView`：date、time（TimeView）、account（AccountSnapshot）、features（名稱→當日symbol值），以及顯式 `params` 的 kwargs。每次 callback 使用獨立參數容器副本；策略若需要跨日狀態可自行維護，但每次新實驗應建立新狀態。參數型別、程式與 feature 值的digest在執行前固定；feature digest不同不會冒充只有策略參數變了。

- load暖身資料可讀；evaluation之前不交易，evaluation開始只有指定現金、零持股。
- 收盤決策最早下一市場session開盤成交；缺價／明確停牌拒單，當日有效不自動延續。尾日意圖列 `end_of_run`。同日同股只允許一個方向的單筆意圖，重複、非法股數及未知代碼報錯。
- 同批先賣後買，淨賣款立即可用；含所有成本檢查，不隱性負現金。有效買單總需求超預算就整批拒買，不按代碼挑成交、不縮量；賣批次亦檢查現金守恆。
- 成交價為 open×(1±bps/10000)，fee=max(notional×fee_rate, minimum_fee)，賣稅=notional×sell_tax_rate。slippage已在成交價反映，績效不能再扣一次。所有成本參數必須明示；允許零成本探索但不是法定／個人歷史費率認證。
- 股數採50位 `Decimal` 理論可分割股數，`ROUND_HALF_EVEN`，不擬真整股／零股與券商逐筆捨入。計算與 callback 使用獨立 context，不受呼叫者 precision/traps影響。
- `target_weights(view, weights, cash_reserve)` 以當時原始收盤與NAV換算意圖，reserve是[0,1]比例，權重總和≤1−reserve；不偷用未來開盤重算股數。待交付股數算曝險但不可賣；跳空／費用／拒單可能令實際權重不同。

## 6. 公司行動與估值

每個session先處理事件及到期權利，再成交，再按**原始收盤價**估值，最後決策。不是用調整價格乘改過的股數：

- 合資格的事件前持倉才取得分配；當日新買不算，當日賣出仍保有已取得分配。
- 現金股利／退還款按每原股口徑；配股為q×b，不重複套用Dataset的1+b倍率。應收與待交付股數列資產，但不是可用現金／可賣庫存。
- 實際付款／新股可用日期可靠時採用；既有台股資料目前未提供標準實際日期，保留None並以effective_date作**模型替代**。可能提早再投資／出售，不稱券商實際帳務。
- 減資必須有直接股數比例；純面額可用直接比例，或前後面額比 `par_before/par_after`；兩者衝突報錯。面額10改2.5，100股變400股、現金不變。市場股價比不是面額比，不從價格比猜持股條件。
- 已知上市面額摘要仍缺前後面額／直接股數條件；持倉跨這類事件會明確不支援，不擅自補抓。缺入帳日的模型替代不補足缺金額、比例或生效日。
- 股數變動取消尚待執行的舊單位意圖，原因 `corporate_action_invalidated_order`；純現金股利不改單位。事件及實際釋放分別有 `LedgerEvent.kind`／record_date，非session事件不擅改日期。
- 一般無價日可用最後已知原價估值，明列price_date/stale；股數／分配改變但無有效本期收盤時明確失敗，不沿用未轉換舊價製造假損益。未交付股數又遇不能確定口徑的事件、同股同日模糊多事件、有償認購／員工配股等亦明確不支援；無關且無持倉／意圖不連坐。

輸入錯誤為 `ResearchInputError`，來源／帳務不足為 `UnsupportedDataError`，策略例外為附日期及原始cause的 `ResearchBacktestError`，均繼承 `ResearchError`。帳務／估值錯誤保留原型別與cause，另在 `__notes__` 附market、session、phase、symbols及來源refs，供機器人定位問題。正常拒單在結果中，不被吞成全期例外或默默續單。

## 7. 摘要、比較及保存

`run.summary()`／`summarize(run)` 回傳小型 `PerformanceSummary`：initial_cash、final_nav、net_pnl、total_return、max_drawdown、fees、tax、slippage、total_cost、before_cost_same_fill_pnl。不附整張明細表。

- `daily_returns(run)`：日期、NAV、每日／累積return、drawdown、undefined_reason。第一日以前的基準為initial_cash；running peak亦包含initial_cash。前期NAV=0時每日百分比None附原因，不輸出NaN。
- `run.snapshots`：cash、positions、receivables、pending_shares、NAV、valuations（含stale）、events、changed_symbols。
- `run.decisions`、fills、rejections、events：按需查核意圖、執行、原因及公司行動，不必重新讀市場資料。
- before_cost_same_fill_pnl=淨損益＋明確成本歸因，只是**同一成交路徑**的歸因，不是另跑零成本而股數／拒單可能改變的反事實績效。不預設Sharpe、年化、勝率或基準指數。

```python
comparison = research.compare_runs({"baseline": run_a, "variant": run_b})
print(comparison.summaries, comparison.differences, comparison.warnings)
saved = research.save_run(run, Path("artifacts/my-new-experiment"))
```

比較列出資料／區間／symbols／現金／程式／參數／features／成本／seed／日曆／分類／來源／假設／Python版本差異；不同輸入仍可作情境比較，不稱只有參數不同。random_seed是紀錄欄位，**不自動替研究者設定全域random狀態**。沒有program_path/hash時明示程式身分不完整；即使有，也不能自動擷取任意closure、外部模型或未提供的研究程式。保存不重讀完成後已改過的程式。要重現，研究者仍須保留完整程式與環境／來源版本；不承諾來源修訂後可自動回放舊資料。

保存只寫明確**新目錄**；POSIX下最終新目錄以0700、新檔（含COMPLETE／暫存marker）以0600建立，只允許擁有者存取；更嚴格的呼叫者umask仍生效。程式不修改全域umask、不更動既有父目錄／檔案權限；此保護不取代其他平台ACL、主機安全或root／同UID使用者隔離。拒既有目錄、symlink路徑、production與載入來源／分類輸入目錄的子路徑。失敗保留部分產物，不自動刪舊資料；`COMPLETE.json`只在內容寫完後原子發布，列出檔案SHA256。完成後暫存清理若失敗不把已完整發布結果改稱失敗。這不是惡意Python／檔案系統競態的沙箱，也不是Dataset bundle：

```text
metadata.json          # 捕捉的規格、程式／params／feature digest、來源及假設
summary.json           # 小型績效摘要
snapshots.jsonl        # 每日現金、持倉、應收、待交付與估值
daily_returns.jsonl    # 每日／累積報酬、drawdown
decisions.jsonl
fills.jsonl
rejections.jsonl
events.jsonl
COMPLETE.json          # 最後發布；schema quant-research-run-v1、hash清單
```

Decimal以字串保存、None為null，拒非有限值。不提供新的自動讀回／版本伺服器；可用一般Python的json逐表讀取，並自行核對完成標記／hash。

## 8. 驗收狀態

人工數值、來源適配、固定讀取視圖、回測、成本／拒單、公司行動、保存安全及3類非互動例子已分段驗收。Research＋Dataset聯合檢查152項通過；其後功能目錄10項亦重驗。最終完整標準檢查473項測試、Ruff lint/format、mypy、build及diff-check通過；獨立Python3.13.13環境安裝wheel後，以實際public API完成特徵／分析／回測／比較／保存，費用後1019.42及成本.58符合手算。34個套件檔案與wheel逐byte一致，import／功能目錄不觸發網路／資料庫／市場資料讀取；實際quant-data help維持原範圍。安裝證據見本機 `artifacts/robot-first-research-20261031/G-final/`，完整紀錄見設計§11。

2026-10-31執行少量真實本機研究：2022-09-01～09-30，TWSE2368／3356、另一次TPEx5536／1586；每市場21個observed sessions、35筆選用行情。各執行固定10股、修改5股、動量排名三種情境；完成特徵、1／5-session labels、研究／樣本外切片、回測、比較及6份保存結果，重跑基準的成交／帳戶一致。

固定10股基準初始100000、首日收盤決策、次日開盤買進兩股並持有；fee_rate=.001、minimum_fee=0、sell_tax_rate=.003、slippage_bps=0：

| 市場 | 事件後股數／款項 | 最終淨值 | 手續費 |
|---|---|---:|---:|
| TWSE | 2368：10→9股、退還10；3356仍10股 | 100048.5865 | 1.1135 |
| TPEx | 5536：10→20股；1586仍10股 | 99836.471 | 2.529 |

主助手另以mode=ro／query_only固定交易核對10筆DB行情／事件明細的價格及條件，獨立算式與實際帳戶一致；停牌區間的陳舊估值明示。1586首日股利沒有初始持股，不取得該次分配。這些是模型驗收，不是挑選獲利策略；排名結果不當成經濟效益證明。

全部分類仍ordinary_rule_limited，來源research_ready=False保留、不被改成認證。真實非零股票股利、實際付款／新股交付日期及有權領取現金股利的帳戶案例不在此次F驗收；相關邏輯由人工fixtures驗證，不冒充真實案例。

本機未納版控的證據在 `artifacts/robot-first-research-20261031/`：`preflight.json`、最終driver `run_acceptance_final.py`、`report-final-c8915db8beb74d21be1572a361b7a4c4.json`、對應 `runs-acceptance-final-c8915db8beb74d21be1572a361b7a4c4/`。先前版本因保存driver hash與現存程式不一致被主助手拒絕；只接受最後這一版，6份metadata均匹配driver SHA256 `df8cf2609012bb98fe236c644cba64286aa4fc85f2557eb4ebe7703e763280aa`，完成清單hash均經獨立重讀核對。未執行全量production回測，未下載／改寫來源或啟用排程。
