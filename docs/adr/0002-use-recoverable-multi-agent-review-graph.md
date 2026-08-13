# 每日复盘使用 TradingAgents 风格的可恢复研究 Graph

每日复盘的策略候选和持仓分析使用同一套 13 节点拓扑。实现参考 TradingAgents 的角色分工和阶段顺序,但不导入或执行参考仓代码。

每日复盘外层是严格的三阶段状态机：`市场环境 → 策略候选 → 持仓分析`。只有前一阶段完成，后一阶段才会启动；前一阶段失败或中断时，后续阶段显式记录为“前置阻塞”。策略候选阶段包含选股页策略池筛选和全部候选 Graph，持仓分析只有在所有候选 Graph 完成后才启动。用户重试失败项或从候选 Graph 节点恢复后，状态机会从 checkpoint 继续，并在前置阶段成功时自动释放下游。

运行路径如下:

1. `研究事实` 冻结目标身份,并装配截至复盘日的行情、技术指标、关键价位、公告日合规的财务材料和发布时间合规的新闻证据。
2. `Market Analyst`、`Sentiment Analyst`、`News Analyst` 和 `Fundamentals Analyst` 并行读取事实。News Analyst 只能使用档案内冻结的新闻证据；缺少新闻、情绪或财务材料时,节点必须记录数据缺口。
3. `Bull Researcher` 与 `Bear Researcher` 共享一份持久化辩论状态，按 `Bull₁ → Bear₁ → Bull₂ → Bear₂` 交替发言。每次发言读取完整历史和对方上一轮观点；四次发言完成后，`Research Manager` 才裁决共识、分歧和证据等级。
4. `Trader` 只整理后续研究观察方案。它不能生成买卖、仓位、目标价或其他交易动作。
5. `Aggressive Analyst`、`Conservative Analyst` 和 `Neutral Analyst` 依次审查风险,`Portfolio Manager` 汇总最终研究结论。Graph 到此结束,没有 Execution 节点。

Graph 是运行记录，不是页面上的静态示意图。每个节点持久化状态、尝试次数、结构化输入、结构化输出、错误和时间戳。研究辩论额外保存当前轮次、当前发言方、成功发言数以及每次尝试的输入、输出和错误。每条边持久化等待、就绪、流动中、完成、失败或阻塞状态。前端轮询档案后，按真实边状态显示动态数据流；用户点击 Bull、Bear 或 Research Manager 可以查看完整辩论记录。

普通节点失败时，手动恢复只清空所选节点及其依赖下游，不重跑已完成的兄弟节点。Bull 或 Bear 失败时，系统把辩论本身作为 checkpoint：此前成功发言和失败审计继续保留，恢复从当前逻辑发言继续，再运行剩余发言和 Research Manager。反馈边表达真实的下一轮发言方向，但不参与通用依赖遍历，因此不会把恢复范围扩成无限循环。

运行态和设置页使用 `daily_analysis_graph.graph_definition()` 返回的同一份节点、边、坐标和 Prompt 定义。“设置 → 分析 Agent”展示静态拓扑、实际 System Prompt、User Prompt 模板以及输入输出要求,避免前端另存一套会漂移的配置说明。

实现继续复用当前 `StrategyEngine`、`ScreenerService`、选股页策略池、行情仓库、关键价位计算和 AI provider。每日复盘启动请求会把当前浏览器中选定的股票日线策略 ID 冻结进档案；空策略池保持为空，不回退到全部策略。它不依赖参考仓的数据库、CandidatePool 或 TradingAgents 包,也不连接券商或交易执行系统。
