#!/usr/bin/env bash
set -euo pipefail

PRODUCT_NAME="CTERA Monitoring Dashboard"
PRODUCT_SLUG="ctera-monitoring-dashboard"
DEFAULT_INSTALL_DIR="/opt/monitoring/ctera-monitoring-dashboard"
DEFAULT_CONFIG_FILE="/etc/ctera-monitoring-dashboard.env"
DEFAULT_DATA_DIR="/var/lib/ctera-monitoring-dashboard/data"
DEFAULT_LOG_DIR="/var/log/ctera-monitoring-dashboard"
DEFAULT_SERVICE_USER="ctera-monitoring"
DEFAULT_BACKUP_ROOT="/opt/monitoring-backup"
DEFAULT_SERVICE_FILE="/etc/systemd/system/ctera-monitoring-dashboard.service"
DEFAULT_CRON_FILE="/etc/cron.d/ctera-monitoring-dashboard"
DEFAULT_UPGRADE_HELPER="/usr/local/sbin/ctera-monitoring-dashboard-upgrade"
DEFAULT_UPGRADE_SUDOERS="/etc/sudoers.d/ctera-monitoring-dashboard-upgrade"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSTALL_DIR="${DEFAULT_INSTALL_DIR}"
CONFIG_FILE="${DEFAULT_CONFIG_FILE}"
DATA_DIR="${DEFAULT_DATA_DIR}"
LOG_DIR="${DEFAULT_LOG_DIR}"
SERVICE_USER="${DEFAULT_SERVICE_USER}"
BACKUP_ROOT="${DEFAULT_BACKUP_ROOT}"
SERVICE_FILE="${DEFAULT_SERVICE_FILE}"
CRON_FILE="${DEFAULT_CRON_FILE}"
UPGRADE_HELPER="${DEFAULT_UPGRADE_HELPER}"
UPGRADE_SUDOERS="${DEFAULT_UPGRADE_SUDOERS}"
NONINTERACTIVE=0
THRESHOLD_STRATEGY="merge"
PKG_MGR=""
HELPER_NAME="ctera-secret-helper"
HELPER_INSTALL_PATH="/usr/local/bin/${HELPER_NAME}"
HELPER_VERSION="0.1.0"
HELPER_REPO="mj-ctera/binary-token"
HELPER_ASSET_NAME_LINUX_AMD64="${HELPER_NAME}-linux-amd64"
HELPER_CHECKSUM_SUFFIX=".sha256"
HELPER_REF="main"
HELPER_TOKEN_FILE="/etc/ctera-monitoring-dashboard-helper.token"
HELPER_LOCAL_SOURCE="${CTERA_HELPER_LOCAL_PATH:-}"

usage() {
  cat <<'EOF'
Usage:
  sudo bash ./upgrade.sh [options]

Options:
  --install-dir /opt/monitoring/ctera-monitoring-dashboard
  --config-file /etc/ctera-monitoring-dashboard.env
  --data-dir /var/lib/ctera-monitoring-dashboard/data
  --log-dir /var/log/ctera-monitoring-dashboard
  --user ctera-monitoring
  --backup-root /opt/monitoring-backup
  --threshold-strategy merge|replace
  --non-interactive
  -h, --help
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --install-dir)
      INSTALL_DIR="${2:-}"
      shift 2
      ;;
    --config-file)
      CONFIG_FILE="${2:-}"
      shift 2
      ;;
    --data-dir)
      DATA_DIR="${2:-}"
      shift 2
      ;;
    --log-dir)
      LOG_DIR="${2:-}"
      shift 2
      ;;
    --user)
      SERVICE_USER="${2:-}"
      shift 2
      ;;
    --backup-root)
      BACKUP_ROOT="${2:-}"
      shift 2
      ;;
    --threshold-strategy)
      THRESHOLD_STRATEGY="${2:-}"
      shift 2
      ;;
    --non-interactive)
      NONINTERACTIVE=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown argument: $1" >&2
      usage
      exit 1
      ;;
  esac
done

if [[ "${EUID}" -ne 0 ]]; then
  echo "Run this upgrade as root, for example: sudo bash ./upgrade.sh" >&2
  exit 1
fi

prompt_value() {
  local label="$1"
  local default="$2"
  local value

  if [[ "${NONINTERACTIVE}" -eq 1 ]]; then
    printf '%s' "${default}"
    return
  fi

  printf '%s [%s]: ' "${label}" "${default}" >&2
  read -r value
  printf '%s' "${value:-${default}}"
}

prompt_threshold_strategy() {
  local value

  if [[ "${NONINTERACTIVE}" -eq 1 ]]; then
    printf '%s' "${THRESHOLD_STRATEGY}"
    return
  fi

  echo >&2
  echo "Threshold handling:" >&2
  echo "  [1] Keep existing thresholds and merge in new missing defaults (Recommended)" >&2
  echo "  [2] Replace thresholds with the latest shipped defaults" >&2
  echo "      This overwrites the installed thresholds.yaml and does not merge old settings." >&2
  printf 'Choose [1/2] [1]: ' >&2
  read -r value
  case "${value:-1}" in
    1|merge|MERGE) printf 'merge' ;;
    2|replace|REPLACE) printf 'replace' ;;
    *)
      echo "Unknown threshold choice: ${value}. Using merge." >&2
      printf 'merge'
      ;;
  esac
}

section() {
  echo
  echo "==> $1"
}

detect_platform_tools() {
  if command -v apt >/dev/null 2>&1; then
    PKG_MGR="apt"
  elif command -v dnf >/dev/null 2>&1; then
    PKG_MGR="dnf"
  elif command -v yum >/dev/null 2>&1; then
    PKG_MGR="yum"
  else
    PKG_MGR=""
  fi
}

install_os_packages() {
  case "${PKG_MGR}" in
    apt)
      apt update
      apt install -y "$@"
      ;;
    dnf)
      dnf install -y "$@"
      ;;
    yum)
      yum install -y "$@"
      ;;
    *)
      return 1
      ;;
  esac
}

helper_asset_name() {
  local os_name arch_name
  os_name="$(uname -s 2>/dev/null || echo "")"
  arch_name="$(uname -m 2>/dev/null || echo "")"

  if [[ "${os_name}" != "Linux" ]]; then
    echo "Unsupported helper platform: ${os_name}" >&2
    return 1
  fi

  case "${arch_name}" in
    x86_64|amd64)
      printf '%s' "${HELPER_ASSET_NAME_LINUX_AMD64}"
      ;;
    *)
      echo "Unsupported helper architecture: ${arch_name}" >&2
      return 1
      ;;
  esac
}

github_curl_args() {
  local -a args=(--http1.1 -fsSL)
  if [[ "${FEATHERDASH_GITHUB_INSECURE:-false}" == "true" ]]; then
    args+=(-k)
  fi
  printf '%s\n' "${args[@]}"
}

helper_installed_version() {
  if [[ ! -x "${HELPER_INSTALL_PATH}" ]]; then
    return 1
  fi
  "${HELPER_INSTALL_PATH}" --version 2>/dev/null | head -n1
}

save_helper_token() {
  local token="$1"
  umask 077
  printf '%s\n' "${token}" > "${HELPER_TOKEN_FILE}"
  chmod 600 "${HELPER_TOKEN_FILE}"
  chown root:root "${HELPER_TOKEN_FILE}"
}

prompt_yes_no() {
  local label="$1"
  local default="${2:-n}"
  local answer

  if [[ "${NONINTERACTIVE}" -eq 1 ]]; then
    [[ "${default}" =~ ^[Yy]$ ]]
    return
  fi

  while true; do
    if [[ "${default}" =~ ^[Yy]$ ]]; then
      printf '%s [Y/n]: ' "${label}" >&2
    else
      printf '%s [y/N]: ' "${label}" >&2
    fi
    read -r answer
    case "${answer:-${default}}" in
      y|Y|yes|YES) return 0 ;;
      n|N|no|NO) return 1 ;;
    esac
    echo "Enter y or n." >&2
  done
}

prompt_helper_source_mode() {
  local answer=""

  if [[ -n "${HELPER_LOCAL_SOURCE}" ]]; then
    printf '%s' "local"
    return 0
  fi

  if [[ "${NONINTERACTIVE}" -eq 1 ]]; then
    printf '%s' "github"
    return 0
  fi

  while true; do
    echo >&2
    echo "Private helper install source:" >&2
    echo "  1) Download from GitHub" >&2
    echo "  2) Use a local file path" >&2
    printf 'Choose [1/2] (default 1): ' >&2
    read -r answer
    case "${answer:-1}" in
      1) printf '%s' "github"; return 0 ;;
      2) printf '%s' "local"; return 0 ;;
    esac
    echo "Enter 1 or 2." >&2
  done
}

prompt_helper_local_source() {
  local local_path="${HELPER_LOCAL_SOURCE:-}"

  if [[ "${NONINTERACTIVE}" -eq 1 ]]; then
    if [[ -z "${local_path}" ]]; then
      echo "Set CTERA_HELPER_LOCAL_PATH in non-interactive mode to use a local helper binary." >&2
      return 1
    fi
  else
    while true; do
      local_path="$(prompt_value "Local path to ${HELPER_NAME}" "${local_path}")"
      if [[ -f "${local_path}" ]]; then
        printf '%s' "${local_path}"
        return 0
      fi
      echo "File not found: ${local_path}" >&2
    done
  fi

  if [[ ! -f "${local_path}" ]]; then
    echo "Local helper file not found: ${local_path}" >&2
    return 1
  fi
  printf '%s' "${local_path}"
}

load_or_prompt_helper_token() {
  local token=""
  local prompt_tty=""
  local saved_token=""

  if [[ -n "${CTERA_HELPER_GITHUB_TOKEN:-}" ]]; then
    printf '%s' "${CTERA_HELPER_GITHUB_TOKEN}"
    return 0
  fi

  if [[ -f "${HELPER_TOKEN_FILE}" ]]; then
    saved_token="$(tr -d '\r\n' < "${HELPER_TOKEN_FILE}")"
    if [[ -n "${saved_token}" ]]; then
      if [[ "${NONINTERACTIVE}" -eq 1 ]]; then
        printf '%s' "${saved_token}"
        return 0
      fi
      if prompt_yes_no "Use saved helper GitHub token from ${HELPER_TOKEN_FILE}?" "y"; then
        printf '%s' "${saved_token}"
        return 0
      fi
      if prompt_yes_no "Replace saved helper GitHub token?" "y"; then
        rm -f "${HELPER_TOKEN_FILE}"
      else
        echo "A helper GitHub token is required to continue." >&2
        return 1
      fi
    fi
  fi

  if [[ "${NONINTERACTIVE}" -eq 1 ]]; then
    echo "Private helper token is required in non-interactive mode. Set CTERA_HELPER_GITHUB_TOKEN." >&2
    echo "Get a fine-grained GitHub PAT for ${HELPER_REPO} read-only access from CTERA support." >&2
    return 1
  fi

  if [[ -r /dev/tty && -w /dev/tty ]]; then
    prompt_tty="/dev/tty"
  fi

  echo
  echo "A private helper binary is required from ${HELPER_REPO}."
  echo "Get a fine-grained GitHub PAT with read-only access to ${HELPER_REPO} from CTERA support." >&2
  if [[ -n "${prompt_tty}" ]]; then
    printf 'GitHub token for private helper download: ' > "${prompt_tty}"
    read -rs token < "${prompt_tty}"
    printf '\n' > "${prompt_tty}"
  else
    echo "This upgrade session does not have an interactive TTY for secret entry." >&2
    echo "Re-run interactively or export CTERA_HELPER_GITHUB_TOKEN before upgrade." >&2
    return 1
  fi
  echo >&2
  if [[ -z "${token}" ]]; then
    echo "GitHub token is required to download ${HELPER_NAME}." >&2
    return 1
  fi

  if prompt_yes_no "Save helper GitHub token for future upgrades?" "y"; then
    save_helper_token "${token}"
  fi

  printf '%s' "${token}"
}

install_helper_from_local_path() {
  local source_path="$1"
  local current_version=""
  local tmp_dir tmp_file

  tmp_dir="$(mktemp -d)"
  tmp_file="${tmp_dir}/$(basename "${source_path}")"
  cp "${source_path}" "${tmp_file}"
  chmod 0755 "${tmp_file}"
  install -d "$(dirname "${HELPER_INSTALL_PATH}")"
  install -m 0755 "${tmp_file}" "${HELPER_INSTALL_PATH}"
  rm -rf "${tmp_dir}"

  current_version="$(helper_installed_version || true)"
  if [[ "${current_version}" != "${HELPER_VERSION}" ]]; then
    echo "Installed helper version '${current_version:-unknown}' does not match expected ${HELPER_VERSION}." >&2
    return 1
  fi

  echo "  Installed ${HELPER_NAME} ${current_version} from local path to ${HELPER_INSTALL_PATH}"
}

install_private_helper() {
  local current_version=""
  local helper_source_mode=""
  local local_helper_path=""
  current_version="$(helper_installed_version || true)"
  if [[ "${current_version}" == "${HELPER_VERSION}" ]]; then
    return 0
  fi

  section "Installing private telnet helper"

  helper_source_mode="$(prompt_helper_source_mode)"
  if [[ "${helper_source_mode}" == "local" ]]; then
    local_helper_path="$(prompt_helper_local_source)" || return 1
    install_helper_from_local_path "${local_helper_path}"
    return 0
  fi

  local asset_name checksum_name token asset_api_url checksum_api_url tmp_dir tmp_file checksum_file
  asset_name="$(helper_asset_name)"
  checksum_name="${asset_name}${HELPER_CHECKSUM_SUFFIX}"
  token="$(load_or_prompt_helper_token)"
  asset_api_url="https://api.github.com/repos/${HELPER_REPO}/contents/${asset_name}?ref=${HELPER_REF}"
  checksum_api_url="https://api.github.com/repos/${HELPER_REPO}/contents/${checksum_name}?ref=${HELPER_REF}"
  tmp_dir="$(mktemp -d)"
  tmp_file="${tmp_dir}/${asset_name}"
  checksum_file="${tmp_dir}/${checksum_name}"

  mapfile -t CURL_ARGS < <(github_curl_args)
  if ! curl "${CURL_ARGS[@]}" \
    -H "Accept: application/vnd.github.raw" \
    -H "Authorization: Bearer ${token}" \
    "${asset_api_url}" -o "${tmp_file}"; then
    rm -rf "${tmp_dir}"
    echo "Could not download helper asset ${asset_name} from ${HELPER_REPO}." >&2
    return 1
  fi

  if ! curl "${CURL_ARGS[@]}" \
    -H "Accept: application/vnd.github.raw" \
    -H "Authorization: Bearer ${token}" \
    "${checksum_api_url}" -o "${checksum_file}"; then
    rm -rf "${tmp_dir}"
    echo "Could not download helper checksum ${checksum_name} from ${HELPER_REPO}." >&2
    return 1
  fi

  if ! (cd "${tmp_dir}" && sha256sum -c "${checksum_name}"); then
    rm -rf "${tmp_dir}"
    echo "Checksum validation failed for ${asset_name}." >&2
    return 1
  fi

  install -d "$(dirname "${HELPER_INSTALL_PATH}")"
  install -m 0755 "${tmp_file}" "${HELPER_INSTALL_PATH}"
  rm -rf "${tmp_dir}"

  current_version="$(helper_installed_version || true)"
  if [[ "${current_version}" != "${HELPER_VERSION}" ]]; then
    echo "Installed helper version '${current_version:-unknown}' does not match expected ${HELPER_VERSION}." >&2
    return 1
  fi

  echo "  Installed ${HELPER_NAME} ${current_version} to ${HELPER_INSTALL_PATH}"
}

ensure_scheduler_packages() {
  local missing=0
  command -v sqlite3 >/dev/null 2>&1 || missing=1
  if [[ "${missing}" -eq 0 ]]; then
    return 0
  fi

  detect_platform_tools
  section "Installing scheduler dependencies"
  case "${PKG_MGR}" in
    apt)
      install_os_packages sqlite3
      ;;
    dnf|yum)
      install_os_packages sqlite
      ;;
    *)
      echo "Warning: could not determine package manager to install sqlite3 automatically." >&2
      ;;
  esac
}

ensure_sudoers_include() {
  local include_pattern='^[[:space:]]*[#@]includedir[[:space:]]+/etc/sudoers\.d([[:space:]]|$)'
  if grep -Eq "${include_pattern}" /etc/sudoers 2>/dev/null; then
    return 0
  fi
  if ! command -v visudo >/dev/null 2>&1; then
    echo "Warning: visudo not found; cannot automatically enable /etc/sudoers.d include." >&2
    return 1
  fi
  section "Enabling /etc/sudoers.d include"
  cp /etc/sudoers "/etc/sudoers.ctera-monitoring-dashboard.bak"
  printf '\n#includedir /etc/sudoers.d\n' >> /etc/sudoers
  if ! visudo -c >/dev/null 2>&1; then
    mv -f "/etc/sudoers.ctera-monitoring-dashboard.bak" /etc/sudoers
    echo "Failed to validate /etc/sudoers after enabling sudoers.d include." >&2
    return 1
  fi
  rm -f "/etc/sudoers.ctera-monitoring-dashboard.bak"
  return 0
}

install_upgrade_helper() {
  section "Installing UI upgrade helper"
  ensure_sudoers_include || true
  cat > "${UPGRADE_HELPER}" <<EOF
#!/usr/bin/env bash
set -euo pipefail

INSTALL_DIR='${INSTALL_DIR}'
CONFIG_FILE='${CONFIG_FILE}'
DATA_DIR='${DATA_DIR}'
LOG_DIR='${LOG_DIR}'
SERVICE_USER='${SERVICE_USER}'
BACKUP_ROOT='${BACKUP_ROOT}'
STATE_DIR='${INSTALL_DIR}/state'
STATE_FILE="\${STATE_DIR}/upgrade.state"
SETTINGS_FILE="\${STATE_DIR}/upgrade_network.env"
LOG_FILE="\${LOG_DIR}/upgrade.log"
ARCHIVE_URL='https://github.com/ctera/CTERA-Monitoring-Dashboard/archive/refs/heads/main.tar.gz'
THRESHOLD_STRATEGY="\${1:-merge}"

if [[ "\${THRESHOLD_STRATEGY}" == "--validate-access" ]]; then
  exit 0
fi

case "\${THRESHOLD_STRATEGY}" in
  merge|replace) ;;
  *)
    echo "Unsupported threshold strategy: \${THRESHOLD_STRATEGY}" >&2
    exit 2
    ;;
esac

mkdir -p "\${STATE_DIR}" "\${LOG_DIR}" "\${BACKUP_ROOT}"
STARTED_AT="\$(date -u +%Y-%m-%dT%H:%M:%SZ)"
TMP_DIR="\$(mktemp -d /tmp/ctera-monitoring-dashboard-upgrade-XXXXXX)"
if [[ -f "\${SETTINGS_FILE}" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "\${SETTINGS_FILE}"
  set +a
fi
if [[ -n "\${FEATHERDASH_GITHUB_HTTP_PROXY:-}" ]]; then
  export http_proxy="\${FEATHERDASH_GITHUB_HTTP_PROXY}" HTTP_PROXY="\${FEATHERDASH_GITHUB_HTTP_PROXY}"
fi
if [[ -n "\${FEATHERDASH_GITHUB_HTTPS_PROXY:-}" ]]; then
  export https_proxy="\${FEATHERDASH_GITHUB_HTTPS_PROXY}" HTTPS_PROXY="\${FEATHERDASH_GITHUB_HTTPS_PROXY}"
fi
CURL_ARGS=(-fsSL)
if [[ "\${FEATHERDASH_GITHUB_INSECURE:-false}" == "true" ]]; then
  CURL_ARGS+=(-k)
fi

write_state() {
  local status="\$1"
  local finished_at="\$2"
  local last_exit="\$3"
  local pid_value="\${4:-}"
  local tmp="\${STATE_FILE}.tmp"
  cat > "\${tmp}" <<STATEEOF
status=\${status}
started_at=\${STARTED_AT}
finished_at=\${finished_at}
last_exit=\${last_exit}
pid=\${pid_value}
STATEEOF
  mv "\${tmp}" "\${STATE_FILE}"
}

finish_upgrade() {
  local rc=\$?
  local final_status="finished"
  if [[ "\${rc}" -ne 0 ]]; then
    final_status="failed"
  fi
  write_state "\${final_status}" "\$(date -u +%Y-%m-%dT%H:%M:%SZ)" "\${rc}" ""
  rm -rf "\${TMP_DIR}" >/dev/null 2>&1 || true
  exit "\${rc}"
}

write_state "running" "" "" "\$\$"
trap finish_upgrade EXIT

{
  echo "\$(date '+%Y-%m-%d %H:%M:%S') Starting UI-triggered upgrade (\${THRESHOLD_STRATEGY})"
  curl "\${CURL_ARGS[@]}" "\${ARCHIVE_URL}" -o "\${TMP_DIR}/package.tgz"
  mkdir -p "\${TMP_DIR}/package"
  tar -xzf "\${TMP_DIR}/package.tgz" -C "\${TMP_DIR}/package" --strip-components=1
  cd "\${TMP_DIR}/package"
  bash ./upgrade.sh \\
    --install-dir "\${INSTALL_DIR}" \\
    --config-file "\${CONFIG_FILE}" \\
    --data-dir "\${DATA_DIR}" \\
    --log-dir "\${LOG_DIR}" \\
    --user "\${SERVICE_USER}" \\
    --backup-root "\${BACKUP_ROOT}" \\
    --threshold-strategy "\${THRESHOLD_STRATEGY}" \\
    --non-interactive
  echo "\$(date '+%Y-%m-%d %H:%M:%S') Upgrade helper completed"
} >> "\${LOG_FILE}" 2>&1
EOF
  chmod 755 "${UPGRADE_HELPER}"
  chown root:root "${UPGRADE_HELPER}"

  cat > "${UPGRADE_SUDOERS}" <<EOF
${SERVICE_USER} ALL=(root) NOPASSWD: ${UPGRADE_HELPER}
EOF
  chmod 440 "${UPGRADE_SUDOERS}"
  chown root:root "${UPGRADE_SUDOERS}"
}

read_version_file() {
  local version_file="$1"
  if [[ -f "${version_file}" ]]; then
    tr -d '\r' < "${version_file}" | head -n 1
  else
    printf 'unknown'
  fi
}

copy_dir_contents() {
  local src_dir="$1"
  local dst_dir="$2"
  mkdir -p "${dst_dir}"
  if [[ -d "${src_dir}" ]]; then
    cp -a "${src_dir}/." "${dst_dir}/"
  fi
}

if [[ "${NONINTERACTIVE}" -eq 0 ]]; then
  INSTALL_DIR="$(prompt_value "Current installation directory" "${INSTALL_DIR}")"
  BACKUP_ROOT="$(prompt_value "Backup location" "${BACKUP_ROOT}")"
  THRESHOLD_STRATEGY="$(prompt_threshold_strategy)"
fi

if [[ ! -d "${INSTALL_DIR}" ]]; then
  echo "Installation directory does not exist: ${INSTALL_DIR}" >&2
  exit 1
fi

CURRENT_VERSION="$(read_version_file "${INSTALL_DIR}/VERSION")"
PACKAGE_VERSION="$(read_version_file "${SCRIPT_DIR}/VERSION")"
TIMESTAMP="$(date '+%Y%m%d-%H%M%S')"
BACKUP_DIR="${BACKUP_ROOT}/${PRODUCT_SLUG}-${CURRENT_VERSION}-${TIMESTAMP}"
TMP_BACKUP="$(mktemp -d)"

cleanup() {
  rm -rf "${TMP_BACKUP}"
}
trap cleanup EXIT

section "Preparing upgrade"
echo "Product:      ${PRODUCT_NAME}"
echo "Current ver:  ${CURRENT_VERSION}"
echo "Package ver:  ${PACKAGE_VERSION}"
echo "Package dir:  ${SCRIPT_DIR}"
echo "Install dir:  ${INSTALL_DIR}"
echo "Config file:  ${CONFIG_FILE}"
echo "Data dir:     ${DATA_DIR}"
echo "Log dir:      ${LOG_DIR}"
echo "Service user: ${SERVICE_USER}"
echo "Backup dir:   ${BACKUP_DIR}"
echo "Thresholds:   ${THRESHOLD_STRATEGY}"

preserve_if_present() {
  local rel_path="$1"
  if [[ -e "${INSTALL_DIR}/${rel_path}" ]]; then
    mkdir -p "${TMP_BACKUP}/$(dirname "${rel_path}")"
    cp -a "${INSTALL_DIR}/${rel_path}" "${TMP_BACKUP}/${rel_path}"
  fi
}

restore_if_present() {
  local rel_path="$1"
  if [[ -e "${TMP_BACKUP}/${rel_path}" ]]; then
    mkdir -p "${INSTALL_DIR}/$(dirname "${rel_path}")"
    cp -a "${TMP_BACKUP}/${rel_path}" "${INSTALL_DIR}/${rel_path}"
  fi
}

merge_thresholds_if_needed() {
  local existing_path="${TMP_BACKUP}/thresholds.yaml"
  local shipped_path="${INSTALL_DIR}/thresholds.yaml"
  if [[ ! -f "${existing_path}" || ! -f "${shipped_path}" ]]; then
    return
  fi
  "${INSTALL_DIR}/venv/bin/python" - "${existing_path}" "${shipped_path}" <<'PY'
import copy
import sys
from pathlib import Path
import yaml

existing_path = Path(sys.argv[1])
shipped_path = Path(sys.argv[2])

def merge_keep_existing(existing, shipped):
    if isinstance(existing, dict) and isinstance(shipped, dict):
        merged = copy.deepcopy(existing)
        for key, value in shipped.items():
            if key in merged:
                merged[key] = merge_keep_existing(merged[key], value)
            else:
                merged[key] = copy.deepcopy(value)
        return merged
    return copy.deepcopy(existing)

existing = yaml.safe_load(existing_path.read_text(encoding="utf-8")) or {}
shipped = yaml.safe_load(shipped_path.read_text(encoding="utf-8")) or {}
merged = merge_keep_existing(existing, shipped)
with shipped_path.open("w", encoding="utf-8", newline="\n") as handle:
    handle.write(yaml.safe_dump(merged, sort_keys=False, allow_unicode=False))
PY
  echo "Thresholds merged: kept installed values and added any new shipped defaults."
}

if [[ "${THRESHOLD_STRATEGY}" == "merge" ]]; then
  preserve_if_present "thresholds.yaml"
fi
preserve_if_present "dashboard/config.yaml"

section "Creating backup"
mkdir -p "${BACKUP_DIR}/app" "${BACKUP_DIR}/paths" "${BACKUP_DIR}/service"
copy_dir_contents "${INSTALL_DIR}" "${BACKUP_DIR}/app"
if [[ -f "${CONFIG_FILE}" ]]; then
  mkdir -p "${BACKUP_DIR}/config"
  cp -a "${CONFIG_FILE}" "${BACKUP_DIR}/config/"
fi
if [[ -d "${DATA_DIR}" ]]; then
  mkdir -p "${BACKUP_DIR}/data"
  copy_dir_contents "${DATA_DIR}" "${BACKUP_DIR}/data"
fi
if [[ -d "${LOG_DIR}" ]]; then
  mkdir -p "${BACKUP_DIR}/logs"
  copy_dir_contents "${LOG_DIR}" "${BACKUP_DIR}/logs"
fi
if [[ -f "${SERVICE_FILE}" ]]; then
  cp -a "${SERVICE_FILE}" "${BACKUP_DIR}/service/"
fi
if [[ -f "${CRON_FILE}" ]]; then
  cp -a "${CRON_FILE}" "${BACKUP_DIR}/service/"
fi

cat > "${BACKUP_DIR}/paths/metadata.env" <<EOF
PRODUCT_NAME='${PRODUCT_NAME}'
PRODUCT_SLUG='${PRODUCT_SLUG}'
INSTALL_DIR='${INSTALL_DIR}'
CONFIG_FILE='${CONFIG_FILE}'
DATA_DIR='${DATA_DIR}'
LOG_DIR='${LOG_DIR}'
SERVICE_USER='${SERVICE_USER}'
SERVICE_FILE='${SERVICE_FILE}'
CRON_FILE='${CRON_FILE}'
BACKUP_VERSION='${CURRENT_VERSION}'
BACKUP_CREATED_AT='${TIMESTAMP}'
EOF

cat > "${BACKUP_DIR}/restore.sh" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail

BACKUP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${BACKUP_DIR}/paths/metadata.env"

copy_dir_contents() {
  local src_dir="$1"
  local dst_dir="$2"
  mkdir -p "${dst_dir}"
  if [[ -d "${src_dir}" ]]; then
    cp -a "${src_dir}/." "${dst_dir}/"
  fi
}

echo
echo "==> Restoring ${PRODUCT_NAME} from backup"
echo "Backup dir:   ${BACKUP_DIR}"
echo "Install dir:  ${INSTALL_DIR}"
echo "Config file:  ${CONFIG_FILE}"
echo "Data dir:     ${DATA_DIR}"
echo "Log dir:      ${LOG_DIR}"

if [[ "${EUID}" -ne 0 ]]; then
  echo "Run this restore as root, for example: sudo bash ./restore.sh" >&2
  exit 1
fi

if systemctl list-unit-files | grep -q "^${PRODUCT_SLUG}\.service"; then
  systemctl stop "${PRODUCT_SLUG}" || true
fi

rm -rf "${INSTALL_DIR}"
mkdir -p "$(dirname "${INSTALL_DIR}")"
mkdir -p "${INSTALL_DIR}"
copy_dir_contents "${BACKUP_DIR}/app" "${INSTALL_DIR}"

if [[ -d "${BACKUP_DIR}/data" ]]; then
  rm -rf "${DATA_DIR}"
  mkdir -p "$(dirname "${DATA_DIR}")"
  mkdir -p "${DATA_DIR}"
  copy_dir_contents "${BACKUP_DIR}/data" "${DATA_DIR}"
fi

if [[ -d "${BACKUP_DIR}/logs" ]]; then
  rm -rf "${LOG_DIR}"
  mkdir -p "$(dirname "${LOG_DIR}")"
  mkdir -p "${LOG_DIR}"
  copy_dir_contents "${BACKUP_DIR}/logs" "${LOG_DIR}"
fi

if [[ -d "${BACKUP_DIR}/config" ]]; then
  mkdir -p "$(dirname "${CONFIG_FILE}")"
  cp -a "${BACKUP_DIR}/config/$(basename "${CONFIG_FILE}")" "${CONFIG_FILE}"
fi

if [[ -f "${BACKUP_DIR}/service/$(basename "${SERVICE_FILE}")" ]]; then
  mkdir -p "$(dirname "${SERVICE_FILE}")"
  cp -a "${BACKUP_DIR}/service/$(basename "${SERVICE_FILE}")" "${SERVICE_FILE}"
fi

if [[ -f "${BACKUP_DIR}/service/$(basename "${CRON_FILE}")" ]]; then
  mkdir -p "$(dirname "${CRON_FILE}")"
  cp -a "${BACKUP_DIR}/service/$(basename "${CRON_FILE}")" "${CRON_FILE}"
  chmod 644 "${CRON_FILE}" || true
fi

chown -R "${SERVICE_USER}:${SERVICE_USER}" "${INSTALL_DIR}" || true
if [[ -d "${DATA_DIR}" ]]; then
  chown -R "${SERVICE_USER}:${SERVICE_USER}" "${DATA_DIR}" || true
fi
if [[ -d "${LOG_DIR}" ]]; then
  chown -R "${SERVICE_USER}:${SERVICE_USER}" "${LOG_DIR}" || true
fi

systemctl daemon-reload
systemctl enable --now cron >/dev/null 2>&1 || true
systemctl restart "${PRODUCT_SLUG}"

echo
echo "Restore complete."
echo "Useful checks:"
echo "  sudo systemctl status ${PRODUCT_SLUG} --no-pager"
echo "  sudo journalctl -u ${PRODUCT_SLUG} -n 100 --no-pager"
EOF
chmod +x "${BACKUP_DIR}/restore.sh"
echo "Backup created at ${BACKUP_DIR}"

section "Copying updated application files"
cp -a "${SCRIPT_DIR}/." "${INSTALL_DIR}/"

restore_if_present "dashboard/config.yaml"

chmod +x \
  "${INSTALL_DIR}/install.sh" \
  "${INSTALL_DIR}/install_featherdash.sh" \
  "${INSTALL_DIR}/upgrade.sh" \
  "${INSTALL_DIR}/scheduler_jobs.sh" \
  "${INSTALL_DIR}/portal_jobs.sh" \
  "${INSTALL_DIR}/filer_jobs.sh"

if [[ ! -d "${INSTALL_DIR}/venv" ]]; then
  section "Creating virtual environment"
  python3 -m venv "${INSTALL_DIR}/venv"
fi

section "Installing Python requirements"
"${INSTALL_DIR}/venv/bin/pip" install -r "${INSTALL_DIR}/requirements.txt"
install_private_helper

ensure_scheduler_packages
install_upgrade_helper

if [[ "${THRESHOLD_STRATEGY}" == "merge" ]]; then
  section "Merging threshold defaults"
  merge_thresholds_if_needed
else
  section "Replacing thresholds"
  echo "Thresholds replaced with the latest shipped defaults."
fi

section "Fixing ownership"
mkdir -p "${DATA_DIR}" "${LOG_DIR}"
chown -R "${SERVICE_USER}:${SERVICE_USER}" "${INSTALL_DIR}"
chown -R "${SERVICE_USER}:${SERVICE_USER}" "${DATA_DIR}"
chown -R "${SERVICE_USER}:${SERVICE_USER}" "${LOG_DIR}"

section "Restarting service"
systemctl daemon-reload
systemctl restart "${PRODUCT_SLUG}"

echo
echo "${PRODUCT_NAME} upgrade complete."
echo "Backup saved at: ${BACKUP_DIR}"
echo "Restore command:"
echo "  sudo bash ${BACKUP_DIR}/restore.sh"
echo
echo "Useful checks:"
echo "  sudo systemctl status ${PRODUCT_SLUG} --no-pager"
echo "  curl -I http://127.0.0.1:\${PORT:-8080}/healthz"
echo "  sudo journalctl -u ${PRODUCT_SLUG} -n 100 --no-pager"
