#!/usr/bin/env python3
# Export tenants (portals) from Postgres into CSV.
# Mirrors ssh_collect_from_pg.py CLI; supports optional SSH tunnel.

import argparse, csv, os, subprocess, time, sys
import psycopg2, psycopg2.extras

def fetch_tenants(conn):
    # Pull tenant name and create_date from base_objects, plan name via
    # plans->base_objects, and use base_objects.is_deleted plus
    # portals.deletion_date for tenant lifecycle state.
    q = """
    SELECT
        p.uid                                         AS uid,
        bo.name                                       AS tenant,
        p.portal_type                                 AS portal_type,
        p.enable_reseller_provisioning                AS enable_reseller_provisioning,
        bo.create_date                                AS created_date,
        bo.is_deleted                                 AS deleted,
        p.deletion_date                               AS deleted_date,
        boplan.name                                   AS plan_name
    FROM portals p
    JOIN base_objects bo
      ON bo.uid = p.uid
    LEFT JOIN plans pl
      ON pl.uid = p.plan_id
    LEFT JOIN base_objects boplan
      ON boplan.uid = pl.uid
    ORDER BY LOWER(bo.name);
    """
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(q)
        return [dict(r) for r in cur.fetchall()]

def write_csv(rows, out_path):
    headers = ["UID","Tenant","PortalType",
               "EnableResellerProvisioning","Active","Deleted","CreatedDate","DeletedDate","PlanName"]
    tmp = out_path + ".tmp"
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    with open(tmp, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=headers)
        w.writeheader()
        for r in rows:
            w.writerow({
                "UID": r.get("uid",""),
                "Tenant": r.get("tenant",""),
                "PortalType": r.get("portal_type",""),
                "EnableResellerProvisioning": r.get("enable_reseller_provisioning",""),
                "Active": not bool(r.get("deleted", False)),
                "Deleted": r.get("deleted",""),
                "CreatedDate": r.get("created_date",""),
                "DeletedDate": r.get("deleted_date",""),
                "PlanName": r.get("plan_name",""),
            })
    os.replace(tmp, out_path)
    print(f"Wrote {len(rows)} rows to {out_path}")

def start_ssh_tunnel(user, host, key, remote_pg_port, local_port, ssh_port=22, timeout=10):
    if not (user and host and key):
        return None
    cmd = [
        "ssh","-i", key,
        "-o","StrictHostKeyChecking=no","-o","UserKnownHostsFile=/dev/null",
        "-p", str(ssh_port), f"{user}@{host}",
        "-N","-L", f"{local_port}:127.0.0.1:{remote_pg_port}",
    ]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            psycopg2.connect(host="127.0.0.1", port=local_port,
                             dbname="postgres", user="invalid",
                             password="invalid", connect_timeout=1).close()
            break
        except Exception:
            if proc.poll() is not None:
                err = proc.stderr.read().decode(errors="ignore")
                raise RuntimeError(f"SSH tunnel failed to start: {err}")
            time.sleep(0.2)
    return proc

def stop_ssh_tunnel(proc):
    if not proc: return
    try:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
    except Exception:
        pass

def main():
    ap = argparse.ArgumentParser(description="Collect tenants (portals) from Postgres into CSV.")
    ap.add_argument("--pg-host", required=True)
    ap.add_argument("--pg-port", type=int, default=5432)
    ap.add_argument("--pg-db", default="postgres")
    ap.add_argument("--pg-user", required=True)
    ap.add_argument("--pg-password", required=True)
    ap.add_argument("--pg-sslmode",
                    choices=["disable","allow","prefer","require","verify-ca","verify-full"],
                    default="prefer")
    ap.add_argument("--out", required=True, help="Output CSV path")

    # optional SSH-tunnel knobs (CLI-compatible with your other collector)
    ap.add_argument("--user", help="SSH user (used only with --ssh-host)")
    ap.add_argument("--password", help="SSH password (unused; keys recommended)", default=None)
    ap.add_argument("--key", help="SSH private key path (used only with --ssh-host)", default=None)
    ap.add_argument("--passphrase", help="SSH key passphrase (unused here)", default=None)
    ap.add_argument("--ssh-host", help="Open an SSH tunnel to this host for PG access", default=None)
    ap.add_argument("--ssh-port", type=int, default=22)
    ap.add_argument("--ssh-timeout", type=int, default=10)
    ap.add_argument("--local-port", type=int, default=6543, help="Local port for the SSH tunnel")

    args = ap.parse_args()

    tunnel = None
    pg_host = args.pg_host
    pg_port = args.pg_port

    if args.ssh_host:
        if not (args.user and args.key):
            print("ERROR: --ssh-host requires --user and --key", file=sys.stderr)
            sys.exit(2)
        tunnel = start_ssh_tunnel(
            user=args.user, host=args.ssh_host, key=args.key,
            remote_pg_port=args.pg_port, local_port=args.local_port,
            ssh_port=args.ssh_port, timeout=args.ssh_timeout
        )
        pg_host = "127.0.0.1"
        pg_port = args.local_port

    try:
        conn = psycopg2.connect(
            host=pg_host, port=pg_port, dbname=args.pg_db,
            user=args.pg_user, password=args.pg_password, sslmode=args.pg_sslmode
        )
        try:
            rows = fetch_tenants(conn)
        finally:
            conn.close()
        write_csv(rows, args.out)
    finally:
        stop_ssh_tunnel(tunnel)

if __name__ == "__main__":
    main()
