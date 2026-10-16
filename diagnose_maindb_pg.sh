#!/usr/bin/env bash
# Run on the monitoring host to compare direct vs SSH-tunneled Postgres (no secrets printed).
set -euo pipefail

RUNTIME_ENV="${1:-}"
if [[ -z "${RUNTIME_ENV}" || ! -f "${RUNTIME_ENV}" ]]; then
  echo "Usage: $0 /path/to/state/runtime_env/environment-<id>.env" >&2
  exit 2
fi

set -a
# shellcheck disable=SC1090
source "${RUNTIME_ENV}"
set +a

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "${SCRIPT_DIR}"
# shellcheck disable=SC1091
source "${SCRIPT_DIR}/venv/bin/activate"

echo "=== Config (no passwords) ==="
echo "PGHOST=${PGHOST:-} PGPORT=${PGPORT:-} PGUSER=${PGUSER:-postgres}"
echo "PGPASSWORD length: ${#PGPASSWORD}"
echo "ROOT_KEY=${ROOT_KEY:-unset} readable=$([[ -n "${ROOT_KEY:-}" && -r "${ROOT_KEY}" ]] && echo yes || echo no)"
echo "SERVER_SSH_USER=${SERVER_SSH_USER:-root} SERVER_SSH_PORT=${SERVER_SSH_PORT:-22}"
echo "JUMP_HOST_ENABLED=${JUMP_HOST_ENABLED:-false}"

python - <<'PY'
import os, socket, sys
import psycopg2

def try_connect(label, host, port, sslmode):
    pw = os.environ.get("PGPASSWORD", "")
    user = os.environ.get("PGUSER", "postgres")
    db = os.environ.get("PGDATABASE", "postgres")
    print(f"\n--- {label}: host={host} port={port} sslmode={sslmode} pw_len={len(pw)} ---")
    try:
        conn = psycopg2.connect(
            host=host, port=int(port), dbname=db, user=user, password=pw, sslmode=sslmode,
            connect_timeout=8,
        )
        cur = conn.cursor()
        cur.execute("SELECT inet_server_addr()::text, inet_server_port()")
        row = cur.fetchone()
        print(f"OK server_addr={row[0]} server_port={row[1]}")
        conn.close()
        return True
    except Exception as exc:
        print(f"FAIL: {exc}")
        return False

pghost = os.environ.get("PGHOST", "")
pgport = os.environ.get("PGPORT", "5432")
try_connect("Direct from monitoring host", pghost, pgport, "disable")

key = os.environ.get("ROOT_KEY", "")
user = os.environ.get("SERVER_SSH_USER", "root")
ssh_port = int(os.environ.get("SERVER_SSH_PORT", "22"))
if not (key and os.path.isfile(key) and pghost):
    print("\n(Skip tunnel test: ROOT_KEY or PGHOST missing)")
    sys.exit(0)

s = socket.socket()
s.bind(("127.0.0.1", 0))
local_port = s.getsockname()[1]
s.close()

import subprocess
ssh = [
    "ssh", "-N",
    "-o", "BatchMode=yes",
    "-o", "ExitOnForwardFailure=yes",
    "-o", "StrictHostKeyChecking=no",
    "-o", "UserKnownHostsFile=/dev/null",
    "-o", "IdentitiesOnly=yes",
    "-p", str(ssh_port),
    "-i", key,
    "-L", f"127.0.0.1:{local_port}:127.0.0.1:{pgport}",
    f"{user}@{pghost}",
]
proc = subprocess.Popen(ssh)
try:
    import time
    time.sleep(1)
    ok = try_connect("Via SSH tunnel (like portal_jobs)", "127.0.0.1", local_port, "disable")
    sys.exit(0 if ok else 1)
finally:
    proc.terminate()
    proc.wait(timeout=5)
PY
