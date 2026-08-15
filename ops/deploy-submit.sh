#!/usr/bin/env bash
# 把低权限上传目录中的发布包固定到 root 队列，并提交为独立 systemd 事务。
set -Eeuo pipefail

umask 077

readonly UPLOAD_ROOT="/home/tickflow-deploy/incoming"
readonly QUEUE_ROOT="/var/lib/tickflow/deploy-queue"
readonly RESULTS_ROOT="/var/lib/tickflow/deploy-results"
readonly JOB_BIN="/usr/local/sbin/tickflow-deploy-job"

queue_dir=""

fail() {
  printf '[tickflow-deploy-submit] 错误: %s\n' "$*" >&2
  return 1
}

cleanup_on_error() {
  local exit_code=$?
  trap - ERR
  if [[ -n "$queue_dir" && -d "$queue_dir" ]]; then
    find "$queue_dir" -depth -delete
  fi
  exit "$exit_code"
}

trap cleanup_on_error ERR

[[ ${EUID} -eq 0 ]] || fail "必须以 root 运行"
[[ $# -eq 1 ]] || fail "用法: tickflow-deploy-submit <上传归档>"
[[ -x "$JOB_BIN" ]] || fail "缺少部署任务执行器: ${JOB_BIN}"

source_archive="$(realpath -- "$1")"
archive_name="$(basename -- "$source_archive")"
[[ "$archive_name" =~ ^tickflow-server-([0-9a-f]{12})\.tar\.gz$ ]] || fail "发布包名称不合法"
short_sha="${BASH_REMATCH[1]}"
[[ "$source_archive" == "${UPLOAD_ROOT}/${archive_name}" ]] || fail "发布包不在固定上传目录"
[[ -f "$source_archive" ]] || fail "发布包不存在"
[[ -f "${source_archive}.sha256" ]] || fail "发布包缺少校验文件"

install -d -o root -g root -m 0700 "$QUEUE_ROOT"
install -d -o root -g tickflow-deploy -m 0750 "$RESULTS_ROOT"
job_id="deploy-${short_sha}-$(date -u +'%Y%m%dT%H%M%SZ')-$$"
queue_dir="${QUEUE_ROOT}/${job_id}"
result_file="${RESULTS_ROOT}/${job_id}.status"
install -d -o root -g root -m 0700 "$queue_dir"
install -o root -g root -m 0600 "$source_archive" "${queue_dir}/${archive_name}"
install -o root -g root -m 0600 "${source_archive}.sha256" "${queue_dir}/${archive_name}.sha256"
install -o root -g tickflow-deploy -m 0640 /dev/null "$result_file"
printf 'queued\n' > "$result_file"

systemd-run \
  --unit="tickflow-${job_id}" \
  --property=Type=oneshot \
  --property=TimeoutStartSec=infinity \
  --no-block \
  --quiet \
  "$JOB_BIN" "$queue_dir" "${queue_dir}/${archive_name}" "$result_file"

queue_dir=""
find "$RESULTS_ROOT" -maxdepth 1 -type f -name 'deploy-*.status' -mtime +30 -delete
printf '%s\n' "$job_id"
