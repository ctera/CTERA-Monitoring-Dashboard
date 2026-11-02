#!/usr/bin/env python3
"""
Lightweight infrastructure probes (Phase 1): Postgres, MainDB SSH, portal-manage status on MainDB.
Writes infrastructure.csv for threshold alerts.
"""

import argparse
import csv
import os
import re
import sys
from datetime import datetime, timezone

try:
    import psycopg2
except Exception:
    print("ERROR: psycopg2 not installed.", file=sys.stderr)
    raise

from ssh_collect_from_pg import (
    as_root_command,
    connect_ssh,
    load_pkey,
    parse_portal_manage_versions,
    ssh_exec_text,
)

CSV_HEADERS = [
    "CheckType",
    "TargetName",
    "TargetHost",
    "Result",
    "Detail",
    "CheckedAtUTC",
]


def utc_now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def write_rows(path, rows):
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_HEADERS, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def assess_portal_manage_status(text):
    body = str(text or "")
    if not body.strip():
        return False, "empty output from portal-manage status"
    lower = body.lower()
    if re.search(r"\b(stopped|failed|not running|inactive)\b", lower):
        if not re.search(r"\b0 failed\b", lower):
            return False, "portal-manage output suggests a stopped or failed service"
    image, service = parse_portal_manage_versions(body)
    if image or service:
        return True, f"Image {image or '?'} | Service {service or '?'}"
    if "running" in lower:
        return True, "portal-manage output mentions running services"
    return False, "could not parse portal-manage status (no version line or running hint)"


def probe_postgres(host, port, dbname, user, password, sslmode, timeout):
    try:
        conn = psycopg2.connect(
            host=host,
            port=port,
            dbname=dbname,
            user=user,
            password=password,
            sslmode=sslmode,
            connect_timeout=max(1, int(timeout)),
        )
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
                cur.fetchone()
        finally:
            conn.close()
        return "OK", "SELECT 1 succeeded"
    except Exception as exc:
        return "FAIL", str(exc)


def probe_maindb_ssh(jump_host, jump_port, jump_user, key_path, timeout):
    try:
        pkey = load_pkey(key_path)
        client = connect_ssh(jump_host, jump_user, port=jump_port, pkey=pkey, timeout=timeout)
        try:
            out = ssh_exec_text(client, "echo infra-ssh-ok", timeout=timeout)
            if "infra-ssh-ok" not in out:
                return "FAIL", f"unexpected echo response: {out[:200]}"
            return "OK", "SSH session established on MainDB forward"
        finally:
            client.close()
    except Exception as exc:
        return "FAIL", str(exc)


def probe_portal_manage(jump_host, jump_port, jump_user, key_path, use_sudo, timeout):
    try:
        pkey = load_pkey(key_path)
        client = connect_ssh(jump_host, jump_user, port=jump_port, pkey=pkey, timeout=timeout)
        try:
            def exec_fn(cmd):
                wrapped = as_root_command(cmd, use_sudo=use_sudo)
                return ssh_exec_text(client, wrapped, timeout=timeout + 15)

            text = exec_fn(
                """
export PATH="$PATH:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
for candidate in \
  portal-manage.sh \
  /usr/local/bin/portal-manage.sh \
  /usr/bin/portal-manage.sh \
  /opt/ctera/bin/portal-manage.sh \
  portal.sh \
  /usr/local/bin/portal.sh \
  /usr/bin/portal.sh \
  /opt/ctera/bin/portal.sh
do
  resolved=""
  if command -v "$candidate" >/dev/null 2>&1; then
    resolved="$(command -v "$candidate")"
  elif [ -x "$candidate" ]; then
    resolved="$candidate"
  fi
  if [ -n "$resolved" ]; then
    "$resolved" status 2>&1
    exit 0
  fi
done
echo "__PORTAL_MANAGE_NOT_FOUND__"
""".strip()
            )
            if text.strip() == "__PORTAL_MANAGE_NOT_FOUND__":
                return "FAIL", "portal-manage.sh not found on MainDB"
            ok, detail = assess_portal_manage_status(text)
            return ("OK" if ok else "FAIL"), detail
        finally:
            client.close()
    except Exception as exc:
        return "FAIL", str(exc)


def tunnel_failure_rows(maindb_host, maindb_label, error_message):
    ts = utc_now()
    msg = str(error_message or "MainDB SSH tunnel setup failed").strip()
    base = {
        "TargetName": maindb_label or "MainDB",
        "TargetHost": maindb_host or "",
        "CheckedAtUTC": ts,
    }
    return [
        {**base, "CheckType": "postgres", "Result": "FAIL", "Detail": msg},
        {**base, "CheckType": "maindb_ssh", "Result": "FAIL", "Detail": msg},
        {**base, "CheckType": "portal_manage", "Result": "FAIL", "Detail": msg},
    ]


def main():
    ap = argparse.ArgumentParser(description="Write infrastructure probe CSV for CTERA Monitoring Dashboard.")
    ap.add_argument("--out", required=True, help="Output CSV path")
    ap.add_argument("--maindb-label", default="MainDB", help="Display name for MainDB target")
    ap.add_argument("--maindb-host", default="", help="MainDB host/IP for reporting")
    ap.add_argument("--tunnel-error", default="", help="If set, write FAIL rows for all checks without probing")
    ap.add_argument("--pg-host", default="127.0.0.1")
    ap.add_argument("--pg-port", type=int, default=5432)
    ap.add_argument("--pg-db", default="postgres")
    ap.add_argument("--pg-user", required=True)
    ap.add_argument("--pg-password", default=os.environ.get("PGPASSWORD") or "")
    ap.add_argument("--pg-sslmode", default="disable")
    ap.add_argument("--ssh-jump-host", default="127.0.0.1")
    ap.add_argument("--ssh-jump-port", type=int, default=22)
    ap.add_argument("--ssh-jump-user", default="root")
    ap.add_argument("--ssh-key", default="")
    ap.add_argument("--ssh-sudo", action="store_true")
    ap.add_argument("--timeout", type=int, default=10)
    args = ap.parse_args()

    if not str(args.pg_password or "").strip():
        print("ERROR: Postgres password is required (PGPASSWORD).", file=sys.stderr)
        return 2

    if str(args.tunnel_error or "").strip():
        write_rows(args.out, tunnel_failure_rows(args.maindb_host, args.maindb_label, args.tunnel_error))
        print(f"Wrote tunnel-failure infrastructure probes to {args.out}")
        return 0

    ts = utc_now()
    target_name = str(args.maindb_label or "MainDB")
    target_host = str(args.maindb_host or args.pg_host or "")

    pg_result, pg_detail = probe_postgres(
        args.pg_host,
        args.pg_port,
        args.pg_db,
        args.pg_user,
        args.pg_password,
        args.pg_sslmode,
        args.timeout,
    )
    ssh_result, ssh_detail = probe_maindb_ssh(
        args.ssh_jump_host,
        args.ssh_jump_port,
        args.ssh_jump_user,
        args.ssh_key,
        args.timeout,
    )
    pm_result, pm_detail = probe_portal_manage(
        args.ssh_jump_host,
        args.ssh_jump_port,
        args.ssh_jump_user,
        args.ssh_key,
        args.ssh_sudo,
        args.timeout,
    )

    rows = [
        {
            "CheckType": "postgres",
            "TargetName": target_name,
            "TargetHost": target_host,
            "Result": pg_result,
            "Detail": pg_detail,
            "CheckedAtUTC": ts,
        },
        {
            "CheckType": "maindb_ssh",
            "TargetName": target_name,
            "TargetHost": target_host,
            "Result": ssh_result,
            "Detail": ssh_detail,
            "CheckedAtUTC": ts,
        },
        {
            "CheckType": "portal_manage",
            "TargetName": target_name,
            "TargetHost": target_host,
            "Result": pm_result,
            "Detail": pm_detail,
            "CheckedAtUTC": ts,
        },
    ]
    write_rows(args.out, rows)
    print(f"Wrote infrastructure probes to {args.out} (postgres={pg_result}, ssh={ssh_result}, portal_manage={pm_result})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
