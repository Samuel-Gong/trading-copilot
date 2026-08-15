#!/usr/bin/env bash
set -Eeuo pipefail

readonly REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
readonly DEPLOYER="${REPO_ROOT}/ops/deploy.sh"

test_root="$(mktemp -d)"
trap 'find "$test_root" -depth -delete' EXIT

file_owner() {
  local target="$1"
  if stat -c '%u:%g' "$target" >/dev/null 2>&1; then
    stat -c '%u:%g' "$target"
  else
    stat -f '%u:%g' "$target"
  fi
}

# release/.venv 可能与 ubuntu 的默认 uv cache 共享 inode。部署完成后如果再把
# 整个 release 递归改为 root，会同时污染 cache，导致下一次 uv sync 无法写入。
if grep -Fq 'chown -R root:root "$release_dir"' "$DEPLOYER"; then
  printf '部署器不应把整个 release 递归改为 root 所有\n' >&2
  exit 1
fi

# 生产机和 ubuntu 管理账号属于可信运维边界，不用复制模式换取只读 release。
if grep -Eq -- '--link-mode([=[:space:]]|$)|UV_LINK_MODE' "$DEPLOYER"; then
  printf '部署器应继续使用 uv 默认 link mode\n' >&2
  exit 1
fi

if ! grep -Fq 'chown -R "${SERVICE_USER}:${SERVICE_GROUP}" "$release_dir"' "$DEPLOYER"; then
  printf '部署器必须在安装依赖前把 release 交给服务用户\n' >&2
  exit 1
fi

# 用硬链接模拟 uv cache 与 release/.venv 共享 inode。保留下来的 chmod 可以调整
# release 的读写位，但不得改变 cache inode 的所有者。
mkdir -p "${test_root}/cache" "${test_root}/release/.venv"
touch "${test_root}/cache/package.py"
ln "${test_root}/cache/package.py" "${test_root}/release/.venv/package.py"
owner_before="$(file_owner "${test_root}/cache/package.py")"
chmod -R u=rwX,go=rX "${test_root}/release"
owner_after="$(file_owner "${test_root}/cache/package.py")"
[[ "$owner_after" == "$owner_before" ]] || {
  printf 'release 权限收口不应改变共享 cache inode 的所有者\n' >&2
  exit 1
}

printf 'deploy release ownership tests passed\n'
