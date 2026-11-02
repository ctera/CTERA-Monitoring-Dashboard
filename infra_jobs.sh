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
SERVER_METRICS_SUDO="${SERVER_METRICS_SUDO:-true}"
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
  "$@" --out "${tmp}"
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

PROBE_ARGS=(
  --maindb-label "${MAINDB_LABEL}"
  --maindb-host "${PGHOST}"
  --pg-user "${PGUSER}"
  --pg-password "${PGPASSWORD}"
  --ssh-key "${ROOT_KEY:-}"
  --timeout 10
)

if [[ "${SERVER_METRICS_SUDO}" =~ ^(1|true|yes|on)$ ]]; then
  PROBE_ARGS+=(--ssh-sudo)
fi

build_probe_ssh_args() {
  if [[ "${JUMP_HOST_ENABLED}" =~ ^(1|true|yes|on)$ && "${MAINDB_VIA_JUMP_PRECONFIGURED}" =~ ^(1|true|yes|on)$ ]]; then
    PROBE_ARGS+=(
      --ssh-mode via-jump
      --jump-host "${JUMP_HOST}"
      --jump-port "${JUMP_SSH_PORT}"
      --jump-user "${JUMP_SSH_USER}"
      --maindb-host "${PGHOST}"
      --maindb-port "${SERVER_SSH_PORT}"
      --maindb-ssh-user "${MAINDB_JUMP_USERNAME}"
      --jump-run-as-user "${SERVER_METRICS_JUMP_RUN_AS_USER:-}"
    )
  else
    PROBE_ARGS+=(
      --ssh-mode local-forward
      --ssh-jump-host "${SERVER_METRICS_JUMP_HOST}"
      --ssh-jump-port "${LOCAL_MAINDB_SSH_PORT}"
      --ssh-jump-user "${SERVER_METRICS_JUMP_USER}"
    )
  fi
}

source "${SCRIPT_DIR}/portal_maindb_tunnel.sh"
trap cleanup_portal_maindb_tunnel EXIT

TUNNEL_ERR=""
if ! setup_portal_maindb_tunnel; then
  TUNNEL_ERR="MainDB SSH tunnel setup failed (see infra.log)."
  echo "${TUNNEL_ERR}" >&2
fi

if [[ -n "${TUNNEL_ERR}" ]]; then
  collect_replace "${INFRA_CSV}" \
    python "${SCRIPT_DIR}/infra_probe.py" \
    "${PROBE_ARGS[@]}" \
    --tunnel-error "${TUNNEL_ERR}"
else
  build_probe_ssh_args
  collect_replace "${INFRA_CSV}" \
    python "${SCRIPT_DIR}/infra_probe.py" \
    "${PROBE_ARGS[@]}" \
    --pg-host "${LOCAL_PGHOST}" \
    --pg-port "${LOCAL_PGPORT}" \
    --pg-db "${PGDATABASE}" \
    --pg-sslmode "${PG_SSLMODE}"
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
