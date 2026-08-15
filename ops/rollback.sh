#!/usr/bin/env bash
# 手工回滚代码版本，不自动回退生产数据。
set -Eeuo pipefail

readonly APP_ROOT="/opt/tickflow"
readonly RELEASES_DIR="${APP_ROOT}/releases"
readonly CURRENT_LINK="${APP_ROOT}/current"
readonly PREVIOUS_LINK="${APP_ROOT}/previous"
readonly CURRENT_NEXT="${APP_ROOT}/.current.next"
readonly SERVICE_NAME="tickflow.service"
readonly HEALTH_URL="http://127.0.0.1:3018/health"
readonly LOCK_FILE="/run/lock/tickflow-deploy.lock"
restore_needed=0
current_target=""

fail() {
  printf '[tickflow-rollback] 错误: %s\n' "$*" >&2
  return 1
}

switch_current() {
  local target="$1"
  rm -f -- "$CURRENT_NEXT"
  ln -s -- "$target" "$CURRENT_NEXT"
  mv -Tf -- "$CURRENT_NEXT" "$CURRENT_LINK"
}

wait_for_health() {
  local expected_sha="$1"
  local attempt response
  for attempt in $(seq 1 60); do
    if response="$(curl --noproxy '*' -fsS --max-time 3 "$HEALTH_URL")"; then
      if grep -Eq "\"git_sha\"[[:space:]]*:[[:space:]]*\"${expected_sha}\"" <<< "$response"; then
        return 0
      fi
    fi
    sleep 2
  done
  return 1
}

restore_on_error() {
  local exit_code=$?
  trap - ERR
  set +e
  if (( restore_needed )) && [[ -n "$current_target" && -d "$current_target" ]]; then
    printf '[tickflow-rollback] 回滚过程失败，恢复操作前版本\n' >&2
    systemctl stop "$SERVICE_NAME"
    switch_current "$current_target"
    systemctl start "$SERVICE_NAME"
    wait_for_health "$(basename -- "$current_target")"
  fi
  exit "$exit_code"
}

trap restore_on_error ERR

[[ ${EUID} -eq 0 ]] || fail "必须以 root 运行"
exec 9>"$LOCK_FILE"
flock -n 9 || fail "已有部署或回滚正在进行"

current_target="$(readlink -f -- "$CURRENT_LINK")"
if [[ $# -eq 1 ]]; then
  [[ "$1" =~ ^[0-9a-f]{40}$ ]] || fail "参数必须是完整的 40 位 Git SHA"
  target="${RELEASES_DIR}/$1"
elif [[ $# -eq 0 && -L "$PREVIOUS_LINK" ]]; then
  target="$(readlink -f -- "$PREVIOUS_LINK")"
else
  fail "用法: tickflow-rollback [完整 Git SHA]；未传参数时回滚到 previous"
fi

[[ "$current_target" == "${RELEASES_DIR}/"* ]] || fail "current 软链接异常"
[[ "$target" == "${RELEASES_DIR}/"* ]] || fail "目标版本越界"
[[ -f "${target}/.healthy" ]] || fail "目标版本不存在或从未通过健康检查: ${target}"
[[ "$target" != "$current_target" ]] || fail "目标版本已经在运行"

restore_needed=1
systemctl stop "$SERVICE_NAME"
switch_current "$target"

if systemctl start "$SERVICE_NAME" && wait_for_health "$(basename -- "$target")"; then
  rm -f -- "${APP_ROOT}/.previous.next"
  ln -s -- "$current_target" "${APP_ROOT}/.previous.next"
  mv -Tf -- "${APP_ROOT}/.previous.next" "$PREVIOUS_LINK"
  restore_needed=0
  printf '[tickflow-rollback] 已回滚到 %s\n' "$(basename -- "$target")"
  exit 0
fi

printf '[tickflow-rollback] 目标版本探活失败，恢复原版本\n' >&2
systemctl stop "$SERVICE_NAME"
switch_current "$current_target"
systemctl start "$SERVICE_NAME"
wait_for_health "$(basename -- "$current_target")" || fail "原版本也未能通过健康检查，请立即检查 systemd 日志"
restore_needed=0
fail "回滚目标未通过健康检查，已恢复原版本"
