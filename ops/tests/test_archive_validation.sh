#!/usr/bin/env bash
set -Eeuo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
tmp_dir="$(mktemp -d)"

cleanup() {
  find "$tmp_dir" -depth -delete
}
trap cleanup EXIT

mkdir -p "${tmp_dir}/valid/release"
touch "${tmp_dir}/valid/release/VERSION"
tar -C "${tmp_dir}/valid" -czf "${tmp_dir}/valid.tar.gz" release
(cd "$tmp_dir" && sha256sum valid.tar.gz > valid.tar.gz.sha256)
"${repo_root}/ops/deploy.sh" --validate-only "${tmp_dir}/valid.tar.gz"

mkdir -p "${tmp_dir}/invalid/release"
mkfifo "${tmp_dir}/invalid/release/device-pipe"
tar -C "${tmp_dir}/invalid" -czf "${tmp_dir}/invalid.tar.gz" release
(cd "$tmp_dir" && sha256sum invalid.tar.gz > invalid.tar.gz.sha256)
if "${repo_root}/ops/deploy.sh" --validate-only "${tmp_dir}/invalid.tar.gz"; then
  printf '特殊成员发布包不应通过校验\n' >&2
  exit 1
fi

cp "${tmp_dir}/valid.tar.gz" "${tmp_dir}/wrong-target.tar.gz"
valid_hash="$(sha256sum "${tmp_dir}/wrong-target.tar.gz" | cut -d' ' -f1)"
printf '%s  /etc/passwd\n' "$valid_hash" > "${tmp_dir}/wrong-target.tar.gz.sha256"
if "${repo_root}/ops/deploy.sh" --validate-only "${tmp_dir}/wrong-target.tar.gz"; then
  printf '指向其他路径的校验文件不应通过校验\n' >&2
  exit 1
fi

printf '%s\n' \
  'LOG_LEVEL=INFO' \
  'BACKEND_EXTRAS=backtest' \
  '# DATA_DIR=/legacy/data' \
  > "${tmp_dir}/valid.env"
"${repo_root}/ops/deploy.sh" --validate-env-only "${tmp_dir}/valid.env"

for reserved_key in DATA_DIR data_dir Data_Dir STATIC_DIR static_dir TIERS_YAML tiers_yaml; do
  printf '%s = /unexpected/path\n' "$reserved_key" > "${tmp_dir}/invalid.env"
  if "${repo_root}/ops/deploy.sh" --validate-env-only "${tmp_dir}/invalid.env"; then
    printf '生产私有环境文件不应允许保留变量: %s\n' "$reserved_key" >&2
    exit 1
  fi
done
