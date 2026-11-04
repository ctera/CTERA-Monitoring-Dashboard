#!/usr/bin/env bash
# Shared SSH tunnel to MainDB Postgres (+ local forward for MainDB SSH).
# Source from portal_jobs.sh / infra_jobs.sh after loading runtime env.

PORTAL_TUNNEL_SOCKET=""
PORTAL_TUNNEL_TARGET=""
PORTAL_JUMP_SOCKET=""
PORTAL_JUMP_TARGET=""
PORTAL_REMOTE_TUNNEL_PID=""
LOCAL_PGHOST="${PGHOST:-}"
LOCAL_PGPORT="${PGPORT:-5432}"
LOCAL_MAINDB_SSH_PORT=""
SERVER_METRICS_JUMP_HOST="${SERVER_METRICS_JUMP_HOST:-}"
SERVER_METRICS_JUMP_USER="${SERVER_METRICS_JUMP_USER:-}"

cleanup_portal_maindb_tunnel() {
  if [[ -n "${PORTAL_REMOTE_TUNNEL_PID}" && -n "${PORTAL_JUMP_SOCKET}" && -n "${PORTAL_JUMP_TARGET}" ]]; then
    ssh -S "${PORTAL_JUMP_SOCKET}" "${PORTAL_JUMP_TARGET}" "kill ${PORTAL_REMOTE_TUNNEL_PID} >/dev/null 2>&1 || true" >/dev/null 2>&1 || true
  fi
  if [[ -n "${PORTAL_TUNNEL_SOCKET}" ]]; then
    ssh -S "${PORTAL_TUNNEL_SOCKET}" -O exit "${PORTAL_TUNNEL_TARGET}" >/dev/null 2>&1 || true
    rm -f "${PORTAL_TUNNEL_SOCKET}" >/dev/null 2>&1 || true
  fi
  if [[ -n "${PORTAL_JUMP_SOCKET}" ]]; then
    ssh -S "${PORTAL_JUMP_SOCKET}" -O exit "${PORTAL_JUMP_TARGET}" >/dev/null 2>&1 || true
    rm -f "${PORTAL_JUMP_SOCKET}" >/dev/null 2>&1 || true
  fi
}

_portal_choose_free_port() {
  python - <<'PY'
import socket
s = socket.socket()
s.bind(("127.0.0.1", 0))
print(s.getsockname()[1])
s.close()
PY
}

setup_portal_maindb_tunnel() {
  LOCAL_PGHOST="${PGHOST}"
  LOCAL_PGPORT="${PGPORT}"
  LOCAL_MAINDB_SSH_PORT=""
  unset SERVER_METRICS_JUMP_CONNECT_PORT

  if [[ "${JUMP_HOST_ENABLED}" =~ ^(1|true|yes|on)$ ]]; then
    require_var JUMP_HOST
    require_var JUMP_SSH_USER
    require_var ROOT_KEY
    if [[ ! -r "${ROOT_KEY}" ]]; then
      echo "Jump-host runtime key is not readable: ${ROOT_KEY}" >&2
      return 1
    fi
    LOCAL_PGPORT="$(_portal_choose_free_port)"
    LOCAL_MAINDB_SSH_PORT="$(_portal_choose_free_port)"
    if [[ "${MAINDB_VIA_JUMP_PRECONFIGURED}" =~ ^(1|true|yes|on)$ ]]; then
      require_var MAINDB_JUMP_USERNAME
      local REMOTE_PGPORT REMOTE_MAINDB_SSH_PORT REMOTE_BOOTSTRAP_LOG
      REMOTE_PGPORT="$(_portal_choose_free_port)"
      REMOTE_MAINDB_SSH_PORT="$(_portal_choose_free_port)"
      PORTAL_JUMP_SOCKET="/tmp/ctera-monitoring-dashboard-jump-${RANDOM}-${RANDOM}.sock"
      PORTAL_JUMP_TARGET="${JUMP_SSH_USER}@${JUMP_HOST}"
      ssh -M -S "${PORTAL_JUMP_SOCKET}" -fnNT \
        -o BatchMode=yes \
        -o ExitOnForwardFailure=yes \
        -o StrictHostKeyChecking=no \
        -o UserKnownHostsFile=/dev/null \
        -o IdentitiesOnly=yes \
        -p "${JUMP_SSH_PORT}" \
        -i "${ROOT_KEY}" \
        "${PORTAL_JUMP_TARGET}" || return 1

      REMOTE_BOOTSTRAP_LOG="/tmp/ctera-monitoring-main-db-hop-${RANDOM}-${RANDOM}.log"
      PORTAL_REMOTE_TUNNEL_PID="$(
        ssh -S "${PORTAL_JUMP_SOCKET}" "${PORTAL_JUMP_TARGET}" "bash -lc '
          log=${REMOTE_BOOTSTRAP_LOG@Q}
          inner_ssh=\"ssh -o BatchMode=yes -o PreferredAuthentications=publickey -o PasswordAuthentication=no -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -p ${SERVER_SSH_PORT@Q} -N -L 127.0.0.1:${REMOTE_PGPORT}:127.0.0.1:${PGPORT} -L 127.0.0.1:${REMOTE_MAINDB_SSH_PORT}:127.0.0.1:${SERVER_SSH_PORT} ${MAINDB_JUMP_USERNAME}@${PGHOST}\"
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
      if [[ -z "${PORTAL_REMOTE_TUNNEL_PID}" ]]; then
        echo "Could not establish the jump host's preconfigured SSH hop to MainDB." >&2
        return 1
      fi

      PORTAL_TUNNEL_SOCKET="/tmp/ctera-monitoring-dashboard-local-${RANDOM}-${RANDOM}.sock"
      PORTAL_TUNNEL_TARGET="${PORTAL_JUMP_TARGET}"
      ssh -M -S "${PORTAL_TUNNEL_SOCKET}" -fnNT \
        -o BatchMode=yes \
        -o ExitOnForwardFailure=yes \
        -o StrictHostKeyChecking=no \
        -o UserKnownHostsFile=/dev/null \
        -o IdentitiesOnly=yes \
        -p "${JUMP_SSH_PORT}" \
        -i "${ROOT_KEY}" \
        -L "127.0.0.1:${LOCAL_PGPORT}:127.0.0.1:${REMOTE_PGPORT}" \
        -L "127.0.0.1:${LOCAL_MAINDB_SSH_PORT}:127.0.0.1:${REMOTE_MAINDB_SSH_PORT}" \
        "${PORTAL_TUNNEL_TARGET}" || return 1
    else
      PORTAL_TUNNEL_SOCKET="/tmp/ctera-monitoring-dashboard-${RANDOM}-${RANDOM}.sock"
      PORTAL_TUNNEL_TARGET="${SERVER_SSH_USER}@${PGHOST}"
      local PROXY_CMD
      PROXY_CMD="ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o IdentitiesOnly=yes -i ${ROOT_KEY} -p ${JUMP_SSH_PORT} -W %h:%p ${JUMP_SSH_USER}@${JUMP_HOST}"
      ssh -M -S "${PORTAL_TUNNEL_SOCKET}" -fnNT \
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
        "${PORTAL_TUNNEL_TARGET}" || return 1
    fi
    LOCAL_PGHOST="127.0.0.1"
    SERVER_METRICS_JUMP_HOST="127.0.0.1"
    SERVER_METRICS_JUMP_USER="${SERVER_SSH_USER}"
  elif [[ -n "${ROOT_KEY:-}" && -r "${ROOT_KEY}" ]]; then
    LOCAL_PGPORT="$(_portal_choose_free_port)"
    LOCAL_MAINDB_SSH_PORT="$(_portal_choose_free_port)"
    PORTAL_TUNNEL_SOCKET="/tmp/ctera-monitoring-dashboard-${RANDOM}-${RANDOM}.sock"
    PORTAL_TUNNEL_TARGET="${SERVER_SSH_USER}@${PGHOST}"
    ssh -M -S "${PORTAL_TUNNEL_SOCKET}" -fnNT \
      -o BatchMode=yes \
      -o ExitOnForwardFailure=yes \
      -o StrictHostKeyChecking=no \
      -o UserKnownHostsFile=/dev/null \
      -o IdentitiesOnly=yes \
      -p "${SERVER_SSH_PORT}" \
      -i "${ROOT_KEY}" \
      -L "127.0.0.1:${LOCAL_PGPORT}:127.0.0.1:${PGPORT}" \
      -L "127.0.0.1:${LOCAL_MAINDB_SSH_PORT}:127.0.0.1:${SERVER_SSH_PORT}" \
      "${PORTAL_TUNNEL_TARGET}" || return 1
    LOCAL_PGHOST="127.0.0.1"
    # Paramiko jump: direct to MainDB SSH (manual ssh -p PORT user@PGHOST). Local forward to
    # remote 127.0.0.1:PORT often gets RST when sshd does not listen on loopback (common on cloud).
    SERVER_METRICS_JUMP_HOST="${PGHOST}"
    SERVER_METRICS_JUMP_USER="${SERVER_SSH_USER}"
    SERVER_METRICS_JUMP_CONNECT_PORT="${SERVER_SSH_PORT}"
    echo "MainDB Postgres via SSH tunnel (${PORTAL_TUNNEL_TARGET} -> remote 127.0.0.1:${PGPORT}, local ${LOCAL_PGHOST}:${LOCAL_PGPORT})"
  else
    echo "ERROR: MainDB Postgres must be reached via SSH to localhost on MainDB (ROOT_KEY missing or unreadable: ${ROOT_KEY:-unset})." >&2
    echo "Run Save and Run Bootstrap for this portal environment, then retry." >&2
    return 1
  fi

  PG_SSLMODE="${PG_SSLMODE:-disable}"
  unset PGSSLMODE PGSSLCERT PGSSLKEY PGSSLROOTCERT PGSSLCRL

  export LOCAL_PGHOST
  export PG_SSLMODE
  export PGSSLMODE="${PG_SSLMODE}"
  export LOCAL_PGPORT
  export LOCAL_MAINDB_SSH_PORT
  export SERVER_METRICS_JUMP_HOST
  export SERVER_METRICS_JUMP_USER
  if [[ -n "${SERVER_METRICS_JUMP_CONNECT_PORT:-}" ]]; then
    export SERVER_METRICS_JUMP_CONNECT_PORT
  fi
  return 0
}
