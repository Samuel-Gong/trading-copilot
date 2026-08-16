# UCloud 单生产环境发布与运维

本文档定义 TickFlow 在 UCloud 上的正式发布流程。当前部署采用 **Linux 源码发布包 + systemd + Nginx**，不使用 Docker，也不设置 staging 环境。本机只用于开发和验证，UCloud 是唯一长期运行的生产环境。

## 1. 发布原则

- `main` 是唯一允许部署到生产环境的分支。
- `main` 受分支保护：改动必须经过 PR、后端/前端 CI 和讨论解决，由维护者人工 Merge。
- 合并到 `main` 后自动构建按 SHA 标识的发布包并部署生产；人工 Merge 即生产发布批准，因此正常 PR 只在非交易时段合并。
- GitHub `production` Environment 只允许 `main`。`workflow_dispatch` 仅用于重试 `main` 当前 SHA，不允许选择其他分支发布。
- 生产目录禁止直接执行 `git pull`，也禁止在服务器上临时修改源码。
- 每个发布包由完整 Git SHA 标识；服务器用版本目录保存，不覆盖旧版本。
- `.env`、API Key、访问密码、用户数据和备份只保存在服务器，不进入发布包和 Git。
- 同一时间只允许一个生产进程访问 `/var/lib/tickflow/data`，避免重复调度、重复通知和并发写入。
- 正常发布安排在非交易时段；盘中除紧急故障外不发布。

## 2. 环境与目录

生产服务器使用以下固定目录：

```text
/opt/tickflow/
├── current -> releases/<git-sha>
├── previous -> releases/<previous-git-sha>
└── releases/
    └── <git-sha>/
        ├── .release.env
        ├── backend/
        │   └── .venv/
        ├── frontend/dist/
        ├── tiers.yaml
        └── VERSION

/var/lib/tickflow/
├── data/
├── backups/
├── incoming/
├── deploy-queue/
└── deploy-results/

/etc/tickflow/
└── tickflow.env

/home/tickflow-deploy/
└── incoming/
```

职责边界：

- `/opt/tickflow/releases/<git-sha>`：按 Git SHA 安装的应用版本和该版本自己的 Python `.venv`；目录由服务用户持有。
- `/opt/tickflow/current`：当前运行版本，部署时原子切换软链接。
- `/opt/tickflow/previous`：最近一个可手工回滚的版本。
- `/var/lib/tickflow/data`：唯一生产数据目录，发布与代码回滚都不会删除。
- `/var/lib/tickflow/backups`：部署前创建的数据快照，默认保留最近 5 份。
- `/var/lib/tickflow/incoming`：root 专用的发布包快照目录，避免上传用户在校验后替换归档。
- `/var/lib/tickflow/deploy-queue`：SSH 提交器固定下来的 root 发布队列。
- `/var/lib/tickflow/deploy-results`：服务器托管部署任务的持久状态；目录为 `root:tickflow-deploy 0750`、状态文件为 `root:tickflow-deploy 0640`，供 Actions 使用的低权限账号只读轮询。
- `/home/tickflow-deploy/incoming`：GitHub-hosted job 使用低权限 SSH 用户上传本次发布包的固定目录。
- `/etc/tickflow/tickflow.env`：生产密钥与配置，权限必须为 `600`。

不要把本机 macOS 的 `.venv` 复制到 UCloud。虚拟环境不可跨操作系统迁移；部署器会以现有 `ubuntu` 服务用户在每个 Linux release 目录中执行锁定的 `uv sync`。`uv` 使用 `/home/ubuntu` 下该用户的默认全局缓存，不设置单独的 `UV_CACHE_DIR`，也不覆盖默认 link mode。

release 在依赖安装后继续由 `ubuntu` 持有。这样即使 `.venv` 与全局 `uv` cache 共享 inode，部署器也不会通过递归 `chown root:root` 把 cache 文件变成服务用户不可写。当前信任模型把 UCloud 生产机及其 `ubuntu` 管理账号视为可信运维边界，因此不通过 root 所有权、systemd 只读挂载或启动前复验强制已安装 release 不可变。发布包的 SHA256 和完整 Git SHA 探活用于校验发布入口和本次切换的版本，不用于抵御主机管理员事后修改。运维流程仍禁止在服务器直接编辑源码；如果怀疑主机或账号已失信，必须停止发布并重建可信环境，不使用现有 release 回滚。

## 3. 首次初始化 UCloud

以下命令以 Ubuntu 系统为例；其他使用 systemd 的发行版安装等价软件包即可。

### 3.1 安装系统依赖

```bash
sudo apt-get update
sudo apt-get install -y nginx rsync curl tesseract-ocr snapd
sudo snap install --classic certbot
sudo ln -s /snap/bin/certbot /usr/local/bin/certbot
certbot --version
```

`certbot --version` 必须显示 5.4 或更高版本；Ubuntu 22.04 的旧 apt 包不满足 IP 地址 webroot 续期要求，因此使用 Certbot 官方推荐的 snap 包。服务器还需要 Python 3.11 以上和 `/home/ubuntu/.local/bin/uv`。Node.js 与 pnpm 只在 GitHub Actions 构建前端，生产服务器不需要安装。

如果启用 `codex_cli` AI provider，需要另外在生产服务器安装并授权 Codex CLI；如果启用 `stock-sdk` 插件，则需要根据其合规说明另外安装 Node.js 依赖。二者都不是基础生产部署的必需项。

### 3.2 创建目录

```bash
sudo install -d -o root -g root -m 0755 /opt/tickflow/releases
sudo install -d -o ubuntu -g ubuntu -m 0700 /var/lib/tickflow/data
sudo install -d -o root -g root -m 0700 /var/lib/tickflow/backups
sudo install -d -o root -g root -m 0700 /var/lib/tickflow/incoming
sudo install -d -o root -g root -m 0755 /etc/tickflow
```

应用继续使用 UCloud 已有的 `ubuntu` 用户运行，以兼容现有数据权限、`uv` Python 运行时和用户级 CLI 配置。

### 3.3 安装受控部署器和服务配置

在一份可信的仓库检出中执行：

```bash
sudo install -o root -g root -m 0755 ops/deploy.sh /usr/local/sbin/tickflow-deploy
sudo install -o root -g root -m 0755 ops/rollback.sh /usr/local/sbin/tickflow-rollback
sudo install -o root -g root -m 0755 ops/deploy-submit.sh /usr/local/sbin/tickflow-deploy-submit
sudo install -o root -g root -m 0755 ops/deploy-job.sh /usr/local/sbin/tickflow-deploy-job
sudo install -o root -g root -m 0755 ops/deploy-ssh.sh /usr/local/sbin/tickflow-deploy-ssh
sudo install -o root -g root -m 0644 ops/systemd/tickflow.service /etc/systemd/system/tickflow.service
sudo install -o root -g root -m 0644 ops/ssh/tickflow-deploy.conf /etc/ssh/sshd_config.d/60-tickflow-deploy.conf
sudo sshd -t
sudo systemctl reload ssh
sudo systemctl daemon-reload
```

首次发布成功并完成探活后才启用 `tickflow.service` 开机启动，避免迁移失败后它与旧服务在重启时争用端口。

部署器自身发生变更时，必须先审查 diff，再重新执行对应的 `install` 命令。生产 workflow 调用固定安装在 `/usr/local/sbin` 的部署器，不会从发布包中以 root 权限执行脚本。

### 3.3.1 从旧的 root-owned uv cache 一次性迁移

如果服务器曾使用会在安装依赖后递归执行 `chown root:root` 的旧部署器，默认 `uv` cache 可能已经通过共享 inode 混入 root 所有的文件。不要对旧 cache 执行递归 `chown`，否则同一 inode 对应的当前生产 `.venv` 也会被反向改为 `ubuntu` 所有。

先完成 PR 审查并合并，再从该 `main` 提交的可信检出中上传新的 `ops/deploy.sh`。不在服务器编辑脚本，也不执行 `git pull`。先在本机记录该文件的 SHA256：

```bash
# 本机：上传已合并 main 中的受控部署器
sha256sum ops/deploy.sh
scp ops/deploy.sh ubuntu@106.75.247.19:/tmp/tickflow-deploy
```

在 UCloud 上先把上传内容复制到 root 专用目录，再将下面的占位值替换为本机刚输出的该哈希并核验 root 快照。只有核验通过才安装，避免从服务用户可写路径直接安装 root 部署器：

```bash
set -Eeuo pipefail
sudo install -d -o root -g root -m 0700 /var/lib/tickflow/operator-update
sudo install -o root -g root -m 0600 /tmp/tickflow-deploy /var/lib/tickflow/operator-update/tickflow-deploy
printf '%s  %s\n' \
  '<deploy.sh 的 SHA256>' /var/lib/tickflow/operator-update/tickflow-deploy \
  | sudo sha256sum --check --strict

sudo install -o root -g root -m 0755 /var/lib/tickflow/operator-update/tickflow-deploy /usr/local/sbin/tickflow-deploy
sudo mv /home/ubuntu/.cache/uv /home/ubuntu/.cache/uv.before-release-owner-fix
sudo chown root:root /home/ubuntu/.cache/uv.before-release-owner-fix
sudo chmod 0700 /home/ubuntu/.cache/uv.before-release-owner-fix
sudo install -d -o ubuntu -g ubuntu -m 0775 /home/ubuntu/.cache/uv
```

这里仅修改旧 cache 顶层目录的所有权和权限，不递归触碰其中与旧 release 共享的文件 inode。合并触发的第一次自动发布仍可能在旧部署器上失败，这是本次部署器迁移的一次性引导例外。完成上述安装和 cache 隔离后，在 Actions 页面手动重试 `main` 当前 SHA。确认发布、回滚和后续再次发布均正常之前，保留 `uv.before-release-owner-fix`，不要删除。

### 3.4 创建生产配置

```bash
sudo install -o root -g root -m 0600 /dev/null /etc/tickflow/tickflow.env
sudoedit /etc/tickflow/tickflow.env
```

按实际能力填写，例如：

```dotenv
TICKFLOW_API_KEY=
AI_PROVIDER=openai_compat
AI_BASE_URL=
AI_API_KEY=
AI_MODEL=
AUTH_PASSWORD=
LOG_LEVEL=INFO
BACKEND_EXTRAS=backtest
```

`AUTH_PASSWORD` 只在首次初始化密码时使用；已有 `auth.json` 后不会覆盖页面中修改的新密码。不要把任何真实值复制到仓库、Issue、PR 或 Actions 日志。

`DATA_DIR`、`STATIC_DIR` 和 `TIERS_YAML` 已由 systemd 固定为生产绝对路径，不需要写入此文件。systemd 还会显式清除大小写形式的 `HTTP_PROXY`、`HTTPS_PROXY` 和 `ALL_PROXY`，确保应用启动后不经过开发代理。

### 3.5 为公网 IP 申请 TLS 证书

公网登录不能使用明文 HTTP。Let's Encrypt 的 IP 地址证书是约 6 天有效期的短期证书，必须启用自动续期。先确认 UCloud 安全组已开放 80 和 443，并安装 Certbot 5.4 或更高版本，然后首次签发：

```bash
sudo systemctl stop nginx
sudo certbot certonly \
  --preferred-profile shortlived \
  --standalone \
  --ip-address 106.75.247.19 \
  --email <运维邮箱> \
  --agree-tos \
  --no-eff-email
```

证书会写入 `/etc/letsencrypt/live/106.75.247.19/`。首次签发完成后先安装并启动下一节的 Nginx 配置；该配置通过 webroot 响应后续 ACME 校验。Nginx 已能同时提供证书和 challenge 文件后，再把续期方式调整为 webroot，并配置 reload hook。

### 3.6 配置 Nginx

```bash
sudo install -d -o root -g www-data -m 0755 /var/www/letsencrypt/.well-known/acme-challenge
sudo install -o root -g root -m 0644 ops/nginx/tickflow.conf /etc/nginx/conf.d/tickflow.conf
sudo test ! -L /etc/nginx/sites-enabled/default || sudo unlink /etc/nginx/sites-enabled/default
sudo nginx -t
sudo systemctl enable --now nginx

sudo certbot reconfigure \
  --cert-name 106.75.247.19 \
  --preferred-profile shortlived \
  --webroot \
  --webroot-path /var/www/letsencrypt \
  --ip-address 106.75.247.19 \
  --deploy-hook "systemctl reload nginx"
sudo certbot renew --dry-run
sudo systemctl list-timers --all | grep -E 'certbot|snap.certbot'
```

最后一条命令必须能看到 Certbot 安装方式对应的自动续期 timer；若没有，先按该安装方式启用定时续期，不得上线。模板把 80 端口除 ACME 校验外的请求永久重定向到 HTTPS，并在 443 端口反代到仅监听 `127.0.0.1:3018` 的应用，同时关闭 API 缓冲，以支持实时行情 SSE、回测流和 AI 流式输出。生产 systemd 还固定设置 `AUTH_COOKIE_SECURE=true`，浏览器不会在 HTTP 跳转前发送会话 Cookie。初始化命令只删除系统默认站点的启用软链接，原配置仍保留在 `/etc/nginx/sites-available/default`，需要时可以重新创建链接恢复。

UCloud 安全组只开放必要端口：

- 80/443：公网访问。
- 22：标准 GitHub-hosted runner 没有固定出口地址，无法做窄范围 allowlist；当前需要公网可达，并依靠独立密钥、root 持有的认证文件、`ForceCommand` 白名单和最小 sudo 权限收窄风险。若购买带静态 IP 的 larger runner，应立即把安全组改为静态 allowlist。
- 3018：不向公网开放。

用户只通过 `https://106.75.247.19/` 访问，不使用 3018 端口或明文 HTTP。若公网 IP 发生变化，必须重新签发证书，并同步更新 Nginx 的 `server_name`/证书路径、生产 workflow 的 `HostName`、`UCLOUD_SSH_KNOWN_HOSTS` 和 UCloud 安全组。

### 3.7 一次性迁移现有部署

已有 `/opt/trading-copilot` 部署时，首次发布前需要把旧数据一致地迁到新目录，并确保旧、新服务不同时占用 3018 或写数据。先在旧服务仍运行时做预同步：

```bash
set -Eeuo pipefail
sudo rsync -a --delete /opt/trading-copilot/data/ /var/lib/tickflow/data/
sudo chown -R ubuntu:ubuntu /var/lib/tickflow/data
```

首次切换是自动发布流程的引导例外：在配置 `production` Environment secrets 之前先进入维护窗口并完成最终同步；如果 secrets 已经配置，维护窗口内不得合并任何 PR。最终同步完成后再配置 secrets，并手动触发一次 `main` 的 `UCloud 生产发布`。这会增加一次构建与安装时长的停机，但能保证旧、新进程不会争用端口或数据：

```bash
set -Eeuo pipefail
sudo systemctl stop trading-copilot.service
sudo rsync -a --delete /opt/trading-copilot/data/ /var/lib/tickflow/data/
sudo chown -R ubuntu:ubuntu /var/lib/tickflow/data
sudo systemctl disable trading-copilot.service
```

确认旧服务已经停止且最终同步完成后再触发 workflow。首次成功后，后续版本全部走合并自动发布。新版本失败时，Actions 会保持失败；部署器会移除指向失败版本的 `current`，并且不会把未带 `.healthy` 标记的版本作为后续回滚基线。第一次发布还没有 `previous` 可自动回滚，应立即恢复旧服务：

```bash
sudo systemctl disable --now tickflow.service
sudo systemctl enable --now trading-copilot.service
curl --noproxy '*' -fsS http://127.0.0.1:3018/health
```

首次发布成功后执行 `sudo systemctl enable tickflow.service`。完成验证前，不删除 `/opt/trading-copilot` 及其数据。此后发布只使用 `/var/lib/tickflow/data`，旧服务保持禁用。

## 4. GitHub Actions 配置

仓库包含两个相关 workflow：

- `.github/workflows/ci.yml`：PR 和 `main` 的后端 Ruff、Pytest 与前端构建。
- `.github/workflows/server-production.yml`：为 `main` 生成发布包并自动部署；手动触发只重试 `main` 当前 SHA。

### 4.1 production Environment

在 GitHub 仓库设置中创建名为 `production` 的 Environment：

1. Deployment branches 只允许 `main`。
2. 保存 `UCLOUD_SSH_PRIVATE_KEY` 和 `UCLOUD_SSH_KNOWN_HOSTS` 两个 Environment secret。
3. 不在 Environment 中保存应用 API Key；应用密钥只保存在 UCloud 的 `/etc/tickflow/tickflow.env`。
4. 不设置 Environment Required reviewers；人工 Merge 已是发布批准，再增加部署等待会破坏“合并后自动发布”的单一闸门。

### 4.2 PR 与 Review 闸门

`main` 分支保护要求所有改动经过 PR、`后端检查` 与 `前端构建`，合并前分支必须与 `main` 保持最新，所有讨论必须解决；管理员同样受规则约束，并禁止强推或删除 `main`。

开发先创建 GitHub Issue，再创建关联 PR。每轮修改前重新读取 Issue 与 PR 的最新评论和未解决 Review threads。Codex 原生 GitHub Code Review 作为独立 Review Agent：在 Codex Code Review 设置中为仓库开启 Automatic reviews，或在 PR 评论中使用 `@codex review`；维护者人工核对 Review、代码和 CI 后点击 Merge。Codex Review 不替代确定性的 CI 与人工判断。

### 4.3 GitHub-hosted SSH 部署通道

UCloud 到 GitHub 的出站 443 不稳定，因此生产机不运行 self-hosted runner。`deploy-production` 使用 GitHub-hosted `ubuntu-latest`，把当前 SHA 的发布包通过 SSH 上传到低权限账号：

```text
tickflow-deploy@106.75.247.19:/home/tickflow-deploy/incoming/
```

为该账号创建独立 ED25519 密钥，私钥只保存在 `production` Environment secret。公钥放在 root 管理的 `/etc/ssh/authorized_keys/tickflow-deploy`；`Match User` 配置强制所有连接进入 `/usr/local/sbin/tickflow-deploy-ssh`，不允许普通 shell、端口转发或任意命令。账号没有生产数据读取权限，只能通过白名单协议上传、提交、查状态和清理自己的发布包。

```bash
sudo useradd --system --create-home --home-dir /home/tickflow-deploy --shell /bin/bash tickflow-deploy
sudo install -d -o tickflow-deploy -g tickflow-deploy -m 0700 /home/tickflow-deploy/incoming
sudo install -d -o root -g root -m 0755 /etc/ssh/authorized_keys
sudo install -o root -g root -m 0644 \
  /path/to/tickflow-deploy.pub /etc/ssh/authorized_keys/tickflow-deploy
sudo passwd -l tickflow-deploy
```

私钥不得复制到服务器、仓库文件或 Actions 日志。`ops/ssh/tickflow-deploy.conf` 和三个入口脚本必须由 root 持有，修改后先通过 PR 审查再安装。

sudoers 只允许它把固定目录中的发布包交给受控部署器：

```sudoers
tickflow-deploy ALL=(root) NOPASSWD: /usr/local/sbin/tickflow-deploy-submit /home/tickflow-deploy/incoming/tickflow-server-*.tar.gz
```

`UCLOUD_SSH_KNOWN_HOSTS` 不能只靠 `ssh-keyscan` 后直接信任。先通过已有可信管理会话或 UCloud 控制台读取 `/etc/ssh/ssh_host_*_key.pub` 的指纹，再与本机 `ssh-keyscan` 结果逐字核对；只有匹配后才把精确 host key 行写入 Environment secret。

workflow 开启严格主机密钥校验，并在单次 job 内复用 SSH 连接，避免上传、提交和状态轮询反复建立握手。提交器先把上传文件复制到 root 队列，再用独立 systemd transient unit 执行部署；Actions 的 SSH 断线、取消或超时不会终止服务器事务。workflow 只轮询持久状态，状态查询遇到瞬时 SSH 断连时会继续有界轮询，但不会自动重试语义不明确的 `submit`；上传账号无法替换队列归档。不要授权任意 shell、`systemctl *` 或无边界的 root 命令。

## 5. 发布过程

### 5.1 发布包

每次 push 到 `main` 都会生成：

```text
tickflow-server-<short-sha>.tar.gz
tickflow-server-<short-sha>.tar.gz.sha256
```

发布包只包含后端运行代码、锁文件、CI 构建好的 `frontend/dist`、`tiers.yaml`、版本文件和构建元数据。不包含 `.env`、数据、备份、`node_modules` 或任何本机 `.venv`。

Actions 构建产物保留 30 天；UCloud 默认保留最近 5 个已安装 release，因此日常代码回滚不依赖 Actions 产物是否过期。

### 5.2 合并后自动发布生产

1. 在非交易时段确认 PR 的 CI、Codex Review、人工审查和讨论解决均已完成。
2. 维护者人工点击 Merge；GitHub 随即触发 `UCloud 生产发布`。
3. 等待 `package` 和 `deploy-production` 完成。
4. 打开设置页或侧栏，确认显示的短 Git SHA 与合并后的完整提交一致。
5. 检查 `/health`、核心页面、实时行情连接和监控中心，至少观察 10 分钟。

若 Actions 或网络瞬时故障导致部署未提交，可在 Actions 页面手动运行 `UCloud 生产发布`，但必须选择 `main`；这只重试当前 `main` SHA，不是发布另一个分支的入口。部署器已经接收任务后，即使 Actions 取消或超时，服务器事务仍会继续，应先查询任务状态，避免盲目重复提交。

部署器执行顺序：

1. SSH 白名单入口接收发布包并提交，root 复制到部署队列。
2. systemd 托管独立部署事务，Actions 轮询 `deploy-results` 状态。
3. 校验 SHA256、压缩包路径和 Git SHA。
4. 解压到 `/opt/tickflow/releases/<git-sha>`。
5. 使用该版本自己的 `backend/.venv` 安装锁定依赖；默认包含 `backtest` extra。
6. 在服务仍运行时预同步数据快照。
7. 停止 systemd 服务，再做一次增量同步，得到一致的数据快照。
8. 原子切换 `/opt/tickflow/current`。
9. 启动服务并轮询 `/health`，最长等待约 2 分钟。
10. 探活必须返回本次发布的完整 Git SHA；失败或仍命中旧进程时自动切回旧版本并重新启动。
11. 成功后更新 `/opt/tickflow/previous`，清理超出保留数量的旧版本和旧快照。

第一次数据快照可能耗时较长；后续快照通过 `rsync --link-dest` 复用未变化文件。仍应结合 UCloud 云盘快照或异机备份做灾备，不能把同盘快照当作唯一备份。

服务器 CPU 不支持 AVX2/FMA 时，把 `/etc/tickflow/tickflow.env` 中的配置改为：

```dotenv
BACKEND_EXTRAS=legacy-cpu backtest
```

部署器会安全地读取这一项并分别安装两个 extra；没有配置时默认使用 `backtest`。不要为兼容 CPU 临时修改锁文件。

## 6. 回滚

### 6.1 自动回滚

新版本启动失败或 `/health` 超时时，部署器自动把 `current` 切回原版本并重启。Actions job 会保持失败状态，不能把它标记为发布成功。

### 6.2 手工回滚代码

回滚到最近上一版本：

```bash
sudo /usr/local/sbin/tickflow-rollback
```

回滚到指定版本：

```bash
sudo /usr/local/sbin/tickflow-rollback <完整的40位Git SHA>
```

手工回滚同样会探活；目标版本失败时会恢复回滚前版本。代码回滚不会自动回退生产数据，因为恢复旧数据会覆盖用户在发布后产生的交易、设置和监控记录。

如果故障涉及数据破坏，应先停止服务，保留现场，再从 `/var/lib/tickflow/backups` 或 UCloud 云盘快照中选择明确时点恢复。数据恢复是单独的高风险操作，不包含在自动发布流程中。

## 7. 发布策略

### 7.1 普通 Bug 和需求

```text
公网生产环境发现问题
  → 创建 GitHub Issue，记录时间、页面、复现步骤和当前 Git SHA
  → 本地从最新 main 创建 fix/*、feature/* 或 chore/* 分支
  → 本地复现并完成代码、测试和前端构建
  → 创建关联 PR，完成 CI、自审、独立 Codex Review 和人工复审
  → 非交易时段由维护者人工 Merge
  → main 自动构建并部署该完整 Git SHA
  → 核对 Git SHA，验证核心流程并观察至少 10 分钟
  → 关闭 Issue
```

没有 staging 后，PR 合并前必须在本机完成与生产等价的构建和针对性验证。不能以“生产上再试”为验证方式。

### 7.2 紧急故障

- `P0`：数据破坏、安全漏洞或严重错误交易结果。立即停用相关入口或停止服务，保留现场；能通过代码回滚解除时先回滚。
- `P1`：核心流程不可用或广泛回归。优先回滚到已知正常 SHA，再从当前生产基线创建 `hotfix/*`。
- `P2/P3`：走普通 PR，在非交易时段人工 Merge；不绕过 Review 和分支保护。

紧急修复仍必须执行针对性测试、CI 和生产健康检查。不得在服务器直接改代码，也不得为了抢时间让两个实例同时访问同一数据目录。

### 7.3 发布完成标准

以下条件全部满足才算完成：

- Actions 的 `deploy-production` 成功。
- `/health` 返回 `status=ok`，Git SHA 与目标提交一致。
- 登录、数据页、持仓页和监控中心至少完成一次关键路径检查。
- 实时行情按持久化开关状态恢复，SSE 没有持续重连。
- systemd 没有连续重启或新的高频错误日志。
- 发布记录关联 Issue/PR、Git SHA、发布时间、验证结果和回滚方式。

## 8. 常用运维命令

```bash
# 当前版本
readlink -f /opt/tickflow/current
curl --noproxy '*' -fsS http://127.0.0.1:3018/health

# 服务状态与日志
sudo systemctl status tickflow.service
sudo journalctl -u tickflow.service -n 200 --no-pager
sudo journalctl -u tickflow.service -f

# 已安装版本和数据快照
sudo ls -lah /opt/tickflow/releases
sudo ls -lah /var/lib/tickflow/backups

# 手工回滚
sudo /usr/local/sbin/tickflow-rollback
```

不要运行 `git clean -fdx`、`git reset --hard` 或删除 `/var/lib/tickflow` 来处理发布故障。
