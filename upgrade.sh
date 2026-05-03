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

install_upgrade_helper() {
  section "Installing UI upgrade helper"
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
LOG_FILE="\${LOG_DIR}/upgrade.log"
ARCHIVE_URL='https://github.com/ctera/CTERA-Monitoring-Dashboard/archive/refs/heads/main.tar.gz'
THRESHOLD_STRATEGY="\${1:-merge}"

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
  curl -fsSL "\${ARCHIVE_URL}" -o "\${TMP_DIR}/package.tgz"
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
