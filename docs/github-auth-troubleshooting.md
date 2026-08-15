# GitHub 认证分层诊断

当 Codex 沙箱、用户终端或不同 GitHub 工具给出相互矛盾的认证结果时，按本文诊断。目标是定位失败层，不泄漏凭据，也不让用户执行无意义的重复登录。

## 1. 先区分三层凭据

| 层级 | 主要用途 | 凭据位置与边界 |
| --- | --- | --- |
| GitHub 连接器 | 读取或操作 Issue、PR、Review 等连接器支持的资源 | 由 Codex 应用托管；连接成功不代表本地命令行已登录，也不能把连接器 Token 导出给 shell |
| `gh` CLI | 执行 `gh issue`、`gh pr`、`gh run` 等命令 | 通常由 `gh` 配置和系统 Keychain 提供；沙箱可能无法读取用户终端可见的 Keychain 项 |
| HTTPS Git | 执行 `git fetch`、`git push` 等 Git 传输 | 由 Git credential helper（macOS 常见为 `osxkeychain`）提供；它与 `gh` 的认证状态不能互相代替 |

因此：连接器可用不表示 `gh` 或 `git push` 一定可用；`gh auth status` 失败也不表示 HTTPS Git 凭据已经失效。

## 2. 安全诊断顺序

1. **确认实际需要的层。** Issue、PR 或 Review 操作优先使用已连接的 GitHub 连接器；本地提交的 fetch/push 使用 Git；只有连接器不能覆盖的命令行操作才依赖 `gh`。不要为了验证一个层而改动另一个层的认证配置。
2. **确认命令、目标和覆盖来源。** 先从 Git remote 确认目标主机，再检查 `gh` 的路径与版本、remote 协议和 `credential.helper`。只提取并报告 remote 的协议与主机，不回传可能包含用户名、凭据或本机路径的完整 URL；发现 URL 内嵌凭据时停止探测并改用 credential helper。裸 `gh auth status` 会检查所有已知主机和账号，任一陈旧账号失败都可能让命令返回失败，因此不得用它的汇总退出码判断目标账号。只报告 `GH_TOKEN`、`GITHUB_TOKEN`、`GH_ENTERPRISE_TOKEN`、`GITHUB_ENTERPRISE_TOKEN`、`GH_CONFIG_DIR`、`XDG_CONFIG_HOME` 等变量是否存在，不输出变量值或解析后的本机路径；环境 Token 可能覆盖 Keychain 中的有效凭据。`gh` 的配置目录依次受 `GH_CONFIG_DIR`、`XDG_CONFIG_HOME`、Windows 的 `%AppData%` 或 Unix/macOS 的 `$HOME` 用户配置根影响，沙箱与用户终端必须解析到预期的同一用户配置范围。
3. **先做沙箱内的脱敏探测。** 对目标主机执行 `gh auth status --active --hostname <remote-host>`，只判断该主机的活动账号。该命令的 stdout 和 stderr 必须在命令层同时重定向到平台空设备（例如 Unix 的 `/dev/null`），只根据退出状态报告“成功”或“失败”等脱敏分类。再用 stdout 和 stderr 均已重定向的 `gh auth token --hostname <remote-host>` 或 Keychain 查询判断“凭据是否可读”。禁止执行会打印 Token 的命令，禁止使用 `security ... -g`，禁止开启会回显命令参数或环境变量的 shell trace。
4. **识别沙箱假阴性。** 如果沙箱内针对目标活动账号的 status 检查失败，同时对应主机的 Token 和 Keychain 查询均无法读取凭据，应先怀疑沙箱权限边界，不能直接判定 Token 失效，也不能立即要求用户重新登录。
5. **在沙箱外复查。** 取得批准后，在沙箱外重新执行同一条针对目标活动账号的 status 检查，仍须在命令层同时重定向 stdout 和 stderr，只回传脱敏分类。保留执行环境已经注入的 `HTTP_PROXY`/`http_proxy`、`HTTPS_PROXY`/`https_proxy`、`ALL_PROXY`/`all_proxy`，不得用固定地址覆盖任一大小写形式的有效配置；当前本机开发环境未注入这些代理变量时，才按仓库约定显式设置为 `http://127.0.0.1:7897`，CI 或其他主机使用其受控配置。如果凭据检查失败且存在环境 Token、`GH_CONFIG_DIR` 或 `XDG_CONFIG_HOME`，应在不修改原环境的子进程中排除相关覆盖后复查默认配置与 Keychain，或验证并修正这些变量的受控来源。还必须确认沙箱外进程使用用户预期的 Windows `%AppData%` 或 Unix/macOS `$HOME` 配置根；这些用户配置根不应简单清空，若范围不符，应切换到正确的用户会话再复查。环境 Token 优先于已存凭据，配置目录覆盖会选择另一套配置，重新登录不能证明默认凭据已经失效。若沙箱外成功，应记录为“凭据有效，沙箱不可见”；若仍失败，再分别排查代理连通性、活动账号选择和凭据有效性。
6. **独立验证 Git 传输。** fetch/push 是否可用以对应的 HTTPS Git 操作为准，不能从 `gh` 结果推断。只验证读取可达性时使用不会更新本地引用的 `git ls-remote`；公共 remote 可能匿名成功，因此该结果只能记录为“读取可达”，不得据此记录 HTTPS Git 凭据有效。只有私有 remote 等明确要求认证的读取成功，或已授权范围内的其他认证必需操作成功，才能记录对应凭据已通过认证。验证 push 能力时必须使用指向已确认 remote 和精确 refspec 的 `git push --dry-run --no-verify`，既不发布远端引用，也不执行可能产生副作用或掩盖认证结果的 `pre-push` hook；失败结果同时可能来自权限不足、网络或凭据问题，不得直接判定 Token 失效。两类探测都须设置 `GIT_TERMINAL_PROMPT=0` 禁用交互式凭据提示，在命令层同时重定向 stdout 和 stderr，并只回传脱敏分类。真实 push 是独立写操作，只有取得明确授权后才能执行。验证时继续使用当前执行环境经确认的 GitHub 代理。
7. **最后才重新登录目标主机。** 只有在沙箱外针对目标主机活动账号的 `gh` 检查也失败、代理正常、账号选择无误且环境凭据与配置目录覆盖已经排除或修正后，才执行 `gh auth login --hostname <remote-host>`。登录进程本身不得继承已判定失效的 `GH_TOKEN`、`GITHUB_TOKEN`、`GH_ENTERPRISE_TOKEN` 或 `GITHUB_ENTERPRISE_TOKEN`；应先修正其受控来源，或只在该登录子进程中排除相应变量。`GH_CONFIG_DIR` 和 `XDG_CONFIG_HOME` 必须先确认会选择预期配置目录，否则在同一子进程中排除；Windows `%AppData%` 或 Unix/macOS `$HOME` 也必须属于预期用户会话，避免把新凭据写入错误位置。交互式登录由用户在自己的终端完成，不得让设备码、浏览器登录地址或提示输出进入工具回传。诊断和登录期间不得修改父进程环境，也不得删除现有 Keychain 项。

## 3. 结论记录格式

只记录以下脱敏信息：

- 失败的是连接器、`gh` 还是 HTTPS Git；
- HTTPS Git 的结果是“公共读取可达”还是“认证必需操作成功”，前者不得记录为凭据有效；
- 结果来自沙箱内还是经批准的沙箱外环境；
- 代理是否按仓库约定显式设置；
- 凭据是“可读且有效”“不可读”还是“可读但验证失败”；
- 下一步针对哪一层，不把某一层的结论外推到其他层。

不得把 Token、Cookie、Keychain 密文、完整认证日志或本机绝对路径写入终端回传、Issue、PR、测试夹具或提交内容。
