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
FEATHERDASH_STATE_DIR="${FEATHERDASH_STATE_DIR:-${SCRIPT_DIR}/state}"
JOB_STATE_PATH="${FEATHERDASH_STATE_DIR}/filer.state"
JOB_STARTED_AT="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

cd "${SCRIPT_DIR}"

set -a
source "${FEATHERDASH_CONFIG}"
set +a

FEATHERDASH_ENV_LABEL="${FEATHERDASH_ENV_NAME:-${CTERA_HOST:-filer}}"

exec > >(timestamp_log) 2>&1

mkdir -p "${FEATHERDASH_STATE_DIR}"

write_job_state() {
  local status="$1"
  local finished_at="$2"
  local last_exit="$3"
  local pid_value="${4:-}"
  local tmp="${JOB_STATE_PATH}.tmp"
  cat > "${tmp}" <<EOF
status=${status}
started_at=${JOB_STARTED_AT}
finished_at=${finished_at}
last_exit=${last_exit}
pid=${pid_value}
EOF
  mv "${tmp}" "${JOB_STATE_PATH}"
}

finish_job_state() {
  local rc=$?
  local final_status="finished"
  if [[ "${rc}" -ne 0 ]]; then
    final_status="failed"
  fi
  write_job_state "${final_status}" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "${rc}" ""
  return "${rc}"
}

write_job_state "running" "" "" "$$"
trap finish_job_state EXIT

echo "Starting filer_jobs.sh"

CTERA_HOST="${CTERA_HOST:-${PORTAL:-}}"
CTERA_USERNAME="${CTERA_USERNAME:-${CTERA_USER:-}}"
if [[ -z "${CTERA_USERNAME}" && -n "${USER:-}" && "${USER}" != "root" ]]; then
  CTERA_USERNAME="${USER}"
fi
FEATHERDASH_DATA_DIR="${FEATHERDASH_DATA_DIR:-/var/lib/ctera-monitoring-dashboard/data}"

require_var() {
  local name="$1"
  if [[ -z "${!name:-}" ]]; then
    echo "Missing required environment variable: ${name}" >&2
    exit 1
  fi
}

require_var CTERA_HOST
require_var CTERA_USERNAME
require_var CTERA_PASSWORD

mkdir -p "${FEATHERDASH_DATA_DIR}"

source "${SCRIPT_DIR}/venv/bin/activate"

# CTERA (no -p flag)
rm -f "${FEATHERDASH_DATA_DIR}/filer.csv"
python ctera_collect.py -H "${CTERA_HOST}" -u "${CTERA_USERNAME}" -p "${CTERA_PASSWORD}" --mode filers --all-tenants --global-admin -o "${FEATHERDASH_DATA_DIR}/filer.csv"

PORT="${PORT:-8080}"
if command -v curl >/dev/null 2>&1; then
  if curl -fsS -X POST "http://127.0.0.1:${PORT}/notifications_run" >/dev/null 2>&1; then
    echo "Notification alert check completed after filer_jobs.sh"
  else
    echo "Notification alert check skipped or failed after filer_jobs.sh"
  fi
fi

echo "Completed filer_jobs.sh"
