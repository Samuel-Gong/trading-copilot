# 选股结果导出与 API 接入

在「策略」页点击「导出选股结果」，选择当前策略或策略池，下载 CSV 或代码 TXT。当前支持股票日线策略。

导出包含所选策略最近一次已保存的选股结果，保留策略配置的数量上限与运行时指定的股票池；页面的临时筛选和排序不影响导出。CSV 每个策略命中一行，同一股票命中两个策略时保留两行及各自评分；TXT 和 JSON 的 `symbols` 跨策略去重。

策略的 `META.limit` 或 `display_limit` 会在引擎生成结果时生效，属于本次已完成结果的数量口径。例如满足筛选条件的候选有 120 只、策略设置最多返回 50 只，则运行结果与导出均为 50 只。需要更多结果时，先在策略设置中调整数量上限并重新运行；接口不会自行扩大候选集或重新计算。

升级后首次导出如果提示缓存缺少股票日线标记，点击「重载」重新运行策略。旧缓存仍可在原页面读取，但导出需要明确的资产与周期标记，避免旧版本 ETF 运行结果混入清单。

## 请求接口

```http
GET /api/screener/export?strategy_id=trend_breakout&format=json
```

| 参数 | 类型 | 含义 |
| --- | --- | --- |
| `format` | `json`、`csv`、`txt` | 默认 `json`；文件响应带下载文件名 |
| `strategy_id` | 可重复的字符串参数 | 按传入顺序导出一个或多个策略；省略时选择当前已加载、支持股票日线且有缓存结果的策略，包含策略池以外已运行的策略 |
| `as_of` | `YYYY-MM-DD` | 可选，要求所有结果属于指定交易日期；不匹配返回 409，不会自动重新选股 |

多策略示例：

```http
GET /api/screener/export?strategy_id=trend_breakout&strategy_id=ma_golden_cross&as_of=2026-09-04&format=csv
```

接口只读取最近一次已保存的股票日线策略运行结果，不读取盘中监控快照。监控规则、行情套餐、实时模式或监控命中变化不会改变导出清单；需要更新清单时先重新运行并保存策略，再请求导出。接口本身不计算策略，也不提供历史归档。

导出不追加 `today_ever_rows` 中的“今日曾命中”历史行，不承诺持续跟踪盘中有效性。数据内容与策略当次执行结果一致，沿用现有数据源、参数与股票池口径。所选策略必须全部有保存结果且日期一致，否则整批拒绝。

省略 `as_of` 时读取当前保存快照的日期；指定时要求匹配，不匹配返回 409。不会因为实时行情进入新交易日而自动替换已保存的结果。

单独或批量运行历史日期策略时，如果已有较新日期的缓存，本次历史结果仍返回页面，但不会覆盖共享缓存或其他策略的结果。本接口没有历史归档，此时请求历史日期会返回 409；首次无缓存、同日及较新日期的股票日线运行会正常保存。

弹窗复制的 API 地址省略日期，可每天重复请求；下载文件使用页面所选日期。接入程序应检查响应 `as_of`，或显式传入预期交易日，避免把上一交易日结果当成当日结果。其他设备需使用实际可访问的面板域名或 IP。

## JSON 响应

以下均为合成示例数据：

```json
{
  "as_of": "2026-09-04",
  "asset_type": "stock",
  "timeframe": "1d",
  "total": 1,
  "symbols": ["000001.SZ"],
  "results": {
    "trend_breakout": {
      "name": "测试趋势策略",
      "as_of": "2026-09-04",
      "total": 1,
      "rows": [
        {"symbol": "000001.SZ", "name": "合成股票", "close": 10.5, "change_pct": 0.025, "turnover_rate": 5, "score": null}
      ]
    }
  }
}
```

顶层 `total` 是去重股票数；每个策略的 `total` 是该策略结果行数。`rows` 保留策略原始字段和顺序，不追加最新扩展表列。缺失值及 NaN、Infinity 输出为 `null`，不填成零。证券代码作为字符串传递，保留前导零和 `.SH`、`.SZ`、`.BJ` 后缀。

CSV 固定列为 `as_of,strategy_id,strategy_name,symbol,name,close,change_pct,turnover_rate,score`，编码为 UTF-8 BOM，行尾为 CRLF。CSV 缺失字段为空，逗号、双引号和换行按 CSV 规则转义；可能被表格软件执行的文本公式前加单引号。CSV 与 JSON 均沿用策略原始数值：日线 `close` 为前复权价格，`change_pct=0.025` 表示 2.5%，`turnover_rate=5` 表示 5%。评分属于各自策略，不能把不同策略的分数直接当作同一排序口径。

TXT 是 UTF-8 文本，每行一个完整证券代码，跨策略按首次出现顺序去重，不含表头。

## 认证和调用示例

接口沿用面板现有 Cookie 会话认证，没有单独的 API Key。设置访问密码后，先请求 `POST /api/auth/login`，保存响应中的 `tf_session` Cookie，后续请求携带该 Cookie。会话通常有效 30 天，过期或修改密码后重新登录；HTTPS 部署应全程使用 HTTPS。

下面用 `curl` 展示流程。`login.json` 是调用方在本机保存的忽略文件，内容为 `{"password":"<面板访问密码>"}`，不要把实际密码提交到仓库。`session.cookies` 同样按凭据保管。

```bash
BASE_URL='https://panel.example.com'

curl --fail-with-body -c session.cookies \
  -H 'Content-Type: application/json' \
  --data-binary @login.json \
  "$BASE_URL/api/auth/login"

curl --fail-with-body -b session.cookies --get \
  --data-urlencode 'strategy_id=trend_breakout' \
  --data-urlencode 'as_of=2026-09-04' \
  "$BASE_URL/api/screener/export"

curl --fail-with-body -b session.cookies --get \
  --data-urlencode 'strategy_id=trend_breakout' \
  --data-urlencode 'format=csv' \
  --output selection.csv \
  "$BASE_URL/api/screener/export"
```

接入程序只需代码时读取 JSON 的 `symbols`，或请求 `format=txt`；需要分析字段时读取 `results`。接口返回 `Cache-Control: no-store`。

| 状态码 | 含义与处理 |
| --- | --- |
| 200 | 成功；已运行且无命中时 `symbols=[]`、`total=0`，CSV 仅表头、TXT 为空 |
| 401 | 未登录或会话过期，重新登录 |
| 403 | 面板尚未初始化且请求来自公网，先在本机或内网初始化 |
| 404 | 没有可导出结果，或指定策略不存在、不支持股票日线 |
| 409 | 指定策略未运行、日期不一致、旧缓存缺少资产标记或结果不完整，重新运行所需日期的策略后再请求 |
| 422 | 参数格式错误，例如非法日期或不支持的文件格式 |
| 503 | 策略引擎尚未初始化 |

错误响应为 `{"detail":"具体原因"}`；422 的 `detail` 为参数校验错误数组。调用方应先检查 HTTP 状态，不能把错误正文作为股票文件导入。
