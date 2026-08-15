#!/usr/bin/env bash
set -Eeuo pipefail

readonly REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
readonly GATEWAY="${REPO_ROOT}/ops/deploy-ssh.sh"

test_root="$(mktemp -d)"
trap 'find "$test_root" -depth -delete' EXIT
incoming="${test_root}/incoming"
results="${test_root}/results"
mkdir -p "$incoming" "$results"

archive_name="tickflow-server-0123456789ab.tar.gz"
common_env=(
  TICKFLOW_SSH_INCOMING_ROOT="$incoming"
  TICKFLOW_SSH_RESULTS_ROOT="$results"
)

printf 'archive-payload' | env "${common_env[@]}" \
  SSH_ORIGINAL_COMMAND="upload ${archive_name}" bash "$GATEWAY" >/dev/null
[[ "$(< "${incoming}/${archive_name}")" == "archive-payload" ]]

printf 'checksum-payload' | env "${common_env[@]}" \
  SSH_ORIGINAL_COMMAND="upload ${archive_name}.sha256" bash "$GATEWAY" >/dev/null
[[ "$(< "${incoming}/${archive_name}.sha256")" == "checksum-payload" ]]

job_id="deploy-0123456789ab-20260815T120000Z-123"
printf 'running\n' > "${results}/${job_id}.status"
status="$(env "${common_env[@]}" SSH_ORIGINAL_COMMAND="status ${job_id}" bash "$GATEWAY")"
[[ "$status" == "running" ]]

env "${common_env[@]}" SSH_ORIGINAL_COMMAND="cleanup ${archive_name}" bash "$GATEWAY"
[[ ! -e "${incoming}/${archive_name}" ]]
[[ ! -e "${incoming}/${archive_name}.sha256" ]]

if env "${common_env[@]}" SSH_ORIGINAL_COMMAND="upload ../../etc/passwd" bash "$GATEWAY" </dev/null; then
  printf '越界上传未被拒绝\n' >&2
  exit 1
fi

if env "${common_env[@]}" SSH_ORIGINAL_COMMAND="bash -i" bash "$GATEWAY" </dev/null; then
  printf '普通 shell 未被拒绝\n' >&2
  exit 1
fi

printf 'deploy SSH gateway tests passed\n'
