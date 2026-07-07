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

TUNNEL_SOCKET=""
TUNNEL_TARGET=""
JUMP_SOCKET=""
JUMP_TARGET=""
REMOTE_TUNNEL_PID=""
LOCAL_PGHOST="${PGHOST}"
LOCAL_PGPORT="${PGPORT}"
LOCAL_MAINDB_SSH_PORT=""

cleanup_tunnel() {
  if [[ -n "${REMOTE_TUNNEL_PID}" && -n "${JUMP_SOCKET}" && -n "${JUMP_TARGET}" ]]; then
    ssh -S "${JUMP_SOCKET}" "${JUMP_TARGET}" "kill ${REMOTE_TUNNEL_PID} >/dev/null 2>&1 || true" >/dev/null 2>&1 || true
  fi
  if [[ -n "${TUNNEL_SOCKET}" ]]; then
    ssh -S "${TUNNEL_SOCKET}" -O exit "${TUNNEL_TARGET}" >/dev/null 2>&1 || true
    rm -f "${TUNNEL_SOCKET}" >/dev/null 2>&1 || true
  fi
  if [[ -n "${JUMP_SOCKET}" ]]; then
    ssh -S "${JUMP_SOCKET}" -O exit "${JUMP_TARGET}" >/dev/null 2>&1 || true
    rm -f "${JUMP_SOCKET}" >/dev/null 2>&1 || true
  fi
}
trap cleanup_tunnel EXIT

choose_free_port() {
  python - <<'PY'
import socket
s = socket.socket()
s.bind(("127.0.0.1", 0))
print(s.getsockname()[1])
s.close()
PY
}

if [[ "${JUMP_HOST_ENABLED}" =~ ^(1|true|yes|on)$ ]]; then
  require_var JUMP_HOST
  require_var JUMP_SSH_USER
  require_var ROOT_KEY
  if [[ ! -r "${ROOT_KEY}" ]]; then
    echo "Jump-host runtime key is not readable: ${ROOT_KEY}" >&2
    exit 1
  fi
  LOCAL_PGPORT="$(choose_free_port)"
  LOCAL_MAINDB_SSH_PORT="$(choose_free_port)"
  if [[ "${MAINDB_VIA_JUMP_PRECONFIGURED}" =~ ^(1|true|yes|on)$ ]]; then
    require_var MAINDB_JUMP_USERNAME
    REMOTE_PGPORT="$(choose_free_port)"
    REMOTE_MAINDB_SSH_PORT="$(choose_free_port)"
    JUMP_SOCKET="/tmp/ctera-monitoring-dashboard-jump-${RANDOM}-${RANDOM}.sock"
    JUMP_TARGET="${JUMP_SSH_USER}@${JUMP_HOST}"
    ssh -M -S "${JUMP_SOCKET}" -fnNT \
      -o BatchMode=yes \
      -o ExitOnForwardFailure=yes \
      -o StrictHostKeyChecking=no \
      -o UserKnownHostsFile=/dev/null \
      -o IdentitiesOnly=yes \
      -p "${JUMP_SSH_PORT}" \
      -i "${ROOT_KEY}" \
      "${JUMP_TARGET}"

    REMOTE_BOOTSTRAP_LOG="/tmp/ctera-monitoring-main-db-hop-${RANDOM}-${RANDOM}.log"
    REMOTE_TUNNEL_PID="$(
      ssh -S "${JUMP_SOCKET}" "${JUMP_TARGET}" "bash -lc '
        log=${REMOTE_BOOTSTRAP_LOG@Q}
        inner_ssh=$(cat <<EOF
ssh -o BatchMode=yes -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
          -p ${SERVER_SSH_PORT} \
          -N \
          -L 127.0.0.1:${REMOTE_PGPORT}:127.0.0.1:${PGPORT} \
          -L 127.0.0.1:${REMOTE_MAINDB_SSH_PORT}:127.0.0.1:${SERVER_SSH_PORT} \
          ${MAINDB_JUMP_USERNAME}@${PGHOST}
EOF
)
        if [ ${MAINDB_JUMP_USERNAME@Q} = root ]; then
          nohup bash -lc \"\$inner_ssh\" >\"\$log\" 2>&1 < /dev/null &
        elif command -v sudo >/dev/null 2>&1; then
          nohup sudo -n -u ${MAINDB_JUMP_USERNAME@Q} bash -lc \"\$inner_ssh\" >\"\$log\" 2>&1 < /dev/null &
        else
          nohup su - ${MAINDB_JUMP_USERNAME@Q} -c \"\$inner_ssh\" >\"\$log\" 2>&1 < /dev/null &
        fi
        pid=\$!
        for _ in \$(seq 1 20); do
          if command -v ss >/dev/null 2>&1; then
            ss -ltn | grep -q \":${REMOTE_PGPORT} \" && ss -ltn | grep -q \":${REMOTE_MAINDB_SSH_PORT} \" && { echo \$pid; exit 0; }
          else
            netstat -ltn 2>/dev/null | grep -q \":${REMOTE_PGPORT} \" && netstat -ltn 2>/dev/null | grep -q \":${REMOTE_MAINDB_SSH_PORT} \" && { echo \$pid; exit 0; }
          fi
          sleep 1
        done
        cat \"\$log\" >&2 || true
        kill \$pid >/dev/null 2>&1 || true
        exit 1
      '" | tr -d '\r'
    )"
    if [[ -z "${REMOTE_TUNNEL_PID}" ]]; then
      echo "Could not establish the jump host's preconfigured SSH hop to MainDB." >&2
      exit 1
    fi

    TUNNEL_SOCKET="/tmp/ctera-monitoring-dashboard-local-${RANDOM}-${RANDOM}.sock"
    TUNNEL_TARGET="${JUMP_TARGET}"
    ssh -M -S "${TUNNEL_SOCKET}" -fnNT \
      -o BatchMode=yes \
      -o ExitOnForwardFailure=yes \
      -o StrictHostKeyChecking=no \
      -o UserKnownHostsFile=/dev/null \
      -o IdentitiesOnly=yes \
      -p "${JUMP_SSH_PORT}" \
      -i "${ROOT_KEY}" \
      -L "127.0.0.1:${LOCAL_PGPORT}:127.0.0.1:${REMOTE_PGPORT}" \
      -L "127.0.0.1:${LOCAL_MAINDB_SSH_PORT}:127.0.0.1:${REMOTE_MAINDB_SSH_PORT}" \
      "${TUNNEL_TARGET}"
  else
    TUNNEL_SOCKET="/tmp/ctera-monitoring-dashboard-${RANDOM}-${RANDOM}.sock"
    TUNNEL_TARGET="${SERVER_SSH_USER}@${PGHOST}"
    PROXY_CMD="ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o IdentitiesOnly=yes -i ${ROOT_KEY} -p ${JUMP_SSH_PORT} -W %h:%p ${JUMP_SSH_USER}@${JUMP_HOST}"
    ssh -M -S "${TUNNEL_SOCKET}" -fnNT \
      -o BatchMode=yes \
      -o ExitOnForwardFailure=yes \
      -o StrictHostKeyChecking=no \
      -o UserKnownHostsFile=/dev/null \
      -o IdentitiesOnly=yes \
      -o ProxyCommand="${PROXY_CMD}" \
      -p "${SERVER_SSH_PORT}" \
      -i "${ROOT_KEY}" \
      -L "127.0.0.1:${LOCAL_PGPORT}:127.0.0.1:${PGPORT}" \
      -L "127.0.0.1:${LOCAL_MAINDB_SSH_PORT}:127.0.0.1:${SERVER_SSH_PORT}" \
      "${TUNNEL_TARGET}"
  fi
  LOCAL_PGHOST="127.0.0.1"
  SERVER_METRICS_JUMP_HOST="127.0.0.1"
  SERVER_METRICS_JUMP_USER="${SERVER_SSH_USER}"
fi

rm -f "${FEATHERDASH_DATA_DIR}/storage.csv"
python ctera_collect.py -H "${CTERA_HOST}" -u "${CTERA_USERNAME}" -p "${CTERA_PASSWORD}" --mode storage --global-admin -o "${FEATHERDASH_DATA_DIR}/storage.csv"

rm -f "${FEATHERDASH_DATA_DIR}/servers.csv"
python ctera_collect.py -H "${CTERA_HOST}" -u "${CTERA_USERNAME}" -p "${CTERA_PASSWORD}" --mode servers --global-admin -o "${FEATHERDASH_DATA_DIR}/servers.csv"

rm -f "${FEATHERDASH_DATA_DIR}/tasks.csv"
python ctera_collect.py -H "${CTERA_HOST}" -u "${CTERA_USERNAME}" -p "${CTERA_PASSWORD}" --mode tasks --global-admin -o "${FEATHERDASH_DATA_DIR}/tasks.csv"

rm -f "${FEATHERDASH_DB_DIR}"/*
python pg_healthcheck.py --host "${LOCAL_PGHOST}" --port "${LOCAL_PGPORT}" --dbname "${PGDATABASE}" --user "${PGUSER}" --password "${PGPASSWORD}" --min-age-seconds 60 --format csv --outdir "${FEATHERDASH_DB_DIR}" --bloat-method community

if [[ -n "${ROOT_KEY:-}" && -r "${ROOT_KEY}" ]]; then
  rm -f "${FEATHERDASH_DATA_DIR}/server_metrics.csv"
  SSH_METRICS_ARGS=(
    --pg-host "${LOCAL_PGHOST}"
    --pg-port "${LOCAL_PGPORT}"
    --pg-db "${PGDATABASE}"
    --pg-user "${PGUSER}"
    --pg-password "${PGPASSWORD}"
    --out "${FEATHERDASH_DATA_DIR}/server_metrics.csv"
    --nomad-out "${FEATHERDASH_DATA_DIR}/nomad_nodes.csv"
    --consul-out "${FEATHERDASH_DATA_DIR}/consul_members.csv"
    --docker-out "${FEATHERDASH_DATA_DIR}/docker_containers.csv"
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
        --jump-port "${LOCAL_MAINDB_SSH_PORT:-22}"
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

rm -f "${FEATHERDASH_DATA_DIR}/tenants.csv"
python pg_collect_tenants.py --pg-host "${LOCAL_PGHOST}" --pg-port "${LOCAL_PGPORT}" --pg-db "${PGDATABASE}" --pg-user "${PGUSER}" --pg-password "${PGPASSWORD}" --out "${FEATHERDASH_DATA_DIR}/tenants.csv"

PORT="${PORT:-8080}"
if command -v curl >/dev/null 2>&1; then
  if curl -fsS -X POST "http://127.0.0.1:${PORT}/notifications_run" >/dev/null 2>&1; then
    echo "Notification alert check completed after portal_jobs.sh"
  else
    echo "Notification alert check skipped or failed after portal_jobs.sh"
  fi
fi

echo "Completed portal_jobs.sh"
