#!/usr/bin/env python3
# ssh_collect_from_pg.py  (servers health + friendly units + Name join)
#
# - Joins servers.uid -> base_objects.uid to emit Name (first column)
# - Removes noisy fields (Timestamp/User/Port/IsCatalogNode)
# - Memory & disk byte fields rendered as GB (2 decimals)
# - All *Pct fields rendered with % (2 decimals)
# - Pools:
#     /usr/local/lib/ctera OR /usr/local/lib/data  -> DataPool*
#     /usr/local/lib/db_archive OR /usr/local/lib/data_archive -> DBArchivePool*
#
# Requires: paramiko, psycopg2-binary

import argparse
import csv
import hashlib
import os
import shlex
import time
import json

import paramiko
import psycopg2
import psycopg2.extras


# ---------------------------
# Tiny helpers
# ---------------------------

def to_int(v, default=None):
    try:
        return int(float(v))
    except Exception:
        return default

def to_float(v, default=None):
    try:
        return float(v)
    except Exception:
        return default

def gb(n):
    if n is None or n == "":
        return None
    try:
        return float(n) / (1024.0 ** 3)
    except Exception:
        return None

def fmt_gb(n):
    g = gb(n)
    return f"{g:.2f}" if g is not None else ""

def fmt_pct(x):
    try:
        if x is None or x == "":
            return ""
        return f"{float(x):.2f}%"
    except Exception:
        return ""


# ---------------------------
# SSH key loader (Paramiko 3.x+, no DSA)
# ---------------------------

def load_pkey(path, passphrase=None):
    """
    Try Ed25519, RSA, ECDSA in that order.
    """
    kinds = []
    for name in ("Ed25519Key", "RSAKey", "ECDSAKey"):
        cls = getattr(paramiko, name, None)
        if cls:
            kinds.append(cls)
    last = None
    for cls in kinds:
        try:
            return cls.from_private_key_file(path, password=passphrase or None)
        except Exception as e:
            last = e
            continue
    raise ValueError(f"Could not load SSH key '{path}': {last}")


# ---------------------------
# SSH exec / parsers
# ---------------------------

def ssh_exec_text(client: paramiko.SSHClient, cmd: str, timeout: int = 10) -> str:
    stdin, stdout, stderr = client.exec_command(cmd, timeout=timeout)
    out = stdout.read().decode("utf-8", errors="replace")
    err = stderr.read().decode("utf-8", errors="replace")
    rc = stdout.channel.recv_exit_status()
    if rc != 0:
        detail = err.strip() or out.strip() or f"exit status {rc}"
        raise RuntimeError(detail)
    return out

def parse_meminfo(text):
    """
    Returns (total_bytes, used_bytes, used_pct)
    Uses MemAvailable if present, otherwise MemFree+Buffers+Cached fallback.
    """
    kv = {}
    for line in text.splitlines():
        if ":" not in line:
            continue
        k, v = line.split(":", 1)
        kv[k.strip()] = v.strip()

    def kB_to_B(s):
        num = s.split()[0]
        return int(float(num)) * 1024

    total = kB_to_B(kv.get("MemTotal", "0 kB"))
    avail = kv.get("MemAvailable")
    if avail:
        used = total - kB_to_B(avail)
    else:
        free = kB_to_B(kv.get("MemFree", "0 kB"))
        buffers = kB_to_B(kv.get("Buffers", "0 kB"))
        cached = kB_to_B(kv.get("Cached", "0 kB"))
        used = total - (free + buffers + cached)

    used_pct = (used * 100.0 / total) if total > 0 else 0.0
    return total, used, used_pct

def parse_df_posix(text):
    """
    Parse `df -P -B1` output. Handles:
      Filesystem 1-blocks Used Available Capacity Mounted on
      Filesystem 1B-blocks Used Available Use%     Mounted on
    Returns: list of dicts: { mount, size, used, used_pct }
    """
    lines = [l for l in text.splitlines() if l.strip()]
    if len(lines) < 2:
        return []
    results = []
    for line in lines[1:]:
        parts = line.split()
        if len(parts) < 6:
            continue
        try:
            size = int(parts[1])
            used = int(parts[2])
            pct_tok = parts[-2]
            used_pct = float(pct_tok[:-1]) if pct_tok.endswith("%") else float(pct_tok)
            mount = parts[-1]
        except Exception:
            continue
        results.append({"mount": mount, "size": size, "used": used, "used_pct": used_pct})
    return results

def read_cpu_stat_snapshot(text):
    for line in text.splitlines():
        if line.startswith("cpu "):
            parts = line.split()
            vals = [to_int(x, 0) for x in parts[1:11]]
            keys = ["user","nice","system","idle","iowait","irq","softirq","steal","guest","guest_nice"]
            return dict(zip(keys, vals))
    return None

def cpu_breakdown(s1, s2):
    if not s1 or not s2:
        return (None, None, None, None)
    def total(s):
        return sum([s[k] for k in ("user","nice","system","idle","iowait","irq","softirq","steal")])
    idle1 = s1["idle"] + s1["iowait"]
    idle2 = s2["idle"] + s2["iowait"]
    non1 = total(s1) - idle1
    non2 = total(s2) - idle2
    t1 = idle1 + non1
    t2 = idle2 + non2
    dt = max(1, t2 - t1)
    d_idle = max(0, idle2 - idle1)
    d_user = max(0, (s2["user"] + s2["nice"]) - (s1["user"] + s1["nice"]))
    d_sys  = max(0, (s2["system"] + s2["irq"] + s2["softirq"]) - (s1["system"] + s1["irq"] + s1["softirq"]))
    d_iow  = max(0, s2["iowait"] - s1["iowait"])
    idle_pct = 100.0 * d_idle / dt
    user_pct = 100.0 * d_user / dt
    sys_pct  = 100.0 * d_sys  / dt
    iow_pct  = 100.0 * d_iow  / dt
    return (user_pct, sys_pct, iow_pct, idle_pct)


def split_wide_columns(line):
    return [part.strip() for part in line.strip().split("  ") if part.strip()]


def compute_view_hash(rows, keys):
    canonical = []
    for row in rows:
        canonical.append("|".join(str(row.get(key, "")).strip() for key in keys))
    canonical.sort()
    return hashlib.sha1("\n".join(canonical).encode("utf-8")).hexdigest()[:12] if canonical else ""


def parse_nomad_nodes(text):
    lines = [line.rstrip() for line in text.splitlines() if line.strip()]
    if len(lines) < 2:
        return []
    rows = []
    for line in lines[1:]:
        parts = split_wide_columns(line)
        if len(parts) < 9:
            continue
        rows.append({
            "NodeID": parts[0],
            "NodePool": parts[1],
            "DC": parts[2],
            "Name": parts[3],
            "Class": parts[4],
            "Address": parts[5],
            "Version": parts[6],
            "Drain": parts[7],
            "Eligibility": parts[8],
            "Status": parts[9] if len(parts) > 9 else "",
        })
    return rows


def parse_consul_members(text):
    lines = [line.rstrip() for line in text.splitlines() if line.strip()]
    if len(lines) < 2:
        return []
    rows = []
    for line in lines[1:]:
        parts = split_wide_columns(line)
        if len(parts) < 8:
            continue
        rows.append({
            "Node": parts[0],
            "Address": parts[1],
            "Status": parts[2],
            "Type": parts[3],
            "Build": parts[4],
            "Protocol": parts[5],
            "DC": parts[6],
            "Partition": parts[7],
            "Segment": parts[8] if len(parts) > 8 else "",
        })
    return rows


def parse_docker_ps(text):
    rows = []
    for line in text.splitlines():
        if not line.strip():
            continue
        parts = line.split("\t")
        while len(parts) < 4:
            parts.append("")
        rows.append({
            "ContainerID": parts[0].strip(),
            "ContainerName": parts[1].strip(),
            "Image": parts[2].strip(),
            "StatusText": parts[3].strip(),
        })
    return rows


def parse_docker_inspect_line(line):
    payload = json.loads((line or "").strip() or "{}")
    state = payload.get("State") or {}
    health = state.get("Health") or {}
    host_config = payload.get("HostConfig") or {}
    restart_policy = host_config.get("RestartPolicy") or {}
    return {
        "ContainerID": str(payload.get("Id") or "").strip(),
        "InspectName": str(payload.get("Name") or "").strip().lstrip("/"),
        "State": str(state.get("Status") or "").strip(),
        "Health": str(health.get("Status") or "").strip(),
        "RestartCount": to_int(payload.get("RestartCount"), 0) or 0,
        "RestartPolicy": str(restart_policy.get("Name") or "").strip(),
        "StartedAt": str(state.get("StartedAt") or "").strip(),
        "FinishedAt": str(state.get("FinishedAt") or "").strip(),
    }


def load_previous_docker_counts(path):
    previous = {}
    if not path or not os.path.exists(path):
        return previous
    try:
        with open(path, "r", encoding="utf-8", newline="") as handle:
            reader = csv.DictReader(handle)
            for row in reader:
                key = (
                    str(row.get("SourceHost", "")).strip(),
                    str(row.get("ContainerName", "")).strip(),
                )
                previous[key] = to_int(row.get("RestartCount"), 0) or 0
    except Exception:
        return {}
    return previous


def gather_docker_rows(meta, exec_fn, previous_counts):
    docker_rows = []
    host_uptime = to_int(meta.get("HostUptimeSeconds"), 0) or 0
    recently_booted = host_uptime > 0 and host_uptime < 600
    try:
        ps_text = exec_fn(
            "if command -v docker >/dev/null 2>&1; then docker ps -a --format '{{.ID}}\t{{.Names}}\t{{.Image}}\t{{.Status}}'; else echo '__DOCKER_NOT_INSTALLED__'; fi"
        )
        if "__DOCKER_NOT_INSTALLED__" in ps_text:
            raise RuntimeError("docker command not installed")
        containers = parse_docker_ps(ps_text)
        for container in containers:
            inspect_cmd = (
                "docker inspect --format "
                "'{{json .}}' "
                + shlex.quote(container["ContainerID"])
            )
            inspect_text = exec_fn(inspect_cmd).strip()
            inspect_data = parse_docker_inspect_line(inspect_text)
            container_name = container["ContainerName"] or inspect_data["InspectName"]
            prev_restart_count = previous_counts.get((meta["Host"], container_name), 0)
            restart_count = inspect_data["RestartCount"]
            docker_rows.append({
                "SourceName": meta["Name"],
                "SourceHost": meta["Host"],
                "SourceUID": meta["UID"],
                "HostUptimeSeconds": host_uptime,
                "RecentlyBooted": "True" if recently_booted else "False",
                "GraceState": "Host reboot grace" if recently_booted else "",
                "ContainerID": inspect_data["ContainerID"] or container["ContainerID"],
                "ContainerName": container_name,
                "Image": container["Image"],
                "State": inspect_data["State"],
                "Health": inspect_data["Health"],
                "RestartCount": restart_count,
                "RestartDelta": max(0, restart_count - prev_restart_count),
                "RestartPolicy": inspect_data["RestartPolicy"],
                "StartedAt": inspect_data["StartedAt"],
                "FinishedAt": inspect_data["FinishedAt"],
                "StatusText": container["StatusText"],
                "CollectionError": "",
            })
    except Exception as exc:
        docker_rows.append({
            "SourceName": meta["Name"],
            "SourceHost": meta["Host"],
            "SourceUID": meta["UID"],
            "HostUptimeSeconds": host_uptime,
            "RecentlyBooted": "True" if recently_booted else "False",
            "GraceState": "Host reboot grace" if recently_booted else "",
            "ContainerID": "",
            "ContainerName": "",
            "Image": "",
            "State": "ERROR",
            "Health": "",
            "RestartCount": "",
            "RestartDelta": "",
            "RestartPolicy": "",
            "StartedAt": "",
            "FinishedAt": "",
            "StatusText": "",
            "CollectionError": str(exc),
        })
    return docker_rows


# ---------------------------
# Metric gatherer over SSH
# ---------------------------

def gather_metrics(client, exec_text=None):
    if exec_text is None:
        exec_text = lambda cmd: ssh_exec_text(client, cmd)

    # uptime
    up = exec_text("cut -d' ' -f1 /proc/uptime || echo 0").strip()
    uptime_seconds = to_int(up, 0)

    # load averages
    load_text = exec_text("cat /proc/loadavg || echo ''")
    l1 = l5 = l15 = 0.0
    try:
        a, b, c = load_text.split()[:3]
        l1, l5, l15 = to_float(a, 0.0), to_float(b, 0.0), to_float(c, 0.0)
    except Exception:
        pass

    # memory
    mem_total = mem_used = 0
    mem_used_pct = 0.0
    mi = exec_text("cat /proc/meminfo || echo ''")
    try:
        mem_total, mem_used, mem_used_pct = parse_meminfo(mi)
    except Exception:
        pass

    # filesystem candidates
    df_cmd = r"""
candidates="/ /usr/local/lib/ctera /usr/local/lib/data /usr/local/lib/db_archive /usr/local/lib/data_archive"
list=""
for p in $candidates; do [ -e "$p" ] && list="$list $p"; done
[ -n "$list" ] && df -P -B1 $list
"""
    mounts = parse_df_posix(exec_text(df_cmd))
    disk = {
        "root":  {"total": None, "used": None, "pct": None},
        "ctera": {"total": None, "used": None, "pct": None},
        "dbarch":{"total": None, "used": None, "pct": None},
    }
    CTERA_MOUNTS   = {"/usr/local/lib/ctera", "/usr/local/lib/data"}
    DBARCH_MOUNTS  = {"/usr/local/lib/db_archive", "/usr/local/lib/data_archive"}

    for m in mounts:
        rec = {"total": m["size"], "used": m["used"], "pct": m["used_pct"]}
        if m["mount"] == "/":
            disk["root"]  = rec
        elif m["mount"] in CTERA_MOUNTS:
            disk["ctera"] = rec
        elif m["mount"] in DBARCH_MOUNTS:
            disk["dbarch"] = rec

    # cpu breakdown (0.5s window)
    stat1 = read_cpu_stat_snapshot(exec_text("cat /proc/stat"))
    time.sleep(0.5)
    stat2 = read_cpu_stat_snapshot(exec_text("cat /proc/stat"))
    user_pct, sys_pct, iow_pct, idle_pct = cpu_breakdown(stat1, stat2)

    return {
        "UptimeSeconds": uptime_seconds,
        "Load1": l1, "Load5": l5, "Load15": l15,
        "MemTotalBytes": mem_total, "MemUsedBytes": mem_used, "MemUsedPct": mem_used_pct,
        "DiskRootTotalBytes": disk["root"]["total"], "DiskRootUsedBytes": disk["root"]["used"], "DiskRootUsedPct": disk["root"]["pct"],
        "DiskCteraTotalBytes": disk["ctera"]["total"], "DiskCteraUsedBytes": disk["ctera"]["used"], "DiskCteraUsedPct": disk["ctera"]["pct"],
        "DiskDbArchiveTotalBytes": disk["dbarch"]["total"], "DiskDbArchiveUsedBytes": disk["dbarch"]["used"], "DiskDbArchiveUsedPct": disk["dbarch"]["pct"],
        "CPUUserPct": user_pct, "CPUSystemPct": sys_pct, "CPUIOWaitPct": iow_pct, "CPUIDLEPct": idle_pct
    }


# ---------------------------
# Postgres fetch (with Name join)
# ---------------------------

def fetch_servers(pg_conn, only_connected=False):
    q = """
    SELECT
        s.uid,
        bo.name            AS name,
        s.connected,
        s.default_ipaddr,
        s.public_ipaddr,
        s.main_db,
        s.running_version
    FROM servers s
    JOIN base_objects bo ON bo.uid = s.uid
    {where}
    ORDER BY s.uid
    """.format(where="WHERE s.connected = TRUE" if only_connected else "")
    with pg_conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(q)
        return [dict(r) for r in cur.fetchall()]


def connect_ssh(host, user, port=22, pkey=None, password=None, timeout=10):
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(
        host, port=port, username=user,
        pkey=pkey,
        password=(None if pkey else password),
        allow_agent=False, look_for_keys=False,
        timeout=timeout
    )
    return client


def as_root_command(cmd, use_sudo=False):
    quoted = shlex.quote(cmd)
    if use_sudo:
        return f'if [ "$(id -u)" -eq 0 ]; then bash -lc {quoted}; else sudo -n bash -lc {quoted}; fi'
    return f"bash -lc {quoted}"


def jump_exec_text(jump_client, host, target_user, cmd, *, target_sudo=False, jump_run_as_user=None, timeout=20):
    inner = as_root_command(cmd, use_sudo=target_sudo)
    ssh_parts = [
        "ssh",
        "-o", "BatchMode=yes",
        "-o", "LogLevel=ERROR",
        "-o", "StrictHostKeyChecking=no",
        "-o", "UserKnownHostsFile=/dev/null",
        "-o", f"ConnectTimeout={timeout}",
        f"{target_user}@{host}",
        inner,
    ]
    ssh_cmd = " ".join(shlex.quote(part) for part in ssh_parts)
    if jump_run_as_user:
        ssh_cmd = f"sudo -n -u {shlex.quote(jump_run_as_user)} {ssh_cmd}"
    return ssh_exec_text(jump_client, ssh_cmd, timeout=timeout + 10)


def main_db_exec_text_via_jump(jump_client, main_db_host, main_db_user, cmd, *, target_sudo=False, jump_run_as_user=None, timeout=20):
    inner = as_root_command(cmd, use_sudo=target_sudo)
    ssh_parts = [
        "ssh",
        "-o", "BatchMode=yes",
        "-o", "LogLevel=ERROR",
        "-o", "StrictHostKeyChecking=no",
        "-o", "UserKnownHostsFile=/dev/null",
        "-o", f"ConnectTimeout={timeout}",
        f"{main_db_user}@{main_db_host}",
        inner,
    ]
    ssh_cmd = " ".join(shlex.quote(part) for part in ssh_parts)
    if jump_run_as_user:
        ssh_cmd = f"sudo -n -u {shlex.quote(jump_run_as_user)} {ssh_cmd}"
    return ssh_exec_text(jump_client, ssh_cmd, timeout=timeout + 10)


def target_exec_text_via_main_db(jump_client, main_db_host, main_db_user, target_host, target_user, cmd, *, target_sudo=False, jump_run_as_user=None, timeout=20):
    target_inner = as_root_command(cmd, use_sudo=target_sudo)
    target_ssh_parts = [
        "ssh",
        "-o", "BatchMode=yes",
        "-o", "LogLevel=ERROR",
        "-o", "StrictHostKeyChecking=no",
        "-o", "UserKnownHostsFile=/dev/null",
        "-o", f"ConnectTimeout={timeout}",
        f"{target_user}@{target_host}",
        target_inner,
    ]
    target_ssh_cmd = " ".join(shlex.quote(part) for part in target_ssh_parts)
    return main_db_exec_text_via_jump(
        jump_client,
        main_db_host,
        main_db_user,
        target_ssh_cmd,
        target_sudo=False,
        jump_run_as_user=jump_run_as_user,
        timeout=timeout,
    )


# ---------------------------
# Main
# ---------------------------

def main():
    ap = argparse.ArgumentParser(description="Collect Linux metrics over SSH for servers listed in Postgres 'servers' (joined with base_objects for Name).")
    ap.add_argument("--pg-host", required=True)
    ap.add_argument("--pg-port", type=int, default=5432)
    ap.add_argument("--pg-db", default="postgres")
    ap.add_argument("--pg-user", required=True)
    ap.add_argument("--pg-password", required=True)
    ap.add_argument("--pg-sslmode", choices=["disable","allow","prefer","require","verify-ca","verify-full"], default="prefer")

    ap.add_argument("--only-connected", action="store_true", help="Only collect from rows where servers.connected = true")

    ap.add_argument("--user", default="root", help="SSH username")
    ap.add_argument("--password", default=None, help="SSH password (omit if using key)")
    ap.add_argument("--key", default=None, help="Path to SSH private key (ed25519/rsa/ecdsa)")
    ap.add_argument("--passphrase", default=None, help="Passphrase for private key (if any)")
    ap.add_argument("--port", type=int, default=22)
    ap.add_argument("--ssh-timeout", type=int, default=10)
    ap.add_argument("--sudo", action="store_true", help="Use sudo -n for metric commands on target servers")
    ap.add_argument("--jump-host", default=None, help="SSH jump host, usually MainDB")
    ap.add_argument("--jump-port", type=int, default=22, help="SSH port for the jump host (default: 22)")
    ap.add_argument("--jump-user", default=None, help="SSH username for the jump host")
    ap.add_argument("--jump-key", default=None, help="SSH private key for the jump host")
    ap.add_argument("--jump-passphrase", default=None, help="Passphrase for the jump host private key")
    ap.add_argument("--jump-run-as-user", default=None, help="Run onward ssh from jump host as this local user, usually ctera")
    ap.add_argument("--via-main-db-host", default=None, help="When jump-host access to MainDB is already configured, SSH onward to this MainDB host from the jump host")
    ap.add_argument("--via-main-db-user", default=None, help="SSH username to use from the jump host when connecting onward to MainDB")
    ap.add_argument("--out", required=True, help="Output CSV path")
    ap.add_argument("--nomad-out", default=None, help="Optional CSV output path for Nomad node status snapshots")
    ap.add_argument("--consul-out", default=None, help="Optional CSV output path for Consul member snapshots")
    ap.add_argument("--docker-out", default=None, help="Optional CSV output path for Docker container snapshots")

    args = ap.parse_args()

    # Connect to Postgres
    conn = psycopg2.connect(
        host=args.pg_host, port=args.pg_port, dbname=args.pg_db,
        user=args.pg_user, password=args.pg_password,
        sslmode=args.pg_sslmode
    )

    servers = fetch_servers(conn, only_connected=args.only_connected)
    if not servers:
        print("No servers returned from Postgres; nothing to do.")
        return

    rows_out = []
    nomad_rows_out = []
    consul_rows_out = []
    docker_rows_out = []
    previous_docker_counts = load_previous_docker_counts(args.docker_out) if args.docker_out else {}

    # Prepare SSH auth
    pkey = load_pkey(args.key, args.passphrase) if args.key else None
    jump_client = None
    jump_pkey = load_pkey(args.jump_key, args.jump_passphrase) if args.jump_key else None
    if args.jump_host:
        jump_client = connect_ssh(
            args.jump_host,
            args.jump_user or args.user,
            port=args.jump_port,
            pkey=jump_pkey or pkey,
            password=(None if (jump_pkey or pkey) else args.password),
            timeout=args.ssh_timeout
        )

    def gather_cluster_rows(meta, exec_fn):
        local_nomad = []
        local_consul = []
        try:
            nomad_text = exec_fn("nomad node status --verbose")
            parsed_nomad = parse_nomad_nodes(nomad_text)
            nomad_hash = compute_view_hash(parsed_nomad, ["NodeID", "Name", "Address", "Status", "Eligibility", "Drain"])
            for item in parsed_nomad:
                local_nomad.append({
                    "SourceName": meta["Name"],
                    "SourceHost": meta["Host"],
                    "SourceUID": meta["UID"],
                    "ViewHash": nomad_hash,
                    "NodeID": item["NodeID"],
                    "NodePool": item["NodePool"],
                    "DC": item["DC"],
                    "Name": item["Name"],
                    "Class": item["Class"],
                    "Address": item["Address"],
                    "Version": item["Version"],
                    "Drain": item["Drain"],
                    "Eligibility": item["Eligibility"],
                    "Status": item["Status"],
                    "CollectionError": "",
                })
        except Exception as exc:
            local_nomad.append({
                "SourceName": meta["Name"],
                "SourceHost": meta["Host"],
                "SourceUID": meta["UID"],
                "ViewHash": "",
                "NodeID": "",
                "NodePool": "",
                "DC": "",
                "Name": "",
                "Class": "",
                "Address": "",
                "Version": "",
                "Drain": "",
                "Eligibility": "",
                "Status": "ERROR",
                "CollectionError": str(exc),
            })
        try:
            consul_text = exec_fn("consul members")
            parsed_consul = parse_consul_members(consul_text)
            consul_hash = compute_view_hash(parsed_consul, ["Node", "Address", "Status", "Type", "Build"])
            for item in parsed_consul:
                local_consul.append({
                    "SourceName": meta["Name"],
                    "SourceHost": meta["Host"],
                    "SourceUID": meta["UID"],
                    "ViewHash": consul_hash,
                    "Node": item["Node"],
                    "Address": item["Address"],
                    "Status": item["Status"],
                    "Type": item["Type"],
                    "Build": item["Build"],
                    "Protocol": item["Protocol"],
                    "DC": item["DC"],
                    "Partition": item["Partition"],
                    "Segment": item["Segment"],
                    "CollectionError": "",
                })
        except Exception as exc:
            local_consul.append({
                "SourceName": meta["Name"],
                "SourceHost": meta["Host"],
                "SourceUID": meta["UID"],
                "ViewHash": "",
                "Node": "",
                "Address": "",
                "Status": "ERROR",
                "Type": "",
                "Build": "",
                "Protocol": "",
                "DC": "",
                "Partition": "",
                "Segment": "",
                "CollectionError": str(exc),
            })
        return local_nomad, local_consul

    for s in servers:
        host = s.get("default_ipaddr") or s.get("public_ipaddr")
        if not host:
            continue

        meta = {
            "Name": s.get("name") or "",
            "Host": host,
            "Status": "OK",
            "UID": s.get("uid"),
            "Connected": s.get("connected"),
            "MainDB": s.get("main_db"),
            "RunningVersion": s.get("running_version"),
            "PublicIP": s.get("public_ipaddr") or "",
        }

        client = None
        try:
            exec_fn = None
            if jump_client and args.via_main_db_host and not s.get("main_db"):
                exec_fn = lambda cmd, h=host: target_exec_text_via_main_db(
                    jump_client,
                    args.via_main_db_host,
                    args.via_main_db_user or args.user,
                    h,
                    args.user,
                    cmd,
                    target_sudo=args.sudo,
                    jump_run_as_user=args.jump_run_as_user,
                    timeout=args.ssh_timeout
                )
                m = gather_metrics(None, exec_text=exec_fn)
            elif jump_client and args.via_main_db_host and s.get("main_db"):
                exec_fn = lambda cmd: main_db_exec_text_via_jump(
                    jump_client,
                    args.via_main_db_host,
                    args.via_main_db_user or args.user,
                    cmd,
                    target_sudo=args.sudo,
                    jump_run_as_user=args.jump_run_as_user,
                    timeout=args.ssh_timeout
                )
                m = gather_metrics(None, exec_text=exec_fn)
            elif jump_client and not s.get("main_db"):
                exec_fn = lambda cmd, h=host: jump_exec_text(
                    jump_client, h, args.user, cmd,
                    target_sudo=args.sudo,
                    jump_run_as_user=args.jump_run_as_user,
                    timeout=args.ssh_timeout
                )
                m = gather_metrics(None, exec_text=exec_fn)
            elif jump_client and s.get("main_db"):
                exec_fn = lambda cmd: ssh_exec_text(jump_client, as_root_command(cmd, use_sudo=args.sudo))
                m = gather_metrics(jump_client, exec_text=exec_fn)
            else:
                client = connect_ssh(
                    host, args.user, port=args.port, pkey=pkey,
                    password=(None if pkey else args.password),
                    timeout=args.ssh_timeout
                )
                exec_fn = lambda cmd: ssh_exec_text(client, as_root_command(cmd, use_sudo=args.sudo))
                m = gather_metrics(client, exec_text=exec_fn)
                client.close()

            row = {
                "Name": meta["Name"],
                "Host": meta["Host"],
                "Status": meta["Status"],
                "UID": meta["UID"],
                "Connected": meta["Connected"],
                "MainDB": meta["MainDB"],
                "RunningVersion": meta["RunningVersion"],
                "PublicIP": meta["PublicIP"],

                "UptimeSeconds": m["UptimeSeconds"],
                "Load1": f"{float(m['Load1']):.2f}" if m["Load1"] is not None else "",
                "Load5": f"{float(m['Load5']):.2f}" if m["Load5"] is not None else "",
                "Load15": f"{float(m['Load15']):.2f}" if m["Load15"] is not None else "",

                "MemTotalGB": fmt_gb(m["MemTotalBytes"]),
                "MemUsedGB":  fmt_gb(m["MemUsedBytes"]),
                "MemUsedPct": fmt_pct(m["MemUsedPct"]),

                "RootDiskSizeGB": fmt_gb(m["DiskRootTotalBytes"]),
                "RootDiskUsedGB": fmt_gb(m["DiskRootUsedBytes"]),
                "RootDiskUsedPct": fmt_pct(m["DiskRootUsedPct"]),

                "DataPoolSizeGB": fmt_gb(m["DiskCteraTotalBytes"]),
                "DataPoolUsedGB": fmt_gb(m["DiskCteraUsedBytes"]),
                "DataPoolUsedPct": fmt_pct(m["DiskCteraUsedPct"]),

                "DBArchivePoolSizeGB": fmt_gb(m["DiskDbArchiveTotalBytes"]),
                "DBArchivePoolUsedGB": fmt_gb(m["DiskDbArchiveUsedBytes"]),
                "DBArchivePoolUsedPct": fmt_pct(m["DiskDbArchiveUsedPct"]),

                "CPUUserPct":     fmt_pct(m["CPUUserPct"]),
                "CPUSystemPct":   fmt_pct(m["CPUSystemPct"]),
                "CPUIOWaitPct":   fmt_pct(m["CPUIOWaitPct"]),
                "CPUIDLEPct":     fmt_pct(m["CPUIDLEPct"]),
            }
            rows_out.append(row)
            meta["HostUptimeSeconds"] = m["UptimeSeconds"]
            cluster_nomad_rows, cluster_consul_rows = gather_cluster_rows(meta, exec_fn)
            nomad_rows_out.extend(cluster_nomad_rows)
            consul_rows_out.extend(cluster_consul_rows)
            if args.docker_out:
                docker_rows_out.extend(gather_docker_rows(meta, exec_fn, previous_docker_counts))

        except Exception as e:
            try:
                if client:
                    client.close()
            except Exception:
                pass
            row = {
                "Name": meta["Name"],
                "Host": meta["Host"],
                "Status": f"SSH_ERROR: {e.__class__.__name__}",
                "UID": meta["UID"],
                "Connected": meta["Connected"],
                "MainDB": meta["MainDB"],
                "RunningVersion": meta["RunningVersion"],
                "PublicIP": meta["PublicIP"],
            }
            rows_out.append(row)
            nomad_rows_out.append({
                "SourceName": meta["Name"],
                "SourceHost": meta["Host"],
                "SourceUID": meta["UID"],
                "ViewHash": "",
                "NodeID": "",
                "NodePool": "",
                "DC": "",
                "Name": "",
                "Class": "",
                "Address": "",
                "Version": "",
                "Drain": "",
                "Eligibility": "",
                "Status": "ERROR",
                "CollectionError": str(e),
            })
            consul_rows_out.append({
                "SourceName": meta["Name"],
                "SourceHost": meta["Host"],
                "SourceUID": meta["UID"],
                "ViewHash": "",
                "Node": "",
                "Address": "",
                "Status": "ERROR",
                "Type": "",
                "Build": "",
                "Protocol": "",
                "DC": "",
                "Partition": "",
                "Segment": "",
                "CollectionError": str(e),
            })
            if args.docker_out:
                docker_rows_out.append({
                    "SourceName": meta["Name"],
                    "SourceHost": meta["Host"],
                    "SourceUID": meta["UID"],
                    "HostUptimeSeconds": "",
                    "RecentlyBooted": "",
                    "GraceState": "",
                    "ContainerID": "",
                    "ContainerName": "",
                    "Image": "",
                    "State": "ERROR",
                    "Health": "",
                    "RestartCount": "",
                    "RestartDelta": "",
                    "RestartPolicy": "",
                    "StartedAt": "",
                    "FinishedAt": "",
                    "StatusText": "",
                    "CollectionError": str(e),
                })

    # Write CSV
    headers = [
        "Name","Host","Status","UID","Connected","MainDB","RunningVersion","PublicIP",
        "UptimeSeconds","Load1","Load5","Load15",
        "MemTotalGB","MemUsedGB","MemUsedPct",
        "RootDiskSizeGB","RootDiskUsedGB","RootDiskUsedPct",
        "DataPoolSizeGB","DataPoolUsedGB","DataPoolUsedPct",
        "DBArchivePoolSizeGB","DBArchivePoolUsedGB","DBArchivePoolUsedPct",
        "CPUUserPct","CPUSystemPct","CPUIOWaitPct","CPUIDLEPct"
    ]

    tmp = args.out + ".tmp"
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(tmp, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=headers)
        w.writeheader()
        for r in rows_out:
            w.writerow({h: r.get(h, "") for h in headers})
    os.replace(tmp, args.out)

    if args.nomad_out:
        nomad_headers = [
            "SourceName","SourceHost","SourceUID","ViewHash","NodeID","NodePool","DC","Name","Class",
            "Address","Version","Drain","Eligibility","Status","CollectionError"
        ]
        tmp = args.nomad_out + ".tmp"
        os.makedirs(os.path.dirname(args.nomad_out), exist_ok=True)
        with open(tmp, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=nomad_headers)
            w.writeheader()
            for r in nomad_rows_out:
                w.writerow({h: r.get(h, "") for h in nomad_headers})
        os.replace(tmp, args.nomad_out)

    if args.consul_out:
        consul_headers = [
            "SourceName","SourceHost","SourceUID","ViewHash","Node","Address","Status","Type","Build",
            "Protocol","DC","Partition","Segment","CollectionError"
        ]
        tmp = args.consul_out + ".tmp"
        os.makedirs(os.path.dirname(args.consul_out), exist_ok=True)
        with open(tmp, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=consul_headers)
            w.writeheader()
            for r in consul_rows_out:
                w.writerow({h: r.get(h, "") for h in consul_headers})
        os.replace(tmp, args.consul_out)

    if args.docker_out:
        docker_headers = [
            "SourceName","SourceHost","SourceUID","HostUptimeSeconds","RecentlyBooted","GraceState",
            "ContainerID","ContainerName","Image","State","Health","RestartCount","RestartDelta",
            "RestartPolicy","StartedAt","FinishedAt","StatusText","CollectionError"
        ]
        tmp = args.docker_out + ".tmp"
        os.makedirs(os.path.dirname(args.docker_out), exist_ok=True)
        with open(tmp, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=docker_headers)
            w.writeheader()
            for r in docker_rows_out:
                w.writerow({h: r.get(h, "") for h in docker_headers})
        os.replace(tmp, args.docker_out)

    if jump_client:
        jump_client.close()

    print(f"Wrote {len(rows_out)} rows to {args.out}")

if __name__ == "__main__":
    main()

