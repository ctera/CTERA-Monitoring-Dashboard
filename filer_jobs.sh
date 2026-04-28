#!/usr/bin/env bash
set -euo pipefail

timestamp_log() {
  while IFS= read -r line; do
    printf '%s %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "${line}"
  done
}

exec > >(timestamp_log) 2>&1

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FEATHERDASH_CONFIG="${FEATHERDASH_CONFIG:-/etc/ctera-monitoring-dashboard.env}"

echo "Starting filer_jobs.sh"

cd "${SCRIPT_DIR}"

set -a
source "${FEATHERDASH_CONFIG}"
set +a

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
