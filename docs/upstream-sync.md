# Upstream 版本同步

本仓库只同步 `shy3130/tickflow-stock-panel` 的语义化版本 Tag，不跟踪 upstream `main` 的普通提交。流程分成两个独立阶段：GitHub Actions 发现版本并创建 Issue；ChatGPT 桌面端定时任务在独立 worktree 中完成合并、冲突处理、验证和 Draft PR。

## 运行流程

1. `.github/workflows/upstream-release-monitor.yml` 每天北京时间 23:00 获取 upstream Tags。
2. `.github/scripts/select_upstream_release.py` 只接受 `^v?\d+\.\d+\.\d+$`，按数值版本选择最新 Tag，并把 Tag 固定到 40 位 commit SHA。
3. 新版本只创建一次 `[Upstream] 同步 <Tag>` Issue。已同步 Tag 被重新指向或删除时改为创建安全告警 Issue，禁止自动同步。
4. ChatGPT 定时任务每天北京时间 23:10 处理最新同步 Issue，自动解决普通代码冲突并运行完整验证。
5. 验证通过后由定时任务推送同步分支并创建 Draft PR；CI、独立 Review Agent 和维护者继续负责最终门禁。

## 创建 ChatGPT 定时任务

在 ChatGPT 桌面端为本仓库创建 project-scoped scheduled task：

- 项目：本仓库本地目录。
- 运行方式：每次使用新的独立 worktree。
- 时间：每天北京时间 23:10（`Asia/Shanghai`），自定义规则为 `RRULE:FREQ=DAILY;BYHOUR=23;BYMINUTE=10`。
- 推荐模型：使用创建任务时可用的最新 Codex 编码模型和较高 reasoning effort。
- 保存的任务提示：

  ```text
  完整执行仓库内 .github/prompts/upstream-release-sync.md 的 Upstream 版本同步流程。每轮至多处理一个最新的 open upstream-sync Issue；没有待处理 Issue 时只报告 no-op。禁止自动合并 PR。
  ```

本地项目任务依赖电脑开机、ChatGPT 桌面应用运行，以及项目目录、GitHub 凭据和代理可用。创建后应先手动运行一次，并检查前几次执行结果。Web scheduled task 不能直接使用本机项目目录，因此本流程不使用纯 Web 任务。参见 [OpenAI Scheduled tasks 文档](https://learn.chatgpt.com/docs/automations)。

## 首次 baseline

当前 downstream 与 upstream 没有共同 Git 历史。`.github/upstream-sync-state.json` 记录：

- upstream baseline commit：`99bdec875d4bbf5c30ddc43534b81ada0f3b0f6b`；
- 接入时已覆盖的最新版本：`v0.1.88`；
- 对应 Tag commit：`8ead30037a8806518e400dc26b67a7e5a1294282`。

`merge_base_commit` 初始与 baseline 相同。首次出现更高版本时，定时任务会先创建一个内容保持 downstream 不变的双亲 history-link merge commit，再正常合并新 release；每次同步后再把 `merge_base_commit` 更新为新 release commit。即使维护者误用 squash 导致 upstream 父提交丢失，下一次任务也能从上次已同步 commit 恢复正确共同祖先。`ours` strategy 只允许用于历史连接；release 合并冲突必须逐项解决。

## 安全边界

- workflow 不读取 Release notes 或把 upstream 文案送入 Codex，避免把外部内容当成指令。
- 定时任务只接受 `github-actions[bot]` 创建的同步 Issue；无仓库写权限作者的评论不能改变任务范围。
- Tag 改写或删除、同版本多 Tag 指向不同 commit、状态倒退都会 fail-closed。
- upstream 对同步状态、定时任务提示和 monitor workflow 的修改不会被自动接受。
- 认证、数据库迁移、金融数据契约、生产 workflow 和部署脚本发生冲突时不自动取舍。
- 自动化不直接修改 `main`，不自动合并 PR，也不触发生产部署。
- 同步 PR 应使用 merge commit 合并，以保留 upstream 父提交并减少下次同步的历史修复。
- GitHub 认证异常按 `docs/github-auth-troubleshooting.md` 分层诊断。
