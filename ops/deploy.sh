#!/usr/bin/env bash
# UCloud 单生产环境部署器。应安装为 /usr/local/sbin/tickflow-deploy，并由 root 执行。
set -Eeuo pipefail

umask 077

readonly APP_ROOT="/opt/tickflow"
readonly RELEASES_DIR="${APP_ROOT}/releases"
readonly CURRENT_LINK="${APP_ROOT}/current"
readonly PREVIOUS_LINK="${APP_ROOT}/previous"
readonly CURRENT_NEXT="${APP_ROOT}/.current.next"
readonly DATA_DIR="/var/lib/tickflow/data"
readonly BACKUPS_DIR="/var/lib/tickflow/backups"
readonly STAGING_ROOT="/var/lib/tickflow/incoming"
readonly ENV_FILE="/etc/tickflow/tickflow.env"
readonly LOCK_FILE="/run/lock/tickflow-deploy.lock"
readonly SERVICE_NAME="tickflow.service"
readonly HEALTH_URL="http://127.0.0.1:3018/health"
readonly KEEP_RELEASES=5
readonly KEEP_BACKUPS=5
readonly SERVICE_USER="ubuntu"
readonly SERVICE_GROUP="ubuntu"
readonly SERVICE_HOME="/home/ubuntu"
readonly UV_BIN="${SERVICE_HOME}/.local/bin/uv"

incoming_dir=""
backup_tmp=""
previous_target=""
service_touched=0
created_release_dir=""
staging_dir=""
staging_root_used="$STAGING_ROOT"

log() {
  printf '[tickflow-deploy] %s\n' "$*"
}

fail() {
  printf '[tickflow-deploy] 错误: %s\n' "$*" >&2
  return 1
}

validate_runtime_env_file() {
  local env_file="$1"

  [[ -f "$env_file" ]] || fail "生产配置不存在: ${env_file}"
  if grep -Eiq '^[[:space:]]*(DATA_DIR|STATIC_DIR|TIERS_YAML)[[:space:]]*=' "$env_file"; then
    fail "生产配置不得声明 DATA_DIR、STATIC_DIR 或 TIERS_YAML；这些路径由 systemd 固定管理"
  fi
}

validate_archive_members() {
  local archive_path="$1"
  local entry mode

  [[ -f "$archive_path" ]] || fail "发布包不存在: ${archive_path}"
  while IFS= read -r entry; do
    [[ "$entry" != /* ]] || fail "发布包包含绝对路径: ${entry}"
    case "/${entry}/" in
      *"/../"*) fail "发布包包含越界路径: ${entry}" ;;
    esac
  done < <(tar -tzf "$archive_path")

  # Runner 可以控制传入的普通文件，因此 root 解压前必须拒绝链接、设备和 FIFO。
  # tar 详细列表首字符是成员类型：仅 '-' 普通文件与 'd' 目录可进入发布包。
  while IFS= read -r mode _; do
    case "${mode:0:1}" in
      -|d) ;;
      *) fail "发布包包含不允许的特殊成员类型: ${mode:0:1}" ;;
    esac
  done < <(tar -tvzf "$archive_path")
}

validate_archive_checksum() {
  local archive_path="$1"
  local checksum_path="$2"
  local expected_hash expected_name extra actual_hash checksum_line line_count

  line_count="$(awk 'END { print NR }' "$checksum_path")"
  [[ "$line_count" == "1" ]] || fail "校验文件必须只有一行"
  IFS= read -r checksum_line < "$checksum_path"
  read -r expected_hash expected_name extra <<< "$checksum_line"
  [[ "$expected_hash" =~ ^[0-9a-f]{64}$ ]] || fail "SHA256 格式不合法"
  [[ -z "$extra" ]] || fail "校验文件包含多余字段"
  [[ "$expected_name" == "$(basename -- "$archive_path")" ]] \
    || fail "校验文件目标不是本次发布包"
  actual_hash="$(sha256sum "$archive_path" | cut -d' ' -f1)"
  [[ "$actual_hash" == "$expected_hash" ]] || fail "发布包 SHA256 不匹配"
  log "发布包 SHA256 校验通过"
}

stage_archive_snapshot() {
  local source_archive="$archive"
  local source_checksum="$checksum_file"
  local archive_name

  archive_name="$(basename -- "$source_archive")"
  staging_dir="$(mktemp -d "${staging_root_used}/.archive.XXXXXX")"
  # 先固定校验文件，再把归档复制到 Runner 不可写的新 inode。此后全部操作
  # 只读取 root 快照，避免检查与解压之间被原子替换或原地改写。
  if [[ ${EUID} -eq 0 ]]; then
    install -o root -g root -m 0600 "$source_checksum" "${staging_dir}/${archive_name}.sha256"
    install -o root -g root -m 0600 "$source_archive" "${staging_dir}/${archive_name}"
  else
    install -m 0600 "$source_checksum" "${staging_dir}/${archive_name}.sha256"
    install -m 0600 "$source_archive" "${staging_dir}/${archive_name}"
  fi
  archive="${staging_dir}/${archive_name}"
  checksum_file="${archive}.sha256"
}

safe_remove_tree() {
  local root="$1"
  local target="$2"
  local resolved_root resolved_target

  resolved_root="$(realpath -- "$root")"
  resolved_target="$(realpath -- "$target")"
  [[ "$resolved_target" != "$resolved_root" ]] || fail "拒绝删除目录根: ${resolved_target}"
  [[ "$resolved_target" == "${resolved_root}/"* ]] || fail "删除目标越界: ${resolved_target}"
  [[ -e "$resolved_target" ]] || return 0
  find "$resolved_target" -depth -delete
}

switch_current() {
  local target="$1"
  [[ -d "$target" ]] || fail "目标版本不存在: ${target}"
  [[ "$target" == "${RELEASES_DIR}/"* ]] || fail "目标版本不在 releases 目录: ${target}"
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

cleanup() {
  set +e
  rm -f -- "$CURRENT_NEXT"
  if [[ -n "$incoming_dir" && -d "$incoming_dir" ]]; then
    safe_remove_tree "$RELEASES_DIR" "$incoming_dir"
  fi
  if [[ -n "$backup_tmp" && -d "$backup_tmp" ]]; then
    safe_remove_tree "$BACKUPS_DIR" "$backup_tmp"
  fi
  if [[ -n "$created_release_dir" && -d "$created_release_dir" ]]; then
    safe_remove_tree "$RELEASES_DIR" "$created_release_dir"
  fi
  if [[ -n "$staging_dir" && -d "$staging_dir" ]]; then
    safe_remove_tree "$staging_root_used" "$staging_dir"
  fi
}

rollback_on_error() {
  local exit_code=$?
  trap - ERR
  set +e
  if (( service_touched )); then
    log "部署失败，开始恢复上一版本"
    systemctl stop "$SERVICE_NAME"
    if [[ -n "$previous_target" && -d "$previous_target" ]]; then
      switch_current "$previous_target"
      systemctl start "$SERVICE_NAME"
      if wait_for_health "$(basename -- "$previous_target")"; then
        log "已恢复上一版本: ${previous_target}"
      else
        log "上一版本已切回，但健康检查仍失败，请立即检查 systemd 日志"
      fi
    else
      rm -f -- "$CURRENT_LINK"
      log "没有可恢复的上一版本，服务保持停止"
    fi
  fi
  cleanup
  exit "$exit_code"
}

trap cleanup EXIT
trap rollback_on_error ERR

if [[ ${1:-} == "--validate-only" ]]; then
  [[ $# -eq 2 ]] || fail "用法: tickflow-deploy --validate-only <tar.gz>"
  archive="$(realpath -- "$2")"
  checksum_file="${archive}.sha256"
  [[ -f "$checksum_file" ]] || fail "缺少校验文件: ${checksum_file}"
  staging_root_used="${TMPDIR:-/tmp}"
  stage_archive_snapshot
  validate_archive_checksum "$archive" "$checksum_file"
  validate_archive_members "$archive"
  exit 0
fi

if [[ ${1:-} == "--validate-env-only" ]]; then
  [[ $# -eq 2 ]] || fail "用法: tickflow-deploy --validate-env-only <tickflow.env>"
  validate_runtime_env_file "$2"
  exit 0
fi

[[ ${EUID} -eq 0 ]] || fail "必须以 root 运行"
[[ $# -eq 1 ]] || fail "用法: tickflow-deploy <tickflow-server-*.tar.gz>"

archive="$(realpath -- "$1")"
checksum_file="${archive}.sha256"

[[ -f "$archive" ]] || fail "发布包不存在: ${archive}"
[[ -f "$checksum_file" ]] || fail "缺少校验文件: ${checksum_file}"
[[ -f "$ENV_FILE" ]] || fail "缺少生产配置: ${ENV_FILE}"

for command_name in awk curl cut find flock grep install realpath rsync runuser sha256sum tar; do
  command -v "$command_name" >/dev/null || fail "缺少命令: ${command_name}"
done
validate_runtime_env_file "$ENV_FILE"
id "$SERVICE_USER" >/dev/null 2>&1 || fail "缺少系统用户: ${SERVICE_USER}"
runuser -u "$SERVICE_USER" -- test -x "$UV_BIN" \
  || fail "${SERVICE_USER} 用户无法执行 uv: ${UV_BIN}"

install -d -o root -g root -m 0755 "$RELEASES_DIR"
install -d -o "$SERVICE_USER" -g "$SERVICE_GROUP" -m 0700 "$DATA_DIR"
install -d -o root -g root -m 0700 "$BACKUPS_DIR"
install -d -o root -g root -m 0700 "$STAGING_ROOT"

exec 9>"$LOCK_FILE"
flock -n 9 || fail "已有部署正在进行"

stage_archive_snapshot
log "校验发布包"
validate_archive_checksum "$archive" "$checksum_file"

validate_archive_members "$archive"

incoming_dir="$(mktemp -d "${RELEASES_DIR}/.incoming.XXXXXX")"
tar -xzf "$archive" -C "$incoming_dir" --no-same-owner --no-same-permissions

mapfile -t package_roots < <(find "$incoming_dir" -mindepth 1 -maxdepth 1 -type d -print)
[[ ${#package_roots[@]} -eq 1 ]] || fail "发布包必须只有一个顶层目录"
package_root="${package_roots[0]}"
release_env="${package_root}/.release.env"
[[ -f "$release_env" ]] || fail "发布包缺少 .release.env"

git_sha="$(sed -n 's/^GIT_SHA=//p' "$release_env" | head -n 1)"
[[ "$git_sha" =~ ^[0-9a-f]{40}$ ]] || fail "GIT_SHA 不合法"
release_dir="${RELEASES_DIR}/${git_sha}"

[[ -f "${package_root}/backend/uv.lock" ]] || fail "发布包缺少 backend/uv.lock"
[[ -f "${package_root}/frontend/dist/index.html" ]] || fail "发布包缺少前端产物"
[[ -f "${package_root}/tiers.yaml" ]] || fail "发布包缺少 tiers.yaml"

if [[ -d "$release_dir" ]]; then
  [[ -f "${release_dir}/.ready" ]] || fail "同 SHA 的版本目录不完整: ${release_dir}"
  log "复用已经安装的版本: ${git_sha}"
else
  mv -- "$package_root" "$release_dir"
  created_release_dir="$release_dir"
  chown -R "${SERVICE_USER}:${SERVICE_GROUP}" "$release_dir"
  log "使用 uv 默认全局缓存安装锁定依赖"
  sync_args=(sync --frozen --no-dev --no-install-project)
  extras="${TICKFLOW_BACKEND_EXTRAS:-}"
  if [[ -z "$extras" ]]; then
    extras="$(sed -n 's/^[[:space:]]*BACKEND_EXTRAS[[:space:]]*=[[:space:]]*//p' "$ENV_FILE" | tail -n 1)"
    extras="${extras#\"}"
    extras="${extras%\"}"
    extras="${extras#\'}"
    extras="${extras%\'}"
  fi
  extras="${extras:-backtest}"
  read -r -a extra_names <<< "$extras"
  for extra in "${extra_names[@]}"; do
    [[ "$extra" =~ ^[a-z0-9_-]+$ ]] || fail "依赖 extra 名称不合法: ${extra}"
    sync_args+=(--extra "$extra")
  done
  (
    cd "${release_dir}/backend"
    runuser -u "$SERVICE_USER" -- env \
      -u HTTP_PROXY -u HTTPS_PROXY -u ALL_PROXY \
      -u http_proxy -u https_proxy -u all_proxy \
      -u UV_CACHE_DIR -u XDG_CACHE_HOME \
      HOME="$SERVICE_HOME" \
      "$UV_BIN" "${sync_args[@]}"
  )
  touch "${release_dir}/.ready"
  created_release_dir=""
fi

chmod -R u=rwX,go=rX "$release_dir"

if [[ -L "$CURRENT_LINK" ]]; then
  previous_target="$(readlink -f -- "$CURRENT_LINK")"
  [[ "$previous_target" == "${RELEASES_DIR}/"* ]] || fail "current 软链接指向异常位置"
  [[ -f "${previous_target}/.healthy" ]] || fail "current 指向尚未通过探活的版本: ${previous_target}"
fi

backup_id="$(date -u +'%Y%m%dT%H%M%SZ')-${git_sha:0:12}"
backup_tmp="${BACKUPS_DIR}/.incoming-${backup_id}"
backup_dir="${BACKUPS_DIR}/${backup_id}"
mkdir -p "$backup_tmp"

mapfile -t existing_backups < <(find "$BACKUPS_DIR" -mindepth 1 -maxdepth 1 -type d ! -name '.incoming-*' -printf '%T@ %p\n' | sort -nr | cut -d' ' -f2-)
rsync_args=(-a --delete)
if [[ ${#existing_backups[@]} -gt 0 ]]; then
  rsync_args+=(--link-dest="${existing_backups[0]}")
fi

log "预同步生产数据快照"
rsync "${rsync_args[@]}" "${DATA_DIR}/" "${backup_tmp}/"

service_touched=1
systemctl stop "$SERVICE_NAME"

# 停服后补一次增量，确保快照与某个完整时点一致。
rsync "${rsync_args[@]}" "${DATA_DIR}/" "${backup_tmp}/"
mv -- "$backup_tmp" "$backup_dir"
backup_tmp=""

switch_current "$release_dir"
systemctl start "$SERVICE_NAME"

if ! wait_for_health "$git_sha"; then
  fail "新版本健康检查超时"
fi
touch "${release_dir}/.healthy"

if [[ -n "$previous_target" && "$previous_target" != "$release_dir" ]]; then
  rm -f -- "${APP_ROOT}/.previous.next"
  ln -s -- "$previous_target" "${APP_ROOT}/.previous.next"
  mv -Tf -- "${APP_ROOT}/.previous.next" "$PREVIOUS_LINK"
fi

service_touched=0
log "发布成功: ${git_sha}"
trap - ERR

# 清理旧快照；快照间通过 --link-dest 共享未变化文件，删除旧目录不会破坏新快照。
mapfile -t backups_to_remove < <(find "$BACKUPS_DIR" -mindepth 1 -maxdepth 1 -type d ! -name '.incoming-*' -printf '%T@ %p\n' | sort -nr | tail -n "+$((KEEP_BACKUPS + 1))" | cut -d' ' -f2-)
for old_backup in "${backups_to_remove[@]}"; do
  if ! safe_remove_tree "$BACKUPS_DIR" "$old_backup"; then
    log "警告: 清理旧数据快照失败: ${old_backup}"
  fi
done

mapfile -t release_candidates < <(find "$RELEASES_DIR" -mindepth 1 -maxdepth 1 -type d ! -name '.incoming.*' -printf '%T@ %p\n' | sort -nr | cut -d' ' -f2-)
kept=0
for old_release in "${release_candidates[@]}"; do
  if [[ "$old_release" == "$release_dir" || "$old_release" == "$previous_target" ]]; then
    continue
  fi
  kept=$((kept + 1))
  if (( kept > KEEP_RELEASES - 2 )); then
    if ! safe_remove_tree "$RELEASES_DIR" "$old_release"; then
      log "警告: 清理旧版本失败: ${old_release}"
    fi
  fi
done
