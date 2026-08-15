#!/usr/bin/env bash
# systemd 托管的生产部署事务；调用端断线不会向本进程传播 HUP/TERM。
set -Eeuo pipefail

umask 027

readonly QUEUE_ROOT="/var/lib/tickflow/deploy-queue"
readonly RESULTS_ROOT="/var/lib/tickflow/deploy-results"
readonly DEPLOY_BIN="/usr/local/sbin/tickflow-deploy"

[[ ${EUID} -eq 0 ]]
[[ $# -eq 3 ]]

queue_dir="$(realpath -- "$1")"
archive="$(realpath -- "$2")"
result_file="$(realpath -- "$3")"
[[ "$queue_dir" == "${QUEUE_ROOT}/deploy-"* ]]
[[ "$archive" == "${queue_dir}/tickflow-server-"*.tar.gz ]]
[[ "$result_file" == "${RESULTS_ROOT}/deploy-"*.status ]]

write_status() {
  local status="$1"
  local status_tmp
  status_tmp="$(mktemp "${RESULTS_ROOT}/.status.XXXXXX")"
  printf '%s\n' "$status" > "$status_tmp"
  chown root:tickflow-deploy "$status_tmp"
  chmod 0640 "$status_tmp"
  mv -f -- "$status_tmp" "$result_file"
}

finish() {
  local exit_code=$?
  trap - EXIT
  if (( exit_code == 0 )); then
    write_status success
  else
    write_status "failed:${exit_code}"
  fi
  find "$queue_dir" -depth -delete
  exit "$exit_code"
}

trap finish EXIT
write_status running
"$DEPLOY_BIN" "$archive"
