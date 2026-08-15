# AI 开发入口

修改、调试或审查本仓库前，必须完整阅读并遵循根目录的 [`CONTRIBUTING.md`](CONTRIBUTING.md)。其中定义了项目架构、数据契约、数据源插件化、缓存与性能要求、测试矩阵以及 PR 复审和合并标准。

同时遵守以下规则：

- 先理解调用链和现有测试，再进行修改。
- 保持实现简单、改动范围最小，不处理无关问题。
- 不覆盖工作区已有修改，不虚构测试或审查结果。
- 以实际验证结果作为完成标准。
- Issue/PR 驱动的开发执行 [`CONTRIBUTING.md` 的 Issue-first 与评论闭环](CONTRIBUTING.md#91-issue-first-开发与评论闭环)：修改前和每轮反馈后都重新读取关联 Issue、PR 评论及未解决 Review threads，作者与最终 Review Agent 分离。
- 登录本地 TickFlow 面板（`localhost` / `127.0.0.1`）时，读取根目录的 [`.agent-secrets.md`](.agent-secrets.md)，其中凭据仅限本机登录使用，不得复制到提交内容、日志或对外请求。
- 启动本项目应用进程时，必须在启动命令中清除大小写的 `HTTP_PROXY`、`HTTPS_PROXY`、`ALL_PROXY`，避免应用继承 Codex shell 代理；该例外仅适用于应用进程，其他外部网络请求仍遵循全局代理约定。

## Code Review Rules

### Secret 与用户数据

- 发现提交内容、测试夹具、日志、Issue/PR 文案或发布包可能包含真实密码、API Key、Cookie、SSH Key、持仓交易数据或本机绝对路径时，按 P0/P1 阻断；安全路径是使用占位值或脱敏的最小合成数据，并把真实值保留在 GitHub Secret 或本机/服务器忽略文件中。

### 金融时点与数据口径

- 发现历史复盘、持仓、行情、财务或新闻读取了业务日期之后的信息，或混用了单位、复权、交易日、时区和资产类型时，按 P0/P1 阻断；安全路径是沿用仓库现有 point-in-time 截断、标准化与 Provider 能力边界，并补负例测试。

### 生产发布边界

- 发现生产部署可从非 `main` 分支触发、执行未审查的服务器源码、绕过不可变发布包/SHA 探活，或让上传账号获得任意 shell/root 能力时，按 P0/P1 阻断；安全路径是 GitHub-hosted runner 传输当前 SHA 的发布包，由受控服务器部署器完成原子切换、探活和失败回滚。
