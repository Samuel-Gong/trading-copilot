#!/usr/bin/env bash
# tickflow-deploy SSH 账号的强制命令入口。拒绝普通 shell，仅开放固定上传与部署协议。
set -Eeuo pipefail

umask 077

readonly INCOMING_ROOT="${TICKFLOW_SSH_INCOMING_ROOT:-/home/tickflow-deploy/incoming}"
readonly RESULTS_ROOT="${TICKFLOW_SSH_RESULTS_ROOT:-/var/lib/tickflow/deploy-results}"
readonly SUBMIT_BIN="${TICKFLOW_SSH_SUBMIT_BIN:-/usr/local/sbin/tickflow-deploy-submit}"
readonly SUDO_BIN="${TICKFLOW_SSH_SUDO_BIN:-/usr/bin/sudo}"

fail() {
  printf '[tickflow-deploy-ssh] 拒绝: %s\n' "$*" >&2
  return 1
}

original_command="${SSH_ORIGINAL_COMMAND:-}"
[[ "$original_command" != *$'\n'* && "$original_command" != *$'\r'* ]] || fail "命令包含换行"
read -r action argument extra <<< "$original_command"
[[ -n "${action:-}" && -n "${argument:-}" && -z "${extra:-}" ]] || fail "命令格式不合法"

case "$action" in
  upload)
    [[ "$argument" =~ ^tickflow-server-[0-9a-f]{12}\.tar\.gz(\.sha256)?$ ]] || fail "上传文件名不合法"
    install -d -m 0700 "$INCOMING_ROOT"
    upload_tmp="$(mktemp "${INCOMING_ROOT}/.upload.XXXXXX")"
    trap 'rm -f -- "$upload_tmp"' EXIT
    ulimit -f 1048576
    cat > "$upload_tmp"
    [[ -s "$upload_tmp" ]] || fail "上传内容为空"
    chmod 0600 "$upload_tmp"
    mv -f -- "$upload_tmp" "${INCOMING_ROOT}/${argument}"
    trap - EXIT
    printf 'uploaded\n'
    ;;
  submit)
    [[ "$argument" =~ ^tickflow-server-[0-9a-f]{12}\.tar\.gz$ ]] || fail "发布包名称不合法"
    "$SUDO_BIN" -n "$SUBMIT_BIN" "${INCOMING_ROOT}/${argument}"
    ;;
  status)
    [[ "$argument" =~ ^deploy-[0-9a-f]{12}-[0-9]{8}T[0-9]{6}Z-[0-9]+$ ]] || fail "任务编号不合法"
    status_file="${RESULTS_ROOT}/${argument}.status"
    [[ -f "$status_file" ]] || fail "任务状态不存在"
    cat "$status_file"
    ;;
  cleanup)
    [[ "$argument" =~ ^tickflow-server-[0-9a-f]{12}\.tar\.gz$ ]] || fail "清理文件名不合法"
    rm -f -- "${INCOMING_ROOT}/${argument}" "${INCOMING_ROOT}/${argument}.sha256"
    ;;
  *)
    fail "不允许的操作"
    ;;
esac
