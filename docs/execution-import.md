# 同花顺成交导入 v1

桌面端负责读取所选范围、显式确认来源账户、保存批次并在 preview 后发送 commit。接收端只记录成交，不执行买卖或撤单。

## 接口

沿用 `POST /api/auth/login` 和 `tf_session` Cookie。`GET /api/portfolio/accounts` 返回 `{ "items": [{ "id": "…", "name": "…" }] }`（账户可含既有时间字段）。

`POST /api/portfolio/execution-imports` 接受：

```json
{
  "schema_version": 1,
  "batch_id": "00000000-0000-4000-8000-000000000001",
  "source": "tonghuashun",
  "source_account_id": "synthetic-broker",
  "account_id": "使用账户接口返回的目标 ID",
  "mode": "preview",
  "items": [{
    "source_record_id": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    "identity_kind": "row_fingerprint",
    "executed_at": "2026-07-30T02:00:00.000Z",
    "trade_date": "2026-07-30",
    "stock_code": "600000",
    "stock_name": "合成测试标的",
    "side": "buy",
    "quantity": 100,
    "price": 10.123,
    "amount": 1012.34,
    "contract_number": null,
    "order_reference": null,
    "fee": null,
    "tax": null
  }]
}
```

`batch_id` 为客户端持久化 UUID；重试只改变 `mode`，不能改变记录内容、顺序、来源或目标。1–2000 行；行指纹为 64 位十六进制 SHA-256；成交时间必须带时区，上海日期必须匹配且不能在未来。数量、价格、金额为有限正数；费用为有限非负数或 null。当前只支持普通 buy/sell 及证券目录中可唯一解析的 A 股、场内 ETF。名称与资产类型以本地证券目录为准。

成功响应保留输入顺序，逐行一一对应：

```json
{
  "schema_version": 1,
  "batch_id": "00000000-0000-4000-8000-000000000001",
  "mode": "preview",
  "items": [{
    "source_record_id": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    "status": "ready",
    "trade_id": null,
    "message": ""
  }]
}
```

preview 仅返回 ready、duplicate、conflict、unsupported。commit 成功仅返回 inserted、duplicate，均带非空 trade_id。commit 重新检查当前账本，任一行不安全即返回 HTTP 409，响应仍为上述逐行结构，整批不写入。非法请求 422、目标不存在 404、无法安全读取账本 409、服务失败 500 使用固定 `detail`，不回显来源账户或请求。未登录沿用认证响应 401。

## 幂等、排序和金额

- preview 持久化批次内容摘要，但不写交易、不预留来源身份。同一批次异内容直接 409；同一内容可反复 preview/commit。
- 来源键为 source＋source_account_id＋source_record_id，与目标账户及内容摘要绑定。绑定和交易在现有写入锁内一次原子替换账本；不同批次、回执丢失、进程重启不重复入账。
- 删除来源交易保留绑定墓碑，重发返回 conflict，不重新插入。导入交易允许按既有流程校准费用；不允许手工修改其数量、价格、日期或顺序，以免破坏来源事实。重试不会清零或覆盖已校准费用。
- 相同合同/委托的多行可能是累计更新，v1 保守阻止；合同号、委托号不当作成交编号。相同指纹重复行、同账户同证券同日的同秒多行也必须人工核对。
- 同一来源账户、不同时刻、不同合同/委托的独立分笔可以写入，即使数量与价格相同。不同来源账户的自然键候选会阻止，避免来源别名变化导致重复。
- 同账户、同证券、同日存在没有 executed_at 的手工或交割单流水时，v1 明确阻止混入，因为无法确定相对顺序。更早日期的既有买入可以参与回放。新导入记录按真实时间分配既有 seq 槽位，其他流水保持相对顺序，不采用交割单的同日先买后卖规则。
- commit 做全账本 FIFO 校验，历史补录不能破坏后续卖出；缺少买入历史时整批拒绝，不生成虚构买入。
- 原始 amount 参与 FIFO 成本及持仓页分组净金额；旧流水缺少 amount 时仍使用 quantity×price。允许三位展示均价和分位金额舍入可解释的差异：绝对差不超过 quantity×0.0005＋0.005；超出范围拒绝。保留原始展示 price，不把金额差异伪装成费用。
- fee 或 tax 为 null 时仅估算未知项并标记 estimated，已提供项保留；估算基于原始 amount。来源绑定只比较原始导入内容，不将后续费用校准误判为重复写入。
- 买入移除观察池中的持有标的；清仓沿用现有监控清理。duplicate 重试也重新尝试清理，覆盖回执丢失和提交后进程退出的情况。

## 首次历史冲突的处理

保留桌面持久化批次，在 Trading Copilot 中查看目标账户的历史流水，并核对券商原始逐笔成交编号、日期、数量和费用。v1 不提供自动绑定或合并手工历史的接口；有重叠的范围不要提交。用户核实后可选择不重叠的采集范围，生成新批次；有累计或同秒歧义的范围须待后续支持独立成交编号后处理。不要删除历史来尝试绕过去重，也不要更改来源别名或自造 trade_id。

## 兼容和运行限制

桌面 v1 的路径、请求字段、成功状态及响应结构不变。preview 会写批次摘要，这属于新增的服务端持久状态；客户端不需要增加请求。旧账本保持 schema_version=4，新增可选 execution_imports 字段，旧数据读取沿用已有迁移与备份，未导入流水仍可正常编辑。

旧服务版本不认识来源元数据，写入账本时可能丢弃绑定，且不能按原始 amount 计算。因此回滚应用后应暂停所有账本写入，并保留完整账本文件；不能删除元数据后重新导入。源记录存在身份歧义时宁可拒绝，不宣称对所有同花顺页面都能可靠导入。

并发边界沿用现有进程级 RLock，服务应保持单进程运行；不支持多个服务进程同时写同一个 JSON 账本。批次和来源绑定不自动过期，保证跨重启幂等。该接口是显式批处理，不进入行情实时计算路径；处理成本随请求及账本规模增长，未声称性能提升。

## 隔离联调

从本功能 Worktree 启动，不复制真实 data、配置或凭据。后端初始化命令：

```bash
cd backend
uv sync --frozen --no-install-project --extra dev --python 3.12
```

通过环境变量提供自行选择的临时测试密码（至少 6 位），不要使用真实面板密码。启动前清除代理：

```bash
env -u HTTP_PROXY -u HTTPS_PROXY -u ALL_PROXY \
  -u http_proxy -u https_proxy -u all_proxy \
  EXECUTION_SANDBOX_PORT=3038 \
  uv run --no-sync python -m scripts.execution_import_sandbox
```

启动命令会读取 `EXECUTION_SANDBOX_PASSWORD`。服务只监听 `127.0.0.1:3038`，每次启动新建临时目录，只有一个“合成联调账户”和 `600000.SH` 合成证券目录；退出时删除这次合成数据。桌面端以此地址登录，再从账户接口取 ID。预览、提交、丢失回执重试可以在该环境执行；如需验证真实进程重启的持久性，使用自动化测试的独立目录。不要将联调地址设成生产服务。

可选先执行 `pnpm --dir frontend install --frozen-lockfile`、`pnpm --dir frontend build`，沙盒会托管构建后的页面供持仓检查。沙盒只提供认证和持仓接口，侧栏行情及其他业务 API 不可用，不启动行情网络任务。

回归命令（backend 目录）：

```bash
uv run --no-sync python -m pytest tests/test_execution_import.py tests/test_portfolio_trade_ledger.py tests/test_portfolio_api.py tests/test_portfolio_statement_import.py tests/test_trade_fees.py tests/test_portfolio_price_monitors.py -q
```
