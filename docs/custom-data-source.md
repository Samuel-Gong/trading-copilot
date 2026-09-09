# 自定义数据源接入

本项目默认使用 TickFlow。自定义数据源是一个可选扩展: 外部 HTTP 服务负责取数和整理, 本项目只把返回结果映射成内部标准字段, 然后复用现有存储、指标、enriched、策略和前端展示逻辑。

## 支持范围

当前自定义源支持七类数据:

| 数据集 | 配置名 | 说明 |
| --- | --- | --- |
| 日K | `daily` | 批量返回一组股票在指定区间内的日K |
| 标的维表 | `instruments` | 返回股票池及名称、上市日期、当前股本等字段；至少提供 `symbol` |
| 除权因子 | `adj_factor` | 批量返回一组股票的复权因子 |
| 实时行情 | `realtime` | 返回全市场快照,用于盘中 enriched 增量计算 |
| 分钟K | `minute` | 返回 1m 分钟K(需映射出 symbol / datetime / OHLC / 量额) |
| 全量分钟 | `full_minute` | 与 `minute` 同形;声明后可被路由为「全量分钟」生效源,内置服务盘中按当日窗口全市场批量落盘(仅修复轮语义,节奏下限 60s) |
| 财务数据 | `financial` | 一个配置覆盖全部财务表，请求时把表名作为参数传给上游；必须映射 `symbol`、`period_end`、`announce_date`，比例指标还要声明单位 |

深度盘口(depth5)暂无数据集契约,仍由 TickFlow 提供。

选择自定义日K源时，必须同时声明 `daily` 和 `instruments`，避免行情源与股票池不一致。旧配置若缺少维表能力会显示为不可用，不会静默切换到 TickFlow；补齐后重新加载并选择即可。维表中的当前股本不用于填充历史回测，历史股本必须通过财务公告日期确定可用时点。

`full_minute` 声明式源只提供修复轮(当日窗口批量);廉价增量端点
(`get_intraday_latest`)是 Python 插件契约,见
[plugin-development.md](./plugin-development.md)。

## 配置位置

把 YAML 放到运行数据目录下:

```text
data/data_sources/*.yaml
```

Dev 模式下，默认位置是项目根目录的 `data/`；Docker 部署中，项目的 `data/` 会挂载为容器内的 `/app/data`。可通过 `DATA_DIR` 覆盖。

修改 YAML 后可在「设置 -> 数据源」点击「重新加载」,或调用:

```bash
curl -X POST http://127.0.0.1:3018/api/settings/data-sources/reload
```

## 最小 YAML

```yaml
name: mock_source
display_name: "Mock 自定义数据源"
auth:
  type: none

datasets:
  instruments:
    url: http://127.0.0.1:3021/instruments
    method: GET
    response_path: data
    field_map:
      ts_code: symbol
      name: name

  daily:
    url: http://127.0.0.1:3021/daily
    method: POST
    batch: 100
    rpm: 200
    volume_unit: lots
    response_path: data
    field_map:
      ts_code: symbol
      trade_date: date
      open: open
      high: high
      low: low
      close: close
      vol: volume
      amt: amount
    transforms:
      date: "parse_date(value, '%Y-%m-%d')"

  adj_factor:
    url: http://127.0.0.1:3021/adj_factor
    method: POST
    batch: 100
    rpm: 200
    adj_factor_kind: event_ratio
    response_path: data
    field_map:
      ts_code: symbol
      trade_date: trade_date
      factor: ex_factor
    transforms:
      trade_date: "parse_date(value, '%Y-%m-%d')"

  realtime:
    url: http://127.0.0.1:3021/realtime
    method: GET
    rpm: 60
    pct_unit: decimal
    volume_unit: lots
    response_path: data
    field_map:
      ts_code: symbol
      name: name
      last: last_price
      pre_close: prev_close
      open: open
      high: high
      low: low
      vol: volume
      amt: amount
      pct: change_pct
      amount_change: change_amount
      amplitude: amplitude
      turnover: turnover_rate
      timestamp: timestamp
```

## 字段契约

### daily 必填

| 内部字段 | 含义 |
| --- | --- |
| `symbol` | 标准代码,如 `000001.SZ` |
| `date` | 交易日 |
| `open` / `high` / `low` / `close` | 不复权 OHLC |
| `volume` | 成交量；内部单位为手，配置必须声明 `volume_unit` |
| `amount` | 成交额 |

### adj_factor 必填

| 内部字段 | 含义 |
| --- | --- |
| `symbol` | 标准代码 |
| `trade_date` | 除权日期 |
| `ex_factor` | 单次除权事件的 pre/post 比值 |

`adj_factor` 必须声明 `adj_factor_kind`。`event_ratio` 表示接口已返回单次事件比值；
`cumulative` 表示接口返回按日期递增的累计因子，Provider 会转换为相邻累计值之比，
避免把累计序列再次连乘。

### realtime 必填

| 内部字段 | 含义 |
| --- | --- |
| `symbol` | 标准代码 |
| `timestamp` | 服务端正整数 Unix epoch 毫秒；用于换算北京交易日归属，缺失或无效时整份快照 fail-closed |
| `last_price` | 最新价 |
| `prev_close` | 昨收 |
| `open` / `high` / `low` | 当日 OHLC |
| `volume` | 成交量；内部单位为手，配置必须声明 `volume_unit` |

### financial 必填与单位

所有财务表都必须映射 `symbol`、`period_end`（报告期）和 `announce_date`（公告日）。
缺少公告时间的数据无法满足 point-in-time 契约，会被拒绝而不会进入策略或回测。

当 `field_map` 映射 `roe`、`gross_margin`、`net_margin`、`revenue_yoy`、
`net_income_yoy` 或 `debt_to_asset_ratio` 时，必须声明 `pct_unit`：

- `percent`：上游 `20` 表示 `20%`，内部保持为 `20`；
- `decimal`：上游 `0.2` 表示 `20%`，写入前乘以 `100`。

建议实时接口额外提供 `amount`、`change_pct`、`change_amount`、`amplitude`、`turnover_rate`、`name`。缺失时部分字段会由 pipeline 回算,但精度取决于可用输入。

`change_pct`、`amplitude`、`turnover_rate` 统一使用小数制,例如 `0.0366` 表示 `3.66%`。百分制单位必须在 realtime 数据集上**显式声明**,不做数值猜测(数值无法区分两种单位:`0.05` 既可能是 0.05% 也可能是 5%):

```yaml
datasets:
  realtime:
    url: https://api.example.com/snapshot
    pct_unit: percent   # 接口返回 3.66 表示 3.66%;小数制源必须声明 decimal
```

处理规则:

| 声明 | 行为 |
| --- | --- |
| `pct_unit: percent` | `change_pct` / `amplitude` / `turnover_rate` 无条件 `/100` |
| `pct_unit: decimal` | 三列原样透传 |
| 未声明 | 配置校验失败;防御性运行路径会把所有比例列置 `None`,绝不根据数值猜测 |

只要 `field_map` 映射了任一比例字段,即使同时配置了 `transforms`,也必须声明 `pct_unit`。

`daily`、`realtime`、`minute` 或 `full_minute` 只要映射 `volume`，都必须显式声明：

- `volume_unit: lots`：上游已按手返回，原样使用；
- `volume_unit: shares`：上游按股返回，写入前除以 `100`。

## 请求约定

- `daily` / `adj_factor` 会按 `batch` 切分 symbols。
- POST 请求会发送 JSON body: `symbols`、`start_time`、`end_time`。
- GET 请求会发送 query 参数: `symbols=000001.SZ,600000.SH`。
- `realtime` 必须是全市场快照接口,不支持逐个 symbol 拉实时行情。

可通过这些字段改参数名:

```yaml
symbols_param: symbols
start_param: start_time
end_param: end_time
```

分钟数据源如果需要区分资产类型或周期，可继续配置：

```yaml
asset_type_param: asset_type
freq_param: period
```

配置后，分钟请求会分别传入 `stock` / `etf` / `index` 和 `1m`；留空时不向上游发送这两个参数，以兼容已有数据源。

### 请求超时

每个数据集可单独配置请求超时（秒），默认 30：

```yaml
timeout: 60
```

留空或省略时用默认 30 秒，可配置范围为大于 0 且不超过 300 秒；该值对数据同步与「试拉测试」均生效。在设置页编辑数据源时可在「超时」输入框修改（与 批量 / RPM / 响应路径 同行）。「试拉测试」直接使用当前表单内容，新建数据源或尚未保存的修改也可测试。

## 鉴权

支持三种简单鉴权:

```yaml
auth:
  type: bearer
  token_env: MY_DATA_TOKEN
```

```yaml
auth:
  type: header
  header: X-Token
  token_env: MY_DATA_TOKEN
```

```yaml
auth:
  type: query
  param: token
  token_env: MY_DATA_TOKEN
```

启用鉴权时 `token_env` 必填，且必须是合法环境变量名。Token 可以放在系统环境变量或项目 `.env` 中；变量未设置时请求会直接失败，不会以匿名方式访问上游。

## 联调流程

1. 启动 mock 数据源:

```bash
cd docs/examples/custom-data-source
python mock_server.py
```

2. 复制示例配置:

```bash
mkdir -p data/data_sources
cp docs/examples/custom-data-source/mock_source.yaml data/data_sources/mock_source.yaml
```

3. 在「设置 -> 数据源」点击「重新加载」。

4. 使用「试拉测试」选择 `mock_source` 和 `daily` / `adj_factor` / `realtime`。

5. 保存数据源选择:

- 日K: `mock_source`
- 除权因子: `mock_source` (或保持默认 `tickflow`)
- 实时行情: `mock_source`

6. 触发同步或开启实时行情。

## 常见错误

| 现象 | 处理 |
| --- | --- |
| 列表里没有 custom 源 | 检查 YAML 是否放在 `data/data_sources/` 并点击重新加载 |
| errors 提示 missing mapped fields | `field_map` 没映射到必填内部字段 |
| 试拉 rows 为 0 | 检查 `response_path` 是否指向数组 |
| 日期列全为空 | 检查 `parse_date` 的格式是否和返回值一致 |
| 实时行情没刷新 | 确认实时数据源已保存为 custom,且返回全市场快照 |

## 用 AI 生成映射配置

如果你的数据源 API 文档比较复杂,可以把 API 文档和返回示例丢给 AI,让它帮你生成 `field_map` 和 YAML 配置。

### 操作步骤

1. 从你的数据源获取 API 文档(接口地址、请求方式、返回字段说明)
2. 试拉一次,拿到返回的 JSON 示例
3. 把下面的 prompt 模板 + API 文档 + JSON 示例一起发给 AI
4. 把 AI 生成的 YAML 贴到 `data/data_sources/xxx.yaml`
5. 在设置页点「重新加载」,再「试拉测试」验证

### Prompt 模板

复制以下内容发给 AI(替换方括号部分):

```text
我在配置一个自定义数据源接入股票面板。请根据我的 API 文档和返回示例,生成 YAML 配置。

要求:
1. 输出标准 YAML 配置,包含 name / display_name / auth / datasets
2. 每个数据集的 field_map 把我的接口字段名映射到内部字段名
3. 日期类字段如果格式不是 YYYY-MM-DD, 加上 transforms 里的 parse_date
4. 只配置我能提供的接口, 不存在的数据集不要写

内部字段对照表:

日K (daily):
  symbol = 股票代码, 格式 000001.SZ / 600000.SH
  date = 交易日期
  open / high / low / close = OHLC
  volume = 成交量
  amount = 成交额

除权因子 (adj_factor):
  symbol = 股票代码
  trade_date = 除权日期
  ex_factor = 复权因子（需声明 adj_factor_kind: event_ratio 或 cumulative）

实时行情 (realtime):
  symbol = 股票代码
  last_price = 最新价
  prev_close = 昨收价
  open / high / low = 当日 OHLC
  volume = 成交量（需声明 volume_unit: lots 或 shares）
  amount = 成交额
  change_pct = 涨跌幅 (小数, 0.0366 = 3.66%)
  change_amount = 涨跌额
  amplitude = 振幅 (小数, 0.024 = 2.4%)
  turnover_rate = 换手率 (小数, 0.05 = 5%)
  # 上游若返回百分数值 (3.66 表示 3.66%), 在 realtime 数据集声明 pct_unit: percent,
  # 不要依赖数值自动识别; 逐列转换也可用 transforms: turnover_rate: "value / 100"
  # daily/realtime/minute/full_minute 映射 volume 时必须声明 volume_unit: lots 或 shares

分钟K (minute) 与 全量分钟 (full_minute, 字段同 minute):
  symbol = 股票代码
  # datetime 必须是北京时间墙钟 (如 2026-08-28 09:35:00), 不要返回 UTC;
  # 入口守卫会自动纠偏 UTC 特征帧, 但契约仍要求源头写对
  datetime = 北京时间墙钟 (YYYY-MM-DD HH:MM:SS)
  open / high / low / close = OHLC
  volume = 成交量
  amount = 成交额

=== 我的 API 文档 ===
[把你的接口文档贴这里: URL / 请求方式 / 参数 / 返回字段说明]

=== 返回 JSON 示例 ===
[把试拉的 JSON 返回贴这里]
```

AI 会输出类似这样的结果:

```yaml
name: my_source
display_name: "我的数据源"
auth:
  type: bearer
  token_env: MY_API_TOKEN

datasets:
  daily:
    url: https://api.example.com/kline
    method: POST
    batch: 100
    rpm: 200
    response_path: data.list
    field_map:
      ts_code: symbol
      trade_date: date
      open: open
      vol: volume
    transforms:
      date: "parse_date(value, '%Y%m%d')"
```

把这段 YAML 保存为 `data/data_sources/my_source.yaml`,然后在设置页重新加载即可。
