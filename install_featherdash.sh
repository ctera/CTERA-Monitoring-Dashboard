#!/usr/bin/env bash
set -euo pipefail

PRODUCT_NAME="CTERA Monitoring Dashboard"
PRODUCT_SLUG="ctera-monitoring-dashboard"
LEGACY_SERVICE_NAME="featherdash"
INSTALL_DIR="/opt/monitoring/ctera-monitoring-dashboard"
CONFIG_FILE="/etc/ctera-monitoring-dashboard.env"
DATA_DIR="/var/lib/ctera-monitoring-dashboard/data"
LOG_DIR="/var/log/ctera-monitoring-dashboard"
SERVICE_USER="ctera-monitoring"
UPGRADE_HELPER="/usr/local/sbin/ctera-monitoring-dashboard-upgrade"
UPGRADE_SUDOERS="/etc/sudoers.d/ctera-monitoring-dashboard-upgrade"
ARCHIVE=""
NONINTERACTIVE=0
SKIP_CONFIRM=0
DASHBOARD_PORT="8080"
PKG_MGR=""
CRON_SERVICE_NAME="cron"
HELPER_NAME="ctera-secret-helper"
HELPER_INSTALL_PATH="/usr/local/bin/${HELPER_NAME}"
HELPER_VERSION="0.1.0"
HELPER_REPO="mj-ctera/binary-token"
HELPER_ASSET_NAME_LINUX_AMD64="${HELPER_NAME}-linux-amd64"
HELPER_CHECKSUM_SUFFIX=".sha256"
HELPER_REF="main"
HELPER_TOKEN_FILE="/etc/ctera-monitoring-dashboard-helper.token"
HELPER_LOCAL_SOURCE="${CTERA_HELPER_LOCAL_PATH:-}"
HELPER_SOURCE_MODE="${CTERA_HELPER_SOURCE_MODE:-}"
HELPER_SAVE_TOKEN="${CTERA_HELPER_SAVE_TOKEN:-false}"

usage() {
  cat <<'EOF'
Usage:
  sudo bash ./install.sh [options]

Options:
  --install-dir /opt/monitoring/ctera-monitoring-dashboard   App location
  --config-file /etc/ctera-monitoring-dashboard.env          One admin-editable config file
  --data-dir /var/lib/ctera-monitoring-dashboard/data        One CSV data location
  --user ctera-monitoring                                     Dedicated service user
  --archive /path/to/ctera-monitoring-dashboard.tgz          Extract archive before installing
  --non-interactive                           Use defaults and blank credentials
  --yes                                       Do not ask for final confirmation

Recommended:
  sudo tar -xzf ctera-monitoring-dashboard.tgz -C /opt/monitoring
  cd /opt/monitoring/ctera-monitoring-dashboard
  sudo bash ./install.sh
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
    --user)
      SERVICE_USER="${2:-}"
      shift 2
      ;;
    --archive)
      ARCHIVE="${2:-}"
      shift 2
      ;;
    --non-interactive)
      NONINTERACTIVE=1
      shift
      ;;
    --yes|-y)
      SKIP_CONFIRM=1
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

section() {
  echo
  echo "==> $1"
}

detect_platform_tools() {
  if command -v apt >/dev/null 2>&1; then
    PKG_MGR="apt"
    CRON_SERVICE_NAME="cron"
  elif command -v dnf >/dev/null 2>&1; then
    PKG_MGR="dnf"
    CRON_SERVICE_NAME="crond"
  elif command -v yum >/dev/null 2>&1; then
    PKG_MGR="yum"
    CRON_SERVICE_NAME="crond"
  else
    echo "Unsupported system: no apt, dnf, or yum package manager found." >&2
    exit 1
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
      echo "Package manager not initialized." >&2
      exit 1
      ;;
  esac
}

install_base_packages() {
  case "${PKG_MGR}" in
    apt)
      install_os_packages python3 python3-venv python3-pip cron curl jq net-tools openssh-client sqlite3 nginx
      ;;
    dnf|yum)
      install_os_packages python3 python3-pip cronie curl jq net-tools openssh-clients sshpass sqlite nginx
      ;;
  esac
}

install_ssh_helper_packages() {
  case "${PKG_MGR}" in
    apt)
      install_os_packages sshpass openssh-client
      ;;
    dnf|yum)
      install_os_packages sshpass openssh-clients
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

helper_bundled_source() {
  local asset_name=""
  asset_name="$(helper_asset_name 2>/dev/null || true)"
  if [[ -z "${asset_name}" ]]; then
    return 1
  fi
  if [[ -f "${SCRIPT_DIR}/${asset_name}" ]]; then
    printf '%s' "${SCRIPT_DIR}/${asset_name}"
    return 0
  fi
  return 1
}

github_curl_args() {
  local -a args=(--http1.1 -fsSL --retry 5 --retry-delay 2 --retry-all-errors)
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

is_truthy() {
  case "$(printf '%s' "${1:-}" | tr '[:upper:]' '[:lower:]')" in
    1|true|yes|y|on) return 0 ;;
  esac
  return 1
}

prompt_helper_source_mode() {
  local answer=""

  if [[ "${HELPER_SOURCE_MODE}" == "bundled" || "${HELPER_SOURCE_MODE}" == "github" || "${HELPER_SOURCE_MODE}" == "local" ]]; then
    printf '%s' "${HELPER_SOURCE_MODE}"
    return 0
  fi

  if [[ -n "${HELPER_LOCAL_SOURCE}" ]]; then
    printf '%s' "local"
    return 0
  fi

  if helper_bundled_source >/dev/null 2>&1; then
    if [[ "${NONINTERACTIVE}" -eq 1 ]]; then
      printf '%s' "bundled"
      return 0
    fi
  fi

  if [[ "${NONINTERACTIVE}" -eq 1 ]]; then
    printf '%s' "github"
    return 0
  fi

  while true; do
    echo >&2
    echo "Helper install source:" >&2
    if helper_bundled_source >/dev/null 2>&1; then
      echo "  1) Use the existing helper on the server (Recommended)" >&2
      echo "  2) Download a new helper from GitHub" >&2
      echo "  3) Use a local file path" >&2
      printf 'Choose [1/2/3] (default 1): ' >&2
    else
      echo "  1) Download a new helper from GitHub" >&2
      echo "  2) Use a local file path" >&2
      printf 'Choose [1/2] (default 1): ' >&2
    fi
    read -r answer
    if helper_bundled_source >/dev/null 2>&1; then
      case "${answer:-1}" in
        1) printf '%s' "bundled"; return 0 ;;
        2) printf '%s' "github"; return 0 ;;
        3) printf '%s' "local"; return 0 ;;
      esac
      echo "  Enter 1, 2, or 3." >&2
    else
      case "${answer:-1}" in
        1) printf '%s' "github"; return 0 ;;
        2) printf '%s' "local"; return 0 ;;
      esac
      echo "  Enter 1 or 2." >&2
    fi
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
      local_path="$(prompt_value "Local path to ${HELPER_NAME}" "${local_path}" 0 1)"
      if [[ -f "${local_path}" ]]; then
        printf '%s' "${local_path}"
        return 0
      fi
      echo "  File not found: ${local_path}" >&2
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
    if is_truthy "${HELPER_SAVE_TOKEN}"; then
      save_helper_token "${CTERA_HELPER_GITHUB_TOKEN}"
    fi
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
    echo "This install session does not have an interactive TTY for secret entry." >&2
    echo "Re-run interactively or export CTERA_HELPER_GITHUB_TOKEN before install." >&2
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

  section "Installing helper"

  helper_source_mode="$(prompt_helper_source_mode)"
  if [[ "${helper_source_mode}" == "bundled" ]]; then
    local_helper_path="$(helper_bundled_source)" || {
      echo "No helper was found on the server, and no helper binary was found in this package." >&2
      return 1
    }
    install_helper_from_local_path "${local_helper_path}"
    return 0
  fi
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

configure_local_firewall() {
  local port="$1"

  section "Configuring local firewall for port ${port}"

  if command -v firewall-cmd >/dev/null 2>&1; then
    if firewall-cmd --state >/dev/null 2>&1; then
      firewall-cmd --quiet --add-port="${port}/tcp"
      firewall-cmd --quiet --permanent --add-port="${port}/tcp"
      echo "  Opened TCP/${port} in firewalld."
      return 0
    fi
  fi

  if command -v ufw >/dev/null 2>&1; then
    if ufw status 2>/dev/null | grep -qi '^status: active'; then
      ufw allow "${port}/tcp" >/dev/null 2>&1 || true
      echo "  Opened TCP/${port} in ufw."
      return 0
    fi
  fi

  echo "  No active supported firewall manager detected. Skipping automatic firewall rule."
  return 0
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
BACKUP_ROOT='/opt/monitoring-backup'
STATE_DIR='${INSTALL_DIR}/state'
STATE_FILE="\${STATE_DIR}/upgrade.state"
SETTINGS_FILE="\${STATE_DIR}/upgrade_network.env"
SSL_REQUEST_FILE="\${INSTALL_DIR}/state/ssl/runtime.env"
SSL_LOG_FILE="\${LOG_DIR}/ssl-apply.log"
NGINX_CONF_FILE='/etc/nginx/conf.d/ctera-monitoring-dashboard.conf'
LOG_FILE="\${LOG_DIR}/upgrade.log"
ARCHIVE_URL='https://github.com/ctera/CTERA-Monitoring-Dashboard/archive/refs/heads/main.tar.gz'
ACTION="\${1:-merge}"

if [[ "\${ACTION}" == "--validate-access" ]]; then
  exit 0
fi

set_config_key() {
  local key="\$1"
  local value="\$2"
  if grep -qE "^\\\${key}=" "\${CONFIG_FILE}" 2>/dev/null; then
    sed -i "s|^\\\${key}=.*|\\\${key}=\\\${value}|" "\${CONFIG_FILE}"
  else
    printf '%s=%s\n' "\${key}" "\${value}" >> "\${CONFIG_FILE}"
  fi
}

open_firewall_port() {
  local port="\$1"
  if command -v firewall-cmd >/dev/null 2>&1 && firewall-cmd --state >/dev/null 2>&1; then
    firewall-cmd --quiet --add-port="\${port}/tcp" || true
    firewall-cmd --quiet --permanent --add-port="\${port}/tcp" || true
  elif command -v ufw >/dev/null 2>&1 && ufw status 2>/dev/null | grep -qi '^status: active'; then
    ufw allow "\${port}/tcp" >/dev/null 2>&1 || true
  fi
}

install_nginx_if_missing() {
  if command -v nginx >/dev/null 2>&1; then
    return 0
  fi
  if command -v apt >/dev/null 2>&1; then
    apt update
    apt install -y nginx
  elif command -v dnf >/dev/null 2>&1; then
    dnf install -y nginx
  elif command -v yum >/dev/null 2>&1; then
    yum install -y nginx
  else
    echo "Could not install nginx automatically." >&2
    exit 1
  fi
}

apply_ssl_runtime() {
  mkdir -p "\$(dirname "\${SSL_LOG_FILE}")" "\$(dirname "\${NGINX_CONF_FILE}")"
  if [[ -f "\${SSL_REQUEST_FILE}" ]]; then
    set -a
    # shellcheck disable=SC1090
    source "\${SSL_REQUEST_FILE}"
    set +a
  else
    echo "SSL runtime request file not found: \${SSL_REQUEST_FILE}" >&2
    exit 1
  fi

  local enabled="\${SSL_ENABLED:-false}"
  local https_port="\${SSL_HTTPS_PORT:-8443}"
  local redirect_http="\${SSL_REDIRECT_HTTP:-true}"
  local cert_path="\${SSL_CERT_PATH:-}"
  local key_path="\${SSL_KEY_PATH:-}"
  local ca_path="\${SSL_CA_PATH:-}"

  if [[ "\${enabled}" == "true" ]]; then
    if [[ ! -f "\${cert_path}" || ! -f "\${key_path}" ]]; then
      echo "HTTPS is enabled but the certificate files are missing." >&2
      exit 1
    fi
    install_nginx_if_missing

    local redirect_target="https://\\\$host"
    if [[ "\${https_port}" != "443" ]]; then
      redirect_target="https://\\\$host:\${https_port}"
    fi

    cat > "\${NGINX_CONF_FILE}" <<NGINXEOF
server {
    listen \${https_port} ssl;
    server_name _;

    ssl_certificate \${cert_path};
    ssl_certificate_key \${key_path};
NGINXEOF
    if [[ -n "\${ca_path}" && -f "\${ca_path}" ]]; then
      cat >> "\${NGINX_CONF_FILE}" <<NGINXEOF
    ssl_trusted_certificate \${ca_path};
NGINXEOF
    fi
    cat >> "\${NGINX_CONF_FILE}" <<'NGINXEOF'

    location / {
        proxy_pass http://127.0.0.1:8081;
        proxy_http_version 1.1;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto https;
        proxy_set_header X-Forwarded-Host \$host;
    }
}
NGINXEOF
    if [[ "\${redirect_http}" == "true" ]]; then
      cat >> "\${NGINX_CONF_FILE}" <<NGINXEOF

server {
    listen 8080;
    server_name _;
    return 301 \${redirect_target}\\\$request_uri;
}
NGINXEOF
    fi
    nginx -t
    systemctl enable nginx >/dev/null 2>&1 || true
    systemctl restart nginx
    set_config_key PORT 8081
    set_config_key FEATHERDASH_BIND_HOST 127.0.0.1
    systemctl restart "${PRODUCT_SLUG}"
    open_firewall_port "\${https_port}"
    if [[ "\${redirect_http}" == "true" ]]; then
      open_firewall_port 8080
    fi
  else
    set_config_key PORT 8080
    set_config_key FEATHERDASH_BIND_HOST 0.0.0.0
    systemctl restart "${PRODUCT_SLUG}"
    rm -f "\${NGINX_CONF_FILE}"
    if command -v nginx >/dev/null 2>&1; then
      nginx -t >/dev/null 2>&1 || true
      systemctl restart nginx >/dev/null 2>&1 || true
    fi
  fi
}

case "\${ACTION}" in
  merge|replace)
    THRESHOLD_STRATEGY="\${ACTION}"
    ;;
  ssl-apply)
    apply_ssl_runtime >> "\${SSL_LOG_FILE}" 2>&1
    exit \$?
    ;;
  *)
    echo "Unsupported helper action: \${ACTION}" >&2
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

if [[ "${EUID}" -ne 0 ]]; then
  echo "Run this installer as root, for example: sudo bash ./install_featherdash.sh" >&2
  exit 1
fi

detect_platform_tools

if [[ -z "${INSTALL_DIR}" || -z "${CONFIG_FILE}" || -z "${DATA_DIR}" || -z "${SERVICE_USER}" ]]; then
  echo "Install dir, config file, data dir, and user must not be empty." >&2
  exit 1
fi

DB_DIR="${DATA_DIR}/db"

print_banner() {
  cat <<EOF
${PRODUCT_NAME} prompted installer

This will install ${PRODUCT_NAME} with:
  App code:     ${INSTALL_DIR}
  Config file:  ${CONFIG_FILE}
  CSV data:     ${DATA_DIR}
  Logs:         ${LOG_DIR}
  Service user: ${SERVICE_USER}
  Port:         ${DASHBOARD_PORT}

This installer sets up the platform only.

After install, sign in to the UI and add one or more portal environments under:
  Administration -> Portal Environments
EOF
}

prompt_value() {
  local label="$1"
  local default="${2:-}"
  local secret="${3:-0}"
  local required="${4:-0}"
  local value

  if [[ "${NONINTERACTIVE}" -eq 1 ]]; then
    printf '%s' "${default}"
    return
  fi

  while true; do
    if [[ "${secret}" -eq 1 ]]; then
      printf '%s: ' "${label}" >&2
      read -r -s value
      printf '\n' >&2
    elif [[ -n "${default}" ]]; then
      printf '%s [%s]: ' "${label}" "${default}" >&2
      read -r value
      value="${value:-${default}}"
    else
      printf '%s: ' "${label}" >&2
      read -r value
    fi
    if [[ "${required}" -eq 0 || -n "${value}" ]]; then
      break
    fi
    echo "  ${label} is required." >&2
  done
  printf '%s' "${value}"
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
      read -r answer
      answer="${answer:-Y}"
    else
      printf '%s [y/N]: ' "${label}" >&2
      read -r answer
      answer="${answer:-N}"
    fi
    case "${answer}" in
      y|Y|yes|YES) return 0 ;;
      n|N|no|NO) return 1 ;;
      *) echo "  Please answer yes or no." >&2 ;;
    esac
  done
}

prompt_positive_int() {
  local label="$1"
  local default="$2"
  local value

  if [[ "${NONINTERACTIVE}" -eq 1 ]]; then
    printf '%s' "${default}"
    return
  fi

  while true; do
    value="$(prompt_value "${label}" "${default}" 0 1)"
    if [[ "${value}" =~ ^[0-9]+$ && "${value}" -gt 0 ]]; then
      printf '%s' "${value}"
      return
    fi
    echo "  Enter a whole number greater than 0." >&2
  done
}

cron_every_minutes() {
  local minutes="$1"

  if [[ "${minutes}" -lt 60 ]]; then
    if (( 60 % minutes == 0 )); then
      printf '*/%s * * * *' "${minutes}"
    else
      echo "Collector minutes must divide evenly into 60, for example 5, 10, 15, 20, 30, or 60." >&2
      return 1
    fi
  elif (( minutes % 60 == 0 )); then
    local hours=$((minutes / 60))
    if [[ "${hours}" -eq 1 ]]; then
      printf '0 * * * *'
    else
      printf '0 */%s * * *' "${hours}"
    fi
  else
    echo "Collector minutes must be less than 60 or a whole number of hours." >&2
    return 1
  fi
}

cron_every_hours() {
  local hours="$1"

  if [[ "${hours}" -eq 1 ]]; then
    printf '0 * * * *'
  else
    printf '0 */%s * * *' "${hours}"
  fi
}

env_quote() {
  local value="$1"
  value="${value//\'/\'\\\'\'}"
  printf "'%s'" "${value}"
}

sh_quote() {
  local value="$1"
  value="${value//\'/\'\\\'\'}"
  printf "'%s'" "${value}"
}

setup_ssh_key() {
  local key_path="$1"
  local ssh_user="$2"
  local main_db_host="$3"
  local ssh_password="$4"
  local key_dir
  local pub_key

  key_dir="$(dirname "${key_path}")"
  mkdir -p "${key_dir}"

  if [[ ! -f "${key_path}" ]]; then
    section "Generating ${PRODUCT_NAME} SSH key"
    ssh-keygen -t ed25519 -N "" -C "${PRODUCT_SLUG}@$(hostname -f 2>/dev/null || hostname)" -f "${key_path}"
  else
    echo "Using existing SSH key: ${key_path}"
  fi

  chmod 700 "${key_dir}"
  chmod 600 "${key_path}"
  pub_key="$(cat "${key_path}.pub")"

  if [[ -n "${main_db_host//[[:space:]]/}" ]]; then
    section "Installing ${PRODUCT_NAME} public key on MainDB server"
    install_ssh_helper_packages
    echo "  Installing key on ${ssh_user}@${main_db_host}"
    sshpass -p "${ssh_password}" ssh -o StrictHostKeyChecking=accept-new -o ConnectTimeout=10 "${ssh_user}@${main_db_host}" \
      "mkdir -p ~/.ssh && chmod 700 ~/.ssh && grep -qxF '${pub_key}' ~/.ssh/authorized_keys 2>/dev/null || echo '${pub_key}' >> ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys"
  fi
}

remote_root_exec() {
  local main_db_host="$1"
  local ssh_user="$2"
  local key_path="$3"
  local ssh_password="$4"
  local sudo_password="$5"
  local use_sudo="$6"
  local remote_cmd="$7"
  local quoted_remote_cmd
  local quoted_password
  local sudo_cmd

  quoted_remote_cmd="$(sh_quote "${remote_cmd}")"

  if [[ "${use_sudo}" -eq 1 ]]; then
    if [[ -n "${sudo_password}" ]]; then
      quoted_password="$(sh_quote "${sudo_password}")"
      sudo_cmd="printf '%s\n' ${quoted_password} | sudo -S -p '' bash -lc ${quoted_remote_cmd}"
    else
      sudo_cmd="sudo -n bash -lc ${quoted_remote_cmd}"
    fi
  else
    sudo_cmd="bash -lc ${quoted_remote_cmd}"
  fi

  if [[ -n "${ssh_password}" ]]; then
    install_ssh_helper_packages >&2
    sshpass -p "${ssh_password}" ssh -o StrictHostKeyChecking=accept-new -o ConnectTimeout=10 "${ssh_user}@${main_db_host}" "${sudo_cmd}"
  else
    ssh -i "${key_path}" -o StrictHostKeyChecking=accept-new -o ConnectTimeout=10 "${ssh_user}@${main_db_host}" "${sudo_cmd}"
  fi
}

reveal_postgres_password() {
  local main_db_host="$1"
  local ssh_user="$2"
  local key_path="$3"
  local ssh_password="$4"
  local sudo_password="$5"
  local use_sudo="$6"
  local reveal_cmd
  local output

  if [[ -z "${main_db_host//[[:space:]]/}" ]]; then
    return 1
  fi

  section "Retrieving Postgres password from MainDB" >&2
  reveal_cmd="/usr/local/ctera/jdk/bin/java -cp '/usr/local/ctera/apache-tomcat/lib/portal/*:/usr/local/ctera/apache-tomcat/lib/common.jar' com.ctera.utils.password.PostgresPasswordTool \$(cat /etc/ctera/portal_key) \$(grep CTERA_LOCAL_POSTGRES_PASS /etc/ctera/portal.cfg | cut -d '=' -f2) reveal"
  output="$(remote_root_exec "${main_db_host}" "${ssh_user}" "${key_path}" "${ssh_password}" "${sudo_password}" "${use_sudo}" "${reveal_cmd}" 2>/dev/null || true)"

  output="$(printf '%s\n' "${output}" | tr -d '\r' | sed -n '/./p' | tail -n 1)"
  if [[ -z "${output}" ]]; then
    echo "  Could not retrieve the Postgres password automatically." >&2
    return 1
  fi

  printf '%s' "${output}"
}

replace_token() {
  local file="$1"
  local token="$2"
  local value="$3"
  local escaped
  escaped="$(printf '%s' "${value}" | sed 's/[\/&]/\\&/g')"
  sed -i "s|${token}|${escaped}|g" "${file}"
}

run_with_spinner() {
  local label="$1"
  local log_path="$2"
  shift 2
  local pid
  local spinner='|/-\'
  local i=0
  local rc

  echo "  Running ${label} collector..."
  echo "  Watch progress in another terminal with:"
  echo "    tail -F ${log_path}"

  "$@" >> "${log_path}" 2>&1 &
  pid=$!

  while kill -0 "${pid}" >/dev/null 2>&1; do
    i=$(( (i + 1) % 4 ))
    printf '\r  %s %s collector is still running...' "${spinner:$i:1}" "${label}"
    sleep 0.2
  done

  wait "${pid}" || rc=$?
  rc="${rc:-0}"
  printf '\r'
  if [[ "${rc}" -eq 0 ]]; then
    echo "  [done] ${label} collector finished."
  else
    echo "  [failed] ${label} collector failed. Check ${log_path}"
  fi
  return "${rc}"
}

print_banner
if [[ "${NONINTERACTIVE}" -eq 0 && "${SKIP_CONFIRM}" -eq 0 ]]; then
  echo
  read -r -p "Continue with install? [Y/n]: " CONFIRM
  case "${CONFIRM:-Y}" in
    y|Y|yes|YES) ;;
    *) echo "Install cancelled."; exit 0 ;;
  esac
fi

section "Installing OS packages"
install_base_packages

if ! id -u "${SERVICE_USER}" >/dev/null 2>&1; then
  section "Creating service user: ${SERVICE_USER}"
  useradd --system --home "${INSTALL_DIR}" --shell /usr/sbin/nologin "${SERVICE_USER}"
fi

if [[ -n "${ARCHIVE}" ]]; then
  if [[ ! -f "${ARCHIVE}" ]]; then
    echo "Archive not found: ${ARCHIVE}" >&2
    exit 1
  fi
  mkdir -p "$(dirname "${INSTALL_DIR}")"
  tar -xzf "${ARCHIVE}" -C "$(dirname "${INSTALL_DIR}")"
elif [[ "$(pwd)" != "${INSTALL_DIR}" ]]; then
  mkdir -p "${INSTALL_DIR}"
  cp -a . "${INSTALL_DIR}/"
fi

cd "${INSTALL_DIR}"
mkdir -p "${DATA_DIR}" "${DB_DIR}" "${LOG_DIR}"

section "Preparing centralized CSV data directory"
for csv_name in filer.csv tenants.csv servers.csv storage.csv tasks.csv task.csv server_metrics.csv; do
  if [[ -f "${INSTALL_DIR}/${csv_name}" && ! -f "${DATA_DIR}/${csv_name}" ]]; then
    mv "${INSTALL_DIR}/${csv_name}" "${DATA_DIR}/${csv_name}"
  fi
  if [[ -f "${INSTALL_DIR}/data/${csv_name}" && ! -f "${DATA_DIR}/${csv_name}" ]]; then
    mv "${INSTALL_DIR}/data/${csv_name}" "${DATA_DIR}/${csv_name}"
  fi
done
for source_dir in "${INSTALL_DIR}/db" "${INSTALL_DIR}/db.csv" "${INSTALL_DIR}/data/db"; do
  if [[ -d "${source_dir}" ]]; then
    for db_csv in "${source_dir}"/*.csv; do
      if [[ -f "${db_csv}" && ! -f "${DB_DIR}/$(basename "${db_csv}")" ]]; then
        mv "${db_csv}" "${DB_DIR}/"
      fi
    done
  fi
done

section "Creating Python virtualenv"
python3 -m venv "${INSTALL_DIR}/venv"
"${INSTALL_DIR}/venv/bin/python" -m pip install --upgrade pip
"${INSTALL_DIR}/venv/bin/pip" install -r "${INSTALL_DIR}/requirements.txt"
install_private_helper

CONFIGURE_ENV=1
if [[ -f "${CONFIG_FILE}" ]]; then
  section "Existing configuration found"
  if prompt_yes_no "Keep existing ${CONFIG_FILE} runtime settings?" "y"; then
    CONFIGURE_ENV=0
  else
    CONFIG_BACKUP="${CONFIG_FILE}.bak.$(date +%Y%m%d%H%M%S)"
    cp -p "${CONFIG_FILE}" "${CONFIG_BACKUP}"
    echo "  Backed up existing config to ${CONFIG_BACKUP}"
  fi
fi

if [[ "${CONFIGURE_ENV}" -eq 1 ]]; then
  section "Creating base runtime config"
  CTERA_HOST=""
  CTERA_USERNAME=""
  CTERA_PASSWORD=""
  PGHOST=""
  PGPORT="5432"
  PGDATABASE="postgres"
  PGUSER="postgres"
  PGPASSWORD=""
  SERVER_SSH_USER="root"
  ROOT_KEY="${INSTALL_DIR}/ssh/id_ed25519"
  OPENAI_API_KEY=""
  DASHBOARD_PORT="8080"

  DASHBOARD_PORT="$(prompt_positive_int "Dashboard port" "8080")"

  umask 077
  cat > "${CONFIG_FILE}" <<EOF
CTERA_HOST=$(env_quote "${CTERA_HOST}")
CTERA_USERNAME=$(env_quote "${CTERA_USERNAME}")
CTERA_PASSWORD=$(env_quote "${CTERA_PASSWORD}")
CTERA_VERIFY_SSL=false
PGHOST=$(env_quote "${PGHOST}")
PGPORT=$(env_quote "${PGPORT}")
PGDATABASE=$(env_quote "${PGDATABASE}")
PGUSER=$(env_quote "${PGUSER}")
PGPASSWORD=$(env_quote "${PGPASSWORD}")
SERVER_SSH_USER=$(env_quote "${SERVER_SSH_USER}")
ROOT_KEY=$(env_quote "${ROOT_KEY}")
SERVER_METRICS_MODE=jump
SERVER_METRICS_TARGET_USER=ctera
SERVER_METRICS_JUMP_HOST=$(env_quote "${PGHOST}")
SERVER_METRICS_JUMP_USER=$(env_quote "${SERVER_SSH_USER}")
SERVER_METRICS_JUMP_RUN_AS_USER=ctera
SERVER_METRICS_SUDO=true
OPENAI_API_KEY=$(env_quote "${OPENAI_API_KEY}")
PORT=${DASHBOARD_PORT}
FEATHERDASH_BIND_HOST=0.0.0.0
FEATHERDASH_DATA_DIR=${DATA_DIR}
FEATHERDASH_DB_DIR=${DB_DIR}
FEATHERDASH_THRESHOLDS=${INSTALL_DIR}/thresholds.yaml
PYTHONUNBUFFERED=1
EOF
else
  section "Keeping existing ${CONFIG_FILE}"
  if [[ -f "${CONFIG_FILE}" ]]; then
    existing_port="$(grep '^PORT=' "${CONFIG_FILE}" | tail -n1 | cut -d '=' -f2- | sed -e "s/^['\"]//" -e "s/['\"]$//" || true)"
    if [[ -n "${existing_port}" ]]; then
      DASHBOARD_PORT="${existing_port}"
    fi
  fi
fi
chmod 600 "${CONFIG_FILE}"

chmod +x "${INSTALL_DIR}"/*jobs.sh
chmod +x "${INSTALL_DIR}/scheduler_jobs.sh"
chmod +x "${INSTALL_DIR}/install_featherdash.sh"
touch "${LOG_DIR}/portal.log" "${LOG_DIR}/filer.log" "${LOG_DIR}/scheduler.log"
chown -R "${SERVICE_USER}:${SERVICE_USER}" "${INSTALL_DIR}" "${DATA_DIR}" "${LOG_DIR}"
chown root:"${SERVICE_USER}" "${CONFIG_FILE}"
chmod 640 "${CONFIG_FILE}"

if systemctl list-unit-files | grep -q "^${LEGACY_SERVICE_NAME}\.service"; then
  section "Stopping previous ${LEGACY_SERVICE_NAME} service"
  systemctl disable --now "${LEGACY_SERVICE_NAME}" || true
  rm -f "/etc/systemd/system/${LEGACY_SERVICE_NAME}.service"
fi
rm -f "/etc/cron.d/${LEGACY_SERVICE_NAME}" || true

section "Installing systemd service"
SERVICE_TMP="$(mktemp)"
cp "${INSTALL_DIR}/deploy/featherdash.service" "${SERVICE_TMP}"
replace_token "${SERVICE_TMP}" "__INSTALL_DIR__" "${INSTALL_DIR}"
replace_token "${SERVICE_TMP}" "__CONFIG_FILE__" "${CONFIG_FILE}"
replace_token "${SERVICE_TMP}" "__SERVICE_USER__" "${SERVICE_USER}"
replace_token "${SERVICE_TMP}" "__PRODUCT_NAME__" "${PRODUCT_NAME}"
cp "${SERVICE_TMP}" "/etc/systemd/system/${PRODUCT_SLUG}.service"
rm -f "${SERVICE_TMP}"
systemctl daemon-reload
systemctl enable --now "${PRODUCT_SLUG}"

install_upgrade_helper

section "Collector schedule"
SCHEDULER_CRON="*/5 * * * *"
echo "  The host scheduler wakes every 5 minutes."
echo "  Each enabled portal environment then runs only when its own saved interval is due."
echo "  Per-portal collector timing is managed in the UI under Administration -> Portals."

section "Installing cron collectors"
CRON_FILE="/etc/cron.d/${PRODUCT_SLUG}"
CRON_TMP="$(mktemp)"
cp "${INSTALL_DIR}/deploy/crontab.featherdash" "${CRON_TMP}"
replace_token "${CRON_TMP}" "__INSTALL_DIR__" "${INSTALL_DIR}"
replace_token "${CRON_TMP}" "__SERVICE_USER__" "${SERVICE_USER}"
replace_token "${CRON_TMP}" "__LOG_DIR__" "${LOG_DIR}"
cp "${CRON_TMP}" "${CRON_FILE}"
rm -f "${CRON_TMP}"
chmod 644 "${CRON_FILE}"
systemctl enable --now "${CRON_SERVICE_NAME}"

configure_local_firewall "${DASHBOARD_PORT}"

echo
echo "${PRODUCT_NAME} install complete."
echo
echo "Open the dashboard:"
echo "  http://<instance-ip>:${DASHBOARD_PORT}/"
echo
echo "Next step:"
echo "  Sign in to the dashboard and go to Administration -> Portal Environments"
echo "  to add your portal systems."
echo
echo "Health check:"
echo "  http://<instance-ip>:${DASHBOARD_PORT}/healthz"
echo
echo "App code:     ${INSTALL_DIR}"
echo "Admin config: ${CONFIG_FILE}"
echo "CSV data:     ${DATA_DIR}"
echo "Logs:         ${LOG_DIR}"
echo "Service user: ${SERVICE_USER}"
echo "Scheduler:    ${SCHEDULER_CRON}"
echo "Cron service: ${CRON_SERVICE_NAME}"
echo
echo "Useful checks:"
echo "  sudo systemctl status ${PRODUCT_SLUG} --no-pager"
echo "  curl -I http://127.0.0.1:${DASHBOARD_PORT}/healthz"
echo "  sudo journalctl -u ${PRODUCT_SLUG} -n 100 --no-pager"
echo "  sudo tail -F ${LOG_DIR}/portal.log"
echo "  sudo tail -F ${LOG_DIR}/filer.log"
echo "  sudo tail -F ${LOG_DIR}/scheduler.log"
