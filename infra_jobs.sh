#!/usr/bin/env bash
set -euo pipefail

timestamp_log() {
  while IFS= read -r line; do
    local prefix=""
    if [[ -n "${FEATHERDASH_ENV_LABEL:-}" ]]; then
      prefix="[${FEATHERDASH_ENV_LABEL}] "
    fi
    printf '%s %s%s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "${prefix}" "${line}"
  done
}

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FEATHERDASH_CONFIG="${FEATHERDASH_CONFIG:-/etc/ctera-monitoring-dashboard.env}"

cd "${SCRIPT_DIR}"

set -a
source "${FEATHERDASH_CONFIG}"
set +a

FEATHERDASH_ENV_LABEL="${FEATHERDASH_ENV_NAME:-${CTERA_HOST:-portal}}"

exec > >(timestamp_log) 2>&1

echo "Starting infra_jobs.sh"

PGHOST="${PGHOST:-${PGIP:-}}"
PGPORT="${PGPORT:-5432}"
PGDATABASE="${PGDATABASE:-postgres}"
PGUSER="${PGUSER:-postgres}"
SERVER_SSH_USER="${SERVER_SSH_USER:-root}"
SERVER_SSH_PORT="${SERVER_SSH_PORT:-22}"
SERVER_METRICS_MODE="${SERVER_METRICS_MODE:-jump}"
SERVER_METRICS_TARGET_USER="${SERVER_METRICS_TARGET_USER:-ctera}"
SERVER_METRICS_JUMP_HOST="${SERVER_METRICS_JUMP_HOST:-${PGHOST}}"
SERVER_METRICS_JUMP_USER="${SERVER_METRICS_JUMP_USER:-${SERVER_SSH_USER}}"
SERVER_METRICS_JUMP_RUN_AS_USER="${SERVER_METRICS_JUMP_RUN_AS_USER:-ctera}"
SERVER_METRICS_SUDO="${SERVER_METRICS_SUDO:-true}"
JUMP_HOST_ENABLED="${JUMP_HOST_ENABLED:-false}"
JUMP_HOST="${JUMP_HOST:-}"
JUMP_SSH_USER="${JUMP_SSH_USER:-root}"
JUMP_SSH_PORT="${JUMP_SSH_PORT:-22}"
MAINDB_VIA_JUMP_PRECONFIGURED="${MAINDB_VIA_JUMP_PRECONFIGURED:-false}"
MAINDB_JUMP_USERNAME="${MAINDB_JUMP_USERNAME:-${SERVER_SSH_USER}}"
FEATHERDASH_DATA_DIR="${FEATHERDASH_DATA_DIR:-/var/lib/ctera-monitoring-dashboard/data}"
INFRA_CSV="${FEATHERDASH_DATA_DIR}/infrastructure.csv"
MAINDB_LABEL="${PGHOST:-MainDB}"

require_var() {
  local name="$1"
  if [[ -z "${!name:-}" ]]; then
    echo "Missing required environment variable: ${name}" >&2
    exit 1
  fi
}

require_var PGHOST
require_var PGPASSWORD

mkdir -p "${FEATHERDASH_DATA_DIR}"

source "${SCRIPT_DIR}/venv/bin/activate"

collect_replace() {
  local dest="$1"
  shift
  local tmp="${dest}.tmp.$$"
  local rc=0
  rm -f "${tmp}"
  set +e
  "$@" --infra-out "${tmp}"
  rc=$?
  set -e
  if [[ "${rc}" -eq 0 ]]; then
    mv -f "${tmp}" "${dest}"
    return 0
  fi
  rm -f "${tmp}"
  echo "WARNING: infra probe failed for ${dest}; keeping previous file if present (exit ${rc})" >&2
  return "${rc}"
}

source "${SCRIPT_DIR}/portal_maindb_tunnel.sh"
trap cleanup_portal_maindb_tunnel EXIT

TUNNEL_ERR=""
if ! setup_portal_maindb_tunnel; then
  TUNNEL_ERR="MainDB SSH tunnel setup failed (see infra.log)."
  echo "${TUNNEL_ERR}" >&2
fi

INFRA_ARGS=(
  --pg-host "${LOCAL_PGHOST:-${PGHOST}}"
  --pg-port "${LOCAL_PGPORT:-${PGPORT}}"
  --pg-db "${PGDATABASE}"
  --pg-user "${PGUSER}"
  --pg-password "${PGPASSWORD}"
  --pg-sslmode "${PG_SSLMODE:-disable}"
  --infra-maindb-label "${MAINDB_LABEL}"
  --infra-maindb-host "${PGHOST}"
  --only-connected
  --ssh-timeout 10
)

if [[ -n "${TUNNEL_ERR}" ]]; then
  INFRA_ARGS+=(--tunnel-error "${TUNNEL_ERR}")
fi

if [[ "${SERVER_METRICS_MODE}" == "jump" && -n "${ROOT_KEY:-}" && -r "${ROOT_KEY}" ]]; then
  if [[ -z "${LOCAL_MAINDB_SSH_PORT:-}" ]]; then
    INFRA_ARGS+=(--tunnel-error "MainDB SSH local forward missing after tunnel setup (check MainDB SSH port ${SERVER_SSH_PORT} and key access).")
  fi
fi

if [[ -n "${ROOT_KEY:-}" && -r "${ROOT_KEY}" ]]; then
  if [[ "${SERVER_METRICS_MODE}" == "jump" ]]; then
    if [[ "${JUMP_HOST_ENABLED}" =~ ^(1|true|yes|on)$ && "${MAINDB_VIA_JUMP_PRECONFIGURED}" =~ ^(1|true|yes|on)$ ]]; then
      INFRA_ARGS+=(
        --user "${SERVER_METRICS_TARGET_USER}"
        --key "${ROOT_KEY}"
        --jump-host "${JUMP_HOST}"
        --jump-port "${JUMP_SSH_PORT}"
        --jump-user "${JUMP_SSH_USER}"
        --jump-key "${ROOT_KEY}"
        --via-main-db-host "${PGHOST}"
        --via-main-db-port "${SERVER_SSH_PORT}"
        --via-main-db-user "${MAINDB_JUMP_USERNAME}"
      )
    else
      INFRA_ARGS+=(
        --user "${SERVER_METRICS_TARGET_USER}"
        --key "${ROOT_KEY}"
        --jump-host "${SERVER_METRICS_JUMP_HOST}"
        --jump-port "${LOCAL_MAINDB_SSH_PORT:-22}"
        --jump-user "${SERVER_METRICS_JUMP_USER}"
        --jump-key "${ROOT_KEY}"
      )
    fi
    if [[ -n "${SERVER_METRICS_JUMP_RUN_AS_USER}" ]]; then
      INFRA_ARGS+=(--jump-run-as-user "${SERVER_METRICS_JUMP_RUN_AS_USER}")
    fi
  else
    INFRA_ARGS+=(--user "${SERVER_SSH_USER}" --port "${SERVER_SSH_PORT}" --key "${ROOT_KEY}")
  fi
  if [[ "${SERVER_METRICS_SUDO}" =~ ^(1|true|yes|on)$ ]]; then
    INFRA_ARGS+=(--sudo)
  fi
  collect_replace "${INFRA_CSV}" \
    python "${SCRIPT_DIR}/ssh_collect_from_pg.py" "${INFRA_ARGS[@]}"
else
  echo "ROOT_KEY missing; writing infrastructure failure rows." >&2
  collect_replace "${INFRA_CSV}" \
    python "${SCRIPT_DIR}/ssh_collect_from_pg.py" \
    "${INFRA_ARGS[@]}" \
    --tunnel-error "ROOT_KEY is not set or not readable (${ROOT_KEY:-unset})"
fi

PORT="${PORT:-8080}"
if command -v curl >/dev/null 2>&1; then
  if curl -fsS -X POST "http://127.0.0.1:${PORT}/notifications_run" >/dev/null 2>&1; then
    echo "Notification alert check completed after infra_jobs.sh"
  else
    echo "Notification alert check skipped or failed after infra_jobs.sh"
  fi
fi

echo "Completed infra_jobs.sh"
