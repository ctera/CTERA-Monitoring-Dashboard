#!/usr/bin/env bash
# Test MainDB Postgres path the same way portal_jobs.sh does (10f+).
# Run on the monitoring host. Does not print postgres passwords.
set -euo pipefail

ENV_ID="${1:-1}"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${REPO_ROOT}"

ENV_FILE="${REPO_ROOT}/state/runtime_env/environment-${ENV_ID}.env"
if [[ ! -f "${ENV_FILE}" ]]; then
  echo "FAIL: missing ${ENV_FILE}" >&2
  exit 2
fi

set -a
# shellcheck disable=SC1090
source "${ENV_FILE}"
set +a
# shellcheck disable=SC1091
source "${REPO_ROOT}/venv/bin/activate"

echo "========== test_pg_resolve (env id ${ENV_ID}) =========="
echo "VERSION=$(cat VERSION 2>/dev/null || echo unknown)"
echo "PGHOST=${PGHOST}  PGPORT=${PGPORT:-5432}  PGUSER=${PGUSER:-postgres}"
echo "ROOT_KEY=${ROOT_KEY:-unset} readable=$([[ -n "${ROOT_KEY:-}" && -r "${ROOT_KEY}" ]] && echo yes || echo NO)"
echo "SERVER_SSH_USER=${SERVER_SSH_USER:-root}  SERVER_SSH_PORT=${SERVER_SSH_PORT:-22}"
echo "Saved PGPASSWORD in env file: $([[ -n "${PGPASSWORD:-}" ]] && echo "present len=${#PGPASSWORD}" || echo absent)"

[[ -r "${ROOT_KEY:-}" ]] || { echo "FAIL: ROOT_KEY not readable"; exit 1; }
[[ -n "${PGHOST:-}" ]] || { echo "FAIL: PGHOST empty"; exit 1; }

choose_free_port() {
  python - <<'PY'
import socket
s = socket.socket()
s.bind(("127.0.0.1", 0))
print(s.getsockname()[1])
s.close()
PY
}

cleanup() {
  if [[ -n "${TUNNEL_SOCKET:-}" && -n "${TUNNEL_TARGET:-}" ]]; then
    ssh -S "${TUNNEL_SOCKET}" -O exit "${TUNNEL_TARGET}" >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT

LOCAL_PGPORT="$(choose_free_port)"
LOCAL_MAINDB_SSH_PORT="$(choose_free_port)"
TUNNEL_SOCKET="/tmp/ctera-monitoring-test-pg-${ENV_ID}-$$.sock"
TUNNEL_TARGET="${SERVER_SSH_USER}@${PGHOST}"

echo ""
echo "--- SSH tunnel (same as portal_jobs) ---"
ssh -M -S "${TUNNEL_SOCKET}" -fnNT \
  -o BatchMode=yes \
  -o ExitOnForwardFailure=yes \
  -o StrictHostKeyChecking=no \
  -o UserKnownHostsFile=/dev/null \
  -o IdentitiesOnly=yes \
  -p "${SERVER_SSH_PORT}" \
  -i "${ROOT_KEY}" \
  -L "127.0.0.1:${LOCAL_PGPORT}:127.0.0.1:${PGPORT:-5432}" \
  -L "127.0.0.1:${LOCAL_MAINDB_SSH_PORT}:127.0.0.1:${SERVER_SSH_PORT}" \
  "${TUNNEL_TARGET}"
echo "OK tunnel local_pg=${LOCAL_PGPORT} local_ssh=${LOCAL_MAINDB_SSH_PORT}"

echo ""
echo "--- PostgresPasswordTool on MainDB (diagnostics only) ---"
DIAG="$(
  ssh -o BatchMode=yes -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o IdentitiesOnly=yes \
    -i "${ROOT_KEY}" -p "${LOCAL_MAINDB_SSH_PORT}" "${SERVER_SSH_USER}@127.0.0.1" bash -s 2>&1 <<'EOF' || true
set -euo pipefail
enc=$(awk -F= '/^[[:space:]]*CTERA_LOCAL_POSTGRES_PASS[[:space:]]*=/ { sub(/^[^=]*=/, ""); gsub(/^[[:space:]]+|[[:space:]]+$/, ""); print; exit }' /etc/ctera/portal.cfg)
enc="${enc%\"}"; enc="${enc#\"}"; enc="${enc%\'}"; enc="${enc#\'}"
echo "enc_len=${#enc}"
test -r /etc/ctera/portal_key && echo portal_key_readable=yes || echo portal_key_readable=NO
out=$(/usr/local/ctera/jdk/bin/java -cp '/usr/local/ctera/apache-tomcat/lib/portal/*:/usr/local/ctera/apache-tomcat/lib/common.jar' \
  com.ctera.utils.password.PostgresPasswordTool "$(cat /etc/ctera/portal_key)" "$enc" reveal 2>&1) || true
printf '%s\n' "$out" | awk '{ print "tool_line_len=" length($0) }'
printf '%s\n' "$out" | grep -qxF false && echo tool_emitted_false=YES
printf '%s\n' "$out" | grep -qxF true && echo tool_emitted_true=YES
EOF
)"
printf '%s\n' "${DIAG}"

echo ""
echo "--- Resolve password (portal_jobs parser) ---"
REVEAL_SSH=(ssh -o BatchMode=yes -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o IdentitiesOnly=yes
  -i "${ROOT_KEY}" -p "${LOCAL_MAINDB_SSH_PORT}" "${SERVER_SSH_USER}@127.0.0.1")
if [[ "${SERVER_SSH_USER}" != "root" && "${SERVER_METRICS_SUDO:-true}" =~ ^(1|true|yes|on)$ ]]; then
  REVEAL_REMOTE=(sudo -n bash -s)
else
  REVEAL_REMOTE=(bash -s)
fi
RAW="$(
  "${REVEAL_SSH[@]}" "${REVEAL_REMOTE[@]}" 2>&1 <<'EOF' || true
set -euo pipefail
enc=$(awk -F= '/^[[:space:]]*CTERA_LOCAL_POSTGRES_PASS[[:space:]]*=/ { sub(/^[^=]*=/, ""); gsub(/^[[:space:]]+|[[:space:]]+$/, ""); print; exit }' /etc/ctera/portal.cfg)
enc="${enc%\"}"; enc="${enc#\"}"; enc="${enc%\'}"; enc="${enc#\'}"
exec /usr/local/ctera/jdk/bin/java -cp '/usr/local/ctera/apache-tomcat/lib/portal/*:/usr/local/ctera/apache-tomcat/lib/common.jar' \
  com.ctera.utils.password.PostgresPasswordTool "$(cat /etc/ctera/portal_key)" "$enc" reveal
EOF
)"
PARSED="$(
  printf '%s' "${RAW}" | tr -d '\r' | sed '/^[[:space:]]*$/d' \
    | grep -viE '^(exception|error:|at com\.|at java\.|picked up _java_options)' \
    | grep -viE '^(true|false)$' | awk '{ print length, $0 }' | sort -rn | head -1 | cut -d' ' -f2-
)"
echo "parsed_password_len=${#PARSED}"
if [[ "${#PARSED}" -lt 8 ]]; then
  echo "FAIL: parsed password too short — PostgresPasswordTool did not return a real password."
  echo "      If you saw tool_emitted_false=YES above, fix portal_key / portal.cfg on MainDB."
  exit 1
fi

export PGPASSWORD="${PARSED}"
echo ""
echo "--- psycopg2 via tunnel (127.0.0.1:${LOCAL_PGPORT}) ---"
python - <<PY
import os, sys, psycopg2
host, port = "127.0.0.1", int("${LOCAL_PGPORT}")
pw = os.environ["PGPASSWORD"]
try:
    c = psycopg2.connect(host=host, port=port, dbname=os.environ.get("PGDATABASE", "postgres"),
        user=os.environ.get("PGUSER", "postgres"), password=pw, sslmode="disable", connect_timeout=10)
    cur = c.cursor()
    cur.execute("SELECT 1")
    print("OK: SELECT 1 via tunnel + resolved password")
    c.close()
except Exception as exc:
    print(f"FAIL: {exc}", file=sys.stderr)
    sys.exit(1)
PY

echo ""
echo "========== PASS: collector Postgres path should work =========="
