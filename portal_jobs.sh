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

echo "Starting portal_jobs.sh"

CTERA_HOST="${CTERA_HOST:-${PORTAL:-}}"
CTERA_USERNAME="${CTERA_USERNAME:-${CTERA_USER:-}}"
if [[ -z "${CTERA_USERNAME}" && -n "${USER:-}" && "${USER}" != "root" ]]; then
  CTERA_USERNAME="${USER}"
fi
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
FEATHERDASH_DB_DIR="${FEATHERDASH_DB_DIR:-${FEATHERDASH_DATA_DIR}/db}"

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
require_var PGHOST
require_var PGPASSWORD

mkdir -p "${FEATHERDASH_DATA_DIR}" "${FEATHERDASH_DB_DIR}"

source "${SCRIPT_DIR}/venv/bin/activate"

source "${SCRIPT_DIR}/portal_maindb_tunnel.sh"
trap cleanup_portal_maindb_tunnel EXIT
setup_portal_maindb_tunnel || exit 1

# Write to a temp file and only replace the live CSV on success so a failed
# collect cannot blank dashboard freshness (rm-then-fail left "-" forever).
collect_replace() {
  local dest="$1"
  local out_flag="$2"
  shift 2
  local tmp="${dest}.tmp.$$"
  local rc=0
  rm -f "${tmp}"
  set +e
  "$@" "${out_flag}" "${tmp}"
  rc=$?
  set -e
  if [[ "${rc}" -eq 0 ]]; then
    mv -f "${tmp}" "${dest}"
    return 0
  fi
  rm -f "${tmp}"
  echo "WARNING: collect failed for ${dest}; keeping previous file if present (exit ${rc})" >&2
  return "${rc}"
}

collect_replace "${FEATHERDASH_DATA_DIR}/storage.csv" -o \
  python ctera_collect.py -H "${CTERA_HOST}" -u "${CTERA_USERNAME}" -p "${CTERA_PASSWORD}" --mode storage --global-admin

collect_replace "${FEATHERDASH_DATA_DIR}/servers.csv" -o \
  python ctera_collect.py -H "${CTERA_HOST}" -u "${CTERA_USERNAME}" -p "${CTERA_PASSWORD}" --mode servers --global-admin

collect_replace "${FEATHERDASH_DATA_DIR}/tasks.csv" -o \
  python ctera_collect.py -H "${CTERA_HOST}" -u "${CTERA_USERNAME}" -p "${CTERA_PASSWORD}" --mode tasks --global-admin

rm -f "${FEATHERDASH_DB_DIR}"/*
python pg_healthcheck.py --host "${LOCAL_PGHOST}" --port "${LOCAL_PGPORT}" --dbname "${PGDATABASE}" --user "${PGUSER}" --password "${PGPASSWORD}" --sslmode "${PG_SSLMODE}" --min-age-seconds 60 --format csv --outdir "${FEATHERDASH_DB_DIR}" --bloat-method community

if [[ -n "${ROOT_KEY:-}" && -r "${ROOT_KEY}" ]]; then
  rm -f "${FEATHERDASH_DATA_DIR}/server_metrics.csv"
  SSH_METRICS_ARGS=(
    --pg-host "${LOCAL_PGHOST}"
    --pg-port "${LOCAL_PGPORT}"
    --pg-db "${PGDATABASE}"
    --pg-user "${PGUSER}"
    --pg-password "${PGPASSWORD}"
    --pg-sslmode "${PG_SSLMODE}"
    --out "${FEATHERDASH_DATA_DIR}/server_metrics.csv"
    --nomad-out "${FEATHERDASH_DATA_DIR}/nomad_nodes.csv"
    --consul-out "${FEATHERDASH_DATA_DIR}/consul_members.csv"
    --docker-out "${FEATHERDASH_DATA_DIR}/docker_containers.csv"
    --replication-out "${FEATHERDASH_DB_DIR}/replication_status.csv"
    --snapshots-out "${FEATHERDASH_DB_DIR}/snapshots.csv"
  )
  if [[ "${SERVER_METRICS_MODE}" == "jump" ]]; then
    if [[ "${JUMP_HOST_ENABLED}" =~ ^(1|true|yes|on)$ && "${MAINDB_VIA_JUMP_PRECONFIGURED}" =~ ^(1|true|yes|on)$ ]]; then
      SSH_METRICS_ARGS+=(
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
      SSH_METRICS_ARGS+=(
        --user "${SERVER_METRICS_TARGET_USER}"
        --key "${ROOT_KEY}"
        --jump-host "${SERVER_METRICS_JUMP_HOST}"
        --jump-port "${SERVER_METRICS_JUMP_CONNECT_PORT:-${LOCAL_MAINDB_SSH_PORT:-22}}"
        --jump-user "${SERVER_METRICS_JUMP_USER}"
        --jump-key "${ROOT_KEY}"
      )
    fi
    if [[ -n "${SERVER_METRICS_JUMP_RUN_AS_USER}" ]]; then
      SSH_METRICS_ARGS+=(--jump-run-as-user "${SERVER_METRICS_JUMP_RUN_AS_USER}")
    fi
  else
    SSH_METRICS_ARGS+=(--user "${SERVER_SSH_USER}" --port "${SERVER_SSH_PORT}" --key "${ROOT_KEY}")
  fi
  if [[ "${SERVER_METRICS_SUDO}" =~ ^(1|true|yes|on)$ ]]; then
    SSH_METRICS_ARGS+=(--sudo)
  fi
  python ssh_collect_from_pg.py "${SSH_METRICS_ARGS[@]}"
else
  echo "Skipping server_metrics.csv: ROOT_KEY is not set or not readable (${ROOT_KEY:-unset}). Configure ROOT_KEY in ${FEATHERDASH_CONFIG} to enable SSH server metrics." >&2
  cat > "${FEATHERDASH_DATA_DIR}/server_metrics.csv" <<EOF
Name,Host,Status,UID,Connected,MainDB,RunningVersion,PublicIP,UptimeSeconds,Load1,Load5,Load15,MemTotalGB,MemUsedGB,MemUsedPct,RootDiskSizeGB,RootDiskUsedGB,RootDiskUsedPct,DataPoolSizeGB,DataPoolUsedGB,DataPoolUsedPct,DBArchivePoolSizeGB,DBArchivePoolUsedGB,DBArchivePoolUsedPct,CPUUserPct,CPUSystemPct,CPUIOWaitPct,CPUIDLEPct
SSH key missing,,ROOT_KEY is not set or not readable: ${ROOT_KEY:-unset},,,,,,,,,,,,,,,,,,,,,,,,,
EOF
  cat > "${FEATHERDASH_DATA_DIR}/docker_containers.csv" <<EOF
SourceName,SourceHost,SourceUID,HostUptimeSeconds,RecentlyBooted,GraceState,ContainerID,ContainerName,Image,State,Health,RestartCount,RestartDelta,RestartPolicy,StartedAt,FinishedAt,StatusText,CollectionError
SSH key missing,,,,,,,,"",ERROR,,,,,,,ROOT_KEY is not set or not readable: ${ROOT_KEY:-unset}
EOF
fi

collect_replace "${FEATHERDASH_DATA_DIR}/tenants.csv" --out \
  python pg_collect_tenants.py --pg-host "${LOCAL_PGHOST}" --pg-port "${LOCAL_PGPORT}" --pg-db "${PGDATABASE}" --pg-user "${PGUSER}" --pg-password "${PGPASSWORD}" --pg-sslmode "${PG_SSLMODE}"

PORT="${PORT:-8080}"
if command -v curl >/dev/null 2>&1; then
  if curl -fsS -X POST "http://127.0.0.1:${PORT}/notifications_run" >/dev/null 2>&1; then
    echo "Notification alert check completed after portal_jobs.sh"
  else
    echo "Notification alert check skipped or failed after portal_jobs.sh"
  fi
fi

echo "Completed portal_jobs.sh"
