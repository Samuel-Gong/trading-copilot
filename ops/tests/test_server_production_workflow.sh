#!/usr/bin/env bash
set -Eeuo pipefail

readonly REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
readonly WORKFLOW="${REPO_ROOT}/.github/workflows/server-production.yml"

require_workflow_text() {
  local expected="$1"
  local message="$2"

  if ! grep -Fq -- "$expected" "$WORKFLOW"; then
    printf '%s\n' "$message" >&2
    exit 1
  fi
}

require_workflow_text 'ControlMaster auto' '生产发布必须复用 SSH 连接'
require_workflow_text 'ControlPath ${RUNNER_TEMP}/tickflow-ssh-%C' 'SSH 复用 socket 必须使用 Runner 临时短路径'
require_workflow_text 'ControlPersist 65m' 'SSH 复用连接必须覆盖整个部署 job'
require_workflow_text 'ConnectTimeout 15' 'SSH 建连必须有明确超时上限'
require_workflow_text 'ServerAliveInterval 30' 'SSH 复用连接必须启用存活探测'
require_workflow_text 'ServerAliveCountMax 3' 'SSH 存活探测必须有明确失败上限'
require_workflow_text 'ssh -O exit tickflow-production || true' '清理阶段必须关闭 SSH 复用连接'

poll_block="$(sed -n '/for attempt in \$(seq 1 720); do/,/^[[:space:]]*done$/p' "$WORKFLOW")"
if [[ -z "$poll_block" ]]; then
  printf '未找到生产部署状态轮询循环\n' >&2
  exit 1
fi

for expected in \
  'ssh_exit=0' \
  'status="$(ssh tickflow-production "status ${job_id}")" || ssh_exit=$?'; do
  if ! grep -Fq -- "$expected" <<< "$poll_block"; then
    printf '状态轮询缺少 SSH 退出码捕获: %s\n' "$expected" >&2
    exit 1
  fi
done

transport_failure_block="$(sed -n '/ssh_exit.*255/,/^[[:space:]]*fi$/p' <<< "$poll_block")"
for expected in '远端状态查询连接失败' 'sleep 5' 'continue'; do
  if ! grep -Fq -- "$expected" <<< "$transport_failure_block"; then
    printf 'SSH 传输失败分支缺少有界重试: %s\n' "$expected" >&2
    exit 1
  fi
done

remote_failure_block="$(sed -n '/ssh_exit != 0/,/^[[:space:]]*fi$/p' <<< "$poll_block")"
for expected in '远端状态查询命令失败' 'exit "${ssh_exit}"'; do
  if ! grep -Fq -- "$expected" <<< "$remote_failure_block"; then
    printf '远端命令失败分支必须立即退出: %s\n' "$expected" >&2
    exit 1
  fi
done

submit_count="$(grep -Fc 'ssh tickflow-production "submit ${archive_name}"' "$WORKFLOW" || true)"
if [[ "$submit_count" != "1" ]]; then
  printf '生产部署 submit 必须保持单次调用，实际为 %s 次\n' "$submit_count" >&2
  exit 1
fi

printf 'server production workflow tests passed\n'
