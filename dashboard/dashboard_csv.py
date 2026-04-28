#!/usr/bin/env python3
# dashboard_csv.py — Edge + Portal + Postgres (with sub-tabs) + Servers Health
# VERSION: 2025-11-20 r10 (AI summary styled + bugfix)

import os, csv, re, base64, mimetypes, subprocess, shlex, sqlite3, smtplib, ssl
import paramiko
from flask import Flask, render_template_string, jsonify, request, session, redirect, url_for
import yaml
from datetime import datetime
import json
from collections import Counter
from email.message import EmailMessage
from werkzeug.security import generate_password_hash
from werkzeug.security import check_password_hash

from openai import OpenAI

APP_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.abspath(os.path.join(APP_DIR, ".."))
CONF_PATH = os.path.join(APP_DIR, "config.yaml")
API_KEY_FILE = os.path.join(APP_DIR, "openai_key.txt")
VERSION_FILE = os.path.join(PROJECT_DIR, "VERSION")
PRODUCT_NAME = "CTERA Monitoring Dashboard"
DEFAULT_DATA_DIR = os.environ.get("FEATHERDASH_DATA_DIR", os.path.join(PROJECT_DIR, "data"))
DEFAULT_DB_DIR = os.environ.get("FEATHERDASH_DB_DIR", os.path.join(DEFAULT_DATA_DIR, "db"))
DEFAULT_LOG_DIR = os.environ.get("FEATHERDASH_LOG_DIR", "/var/log/ctera-monitoring-dashboard")
DEFAULT_STATE_DIR = os.environ.get("FEATHERDASH_STATE_DIR", os.path.join(PROJECT_DIR, "state"))
DEFAULT_CONFIG_FILE = os.environ.get("FEATHERDASH_CONFIG_FILE", "/etc/ctera-monitoring-dashboard.env")
DEFAULT_HIDDEN_TASK_NAMES = {"csrequestsprocessor", "csrrequestsprocessor"}
JOB_NAMES = ("portal", "filer")


def _data_path(filename):
    return os.path.join(DEFAULT_DATA_DIR, filename)


def _log_path(job_name):
    return os.path.join(DEFAULT_LOG_DIR, f"{job_name}.log")


def _state_dir():
    os.makedirs(DEFAULT_STATE_DIR, exist_ok=True)
    return DEFAULT_STATE_DIR


def _runtime_env_dir():
    path = os.path.join(_state_dir(), "runtime_env")
    os.makedirs(path, exist_ok=True)
    return path


def _bootstrap_key_dir():
    path = os.path.join(_state_dir(), "ssh_keys")
    os.makedirs(path, exist_ok=True)
    return path


def _slugify(value):
    return re.sub(r"[^A-Za-z0-9._-]+", "-", str(value or "")).strip("-").lower() or "environment"


def _shell_quote(value):
    return shlex.quote(str(value or ""))


def _env_quote_line(value):
    value = str(value or "").replace("'", "'\\''")
    return f"'{value}'"


def _job_state_path(job_name):
    return os.path.join(_state_dir(), f"{job_name}_job_state.json")


def _load_app_version():
    try:
        with open(VERSION_FILE, "r", encoding="utf-8") as handle:
            version = handle.read().strip()
            if version:
                return version
    except Exception:
        pass
    return "1a"


APP_VERSION = _load_app_version()


def load_openai_api_key():
    # 1) environment variable (takes priority if set)
    key = os.environ.get("OPENAI_API_KEY")
    if key:
        return key.strip()

    # 2) fallback: openai_key.txt next to this file
    try:
        with open(API_KEY_FILE, "r") as f:
            return f.read().strip()
    except FileNotFoundError:
        return None


OPENAI_API_KEY = load_openai_api_key()
client = OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None

DEFAULT_CONF = {
    "csv_path": _data_path("filer.csv"),
    "tenants_csv": _data_path("tenants.csv"),
    "refresh_seconds": 0,
    "derive_cpu_mem": True,
    "thresholds": {},
    "thresholds_from": {},   # e.g., { path: ../thresholds.yaml }
    "ui": {
        "clip_by_default": False,
        "clip_columns": [],
        "max_cell_px": 360
    },
    "theme": {
        "preset": "",
        "primary": "#111827",
        "accent":  "#2563EB",
        "bg":      "#FFFFFF",
        "surface": "#FFFFFF",
        "text":    "#111827",
        "muted":   "#6B7280",
        "border":  "#E5E7EB",
        "header":  "#F3F4F6",
        "hover":   "#FAFAFA"
    },
    "brand": {
        "title": "CTERA Dashboard",
        "logo_path": "",
        "logo_height": 40
    },
    "portal": {
        "servers_csv": _data_path("servers.csv"),
        "storage_csv": _data_path("storage.csv"),
        "tasks_csv": _data_path("tasks.csv"),
        "licenses_csv": os.path.join(DEFAULT_DB_DIR, "licenses.csv")
    },
    "postgres": {
        "base_dir": DEFAULT_DB_DIR,
        "topics": {
            "long_running_queries": "long_running_queries.csv",
            "wraparound_database": "wraparound_database.csv",
            "wraparound_top_tables": "wraparound_top_tables.csv",
            "wraparound_summary": "wraparound_summary.csv",
            "vacuum_analyze_stats": "vacuum_analyze_stats.csv",
            "table_sizes": "table_sizes.csv",
            "table_bloat": "table_bloat.csv",
            "index_bloat": "index_bloat.csv"
        }
    },
    "servers_health": {
        "metrics_csv": _data_path("server_metrics.csv"),
        "nomad_csv": _data_path("nomad_nodes.csv"),
        "consul_csv": _data_path("consul_members.csv"),
    }
}

# ---------------- config / theme / brand ----------------
def _abspath_from_app(path):
    if not path or os.path.isabs(path):
        return path
    return os.path.abspath(os.path.join(APP_DIR, path))


def load_conf():
    try:
        with open(CONF_PATH, "r") as f:
            cfg = yaml.safe_load(f) or {}
    except Exception:
        cfg = {}
    base = dict(DEFAULT_CONF)
    for k, v in cfg.items():
        if k in ("ui", "theme", "brand", "portal", "postgres", "servers_health") and isinstance(v, dict):
            base[k] = {**base.get(k, {}), **v}
        else:
            base[k] = v
    data_dir = os.environ.get("FEATHERDASH_DATA_DIR")
    if data_dir:
        db_dir = os.environ.get("FEATHERDASH_DB_DIR", os.path.join(data_dir, "db"))
        base["csv_path"] = os.path.join(data_dir, "filer.csv")
        base["tenants_csv"] = os.path.join(data_dir, "tenants.csv")
        base["portal"] = {
            **base.get("portal", {}),
            "servers_csv": os.path.join(data_dir, "servers.csv"),
            "storage_csv": os.path.join(data_dir, "storage.csv"),
            "tasks_csv": os.path.join(data_dir, "tasks.csv"),
            "licenses_csv": os.path.join(db_dir, "licenses.csv"),
        }
        base["postgres"] = {**base.get("postgres", {}), "base_dir": db_dir}
        base["servers_health"] = {
            **base.get("servers_health", {}),
            "metrics_csv": os.path.join(data_dir, "server_metrics.csv"),
            "nomad_csv": os.path.join(data_dir, "nomad_nodes.csv"),
            "consul_csv": os.path.join(data_dir, "consul_members.csv"),
        }
    base["csv_path"] = _abspath_from_app(base.get("csv_path"))
    base["tenants_csv"] = _abspath_from_app(base.get("tenants_csv"))
    portal = base.get("portal", {})
    for key in ("servers_csv", "storage_csv", "tasks_csv", "licenses_csv"):
        portal[key] = _abspath_from_app(portal.get(key))
    base["portal"] = portal
    # fix postgres base dir
    pg = base.get("postgres", {})
    bdir = pg.get("base_dir", DEFAULT_CONF["postgres"]["base_dir"])
    if not os.path.isabs(bdir):
        pg["base_dir"] = os.path.abspath(os.path.join(APP_DIR, bdir))
    base["postgres"] = pg
    # logo path
    b = base.get("brand") or {}
    lp = b.get("logo_path")
    if lp and not os.path.isabs(lp):
        b["logo_path"] = os.path.join(APP_DIR, lp)
    base["brand"] = b
    sh = base.get("servers_health") or {}
    sh["metrics_csv"] = _abspath_from_app(sh.get("metrics_csv"))
    sh["nomad_csv"] = _abspath_from_app(sh.get("nomad_csv"))
    sh["consul_csv"] = _abspath_from_app(sh.get("consul_csv"))
    base["servers_health"] = sh
    return base


def load_conf_for_environment(env_id=None):
    cfg = load_conf()
    if not env_id:
        return cfg
    env = _get_environment(env_id, include_secret=False)
    if not env:
        return cfg
    data_dir = _environment_data_dir(env)
    db_dir = _environment_db_dir(env)
    os.makedirs(data_dir, exist_ok=True)
    os.makedirs(db_dir, exist_ok=True)
    cfg["csv_path"] = os.path.join(data_dir, "filer.csv")
    cfg["tenants_csv"] = os.path.join(data_dir, "tenants.csv")
    cfg["portal"] = {
        **(cfg.get("portal") or {}),
        "servers_csv": os.path.join(data_dir, "servers.csv"),
        "storage_csv": os.path.join(data_dir, "storage.csv"),
        "tasks_csv": os.path.join(data_dir, "tasks.csv"),
        "licenses_csv": os.path.join(db_dir, "licenses.csv"),
    }
    cfg["postgres"] = {**(cfg.get("postgres") or {}), "base_dir": db_dir}
    cfg["servers_health"] = {
        **(cfg.get("servers_health") or {}),
        "metrics_csv": os.path.join(data_dir, "server_metrics.csv"),
        "nomad_csv": os.path.join(data_dir, "nomad_nodes.csv"),
        "consul_csv": os.path.join(data_dir, "consul_members.csv"),
    }
    return cfg


def resolve_theme(cfg):
    tcfg = cfg.get("theme") or {}
    ctera = dict(
        primary="#5B5BD6", accent="#00BCD4",
        bg="#F7F8FB", surface="#FFFFFF", text="#111827", muted="#6B7280",
        border="#E5E7EB", header="#F3F4F6", hover="#FAFAFA"
    )
    preset = (tcfg.get("preset") or "").lower()
    base = ctera if preset == "ctera" else {
        "primary": "#111827", "accent": "#2563EB",
        "bg": "#FFFFFF", "surface": "#FFFFFF", "text": "#111827", "muted": "#6B7280",
        "border": "#E5E7EB", "header": "#F3F4F6", "hover": "#FAFAFA"
    }
    return {**base, **{k: v for k, v in tcfg.items() if k != "preset"}}


# --- helper: file modified time (UTC) ---
def _file_mtime_utc(path_like):
    try:
        p = path_like or ''
        if not p:
            return '—'
        if not os.path.isabs(p):
            p = os.path.join(APP_DIR, p)
        ts = os.path.getmtime(p)
        return datetime.utcfromtimestamp(ts).strftime('%Y-%m-%d %H:%M:%S UTC')
    except Exception:
        return '—'


def _file_mtime_iso(path_like):
    try:
        p = path_like or ''
        if not p:
            return ''
        if not os.path.isabs(p):
            p = os.path.join(APP_DIR, p)
        ts = os.path.getmtime(p)
        return datetime.utcfromtimestamp(ts).replace(microsecond=0).isoformat() + "Z"
    except Exception:
        return ''


def _now_utc_iso():
    return datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


def _state_dir():
    os.makedirs(DEFAULT_STATE_DIR, exist_ok=True)
    return DEFAULT_STATE_DIR


def _job_state_path(job_name):
    return os.path.join(_state_dir(), f"{job_name}.state")


def _log_path(job_name):
    return os.path.join(DEFAULT_LOG_DIR, f"{job_name}.log")


def _write_state(job_name, values):
    path = _job_state_path(job_name)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        for key, value in values.items():
            f.write(f"{key}={value}\n")
    os.replace(tmp, path)


def _read_state(job_name):
    path = _job_state_path(job_name)
    out = {}
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                if "=" in line:
                    key, value = line.rstrip("\n").split("=", 1)
                    out[key] = value
    except Exception:
        pass
    return out


def _tail_text(path, max_lines=12):
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            lines = f.readlines()
        return "".join(lines[-max_lines:]).strip()
    except Exception:
        return ""


def _job_status(job_name):
    state = _read_state(job_name)
    log_path = _log_path(job_name)
    status = state.get("status", "idle")
    pid = state.get("pid", "")
    if status == "running" and pid:
        try:
            os.kill(int(pid), 0)
        except Exception:
            status = "unknown"
            state["status"] = status
            _write_state(job_name, state)
    return {
        "job": job_name,
        "status": status,
        "started_at": state.get("started_at", ""),
        "finished_at": state.get("finished_at", ""),
        "last_exit": state.get("last_exit", ""),
        "pid": state.get("pid", ""),
        "log_path": log_path,
        "tail_command": f"tail -F {log_path}",
        "last_log_update": _file_mtime_utc(log_path),
        "tail": _tail_text(log_path),
    }


def _launch_job(job_name, environment_id=None):
    if job_name not in JOB_NAMES:
        raise ValueError("Unknown job")
    current = _job_status(job_name)
    if current["status"] == "running":
        return current, False
    if not environment_id or str(environment_id) == "admin":
        raise ValueError("Select a portal environment before running collectors.")
    env = _bootstrap_environment_runtime(environment_id)
    runtime_env_file = _write_runtime_env_file(env)

    script = os.path.join(PROJECT_DIR, f"{job_name}_jobs.sh")
    log_path = _log_path(job_name)
    state_path = _job_state_path(job_name)
    started_at = _now_utc_iso()
    _write_state(job_name, {
        "status": "running",
        "started_at": started_at,
        "finished_at": "",
        "last_exit": "",
        "pid": "",
    })
    wrapper = (
        f"FEATHERDASH_CONFIG={shlex.quote(runtime_env_file)} {shlex.quote(script)} >> {shlex.quote(log_path)} 2>&1; "
        "rc=$?; "
        f"cat > {shlex.quote(state_path)} <<EOF\n"
        f"status=$([ \"$rc\" -eq 0 ] && echo finished || echo failed)\n"
        f"started_at={started_at}\n"
        "finished_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)\n"
        "last_exit=$rc\n"
        "pid=\n"
        "EOF\n"
    )
    proc = subprocess.Popen(
        ["/usr/bin/env", "bash", "-lc", wrapper],
        cwd=PROJECT_DIR,
        start_new_session=True,
    )
    _write_state(job_name, {
        "status": "running",
        "started_at": started_at,
        "finished_at": "",
        "last_exit": "",
        "pid": str(proc.pid),
    })
    return _job_status(job_name), True


def resolve_brand(cfg):
    b = cfg.get("brand") or {}
    title = b.get("title") or DEFAULT_CONF["brand"]["title"]
    logo_height = int(b.get("logo_height", 40))
    logo_data_uri = None
    path = b.get("logo_path")
    if path and os.path.exists(path):
        mime, _ = mimetypes.guess_type(path)
        if not mime:
            ext = os.path.splitext(path)[1].lower()
            mime = "image/png" if ext == ".png" else ("image/svg+xml" if ext == ".svg" else "image/jpeg")
        with open(path, "rb") as f:
            logo_data_uri = f"data:{mime};base64,{base64.b64encode(f.read()).decode('ascii')}"
    return {"title": title, "logo": logo_data_uri, "icon": logo_data_uri, "logo_height": logo_height}


# ---------------- CSV helpers ----------------
def _clean_header(header):
    h = str(header or "").strip()
    h = h.lstrip("\ufeff").replace("ï»¿", "").strip()
    if h.startswith("?") and h[1:].lower() in {"tenant", "name", "status"}:
        h = h[1:]
    return h


def read_csv_rows(csv_path):
    rows, headers = [], []
    if not csv_path or not os.path.exists(csv_path):
        return rows, headers
    with open(csv_path, newline='', encoding='utf-8', errors='ignore') as f:
        reader = csv.DictReader(f)
        raw_headers = reader.fieldnames or []
        headers = [_clean_header(h) for h in raw_headers]
        for r in reader:
            cleaned = {}
            for raw, clean in zip(raw_headers, headers):
                cleaned[clean] = r.get(raw, "")
            rows.append(cleaned)
    return rows, headers


def derive_fields(rows, headers, cfg):
    if cfg.get("derive_cpu_mem") and "Current Performance" in headers:
        cpu_key = "CPU_Current"
        mem_key = "Mem_Current"
        if cpu_key not in headers:
            headers.append(cpu_key)
        if mem_key not in headers:
            headers.append(mem_key)
        pat = re.compile(r"CPU:\s*([0-9]+)%\s*Mem:\s*([0-9]+)%", re.I)
        for r in rows:
            perf = (r.get("Current Performance") or "").strip()
            m = pat.search(perf)
            r[cpu_key] = m.group(1) if m else ""
            r[mem_key] = m.group(2) if m else ""
    return rows, headers


def display_cell(header, value):
    h = str(header or "").strip().lower()
    if h == "tenant":
        text = str(value or "").strip()
        if "/" in text:
            parts = [p for p in text.split("/") if p]
            if parts:
                return parts[-1]
        return text
    if _looks_like_duration_header(h):
        return format_duration(value)
    if _looks_like_bytes_header(h):
        return format_bytes(value)
    if _looks_like_percent_header(h):
        return format_percent(value)
    return value


def display_header(header):
    h = str(header or "").strip().lower()
    if h == "elapsedseconds":
        return "Elapsed"
    if h.endswith("seconds"):
        return re.sub(r"seconds$", "", str(header or ""), flags=re.I) or header
    return header


def format_duration(value):
    seconds = _safe_float(value)
    if seconds is None:
        return value
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    minutes = seconds / 60
    if minutes < 120:
        return f"{minutes:.1f} min"
    hours = minutes / 60
    if hours < 48:
        return f"{hours:.1f} hr"
    days = hours / 24
    return f"{days:.1f} days"


def format_bytes(value):
    number = _safe_float(value)
    if number is None:
        return value
    units = ["B", "KB", "MB", "GB", "TB", "PB"]
    size = float(number)
    unit_idx = 0
    while abs(size) >= 1024 and unit_idx < len(units) - 1:
        size /= 1024.0
        unit_idx += 1
    if unit_idx == 0:
        return f"{int(size)} {units[unit_idx]}"
    return f"{size:.1f} {units[unit_idx]}"


def format_percent(value):
    text = str(value or "").strip()
    if not text:
        return value
    if "%" in text:
        return text
    number = _safe_float(value)
    if number is None:
        return value
    return f"{number:.1f}%"


def _looks_like_duration_header(header_lower):
    if not header_lower:
        return False
    return header_lower == "elapsedseconds" or header_lower.endswith("seconds") or header_lower.endswith("_seconds")


def _looks_like_bytes_header(header_lower):
    if not header_lower:
        return False
    return (
        "bytes" in header_lower
        or header_lower.endswith("_bytes")
        or header_lower in {"cloudsyncdbsize", "cloudsync.db size"}
    )


def _looks_like_percent_header(header_lower):
    if not header_lower:
        return False
    return (
        header_lower.endswith("pct")
        or header_lower.endswith("percentage")
        or header_lower.endswith("usedpct")
    )


def filter_dashboard_tasks(rows):
    filtered = []
    for row in rows:
        task_name = str(row.get("TaskName", "")).strip().lower()
        normalized = re.sub(r"[^a-z0-9]+", "", task_name)
        if normalized in DEFAULT_HIDDEN_TASK_NAMES:
            continue
        filtered.append(row)
    return filtered


# ---------------- threshold machinery ----------------
_TRUTHY = {"true", "1", "yes", "y", "on"}
_FALSY = {"false", "0", "no", "n", "off", ""}


def _boolish(x):
    s = str(x).strip().lower()
    if s in _TRUTHY:
        return True
    if s in _FALSY:
        return False
    return None


def _num(x):
    if x is None:
        return None
    import re as _re
    m = _re.search(r"-?\d+(?:\.\d+)?", str(x))
    return float(m.group(0)) if m else None


def eval_rule(val, rule):
    if not isinstance(rule, dict):
        return False
    for op, thr in rule.items():
        op = str(op).lower()
        if op == "style":  # ignore style for warn evaluation
            continue
        if op in ("eq", "ne"):
            thrb = thr if isinstance(thr, bool) else _boolish(thr)
            vb = _boolish(val)
            if thrb is not None and vb is not None:
                same = (vb == thrb)
                return same if op == "eq" else (not same)
            s_thr = str(thr).strip().lower()
            s_val = str(val).strip().lower()
            same = (s_val == s_thr)
            return same if op == "eq" else (not same)
        if op in ("gt", "ge", "lt", "le"):
            vn = _num(val)
            tn = _num(thr)
            if vn is None or tn is None:
                continue
            if (op == "gt" and vn > tn) or (op == "ge" and vn >= tn) \
               or (op == "lt" and vn < tn) or (op == "le" and vn <= tn):
                return True
    return False


# ------------------------------
# Threshold evaluation (value-aware)
# ------------------------------
def _cmp_ok(val, op, rhs):
    """
    Single operator comparison for numeric or string.
    Operators supported: gt ge lt le eq ne
    """
    if op in ("eq", "ne"):
        rhs_bool = rhs if isinstance(rhs, bool) else _boolish(rhs)
        val_bool = _boolish(val)
        if rhs_bool is not None and val_bool is not None:
            same = (val_bool == rhs_bool)
            return same if op == "eq" else (not same)
        lhs = str(val).strip().lower()
        rhs_text = str(rhs).strip().lower()
        same = (lhs == rhs_text)
        return same if op == "eq" else (not same)

    # numeric comparisons: coerce both sides
    def _ton(x):
        if x is None:
            return None
        if isinstance(x, (int, float)):
            return x
        s = str(x).strip().replace(",", "")
        if s == "":
            return None
        try:
            return float(s) if "." in s else int(s)
        except Exception:
            return None

    lv = _ton(val)
    rv = _ton(rhs)
    if lv is None or rv is None:
        return False

    if op == "gt":
        return lv > rv
    if op == "ge":
        return lv >= rv
    if op == "lt":
        return lv < rv
    if op == "le":
        return lv <= rv
    return False


def _all_ops_ok(value, rules_dict):
    """Return True only if all present operators match the given value."""
    for k in ("gt", "ge", "lt", "le", "eq", "ne"):
        if k in rules_dict and not _cmp_ok(value, k, rules_dict[k]):
            return False
    return True


def _style_from_rule(rule, val=None):
    """
    Returns a CSS class only when:
      1) the rule contains a valid 'style' (critical/warning/ok/muted/info), and
      2) either the rule has no comparison operators (always-on), or
         all of its comparison operators match the provided cell value.
    """
    if not isinstance(rule, dict):
        return ""
    style = (rule.get("style") or "").strip().lower()
    if style not in ("critical", "warning", "ok", "muted", "info"):
        return ""

    cmps = {k: rule[k] for k in ("gt", "ge", "lt", "le", "eq", "ne") if k in rule}
    if not cmps:
        return f"sev-{style}"
    return f"sev-{style}" if _all_ops_ok(val, cmps) else ""


def eval_level(val, rule):
    """
    Returns: 'bad' (critical) | 'warn' (warning) | '' (ok)
    Supports:
      - {crit:{...}, warn:{...}}
      - flat { ge: 90 } -> 'bad'
      - { ge: 80, style: "warn" }
    """
    if not isinstance(rule, dict) or not rule:
        return ''
    # explicit single-level style
    style = rule.get('style')
    base = {k: v for k, v in rule.items() if k in ('gt', 'ge', 'lt', 'le', 'eq', 'ne')}
    if style and base:
        return style if _all_ops_ok(val, base) else ''

    # nested levels
    if any(k in rule for k in ('crit', 'critical', 'warn', 'warning')):
        crit_rule = rule.get('crit') or rule.get('critical')
        if isinstance(crit_rule, dict) and _all_ops_ok(val, crit_rule):
            return 'bad'
        warn_rule = rule.get('warn') or rule.get('warning')
        if isinstance(warn_rule, dict) and _all_ops_ok(val, warn_rule):
            return 'warn'
        return ''

    # flat -> treat as critical
    return 'bad' if eval_rule(val, rule) else ''


# -------- rule resolvers (edge / portal / postgres / servers health / tenants)
def make_edge_warn_fn(base_cfg, ext):
    def _resolve(row):
        eff = {}
        if isinstance(base_cfg, dict):
            eff.update(base_cfg)
        if isinstance(ext, dict):
            eff.update(ext.get("default", {}) or {})

            filers = (ext.get("filers") or {})
            eff.update(filers.get("default", {}) or {})
            eff.update(filers.get(row.get("Filer Name", ""), {}) or {})

            tenants = (ext.get("tenants") or {})
            eff.update(tenants.get(row.get("Tenant", ""), {}) or {})
        return eff

    def warn(col, val, row):
        rule = _resolve(row).get(col)
        return eval_level(val, rule) if rule else ''

    return warn


def make_edge_style_fn(base_cfg, ext):
    def _resolve(row):
        eff = {}
        if isinstance(base_cfg, dict):
            eff.update(base_cfg)
        if isinstance(ext, dict):
            eff.update(ext.get("default", {}) or {})

            filers = (ext.get("filers") or {})
            eff.update(filers.get("default", {}) or {})
            eff.update(filers.get(row.get("Filer Name", ""), {}) or {})

            tenants = (ext.get("tenants") or {})
            eff.update(tenants.get(row.get("Tenant", ""), {}) or {})
        return eff

    def style(col, val, row):
        rule = _resolve(row).get(col)
        return _style_from_rule(rule, val)

    return style


def make_portal_warn_fn(ext, section):
    def _rules(row):
        eff = {}
        ignores = set()
        portal = (ext.get("portal") or {}) if isinstance(ext, dict) else {}
        sec = portal.get(section) or {}
        if isinstance(sec, dict):
            eff.update(sec.get("default", {}) or {})
            eff.update((sec.get("by_name", {}) or {}).get(row.get("Name", ""), {}) or {})
            if section == 'tasks':
                try:
                    for s in (sec.get('ignore_tasknames') or []):
                        if isinstance(s, str) and s.strip():
                            ignores.add(s.strip())
                except Exception:
                    pass
        return eff, ignores

    def warn(col, val, row):
        rules, ignores = _rules(row)
        c = col.strip().lower()

        # TASKS: skip ignored names
        if section == 'tasks':
            tn = (row.get('TaskName') or '').strip()
            if tn and tn in ignores:
                return ''

        # TASKS: ElapsedSeconds only when running
        if section == 'tasks' and c == 'elapsedseconds':
            try:
                state = (row.get('State') or '').strip().lower()
                thr_rule = rules.get('ElapsedSeconds') or rules.get('elapsedseconds')
                if state == 'running' and thr_rule:
                    return eval_level(val, thr_rule) or ''
            except Exception:
                pass  # fallthrough

        # YAML rule for this column?
        rule = rules.get(col)
        if rule:
            return eval_level(val, rule) or ''

        # built-ins (backstop)
        if section == "servers" and c == "connected":
            return 'bad' if (_boolish(val) is not True) else ''
        if section == "tasks":
            try:
                state = (row.get('State') or '').strip().lower()
                elapsed = float(val) if c == 'elapsedseconds' and str(val).strip() else None
                if state == 'running' and elapsed is not None and elapsed >= 86400:
                    return 'bad'
            except Exception:
                pass
        return ''

    return warn


def make_portal_style_fn(ext, section):
    def _rules(row):
        eff = {}
        portal = (ext.get("portal") or {}) if isinstance(ext, dict) else {}
        sec = portal.get(section) or {}
        if isinstance(sec, dict):
            eff.update(sec.get("default", {}) or {})
            eff.update((sec.get("by_name", {}) or {}).get(row.get("Name", ""), {}) or {})
        return eff

    def style(col, val, row):
        rule = _rules(row).get(col)
        return _style_from_rule(rule, val)

    return style


def make_pg_warn_fn(ext):
    def _rules(topic, row):
        eff = {}
        pg = (ext.get("postgres") or {}) if isinstance(ext, dict) else {}
        sec = pg.get(topic) or {}
        if isinstance(sec, dict):
            eff.update(sec.get("default", {}) or {})
            eff.update((sec.get("by_db", {}) or {}).get(row.get("pg_db", ""), {}) or {})
            eff.update((sec.get("by_cluster", {}) or {}).get(row.get("cluster", ""), {}) or {})
            eff.update((sec.get("by_host", {}) or {}).get(row.get("pg_host", ""), {}) or {})
        return eff

    def warn(topic, col, val, row):
        rule = _rules(topic, row).get(col)
        return eval_level(val, rule) if rule else ''

    return warn


def make_pg_style_fn(ext):
    def _rules(topic, row):
        eff = {}
        pg = (ext.get("postgres") or {}) if isinstance(ext, dict) else {}
        sec = pg.get(topic) or {}
        if isinstance(sec, dict):
            eff.update(sec.get("default", {}) or {})
            eff.update((sec.get("by_db", {}) or {}).get(row.get("pg_db", ""), {}) or {})
            eff.update((sec.get("by_cluster", {}) or {}).get(row.get("cluster", ""), {}) or {})
            eff.update((sec.get("by_host", {}) or {}).get(row.get("pg_host", ""), {}) or {})
        return eff

    def style(topic, col, val, row):
        rule = _rules(topic, row).get(col)
        return _style_from_rule(rule, val)

    return style


def make_servers_health_warn_fn(ext):
    def _rules(row):
        eff = {}
        sec = (ext.get("servers_health") or {}) if isinstance(ext, dict) else {}
        if isinstance(sec, dict):
            eff.update(sec.get("default", {}) or {})
            eff.update((sec.get("by_host", {}) or {}).get(row.get("Host", ""), {}) or {})
            eff.update((sec.get("by_uid", {}) or {}).get(row.get("UID", ""), {}) or {})
        return eff

    def warn(col, val, row):
        rule = _rules(row).get(col)
        return eval_level(val, rule) if rule else ''

    return warn


def make_servers_health_style_fn(ext):
    def _rules(row):
        eff = {}
        sec = (ext.get("servers_health") or {}) if isinstance(ext, dict) else {}
        if isinstance(sec, dict):
            eff.update(sec.get("default", {}) or {})
            eff.update((sec.get("by_host", {}) or {}).get(row.get("Host", ""), {}) or {})
            eff.update((sec.get("by_uid", {}) or {}).get(row.get("UID", ""), {}) or {})
        return eff

    def style(col, val, row):
        rule = _rules(row).get(col)
        return _style_from_rule(rule, val)

    return style


def make_tenants_style_fn(ext):
    def _rules(row):
        eff = {}
        sec = (ext.get("tenants") or {}) if isinstance(ext, dict) else {}
        if isinstance(sec, dict):
            eff.update(sec.get("default", {}) or {})
            eff.update((sec.get("by_name", {}) or {}).get(row.get("Tenant", ""), {}) or {})
        return eff

    def style(col, val, row):
        rule = _rules(row).get(col)
        return _style_from_rule(rule, val)

    return style


def make_tenants_warn_fn(ext):
    def _rules(row):
        eff = {}
        sec = (ext.get("tenants") or {}) if isinstance(ext, dict) else {}
        if isinstance(sec, dict):
            eff.update(sec.get("default", {}) or {})
            eff.update((sec.get("by_name", {}) or {}).get(row.get("Tenant", ""), {}) or {})
        return eff

    def warn(col, val, row):
        rule = _rules(row).get(col)
        return eval_level(val, rule) if rule else ''

    return warn


def _thresholds_path(cfg):
    th_src = cfg.get("thresholds_from") or {}
    th_path = th_src.get("path") or os.environ.get("FEATHERDASH_THRESHOLDS") or os.path.join(PROJECT_DIR, "thresholds.yaml")
    if not os.path.isabs(th_path):
        th_path = os.path.abspath(os.path.join(APP_DIR, th_path))
    return th_path


def _load_external_thresholds(cfg):
    th_path = _thresholds_path(cfg)
    try:
        with open(th_path, "r", encoding="utf-8") as f:
            return (yaml.safe_load(f) or {}), th_path
    except FileNotFoundError:
        return {}, th_path
    except Exception:
        return {}, th_path


def _save_external_thresholds(cfg, doc):
    th_path = _thresholds_path(cfg)
    os.makedirs(os.path.dirname(th_path), exist_ok=True)
    with open(th_path, "w", encoding="utf-8") as f:
        yaml.safe_dump(doc or {}, f, sort_keys=False, allow_unicode=False)
    return th_path


_EMAIL_SETTING_DEFAULTS = {
    "smtp_host": "",
    "smtp_port": "587",
    "smtp_username": "",
    "smtp_password": "",
    "sender_name": "CTERA Monitoring Dashboard",
    "sender_email": "",
    "use_tls": "true",
    "use_ssl": "false",
}

_AUTH_SETTING_DEFAULTS = {
    "auth_mode": "none",
}

_THRESHOLD_NOTIFICATION_DEFAULTS = {
    "enabled": False,
    "severity": "critical",
    "repeat_minutes": 0,
    "recipient_mode": "all_enabled",
    "recipient_ids": [],
}


def _notifications_db_path():
    db_path = os.environ.get("FEATHERDASH_NOTIFICATIONS_DB", os.path.join(_state_dir(), "notifications.sqlite"))
    if not os.path.isabs(db_path):
        db_path = os.path.abspath(os.path.join(APP_DIR, db_path))
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    return db_path


def _notifications_conn():
    conn = sqlite3.connect(_notifications_db_path())
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    _ensure_notifications_db(conn)
    return conn


def _ensure_notifications_db(conn=None):
    own_conn = conn is None
    if own_conn:
        conn = sqlite3.connect(_notifications_db_path())
    try:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS email_settings (
                setting_key TEXT PRIMARY KEY,
                setting_value TEXT NOT NULL DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS app_settings (
                setting_key TEXT PRIMARY KEY,
                setting_value TEXT NOT NULL DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS recipients (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                display_name TEXT NOT NULL,
                email_address TEXT NOT NULL UNIQUE,
                enabled INTEGER NOT NULL DEFAULT 1,
                datasets TEXT NOT NULL DEFAULT '',
                severities TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS threshold_notifications (
                dataset_key TEXT NOT NULL,
                field_name TEXT NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 0,
                severity TEXT NOT NULL DEFAULT 'critical',
                repeat_minutes INTEGER NOT NULL DEFAULT 0,
                recipient_mode TEXT NOT NULL DEFAULT 'all_enabled',
                recipient_ids TEXT NOT NULL DEFAULT '[]',
                updated_at TEXT NOT NULL,
                PRIMARY KEY (dataset_key, field_name)
            );

            CREATE TABLE IF NOT EXISTS alert_state (
                alert_key TEXT PRIMARY KEY,
                dataset_key TEXT NOT NULL,
                row_key TEXT NOT NULL DEFAULT '',
                field_name TEXT NOT NULL,
                severity TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'active',
                first_seen TEXT NOT NULL,
                last_seen TEXT NOT NULL,
                last_emailed_at TEXT NOT NULL DEFAULT '',
                repeat_minutes INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS environments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                environment_name TEXT NOT NULL UNIQUE,
                portal_fqdn TEXT NOT NULL DEFAULT '',
                portal_ip TEXT NOT NULL DEFAULT '',
                ctera_username TEXT NOT NULL DEFAULT '',
                ctera_password TEXT NOT NULL DEFAULT '',
                main_db_ip TEXT NOT NULL DEFAULT '',
                jump_host_enabled INTEGER NOT NULL DEFAULT 0,
                main_db_via_jump_preconfigured INTEGER NOT NULL DEFAULT 0,
                jump_host TEXT NOT NULL DEFAULT '',
                main_db_jump_username TEXT NOT NULL DEFAULT '',
                jump_ssh_mode TEXT NOT NULL DEFAULT 'root_password',
                jump_ssh_username TEXT NOT NULL DEFAULT 'root',
                jump_ssh_key_path TEXT NOT NULL DEFAULT '',
                jump_ssh_password TEXT NOT NULL DEFAULT '',
                ssh_mode TEXT NOT NULL DEFAULT 'root_password',
                ssh_username TEXT NOT NULL DEFAULT 'root',
                ssh_key_path TEXT NOT NULL DEFAULT '',
                ssh_password TEXT NOT NULL DEFAULT '',
                sudo_required INTEGER NOT NULL DEFAULT 1,
                pg_host TEXT NOT NULL DEFAULT '',
                pg_port TEXT NOT NULL DEFAULT '5432',
                pg_database TEXT NOT NULL DEFAULT 'postgres',
                pg_user TEXT NOT NULL DEFAULT 'postgres',
                pg_password TEXT NOT NULL DEFAULT '',
                openai_key TEXT NOT NULL DEFAULT '',
                portal_schedule_minutes INTEGER NOT NULL DEFAULT 60,
                filer_schedule_minutes INTEGER NOT NULL DEFAULT 60,
                enabled INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS local_users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL UNIQUE,
                display_name TEXT NOT NULL DEFAULT '',
                password_hash TEXT NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            """
        )
        cols = {row["name"] for row in conn.execute("PRAGMA table_info(environments)").fetchall()}
        environment_additions = [
            ("jump_host_enabled", "INTEGER NOT NULL DEFAULT 0"),
            ("main_db_via_jump_preconfigured", "INTEGER NOT NULL DEFAULT 0"),
            ("jump_host", "TEXT NOT NULL DEFAULT ''"),
            ("main_db_jump_username", "TEXT NOT NULL DEFAULT ''"),
            ("jump_ssh_mode", "TEXT NOT NULL DEFAULT 'root_password'"),
            ("jump_ssh_username", "TEXT NOT NULL DEFAULT 'root'"),
            ("jump_ssh_key_path", "TEXT NOT NULL DEFAULT ''"),
            ("jump_ssh_password", "TEXT NOT NULL DEFAULT ''"),
        ]
        for col_name, col_def in environment_additions:
            if col_name not in cols:
                conn.execute(f"ALTER TABLE environments ADD COLUMN {col_name} {col_def}")
        conn.commit()
    finally:
        if own_conn:
            conn.close()


def _bool_setting(value, default=False):
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in _TRUTHY:
        return True
    if text in _FALSY:
        return False
    return default


def _load_email_settings(include_secret=False):
    out = dict(_EMAIL_SETTING_DEFAULTS)
    with _notifications_conn() as conn:
        rows = conn.execute("SELECT setting_key, setting_value FROM email_settings").fetchall()
    for row in rows:
        out[row["setting_key"]] = row["setting_value"]
    out["use_tls"] = _bool_setting(out.get("use_tls"), True)
    out["use_ssl"] = _bool_setting(out.get("use_ssl"), False)
    out["smtp_port"] = str(out.get("smtp_port") or "587")
    out["smtp_password_set"] = bool(out.get("smtp_password"))
    if not include_secret:
        out["smtp_password"] = ""
    return out


def _load_app_settings():
    out = dict(_AUTH_SETTING_DEFAULTS)
    with _notifications_conn() as conn:
        rows = conn.execute("SELECT setting_key, setting_value FROM app_settings").fetchall()
    for row in rows:
        out[row["setting_key"]] = row["setting_value"]
    out["auth_mode"] = str(out.get("auth_mode") or "none")
    return out


def _save_app_settings(payload):
    current = _load_app_settings()
    current["auth_mode"] = str(payload.get("auth_mode") or current.get("auth_mode") or "none").strip() or "none"
    if current["auth_mode"] not in {"none", "local"}:
        raise ValueError("Unsupported auth mode.")
    with _notifications_conn() as conn:
        for key, value in current.items():
            conn.execute(
                """
                INSERT INTO app_settings(setting_key, setting_value)
                VALUES (?, ?)
                ON CONFLICT(setting_key) DO UPDATE SET setting_value = excluded.setting_value
                """,
                (key, str(value)),
            )
        conn.commit()
    return _load_app_settings()


def _list_local_users():
    with _notifications_conn() as conn:
        rows = conn.execute(
            """
            SELECT id, username, display_name, enabled, created_at, updated_at
            FROM local_users
            ORDER BY lower(username), id
            """
        ).fetchall()
    return [
        {
            "id": row["id"],
            "username": row["username"],
            "display_name": row["display_name"],
            "enabled": bool(row["enabled"]),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }
        for row in rows
    ]


def _save_local_user(payload):
    user_id = payload.get("id")
    username = str(payload.get("username") or "").strip()
    display_name = str(payload.get("display_name") or "").strip()
    password = str(payload.get("password") or "")
    confirm_password = str(payload.get("confirm_password") or "")
    enabled = 1 if _bool_setting(payload.get("enabled"), True) else 0
    if not username:
        raise ValueError("Username is required.")
    if not display_name:
        display_name = username
    if password or confirm_password:
        if password != confirm_password:
            raise ValueError("Passwords do not match.")
    now = _now_utc_iso()
    with _notifications_conn() as conn:
        if user_id:
            existing = conn.execute("SELECT * FROM local_users WHERE id = ?", (int(user_id),)).fetchone()
            if not existing:
                raise ValueError("User not found.")
            password_hash = existing["password_hash"]
            if password.strip():
                password_hash = generate_password_hash(password)
            conn.execute(
                """
                UPDATE local_users
                SET username = ?, display_name = ?, password_hash = ?, enabled = ?, updated_at = ?
                WHERE id = ?
                """,
                (username, display_name, password_hash, enabled, now, int(user_id)),
            )
        else:
            if not password.strip():
                raise ValueError("Password is required for a new local user.")
            conn.execute(
                """
                INSERT INTO local_users(username, display_name, password_hash, enabled, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (username, display_name, generate_password_hash(password), enabled, now, now),
            )
        conn.commit()
    return _list_local_users()


def _delete_local_user(user_id):
    if not user_id:
        raise ValueError("User id is required.")
    with _notifications_conn() as conn:
        conn.execute("DELETE FROM local_users WHERE id = ?", (int(user_id),))
        conn.commit()
    return _list_local_users()


def _save_email_settings(payload):
    current = _load_email_settings(include_secret=True)
    merged = dict(current)
    for key in ("smtp_host", "smtp_port", "smtp_username", "sender_name", "sender_email"):
        if key in payload:
            merged[key] = str(payload.get(key) or "").strip()
    merged["use_tls"] = "true" if _bool_setting(payload.get("use_tls"), current.get("use_tls", True)) else "false"
    merged["use_ssl"] = "true" if _bool_setting(payload.get("use_ssl"), current.get("use_ssl", False)) else "false"
    new_password = str(payload.get("smtp_password") or "")
    if new_password.strip():
        merged["smtp_password"] = new_password
    now = _now_utc_iso()
    with _notifications_conn() as conn:
        for key, value in merged.items():
            if key == "smtp_password_set":
                continue
            conn.execute(
                "INSERT INTO email_settings(setting_key, setting_value) VALUES (?, ?) "
                "ON CONFLICT(setting_key) DO UPDATE SET setting_value=excluded.setting_value",
                (key, str(value)),
            )
        conn.commit()
    saved = _load_email_settings(include_secret=False)
    saved["updated_at"] = now
    return saved


def _list_notification_recipients(enabled_only=False):
    query = "SELECT id, display_name, email_address, enabled, datasets, severities, created_at, updated_at FROM recipients"
    params = []
    if enabled_only:
        query += " WHERE enabled = 1"
    query += " ORDER BY lower(display_name), lower(email_address)"
    with _notifications_conn() as conn:
        rows = conn.execute(query, params).fetchall()
    recipients = []
    for row in rows:
        recipients.append({
            "id": row["id"],
            "name": row["display_name"],
            "email": row["email_address"],
            "enabled": bool(row["enabled"]),
            "datasets": [item for item in str(row["datasets"] or "").split(",") if item],
            "severities": [item for item in str(row["severities"] or "").split(",") if item],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        })
    return recipients


def _save_notification_recipient(payload):
    rec_id = payload.get("id")
    name = str(payload.get("name") or "").strip()
    email = str(payload.get("email") or "").strip()
    if not name or not email:
        raise ValueError("Recipient name and email are required.")
    enabled = 1 if _bool_setting(payload.get("enabled"), True) else 0
    datasets = payload.get("datasets") or []
    severities = payload.get("severities") or []
    if isinstance(datasets, str):
        datasets = [item.strip() for item in datasets.split(",") if item.strip()]
    if isinstance(severities, str):
        severities = [item.strip() for item in severities.split(",") if item.strip()]
    now = _now_utc_iso()
    with _notifications_conn() as conn:
        if rec_id:
            conn.execute(
                """
                UPDATE recipients
                SET display_name = ?, email_address = ?, enabled = ?, datasets = ?, severities = ?, updated_at = ?
                WHERE id = ?
                """,
                (name, email, enabled, ",".join(datasets), ",".join(severities), now, int(rec_id)),
            )
        else:
            conn.execute(
                """
                INSERT INTO recipients(display_name, email_address, enabled, datasets, severities, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (name, email, enabled, ",".join(datasets), ",".join(severities), now, now),
            )
        conn.commit()
    return _list_notification_recipients()


def _delete_notification_recipient(rec_id):
    if not rec_id:
        raise ValueError("Recipient id is required.")
    rec_id = int(rec_id)
    with _notifications_conn() as conn:
        conn.execute("DELETE FROM recipients WHERE id = ?", (rec_id,))
        rows = conn.execute("SELECT dataset_key, field_name, recipient_ids FROM threshold_notifications").fetchall()
        for row in rows:
            ids = _parse_json_list(row["recipient_ids"])
            filtered = [item for item in ids if int(item) != rec_id]
            if filtered != ids:
                conn.execute(
                    """
                    UPDATE threshold_notifications
                    SET recipient_ids = ?, updated_at = ?
                    WHERE dataset_key = ? AND field_name = ?
                    """,
                    (json.dumps(filtered), _now_utc_iso(), row["dataset_key"], row["field_name"]),
                )
        conn.commit()
    return _list_notification_recipients()


def _parse_json_list(value):
    try:
        data = json.loads(value or "[]")
        if isinstance(data, list):
            return data
    except Exception:
        pass
    return []


def _load_threshold_notification(dataset_key, field_name):
    with _notifications_conn() as conn:
        row = conn.execute(
            """
            SELECT enabled, severity, repeat_minutes, recipient_mode, recipient_ids
            FROM threshold_notifications
            WHERE dataset_key = ? AND field_name = ?
            """,
            (dataset_key, field_name),
        ).fetchone()
    if not row:
        return dict(_THRESHOLD_NOTIFICATION_DEFAULTS)
    return {
        "enabled": bool(row["enabled"]),
        "severity": str(row["severity"] or "critical"),
        "repeat_minutes": int(row["repeat_minutes"] or 0),
        "recipient_mode": str(row["recipient_mode"] or "all_enabled"),
        "recipient_ids": [int(item) for item in _parse_json_list(row["recipient_ids"]) if str(item).strip()],
    }


def _save_threshold_notification(dataset_key, field_name, payload):
    repeat_raw = _coerce_threshold_value(payload.get("notify_repeat_minutes"))
    try:
        repeat_minutes = int(repeat_raw or 0)
    except Exception:
        repeat_minutes = 0
    config = {
        "enabled": _bool_setting(payload.get("notify_enabled"), False),
        "severity": str(payload.get("notify_severity") or "critical").strip().lower() or "critical",
        "repeat_minutes": max(repeat_minutes, 0),
        "recipient_mode": str(payload.get("notify_recipient_mode") or "all_enabled").strip() or "all_enabled",
        "recipient_ids": [],
    }
    recipient_ids = payload.get("notify_recipient_ids") or []
    if isinstance(recipient_ids, str):
        recipient_ids = [item for item in recipient_ids.split(",") if item]
    config["recipient_ids"] = [int(item) for item in recipient_ids if str(item).strip().isdigit()]
    with _notifications_conn() as conn:
        if not config["enabled"]:
            conn.execute(
                "DELETE FROM threshold_notifications WHERE dataset_key = ? AND field_name = ?",
                (dataset_key, field_name),
            )
        else:
            conn.execute(
                """
                INSERT INTO threshold_notifications(
                    dataset_key, field_name, enabled, severity, repeat_minutes, recipient_mode, recipient_ids, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(dataset_key, field_name) DO UPDATE SET
                    enabled = excluded.enabled,
                    severity = excluded.severity,
                    repeat_minutes = excluded.repeat_minutes,
                    recipient_mode = excluded.recipient_mode,
                    recipient_ids = excluded.recipient_ids,
                    updated_at = excluded.updated_at
                """,
                (
                    dataset_key,
                    field_name,
                    1,
                    config["severity"],
                    config["repeat_minutes"],
                    config["recipient_mode"],
                    json.dumps(config["recipient_ids"]),
                    _now_utc_iso(),
                ),
            )
        conn.commit()
    return _load_threshold_notification(dataset_key, field_name)


def _alert_state_summary():
    with _notifications_conn() as conn:
        rows = conn.execute(
            "SELECT status, COUNT(*) AS row_count FROM alert_state GROUP BY status"
        ).fetchall()
    summary = {"active": 0, "cleared": 0, "total": 0}
    for row in rows:
        key = str(row["status"] or "active").strip().lower() or "active"
        summary[key] = int(row["row_count"] or 0)
        summary["total"] += int(row["row_count"] or 0)
    return summary


def _list_environments(include_secret=False):
    with _notifications_conn() as conn:
        rows = conn.execute(
            """
            SELECT id, environment_name, portal_fqdn, portal_ip, ctera_username, ctera_password,
                   jump_host_enabled, main_db_via_jump_preconfigured, jump_host, main_db_jump_username, jump_ssh_mode, jump_ssh_username, jump_ssh_key_path, jump_ssh_password,
                   main_db_ip, ssh_mode, ssh_username, ssh_key_path, ssh_password, sudo_required,
                   pg_host, pg_port, pg_database, pg_user, pg_password, openai_key,
                   portal_schedule_minutes, filer_schedule_minutes, enabled, created_at, updated_at
            FROM environments
            ORDER BY lower(environment_name), id
            """
        ).fetchall()
    items = []
    for row in rows:
        item = {
            "id": row["id"],
            "name": row["environment_name"],
            "portal_fqdn": row["portal_fqdn"],
            "portal_ip": row["portal_ip"],
            "ctera_username": row["ctera_username"],
            "ctera_password": row["ctera_password"] if include_secret else "",
            "ctera_password_set": bool(row["ctera_password"]),
            "jump_host_enabled": bool(row["jump_host_enabled"]),
            "main_db_via_jump_preconfigured": bool(row["main_db_via_jump_preconfigured"]),
            "jump_host": row["jump_host"],
            "main_db_jump_username": row["main_db_jump_username"],
            "jump_ssh_mode": row["jump_ssh_mode"],
            "jump_ssh_username": row["jump_ssh_username"],
            "jump_ssh_key_path": row["jump_ssh_key_path"],
            "jump_ssh_password": row["jump_ssh_password"] if include_secret else "",
            "jump_ssh_password_set": bool(row["jump_ssh_password"]),
            "main_db_ip": row["main_db_ip"],
            "ssh_mode": row["ssh_mode"],
            "ssh_username": row["ssh_username"],
            "ssh_key_path": row["ssh_key_path"],
            "ssh_password": row["ssh_password"] if include_secret else "",
            "ssh_password_set": bool(row["ssh_password"]),
            "sudo_required": bool(row["sudo_required"]),
            "pg_host": row["pg_host"],
            "pg_port": str(row["pg_port"] or "5432"),
            "pg_database": row["pg_database"],
            "pg_user": row["pg_user"],
            "pg_password": row["pg_password"] if include_secret else "",
            "pg_password_set": bool(row["pg_password"]),
            "openai_key": row["openai_key"] if include_secret else "",
            "openai_key_set": bool(row["openai_key"]),
            "portal_schedule_minutes": int(row["portal_schedule_minutes"] or 60),
            "filer_schedule_minutes": int(row["filer_schedule_minutes"] or 60),
            "enabled": bool(row["enabled"]),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }
        items.append(item)
    return items


def _get_environment(env_id, include_secret=False):
    if env_id in (None, "", "admin"):
        return None
    target = str(env_id)
    for item in _list_environments(include_secret=include_secret):
        if str(item.get("id")) == target:
            return item
    return None


def _environment_storage_slug(env):
    return f"{int(env.get('id'))}-{_slugify(env.get('name') or 'environment')}"


def _environment_data_dir(env):
    return os.path.join(DEFAULT_DATA_DIR, "environments", _environment_storage_slug(env))


def _environment_db_dir(env):
    return os.path.join(_environment_data_dir(env), "db")


def _request_environment_id():
    env_id = str(request.args.get("env") or "").strip()
    if not env_id or env_id == "admin":
        return None
    env = _get_environment(env_id, include_secret=False)
    return str(env["id"]) if env else None


def _update_environment_bootstrap_fields(env_id, **fields):
    env = _get_environment(env_id, include_secret=True)
    if not env:
        raise ValueError("Environment not found.")
    now = _now_utc_iso()
    for key, value in fields.items():
        env[key] = value
    with _notifications_conn() as conn:
        conn.execute(
            """
            UPDATE environments
            SET ssh_key_path = ?, jump_ssh_key_path = ?, pg_password = ?,
                ssh_mode = ?, ssh_username = ?, ssh_password = ?, jump_ssh_mode = ?, jump_ssh_password = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                str(env.get("ssh_key_path") or ""),
                str(env.get("jump_ssh_key_path") or ""),
                str(env.get("pg_password") or ""),
                str(env.get("ssh_mode") or "root_password"),
                str(env.get("ssh_username") or "root"),
                str(env.get("ssh_password") or ""),
                str(env.get("jump_ssh_mode") or "root_password"),
                str(env.get("jump_ssh_password") or ""),
                now,
                int(env_id),
            ),
        )
        conn.commit()
    return _get_environment(env_id, include_secret=True)


def _load_paramiko_key(path):
    last_error = None
    for key_type in (paramiko.Ed25519Key, paramiko.RSAKey, paramiko.ECDSAKey):
        try:
            return key_type.from_private_key_file(path)
        except Exception as exc:
            last_error = exc
    raise ValueError(f"Could not load the uploaded initial SSH private key '{path}': {last_error}")


def _connect_paramiko_host(host, username, mode, password="", key_path="", label="host", sock=None):
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    kwargs = {
        "hostname": host,
        "username": username,
        "timeout": 15,
        "banner_timeout": 15,
        "auth_timeout": 15,
    }
    if sock is not None:
        kwargs["sock"] = sock
    if "key" in mode:
        if not key_path or not os.path.exists(key_path):
            raise ValueError(f"Initial SSH private key for {label} is missing.")
        try:
            kwargs["pkey"] = _load_paramiko_key(key_path)
        except Exception as exc:
            raise ValueError(f"Initial SSH private key for {label} could not be read. {exc}") from exc
    else:
        if not password:
            raise ValueError(f"Initial SSH password is required for {label}.")
        kwargs["password"] = password
        kwargs["look_for_keys"] = False
        kwargs["allow_agent"] = False
    try:
        client.connect(**kwargs)
    except paramiko.AuthenticationException as exc:
        if "key" in mode:
            raise ValueError(
                f"Initial SSH login to {label} failed for user '{username}' using the uploaded private key. "
                f"Check the SSH username, uploaded key, and whether that key is authorized on {label}."
            ) from exc
        raise ValueError(
            f"Initial SSH login to {label} failed for user '{username}'. "
            "Check the initial SSH username and password."
        ) from exc
    except Exception as exc:
        raise ValueError(f"Could not connect to {label} '{host}' over SSH. {exc}") from exc
    return client


def _connect_bootstrap_ssh(env):
    host = str(env.get("main_db_ip") or "").strip()
    username = str(env.get("ssh_username") or "root").strip() or "root"
    mode = str(env.get("ssh_mode") or "root_password").strip()
    password = str(env.get("ssh_password") or "")
    key_path = str(env.get("ssh_key_path") or "").strip()
    if not host:
        raise ValueError("MainDB IP is required.")
    jump_enabled = bool(env.get("jump_host_enabled"))
    jump_client = None
    if jump_enabled:
        jump_host = str(env.get("jump_host") or "").strip()
        jump_user = str(env.get("jump_ssh_username") or "root").strip() or "root"
        jump_mode = str(env.get("jump_ssh_mode") or "root_password").strip()
        jump_password = str(env.get("jump_ssh_password") or "")
        jump_key_path = str(env.get("jump_ssh_key_path") or "").strip()
        if not jump_host:
            raise ValueError("Jump host is required when jump-host access is enabled.")
        jump_client = _connect_paramiko_host(
            jump_host,
            jump_user,
            jump_mode,
            password=jump_password,
            key_path=jump_key_path,
            label="jump host",
        )
        try:
            channel = jump_client.get_transport().open_channel(
                "direct-tcpip",
                (host, 22),
                ("127.0.0.1", 0),
            )
        except Exception as exc:
            jump_client.close()
            raise ValueError(f"Could not open the SSH hop from jump host '{jump_host}' to MainDB '{host}'. {exc}") from exc
        try:
            main_client = _connect_paramiko_host(
                host,
                username,
                mode,
                password=password,
                key_path=key_path,
                label="MainDB",
                sock=channel,
            )
        except Exception:
            try:
                channel.close()
            except Exception:
                pass
            jump_client.close()
            raise
        return {"main": main_client, "jump": jump_client}
    main_client = _connect_paramiko_host(
        host,
        username,
        mode,
        password=password,
        key_path=key_path,
        label="MainDB",
    )
    return {"main": main_client, "jump": None}


def _exec_ssh_command(client, command, use_sudo=False, sudo_password=""):
    remote = command
    if use_sudo:
        if sudo_password:
            remote = "printf %s\\\\n {pwd} | sudo -S -p '' bash -lc {cmd}".format(
                pwd=_shell_quote(sudo_password),
                cmd=_shell_quote(command),
            )
        else:
            remote = "sudo -n bash -lc {cmd}".format(cmd=_shell_quote(command))
    stdin, stdout, stderr = client.exec_command(remote)
    rc = stdout.channel.recv_exit_status()
    out = stdout.read().decode("utf-8", "ignore")
    err = stderr.read().decode("utf-8", "ignore")
    if rc != 0:
        combined = (err or out or "").strip().lower()
        if use_sudo:
            if "password is required" in combined or "a password is required" in combined:
                raise ValueError("Sudo/root access failed on MainDB. This SSH mode needs sudo, but sudo asked for a password and none worked.")
            if "incorrect password" in combined or "sorry, try again" in combined:
                raise ValueError("Sudo/root access failed on MainDB. Check the initial SSH password used for sudo.")
            if "not in the sudoers" in combined:
                raise ValueError("Sudo/root access failed on MainDB. The selected SSH user is not allowed to run sudo.")
            if "sudo:" in combined:
                raise ValueError(f"Sudo/root access failed on MainDB. {err or out}".strip())
        raise RuntimeError((err or out or f"remote command failed with exit code {rc}").strip())
    return out


def _ensure_runtime_keypair(env):
    existing = str(env.get("ssh_key_path") or "").strip()
    if existing and os.path.exists(existing):
        return existing
    base = os.path.join(_bootstrap_key_dir(), f"{_slugify(env.get('name'))}-runtime-ed25519")
    if not os.path.exists(base):
        subprocess.run(
            ["/usr/bin/env", "ssh-keygen", "-t", "ed25519", "-N", "", "-f", base, "-C", f"{_slugify(env.get('name'))}@ctera-monitoring-dashboard"],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            os.chmod(base, 0o600)
            os.chmod(base + ".pub", 0o644)
        except Exception:
            pass
    return base


def _install_runtime_public_key(client, private_key_path):
    pub_path = private_key_path + ".pub"
    with open(pub_path, "r", encoding="utf-8") as handle:
        pub_key = handle.read().strip()
    remote_cmd = (
        "mkdir -p ~/.ssh && chmod 700 ~/.ssh && touch ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys && "
        f"grep -qxF {_shell_quote(pub_key)} ~/.ssh/authorized_keys 2>/dev/null || echo {_shell_quote(pub_key)} >> ~/.ssh/authorized_keys"
    )
    try:
        _exec_ssh_command(client, remote_cmd)
    except Exception as exc:
        raise ValueError(f"Could not install the dashboard SSH key on MainDB. {exc}") from exc


def _jump_to_target_exec(jump_client, target_host, target_user, command, *, target_sudo=False, timeout=20):
    inner = command
    if target_sudo:
        inner = "if [ \"$(id -u)\" -eq 0 ]; then bash -lc {cmd}; else sudo -n bash -lc {cmd}; fi".format(
            cmd=_shell_quote(command),
        )
    else:
        inner = "bash -lc {cmd}".format(cmd=_shell_quote(command))
    ssh_parts = [
        "ssh",
        "-o", "BatchMode=yes",
        "-o", "StrictHostKeyChecking=no",
        "-o", "UserKnownHostsFile=/dev/null",
        "-o", f"ConnectTimeout={timeout}",
        f"{target_user}@{target_host}",
        inner,
    ]
    ssh_cmd = " ".join(_shell_quote(part) for part in ssh_parts)
    try:
        return _exec_ssh_command(jump_client, ssh_cmd)
    except Exception as exc:
        raise ValueError(
            f"Could not use the jump host's existing SSH access to reach MainDB as user '{target_user}'. {exc}"
        ) from exc


def _install_runtime_public_key_via_jump(jump_client, target_host, target_user, private_key_path, *, target_sudo=False):
    pub_path = private_key_path + ".pub"
    with open(pub_path, "r", encoding="utf-8") as handle:
        pub_key = handle.read().strip()
    remote_cmd = (
        "mkdir -p ~/.ssh && chmod 700 ~/.ssh && touch ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys && "
        f"grep -qxF {_shell_quote(pub_key)} ~/.ssh/authorized_keys 2>/dev/null || echo {_shell_quote(pub_key)} >> ~/.ssh/authorized_keys"
    )
    try:
        _jump_to_target_exec(jump_client, target_host, target_user, remote_cmd, target_sudo=target_sudo)
    except Exception as exc:
        raise ValueError(f"Could not install the dashboard SSH key on MainDB via the jump host. {exc}") from exc


def _reveal_postgres_password(client, env):
    use_sudo = bool(env.get("sudo_required")) or str(env.get("ssh_username") or "root").strip() != "root"
    reveal_cmd = (
        "/usr/local/ctera/jdk/bin/java -cp "
        "'/usr/local/ctera/apache-tomcat/lib/portal/*:/usr/local/ctera/apache-tomcat/lib/common.jar' "
        "com.ctera.utils.password.PostgresPasswordTool "
        "$(cat /etc/ctera/portal_key) "
        "$(grep CTERA_LOCAL_POSTGRES_PASS /etc/ctera/portal.cfg | cut -d '=' -f2) reveal"
    )
    try:
        output = _exec_ssh_command(client, reveal_cmd, use_sudo=use_sudo, sudo_password=str(env.get("ssh_password") or ""))
    except Exception as exc:
        raise ValueError(f"Could not retrieve the Postgres password from MainDB. {exc}") from exc
    lines = [line.strip() for line in output.replace("\r", "").splitlines() if line.strip()]
    if not lines:
        raise ValueError("Could not retrieve the Postgres password from MainDB.")
    return lines[-1]


def _reveal_postgres_password_via_jump(jump_client, env, target_user):
    use_sudo = bool(env.get("sudo_required")) or str(target_user or "root").strip() != "root"
    reveal_cmd = (
        "/usr/local/ctera/jdk/bin/java -cp "
        "'/usr/local/ctera/apache-tomcat/lib/portal/*:/usr/local/ctera/apache-tomcat/lib/common.jar' "
        "com.ctera.utils.password.PostgresPasswordTool "
        "$(cat /etc/ctera/portal_key) "
        "$(grep CTERA_LOCAL_POSTGRES_PASS /etc/ctera/portal.cfg | cut -d '=' -f2) reveal"
    )
    try:
        output = _jump_to_target_exec(
            jump_client,
            str(env.get("main_db_ip") or "").strip(),
            target_user,
            reveal_cmd,
            target_sudo=use_sudo,
        )
    except Exception as exc:
        raise ValueError(f"Could not retrieve the Postgres password from MainDB via the jump host. {exc}") from exc
    lines = [line.strip() for line in output.replace("\r", "").splitlines() if line.strip()]
    if not lines:
        raise ValueError("Could not retrieve the Postgres password from MainDB via the jump host.")
    return lines[-1]


def _bootstrap_environment_runtime(env_id):
    env = _get_environment(env_id, include_secret=True)
    if not env:
        raise ValueError("Select a saved portal environment first.")
    runtime_key_path = _ensure_runtime_keypair(env)
    needs_key_install = not (str(env.get("ssh_key_path") or "").strip() and os.path.exists(str(env.get("ssh_key_path") or "").strip()))
    needs_jump_key_install = bool(env.get("jump_host_enabled")) and not (str(env.get("jump_ssh_key_path") or "").strip() and os.path.exists(str(env.get("jump_ssh_key_path") or "").strip()))
    needs_pg_password = not str(env.get("pg_password") or "").strip()
    preconfigured_jump = bool(env.get("jump_host_enabled")) and bool(env.get("main_db_via_jump_preconfigured"))
    if not needs_key_install and not needs_jump_key_install and not needs_pg_password:
        return env
    if preconfigured_jump:
        jump_host = str(env.get("jump_host") or "").strip()
        jump_user = str(env.get("jump_ssh_username") or "root").strip() or "root"
        jump_mode = str(env.get("jump_ssh_mode") or "root_password").strip()
        jump_password = str(env.get("jump_ssh_password") or "")
        jump_key_path = str(env.get("jump_ssh_key_path") or "").strip()
        target_user = str(env.get("main_db_jump_username") or env.get("ssh_username") or env.get("jump_ssh_username") or "root").strip() or "root"
        jump_client = _connect_paramiko_host(
            jump_host,
            jump_user,
            jump_mode,
            password=jump_password,
            key_path=jump_key_path,
            label="jump host",
        )
        try:
            if needs_jump_key_install:
                _install_runtime_public_key(jump_client, runtime_key_path)
                env["jump_ssh_key_path"] = runtime_key_path
                env["jump_ssh_mode"] = "root_key" if jump_user == "root" else "user_key"
                env["jump_ssh_password"] = ""
            if needs_key_install:
                _install_runtime_public_key_via_jump(
                    jump_client,
                    str(env.get("main_db_ip") or "").strip(),
                    target_user,
                    runtime_key_path,
                    target_sudo=(target_user != "root"),
                )
                env["ssh_key_path"] = runtime_key_path
                env["ssh_username"] = target_user
                env["ssh_mode"] = "root_key" if target_user == "root" else "user_key_sudo"
                env["ssh_password"] = ""
                env["sudo_required"] = 0 if target_user == "root" else 1
            if needs_pg_password:
                env["pg_password"] = _reveal_postgres_password_via_jump(jump_client, env, target_user)
        finally:
            try:
                jump_client.close()
            except Exception:
                pass
        return _update_environment_bootstrap_fields(
            env_id,
            ssh_key_path=env.get("ssh_key_path") or "",
            jump_ssh_key_path=env.get("jump_ssh_key_path") or "",
            ssh_mode=env.get("ssh_mode") or "",
            ssh_username=env.get("ssh_username") or "",
            ssh_password=env.get("ssh_password") or "",
            jump_ssh_mode=env.get("jump_ssh_mode") or "",
            jump_ssh_password=env.get("jump_ssh_password") or "",
            pg_password=env.get("pg_password") or "",
        )
    clients = _connect_bootstrap_ssh(env)
    main_client = clients["main"]
    jump_client = clients["jump"]
    try:
        if needs_jump_key_install and jump_client:
            _install_runtime_public_key(jump_client, runtime_key_path)
            env["jump_ssh_key_path"] = runtime_key_path
            env["jump_ssh_mode"] = "root_key" if str(env.get("jump_ssh_username") or "root").strip() == "root" else "user_key"
            env["jump_ssh_password"] = ""
        if needs_key_install:
            _install_runtime_public_key(main_client, runtime_key_path)
            env["ssh_key_path"] = runtime_key_path
            env["ssh_mode"] = "root_key" if str(env.get("ssh_username") or "root").strip() == "root" else "user_key_sudo"
            env["ssh_password"] = ""
        if needs_pg_password:
            env["pg_password"] = _reveal_postgres_password(main_client, env)
    finally:
        try:
            main_client.close()
        except Exception:
            pass
        if jump_client:
            try:
                jump_client.close()
            except Exception:
                pass
    return _update_environment_bootstrap_fields(
        env_id,
        ssh_key_path=env.get("ssh_key_path") or "",
        jump_ssh_key_path=env.get("jump_ssh_key_path") or "",
        ssh_mode=env.get("ssh_mode") or "",
        ssh_password=env.get("ssh_password") or "",
        jump_ssh_mode=env.get("jump_ssh_mode") or "",
        jump_ssh_password=env.get("jump_ssh_password") or "",
        pg_password=env.get("pg_password") or "",
    )


def _write_runtime_env_file(env):
    env = dict(env or {})
    runtime_path = os.path.join(_runtime_env_dir(), f"environment-{env['id']}.env")
    data_dir = _environment_data_dir(env)
    db_dir = _environment_db_dir(env)
    os.makedirs(data_dir, exist_ok=True)
    os.makedirs(db_dir, exist_ok=True)
    pg_host = str(env.get("pg_host") or env.get("main_db_ip") or "").strip()
    root_key = str(env.get("ssh_key_path") or "").strip()
    ssh_user = str(env.get("ssh_username") or "root").strip() or "root"
    jump_enabled = bool(env.get("jump_host_enabled"))
    jump_host = str(env.get("jump_host") or "").strip()
    jump_user = str(env.get("jump_ssh_username") or "root").strip() or "root"
    lines = [
        f"FEATHERDASH_ENV_NAME={_env_quote_line(env.get('name') or env.get('environment_name') or '')}",
        f"CTERA_HOST={_env_quote_line(env.get('portal_fqdn') or '')}",
        f"CTERA_USERNAME={_env_quote_line(env.get('ctera_username') or '')}",
        f"CTERA_PASSWORD={_env_quote_line(env.get('ctera_password') or '')}",
        "CTERA_VERIFY_SSL=false",
        f"PGHOST={_env_quote_line(pg_host)}",
        "PGPORT='5432'",
        "PGDATABASE='postgres'",
        "PGUSER='postgres'",
        f"PGPASSWORD={_env_quote_line(env.get('pg_password') or '')}",
        f"SERVER_SSH_USER={_env_quote_line(ssh_user)}",
        f"ROOT_KEY={_env_quote_line(root_key)}",
        f"JUMP_HOST_ENABLED={_env_quote_line('true' if jump_enabled else 'false')}",
        f"JUMP_HOST={_env_quote_line(jump_host)}",
        f"JUMP_SSH_USER={_env_quote_line(jump_user)}",
        f"MAINDB_VIA_JUMP_PRECONFIGURED={_env_quote_line('true' if env.get('main_db_via_jump_preconfigured') else 'false')}",
        f"MAINDB_JUMP_USERNAME={_env_quote_line(env.get('main_db_jump_username') or ssh_user)}",
        "SERVER_METRICS_MODE='jump'",
        "SERVER_METRICS_TARGET_USER='ctera'",
        f"SERVER_METRICS_JUMP_HOST={_env_quote_line('127.0.0.1' if jump_enabled else pg_host)}",
        f"SERVER_METRICS_JUMP_USER={_env_quote_line(ssh_user)}",
        "SERVER_METRICS_JUMP_RUN_AS_USER='ctera'",
        f"SERVER_METRICS_SUDO={_env_quote_line('true' if env.get('sudo_required') else 'false')}",
        f"OPENAI_API_KEY={_env_quote_line(env.get('openai_key') or '')}",
        f"PORT={_env_quote_line(os.environ.get('PORT', '8080'))}",
        f"FEATHERDASH_DATA_DIR={_env_quote_line(data_dir)}",
        f"FEATHERDASH_DB_DIR={_env_quote_line(db_dir)}",
        f"FEATHERDASH_THRESHOLDS={_env_quote_line(os.path.join(PROJECT_DIR, 'thresholds.yaml'))}",
        "PYTHONUNBUFFERED='1'",
    ]
    with open(runtime_path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write("\n".join(lines) + "\n")
    try:
        os.chmod(runtime_path, 0o600)
    except Exception:
        pass
    return runtime_path


def _save_environment(payload):
    env_id = payload.get("id")
    name = str(payload.get("environment_name") or "").strip()
    if not name:
        raise ValueError("Environment name is required.")
    current = None
    if env_id:
        current = next((item for item in _list_environments(include_secret=True) if int(item["id"]) == int(env_id)), None)
        if not current:
            raise ValueError("Environment not found.")
    else:
        current = next((item for item in _list_environments(include_secret=True) if str(item["name"]).strip().lower() == name.lower()), None)
        if current:
            env_id = current["id"]
    now = _now_utc_iso()
    merged = dict(current or {})
    fields = [
        "portal_fqdn", "portal_ip", "ctera_username", "main_db_ip",
        "jump_host", "main_db_jump_username", "jump_ssh_mode", "jump_ssh_username", "jump_ssh_key_path",
        "ssh_mode", "ssh_username", "ssh_key_path",
        "pg_host", "pg_port", "pg_database", "pg_user", "portal_schedule_minutes", "filer_schedule_minutes"
    ]
    merged["environment_name"] = name
    for field in fields:
        if field in payload:
            merged[field] = payload.get(field)
    merged["sudo_required"] = 1 if _bool_setting(payload.get("sudo_required"), bool(current.get("sudo_required")) if current else True) else 0
    merged["jump_host_enabled"] = 1 if _bool_setting(payload.get("jump_host_enabled"), bool(current.get("jump_host_enabled")) if current else False) else 0
    merged["main_db_via_jump_preconfigured"] = 1 if _bool_setting(payload.get("main_db_via_jump_preconfigured"), bool(current.get("main_db_via_jump_preconfigured")) if current else False) else 0
    merged["enabled"] = 1 if _bool_setting(payload.get("enabled"), bool(current.get("enabled")) if current else True) else 0
    key_content = str(payload.get("ssh_private_key_content") or "")
    key_name = str(payload.get("ssh_private_key_name") or "")
    if key_content.strip():
        merged["ssh_key_path"] = _store_environment_key_material(name, key_name, key_content)
    jump_key_content = str(payload.get("jump_ssh_private_key_content") or "")
    jump_key_name = str(payload.get("jump_ssh_private_key_name") or "")
    if jump_key_content.strip():
        merged["jump_ssh_key_path"] = _store_environment_key_material(f"{name}-jump", jump_key_name, jump_key_content)
    for secret_key in ("ctera_password", "ssh_password", "jump_ssh_password", "pg_password", "openai_key"):
        incoming = str(payload.get(secret_key) or "")
        if incoming.strip():
            merged[secret_key] = incoming
        elif current:
            merged[secret_key] = current.get(secret_key, "")
        else:
            merged[secret_key] = ""
    portal_fqdn = str(merged.get("portal_fqdn") or "").strip()
    ctera_username = str(merged.get("ctera_username") or "").strip()
    main_db_ip = str(merged.get("main_db_ip") or "").strip()
    jump_enabled = bool(merged.get("jump_host_enabled"))
    main_db_via_jump_preconfigured = bool(merged.get("main_db_via_jump_preconfigured"))
    jump_host = str(merged.get("jump_host") or "").strip()
    main_db_jump_username = str(merged.get("main_db_jump_username") or "").strip()
    jump_ssh_mode = str(merged.get("jump_ssh_mode") or "root_password").strip() or "root_password"
    jump_ssh_username = str(merged.get("jump_ssh_username") or "root").strip()
    jump_ssh_password = str(merged.get("jump_ssh_password") or "")
    jump_ssh_key_path = str(merged.get("jump_ssh_key_path") or "").strip()
    ssh_mode = str(merged.get("ssh_mode") or "root_password").strip() or "root_password"
    ssh_username = str(merged.get("ssh_username") or "root").strip()
    ctera_password = str(merged.get("ctera_password") or "")
    ssh_password = str(merged.get("ssh_password") or "")
    ssh_key_path = str(merged.get("ssh_key_path") or "").strip()
    if jump_enabled and main_db_via_jump_preconfigured:
        jump_needs_password = jump_ssh_mode in {"root_password", "user_password"}
        jump_needs_key = jump_ssh_mode in {"root_key", "user_key"}
        target_user = main_db_jump_username or jump_ssh_username
        merged["ssh_mode"] = "user_password_sudo" if jump_needs_password else "user_key_sudo"
        merged["ssh_username"] = target_user
        merged["ssh_password"] = jump_ssh_password
        merged["ssh_key_path"] = jump_ssh_key_path
        merged["sudo_required"] = 1 if str(target_user or "").strip() != "root" else 0
        ssh_mode = str(merged.get("ssh_mode") or "root_password").strip() or "root_password"
        ssh_username = str(merged.get("ssh_username") or "root").strip()
        ssh_password = str(merged.get("ssh_password") or "")
        ssh_key_path = str(merged.get("ssh_key_path") or "").strip()
    if not portal_fqdn:
        raise ValueError("Portal FQDN is required.")
    if not ctera_username:
        raise ValueError("CTERA read-only username is required.")
    if not main_db_ip:
        raise ValueError("MainDB IP is required.")
    if not ctera_password.strip():
        raise ValueError("CTERA password is required.")
    if jump_enabled:
        if not jump_host:
            raise ValueError("Jump host is required when jump-host access is enabled.")
        if not jump_ssh_username:
            raise ValueError("Jump-host SSH username is required.")
        if main_db_via_jump_preconfigured and not (main_db_jump_username or jump_ssh_username):
            raise ValueError("MainDB SSH username from jump host is required.")
        if jump_ssh_mode in {"root_password", "user_password"} and not jump_ssh_password.strip():
            raise ValueError("Jump-host SSH password is required for this jump-host access mode.")
        if jump_ssh_mode in {"root_key", "user_key"} and not jump_ssh_key_path:
            raise ValueError("Upload a jump-host private key for this jump-host access mode.")
    if not ssh_username:
        raise ValueError("Initial SSH username is required.")
    if ssh_mode in {"root_password", "user_password_sudo"} and not ssh_password.strip():
        raise ValueError("Initial SSH password is required for this SSH access mode.")
    if ssh_mode in {"root_key", "user_key_sudo"} and not ssh_key_path:
        raise ValueError("Upload an initial SSH private key for this SSH access mode.")
    merged["portal_schedule_minutes"] = int(_coerce_threshold_value(merged.get("portal_schedule_minutes")) or 60)
    merged["filer_schedule_minutes"] = int(_coerce_threshold_value(merged.get("filer_schedule_minutes")) or 60)
    merged["pg_port"] = str(merged.get("pg_port") or "5432")
    with _notifications_conn() as conn:
        if env_id:
            conn.execute(
                """
                UPDATE environments
                SET environment_name = ?, portal_fqdn = ?, portal_ip = ?, ctera_username = ?, ctera_password = ?,
                    main_db_ip = ?, jump_host_enabled = ?, main_db_via_jump_preconfigured = ?, jump_host = ?, main_db_jump_username = ?, jump_ssh_mode = ?, jump_ssh_username = ?, jump_ssh_key_path = ?, jump_ssh_password = ?,
                    ssh_mode = ?, ssh_username = ?, ssh_key_path = ?, ssh_password = ?, sudo_required = ?,
                    pg_host = ?, pg_port = ?, pg_database = ?, pg_user = ?, pg_password = ?, openai_key = ?,
                    portal_schedule_minutes = ?, filer_schedule_minutes = ?, enabled = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    merged["environment_name"], str(merged.get("portal_fqdn") or ""), str(merged.get("portal_ip") or ""),
                    str(merged.get("ctera_username") or ""), str(merged.get("ctera_password") or ""),
                    str(merged.get("main_db_ip") or ""), merged["jump_host_enabled"], merged["main_db_via_jump_preconfigured"], str(merged.get("jump_host") or ""), str(merged.get("main_db_jump_username") or ""),
                    str(merged.get("jump_ssh_mode") or "root_password"), str(merged.get("jump_ssh_username") or "root"),
                    str(merged.get("jump_ssh_key_path") or ""), str(merged.get("jump_ssh_password") or ""),
                    str(merged.get("ssh_mode") or "root_password"),
                    str(merged.get("ssh_username") or "root"), str(merged.get("ssh_key_path") or ""),
                    str(merged.get("ssh_password") or ""), merged["sudo_required"], str(merged.get("pg_host") or ""),
                    str(merged.get("pg_port") or "5432"), str(merged.get("pg_database") or "postgres"),
                    str(merged.get("pg_user") or "postgres"), str(merged.get("pg_password") or ""),
                    str(merged.get("openai_key") or ""), merged["portal_schedule_minutes"], merged["filer_schedule_minutes"],
                    merged["enabled"], now, int(env_id),
                ),
            )
        else:
            conn.execute(
                """
                INSERT INTO environments(
                    environment_name, portal_fqdn, portal_ip, ctera_username, ctera_password, main_db_ip,
                    jump_host_enabled, main_db_via_jump_preconfigured, jump_host, main_db_jump_username, jump_ssh_mode, jump_ssh_username, jump_ssh_key_path, jump_ssh_password,
                    ssh_mode, ssh_username, ssh_key_path, ssh_password, sudo_required,
                    pg_host, pg_port, pg_database, pg_user, pg_password, openai_key,
                    portal_schedule_minutes, filer_schedule_minutes, enabled, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    merged["environment_name"], str(merged.get("portal_fqdn") or ""), str(merged.get("portal_ip") or ""),
                    str(merged.get("ctera_username") or ""), str(merged.get("ctera_password") or ""),
                    str(merged.get("main_db_ip") or ""), merged["jump_host_enabled"], merged["main_db_via_jump_preconfigured"], str(merged.get("jump_host") or ""), str(merged.get("main_db_jump_username") or ""),
                    str(merged.get("jump_ssh_mode") or "root_password"), str(merged.get("jump_ssh_username") or "root"),
                    str(merged.get("jump_ssh_key_path") or ""), str(merged.get("jump_ssh_password") or ""),
                    str(merged.get("ssh_mode") or "root_password"),
                    str(merged.get("ssh_username") or "root"), str(merged.get("ssh_key_path") or ""),
                    str(merged.get("ssh_password") or ""), merged["sudo_required"], str(merged.get("pg_host") or ""),
                    str(merged.get("pg_port") or "5432"), str(merged.get("pg_database") or "postgres"),
                    str(merged.get("pg_user") or "postgres"), str(merged.get("pg_password") or ""),
                    str(merged.get("openai_key") or ""), merged["portal_schedule_minutes"], merged["filer_schedule_minutes"],
                    merged["enabled"], now, now,
                ),
            )
        conn.commit()
    return _list_environments(include_secret=False)


def _delete_environment(env_id):
    if not env_id:
        raise ValueError("Environment id is required.")
    with _notifications_conn() as conn:
        conn.execute("DELETE FROM environments WHERE id = ?", (int(env_id),))
        conn.commit()
    return _list_environments(include_secret=False)


def _environment_payload():
    return {
        "items": _list_environments(include_secret=False),
        "count": len(_list_environments(include_secret=False)),
    }


def _store_environment_key_material(env_name, file_name, key_text):
    text = str(key_text or "").strip()
    if not text:
        return ""
    safe_name = re.sub(r"[^A-Za-z0-9._-]+", "-", str(env_name or "environment")).strip("-").lower() or "environment"
    base_name = os.path.basename(str(file_name or "").strip()) or "id_ed25519"
    base_name = re.sub(r"[^A-Za-z0-9._-]+", "-", base_name)
    key_dir = os.path.join(_state_dir(), "ssh_keys")
    os.makedirs(key_dir, exist_ok=True)
    path = os.path.join(key_dir, f"{safe_name}-{base_name}")
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
        if not text.endswith("\n"):
            handle.write("\n")
    try:
        os.chmod(path, 0o600)
    except Exception:
        pass
    return path


def _notification_settings_payload():
    return {
        "db_path": _notifications_db_path(),
        "settings": _load_email_settings(include_secret=False),
        "recipients": _list_notification_recipients(),
        "alert_state": _alert_state_summary(),
    }


def _auth_settings_payload():
    return {
        "settings": _load_app_settings(),
        "users": _list_local_users(),
    }


def _send_test_email(payload):
    settings = _load_email_settings(include_secret=True)
    target = str(payload.get("email") or settings.get("sender_email") or "").strip()
    if not target:
        raise ValueError("Provide a test email address first.")
    host = str(settings.get("smtp_host") or "").strip()
    port = int(str(settings.get("smtp_port") or "0") or 0)
    if not host or not port:
        raise ValueError("SMTP host and port are required before sending a test email.")
    message = EmailMessage()
    sender_email = str(settings.get("sender_email") or "").strip() or str(settings.get("smtp_username") or "").strip()
    sender_name = str(settings.get("sender_name") or "").strip()
    if not sender_email:
        raise ValueError("Sender email is required before sending a test email.")
    message["Subject"] = "CTERA Monitoring Dashboard test email"
    message["From"] = f"{sender_name} <{sender_email}>" if sender_name else sender_email
    message["To"] = target
    message.set_content(
        "This is a test email from CTERA Monitoring Dashboard.\n\n"
        "SMTP settings look good from the dashboard UI."
    )
    username = str(settings.get("smtp_username") or "").strip()
    password = str(settings.get("smtp_password") or "")
    if _bool_setting(settings.get("use_ssl"), False):
        context = ssl.create_default_context()
        with smtplib.SMTP_SSL(host, port, context=context, timeout=20) as server:
            if username:
                server.login(username, password)
            server.send_message(message)
        return
    with smtplib.SMTP(host, port, timeout=20) as server:
        if _bool_setting(settings.get("use_tls"), True):
            context = ssl.create_default_context()
            server.starttls(context=context)
        if username:
            server.login(username, password)
        server.send_message(message)


def _normalized_rule_to_eval_rule(rule):
    if not isinstance(rule, dict):
        return {}
    out = {}
    if rule.get("warn_op"):
        out["warn"] = {str(rule.get("warn_op")).strip().lower(): _coerce_threshold_value(rule.get("warn_value"))}
    if rule.get("crit_op"):
        out["crit"] = {str(rule.get("crit_op")).strip().lower(): _coerce_threshold_value(rule.get("crit_value"))}
    return out


def _alert_row_key(dataset_key, row):
    candidates = []
    if dataset_key == "edge":
        candidates = ["Filer Name", "Name", "Tenant"]
    elif dataset_key == "tenants":
        candidates = ["Tenant", "UID", "Name"]
    elif dataset_key == "portal_servers":
        candidates = ["Name", "Host", "UID"]
    elif dataset_key == "portal_storage":
        candidates = ["Name", "Bucket", "DedicatedTo"]
    elif dataset_key == "portal_tasks":
        candidates = ["TaskID", "TaskName", "ServerName"]
    elif dataset_key == "servers_health":
        candidates = ["Host", "Name", "UID"]
    elif dataset_key.startswith("postgres:"):
        candidates = ["table", "query_id", "pid", "relname", "index_name", "pg_host", "pg_db", "cluster"]
    parts = []
    for key in candidates:
        value = str(row.get(key, "")).strip()
        if value:
            parts.append(f"{key}={value}")
    if parts:
        return "|".join(parts[:3])
    ordered = []
    for key in sorted(row.keys()):
        value = str(row.get(key, "")).strip()
        if value:
            ordered.append(f"{key}={value}")
        if len(ordered) >= 3:
            break
    return "|".join(ordered) if ordered else "row"


def _load_dataset_rows_for_alerts(cfg, dataset_key):
    dataset = next((ds for ds in _threshold_dataset_configs(cfg) if ds["key"] == dataset_key), None)
    if not dataset:
        return None, [], []
    rows, headers = read_csv_rows(dataset["path"])
    if dataset_key == "edge":
        rows, headers = derive_fields(rows, headers, cfg)
    if dataset_key == "portal_tasks":
        rows = filter_dashboard_tasks(rows)
    return dataset, rows, headers


def _warn_fn_for_dataset(cfg, dataset_key):
    ext, _ = _load_external_thresholds(cfg)
    if dataset_key == "edge":
        return lambda field_name, value, row: make_edge_warn_fn(cfg.get("thresholds"), ext)(field_name, value, row)
    if dataset_key == "tenants":
        return lambda field_name, value, row: make_tenants_warn_fn(ext)(field_name, value, row)
    if dataset_key == "portal_servers":
        return lambda field_name, value, row: make_portal_warn_fn(ext, "servers")(field_name, value, row)
    if dataset_key == "portal_storage":
        return lambda field_name, value, row: make_portal_warn_fn(ext, "storage")(field_name, value, row)
    if dataset_key == "portal_tasks":
        return lambda field_name, value, row: make_portal_warn_fn(ext, "tasks")(field_name, value, row)
    if dataset_key == "servers_health":
        return lambda field_name, value, row: make_servers_health_warn_fn(ext)(field_name, value, row)
    if dataset_key.startswith("postgres:"):
        topic = dataset_key.split(":", 1)[1]
        return lambda field_name, value, row: make_pg_warn_fn(ext)(topic, field_name, value, row)
    return None


def _recipients_for_notification(dataset_key, severity, notify_cfg):
    recipients = [recipient for recipient in _list_notification_recipients(enabled_only=True)]
    scoped = []
    selected = set(int(item) for item in (notify_cfg.get("recipient_ids") or []))
    for recipient in recipients:
        if notify_cfg.get("recipient_mode") == "selected" and int(recipient["id"]) not in selected:
            continue
        datasets = recipient.get("datasets") or []
        severities = recipient.get("severities") or []
        if datasets and dataset_key not in datasets:
            continue
        if severities and severity not in severities:
            continue
        scoped.append(recipient)
    return scoped


def _format_alert_email_subject(summary):
    return f"[{summary['severity'].upper()}] {summary['dataset_label']} - {summary['field_name']} ({summary['match_count']} match{'es' if summary['match_count'] != 1 else ''})"


def _format_alert_email_body(summary):
    lines = [
        f"CTERA Monitoring Dashboard detected a {summary['severity']} threshold match.",
        "",
        f"Dataset: {summary['dataset_label']}",
        f"Field: {summary['field_name']}",
        f"Threshold: {summary['rule_text']}",
        f"Matches: {summary['match_count']}",
        "",
        "Top matching rows:",
    ]
    for match in summary["matches"][:10]:
        lines.append(f"- {match['row_key']}: current value = {match['value']}")
    if summary["repeat_minutes"] > 0:
        lines.extend(["", f"This alert is configured to repeat every {summary['repeat_minutes']} minute(s) while it stays active."])
    else:
        lines.extend(["", "This alert is configured as once-only until it clears and reappears."])
    return "\n".join(lines)


def _deliver_threshold_email(summary, recipients):
    if not recipients:
        return []
    settings = _load_email_settings(include_secret=True)
    host = str(settings.get("smtp_host") or "").strip()
    port = int(str(settings.get("smtp_port") or "0") or 0)
    sender_email = str(settings.get("sender_email") or "").strip() or str(settings.get("smtp_username") or "").strip()
    sender_name = str(settings.get("sender_name") or "").strip()
    username = str(settings.get("smtp_username") or "").strip()
    password = str(settings.get("smtp_password") or "")
    if not host or not port or not sender_email:
        raise ValueError("SMTP settings are incomplete. Save SMTP host, port, and sender email before sending alerts.")
    message = EmailMessage()
    message["Subject"] = _format_alert_email_subject(summary)
    message["From"] = f"{sender_name} <{sender_email}>" if sender_name else sender_email
    message["To"] = ", ".join(recipient["email"] for recipient in recipients)
    message.set_content(_format_alert_email_body(summary))
    if _bool_setting(settings.get("use_ssl"), False):
        context = ssl.create_default_context()
        with smtplib.SMTP_SSL(host, port, context=context, timeout=20) as server:
            if username:
                server.login(username, password)
            server.send_message(message)
    else:
        with smtplib.SMTP(host, port, timeout=20) as server:
            if _bool_setting(settings.get("use_tls"), True):
                server.starttls(context=ssl.create_default_context())
            if username:
                server.login(username, password)
            server.send_message(message)
    return [recipient["email"] for recipient in recipients]


def run_threshold_notifications(env_id=None):
    cfg = load_conf_for_environment(env_id)
    catalog = _build_threshold_catalog(cfg)
    now = _now_utc_iso()
    evaluated = []
    sent = []
    checked = []
    current_active_keys = set()
    env = _get_environment(env_id, include_secret=False) if env_id else None
    env_prefix = f"env:{env_id}|" if env_id else ""
    dataset_label_prefix = f"{env.get('name')} / " if env else ""
    with _notifications_conn() as conn:
        if env_id:
            existing_rows = conn.execute(
                "SELECT * FROM alert_state WHERE alert_key LIKE ?",
                (f"{env_prefix}%",),
            ).fetchall()
        else:
            existing_rows = conn.execute(
                "SELECT * FROM alert_state WHERE alert_key NOT LIKE 'env:%'"
            ).fetchall()
        existing = {row["alert_key"]: dict(row) for row in existing_rows}

        for dataset in catalog.get("datasets", []):
            dataset_key = dataset["key"]
            state_dataset_key = f"{env_prefix}{dataset_key}"
            _, rows, _ = _load_dataset_rows_for_alerts(cfg, dataset_key)
            warn_fn = _warn_fn_for_dataset(cfg, dataset_key)
            row_matches = {}
            for field in dataset.get("fields", []):
                notify_cfg = field.get("notify") or {}
                if not notify_cfg.get("enabled"):
                    continue
                eval_rule = _normalized_rule_to_eval_rule(field.get("rule") or {})
                if not eval_rule:
                    continue
                checked.append({
                    "dataset": dataset_label_prefix + dataset["label"],
                    "field": field["name"],
                    "notify_severity": str(notify_cfg.get("severity") or "critical"),
                })
                matches = []
                for row in rows:
                    raw_value = row.get(field["name"], "")
                    severity_hit = warn_fn(field["name"], raw_value, row) if warn_fn else eval_level(raw_value, eval_rule)
                    normalized = "critical" if severity_hit == "bad" else ("warning" if severity_hit == "warn" else "")
                    if not normalized:
                        continue
                    if notify_cfg.get("severity") == "critical" and normalized != "critical":
                        continue
                    if notify_cfg.get("severity") == "warning" and normalized != "warning":
                        continue
                    row_key = _alert_row_key(dataset_key, row)
                    alert_key = f"{env_prefix}{dataset_key}|{field['name']}|{row_key}|{normalized}"
                    current_active_keys.add(alert_key)
                    matches.append({
                        "alert_key": alert_key,
                        "severity": normalized,
                        "row_key": row_key,
                        "value": display_cell(field["name"], raw_value),
                    })
                if not matches:
                    continue
                highest = "critical" if any(match["severity"] == "critical" for match in matches) else "warning"
                recipients = _recipients_for_notification(dataset_key, highest, notify_cfg)
                summary = {
                    "dataset_key": dataset_key,
                    "dataset_label": dataset_label_prefix + dataset["label"],
                    "field_name": field["name"],
                    "severity": highest,
                    "rule_text": describe_threshold_rule_py(field.get("rule") or {}),
                    "match_count": len(matches),
                    "matches": matches,
                    "repeat_minutes": int(notify_cfg.get("repeat_minutes") or 0),
                }
                should_send = False
                last_emailed_at = ""
                primary_key = matches[0]["alert_key"]
                existing_state = existing.get(primary_key)
                if not existing_state or str(existing_state.get("status") or "").lower() != "active":
                    should_send = True
                else:
                    last_emailed_at = str(existing_state.get("last_emailed_at") or "")
                    repeat_minutes = int(existing_state.get("repeat_minutes") or notify_cfg.get("repeat_minutes") or 0)
                    if repeat_minutes > 0 and last_emailed_at:
                        try:
                            delta = datetime.utcnow() - datetime.fromisoformat(last_emailed_at.replace("Z", "+00:00")).replace(tzinfo=None)
                            if delta.total_seconds() >= repeat_minutes * 60:
                                should_send = True
                        except Exception:
                            should_send = True
                mailed_to = []
                if should_send and recipients:
                    mailed_to = _deliver_threshold_email(summary, recipients)
                    sent.append({
                        "dataset": dataset["label"],
                        "field": field["name"],
                        "severity": highest,
                        "recipients": mailed_to,
                        "matches": len(matches),
                    })
                    last_emailed_at = now
                for match in matches:
                    conn.execute(
                        """
                        INSERT INTO alert_state(alert_key, dataset_key, row_key, field_name, severity, status, first_seen, last_seen, last_emailed_at, repeat_minutes)
                        VALUES (?, ?, ?, ?, ?, 'active', ?, ?, ?, ?)
                        ON CONFLICT(alert_key) DO UPDATE SET
                            dataset_key = excluded.dataset_key,
                            row_key = excluded.row_key,
                            field_name = excluded.field_name,
                            severity = excluded.severity,
                            status = 'active',
                            last_seen = excluded.last_seen,
                            last_emailed_at = CASE WHEN excluded.last_emailed_at <> '' THEN excluded.last_emailed_at ELSE alert_state.last_emailed_at END,
                            repeat_minutes = excluded.repeat_minutes
                        """,
                        (
                            match["alert_key"],
                            state_dataset_key,
                            match["row_key"],
                            field["name"],
                            match["severity"],
                            existing.get(match["alert_key"], {}).get("first_seen", now),
                            now,
                            last_emailed_at,
                            int(notify_cfg.get("repeat_minutes") or 0),
                        ),
                    )
                evaluated.append({
                    "dataset": dataset["label"],
                    "field": field["name"],
                    "severity": highest,
                    "matches": len(matches),
                    "emailed": bool(mailed_to),
                    "recipients": mailed_to,
                })

        for alert_key, row in existing.items():
            if alert_key in current_active_keys:
                continue
            if str(row.get("status") or "").lower() == "active":
                conn.execute(
                    "UPDATE alert_state SET status = 'cleared', last_seen = ? WHERE alert_key = ?",
                    (now, alert_key),
                )
        conn.commit()
    return {
        "checked": checked,
        "evaluated": evaluated,
        "sent": sent,
        "alert_state": _alert_state_summary(),
    }


def run_threshold_notifications_all_enabled():
    checked = []
    evaluated = []
    sent = []
    for env in _list_environments(include_secret=False):
        if not env.get("enabled"):
            continue
        result = run_threshold_notifications(env["id"])
        checked.extend(result.get("checked") or [])
        evaluated.extend(result.get("evaluated") or [])
        sent.extend(result.get("sent") or [])
    return {
        "checked": checked,
        "evaluated": evaluated,
        "sent": sent,
        "alert_state": _alert_state_summary(),
    }


def describe_threshold_rule_py(rule):
    if not isinstance(rule, dict):
        return "-"
    parts = []
    if rule.get("warn_op"):
        parts.append(f"Warn: {rule.get('warn_op')} {rule.get('warn_value')}")
    if rule.get("crit_op"):
        parts.append(f"Crit: {rule.get('crit_op')} {rule.get('crit_value')}")
    return " | ".join(parts) if parts else "-"


def _threshold_dataset_configs(cfg):
    portal_cfg = cfg.get("portal") or {}
    pg_cfg = cfg.get("postgres") or {}
    datasets = []

    tenants_src = cfg.get("tenants_csv") or ""
    if tenants_src:
        datasets.append({"key": "tenants", "label": "Tenants", "kind": "tenants", "path": tenants_src})

    edge_src = cfg.get("csv_path") or ""
    if edge_src:
        datasets.append({"key": "edge", "label": "Edge Filers", "kind": "edge", "path": edge_src})

    if portal_cfg.get("servers_csv"):
        datasets.append({"key": "portal_servers", "label": "Portal Servers", "kind": "portal", "section": "servers", "path": portal_cfg.get("servers_csv")})
    if portal_cfg.get("storage_csv"):
        datasets.append({"key": "portal_storage", "label": "Portal Storage", "kind": "portal", "section": "storage", "path": portal_cfg.get("storage_csv")})
    if portal_cfg.get("tasks_csv"):
        datasets.append({"key": "portal_tasks", "label": "Portal Tasks", "kind": "portal", "section": "tasks", "path": portal_cfg.get("tasks_csv")})

    metrics_csv = (cfg.get("servers_health") or {}).get("metrics_csv")
    if metrics_csv:
        datasets.append({"key": "servers_health", "label": "Servers Health", "kind": "servers_health", "path": metrics_csv})

    base_dir = pg_cfg.get("base_dir") or ""
    for topic, filename in (pg_cfg.get("topics") or {}).items():
        if filename:
            datasets.append({
                "key": f"postgres:{topic}",
                "label": f"Postgres: {re.sub(r'[_]+', ' ', topic).title()}",
                "kind": "postgres",
                "topic": topic,
                "path": os.path.join(base_dir, filename),
            })
    return datasets


def _current_value_summary(rows, field):
    values = [r.get(field, "") for r in rows if str(r.get(field, "")).strip() != ""]
    if not values:
        return {"kind": "empty", "summary": "No current values found in the loaded CSV.", "count": 0, "examples": []}

    numeric = []
    for value in values:
        num = _num(value)
        if num is not None:
            numeric.append(num)

    if numeric and len(numeric) >= max(3, int(len(values) * 0.6)):
        avg = sum(numeric) / len(numeric)
        summary = f"Current values across {len(numeric)} rows. Min {min(numeric):.2f}, Avg {avg:.2f}, Max {max(numeric):.2f}."
        return {
            "kind": "numeric",
            "summary": summary,
            "count": len(numeric),
            "examples": [f"{n:.2f}" for n in sorted(numeric, reverse=True)[:5]],
            "min": round(min(numeric), 2),
            "avg": round(avg, 2),
            "max": round(max(numeric), 2),
        }

    counter = Counter(str(v).strip() for v in values if str(v).strip())
    common = counter.most_common(5)
    summary = ", ".join(f"{label} ({count})" for label, count in common) if common else "No repeated values"
    return {
        "kind": "categorical",
        "summary": f"Current values across {len(values)} rows. Most common: {summary}.",
        "count": len(values),
        "examples": [label for label, _ in common],
        "top_values": [{"label": label, "count": count} for label, count in common],
    }


def _normalize_rule_for_editor(rule):
    out = {
        "warn_op": "",
        "warn_value": "",
        "crit_op": "",
        "crit_value": "",
    }
    if not isinstance(rule, dict):
        return out

    def first_op(rule_dict):
        if not isinstance(rule_dict, dict):
            return "", ""
        for op in ("gt", "ge", "lt", "le", "eq", "ne"):
            if op in rule_dict:
                value = rule_dict.get(op)
                if isinstance(value, bool):
                    value = "true" if value else "false"
                return op, str(value)
        return "", ""

    crit_rule = rule.get("crit") or rule.get("critical")
    warn_rule = rule.get("warn") or rule.get("warning")
    out["crit_op"], out["crit_value"] = first_op(crit_rule)
    out["warn_op"], out["warn_value"] = first_op(warn_rule)

    if not out["warn_op"] and not out["crit_op"]:
        base_style = str(rule.get("style") or "").strip().lower()
        base_op, base_value = first_op(rule)
        if base_style in ("warn", "warning"):
            out["warn_op"], out["warn_value"] = base_op, base_value
        elif base_style in ("crit", "critical", "bad"):
            out["crit_op"], out["crit_value"] = base_op, base_value
        elif base_op:
            out["crit_op"], out["crit_value"] = base_op, base_value
    return out


def _dataset_rule_container(doc, dataset_key, create=False):
    if dataset_key == "edge":
        filers = doc.setdefault("filers", {}) if create else (doc.get("filers") or {})
        return filers.setdefault("default", {}) if create else (filers.get("default") or {})
    if dataset_key == "tenants":
        tenants = doc.setdefault("tenants", {}) if create else (doc.get("tenants") or {})
        return tenants.setdefault("default", {}) if create else (tenants.get("default") or {})
    if dataset_key == "servers_health":
        sec = doc.setdefault("servers_health", {}) if create else (doc.get("servers_health") or {})
        return sec.setdefault("default", {}) if create else (sec.get("default") or {})
    if dataset_key.startswith("portal_"):
        sec_name = dataset_key.replace("portal_", "", 1)
        portal = doc.setdefault("portal", {}) if create else (doc.get("portal") or {})
        sec = portal.setdefault(sec_name, {}) if create else (portal.get(sec_name) or {})
        return sec.setdefault("default", {}) if create else (sec.get("default") or {})
    if dataset_key.startswith("postgres:"):
        topic = dataset_key.split(":", 1)[1]
        postgres = doc.setdefault("postgres", {}) if create else (doc.get("postgres") or {})
        sec = postgres.setdefault(topic, {}) if create else (postgres.get(topic) or {})
        return sec.setdefault("default", {}) if create else (sec.get("default") or {})
    return {}


def _prune_empty_threshold_sections(doc, dataset_key):
    if dataset_key == "edge":
        if not ((doc.get("filers") or {}).get("default")):
            (doc.get("filers") or {}).pop("default", None)
        if not (doc.get("filers") or {}):
            doc.pop("filers", None)
        return
    if dataset_key == "tenants":
        if not ((doc.get("tenants") or {}).get("default")):
            (doc.get("tenants") or {}).pop("default", None)
        if not (doc.get("tenants") or {}):
            doc.pop("tenants", None)
        return
    if dataset_key == "servers_health":
        if not ((doc.get("servers_health") or {}).get("default")):
            (doc.get("servers_health") or {}).pop("default", None)
        if not (doc.get("servers_health") or {}):
            doc.pop("servers_health", None)
        return
    if dataset_key.startswith("portal_"):
        sec_name = dataset_key.replace("portal_", "", 1)
        portal = doc.get("portal") or {}
        sec = portal.get(sec_name) or {}
        if not (sec.get("default") or {}):
            sec.pop("default", None)
        if not sec:
            portal.pop(sec_name, None)
        if not portal:
            doc.pop("portal", None)
        return
    if dataset_key.startswith("postgres:"):
        topic = dataset_key.split(":", 1)[1]
        postgres = doc.get("postgres") or {}
        sec = postgres.get(topic) or {}
        if not (sec.get("default") or {}):
            sec.pop("default", None)
        if not sec:
            postgres.pop(topic, None)
        if not postgres:
            doc.pop("postgres", None)


def _build_threshold_catalog(cfg, env_id=None):
    doc, th_path = _load_external_thresholds(cfg)
    catalog = []
    if env_id:
        dataset_sources = []
        for ds in _threshold_dataset_configs(cfg):
            rows, headers = read_csv_rows(ds["path"])
            if ds["key"] == "edge":
                rows, headers = derive_fields(rows, headers, cfg)
            if ds["key"] == "portal_tasks":
                rows = filter_dashboard_tasks(rows)
            dataset_sources.append((ds, rows, headers))
        source_label = (_get_environment(env_id, include_secret=False) or {}).get("name") or "Selected environment"
    else:
        merged = {}
        source_label = "All enabled portal environments"
        for env in _list_environments(include_secret=False):
            if not env.get("enabled"):
                continue
            env_cfg = load_conf_for_environment(env["id"])
            for ds in _threshold_dataset_configs(env_cfg):
                rows, headers = read_csv_rows(ds["path"])
                if ds["key"] == "edge":
                    rows, headers = derive_fields(rows, headers, env_cfg)
                if ds["key"] == "portal_tasks":
                    rows = filter_dashboard_tasks(rows)
                entry = merged.setdefault(ds["key"], {
                    "dataset": dict(ds),
                    "rows": [],
                    "headers": [],
                    "header_set": set(),
                })
                entry["rows"].extend(rows)
                for header in headers:
                    if header not in entry["header_set"]:
                        entry["header_set"].add(header)
                        entry["headers"].append(header)
        dataset_sources = [
            (entry["dataset"], entry["rows"], entry["headers"])
            for entry in merged.values()
        ]

    for ds, rows, headers in dataset_sources:
        rules = _dataset_rule_container(doc, ds["key"], create=False)
        fields = []
        for header in headers:
            fields.append({
                "name": header,
                "rule": _normalize_rule_for_editor(rules.get(header)),
                "current": _current_value_summary(rows, header),
                "notify": _load_threshold_notification(ds["key"], header),
            })
        catalog.append({
            "key": ds["key"],
            "label": ds["label"],
            "path": ds["path"],
            "row_count": len(rows),
            "fields": fields,
        })
    return {
        "path": th_path,
        "datasets": catalog,
        "recipients": _list_notification_recipients(),
        "notification_db_path": _notifications_db_path(),
        "alert_state": _alert_state_summary(),
        "source_label": source_label,
    }


def _coerce_threshold_value(value):
    text = str(value or "").strip()
    if text == "":
        return ""
    lower = text.lower()
    if lower in _TRUTHY:
        return True
    if lower in _FALSY and lower != "":
        return False
    if re.fullmatch(r"-?\d+", text):
        try:
            return int(text)
        except Exception:
            return text
    if re.fullmatch(r"-?\d+\.\d+", text):
        try:
            return float(text)
        except Exception:
            return text
    return text


# ---------------- smart clipping ----------------
def make_clip_check(ui_cfg):
    clip_by_default = bool(ui_cfg.get("clip_by_default", False))
    clip_cols = set(ui_cfg.get("clip_columns", []) or [])

    def looks_jsonish(val):
        if not val:
            return False
        s = str(val)
        return len(s) > 60 and any(c in s for c in '{}[]":')

    def clip_check(col, val):
        if col in clip_cols:
            return True
        if clip_by_default:
            return True
        return looks_jsonish(val)

    return clip_check, int(ui_cfg.get("max_cell_px", 360))


# ---------------- HTML ----------------
HTML = """
<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <title>{{ brand.title }}</title>
  {% if brand.icon %}<link rel="icon" type="image/png" href="{{ brand.icon }}">{% endif %}
  {% if refresh_seconds and refresh_seconds|int > 0 %}
  <meta http-equiv="refresh" content="{{ refresh_seconds|int }}">
  {% endif %}
  <style>
    @font-face {
      font-family: "Open Sans";
      src: local("Open Sans"), local("OpenSans");
      font-weight: 400;
      font-style: normal;
    }
    @font-face {
      font-family: "Open Sans";
      src: local("Open Sans Semibold"), local("OpenSans-Semibold"), local("Open Sans Bold"), local("OpenSans-Bold");
      font-weight: 600;
      font-style: normal;
    }
    :root{
      --bg: {{ theme.bg }}; --surface: {{ theme.surface }}; --text: {{ theme.text }};
      --muted: {{ theme.muted }}; --border: {{ theme.border }}; --header: {{ theme.header }};
      --hover: {{ theme.hover }}; --primary: {{ theme.primary }}; --accent: {{ theme.accent }};
      --crit: #ef4444; --warn: #f59e0b; --ok: #10b981; --muted2: #9ca3af;
      --crit-bg: #fee2e2; --warn-bg: #fef3c7; --ok-bg: #d1fae5; --muted-bg: #e5e7eb;
    }
    /* top horizontal scroller */
    .hscroll { height: 16px; overflow-x: auto; overflow-y: hidden; border: 1px solid var(--border);
           border-radius: 6px; background: #fff; margin-bottom: 8px; }
    .hscroll-inner { height: 1px; }
    .scrollshadow { position: relative; }
    .scrollshadow::before, .scrollshadow::after {
        content: ""; position: absolute; top: 0; bottom: 0; width: 14px; pointer-events: none;
    }
    .scrollshadow::before { left: 0; background: linear-gradient(to right, rgba(0,0,0,0.10), rgba(0,0,0,0)); opacity: 0; transition: opacity .2s; }
    .scrollshadow::after  { right: 0; background: linear-gradient(to left,  rgba(0,0,0,0.10), rgba(0,0,0,0)); opacity: 1; transition: opacity .2s; }
    .scrollshadow.at-left::before { opacity: 0; }
    .scrollshadow.at-left::after  { opacity: 1; }
    .scrollshadow.at-right::before{ opacity: 1; }
    .scrollshadow.at-right::after { opacity: 0; }

    body { background: rgb(242, 243, 247); color: rgb(64, 95, 110); font-family: "Open Sans", "Segoe UI", Tahoma, Arial, sans-serif; font-size:14px; font-weight:400; line-height:1.42857; margin: 0; }
    .app-shell { display:grid; grid-template-columns: 258px minmax(0, 1fr); min-height:100vh; }
    .sidebar { background:#14152b; color:#e5e7eb; border-right:1px solid rgba(148,163,184,0.14); }
    .sidebar-brand { display:flex; align-items:center; gap:12px; padding:18px 18px 16px; background:#ffffff; border-bottom:3px solid #5860ea; }
    .sidebar-brand img { display:block; height: {{ brand.logo_height }}px; }
    .sidebar-brand h1 { margin:0; color:#4f46e5; font-size: 22px; line-height:1.05; font-weight:700; }
    .sidebar-group { padding:12px 0; }
    .sidebar-label { padding:0 18px 10px; color:#94a3b8; font-size:11px; font-weight:700; text-transform:uppercase; letter-spacing:.08em; }
    .nav-sections { display:grid; gap:2px; }
    .nav-section { border-top:1px solid rgba(148,163,184,0.12); }
    .nav-section:first-child { border-top:none; }
    .nav-section.context-hidden { display:none; }
    body[data-initial-context="admin"] .nav-section[data-context="monitoring"] { display:none; }
    body[data-initial-context="env"] .nav-section[data-context="administration"] { display:none; }
    .nav-group-btn { display:flex; align-items:center; justify-content:space-between; gap:10px; width:100%; border:none; background:transparent; color:#d5d8e6; padding:13px 16px; cursor:pointer; font-family:inherit; font-size:14px; font-weight:400; line-height:20px; text-align:left; transition:background .18s ease, color .18s ease; }
    .nav-group-btn:hover { background:rgba(88,96,234,0.10); color:#ffffff; }
    .nav-section.expanded .nav-group-btn { background:rgba(255,255,255,0.03); color:#f8fafc; box-shadow:inset 0 -1px 0 rgba(255,255,255,0.04); }
    .nav-section.active .nav-group-btn { background:#5860ea; color:#ffffff; box-shadow:none; }
    .nav-group-title { display:inline-flex; align-items:center; gap:12px; min-width:0; font-weight:400; }
    .nav-group-title .tabicon { width:20px; height:20px; color:#c7d2fe; border-radius:4px; background:transparent; }
    .nav-group-title .tabicon svg { width:18px; height:18px; }
    .nav-group-btn:hover .tabicon,
    .nav-section.active .nav-group-btn .tabicon { color:#ffffff; background:transparent; }
    .nav-section.expanded .nav-group-btn .tabicon { color:#e2e8f0; background:transparent; }
    .nav-group-toggle { display:inline-flex; align-items:center; justify-content:center; width:22px; height:22px; border-radius:999px; color:inherit; font-size:20px; font-weight:400; line-height:1; flex:0 0 auto; }
    .nav-group-items { display:none; padding:8px 0 10px; background:#050505; }
    .nav-section.expanded .nav-group-items { display:grid; gap:2px; }
    .main-shell { min-width:0; display:flex; flex-direction:column; }
    .topbar { display:flex; align-items:center; justify-content:space-between; gap:14px; padding:18px 26px; background:#ffffff; border-bottom:1px solid #dbe4f0; box-shadow:none; }
    .topbar h2 { margin:0; color:rgb(44, 68, 83); font-size:16px; font-weight:700; line-height:24px; }
    .topbar-sub { color:rgb(64, 95, 110); font-size:13px; font-weight:400; line-height:20px; margin-top:4px; }
    .topbar-meta { display:flex; flex-wrap:wrap; gap:10px; align-items:center; justify-content:flex-end; }
    .top-context { display:flex; flex-direction:column; gap:4px; min-width:240px; }
    .top-context label { color:rgb(99, 118, 131); font-size:11px; font-weight:600; line-height:14px; text-transform:uppercase; letter-spacing:.04em; }
    .top-context select {
      width:100%;
      min-height:42px;
      border:1px solid #cfd8e3;
      border-radius:10px;
      background:#ffffff;
      color:rgb(64, 95, 110);
      font-family:'Open Sans','Segoe UI',Arial,sans-serif;
      font-size:14px;
      font-weight:600;
      line-height:20px;
      padding:10px 12px;
    }
    .top-pill { display:inline-flex; align-items:center; gap:8px; padding:8px 12px; border-radius:999px; background:#eef2ff; color:#3730a3; font-size:12px; font-weight:800; border:1px solid #c7d2fe; }
    .top-user { display:inline-flex; align-items:center; gap:8px; flex-wrap:wrap; }
    .top-user-name { display:inline-flex; align-items:center; gap:8px; padding:8px 12px; border-radius:999px; background:#f8fafc; color:rgb(44, 68, 83); font-size:12px; font-weight:700; border:1px solid #dbe4f0; }
    .top-user-name strong { font-weight:800; color:#1f2937; }
    .top-logout { display:inline-flex; align-items:center; justify-content:center; min-height:36px; padding:8px 12px; border-radius:999px; border:1px solid #dbe4f0; background:#ffffff; color:rgb(44, 68, 83); font-size:12px; font-weight:800; text-decoration:none; }
    .top-logout:hover { border-color:#c7d2fe; background:#f8fafc; color:#312e81; }
    .content-shell { padding:20px 24px 28px; min-width:0; }
    .env-note { margin-top:10px; padding:10px 12px; border-radius:10px; background:#f6f8fe; border:1px solid #d7ddf7; color:rgb(64, 95, 110); font-size:13px; line-height:19px; }
    .env-secret-hint { color:rgb(99, 118, 131); font-size:12px; line-height:18px; margin-top:6px; min-height:36px; }
    .modal-backdrop { position:fixed; inset:0; background:rgba(8, 15, 30, 0.48); display:none; align-items:center; justify-content:center; z-index:10000; padding:24px; }
    .modal-backdrop.open { display:flex; }
    .modal-panel { width:min(980px, 96vw); max-height:88vh; overflow:auto; background:#fff; border:1px solid var(--border); border-radius:10px; box-shadow:0 24px 60px rgba(15,23,42,0.28); }
    .modal-head { display:flex; align-items:flex-start; justify-content:space-between; gap:14px; padding:18px 20px 10px; border-bottom:1px solid var(--border); background:#fff; }
    .modal-head h3 { margin:0; color:var(--primary); font-size:20px; font-weight:600; }
    .modal-sub { color:rgb(64, 95, 110); font-size:13px; line-height:20px; margin-top:4px; }
    .modal-body { padding:18px 20px 20px; }
    .modal-close { border:1px solid var(--border); background:#fff; color:var(--muted); border-radius:8px; min-width:40px; min-height:40px; font-size:22px; line-height:1; cursor:pointer; }
    .portal-crumb { color:var(--accent); font-size:14px; font-weight:600; margin-bottom:6px; }
    .portal-crumb span { color:rgb(44, 68, 83); font-weight:500; }
    .portal-form-shell { background:#fff; border:1px solid var(--border); border-radius:8px; padding:18px; }
    .portal-form-title { margin:0 0 14px; color:rgb(44, 68, 83); font-size:16px; font-weight:700; }

    .hero-grid { display:grid; grid-template-columns: minmax(260px, 1.1fr) minmax(260px, .9fr); gap:14px; margin: 14px 0; }
    .hero-panel { background: var(--surface); border:1px solid var(--border); border-radius:8px; padding:14px; box-shadow: 0 8px 24px rgba(15,23,42,0.06); }
    .hero-title { display:flex; justify-content:space-between; gap:12px; align-items:flex-start; margin-bottom:10px; }
    .hero-title h2 { margin:0; color:var(--primary); font-size:22px; line-height:1.2; font-weight:600; }
    .hero-sub { color:rgb(64, 95, 110); font-size:14px; font-weight:400; line-height:22px; margin-top:4px; }
    .risk-pill { display:inline-flex; align-items:center; gap:6px; padding:5px 10px; border-radius:999px; font-weight:800; font-size:12px; white-space:nowrap; }
    .risk-high { background:var(--crit-bg); color:#7f1d1d; border:1px solid #fecaca; }
    .risk-medium { background:var(--warn-bg); color:#78350f; border:1px solid #fde68a; }
    .risk-low { background:var(--ok-bg); color:#064e3b; border:1px solid #a7f3d0; }
    .headline-metrics { display:grid; grid-template-columns: repeat(3, minmax(0,1fr)); gap:10px; margin-top:12px; }
    .headline-metric { border:1px solid var(--border); border-radius:8px; padding:10px; background:#fff; }
    .metric-label { color:var(--muted); font-size:12px; font-weight:700; text-transform:uppercase; }
    .metric-value { font-size:28px; font-weight:850; line-height:1.05; margin-top:4px; }
    .metric-value.crit { color:var(--crit); } .metric-value.warn { color:var(--warn); } .metric-value.ok { color:var(--ok); }
    .overview-grid { display:grid; grid-template-columns: repeat(2, minmax(260px, 1fr)); gap:12px; margin-top:12px; }
    .ops-toolbar { display:flex; align-items:flex-start; justify-content:space-between; gap:14px; margin:14px 0 0; padding:14px 0 0; border-top:1px solid var(--border); flex-wrap:wrap; }
    .ops-toolbar-copy { display:grid; gap:8px; }
    .ops-actions { display:flex; gap:10px; flex-wrap:wrap; }
    .ops-btn { display:inline-flex; align-items:center; justify-content:center; min-height:40px; border:1px solid #c7d2fe; background:#eef2ff; color:#3730a3; border-radius:8px; padding:10px 14px; font-weight:800; cursor:pointer; box-shadow:0 4px 12px rgba(99,102,241,0.10); transition:transform .15s ease, box-shadow .15s ease, border-color .15s ease, background .15s ease, color .15s ease; }
    .ops-btn:hover { transform:translateY(-1px); border-color:#818cf8; background:#e0e7ff; color:#312e81; box-shadow:0 8px 18px rgba(99,102,241,0.18); }
    .ops-btn.primary { border-color:#2563eb; background:linear-gradient(135deg,#4f46e5,#2563eb); color:#fff; box-shadow:0 10px 20px rgba(37,99,235,0.22); }
    .ops-btn.primary:hover { background:linear-gradient(135deg,#4338ca,#1d4ed8); color:#fff; border-color:#1d4ed8; }
    .ops-btn[disabled] { opacity:.55; cursor:wait; transform:none; box-shadow:none; }
    .ops-status-grid { display:grid; grid-template-columns: repeat(2, minmax(260px,1fr)); gap:12px; margin-top:12px; }
    .ops-status-card { border:1px solid var(--border); border-radius:8px; padding:14px; background:linear-gradient(180deg,#ffffff,#fafbff); box-shadow:0 8px 24px rgba(15,23,42,0.05); }
    .ops-status-head { display:flex; justify-content:space-between; gap:12px; align-items:flex-start; margin-bottom:10px; }
    .ops-status-head h3 { margin:0; font-size:18px; color:var(--primary); font-weight:600; }
    .ops-badge { display:inline-flex; align-items:center; border-radius:999px; padding:6px 12px; font-size:12px; font-weight:900; letter-spacing:.01em; white-space:nowrap; }
    .ops-badge.running { color:#1d4ed8; background:#dbeafe; }
    .ops-badge.finished { color:#166534; background:#dcfce7; }
    .ops-badge.failed { color:#b91c1c; background:#fee2e2; }
    .ops-badge.idle, .ops-badge.unknown { color:#475569; background:#e2e8f0; }
    .ops-meta { color:var(--muted); font-size:13px; display:flex; flex-wrap:wrap; gap:10px 16px; padding:10px 12px; border:1px solid var(--border); border-radius:8px; background:#fff; margin-bottom:10px; }
    .ops-meta span { display:inline-flex; align-items:center; gap:6px; }
    .ops-meta strong { color:var(--text); font-weight:800; }
    .ops-loghint { color:var(--muted); font-size:12px; margin-top:8px; }
    .ops-logtail { margin-top:8px; padding:10px; border-radius:8px; background:#0f172a; color:#e5e7eb; font:12px/1.45 Consolas, Monaco, monospace; max-height:124px; overflow:auto; white-space:pre-wrap; }
    .dash-card { border:1px solid var(--border); border-radius:8px; background:#fff; padding:12px; cursor:pointer; transition: transform .15s ease, box-shadow .15s ease, border-color .15s ease; }
    .dash-card:hover { transform: translateY(-1px); box-shadow:0 10px 28px rgba(15,23,42,0.08); border-color:var(--accent); }
    .dash-card-head { display:flex; align-items:flex-start; justify-content:space-between; gap:12px; margin-bottom:10px; }
    .dash-card h3 { margin:0; font-size:16px; color:var(--primary); font-weight:600; }
    .dash-card .count { font-size:26px; font-weight:850; line-height:1; }
    .count.crit { color:var(--crit); } .count.warn { color:var(--warn); } .count.ok { color:var(--ok); }
    .stack-bar { display:flex; overflow:hidden; height:12px; border-radius:999px; background:var(--muted-bg); border:1px solid var(--border); }
    .bar-crit { background:var(--crit); } .bar-warn { background:var(--warn); } .bar-ok { background:var(--ok); }
    .dash-card-foot { display:flex; flex-wrap:wrap; gap:8px; margin-top:10px; color:var(--muted); font-size:12px; }
    .mini-stat { display:inline-flex; align-items:center; gap:5px; }
    .ops-list { display:grid; gap:8px; margin-top:10px; }
    .ops-row { display:flex; justify-content:space-between; align-items:center; gap:12px; border-bottom:1px solid var(--border); padding:8px 0; }
    .ops-row:last-child { border-bottom:none; }
    .ops-row strong { color:var(--primary); }
    .ops-row span { color:var(--muted); font-size:13px; text-align:right; }
    .viz-grid { display:grid; grid-template-columns: repeat(3, minmax(220px, 1fr)); gap:12px; margin:12px 0; }
    .viz-grid.two { grid-template-columns: repeat(2, minmax(260px, 1fr)); }
    .viz-grid.tenant-summary { grid-template-columns: minmax(260px, .8fr) minmax(260px, 1.2fr); }
    .viz-panel { border:1px solid var(--border); border-radius:8px; background:linear-gradient(180deg,#fff,#fbfcff); padding:12px; box-shadow:0 6px 18px rgba(15,23,42,0.05); }
    .viz-panel h3 { margin:0 0 10px; color:var(--primary); font-size:15px; font-weight:600; }
    .bar-list { display:grid; gap:8px; }
    .bar-row { display:grid; grid-template-columns:minmax(90px, 1fr) minmax(120px, 1.6fr) auto; align-items:center; gap:8px; font-size:13px; }
    .bar-label { overflow:hidden; text-overflow:ellipsis; white-space:nowrap; color:var(--text); }
    .bar-track { display:block; height:13px; border-radius:999px; background:#e5e7eb; overflow:hidden; box-shadow:inset 0 1px 2px rgba(15,23,42,0.12); }
    .bar-fill { display:block; height:100%; border-radius:999px; background:var(--accent); min-width:2px; box-shadow:inset 0 -1px 0 rgba(0,0,0,0.12); }
    .bar-fill.crit { background:linear-gradient(90deg,#ef4444,#b91c1c); }
    .bar-fill.warn { background:linear-gradient(90deg,#f59e0b,#d97706); }
    .bar-fill.ok { background:linear-gradient(90deg,#10b981,#059669); }
    .bar-fill.info { background:linear-gradient(90deg,#6366f1,#2563eb); }
    .bar-fill.sync { background:linear-gradient(90deg,#22c55e,#059669); }
    .bar-fill.running { background:linear-gradient(90deg,#38bdf8,#2563eb); }
    .bar-fill.stalled { background:linear-gradient(90deg,#ef4444,#991b1b); }
    .bar-fill.deleted { background:linear-gradient(90deg,#94a3b8,#64748b); }
    .bar-fill.palette-0 { background:linear-gradient(90deg,#6366f1,#4338ca); }
    .bar-fill.palette-1 { background:linear-gradient(90deg,#06b6d4,#0891b2); }
    .bar-fill.palette-2 { background:linear-gradient(90deg,#10b981,#047857); }
    .bar-fill.palette-3 { background:linear-gradient(90deg,#f59e0b,#b45309); }
    .bar-fill.palette-4 { background:linear-gradient(90deg,#ec4899,#be185d); }
    .bar-fill.palette-5 { background:linear-gradient(90deg,#8b5cf6,#6d28d9); }
    .bar-value { color:var(--muted); font-variant-numeric: tabular-nums; }
    .gauge-grid { display:grid; grid-template-columns: repeat(2, minmax(120px, 1fr)); gap:10px; }
    .gauge { border:1px solid var(--border); border-radius:8px; padding:10px; background:#fff; }
    .gauge-name { color:var(--muted); font-size:12px; font-weight:700; text-transform:uppercase; }
    .gauge-value { font-size:24px; font-weight:850; margin:4px 0 8px; }
    .gauge-track { height:10px; border-radius:999px; background:#e5e7eb; overflow:hidden; box-shadow:inset 0 1px 2px rgba(15,23,42,0.12); }
    .gauge-fill { display:block; height:100%; border-radius:999px; background:linear-gradient(90deg,#10b981,#059669); }
    .gauge-fill.warn { background:linear-gradient(90deg,#f59e0b,#d97706); } .gauge-fill.crit { background:linear-gradient(90deg,#ef4444,#b91c1c); }
    .section-cards { display:grid; grid-template-columns: repeat(3, minmax(160px, 1fr)); gap:10px; margin:12px 0; }
    .section-card { border:1px solid var(--border); border-radius:8px; padding:10px; background:#fff; }
    .section-card strong { display:block; color:var(--primary); margin-bottom:8px; }
    .section-card .nums { display:flex; gap:10px; flex-wrap:wrap; color:var(--muted); font-size:13px; }
    .edge-table-shell { max-height:72vh; overflow:auto; border:1px solid var(--border); border-radius:8px; }
    .edge-table-shell table th { top:0; z-index:2; }
    @media (max-width: 900px) {
      .app-shell { grid-template-columns: 1fr; }
      .sidebar { border-right:none; border-bottom:1px solid rgba(148,163,184,0.18); }
      .content-shell { padding:16px; }
      .topbar { padding:14px 16px; }
      .hero-grid, .overview-grid, .viz-grid, .viz-grid.two, .viz-grid.tenant-summary, .section-cards, .threshold-layout, .threshold-grid, .threshold-form-grid, .threshold-kpis, .notify-grid, .notify-summary-grid { grid-template-columns: 1fr; }
      .headline-metrics, .gauge-grid { grid-template-columns: 1fr; }
    }

    .legend { display:flex; gap:10px; align-items:center; color: var(--muted); font-size: 14px; margin-bottom: 8px;}
    .dot { width:10px; height:10px; border-radius:999px; display:inline-block; margin-right:6px; }
    .dcrit { background: var(--crit); } .dwarn { background: var(--warn); } .dok { background: var(--ok); } .dmuted { background: var(--muted2); }

    .tabs { display:grid; gap:4px; }
    .tabbtn { display:flex; align-items:center; justify-content:space-between; gap:10px; width:100%; text-align:left; padding:12px 14px; border:none; border-radius:0; background:transparent; color:#d5d8e6; cursor:pointer; position:relative; font-family:inherit; font-size:14px; font-weight:400; line-height:21px; }
    .tabbtn:hover { background:rgba(255,255,255,0.05); color:#ffffff; }
    .tabbtn.active { background:transparent; color:#fff; box-shadow:none; }
    .tabbtn-text { display:inline-flex; align-items:center; gap:10px; min-width:0; }
    .tabicon { width:18px; height:18px; display:inline-flex; align-items:center; justify-content:center; color:#9aa7c7; flex:0 0 auto; }
    .tabicon svg { width:18px; height:18px; stroke:currentColor; fill:none; stroke-width:1.8; stroke-linecap:round; stroke-linejoin:round; }
    .tabbtn.active .tabicon, .tabbtn:hover .tabicon { color:#ffffff; }
    .tabbtn-meta { display:inline-flex; align-items:center; gap:6px; flex-wrap:wrap; justify-content:flex-end; }
    .tabbadge { display:inline-block; padding:0 6px; line-height:16px; font-size:12px; border-radius:999px; color:#fff; }
    .tabbadge.crit { background: var(--crit); }
    .tabbadge.warn { background: var(--warn); color:#111; }
    .nav-child { border-radius:0; padding:11px 16px 11px 42px; font-size:13px; color:#d5d8e6; }
    .nav-child:hover { background:rgba(255,255,255,0.06); color:#ffffff; }
    .nav-child.active { background:transparent; color:#ffffff; box-shadow:none; font-weight:600; }
    .nav-child .tabicon { width:16px; height:16px; color:#94a3b8; }
    .nav-child .tabicon svg { width:16px; height:16px; }
    .nav-child.active .tabicon,
    .nav-child:hover .tabicon { color:#ffffff; }
    .threshold-layout { display:grid; grid-template-columns: minmax(0, 340px) minmax(0, 1fr); gap:14px; margin-top:14px; align-items:start; }
    .threshold-sidebar { display:grid; gap:12px; min-width:0; overflow:hidden; }
    .threshold-card { background:#fff; border:1px solid var(--border); border-radius:8px; padding:14px; box-shadow:0 8px 24px rgba(15,23,42,0.05); min-width:0; overflow:hidden; }
    .threshold-card h3 { margin:0 0 10px; color:var(--primary); font-size:16px; font-weight:600; }
    .threshold-select, .threshold-input { width:100%; max-width:100%; min-width:0; min-height:40px; border:1px solid var(--border); border-radius:8px; padding:9px 11px; background:#fff; color:var(--text); font:inherit; }
    .threshold-grid { display:grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap:12px; }
    .threshold-form-grid { display:grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap:12px; margin-top:12px; align-items:start; }
    .threshold-form-grid > * { min-width:0; }
    .threshold-field { display:flex; flex-direction:column; gap:6px; align-self:start; min-width:0; }
    .threshold-field input, .threshold-field select, .threshold-field textarea { width:100%; max-width:100%; box-sizing:border-box; display:block; }
    .threshold-field label { color:var(--muted); font-size:12px; font-weight:700; text-transform:uppercase; }
    .threshold-kpis { display:grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap:10px; margin-top:12px; }
    .threshold-kpi { border:1px solid var(--border); border-radius:8px; background:#f8fafc; padding:10px; }
    .threshold-kpi .metric-label { font-size:11px; }
    .threshold-kpi .metric-value { font-size:20px; margin-top:2px; }
    .threshold-summary { color:rgb(64, 95, 110); font-size:14px; font-weight:400; line-height:22px; margin-top:10px; }
    .threshold-tags { display:flex; flex-wrap:wrap; gap:8px; margin-top:10px; }
    .threshold-tag { display:inline-flex; align-items:center; padding:6px 10px; border-radius:999px; background:#eef2ff; color:#3730a3; font-size:12px; font-weight:700; }
    .threshold-actions { display:flex; gap:10px; flex-wrap:wrap; margin-top:14px; }
    .threshold-status { margin-top:12px; font-size:13px; color:var(--muted); }
    .action-status { display:none; align-items:center; gap:8px; padding:10px 12px; border-radius:8px; border:1px solid var(--border); background:#f8fafc; color:var(--text); font-size:13px; font-weight:600; min-height:20px; }
    .action-status.show { display:flex; }
    .action-status.working { background:#eff6ff; border-color:#bfdbfe; color:#1d4ed8; }
    .action-status.success { background:#ecfdf5; border-color:#a7f3d0; color:#047857; }
    .action-status.error { background:#fef2f2; border-color:#fecaca; color:#b91c1c; }
    .threshold-path { color:var(--muted); font-size:13px; line-height:1.5; }
    .threshold-empty { color:var(--muted); font-size:14px; padding:14px 0; }
    .threshold-list { margin-top:12px; border:1px solid var(--border); border-radius:8px; overflow:hidden; background:#fff; }
    .threshold-list table { width:100%; min-width:100%; font-size:13px; }
    .threshold-list th { position:static; background:#f8fafc; color:var(--muted); font-size:11px; text-transform:uppercase; letter-spacing:.04em; }
    .threshold-list td, .threshold-list th { padding:10px 12px; }
    .threshold-list tr { cursor:pointer; }
    .threshold-list tr:hover td { background:#f8fbff; }
    .threshold-list code { font-size:12px; }
    .threshold-row-actions { display:flex; gap:6px; justify-content:flex-end; }
    .threshold-row-btn { border:1px solid var(--border); background:#fff; color:var(--text); border-radius:6px; padding:5px 8px; font-size:12px; font-weight:700; cursor:pointer; }
    .threshold-row-btn.delete { border-color:#fecaca; color:#b91c1c; background:#fff5f5; }
    .threshold-master-head { display:flex; justify-content:space-between; gap:12px; align-items:flex-start; margin-bottom:12px; flex-wrap:wrap; }
    .notify-grid { display:grid; grid-template-columns:minmax(320px,360px) minmax(0,1fr); gap:14px; margin-top:14px; }
    .notify-summary-grid { display:grid; grid-template-columns:repeat(3, minmax(120px,1fr)); gap:10px; margin-bottom:12px; }
    .notify-checkbox { display:grid; grid-template-columns:18px minmax(0, 1fr); align-items:start; gap:8px; font-weight:600; color:var(--text); }
    .notify-checkbox input { margin:2px 0 0; padding:0; width:16px; height:16px; justify-self:start; }
    .notify-helper { color:var(--muted); font-size:12px; line-height:1.5; margin-top:6px; }
    .notify-actions { display:flex; gap:10px; flex-wrap:wrap; margin-top:14px; }
    .notify-table-wrap { border:1px solid var(--border); border-radius:8px; overflow:hidden; background:#fff; margin-top:12px; }
    .notify-table-wrap table { width:100%; min-width:100%; }
    .notify-empty { color:var(--muted); font-size:14px; padding:14px 0; }

    .tabpane { border:1px solid var(--border); border-radius: 0 8px 8px 8px; padding: 12px; background: var(--surface); }

    /* sub-tabs (for Postgres) */
    .subtabs { display:flex; gap:8px; margin: 8px 0 12px; flex-wrap: wrap; }
    .subbtn, .healthsubbtn, .portalsubbtn { padding:6px 10px; border:1px solid var(--border); border-bottom:none; border-top-left-radius:8px; border-top-right-radius:8px; background:#fff; color: var(--primary); cursor:pointer; position: relative; font-family:inherit; font-size:14px; font-weight:400; }
    .subbtn.active, .healthsubbtn.active, .portalsubbtn.active { border-color: var(--accent); color:#fff; background: var(--accent); }
    .badge { position:absolute; top:-8px; right:-8px; background:#ec4899; color:#fff; font-size:12px; line-height:16px; padding:0 6px; border-radius: 999px; border:2px solid #fff; }

    .controls { display:flex; flex-wrap:wrap; gap:8px; align-items:center; margin-bottom: 8px; }
    input, select, button { padding: 6px 10px; border: 1px solid var(--border); border-radius: 8px; background: #fff; color: rgb(64, 95, 110); cursor: pointer; font-family: inherit; font-size:14px; font-weight:400; line-height:21px; }
    input, select, textarea { position:relative; z-index:0; }
    input:focus, select:focus, textarea:focus {
      outline:none;
      border-color: var(--accent);
      box-shadow: 0 0 0 2px rgba(37, 99, 235, 0.18);
      z-index:0;
    }
    .sub { color: rgb(64, 95, 110); margin-bottom: 8px; font-size:14px; font-weight:400; line-height:22px; }

    .table-wrap { overflow-x: auto; }
    table { border-collapse: collapse; width: max-content; min-width: 100%; font-size: 14px; background: var(--surface); }
    th, td { border-bottom: 1px solid var(--border); padding: 8px 10px; text-align: left; vertical-align: top; font-family: inherit; line-height:22px; }
    th     { background: var(--header); position: sticky; top: 0; color: var(--primary); font-weight:600; }
    thead tr { border-bottom: 2px solid var(--primary); }
    tr:hover { background: var(--hover); }

    /* severity classes from thresholds.yaml style: */
    .sev-critical { background: var(--crit-bg); }
    .sev-warning  { background: var(--warn-bg); }
    .sev-muted    { color: var(--muted); }
    .sev-ok       { background: var(--ok-bg); }

    /* zebra striping for all tables, but don't override severity colors */
    tbody tr:nth-child(odd) td:not(.sev-critical):not(.sev-warning):not(.sev-ok):not(.sev-muted){
      background: #f9fafb;  /* light grey */
    }

    tbody tr:nth-child(even) td:not(.sev-critical):not(.sev-warning):not(.sev-ok):not(.sev-muted){
      background: #ffffff;  /* white */
    }


    /* boolean pills */
    .pill { display:inline-block; padding:2px 8px; border-radius:999px; font-weight:700; font-size:12px; border:1px solid var(--border); }
    .pill-ok   { background: var(--ok-bg); color:#064e3b; }
    .pill-bad  { background: var(--crit-bg); color:#7f1d1d; }
    .pill-info { background: var(--muted-bg); color:#374151; }
    .pill-muted { background:#e2e8f0; color:#475569; }

    .clipcell { max-width: {{ max_cell_px }}px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    .cell-actions { display:flex; gap:6px; margin-top:4px; }
    .btn-xs { font-size: 12px; padding: 4px 8px; border-radius: 6px; }

    .viewer-backdrop { position: fixed; inset: 0; background: rgba(0,0,0,0.5); display: none; align-items: center; justify-content: center; z-index: 9999;}
    .viewer { background: #fff; border-radius: 10px; width: min(900px, 92vw); max-height: 80vh; padding: 12px; border:1px solid var(--border); display:flex; flex-direction:column; gap:8px;}
    .viewer pre { margin:0; padding:10px; background: var(--header); border-radius: 8px; overflow:auto; max-height:60vh; }
    .viewer header { display:flex; justify-content: space-between; align-items:center; gap:8px; }
    .viewer header h3 { margin:0; font-size: 16px; color: var(--primary); }

    /* AI summary card */
    .ai-output{
      white-space: pre-wrap;
      background: #fff;
      border-radius: 12px;
      padding: 10px 14px;
      border: 1px solid var(--border);
      box-shadow: 0 2px 4px rgba(15,23,42,0.06);
      line-height: 1.4;      /* tighter line spacing */
      font-size: 15px;       /* slightly bigger font */
      margin-top: 8px;
    }

    .ai-output h3{
      margin: 2px 0 4px 0;   /* less vertical space around headings */
      font-size: 16px;
    }

    .ai-output ul{
      margin: 2px 0 6px 1.3rem; /* reduce gap between bullets */
      padding-left: 0;
    }

    .ai-output li{
      margin: 2px 0;         /* tighter gap between list items */
    }

    .ai-output p{
      margin: 2px 0;         /* tighter gap for paragraphs */
    }


    .ai-critical{ color: var(--crit); font-weight: 600; }
    .ai-warning{ color: var(--warn); font-weight: 600; }
    .ai-ok{ color: var(--ok); font-weight: 600; }
    .ai-muted{ color: var(--muted2); }

    /* === OVERRIDE: vertical scrolling handled by page (Edge table big) === */
    .table-wrap { overflow-x: auto; overflow-y: visible; }
    #edgeWrap   { overflow: auto; max-height: 72vh; }
    .pane.active { overflow: visible; }
  </style>

  <script>
    function syncEdgeScrollerSetup(){
      const top = document.getElementById('edgeTopScroll');
      const topInner = document.getElementById('edgeTopInner');
      const wrap = document.getElementById('edgeWrap');
      const table = document.getElementById('edgeTable');
      if (!top || !wrap || !table || !topInner) return;

      const setWidths = () => {
        topInner.style.width = table.scrollWidth + 'px';
        updateShadows();
      };
      const syncFromTop = () => { wrap.scrollLeft = top.scrollLeft; updateShadows(); };
      const syncFromWrap = () => { top.scrollLeft = wrap.scrollLeft; updateShadows(); };
      const updateShadows = () => {
        const sc = wrap.scrollLeft;
        const max = table.scrollWidth - wrap.clientWidth - 1;
        if (sc <= 0) { wrap.classList.add('at-left');  wrap.classList.remove('at-right'); }
        else if (sc >= max) { wrap.classList.add('at-right'); wrap.classList.remove('at-left'); }
        else { wrap.classList.remove('at-left'); wrap.classList.remove('at-right'); }
      };

      top.removeEventListener('scroll', syncFromTop);  top.addEventListener('scroll', syncFromTop, {passive:true});
      wrap.removeEventListener('scroll', syncFromWrap); wrap.addEventListener('scroll', syncFromWrap, {passive:true});
      window.addEventListener('resize', setWidths);
      setTimeout(setWidths, 0); setTimeout(setWidths, 300);
    }

    function saveActive(tab){
      try{ localStorage.setItem('fd.activeTab', tab); }catch(e){}
    }
    function loadActive(){
      try{ return localStorage.getItem('fd.activeTab') || 'overview'; }catch(e){ return 'overview'; }
    }
    function saveEnvironmentContext(value){
      try{ localStorage.setItem('fd.environmentContext', value || 'admin'); }catch(e){}
    }
    function currentQueryEnvironmentId(){
      try {
        const params = new URLSearchParams(window.location.search || '');
        return params.get('env') || '';
      } catch (e) {
        return '';
      }
    }
    function loadEnvironmentContext(){
      const fromQuery = currentQueryEnvironmentId();
      if (fromQuery) return fromQuery;
      try{ return localStorage.getItem('fd.environmentContext') || 'admin'; }catch(e){ return 'admin'; }
    }
    function effectiveEnvironmentContext(){
      const raw = loadEnvironmentContext();
      if (raw === 'admin') return 'admin';
      const items = environmentConfig.items || [];
      return items.some(item => String(item.id) === String(raw)) ? String(raw) : 'admin';
    }
    function isAdministrationContext(){
      return effectiveEnvironmentContext() === 'admin';
    }
    function apiUrl(path){
      const envId = effectiveEnvironmentContext();
      if (!envId || envId === 'admin') return path;
      const joiner = path.indexOf('?') >= 0 ? '&' : '?';
      return path + joiner + 'env=' + encodeURIComponent(envId);
    }
    function navigateToEnvironment(envId){
      saveEnvironmentContext(envId || 'admin');
      const url = new URL(window.location.href);
      if (!envId || envId === 'admin') {
        url.searchParams.delete('env');
      } else {
        url.searchParams.set('env', envId);
      }
      window.location.href = url.toString();
    }
    const NAV_SECTION_MAP = {
      overview: 'dashboard',
      jobs: 'dashboard',
      tenants: 'portal',
      portal: 'portal',
      pg: 'portal',
      svrhlth: 'portal',
      edge: 'edge',
      admin_prereq: 'admin_main',
      admin_env: 'admin_main',
      thresholds: 'admin_thresholds',
      thresholds_all: 'admin_thresholds',
      notify_settings: 'admin_notifications',
      notify_recipients: 'admin_notifications',
      auth_settings: 'admin_auth',
      about: 'admin_help'
    };
    const NAV_SECTION_DEFAULT_TAB = {
      dashboard: 'overview',
      portal: 'portal',
      edge: 'edge',
      admin_main: 'admin_prereq',
      admin_thresholds: 'thresholds',
      admin_notifications: 'notify_settings',
      admin_auth: 'auth_settings',
      admin_help: 'about'
    };
    function loadNavExpanded(){
      try {
        return JSON.parse(localStorage.getItem('fd.navExpanded') || '{}');
      } catch (e) {
        return {};
      }
    }
    function saveNavExpanded(state){
      try {
        localStorage.setItem('fd.navExpanded', JSON.stringify(state || {}));
      } catch (e) {}
    }
    function applyNavExpanded(sectionId, expanded){
      const section = document.querySelector('.nav-section[data-section="' + sectionId + '"]');
      if (!section) return;
      section.classList.toggle('expanded', !!expanded);
      const toggle = section.querySelector('.nav-group-toggle');
      if (toggle) toggle.textContent = expanded ? '−' : '+';
      section.setAttribute('aria-expanded', expanded ? 'true' : 'false');
    }
    function setOnlyExpanded(sectionId){
      const state = {};
      document.querySelectorAll('.nav-section').forEach(section => {
        const key = section.getAttribute('data-section');
        const expanded = key === sectionId;
        state[key] = expanded;
        applyNavExpanded(key, expanded);
      });
      saveNavExpanded(state);
    }
    function toggleNavSection(sectionId){
      const section = document.querySelector('.nav-section[data-section="' + sectionId + '"]');
      if (!section) return;
      if (section.classList.contains('active')) return;
      showTab(NAV_SECTION_DEFAULT_TAB[sectionId] || 'overview');
    }
    function syncNavSections(activeTabId){
      const activeSection = NAV_SECTION_MAP[activeTabId] || 'dashboard';
      document.querySelectorAll('.nav-section').forEach(section => {
        section.classList.toggle('active', section.getAttribute('data-section') === activeSection);
      });
      setOnlyExpanded(activeSection);
    }
    function showTab(id){
      document.querySelectorAll('.tabpane').forEach(p => p.style.display = 'none');
      const pane = document.getElementById(id);
      if (pane) pane.style.display = '';
      document.querySelectorAll('.tabbtn').forEach(b => b.classList.remove('active'));
      const btn = document.querySelector('[data-tab="'+id+'"]');
      if (btn) btn.classList.add('active');
      syncNavSections(id);
      saveActive(id);
      if (id === 'edge') {
        syncEdgeScrollerSetup();
      }
    }

    // Postgres sub-tabs
    function savePgActive(sub){
      try{ localStorage.setItem('fd.pgActive', sub); }catch(e){}
    }
    function loadPgActive(){
      try{ return localStorage.getItem('fd.pgActive') || 'pg_overview'; }catch(e){ return 'pg_overview'; }
    }
    function showPgTab(id){
      document.querySelectorAll('.pgpane').forEach(p => p.style.display = 'none');
      document.getElementById(id).style.display = '';
      document.querySelectorAll('.subbtn').forEach(b => b.classList.remove('active'));
      const btn = document.querySelector('[data-sub="'+id+'"]');
      if (btn) btn.classList.add('active');
      savePgActive(id);
    }

    function savePortalActive(sub){
      try{ localStorage.setItem('fd.portalActive', sub); }catch(e){}
    }
    function loadPortalActive(){
      try{ return localStorage.getItem('fd.portalActive') || 'portal_overview'; }catch(e){ return 'portal_overview'; }
    }
    function showPortalTab(id){
      document.querySelectorAll('.portalpane').forEach(p => p.style.display = 'none');
      const pane = document.getElementById(id);
      if (pane) pane.style.display = '';
      document.querySelectorAll('.portalsubbtn').forEach(b => b.classList.remove('active'));
      const btn = document.querySelector('[data-portal-sub="'+id+'"]');
      if (btn) btn.classList.add('active');
      savePortalActive(id);
    }

    function saveHealthActive(sub){
      try{ localStorage.setItem('fd.healthActive', sub); }catch(e){}
    }
    function loadHealthActive(){
      try{ return localStorage.getItem('fd.healthActive') || 'health_overview'; }catch(e){ return 'health_overview'; }
    }
    function showHealthTab(id){
      document.querySelectorAll('.healthpane').forEach(p => p.style.display = 'none');
      const pane = document.getElementById(id);
      if (pane) pane.style.display = '';
      document.querySelectorAll('.healthsubbtn').forEach(b => b.classList.remove('active'));
      const btn = document.querySelector('[data-health-sub="'+id+'"]');
      if (btn) btn.classList.add('active');
      saveHealthActive(id);
    }

    function filterTableByInput(tableId, inputId){
      const q = (document.getElementById(inputId).value || '').toLowerCase();
      const rows = document.querySelectorAll('#'+tableId+' tbody tr');
      rows.forEach(tr => {
        const text = tr.innerText.toLowerCase();
        tr.style.display = (!q || text.indexOf(q) !== -1) ? '' : 'none';
      });
    }

    
    // Edge severity filter (all / crit / warn / crit+warn / none)
    function filterEdgeSeverity(){
      const sel = document.getElementById('edgeSeverityFilter');
      const tbody = document.querySelector('#edgeTable tbody');
      if (!sel || !tbody) return;

      const mode = sel.value || 'all';
      const rows = tbody.querySelectorAll('tr');

      rows.forEach(tr => {
        const cells = tr.querySelectorAll('td');
        let hasCrit = false;
        let hasWarn = false;

        cells.forEach(td => {
          if (td.classList.contains('sev-critical')) {
            hasCrit = true;
          }
          if (td.classList.contains('sev-warning')) {
            hasWarn = true;
          }
        });

        let show = true;
        if (mode === 'crit') {
          show = hasCrit;
        } else if (mode === 'warn') {
          show = hasWarn && !hasCrit;
        } else if (mode === 'critwarn') {
          show = hasCrit || hasWarn;
        } else if (mode === 'none') {
          show = !hasCrit && !hasWarn;
        } else { // 'all'
          show = true;
        }

        tr.style.display = show ? '' : 'none';
      });
    }

    // Postgres severity filter (per sub-tab)
    function filterPgSeverity(key){
      const sel = document.getElementById('pgSeverityFilter_' + key);
      const tbody = document.querySelector('#pgTable_' + key + ' tbody');
      if (!sel || !tbody) return;

      const mode = sel.value || 'all';
      const rows = tbody.querySelectorAll('tr');

      rows.forEach(tr => {
        const cells = tr.querySelectorAll('td');
        let hasCrit = false;
        let hasWarn = false;

        cells.forEach(td => {
          if (td.classList.contains('sev-critical')) {
            hasCrit = true;
          }
          if (td.classList.contains('sev-warning')) {
            hasWarn = true;
          }
        });

        let show = true;
        if (mode === 'crit') {
          show = hasCrit;
        } else if (mode === 'warn') {
          show = hasWarn && !hasCrit;
        } else if (mode === 'critwarn') {
          show = hasCrit || hasWarn;
        } else if (mode === 'none') {
          show = !hasCrit && !hasWarn;
        } else { // 'all'
          show = true;
        }

        tr.style.display = show ? '' : 'none';
      });
    }

async function runAISummary(){
      const out = document.getElementById('aiOutput');
      const ts  = document.getElementById('aiTimestamp');
      const st  = document.getElementById('aiStatus');

      if (!out || !ts || !st) return;

      // keep the old summary visible, just show status
      st.textContent = "Generating…Hang tight, this usually takes 10-12 seconds";

      try {
        const resp = await fetch(apiUrl('/ai_summary'));
        const data = await resp.json();
        out.innerHTML = data.summary || "<p>No summary returned.</p>";

        const now = new Date();
        ts.textContent = "Last generated: " + now.toLocaleString();
        st.textContent = "";
      } catch (e) {
        st.textContent = "Error calling AI: " + e;
      }
    }

    function formatLocalTimestamp(value){
      if (!value) return '—';
      const normalized = String(value).trim().replace(' UTC', 'Z');
      const dt = new Date(normalized);
      if (Number.isNaN(dt.getTime())) return value;
      try {
        return new Intl.DateTimeFormat([], {
          year: 'numeric',
          month: '2-digit',
          day: '2-digit',
          hour: '2-digit',
          minute: '2-digit',
          second: '2-digit',
          hour12: false,
          timeZoneName: 'short'
        }).format(dt);
      } catch (e) {
        return dt.toLocaleString();
      }
    }

    function hydrateLocalTimes(){
      document.querySelectorAll('[data-local-time]').forEach(el => {
        const value = el.getAttribute('data-local-time') || '';
        el.textContent = formatLocalTimestamp(value);
      });
    }

    async function refreshJobStatus(){
      try{
        const resp = await fetch('/job_status');
        const data = await resp.json();
        ['portal','filer'].forEach(name => {
          const card = data[name] || {};
          const badge = document.getElementById('jobBadge_' + name);
          const started = document.getElementById('jobStarted_' + name);
          const finished = document.getElementById('jobFinished_' + name);
          const exitCode = document.getElementById('jobExit_' + name);
          const tailCmd = document.getElementById('jobTailCmd_' + name);
          const tail = document.getElementById('jobTail_' + name);
          const btn = document.getElementById('runBtn_' + name);
          if (badge){
            const status = card.status || 'idle';
            badge.className = 'ops-badge ' + status;
            badge.textContent = status.charAt(0).toUpperCase() + status.slice(1);
          }
          if (started) started.textContent = formatLocalTimestamp(card.started_at || '');
          if (finished) finished.textContent = formatLocalTimestamp(card.finished_at || '');
          if (exitCode) exitCode.textContent = card.last_exit || '—';
          if (tailCmd) tailCmd.textContent = card.tail_command || '';
          if (tail) tail.textContent = card.tail || 'No recent log lines.';
          if (btn) btn.disabled = (card.status === 'running');
        });
        const allBtn = document.getElementById('runBtn_all');
        if (allBtn) {
          allBtn.disabled = ['portal','filer'].some(name => (data[name] || {}).status === 'running');
        }
      } catch (e) {
        console.error('job status failed', e);
      }
    }

    async function runCollector(jobName, forcedEnvironmentId){
      const targetJobs = jobName === 'all' ? ['portal','filer'] : [jobName];
      const environmentId = forcedEnvironmentId || effectiveEnvironmentContext();
      if (environmentId === 'admin') {
        alert('Select a portal environment first.');
        return;
      }
      for (const name of targetJobs) {
        try{
          const resp = await fetch('/run_job/' + name, {
            method:'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ environment_id: environmentId })
          });
          const data = await resp.json();
          if (!resp.ok || !data.ok) {
            throw new Error(data.error || 'Collector launch failed');
          }
        } catch (e) {
          console.error('job launch failed', e);
          alert('Could not start ' + name + ' collector: ' + e.message);
          break;
        }
      }
      refreshJobStatus();
    }

    let thresholdCatalog = { datasets: [], path: '', recipients: [], alert_state: {}, notification_db_path: '', source_label: '' };
    let notificationConfig = { settings: {}, recipients: [], alert_state: {}, db_path: '' };
    let authConfig = { settings: { auth_mode: 'none' }, users: [] };
    let environmentConfig = { items: [], count: 0 };
    let editingRecipientId = null;
    let editingAuthUserId = null;
    let editingEnvironmentId = null;

    function selectedMultiValues(id){
      const el = document.getElementById(id);
      if (!el) return [];
      return Array.from(el.options || []).filter(opt => opt.selected).map(opt => opt.value);
    }

    function setMultiValues(id, values){
      const wanted = new Set((values || []).map(String));
      const el = document.getElementById(id);
      if (!el) return;
      Array.from(el.options || []).forEach(opt => {
        opt.selected = wanted.has(String(opt.value));
      });
    }

    function selectedThresholdDataset(){
      const sel = document.getElementById('thresholdDataset');
      return (sel && sel.value) ? sel.value : '';
    }

    function selectedThresholdField(){
      const sel = document.getElementById('thresholdField');
      return (sel && sel.value) ? sel.value : '';
    }

    function openThresholdEditor(datasetKey, fieldName){
      showTab('thresholds');
      const datasetSel = document.getElementById('thresholdDataset');
      if (datasetSel) datasetSel.value = datasetKey;
      renderThresholdFieldOptions();
      const fieldSel = document.getElementById('thresholdField');
      if (fieldSel && fieldName) fieldSel.value = fieldName;
      renderThresholdEditor();
    }

    function thresholdDatasetEntry(key){
      return (thresholdCatalog.datasets || []).find(ds => ds.key === key) || null;
    }

    function thresholdFieldEntry(datasetKey, fieldName){
      const ds = thresholdDatasetEntry(datasetKey);
      return ds ? (ds.fields || []).find(field => field.name === fieldName) || null : null;
    }

    function hasThresholdRule(rule){
      return Boolean(rule && (rule.warn_op || rule.crit_op));
    }

    function datasetThresholdCount(dataset){
      return ((dataset?.fields || []).filter(field => hasThresholdRule(field.rule))).length;
    }

    function describeThresholdRule(rule){
      if (!hasThresholdRule(rule)) return '—';
      const parts = [];
      if (rule.warn_op) parts.push('Warn: ' + rule.warn_op + ' ' + rule.warn_value);
      if (rule.crit_op) parts.push('Crit: ' + rule.crit_op + ' ' + rule.crit_value);
      return parts.join(' | ');
    }

    function selectThresholdField(fieldName){
      const fieldSel = document.getElementById('thresholdField');
      if (fieldSel) fieldSel.value = fieldName;
      renderThresholdEditor();
    }

    function renderThresholdDatasetOptions(){
      const datasetSel = document.getElementById('thresholdDataset');
      if (!datasetSel) return;
      datasetSel.innerHTML = '';
      const datasets = thresholdCatalog.datasets || [];
      datasets.forEach(ds => {
        const opt = document.createElement('option');
        opt.value = ds.key;
        const ruleCount = datasetThresholdCount(ds);
        opt.textContent = ds.label + ' (' + (ds.row_count || 0) + ' rows' + (ruleCount ? ', ' + ruleCount + ' rules' : '') + ')';
        datasetSel.appendChild(opt);
      });
      const firstWithRules = datasets.find(ds => datasetThresholdCount(ds) > 0);
      if (firstWithRules) {
        datasetSel.value = firstWithRules.key;
      } else if (datasets.length) {
        datasetSel.value = datasets[0].key;
      }
      renderThresholdFieldOptions();
    }

    function renderThresholdFieldOptions(){
      const fieldSel = document.getElementById('thresholdField');
      const dataset = thresholdDatasetEntry(selectedThresholdDataset());
      if (!fieldSel) return;
      fieldSel.innerHTML = '';
      const fields = dataset?.fields || [];
      fields.forEach(field => {
        const opt = document.createElement('option');
        opt.value = field.name;
        opt.textContent = hasThresholdRule(field.rule) ? (field.name + ' *') : field.name;
        fieldSel.appendChild(opt);
      });
      const firstThresholdField = fields.find(field => hasThresholdRule(field.rule));
      if (firstThresholdField) {
        fieldSel.value = firstThresholdField.name;
      } else if (fields.length) {
        fieldSel.value = fields[0].name;
      }
      renderThresholdCurrentListEnhanced();
      renderThresholdEditor();
    }

    function renderThresholdCurrentList(){
      const dataset = thresholdDatasetEntry(selectedThresholdDataset());
      const body = document.getElementById('thresholdCurrentListBody');
      const empty = document.getElementById('thresholdCurrentListEmpty');
      const count = document.getElementById('thresholdCurrentCount');
      const title = document.getElementById('thresholdCurrentDataset');
      if (!body || !empty || !count) return;
      body.innerHTML = '';
      const activeFields = (dataset?.fields || []).filter(field => hasThresholdRule(field.rule));
      count.textContent = String(activeFields.length);
      if (title) title.textContent = dataset ? dataset.label : 'Selected Dataset';
      if (!activeFields.length) {
        empty.style.display = '';
        return;
      }
      empty.style.display = 'none';
      activeFields.forEach(field => {
        const tr = document.createElement('tr');
        tr.onclick = () => {
          const fieldSel = document.getElementById('thresholdField');
          if (fieldSel) fieldSel.value = field.name;
          renderThresholdEditor();
        };
        const fieldTd = document.createElement('td');
        fieldTd.textContent = field.name;
        const warnTd = document.createElement('td');
        warnTd.innerHTML = field.rule?.warn_op ? ('<code>' + field.rule.warn_op + ' ' + field.rule.warn_value + '</code>') : '—';
        const critTd = document.createElement('td');
        critTd.innerHTML = field.rule?.crit_op ? ('<code>' + field.rule.crit_op + ' ' + field.rule.crit_value + '</code>') : '—';
        tr.appendChild(fieldTd);
        tr.appendChild(warnTd);
        tr.appendChild(critTd);
        body.appendChild(tr);
      });
    }

    function renderThresholdCurrentListEnhanced(){
      const dataset = thresholdDatasetEntry(selectedThresholdDataset());
      const body = document.getElementById('thresholdCurrentListBody');
      const empty = document.getElementById('thresholdCurrentListEmpty');
      const count = document.getElementById('thresholdCurrentCount');
      const title = document.getElementById('thresholdCurrentDataset');
      if (!body || !empty || !count) return;
      body.innerHTML = '';
      const activeFields = (dataset?.fields || []).filter(field => hasThresholdRule(field.rule));
      count.textContent = String(activeFields.length);
      if (title) title.textContent = dataset ? dataset.label : 'Selected Dataset';
      if (!activeFields.length) {
        empty.style.display = '';
        return;
      }
      empty.style.display = 'none';
      activeFields.forEach(field => {
        const tr = document.createElement('tr');
        tr.onclick = () => {
          selectThresholdField(field.name);
        };

        const fieldTd = document.createElement('td');
        fieldTd.textContent = field.name;

        const warnTd = document.createElement('td');
        warnTd.innerHTML = field.rule?.warn_op ? ('<code>' + field.rule.warn_op + ' ' + field.rule.warn_value + '</code>') : '—';

        const critTd = document.createElement('td');
        critTd.innerHTML = field.rule?.crit_op ? ('<code>' + field.rule.crit_op + ' ' + field.rule.crit_value + '</code>') : '—';

        const actionsTd = document.createElement('td');
        const actions = document.createElement('div');
        actions.className = 'threshold-row-actions';

        const editBtn = document.createElement('button');
        editBtn.className = 'threshold-row-btn';
        editBtn.textContent = 'Edit';
        editBtn.onclick = (event) => {
          event.stopPropagation();
          selectThresholdField(field.name);
        };

        const deleteBtn = document.createElement('button');
        deleteBtn.className = 'threshold-row-btn delete';
        deleteBtn.textContent = 'Delete';
        deleteBtn.onclick = async (event) => {
          event.stopPropagation();
          await deleteThresholdRule(field.name);
        };

        actions.appendChild(editBtn);
        actions.appendChild(deleteBtn);
        actionsTd.appendChild(actions);

        tr.appendChild(fieldTd);
        tr.appendChild(warnTd);
        tr.appendChild(critTd);
        tr.appendChild(actionsTd);
        body.appendChild(tr);
      });
    }

    function renderAllThresholdList(){
      const body = document.getElementById('allThresholdListBody');
      const empty = document.getElementById('allThresholdListEmpty');
      const count = document.getElementById('allThresholdCount');
      const path = document.getElementById('allThresholdPath');
      if (!body || !empty || !count) return;
      body.innerHTML = '';
      if (path) path.textContent = thresholdCatalog.path || 'thresholds.yaml';
      let total = 0;
      (thresholdCatalog.datasets || []).forEach(dataset => {
        (dataset.fields || []).forEach(field => {
          if (!hasThresholdRule(field.rule)) return;
          total += 1;
          const tr = document.createElement('tr');
          tr.onclick = () => openThresholdEditor(dataset.key, field.name);

          const datasetTd = document.createElement('td');
          datasetTd.textContent = dataset.label;

          const fieldTd = document.createElement('td');
          fieldTd.textContent = field.name;

          const warnTd = document.createElement('td');
          warnTd.innerHTML = field.rule?.warn_op ? ('<code>' + field.rule.warn_op + ' ' + field.rule.warn_value + '</code>') : '—';

          const critTd = document.createElement('td');
          critTd.innerHTML = field.rule?.crit_op ? ('<code>' + field.rule.crit_op + ' ' + field.rule.crit_value + '</code>') : '—';

          const emailTd = document.createElement('td');
          emailTd.innerHTML = field.notify?.enabled
            ? '<span class="pill pill-ok">Email Enabled</span>'
            : '<span class="pill pill-muted">Off</span>';

          const actionsTd = document.createElement('td');
          const actions = document.createElement('div');
          actions.className = 'threshold-row-actions';

          const editBtn = document.createElement('button');
          editBtn.className = 'threshold-row-btn';
          editBtn.textContent = 'Edit';
          editBtn.onclick = (event) => {
            event.stopPropagation();
            openThresholdEditor(dataset.key, field.name);
          };

          const deleteBtn = document.createElement('button');
          deleteBtn.className = 'threshold-row-btn delete';
          deleteBtn.textContent = 'Delete';
          deleteBtn.onclick = async (event) => {
            event.stopPropagation();
            const currentDataset = selectedThresholdDataset();
            await deleteThresholdRule(field.name, dataset.key);
            if (currentDataset && document.getElementById('thresholdDataset')) {
              document.getElementById('thresholdDataset').value = currentDataset;
              renderThresholdFieldOptions();
            }
          };

          actions.appendChild(editBtn);
          actions.appendChild(deleteBtn);
          actionsTd.appendChild(actions);

          tr.appendChild(datasetTd);
          tr.appendChild(fieldTd);
          tr.appendChild(warnTd);
          tr.appendChild(critTd);
          tr.appendChild(emailTd);
          tr.appendChild(actionsTd);
          body.appendChild(tr);
        });
      });
      count.textContent = String(total);
      empty.style.display = total ? 'none' : '';
    }

    function renderThresholdRecipientOptions(notifyConfigForField){
      const select = document.getElementById('thresholdRecipientIds');
      const modeEl = document.getElementById('thresholdRecipientMode');
      const helper = document.getElementById('thresholdRecipientHelp');
      if (!select || !modeEl) return;
      const notify = notifyConfigForField || { recipient_ids: selectedMultiValues('thresholdRecipientIds') };
      select.innerHTML = '';
      (notificationConfig.recipients || []).forEach(recipient => {
        const opt = document.createElement('option');
        opt.value = String(recipient.id);
        opt.textContent = recipient.name + ' <' + recipient.email + '>' + (recipient.enabled ? '' : ' (disabled)');
        select.appendChild(opt);
      });
      setMultiValues('thresholdRecipientIds', notify.recipient_ids || []);
      const selectedMode = modeEl.value || 'all_enabled';
      select.disabled = selectedMode !== 'selected';
      if (helper) {
        const enabledCount = (notificationConfig.recipients || []).filter(recipient => recipient.enabled).length;
        helper.textContent = selectedMode === 'selected'
          ? 'Only the selected recipients will receive this threshold email.'
          : ('This threshold emails all enabled recipients (' + enabledCount + ' configured). Recipient scope still applies, but the threshold itself must also have email enabled.');
      }
    }

    function renderNotificationDatasetOptions(){
      const select = document.getElementById('recipientDatasets');
      if (!select) return;
      const current = selectedMultiValues('recipientDatasets');
      select.innerHTML = '';
      (thresholdCatalog.datasets || []).forEach(dataset => {
        const opt = document.createElement('option');
        opt.value = dataset.key;
        opt.textContent = dataset.label;
        select.appendChild(opt);
      });
      setMultiValues('recipientDatasets', current);
    }

    function clearMultiSelect(id){
      setMultiValues(id, []);
    }

    function setActionStatus(id, message, tone){
      const el = document.getElementById(id);
      if (!el) return;
      el.textContent = message || '';
      el.className = 'action-status';
      if (message) {
        el.classList.add('show');
        if (tone) el.classList.add(tone);
      }
    }

    function setActionButtonsDisabled(containerId, disabled){
      const wrap = document.getElementById(containerId);
      if (!wrap) return;
      wrap.querySelectorAll('button').forEach(btn => {
        btn.disabled = !!disabled;
      });
    }

    function renderNotificationSettings(){
      const settings = notificationConfig.settings || {};
      const state = notificationConfig.alert_state || {};
      const dbPath = document.getElementById('notifyDbPath');
      const active = document.getElementById('notifyActiveAlerts');
      const cleared = document.getElementById('notifyClearedAlerts');
      const total = document.getElementById('notifyTotalAlerts');
      const status = document.getElementById('notifySettingsStatus');
      if (dbPath) dbPath.textContent = notificationConfig.db_path || '';
      if (active) active.textContent = String(state.active || 0);
      if (cleared) cleared.textContent = String(state.cleared || 0);
      if (total) total.textContent = String(state.total || 0);
      const ids = ['smtpHost','smtpPort','smtpUsername','senderName','senderEmail'];
      const keys = ['smtp_host','smtp_port','smtp_username','sender_name','sender_email'];
      ids.forEach((id, index) => {
        const el = document.getElementById(id);
        if (el) el.value = settings[keys[index]] || '';
      });
      const pw = document.getElementById('smtpPassword');
      if (pw) pw.value = '';
      const pwHint = document.getElementById('smtpPasswordHint');
      if (pwHint) pwHint.textContent = settings.smtp_password_set ? 'A password is already saved. Leave blank to keep it.' : 'No password saved yet.';
      const tls = document.getElementById('smtpUseTls');
      const ssl = document.getElementById('smtpUseSsl');
      if (tls) tls.checked = Boolean(settings.use_tls);
      if (ssl) ssl.checked = Boolean(settings.use_ssl);
      if (status && !status.dataset.preserved) status.textContent = 'SQLite alert memory is created automatically on first run. A threshold must have email enabled before any recipient can receive it.';
    }

    function renderNotificationRecipients(){
      const body = document.getElementById('notifyRecipientsBody');
      const empty = document.getElementById('notifyRecipientsEmpty');
      const count = document.getElementById('notifyRecipientCount');
      if (!body || !empty || !count) return;
      body.innerHTML = '';
      const recipients = notificationConfig.recipients || [];
      count.textContent = String(recipients.length);
      if (!recipients.length) {
        empty.style.display = '';
      } else {
        empty.style.display = 'none';
      }
      recipients.forEach(recipient => {
        const tr = document.createElement('tr');
        const coverage = recipient.datasets?.length ? recipient.datasets.join(', ') : 'All datasets';
        const severities = recipient.severities?.length ? recipient.severities.join(', ') : 'All severities';
        tr.innerHTML = '<td>' + recipient.name + '</td>'
          + '<td><code>' + recipient.email + '</code></td>'
          + '<td>' + (recipient.enabled ? '<span class="pill pill-ok">Enabled</span>' : '<span class="pill pill-muted">Disabled</span>') + '</td>'
          + '<td>' + coverage + '</td>'
          + '<td>' + severities + '</td>';
        const actionsTd = document.createElement('td');
        const wrap = document.createElement('div');
        wrap.className = 'threshold-row-actions';
        const editBtn = document.createElement('button');
        editBtn.className = 'threshold-row-btn';
        editBtn.textContent = 'Edit';
        editBtn.onclick = () => editRecipient(recipient.id);
        const deleteBtn = document.createElement('button');
        deleteBtn.className = 'threshold-row-btn delete';
        deleteBtn.textContent = 'Delete';
        deleteBtn.onclick = () => deleteRecipient(recipient.id);
        wrap.appendChild(editBtn);
        wrap.appendChild(deleteBtn);
        actionsTd.appendChild(wrap);
        tr.appendChild(actionsTd);
        body.appendChild(tr);
      });
      renderThresholdRecipientOptions(thresholdFieldEntry(selectedThresholdDataset(), selectedThresholdField())?.notify || {});
    }

    function clearRecipientForm(){
      editingRecipientId = null;
      const formIds = ['recipientName', 'recipientEmail'];
      formIds.forEach(id => {
        const el = document.getElementById(id);
        if (el) el.value = '';
      });
      const enabled = document.getElementById('recipientEnabled');
      if (enabled) enabled.checked = true;
      setMultiValues('recipientDatasets', []);
      setMultiValues('recipientSeverities', []);
      const status = document.getElementById('notifyRecipientsStatus');
      if (status) status.textContent = 'Recipient form cleared.';
      const title = document.getElementById('recipientEditorTitle');
      if (title) title.textContent = 'Add Recipient';
    }

    function editRecipient(id){
      const recipient = (notificationConfig.recipients || []).find(item => Number(item.id) === Number(id));
      if (!recipient) return;
      editingRecipientId = recipient.id;
      document.getElementById('recipientName').value = recipient.name || '';
      document.getElementById('recipientEmail').value = recipient.email || '';
      document.getElementById('recipientEnabled').checked = Boolean(recipient.enabled);
      setMultiValues('recipientDatasets', recipient.datasets || []);
      setMultiValues('recipientSeverities', recipient.severities || []);
      const status = document.getElementById('notifyRecipientsStatus');
      if (status) status.textContent = 'Editing recipient ' + recipient.name + '.';
      const title = document.getElementById('recipientEditorTitle');
      if (title) title.textContent = 'Edit Recipient';
      showTab('notify_recipients');
    }

    async function loadNotificationsConfig(){
      try {
        const resp = await fetch('/notifications_config');
        notificationConfig = await resp.json();
        thresholdCatalog.recipients = notificationConfig.recipients || thresholdCatalog.recipients || [];
        renderNotificationDatasetOptions();
        renderNotificationSettings();
        renderNotificationRecipients();
      } catch (e) {
        const status = document.getElementById('notifySettingsStatus');
        if (status) status.textContent = 'Could not load notification settings.';
        console.error('notification config failed', e);
      }
    }

    async function saveNotificationSettings(){
      const status = document.getElementById('notifySettingsStatus');
      setActionButtonsDisabled('notifySettingsActions', true);
      setActionStatus('notifySettingsFlash', 'Saving email settings...', 'working');
      const payload = {
        smtp_host: document.getElementById('smtpHost').value,
        smtp_port: document.getElementById('smtpPort').value,
        smtp_username: document.getElementById('smtpUsername').value,
        smtp_password: document.getElementById('smtpPassword').value,
        sender_name: document.getElementById('senderName').value,
        sender_email: document.getElementById('senderEmail').value,
        use_tls: document.getElementById('smtpUseTls').checked,
        use_ssl: document.getElementById('smtpUseSsl').checked,
      };
      try {
        const resp = await fetch('/notifications_settings_save', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(payload)
        });
        const data = await resp.json();
        if (!resp.ok || !data.ok) throw new Error(data.error || 'Save failed');
        notificationConfig.settings = data.settings || {};
        notificationConfig.db_path = data.db_path || notificationConfig.db_path;
        if (status) {
          status.dataset.preserved = '1';
          status.textContent = 'Email settings saved.';
        }
        renderNotificationSettings();
        setActionStatus('notifySettingsFlash', 'Email settings saved.', 'success');
      } catch (e) {
        if (status) {
          status.dataset.preserved = '1';
          status.textContent = 'Save failed: ' + e.message;
        }
        setActionStatus('notifySettingsFlash', 'Save failed: ' + e.message, 'error');
      } finally {
        setActionButtonsDisabled('notifySettingsActions', false);
      }
    }

    function notificationSettingsDirty(){
      const saved = notificationConfig.settings || {};
      return String(document.getElementById('smtpHost').value || '') !== String(saved.smtp_host || '')
        || String(document.getElementById('smtpPort').value || '') !== String(saved.smtp_port || '587')
        || String(document.getElementById('smtpUsername').value || '') !== String(saved.smtp_username || '')
        || String(document.getElementById('senderName').value || '') !== String(saved.sender_name || '')
        || String(document.getElementById('senderEmail').value || '') !== String(saved.sender_email || '')
        || Boolean(document.getElementById('smtpUseTls').checked) !== Boolean(saved.use_tls)
        || Boolean(document.getElementById('smtpUseSsl').checked) !== Boolean(saved.use_ssl)
        || String(document.getElementById('smtpPassword').value || '').trim() !== '';
    }

    async function sendNotificationTestEmail(){
      const status = document.getElementById('notifySettingsStatus');
      const target = document.getElementById('testEmailTarget').value;
      const saved = notificationConfig.settings || {};
      if (notificationSettingsDirty()) {
        const message = 'Save Email Settings first, then click Send Test Email.';
        if (status) {
          status.dataset.preserved = '1';
          status.textContent = message;
        }
        setActionStatus('notifySettingsFlash', message, 'error');
        return;
      }
      if (!String(saved.smtp_host || '').trim() || !String(saved.smtp_port || '').trim() || !String(saved.sender_email || '').trim()) {
        const message = 'Save Email Settings first. SMTP host, port, and sender email must be saved before sending a test email.';
        if (status) {
          status.dataset.preserved = '1';
          status.textContent = message;
        }
        setActionStatus('notifySettingsFlash', message, 'error');
        return;
      }
      setActionButtonsDisabled('notifySettingsActions', true);
      setActionStatus('notifySettingsFlash', 'Sending test email...', 'working');
      try {
        const resp = await fetch('/notifications_test_email', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ email: target })
        });
        const data = await resp.json();
        if (!resp.ok || !data.ok) throw new Error(data.error || 'Test failed');
        if (status) {
          status.dataset.preserved = '1';
          status.textContent = 'Test email sent to ' + target + '.';
        }
        setActionStatus('notifySettingsFlash', 'Test email sent to ' + target + '.', 'success');
      } catch (e) {
        if (status) {
          status.dataset.preserved = '1';
          status.textContent = 'Test failed: ' + e.message;
        }
        setActionStatus('notifySettingsFlash', 'Test failed: ' + e.message, 'error');
      } finally {
        setActionButtonsDisabled('notifySettingsActions', false);
      }
    }

    async function runNotificationCheck(){
      const status = document.getElementById('notifySettingsStatus');
      setActionButtonsDisabled('notifySettingsActions', true);
      setActionStatus('notifySettingsFlash', 'Evaluating threshold alerts now...', 'working');
      try {
        const resp = await fetch(apiUrl('/notifications_run'), { method: 'POST' });
        const data = await resp.json();
        if (!resp.ok || !data.ok) throw new Error(data.error || 'Alert evaluation failed');
        await loadNotificationsConfig();
        await loadThresholdCatalog();
        const sentCount = (data.sent || []).length;
        const evalCount = (data.evaluated || []).length;
        const checkedCount = (data.checked || []).length;
        const message = 'Alert check completed. Checked ' + checkedCount + ' notification rule(s); matched ' + evalCount + '; sent ' + sentCount + ' email batch(es).';
        if (status) {
          status.dataset.preserved = '1';
          status.textContent = message;
        }
        setActionStatus('notifySettingsFlash', message, 'success');
      } catch (e) {
        if (status) {
          status.dataset.preserved = '1';
          status.textContent = 'Alert check failed: ' + e.message;
        }
        setActionStatus('notifySettingsFlash', 'Alert check failed: ' + e.message, 'error');
      } finally {
        setActionButtonsDisabled('notifySettingsActions', false);
      }
    }

    function renderAuthUsers(){
      const body = document.getElementById('authUsersBody');
      const empty = document.getElementById('authUsersEmpty');
      const count = document.getElementById('authUserCount');
      if (!body || !empty || !count) return;
      body.innerHTML = '';
      const users = authConfig.users || [];
      count.textContent = String(users.length);
      empty.style.display = users.length ? 'none' : '';
      users.forEach(user => {
        const tr = document.createElement('tr');
        tr.innerHTML = '<td><strong>' + user.username + '</strong></td>'
          + '<td>' + (user.display_name || '') + '</td>'
          + '<td>' + (user.enabled ? '<span class="pill pill-ok">Enabled</span>' : '<span class="pill pill-muted">Disabled</span>') + '</td>';
        const actionsTd = document.createElement('td');
        const wrap = document.createElement('div');
        wrap.className = 'threshold-row-actions';
        const editBtn = document.createElement('button');
        editBtn.className = 'threshold-row-btn';
        editBtn.textContent = 'Edit';
        editBtn.onclick = () => editAuthUser(user.id);
        const deleteBtn = document.createElement('button');
        deleteBtn.className = 'threshold-row-btn delete';
        deleteBtn.textContent = 'Delete';
        deleteBtn.onclick = () => deleteAuthUser(user.id);
        wrap.appendChild(editBtn);
        wrap.appendChild(deleteBtn);
        actionsTd.appendChild(wrap);
        tr.appendChild(actionsTd);
        body.appendChild(tr);
      });
    }

    function clearAuthUserForm(){
      editingAuthUserId = null;
      const username = document.getElementById('authUsername');
      const display = document.getElementById('authDisplayName');
      const password = document.getElementById('authPassword');
      const confirm = document.getElementById('authPasswordConfirm');
      const enabled = document.getElementById('authUserEnabled');
      const title = document.getElementById('authUserEditorTitle');
      if (username) username.value = '';
      if (display) display.value = '';
      if (password) password.value = '';
      if (confirm) confirm.value = '';
      if (enabled) enabled.checked = true;
      if (title) title.textContent = 'Add Local User';
    }

    function editAuthUser(id){
      const user = (authConfig.users || []).find(item => Number(item.id) === Number(id));
      if (!user) return;
      editingAuthUserId = user.id;
      document.getElementById('authUsername').value = user.username || '';
      document.getElementById('authDisplayName').value = user.display_name || '';
      document.getElementById('authPassword').value = '';
      document.getElementById('authPasswordConfirm').value = '';
      document.getElementById('authUserEnabled').checked = Boolean(user.enabled);
      const title = document.getElementById('authUserEditorTitle');
      if (title) title.textContent = 'Edit Local User';
      showTab('auth_settings');
    }

    async function loadAuthConfig(){
      try {
        const resp = await fetch('/auth_config');
        authConfig = await resp.json();
        document.getElementById('authMode').value = authConfig.settings?.auth_mode || 'none';
        renderAuthUsers();
      } catch (e) {
        const status = document.getElementById('authSettingsStatus');
        if (status) status.textContent = 'Could not load access control settings.';
      }
    }

    async function saveAuthSettings(){
      const status = document.getElementById('authSettingsStatus');
      const selectedMode = document.getElementById('authMode').value;
      const previousMode = authConfig.settings?.auth_mode || 'none';
      setActionButtonsDisabled('authSettingsActions', true);
      setActionStatus('authSettingsFlash', 'Saving access control settings...', 'working');
      try {
        const resp = await fetch('/auth_settings_save', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ auth_mode: document.getElementById('authMode').value })
        });
        const data = await resp.json();
        if (!resp.ok || !data.ok) throw new Error(data.error || 'Save failed');
        authConfig.settings = data.settings || authConfig.settings;
        const msg = authConfig.settings.auth_mode === 'none'
          ? 'Access control saved. No login is required.'
          : 'Access control saved. Local login mode is configured here.';
        if (status) status.textContent = msg;
        setActionStatus('authSettingsFlash', msg, 'success');
        if (selectedMode === 'local' && previousMode !== 'local') {
          const redirectMsg = 'Access control saved. Redirecting to the login page...';
          if (status) status.textContent = redirectMsg;
          setActionStatus('authSettingsFlash', redirectMsg, 'success');
          setTimeout(() => { window.location.href = '/logout'; }, 600);
          return;
        }
      } catch (e) {
        if (status) status.textContent = 'Save failed: ' + e.message;
        setActionStatus('authSettingsFlash', 'Save failed: ' + e.message, 'error');
      } finally {
        setActionButtonsDisabled('authSettingsActions', false);
      }
    }

    async function saveAuthUser(){
      const status = document.getElementById('authSettingsStatus');
      const passwordValue = document.getElementById('authPassword').value;
      const confirmValue = document.getElementById('authPasswordConfirm').value;
      if (passwordValue !== confirmValue) {
        const msg = 'Passwords do not match.';
        if (status) status.textContent = msg;
        setActionStatus('authSettingsFlash', msg, 'error');
        return;
      }
      setActionButtonsDisabled('authUserActions', true);
      setActionStatus('authSettingsFlash', 'Saving local user...', 'working');
      try {
        const resp = await fetch('/auth_users_save', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            id: editingAuthUserId,
            username: document.getElementById('authUsername').value,
            display_name: document.getElementById('authDisplayName').value,
            password: passwordValue,
            confirm_password: confirmValue,
            enabled: document.getElementById('authUserEnabled').checked,
          })
        });
        const data = await resp.json();
        if (!resp.ok || !data.ok) throw new Error(data.error || 'Save failed');
        authConfig.users = data.users || [];
        renderAuthUsers();
        clearAuthUserForm();
        if (status) status.textContent = 'Local user saved.';
        setActionStatus('authSettingsFlash', 'Local user saved.', 'success');
      } catch (e) {
        if (status) status.textContent = 'Save failed: ' + e.message;
        setActionStatus('authSettingsFlash', 'Save failed: ' + e.message, 'error');
      } finally {
        setActionButtonsDisabled('authUserActions', false);
      }
    }

    async function deleteAuthUser(id){
      const status = document.getElementById('authSettingsStatus');
      setActionStatus('authSettingsFlash', 'Deleting local user...', 'working');
      try {
        const resp = await fetch('/auth_users_delete', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ id: id })
        });
        const data = await resp.json();
        if (!resp.ok || !data.ok) throw new Error(data.error || 'Delete failed');
        authConfig.users = data.users || [];
        renderAuthUsers();
        if (Number(editingAuthUserId) === Number(id)) clearAuthUserForm();
        if (status) status.textContent = 'Local user deleted.';
        setActionStatus('authSettingsFlash', 'Local user deleted.', 'success');
      } catch (e) {
        if (status) status.textContent = 'Delete failed: ' + e.message;
        setActionStatus('authSettingsFlash', 'Delete failed: ' + e.message, 'error');
      }
    }

    async function saveRecipient(){
      const status = document.getElementById('notifyRecipientsStatus');
      setActionButtonsDisabled('notifyRecipientsActions', true);
      setActionStatus('notifyRecipientsFlash', 'Saving recipient...', 'working');
      const payload = {
        id: editingRecipientId,
        name: document.getElementById('recipientName').value,
        email: document.getElementById('recipientEmail').value,
        enabled: document.getElementById('recipientEnabled').checked,
        datasets: selectedMultiValues('recipientDatasets'),
        severities: selectedMultiValues('recipientSeverities'),
      };
      try {
        const resp = await fetch('/notifications_recipients_save', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(payload)
        });
        const data = await resp.json();
        if (!resp.ok || !data.ok) throw new Error(data.error || 'Save failed');
        notificationConfig.recipients = data.recipients || [];
        thresholdCatalog.recipients = notificationConfig.recipients;
        renderNotificationRecipients();
        clearRecipientForm();
        if (status) status.textContent = 'Recipient saved.';
        setActionStatus('notifyRecipientsFlash', 'Recipient saved.', 'success');
      } catch (e) {
        if (status) status.textContent = 'Save failed: ' + e.message;
        setActionStatus('notifyRecipientsFlash', 'Save failed: ' + e.message, 'error');
      } finally {
        setActionButtonsDisabled('notifyRecipientsActions', false);
      }
    }

    async function deleteRecipient(id){
      const status = document.getElementById('notifyRecipientsStatus');
      setActionButtonsDisabled('notifyRecipientsActions', true);
      setActionStatus('notifyRecipientsFlash', 'Deleting recipient...', 'working');
      try {
        const resp = await fetch('/notifications_recipients_delete', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ id: id })
        });
        const data = await resp.json();
        if (!resp.ok || !data.ok) throw new Error(data.error || 'Delete failed');
        notificationConfig.recipients = data.recipients || [];
        thresholdCatalog.recipients = notificationConfig.recipients;
        renderNotificationRecipients();
        if (Number(editingRecipientId) === Number(id)) clearRecipientForm();
        if (status) status.textContent = 'Recipient deleted.';
        setActionStatus('notifyRecipientsFlash', 'Recipient deleted.', 'success');
      } catch (e) {
        if (status) status.textContent = 'Delete failed: ' + e.message;
        setActionStatus('notifyRecipientsFlash', 'Delete failed: ' + e.message, 'error');
      } finally {
        setActionButtonsDisabled('notifyRecipientsActions', false);
      }
    }

    function selectedEnvironmentRecord(){
      const current = effectiveEnvironmentContext();
      return (environmentConfig.items || []).find(item => String(item.id) === String(current)) || null;
    }

    function renderEnvironmentSelector(){
      const select = document.getElementById('environmentContextSelect');
      const label = document.getElementById('environmentContextLabel');
      if (!select) return;
      const current = effectiveEnvironmentContext();
      select.innerHTML = '';
      const adminOpt = document.createElement('option');
      adminOpt.value = 'admin';
      adminOpt.textContent = 'Administration';
      select.appendChild(adminOpt);
      (environmentConfig.items || []).forEach(item => {
        const opt = document.createElement('option');
        opt.value = String(item.id);
        opt.textContent = item.enabled ? item.name : (item.name + ' (disabled)');
        select.appendChild(opt);
      });
      const hasCurrent = Array.from(select.options).some(opt => opt.value === current);
      const nextValue = hasCurrent ? current : 'admin';
      if (!hasCurrent || current !== nextValue) saveEnvironmentContext(nextValue);
      select.value = nextValue;
      const selected = (environmentConfig.items || []).find(item => String(item.id) === String(nextValue));
      if (label) label.textContent = selected ? selected.name : 'Administration';
      renderContextVisibility();
    }

    function reconcileContextAndActiveTab(){
      const currentTab = loadActive();
      const inAdmin = isAdministrationContext();
      if (inAdmin) {
        if ((NAV_SECTION_MAP[currentTab] || '').startsWith('admin_')) return;
        showTab('admin_env');
      } else {
        if ((NAV_SECTION_MAP[currentTab] || '').startsWith('admin_')) {
          showTab('overview');
        }
      }
    }

    function handleEnvironmentContextChange(){
      const select = document.getElementById('environmentContextSelect');
      const nextValue = select ? (select.value || 'admin') : 'admin';
      navigateToEnvironment(nextValue);
    }

    function renderContextVisibility(){
      const inAdmin = isAdministrationContext();
      const sidebarLabel = document.getElementById('sidebarSectionLabel');
      if (sidebarLabel) sidebarLabel.textContent = inAdmin ? 'Administration' : 'Monitoring';
      document.body.setAttribute('data-initial-context', inAdmin ? 'admin' : 'env');
      document.querySelectorAll('.nav-section[data-context]').forEach(section => {
        const contextType = section.getAttribute('data-context');
        const hidden = inAdmin ? contextType === 'monitoring' : contextType === 'administration';
        section.classList.toggle('context-hidden', hidden);
      });
    }

    function clearEnvironmentForm(){
      editingEnvironmentId = null;
      const values = {
        environmentName: '',
        envPortalFqdn: '',
        envCteraUsername: '',
        envCteraPassword: '',
        envUseJumpHost: false,
        envMainDbViaJumpConfigured: false,
        envJumpHost: '',
        envMainDbJumpUsername: '',
        envJumpSshMode: 'root_password',
        envJumpSshUsername: 'root',
        envMainDbIp: '',
        envInitialSshMode: 'root_password',
        envOpenAiKey: '',
        envPortalSchedule: '60',
        envFilerSchedule: '60',
      };
      Object.entries(values).forEach(([id, value]) => {
        const el = document.getElementById(id);
        if (!el) return;
        if (el.type === 'checkbox') {
          el.checked = Boolean(value);
        } else {
          el.value = value;
        }
      });
      const enabled = document.getElementById('envEnabled');
      if (enabled) enabled.checked = true;
      const sshPassword = document.getElementById('envInitialSshPassword');
      if (sshPassword) sshPassword.value = '';
      const sshKey = document.getElementById('envInitialSshKey');
      if (sshKey) sshKey.value = '';
      const jumpPassword = document.getElementById('envJumpSshPassword');
      if (jumpPassword) jumpPassword.value = '';
      const jumpKey = document.getElementById('envJumpSshKey');
      if (jumpKey) jumpKey.value = '';
      const title = document.getElementById('environmentEditorTitle');
      if (title) title.textContent = 'New Portal Environment';
      const crumb = document.getElementById('environmentEditorCrumb');
      if (crumb) crumb.innerHTML = 'Portals <span>› New Portal Environment</span>';
      const status = document.getElementById('environmentStatus');
      if (status) status.textContent = 'Portal environment form cleared.';
      const hints = {
        envCteraPasswordHint: 'Leave blank if you are not setting the secret yet.',
        envOpenAiKeyHint: 'Optional. Only needed if this environment uses AI Summary.',
        envInitialSshHelp: 'Used one time for bootstrap. After that the dashboard uses the installed SSH key going forward.',
        envInitialSshPasswordHint: 'Enter the bootstrap SSH password only if this mode uses username and password.',
        envInitialSshKeyHint: 'Upload the initial private key only if this mode uses private key bootstrap.',
        envJumpSshHelp: 'Optional. Use a jump host if this monitoring server cannot reach MainDB directly.',
        envMainDbViaJumpConfiguredHint: 'Use this when the jump host can already SSH into MainDB using its own existing trust or SSH setup.',
        envMainDbJumpUsernameHint: 'This is the SSH user the jump host will use when it connects onward to MainDB.',
        envJumpSshPasswordHint: 'Enter the jump-host SSH password only if this mode uses username and password.',
        envJumpSshKeyHint: 'Upload the jump-host private key only if this mode uses private key bootstrap.',
      };
      Object.entries(hints).forEach(([id, text]) => {
        const el = document.getElementById(id);
        if (el) el.textContent = text;
      });
      renderInitialSshFields();
      renderJumpSshFields();
    }

    function currentEditingEnvironment(){
      return (environmentConfig.items || []).find(item => Number(item.id) === Number(editingEnvironmentId)) || null;
    }

    function openEnvironmentModal(mode){
      const modal = document.getElementById('environmentModalBackdrop');
      if (!modal) return;
      if (mode === 'new') clearEnvironmentForm();
      modal.classList.add('open');
    }

    function closeEnvironmentModal(){
      const modal = document.getElementById('environmentModalBackdrop');
      if (!modal) return;
      modal.classList.remove('open');
    }

    function editEnvironment(id){
      const env = (environmentConfig.items || []).find(item => Number(item.id) === Number(id));
      if (!env) return;
      editingEnvironmentId = env.id;
      const values = {
        environmentName: env.name || '',
        envPortalFqdn: env.portal_fqdn || '',
        envCteraUsername: env.ctera_username || '',
        envCteraPassword: '',
        envUseJumpHost: Boolean(env.jump_host_enabled),
        envMainDbViaJumpConfigured: Boolean(env.main_db_via_jump_preconfigured),
        envJumpHost: env.jump_host || '',
        envMainDbJumpUsername: env.main_db_jump_username || env.ssh_username || env.jump_ssh_username || 'root',
        envJumpSshMode: env.jump_ssh_mode || 'root_password',
        envJumpSshUsername: env.jump_ssh_username || 'root',
        envMainDbIp: env.main_db_ip || '',
        envInitialSshMode: env.ssh_mode || 'root_password',
        envOpenAiKey: '',
        envPortalSchedule: String(env.portal_schedule_minutes || 60),
        envFilerSchedule: String(env.filer_schedule_minutes || 60),
      };
      Object.entries(values).forEach(([id, value]) => {
        const el = document.getElementById(id);
        if (!el) return;
        if (el.type === 'checkbox') {
          el.checked = Boolean(value);
        } else {
          el.value = value;
        }
      });
      const enabled = document.getElementById('envEnabled');
      if (enabled) enabled.checked = Boolean(env.enabled);
      const sshPassword = document.getElementById('envInitialSshPassword');
      if (sshPassword) sshPassword.value = '';
      const sshKey = document.getElementById('envInitialSshKey');
      if (sshKey) sshKey.value = '';
      const jumpKey = document.getElementById('envJumpSshKey');
      if (jumpKey) jumpKey.value = '';
      const jumpPass = document.getElementById('envJumpSshPassword');
      if (jumpPass) jumpPass.value = '';
      const title = document.getElementById('environmentEditorTitle');
      if (title) title.textContent = 'Edit Portal Environment';
      const crumb = document.getElementById('environmentEditorCrumb');
      if (crumb) crumb.innerHTML = 'Portals <span>› Edit Portal Environment</span>';
      const status = document.getElementById('environmentStatus');
      if (status) status.textContent = 'Editing environment ' + env.name + '.';
      const hints = {
        envCteraPasswordHint: env.ctera_password_set ? 'A CTERA password is already saved. Leave blank to keep it.' : 'No CTERA password saved yet.',
        envOpenAiKeyHint: env.openai_key_set ? 'An OpenAI key is already saved. Leave blank to keep it.' : 'Optional. Only needed if this environment uses AI Summary.',
        envInitialSshHelp: 'Used one time for bootstrap. After that the dashboard uses the installed SSH key going forward.',
        envInitialSshPasswordHint: env.ssh_password_set ? 'A bootstrap SSH password is already saved. Leave blank to keep it.' : 'Enter the bootstrap SSH password only if this mode uses username and password.',
        envInitialSshKeyHint: env.ssh_key_path ? ('Saved key path: ' + env.ssh_key_path) : 'Upload the initial private key only if this mode uses private key bootstrap.',
        envJumpSshHelp: 'Optional. Use a jump host if this monitoring server cannot reach MainDB directly.',
        envMainDbViaJumpConfiguredHint: 'Use this when the jump host can already SSH into MainDB using its own existing trust or SSH setup.',
        envMainDbJumpUsernameHint: 'This is the SSH user the jump host will use when it connects onward to MainDB.',
        envJumpSshPasswordHint: env.jump_ssh_password_set ? 'A jump-host SSH password is already saved. Leave blank to keep it.' : 'Enter the jump-host SSH password only if this mode uses username and password.',
        envJumpSshKeyHint: env.jump_ssh_key_path ? ('Saved jump-host key path: ' + env.jump_ssh_key_path) : 'Upload the jump-host private key only if this mode uses private key bootstrap.',
      };
      Object.entries(hints).forEach(([id, text]) => {
        const el = document.getElementById(id);
        if (el) el.textContent = text;
      });
      renderInitialSshFields();
      renderJumpSshFields();
      showTab('admin_env');
      openEnvironmentModal();
    }

    function renderInitialSshFields(){
      const viaJumpConfigured = Boolean(document.getElementById('envMainDbViaJumpConfigured')?.checked);
      const section = document.getElementById('envInitialSshSection');
      const jumpUserWrap = document.getElementById('envMainDbJumpUsernameWrap');
      const mode = (document.getElementById('envInitialSshMode')?.value || 'root_password');
      const passwordWrap = document.getElementById('envInitialSshPasswordWrap');
      const keyWrap = document.getElementById('envInitialSshKeyWrap');
      const usernameInput = document.getElementById('envInitialSshUsername');
      const needsPassword = mode === 'root_password' || mode === 'user_password_sudo';
      const needsKey = mode === 'root_key' || mode === 'user_key_sudo';
      const rootMode = mode === 'root_password' || mode === 'root_key';
      if (section) section.style.display = viaJumpConfigured ? 'none' : '';
      if (jumpUserWrap) jumpUserWrap.style.display = viaJumpConfigured ? '' : 'none';
      if (passwordWrap) passwordWrap.style.display = needsPassword ? '' : 'none';
      if (keyWrap) keyWrap.style.display = needsKey ? '' : 'none';
      if (usernameInput) {
        usernameInput.value = rootMode ? 'root' : (usernameInput.value || '');
        usernameInput.readOnly = rootMode;
      }
    }

    function renderJumpSshFields(){
      const enabled = Boolean(document.getElementById('envUseJumpHost')?.checked);
      const viaJumpWrap = document.getElementById('envMainDbViaJumpConfiguredWrap');
      const section = document.getElementById('envJumpHostSection');
      const passwordWrap = document.getElementById('envJumpSshPasswordWrap');
      const keyWrap = document.getElementById('envJumpSshKeyWrap');
      const mode = (document.getElementById('envJumpSshMode')?.value || 'root_password');
      const usernameInput = document.getElementById('envJumpSshUsername');
      const needsPassword = mode === 'root_password' || mode === 'user_password';
      const needsKey = mode === 'root_key' || mode === 'user_key';
      const rootMode = mode === 'root_password' || mode === 'root_key';
      if (section) section.style.display = enabled ? '' : 'none';
      if (viaJumpWrap) viaJumpWrap.style.display = enabled ? '' : 'none';
      if (passwordWrap) passwordWrap.style.display = enabled && needsPassword ? '' : 'none';
      if (keyWrap) keyWrap.style.display = enabled && needsKey ? '' : 'none';
      if (usernameInput) {
        usernameInput.value = rootMode ? 'root' : (usernameInput.value || '');
        usernameInput.readOnly = rootMode;
      }
      if (!enabled) {
        const configured = document.getElementById('envMainDbViaJumpConfigured');
        if (configured) configured.checked = false;
      }
      renderInitialSshFields();
    }

    function renderEnvironmentList(){
      const body = document.getElementById('environmentListBody');
      const empty = document.getElementById('environmentListEmpty');
      const count = document.getElementById('environmentCount');
      if (!body || !empty || !count) return;
      body.innerHTML = '';
      const items = environmentConfig.items || [];
      count.textContent = String(items.length);
      empty.style.display = items.length ? 'none' : '';
      items.forEach(env => {
        const tr = document.createElement('tr');
        tr.innerHTML = '<td><strong>' + env.name + '</strong></td>'
          + '<td>' + (env.portal_fqdn || env.portal_ip || '-') + '</td>'
          + '<td>' + (env.main_db_ip || env.pg_host || '-') + '</td>'
          + '<td>' + (env.enabled ? '<span class="pill pill-ok">Enabled</span>' : '<span class="pill pill-muted">Disabled</span>') + '</td>'
          + '<td>' + formatLocalTimestamp(env.updated_at || env.created_at || '') + '</td>';
        const actionsTd = document.createElement('td');
        const wrap = document.createElement('div');
        wrap.className = 'threshold-row-actions';
        const useBtn = document.createElement('button');
        useBtn.className = 'threshold-row-btn';
        useBtn.textContent = 'Select';
        useBtn.onclick = () => {
          navigateToEnvironment(String(env.id));
        };
        const editBtn = document.createElement('button');
        editBtn.className = 'threshold-row-btn';
        editBtn.textContent = 'Edit';
        editBtn.onclick = () => editEnvironment(env.id);
        const deleteBtn = document.createElement('button');
        deleteBtn.className = 'threshold-row-btn delete';
        deleteBtn.textContent = 'Delete';
        deleteBtn.onclick = () => deleteEnvironment(env.id);
        wrap.appendChild(useBtn);
        wrap.appendChild(editBtn);
        wrap.appendChild(deleteBtn);
        actionsTd.appendChild(wrap);
        tr.appendChild(actionsTd);
        body.appendChild(tr);
      });
    }

    async function loadEnvironmentConfig(){
      const status = document.getElementById('environmentStatus');
      try {
        const resp = await fetch('/environments_config');
        environmentConfig = await resp.json();
        renderEnvironmentSelector();
        renderEnvironmentList();
        reconcileContextAndActiveTab();
      } catch (e) {
        renderEnvironmentSelector();
        reconcileContextAndActiveTab();
        if (status) status.textContent = 'Could not load portal environments.';
        console.error('environment config failed', e);
      }
    }

    async function collectEnvironmentPayload(){
      let uploadedKeyContent = '';
      let uploadedKeyName = '';
      const uploadedKey = document.getElementById('envInitialSshKey');
      if (uploadedKey && uploadedKey.files && uploadedKey.files[0]) {
        uploadedKeyName = uploadedKey.files[0].name || '';
        uploadedKeyContent = await uploadedKey.files[0].text();
      }
      let uploadedJumpKeyContent = '';
      let uploadedJumpKeyName = '';
      const uploadedJumpKey = document.getElementById('envJumpSshKey');
      if (uploadedJumpKey && uploadedJumpKey.files && uploadedJumpKey.files[0]) {
        uploadedJumpKeyName = uploadedJumpKey.files[0].name || '';
        uploadedJumpKeyContent = await uploadedJumpKey.files[0].text();
      }
      return {
        id: editingEnvironmentId,
        environment_name: document.getElementById('environmentName').value,
        portal_fqdn: document.getElementById('envPortalFqdn').value,
        portal_ip: '',
        ctera_username: document.getElementById('envCteraUsername').value,
        ctera_password: document.getElementById('envCteraPassword').value,
        jump_host_enabled: document.getElementById('envUseJumpHost').checked,
        main_db_via_jump_preconfigured: document.getElementById('envMainDbViaJumpConfigured').checked,
        jump_host: document.getElementById('envJumpHost').value,
        main_db_jump_username: document.getElementById('envMainDbJumpUsername').value || '',
        jump_ssh_mode: document.getElementById('envJumpSshMode').value,
        jump_ssh_username: document.getElementById('envJumpSshUsername').value || 'root',
        jump_ssh_password: document.getElementById('envJumpSshPassword').value || '',
        jump_ssh_private_key_name: uploadedJumpKeyName,
        jump_ssh_private_key_content: uploadedJumpKeyContent,
        main_db_ip: document.getElementById('envMainDbIp').value,
        pg_host: document.getElementById('envMainDbIp').value,
        pg_port: '5432',
        pg_database: 'postgres',
        pg_user: 'postgres',
        pg_password: '',
        openai_key: document.getElementById('envOpenAiKey').value,
        portal_schedule_minutes: document.getElementById('envPortalSchedule').value,
        filer_schedule_minutes: document.getElementById('envFilerSchedule').value,
        enabled: document.getElementById('envEnabled').checked,
        ssh_mode: document.getElementById('envInitialSshMode').value,
        ssh_username: document.getElementById('envInitialSshUsername').value || 'root',
        ssh_key_path: '',
        ssh_password: document.getElementById('envInitialSshPassword').value || '',
        ssh_private_key_name: uploadedKeyName,
        ssh_private_key_content: uploadedKeyContent,
        sudo_required: document.getElementById('envInitialSshMode').value === 'user_password_sudo' || document.getElementById('envInitialSshMode').value === 'user_key_sudo',
      };
    }

    function validateEnvironmentPayload(payload){
      const current = currentEditingEnvironment();
      const useJumpHost = Boolean(payload.jump_host_enabled);
      const mainDbViaJumpConfigured = Boolean(payload.main_db_via_jump_preconfigured);
      const jumpMode = String(payload.jump_ssh_mode || 'root_password');
      const jumpNeedsPassword = jumpMode === 'root_password' || jumpMode === 'user_password';
      const jumpNeedsKey = jumpMode === 'root_key' || jumpMode === 'user_key';
      const mode = String(payload.ssh_mode || 'root_password');
      const needsPasswordBootstrap = mode === 'root_password' || mode === 'user_password_sudo';
      const needsKeyBootstrap = mode === 'root_key' || mode === 'user_key_sudo';
      const hasSavedCteraPassword = Boolean(current && current.ctera_password_set);
      const hasSavedJumpPassword = Boolean(current && current.jump_ssh_password_set);
      const hasSavedJumpKey = Boolean(current && current.jump_ssh_key_path);
      const hasSavedSshPassword = Boolean(current && current.ssh_password_set);
      const hasSavedSshKey = Boolean(current && current.ssh_key_path);

      if (!String(payload.environment_name || '').trim()) return 'Environment name is required.';
      if (!String(payload.portal_fqdn || '').trim()) return 'Portal FQDN is required.';
      if (!String(payload.ctera_username || '').trim()) return 'CTERA read-only username is required.';
      if (!String(payload.main_db_ip || '').trim()) return 'MainDB IP is required.';
      if (!String(payload.ctera_password || '').trim() && !hasSavedCteraPassword) return 'CTERA password is required.';
      if (useJumpHost && !String(payload.jump_host || '').trim()) return 'Jump host is required when jump-host access is enabled.';
      if (useJumpHost && !String(payload.jump_ssh_username || '').trim()) return 'Jump-host SSH username is required.';
      if (useJumpHost && jumpNeedsPassword && !String(payload.jump_ssh_password || '').trim() && !hasSavedJumpPassword) return 'Jump-host SSH password is required for this jump-host access mode.';
      if (useJumpHost && jumpNeedsKey && !String(payload.jump_ssh_private_key_content || '').trim() && !hasSavedJumpKey) return 'Upload a jump-host private key for this jump-host access mode.';
      if (mainDbViaJumpConfigured && !String(payload.main_db_jump_username || payload.jump_ssh_username || '').trim()) return 'MainDB SSH username from jump host is required.';
      if (mainDbViaJumpConfigured) return '';
      if (!String(payload.ssh_username || '').trim()) return 'Initial SSH username is required.';
      if (needsPasswordBootstrap && !String(payload.ssh_password || '').trim() && !hasSavedSshPassword) return 'Initial SSH password is required for this SSH access mode.';
      if (needsKeyBootstrap && !String(payload.ssh_private_key_content || '').trim() && !hasSavedSshKey) return 'Upload an initial SSH private key for this SSH access mode.';
      return '';
    }

    async function saveEnvironment(runBootstrap){
      const status = document.getElementById('environmentStatus');
      const payload = await collectEnvironmentPayload();
      const validationError = validateEnvironmentPayload(payload);
      if (validationError) {
        if (status) status.textContent = validationError;
        setActionStatus('environmentFlash', validationError, 'error');
        return;
      }
      setActionButtonsDisabled('environmentActions', true);
      setActionStatus('environmentFlash', runBootstrap ? 'Saving portal environment and running bootstrap...' : 'Saving portal environment...', 'working');
      try {
        const resp = await fetch(runBootstrap ? '/environments_bootstrap' : '/environments_save', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(payload)
        });
        const data = await resp.json();
        if (!resp.ok || !data.ok) throw new Error(data.error || 'Save failed');
        environmentConfig.items = data.items || [];
        environmentConfig.count = data.count || environmentConfig.items.length;
        renderEnvironmentSelector();
        renderEnvironmentList();
        clearEnvironmentForm();
        closeEnvironmentModal();
        let message = runBootstrap
          ? ('Portal environment saved and bootstrap completed.' + (data.runtime_env_path ? ' Runtime config: ' + data.runtime_env_path : ''))
          : 'Portal environment saved.';
        if (runBootstrap && data.portal_job_started) {
          message += ' Portal collector started.';
        } else if (runBootstrap && data.portal_job_already_running) {
          message += ' Portal collector was already running.';
        }
        if (runBootstrap && data.filer_job_started) {
          message += ' Filer collector started.';
        } else if (runBootstrap && data.filer_job_already_running) {
          message += ' Filer collector was already running.';
        }
        if (status) status.textContent = message;
        setActionStatus('environmentFlash', message, 'success');
      } catch (e) {
        if (status) status.textContent = 'Save failed: ' + e.message;
        setActionStatus('environmentFlash', 'Save failed: ' + e.message, 'error');
      } finally {
        setActionButtonsDisabled('environmentActions', false);
      }
    }

    async function deleteEnvironment(id){
      const status = document.getElementById('environmentStatus');
      setActionButtonsDisabled('environmentActions', true);
      setActionStatus('environmentFlash', 'Deleting portal environment...', 'working');
      try {
        const resp = await fetch('/environments_delete', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ id: id })
        });
        const data = await resp.json();
        if (!resp.ok || !data.ok) throw new Error(data.error || 'Delete failed');
        environmentConfig.items = data.items || [];
        environmentConfig.count = data.count || environmentConfig.items.length;
        if (Number(editingEnvironmentId) === Number(id)) clearEnvironmentForm();
        if (String(loadEnvironmentContext()) === String(id)) saveEnvironmentContext('admin');
        renderEnvironmentSelector();
        renderEnvironmentList();
        if (status) status.textContent = 'Portal environment deleted.';
        setActionStatus('environmentFlash', 'Portal environment deleted.', 'success');
      } catch (e) {
        if (status) status.textContent = 'Delete failed: ' + e.message;
        setActionStatus('environmentFlash', 'Delete failed: ' + e.message, 'error');
      } finally {
        setActionButtonsDisabled('environmentActions', false);
      }
    }

    function renderThresholdEditor(){
      const datasetKey = selectedThresholdDataset();
      const fieldName = selectedThresholdField();
      const dataset = thresholdDatasetEntry(datasetKey);
      const field = thresholdFieldEntry(datasetKey, fieldName);
      const pathEl = document.getElementById('thresholdPath');
      const summaryEl = document.getElementById('thresholdCurrentSummary');
      const titleEl = document.getElementById('thresholdEditorTitle');
      const statusEl = document.getElementById('thresholdSaveStatus');
      const rowsEl = document.getElementById('thresholdRows');
      const typeEl = document.getElementById('thresholdType');
      const countEl = document.getElementById('thresholdValueCount');
      const tagsEl = document.getElementById('thresholdExamples');
      if (pathEl) pathEl.textContent = thresholdCatalog.path || '';
      if (!dataset || !field) {
        if (titleEl) titleEl.textContent = 'Threshold Editor';
        if (summaryEl) summaryEl.textContent = 'Threshold rules are global. Email checks evaluate each portal environment against its own CSV files.';
        if (rowsEl) rowsEl.textContent = '0';
        if (typeEl) typeEl.textContent = '—';
        if (countEl) countEl.textContent = '0';
        if (tagsEl) tagsEl.innerHTML = '';
        return;
      }
      if (titleEl) titleEl.textContent = dataset.label + ' — ' + field.name;
      if (summaryEl) {
        const source = thresholdCatalog.source_label ? ('Alert scope: ' + thresholdCatalog.source_label + '. ') : '';
        summaryEl.textContent = source + 'Threshold rules are global. Email checks run separately against each environment.';
      }
      if (rowsEl) rowsEl.textContent = String(dataset.row_count || 0);
      if (typeEl) typeEl.textContent = field.current?.kind || 'unknown';
      if (countEl) countEl.textContent = String(field.current?.count || 0);
      if (tagsEl) tagsEl.innerHTML = '';
      document.getElementById('thresholdWarnOp').value = field.rule?.warn_op || '';
      document.getElementById('thresholdWarnValue').value = field.rule?.warn_value || '';
      document.getElementById('thresholdCritOp').value = field.rule?.crit_op || '';
      document.getElementById('thresholdCritValue').value = field.rule?.crit_value || '';
      document.getElementById('thresholdNotifyEnabled').checked = Boolean(field.notify?.enabled);
      document.getElementById('thresholdNotifySeverity').value = field.notify?.severity || 'critical';
      document.getElementById('thresholdRepeatMinutes').value = String(field.notify?.repeat_minutes || 0);
      document.getElementById('thresholdRecipientMode').value = field.notify?.recipient_mode || 'all_enabled';
      renderThresholdRecipientOptions(field.notify || {});
      if (statusEl) statusEl.textContent = 'Editing ' + dataset.label + ' / ' + field.name + '. Current rule: ' + describeThresholdRule(field.rule);
    }

    async function loadThresholdCatalog(){
      const statusEl = document.getElementById('thresholdSaveStatus');
      setActionStatus('thresholdFlash', 'Loading thresholds...', 'working');
      try {
        const resp = await fetch(apiUrl('/thresholds_catalog'));
        thresholdCatalog = await resp.json();
        if (thresholdCatalog.recipients) {
          notificationConfig.recipients = thresholdCatalog.recipients;
        }
        renderThresholdDatasetOptions();
        renderNotificationDatasetOptions();
        renderNotificationRecipients();
        renderAllThresholdList();
        setActionStatus('thresholdFlash', 'Thresholds loaded from ' + (thresholdCatalog.path || 'thresholds.yaml'), 'success');
      } catch (e) {
        if (statusEl) statusEl.textContent = 'Could not load thresholds catalog.';
        setActionStatus('thresholdFlash', 'Could not load thresholds catalog.', 'error');
        console.error('threshold catalog failed', e);
      }
    }

    async function saveThresholdRule(){
      const statusEl = document.getElementById('thresholdSaveStatus');
      const payload = {
        dataset: selectedThresholdDataset(),
        field: selectedThresholdField(),
        warn_op: document.getElementById('thresholdWarnOp').value,
        warn_value: document.getElementById('thresholdWarnValue').value,
        crit_op: document.getElementById('thresholdCritOp').value,
        crit_value: document.getElementById('thresholdCritValue').value,
        notify_enabled: document.getElementById('thresholdNotifyEnabled').checked,
        notify_severity: document.getElementById('thresholdNotifySeverity').value,
        notify_repeat_minutes: document.getElementById('thresholdRepeatMinutes').value,
        notify_recipient_mode: document.getElementById('thresholdRecipientMode').value,
        notify_recipient_ids: selectedMultiValues('thresholdRecipientIds'),
      };
      if (!payload.dataset || !payload.field) {
        if (statusEl) statusEl.textContent = 'Pick a dataset and field first.';
        setActionStatus('thresholdFlash', 'Pick a dataset and field first.', 'error');
        return;
      }
      if (statusEl) statusEl.textContent = 'Saving threshold to thresholds.yaml...';
      setActionStatus('thresholdFlash', 'Saving threshold to thresholds.yaml...', 'working');
      try {
        const resp = await fetch(apiUrl('/thresholds_save'), {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(payload)
        });
        const data = await resp.json();
        if (!resp.ok || !data.ok) {
          throw new Error(data.error || 'Save failed');
        }
        thresholdCatalog = data.catalog || thresholdCatalog;
        renderThresholdDatasetOptions();
        const datasetSel = document.getElementById('thresholdDataset');
        const fieldSel = document.getElementById('thresholdField');
        if (datasetSel) datasetSel.value = payload.dataset;
        renderThresholdFieldOptions();
        if (fieldSel) fieldSel.value = payload.field;
        renderThresholdEditor();
        renderAllThresholdList();
        const successMessage = 'Threshold saved to ' + (data.path || thresholdCatalog.path || 'thresholds.yaml');
        if (statusEl) statusEl.textContent = successMessage;
        setActionStatus('thresholdFlash', successMessage, 'success');
      } catch (e) {
        if (statusEl) statusEl.textContent = 'Save failed: ' + e.message;
        setActionStatus('thresholdFlash', 'Save failed: ' + e.message, 'error');
      }
    }

    async function deleteThresholdRule(fieldName, datasetKey){
      const dataset = datasetKey || selectedThresholdDataset();
      const targetField = fieldName || selectedThresholdField();
      const statusEl = document.getElementById('thresholdSaveStatus');
      if (!dataset || !targetField) {
        if (statusEl) statusEl.textContent = 'Pick a threshold first.';
        setActionStatus('thresholdFlash', 'Pick a threshold first.', 'error');
        return;
      }
      if (statusEl) statusEl.textContent = 'Deleting threshold from thresholds.yaml...';
      setActionStatus('thresholdFlash', 'Deleting threshold from thresholds.yaml...', 'working');
      try {
        const resp = await fetch(apiUrl('/thresholds_save'), {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            dataset: dataset,
            field: targetField,
            warn_op: '',
            warn_value: '',
            crit_op: '',
            crit_value: '',
          })
        });
        const data = await resp.json();
        if (!resp.ok || !data.ok) {
          throw new Error(data.error || 'Delete failed');
        }
        thresholdCatalog = data.catalog || thresholdCatalog;
        renderThresholdDatasetOptions();
        const datasetSel = document.getElementById('thresholdDataset');
        if (datasetSel) datasetSel.value = dataset;
        renderThresholdFieldOptions();
        renderAllThresholdList();
        const deleteMessage = 'Deleted threshold for ' + targetField;
        if (statusEl) statusEl.textContent = deleteMessage;
        setActionStatus('thresholdFlash', deleteMessage, 'success');
      } catch (e) {
        if (statusEl) statusEl.textContent = 'Delete failed: ' + e.message;
        setActionStatus('thresholdFlash', 'Delete failed: ' + e.message, 'error');
      }
    }

    function clearThresholdRule(){
      document.getElementById('thresholdWarnOp').value = '';
      document.getElementById('thresholdWarnValue').value = '';
      document.getElementById('thresholdCritOp').value = '';
      document.getElementById('thresholdCritValue').value = '';
      const statusEl = document.getElementById('thresholdSaveStatus');
      if (statusEl) statusEl.textContent = 'Threshold inputs cleared. Save now removes this field rule, or use Delete below.';
    }

    function init(){
      const inAdmin = loadEnvironmentContext() === 'admin';
      let initialTab = loadActive();
      if (initialTab === 'ai') initialTab = 'overview';
      const initialSection = NAV_SECTION_MAP[initialTab] || '';
      if (inAdmin && !initialSection.startsWith('admin_')) initialTab = 'admin_env';
      if (!inAdmin && initialSection.startsWith('admin_')) initialTab = 'overview';
      renderEnvironmentSelector();
      showTab(initialTab);
      if (document.getElementById('portal')) { showPortalTab(loadPortalActive()); }
      if (document.getElementById('pg')) { showPgTab(loadPgActive()); }
      if (document.getElementById('svrhlth')) { showHealthTab(loadHealthActive()); }
      hydrateLocalTimes();
      clearEnvironmentForm();
      reconcileContextAndActiveTab();
      loadEnvironmentConfig();
      loadThresholdCatalog();
      loadNotificationsConfig();
      loadAuthConfig();
      refreshJobStatus();
      window.setInterval(refreshJobStatus, 5000);
    }
    window.addEventListener('DOMContentLoaded', init);

    function openViewer(text){
      const bd = document.getElementById('viewerBackdrop');
      const pre = document.getElementById('viewerPre');
      pre.textContent = text || '';
      bd.style.display = 'flex';
    }
    function closeViewer(){ document.getElementById('viewerBackdrop').style.display = 'none'; }
    async function copyText(txt){
      try { await navigator.clipboard.writeText(txt || ''); alert('Copied'); }
      catch(e){
        const ta = document.createElement('textarea');
        ta.value = txt || '';
        document.body.appendChild(ta);
        ta.select();
        try { document.execCommand('copy'); alert('Copy failed'); } catch(e2){ alert('Copy failed'); }
        document.body.removeChild(ta);
      }
    }
  </script>
</head>
<body>
  <script>
    try {
      document.body.setAttribute('data-initial-context', (localStorage.getItem('fd.environmentContext') || 'admin') === 'admin' ? 'admin' : 'env');
    } catch (e) {
      document.body.setAttribute('data-initial-context', 'admin');
    }
  </script>
  <div class="app-shell">
    <aside class="sidebar">
      <div class="sidebar-brand">
        {% if brand.logo %}<img src="{{ brand.logo }}" alt="logo">{% endif %}
        <h1>{{ brand.title }}</h1>
      </div>
      <div class="sidebar-group">
        <div class="sidebar-label" id="sidebarSectionLabel">Administration</div>
        <div class="nav-sections">
        <div class="nav-section" data-section="dashboard" data-context="monitoring" aria-expanded="false">
          <button class="nav-group-btn" type="button" onclick="toggleNavSection('dashboard')">
            <span class="nav-group-title"><span class="tabicon"><svg viewBox="0 0 24 24"><rect x="3" y="3" width="8" height="8" rx="1.5"></rect><rect x="13" y="3" width="8" height="5" rx="1.5"></rect><rect x="13" y="10" width="8" height="11" rx="1.5"></rect><rect x="3" y="13" width="8" height="8" rx="1.5"></rect></svg></span>Main</span>
            <span class="nav-group-toggle">+</span>
          </button>
          <div class="nav-group-items">
            <button class="tabbtn nav-child" data-tab="overview" onclick="showTab('overview')">
              <span class="tabbtn-text"><span class="tabicon"><svg viewBox="0 0 24 24"><rect x="3" y="3" width="8" height="8" rx="1.5"></rect><rect x="13" y="3" width="8" height="5" rx="1.5"></rect><rect x="13" y="10" width="8" height="11" rx="1.5"></rect><rect x="3" y="13" width="8" height="8" rx="1.5"></rect></svg></span>Overview</span>
              <span class="tabbtn-meta">{% if overall_bad %}<span class="tabbadge crit" title="Critical rows">{{ overall_bad }}</span>{% endif %}{% if overall_warn %}<span class="tabbadge warn" title="Warning rows">{{ overall_warn }}</span>{% endif %}</span>
            </button>
            <button class="tabbtn nav-child" data-tab="jobs" onclick="showTab('jobs')">
              <span class="tabbtn-text"><span class="tabicon"><svg viewBox="0 0 24 24"><path d="M4 7h16"></path><path d="M4 12h10"></path><path d="M4 17h7"></path><circle cx="18" cy="12" r="3"></circle></svg></span>Run Jobs Now</span>
              <span class="tabbtn-meta"></span>
            </button>
          </div>
        </div>

        <div class="nav-section" data-section="portal" data-context="monitoring" aria-expanded="false">
          <button class="nav-group-btn" type="button" onclick="toggleNavSection('portal')">
            <span class="nav-group-title"><span class="tabicon"><svg viewBox="0 0 24 24"><path d="M4 20h16"></path><path d="M6 20V9l6-4 6 4v11"></path><path d="M9 20v-5h6v5"></path></svg></span>Portal</span>
            <span class="nav-group-toggle">+</span>
          </button>
          <div class="nav-group-items">
            <button class="tabbtn nav-child" data-tab="tenants" onclick="showTab('tenants')">
              <span class="tabbtn-text"><span class="tabicon"><svg viewBox="0 0 24 24"><path d="M8 11a3 3 0 1 0 0-6 3 3 0 0 0 0 6Z"></path><path d="M16 13a2.5 2.5 0 1 0 0-5 2.5 2.5 0 0 0 0 5Z"></path><path d="M3.5 19a4.5 4.5 0 0 1 9 0"></path><path d="M13 19a3.5 3.5 0 0 1 7 0"></path></svg></span>Tenants</span>
              <span class="tabbtn-meta">{% if tenants_counts.bad %}<span class="tabbadge crit" title="Critical rows">{{ tenants_counts.bad }}</span>{% endif %}{% if tenants_counts.warn %}<span class="tabbadge warn" title="Warning rows">{{ tenants_counts.warn }}</span>{% endif %}</span>
            </button>

            <button class="tabbtn nav-child" data-tab="portal" onclick="showTab('portal')">
              <span class="tabbtn-text"><span class="tabicon"><svg viewBox="0 0 24 24"><path d="M4 20h16"></path><path d="M6 20V9l6-4 6 4v11"></path><path d="M9 20v-5h6v5"></path></svg></span>Portal Admin</span>
              <span class="tabbtn-meta">{% if portal_counts.bad %}<span class="tabbadge crit" title="Critical rows">{{ portal_counts.bad }}</span>{% endif %}{% if portal_counts.warn %}<span class="tabbadge warn" title="Warning rows">{{ portal_counts.warn }}</span>{% endif %}</span>
            </button>

            <button class="tabbtn nav-child" data-tab="pg" onclick="showTab('pg')">
              <span class="tabbtn-text"><span class="tabicon"><svg viewBox="0 0 24 24"><ellipse cx="12" cy="5" rx="7" ry="3"></ellipse><path d="M5 5v6c0 1.7 3.1 3 7 3s7-1.3 7-3V5"></path><path d="M5 11v6c0 1.7 3.1 3 7 3s7-1.3 7-3v-6"></path></svg></span>Postgres</span>
              <span class="tabbtn-meta">{% if pg_counts.bad %}<span class="tabbadge crit" title="Critical rows">{{ pg_counts.bad }}</span>{% endif %}{% if pg_counts.warn %}<span class="tabbadge warn" title="Warning rows">{{ pg_counts.warn }}</span>{% endif %}</span>
            </button>

            <button class="tabbtn nav-child" data-tab="svrhlth" onclick="showTab('svrhlth')">
              <span class="tabbtn-text"><span class="tabicon"><svg viewBox="0 0 24 24"><path d="M3 19h18"></path><path d="M6 16V9"></path><path d="M12 16V5"></path><path d="M18 16v-3"></path></svg></span>Servers Health</span>
              <span class="tabbtn-meta">{% if hosts_counts.bad %}<span class="tabbadge crit" title="Critical rows">{{ hosts_counts.bad }}</span>{% endif %}{% if hosts_counts.warn %}<span class="tabbadge warn" title="Warning rows">{{ hosts_counts.warn }}</span>{% endif %}</span>
            </button>
          </div>
        </div>

        <div class="nav-section" data-section="edge" data-context="monitoring" aria-expanded="false">
          <button class="nav-group-btn" type="button" onclick="toggleNavSection('edge')">
            <span class="nav-group-title"><span class="tabicon"><svg viewBox="0 0 24 24"><path d="M4 7h16v10H4z"></path><path d="M8 7V5h8v2"></path><path d="M8 12h8"></path></svg></span>Edge Filers</span>
            <span class="nav-group-toggle">+</span>
          </button>
          <div class="nav-group-items">
            <button class="tabbtn nav-child" data-tab="edge" onclick="showTab('edge')">
              <span class="tabbtn-text"><span class="tabicon"><svg viewBox="0 0 24 24"><path d="M4 7h16v10H4z"></path><path d="M8 7V5h8v2"></path><path d="M8 12h8"></path></svg></span>Edge Filers</span>
              <span class="tabbtn-meta">{% if edge_counts.bad %}<span class="tabbadge crit" title="Critical rows">{{ edge_counts.bad }}</span>{% endif %}{% if edge_counts.warn %}<span class="tabbadge warn" title="Warning rows">{{ edge_counts.warn }}</span>{% endif %}</span>
            </button>
          </div>
        </div>

        <div class="nav-section" data-section="admin_main" data-context="administration" aria-expanded="false">
          <button class="nav-group-btn" type="button" onclick="toggleNavSection('admin_main')">
            <span class="nav-group-title"><span class="tabicon"><svg viewBox="0 0 24 24"><rect x="3" y="3" width="8" height="8" rx="1.5"></rect><rect x="13" y="3" width="8" height="5" rx="1.5"></rect><rect x="13" y="10" width="8" height="11" rx="1.5"></rect><rect x="3" y="13" width="8" height="8" rx="1.5"></rect></svg></span>Main</span>
            <span class="nav-group-toggle">+</span>
          </button>
          <div class="nav-group-items">
            <button class="tabbtn nav-child" data-tab="admin_prereq" onclick="showTab('admin_prereq')">
              <span class="tabbtn-text"><span class="tabicon"><svg viewBox="0 0 24 24"><path d="M12 3v18"></path><path d="M3 12h18"></path></svg></span>Prerequisites</span>
              <span class="tabbtn-meta"></span>
            </button>
            <button class="tabbtn nav-child" data-tab="admin_env" onclick="showTab('admin_env')">
              <span class="tabbtn-text"><span class="tabicon"><svg viewBox="0 0 24 24"><path d="M4 6h16v12H4z"></path><path d="M8 6V4h8v2"></path><path d="M8 12h8"></path></svg></span>Portals</span>
              <span class="tabbtn-meta"></span>
            </button>
          </div>
        </div>

        <div class="nav-section" data-section="admin_thresholds" data-context="administration" aria-expanded="false">
          <button class="nav-group-btn" type="button" onclick="toggleNavSection('admin_thresholds')">
            <span class="nav-group-title"><span class="tabicon"><svg viewBox="0 0 24 24"><path d="M12 3v18"></path><path d="M7 8h9"></path><path d="M5 14h11"></path><circle cx="16.5" cy="14" r="2.5"></circle></svg></span>Threshold Settings</span>
            <span class="nav-group-toggle">+</span>
          </button>
          <div class="nav-group-items">
            <button class="tabbtn nav-child" data-tab="thresholds" onclick="showTab('thresholds')">
              <span class="tabbtn-text"><span class="tabicon"><svg viewBox="0 0 24 24"><path d="M12 3v18"></path><path d="M7 8h9"></path><path d="M5 14h11"></path><circle cx="16.5" cy="14" r="2.5"></circle></svg></span>Thresholds</span>
              <span class="tabbtn-meta"></span>
            </button>
            <button class="tabbtn nav-child" data-tab="thresholds_all" onclick="showTab('thresholds_all')">
              <span class="tabbtn-text"><span class="tabicon"><svg viewBox="0 0 24 24"><path d="M4 6h16"></path><path d="M4 12h16"></path><path d="M4 18h16"></path><circle cx="7" cy="6" r="1"></circle><circle cx="7" cy="12" r="1"></circle><circle cx="7" cy="18" r="1"></circle></svg></span>All Threshold List</span>
              <span class="tabbtn-meta"></span>
            </button>
          </div>
        </div>

        <div class="nav-section" data-section="admin_notifications" data-context="administration" aria-expanded="false">
          <button class="nav-group-btn" type="button" onclick="toggleNavSection('admin_notifications')">
            <span class="nav-group-title"><span class="tabicon"><svg viewBox="0 0 24 24"><path d="M4 6h16v12H4z"></path><path d="M4 8l8 6 8-6"></path></svg></span>Notification Settings</span>
            <span class="nav-group-toggle">+</span>
          </button>
          <div class="nav-group-items">
            <button class="tabbtn nav-child" data-tab="notify_settings" onclick="showTab('notify_settings')">
              <span class="tabbtn-text"><span class="tabicon"><svg viewBox="0 0 24 24"><path d="M4 6h16v12H4z"></path><path d="M4 8l8 6 8-6"></path></svg></span>Email Settings</span>
              <span class="tabbtn-meta"></span>
            </button>
            <button class="tabbtn nav-child" data-tab="notify_recipients" onclick="showTab('notify_recipients')">
              <span class="tabbtn-text"><span class="tabicon"><svg viewBox="0 0 24 24"><path d="M8 11a3 3 0 1 0 0-6 3 3 0 0 0 0 6Z"></path><path d="M16 13a2.5 2.5 0 1 0 0-5 2.5 2.5 0 0 0 0 5Z"></path><path d="M3.5 19a4.5 4.5 0 0 1 9 0"></path><path d="M13 19a3.5 3.5 0 0 1 7 0"></path></svg></span>Email Recipients</span>
              <span class="tabbtn-meta"></span>
            </button>
          </div>
        </div>

        <div class="nav-section" data-section="admin_auth" data-context="administration" aria-expanded="false">
          <button class="nav-group-btn" type="button" onclick="toggleNavSection('admin_auth')">
            <span class="nav-group-title"><span class="tabicon"><svg viewBox="0 0 24 24"><rect x="3" y="10" width="18" height="11" rx="2"></rect><path d="M7 10V7a5 5 0 0 1 10 0v3"></path><circle cx="12" cy="15.5" r="1"></circle></svg></span>Access Control</span>
            <span class="nav-group-toggle">+</span>
          </button>
          <div class="nav-group-items">
            <button class="tabbtn nav-child" data-tab="auth_settings" onclick="showTab('auth_settings')">
              <span class="tabbtn-text"><span class="tabicon"><svg viewBox="0 0 24 24"><rect x="3" y="10" width="18" height="11" rx="2"></rect><path d="M7 10V7a5 5 0 0 1 10 0v3"></path><circle cx="12" cy="15.5" r="1"></circle></svg></span>User Auth</span>
              <span class="tabbtn-meta"></span>
            </button>
          </div>
        </div>

        <div class="nav-section" data-section="admin_help" data-context="administration" aria-expanded="false">
          <button class="nav-group-btn" type="button" onclick="toggleNavSection('admin_help')">
            <span class="nav-group-title"><span class="tabicon"><svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="9"></circle><path d="M12 17v-5"></path><circle cx="12" cy="8" r="1"></circle></svg></span>Help</span>
            <span class="nav-group-toggle">+</span>
          </button>
          <div class="nav-group-items">
            <button class="tabbtn nav-child" data-tab="about" onclick="showTab('about')">
              <span class="tabbtn-text"><span class="tabicon"><svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="9"></circle><path d="M12 17v-5"></path><circle cx="12" cy="8" r="1"></circle></svg></span>About</span>
              <span class="tabbtn-meta"></span>
            </button>
          </div>
        </div>
        </div>
      </div>
    </aside>

    <main class="main-shell">
      <header class="topbar">
        <div>
          <h2>CTERA Operations Console</h2>
          <div class="topbar-sub">Portal-style monitoring view for collectors, tenants, filers, database health, and server posture.</div>
        </div>
        <div class="topbar-meta">
          <div class="top-context">
            <label for="environmentContextSelect">Context</label>
            <select id="environmentContextSelect" onchange="handleEnvironmentContextChange()">
              <option value="admin">Administration</option>
            </select>
          </div>
          {% if auth_mode == 'local' and current_username %}
          <div class="top-user">
            <span class="top-user-name">Signed in as <strong>{{ current_username }}</strong></span>
            <a class="top-logout" href="/logout">Log Out</a>
          </div>
          {% endif %}
          <span class="top-pill" id="environmentContextLabel">Administration</span>
          <span class="top-pill">{{ overall_risk_label }}</span>
          <span class="top-pill">{{ overall_rows }} monitored rows</span>
        </div>
      </header>

      <div class="content-shell">

  <!-- OVERVIEW -->
  <div id="overview" class="tabpane" style="display:none">
    <div class="hero-grid">
      <section class="hero-panel">
        <div class="hero-title">
          <div>
            <h2>Operations Overview</h2>
            <div class="hero-sub">Live view from the latest collector CSVs. Click any card to drill into the source table.</div>
          </div>
          <span class="risk-pill {{ overall_risk_class }}">{{ overall_risk_label }}</span>
        </div>
        <div class="headline-metrics">
          <div class="headline-metric">
            <div class="metric-label">Total Rows</div>
            <div class="metric-value">{{ overall_rows }}</div>
          </div>
          <div class="headline-metric">
            <div class="metric-label">Critical Rows</div>
            <div class="metric-value crit">{{ overall_bad }}</div>
          </div>
          <div class="headline-metric">
            <div class="metric-label">Warning Rows</div>
            <div class="metric-value warn">{{ overall_warn }}</div>
          </div>
        </div>
        <div class="overview-grid">
          {% for card in overview_cards %}
          <article class="dash-card" onclick="showTab('{{ card.tab }}')">
            <div class="dash-card-head">
              <div>
                <h3>{{ card.label }}</h3>
                <div class="hero-sub"><span data-local-time="{{ card.updated_utc }}">{{ card.updated_utc or '—' }}</span></div>
              </div>
              <div class="count {{ card.status_class }}">{{ card.status_text }}</div>
            </div>
            <div class="stack-bar" title="{{ card.bad }} critical, {{ card.warn }} warning, {{ card.ok }} ok">
              <div class="bar-crit" style="width:{{ card.bad_pct }}%"></div>
              <div class="bar-warn" style="width:{{ card.warn_pct }}%"></div>
              <div class="bar-ok" style="width:{{ card.ok_pct }}%"></div>
            </div>
            <div class="dash-card-foot">
              <span class="mini-stat"><span class="dot dcrit"></span>{{ card.bad }} critical</span>
              <span class="mini-stat"><span class="dot dwarn"></span>{{ card.warn }} warning</span>
              <span class="mini-stat"><span class="dot dok"></span>{{ card.ok }} ok</span>
              <span class="mini-stat">{{ card.rows }} rows</span>
            </div>
          </article>
          {% endfor %}
        </div>
      </section>

      <aside class="hero-panel">
        <div class="hero-title">
          <div>
            <h2>Data Freshness</h2>
            <div class="hero-sub">Collector outputs currently driving the dashboard.</div>
          </div>
        </div>
        <div class="ops-list">
          {% for item in freshness_items %}
          <div class="ops-row">
            <strong>{{ item.label }}</strong>
            <span data-local-time="{{ item.updated_utc }}">{{ item.updated_utc or '—' }}</span>
          </div>
          {% endfor %}
        </div>
      </aside>
    </div>

  </div>

  <!-- RUN JOBS NOW -->
  <div id="jobs" class="tabpane" style="display:none">
    <section class="hero-panel">
      <div class="hero-title">
        <div>
          <h2>Run Jobs Now</h2>
          <div class="hero-sub">Run the dashboard collectors on demand and keep an eye on the latest status in one place.</div>
        </div>
      </div>

      <div class="ops-toolbar">
        <div class="ops-toolbar-copy">
          <div class="ops-actions">
            <button id="runBtn_portal" class="ops-btn" onclick="runCollector('portal')">Run Portal Jobs</button>
            <button id="runBtn_filer" class="ops-btn" onclick="runCollector('filer')">Run Filer Jobs</button>
            <button id="runBtn_all" class="ops-btn primary" onclick="runCollector('all')">Run All Collectors</button>
          </div>
          <div class="hero-sub">Use these when you want fresh CSVs right now instead of waiting for cron.</div>
        </div>
        <div class="hero-sub">Watch live logs in a terminal with <code>tail -F /var/log/ctera-monitoring-dashboard/portal.log</code> or <code>tail -F /var/log/ctera-monitoring-dashboard/filer.log</code>.</div>
      </div>

      <div class="ops-status-grid">
        <article class="ops-status-card">
          <div class="ops-status-head">
            <div>
              <h3>Portal Collector</h3>
              <div class="hero-sub">Portal, MainDB, Postgres, tenant, and server metrics jobs.</div>
            </div>
            <span id="jobBadge_portal" class="ops-badge idle">Idle</span>
          </div>
          <div class="ops-meta">
            <span>Started <strong id="jobStarted_portal">&mdash;</strong></span>
            <span>Finished <strong id="jobFinished_portal">&mdash;</strong></span>
            <span>Exit <strong id="jobExit_portal">&mdash;</strong></span>
          </div>
          <div class="hero-sub">Recent output</div>
          <pre id="jobTail_portal" class="ops-logtail">No recent log lines.</pre>
          <div class="ops-loghint">Tail command: <code id="jobTailCmd_portal">tail -F /var/log/ctera-monitoring-dashboard/portal.log</code></div>
        </article>

        <article class="ops-status-card">
          <div class="ops-status-head">
            <div>
              <h3>Filer Collector</h3>
              <div class="hero-sub">Edge filer inventory, sync status, health, and performance jobs.</div>
            </div>
            <span id="jobBadge_filer" class="ops-badge idle">Idle</span>
          </div>
          <div class="ops-meta">
            <span>Started <strong id="jobStarted_filer">&mdash;</strong></span>
            <span>Finished <strong id="jobFinished_filer">&mdash;</strong></span>
            <span>Exit <strong id="jobExit_filer">&mdash;</strong></span>
          </div>
          <div class="hero-sub">Recent output</div>
          <pre id="jobTail_filer" class="ops-logtail">No recent log lines.</pre>
          <div class="ops-loghint">Tail command: <code id="jobTailCmd_filer">tail -F /var/log/ctera-monitoring-dashboard/filer.log</code></div>
        </article>
      </div>
    </section>
  </div>

  <!-- THRESHOLDS -->
  <div id="thresholds" class="tabpane" style="display:none">
    <div class="hero-title">
      <div>
        <h2>Thresholds</h2>
        <div class="hero-sub">Admins can manage warning and critical limits here. The dashboard writes directly to <code>thresholds.yaml</code>, so nobody needs to edit YAML by hand.</div>
      </div>
    </div>

    <div class="threshold-layout">
      <aside class="threshold-sidebar">
        <section class="threshold-card">
          <h3>Choose What To Edit</h3>
          <div class="threshold-field">
            <label for="thresholdDataset">Dataset</label>
            <select id="thresholdDataset" class="threshold-select" onchange="renderThresholdFieldOptions()"></select>
          </div>
          <div class="threshold-field" style="margin-top:12px;">
            <label for="thresholdField">Field</label>
            <select id="thresholdField" class="threshold-select" onchange="renderThresholdEditor()"></select>
          </div>
          <div class="threshold-status" id="thresholdSaveStatus">Loading current thresholds...</div>
        </section>

        <section class="threshold-card">
          <h3>Threshold File</h3>
          <div class="threshold-path">Changes save directly to:</div>
          <div class="threshold-path"><code id="thresholdPath">thresholds.yaml</code></div>
          <div class="threshold-summary">The YAML stays as the system source of truth, but admins can manage it entirely from this screen.</div>
        </section>

        <section class="threshold-card">
          <h3>Current Thresholds</h3>
          <div class="threshold-summary">Showing rules for <strong id="thresholdCurrentDataset">Selected Dataset</strong>.</div>
          <div class="threshold-summary">Existing rules for the selected dataset. Click a row to load it into the editor.</div>
          <div class="threshold-summary">Active rules: <strong id="thresholdCurrentCount">0</strong></div>
          <div id="thresholdCurrentListEmpty" class="threshold-empty">No threshold rules saved yet for this dataset.</div>
          <div class="threshold-list">
            <table>
              <thead>
                <tr>
                  <th>Field</th>
                  <th>Warning</th>
                  <th>Critical</th>
                  <th>Actions</th>
                </tr>
              </thead>
              <tbody id="thresholdCurrentListBody"></tbody>
            </table>
          </div>
        </section>
      </aside>

      <section class="threshold-card">
        <h3 id="thresholdEditorTitle">Threshold Editor</h3>
        <div class="threshold-kpis">
          <div class="threshold-kpi">
            <div class="metric-label">Dataset Rows</div>
            <div class="metric-value" id="thresholdRows">0</div>
          </div>
          <div class="threshold-kpi">
            <div class="metric-label">Value Type</div>
            <div class="metric-value" id="thresholdType">—</div>
          </div>
          <div class="threshold-kpi">
            <div class="metric-label">Observed Values</div>
            <div class="metric-value" id="thresholdValueCount">0</div>
          </div>
        </div>

        <div class="threshold-summary" id="thresholdCurrentSummary">Pick a dataset and field to see current values and define thresholds.</div>
        <div class="threshold-tags" id="thresholdExamples"></div>

        <div class="threshold-form-grid">
          <div class="threshold-field">
            <label for="thresholdWarnOp">Warning Operator</label>
            <select id="thresholdWarnOp" class="threshold-select">
              <option value="">No warning threshold</option>
              <option value="gt">Greater than</option>
              <option value="ge">Greater than or equal</option>
              <option value="lt">Less than</option>
              <option value="le">Less than or equal</option>
              <option value="eq">Equals</option>
              <option value="ne">Not equal</option>
            </select>
          </div>
          <div class="threshold-field">
            <label for="thresholdWarnValue">Warning Value</label>
            <input id="thresholdWarnValue" class="threshold-input" type="text" placeholder="Example: 80, true, Offline">
          </div>
          <div class="threshold-field">
            <label for="thresholdCritOp">Critical Operator</label>
            <select id="thresholdCritOp" class="threshold-select">
              <option value="">No critical threshold</option>
              <option value="gt">Greater than</option>
              <option value="ge">Greater than or equal</option>
              <option value="lt">Less than</option>
              <option value="le">Less than or equal</option>
              <option value="eq">Equals</option>
              <option value="ne">Not equal</option>
            </select>
          </div>
          <div class="threshold-field">
            <label for="thresholdCritValue">Critical Value</label>
            <input id="thresholdCritValue" class="threshold-input" type="text" placeholder="Example: 90, false, Failed">
          </div>
        </div>

        <div class="threshold-card" style="margin-top:14px; padding:12px;">
          <h3 style="margin-bottom:10px;">Email Notifications</h3>
          <div class="threshold-form-grid">
            <div class="threshold-field">
              <label class="notify-checkbox" for="thresholdNotifyEnabled">
                <input id="thresholdNotifyEnabled" type="checkbox">
                Email when this threshold is met
              </label>
              <div class="notify-helper">This threshold is the master switch. If enabled, the alert is emailed once by default, then remembered in SQLite so service restarts do not resend it.</div>
            </div>
            <div class="threshold-field">
              <label for="thresholdNotifySeverity">Notify On</label>
              <select id="thresholdNotifySeverity" class="threshold-select">
                <option value="critical">Critical only</option>
                <option value="warning">Warning only</option>
                <option value="both">Warning and critical</option>
              </select>
            </div>
            <div class="threshold-field">
              <label for="thresholdRepeatMinutes">Repeat Every (minutes)</label>
              <input id="thresholdRepeatMinutes" class="threshold-input" type="number" min="0" step="1" placeholder="0 = once only">
              <div class="notify-helper">Use <code>0</code> to send once only while the alert stays active. It will not resend until the condition clears and comes back. Use <code>60</code> for hourly reminders on persistent problems.</div>
            </div>
            <div class="threshold-field">
              <label for="thresholdRecipientMode">Recipient Scope</label>
              <select id="thresholdRecipientMode" class="threshold-select" onchange="renderThresholdRecipientOptions()">
                <option value="all_enabled">All enabled recipients</option>
                <option value="selected">Selected recipients only</option>
              </select>
              <div class="notify-helper">Recipient settings define who is eligible. This threshold setting decides whether email is sent at all.</div>
            </div>
            <div class="threshold-field" style="grid-column:1 / -1;">
              <label for="thresholdRecipientIds">Recipients</label>
              <select id="thresholdRecipientIds" class="threshold-select" multiple size="5"></select>
              <div id="thresholdRecipientHelp" class="notify-helper">This threshold emails all enabled recipients. Recipient scope still applies, but the threshold itself must also have email enabled.</div>
            </div>
          </div>
        </div>

        <div class="threshold-actions">
          <button class="ops-btn primary" onclick="saveThresholdRule()">Save Threshold</button>
          <button class="ops-btn" onclick="clearThresholdRule()">Clear Inputs</button>
          <button class="ops-btn" onclick="deleteThresholdRule()">Delete This Threshold</button>
          <button class="ops-btn" onclick="runNotificationCheck()">Run Alert Check Now</button>
          <button class="ops-btn" onclick="showTab('thresholds_all')">View All Thresholds</button>
          <button class="ops-btn" onclick="loadThresholdCatalog()">Reload From File</button>
        </div>
        <div class="action-status" id="thresholdFlash"></div>
      </section>
    </div>
  </div>

  <div id="admin_prereq" class="tabpane" style="display:none">
    <div class="hero-title">
      <div>
        <h2>Prerequisites</h2>
        <div class="hero-sub">Review these requirements before adding a portal environment so bootstrap and collectors can complete cleanly.</div>
      </div>
    </div>

    <section class="threshold-card">
      <div class="threshold-master-head">
        <div>
          <h3 style="margin:0; color:var(--primary);">Before You Add A Portal</h3>
          <div class="threshold-summary">Use this as the onboarding checklist for each new portal environment.</div>
        </div>
        <div class="notify-actions">
          <button class="ops-btn primary" onclick="showTab('admin_env')">Go To Portals</button>
        </div>
      </div>

      <div class="notify-grid">
        <article class="notify-card">
          <h4>CTERA Access</h4>
          <ul class="about-list">
            <li>Make sure you have created a read-only administrator in Global Admin. We recommend naming that user <strong>monitoring</strong>.</li>
            <li>Know the password for the read-only administrator before starting portal setup.</li>
          </ul>
        </article>
        <article class="notify-card">
          <h4>Network Ports</h4>
          <ul class="about-list">
            <li>Open port <strong>22</strong> and port <strong>5432</strong> between this monitoring server and MainDB.</li>
            <li>Open port <strong>443</strong> between this monitoring server and the Tomcat servers.</li>
          </ul>
        </article>
        <article class="notify-card">
          <h4>MainDB SSH Access</h4>
          <ul class="about-list">
            <li>Have a user that can SSH to MainDB using either a password or a private key.</li>
            <li>That SSH user can be <strong>root</strong>, or another user that can <strong>sudo</strong> to root.</li>
          </ul>
        </article>
        <article class="notify-card">
          <h4>Bootstrap Behavior</h4>
          <ul class="about-list">
            <li>The dashboard uses the initial SSH access mode one time for bootstrap.</li>
            <li>After bootstrap, the dashboard uses the installed SSH key and saved runtime environment for ongoing runs.</li>
          </ul>
        </article>
      </div>
    </section>
  </div>

  <div id="admin_env" class="tabpane" style="display:none">
    <div class="hero-title">
      <div>
        <h2>Portals</h2>
        <div class="hero-sub">Add the portal systems this dashboard should know about. Phase A stores the environments here and lets admins switch context from the top selector, without re-running the installer.</div>
      </div>
    </div>

    <section class="threshold-card">
      <div class="threshold-master-head">
        <div>
          <h3 style="margin:0; color:var(--primary);">Configured Portal Environments</h3>
          <div class="threshold-summary">Environment count: <strong id="environmentCount">0</strong></div>
        </div>
        <div class="notify-actions">
          <button class="ops-btn" onclick="showTab('admin_prereq')">Review Prerequisites</button>
          <button class="ops-btn primary" onclick="openEnvironmentModal('new')">New Portal Environment</button>
          <button class="ops-btn" onclick="loadEnvironmentConfig()">Reload</button>
        </div>
      </div>
      <div class="env-note">Review <strong>Prerequisites</strong> before adding a portal. Then use <strong>New Portal Environment</strong> to add it. The dashboard uses the initial SSH access mode one time to bootstrap access and retrieve what it needs, then uses the installed key going forward.</div>
      <div class="threshold-status" id="environmentStatus">Loading portal environments...</div>
      <div id="environmentListEmpty" class="notify-empty">No portal environments saved yet.</div>
      <div class="notify-table-wrap">
        <table>
          <thead>
            <tr>
              <th>Name</th>
              <th>Portal</th>
              <th>MainDB</th>
              <th>Status</th>
              <th>Updated</th>
              <th>Actions</th>
            </tr>
          </thead>
          <tbody id="environmentListBody"></tbody>
        </table>
      </div>
    </section>
  </div>

  <div id="thresholds_all" class="tabpane" style="display:none">
    <div class="hero-title">
      <div>
        <h2>All Threshold List</h2>
        <div class="hero-sub">One place to review every saved threshold across the dashboard. Use Edit to jump into the Thresholds page, or Delete to remove a rule directly.</div>
      </div>
    </div>

    <section class="threshold-card">
      <div class="threshold-master-head">
        <div>
          <h3 style="margin:0; color:var(--primary);">Saved Thresholds</h3>
          <div class="threshold-summary">Total active rules: <strong id="allThresholdCount">0</strong></div>
        </div>
        <div style="display:flex; align-items:center; gap:10px; flex-wrap:wrap; justify-content:flex-end;">
          <button class="ops-btn" onclick="runNotificationCheck()">Run Alert Check Now</button>
          <div class="threshold-path">Source file: <code id="allThresholdPath">thresholds.yaml</code></div>
        </div>
      </div>

      <div id="allThresholdListEmpty" class="threshold-empty">No saved thresholds yet.</div>
      <div class="threshold-list">
        <table>
          <thead>
            <tr>
              <th>Dataset</th>
              <th>Field</th>
              <th>Warning</th>
              <th>Critical</th>
              <th>Email</th>
              <th>Actions</th>
            </tr>
          </thead>
          <tbody id="allThresholdListBody"></tbody>
        </table>
      </div>
    </section>
  </div>

  <div id="notify_settings" class="tabpane" style="display:none">
    <div class="hero-title">
      <div>
        <h2>Email Settings</h2>
        <div class="hero-sub">Set the SMTP server here. The dashboard creates SQLite automatically and stores alert memory there so once-only emails stay remembered after restarts.</div>
      </div>
    </div>

    <div class="notify-grid">
      <aside class="threshold-sidebar">
        <section class="threshold-card">
          <h3>Alert Memory</h3>
          <div class="notify-summary-grid">
            <div class="headline-metric">
              <div class="metric-label">Active</div>
              <div class="metric-value" id="notifyActiveAlerts">0</div>
            </div>
            <div class="headline-metric">
              <div class="metric-label">Cleared</div>
              <div class="metric-value" id="notifyClearedAlerts">0</div>
            </div>
            <div class="headline-metric">
              <div class="metric-label">Tracked</div>
              <div class="metric-value" id="notifyTotalAlerts">0</div>
            </div>
          </div>
          <div class="threshold-path">SQLite file:</div>
          <div class="threshold-path"><code id="notifyDbPath">notifications.sqlite</code></div>
          <div class="threshold-summary">This file is auto-created on first run. No manual SQLite setup is required, and it remembers which alerts were already emailed.</div>
        </section>
      </aside>

      <section class="threshold-card">
        <h3>SMTP Configuration</h3>
        <div class="threshold-form-grid">
          <div class="threshold-field">
            <label for="smtpHost">SMTP Host</label>
            <input id="smtpHost" class="threshold-input" type="text" placeholder="smtp.office365.com">
          </div>
          <div class="threshold-field">
            <label for="smtpPort">SMTP Port</label>
            <input id="smtpPort" class="threshold-input" type="number" min="1" step="1" placeholder="587">
          </div>
          <div class="threshold-field">
            <label for="smtpUsername">SMTP Username</label>
            <input id="smtpUsername" class="threshold-input" type="text" placeholder="alerts@example.com">
          </div>
          <div class="threshold-field">
            <label for="smtpPassword">SMTP Password</label>
            <input id="smtpPassword" class="threshold-input" type="password" placeholder="Leave blank to keep saved password">
            <div id="smtpPasswordHint" class="notify-helper">No password saved yet.</div>
          </div>
          <div class="threshold-field">
            <label for="senderName">Sender Name</label>
            <input id="senderName" class="threshold-input" type="text" placeholder="CTERA Monitoring Dashboard">
          </div>
          <div class="threshold-field">
            <label for="senderEmail">Sender Email</label>
            <input id="senderEmail" class="threshold-input" type="email" placeholder="alerts@example.com">
          </div>
          <div class="threshold-field">
            <label class="notify-checkbox" for="smtpUseTls">
              <input id="smtpUseTls" type="checkbox">
              Use STARTTLS
            </label>
          </div>
          <div class="threshold-field">
            <label class="notify-checkbox" for="smtpUseSsl">
              <input id="smtpUseSsl" type="checkbox">
              Use SSL
            </label>
          </div>
          <div class="threshold-field" style="grid-column:1 / -1;">
            <label for="testEmailTarget">Test Email Target</label>
            <input id="testEmailTarget" class="threshold-input" type="email" placeholder="ops-team@example.com">
            <div class="notify-helper">Useful after saving SMTP settings, just to make sure mail flow is working.</div>
          </div>
        </div>
        <div class="notify-actions" id="notifySettingsActions">
          <button class="ops-btn primary" onclick="saveNotificationSettings()">Save Email Settings</button>
          <button class="ops-btn" onclick="sendNotificationTestEmail()">Send Test Email</button>
          <button class="ops-btn" onclick="runNotificationCheck()">Run Alert Check Now</button>
          <button class="ops-btn" onclick="showTab('notify_recipients')">Manage Recipients</button>
          <button class="ops-btn" onclick="loadNotificationsConfig()">Reload</button>
        </div>
        <div class="action-status" id="notifySettingsFlash"></div>
        <div class="threshold-status" id="notifySettingsStatus">Loading notification settings...</div>
      </section>
    </div>
  </div>

  <div id="notify_recipients" class="tabpane" style="display:none">
    <div class="hero-title">
      <div>
        <h2>Email Recipients</h2>
        <div class="hero-sub">Add the admins who should receive threshold emails. You can keep them global or narrow them to specific datasets and severities, but a threshold still needs email enabled before anything is sent.</div>
      </div>
    </div>

    <div class="notify-grid">
      <aside class="threshold-sidebar">
        <section class="threshold-card">
          <h3 id="recipientEditorTitle">Add Recipient</h3>
          <div class="threshold-form-grid">
            <div class="threshold-field">
              <label for="recipientName">Display Name</label>
              <input id="recipientName" class="threshold-input" type="text" placeholder="Platform Operations">
            </div>
            <div class="threshold-field">
              <label for="recipientEmail">Email Address</label>
              <input id="recipientEmail" class="threshold-input" type="email" placeholder="ops@example.com">
            </div>
            <div class="threshold-field" style="grid-column:1 / -1;">
              <label class="notify-checkbox" for="recipientEnabled">
                <input id="recipientEnabled" type="checkbox" checked>
                Enabled recipient
              </label>
            </div>
            <div class="threshold-field">
              <label for="recipientDatasets">Datasets (optional)</label>
              <select id="recipientDatasets" class="threshold-select" multiple size="6"></select>
              <div class="notify-actions" style="margin-top:8px;">
                <button class="threshold-row-btn" type="button" onclick="clearMultiSelect('recipientDatasets')">Clear Datasets</button>
              </div>
              <div class="notify-helper">Leave empty to let this recipient receive alerts from all datasets when a threshold has email enabled.</div>
            </div>
            <div class="threshold-field">
              <label for="recipientSeverities">Severities (optional)</label>
              <select id="recipientSeverities" class="threshold-select" multiple size="3">
                <option value="warning">Warning</option>
                <option value="critical">Critical</option>
              </select>
              <div class="notify-actions" style="margin-top:8px;">
                <button class="threshold-row-btn" type="button" onclick="clearMultiSelect('recipientSeverities')">Clear Severities</button>
              </div>
              <div class="notify-helper">Leave empty to receive both warning and critical notifications when the threshold is configured to send them.</div>
            </div>
          </div>
          <div class="notify-actions" id="notifyRecipientsActions">
            <button class="ops-btn primary" onclick="saveRecipient()">Save Recipient</button>
            <button class="ops-btn" onclick="clearRecipientForm()">Clear</button>
            <button class="ops-btn" onclick="loadNotificationsConfig()">Reload</button>
          </div>
          <div class="action-status" id="notifyRecipientsFlash"></div>
          <div class="threshold-status" id="notifyRecipientsStatus">Loading recipients...</div>
        </section>
      </aside>

      <section class="threshold-card">
        <div class="threshold-master-head">
          <div>
            <h3 style="margin:0; color:var(--primary);">Configured Recipients</h3>
            <div class="threshold-summary">Current recipient count: <strong id="notifyRecipientCount">0</strong></div>
          </div>
        </div>
        <div id="notifyRecipientsEmpty" class="notify-empty">No recipients saved yet.</div>
        <div class="notify-table-wrap">
          <table>
            <thead>
              <tr>
                <th>Name</th>
                <th>Email</th>
                <th>Status</th>
                <th>Datasets</th>
                <th>Severities</th>
                <th>Actions</th>
              </tr>
            </thead>
            <tbody id="notifyRecipientsBody"></tbody>
          </table>
        </div>
      </section>
    </div>
  </div>

  <div id="auth_settings" class="tabpane" style="display:none">
    <div class="hero-title">
      <div>
        <h2>Access Control</h2>
        <div class="hero-sub">Choose whether the dashboard needs no login or local username/password accounts. Default stays open access until you change it here.</div>
      </div>
    </div>

    <div class="notify-grid">
      <aside class="threshold-sidebar">
        <section class="threshold-card">
          <h3>Access Mode</h3>
          <div class="threshold-field">
            <label for="authMode">Login Requirement</label>
            <select id="authMode" class="threshold-select">
              <option value="none">No login required</option>
              <option value="local">Local username and password</option>
            </select>
            <div class="notify-helper">Use no login for trusted internal access. Switch to local login when you want dashboard-created accounts.</div>
          </div>
          <div class="notify-actions" id="authSettingsActions">
            <button class="ops-btn primary" onclick="saveAuthSettings()">Save Access Control</button>
          </div>
        </section>
      </aside>

      <section class="threshold-card">
        <h3 id="authUserEditorTitle">Add Local User</h3>
        <div class="threshold-form-grid">
          <div class="threshold-field">
            <label for="authUsername">Username</label>
            <input id="authUsername" class="threshold-input" type="text" placeholder="monitoring-admin">
            <div class="notify-helper">People sign in with the <strong>Username</strong>. Display Name is only for display inside the dashboard.</div>
          </div>
          <div class="threshold-field">
            <label for="authDisplayName">Display Name</label>
            <input id="authDisplayName" class="threshold-input" type="text" placeholder="Monitoring Admin">
          </div>
          <div class="threshold-field">
            <label for="authPassword">Password</label>
            <input id="authPassword" class="threshold-input" type="password" placeholder="Enter a password">
            <div class="notify-helper">When editing a user, leave password blank to keep the current password.</div>
          </div>
          <div class="threshold-field">
            <label for="authPasswordConfirm">Confirm Password</label>
            <input id="authPasswordConfirm" class="threshold-input" type="password" placeholder="Type the same password again">
            <div class="notify-helper">This helps catch typos before saving the login.</div>
          </div>
          <div class="threshold-field">
            <label class="notify-checkbox" for="authUserEnabled">
              <input id="authUserEnabled" type="checkbox" checked>
              Enabled user
            </label>
          </div>
        </div>
        <div class="notify-actions" id="authUserActions">
          <button class="ops-btn primary" onclick="saveAuthUser()">Save Local User</button>
          <button class="ops-btn" onclick="clearAuthUserForm()">Clear</button>
        </div>
        <div class="action-status" id="authSettingsFlash"></div>
        <div class="threshold-status" id="authSettingsStatus">Loading access control settings...</div>

        <div class="threshold-list" style="margin-top:14px;">
          <table>
            <thead>
              <tr>
                <th>Username</th>
                <th>Display Name</th>
                <th>Status</th>
                <th>Actions</th>
              </tr>
            </thead>
            <tbody id="authUsersBody"></tbody>
          </table>
        </div>
        <div id="authUsersEmpty" class="threshold-empty">No local users created yet.</div>
        <div class="threshold-summary">Configured local users: <strong id="authUserCount">0</strong></div>
      </section>
    </div>
  </div>

  <div id="about" class="tabpane" style="display:none">
    <div class="hero-title">
      <div>
        <h2>About</h2>
        <div class="hero-sub">Versioning, install details, and the simplest upgrade path for this dashboard.</div>
      </div>
    </div>

    <div class="notify-grid">
      <aside class="threshold-sidebar">
        <section class="threshold-card">
          <h3>{{ product_name }}</h3>
          <div class="threshold-summary">Current version: <strong>{{ app_version }}</strong></div>
        </section>

        <section class="threshold-card">
          <h3>Upgrade</h3>
          <div class="notify-helper">Use the packaged upgrade script to refresh the app code without manually ripping out the existing install.</div>
          <pre class="ops-logtail" style="margin-top:10px;">sudo bash ./upgrade.sh</pre>
          <div class="notify-helper">The upgrade keeps the current config, state, and environment data in place, then restarts the service.</div>
        </section>
      </aside>

      <section class="threshold-card">
        <h3>Runtime Paths</h3>
        <div class="threshold-list">
          <table>
            <thead>
              <tr>
                <th>Item</th>
                <th>Path / Value</th>
              </tr>
            </thead>
            <tbody>
              <tr><td>Version</td><td><code>{{ app_version }}</code></td></tr>
              <tr><td>App Code</td><td><code>{{ project_dir }}</code></td></tr>
              <tr><td>Config File</td><td><code>{{ default_config_file }}</code></td></tr>
              <tr><td>Data Directory</td><td><code>{{ default_data_dir }}</code></td></tr>
              <tr><td>DB CSV Directory</td><td><code>{{ default_db_dir }}</code></td></tr>
              <tr><td>Log Directory</td><td><code>{{ default_log_dir }}</code></td></tr>
              <tr><td>State Directory</td><td><code>{{ default_state_dir }}</code></td></tr>
              <tr><td>Default Port</td><td><code>{{ dashboard_port }}</code></td></tr>
            </tbody>
          </table>
        </div>

        <div class="notify-actions" style="margin-top:14px;">
          <button class="ops-btn" onclick="showTab('auth_settings')">Open Access Control</button>
          <button class="ops-btn" onclick="showTab('admin_env')">Open Portals</button>
        </div>
      </section>
    </div>
  </div>

  <!-- AI SUMMARY -->
  <div id="ai" class="tabpane" style="display:none">
    <div class="legend">
      <span class="dot dcrit"></span>Critical
      <span class="dot dwarn" style="margin-left:14px"></span>Warning
      <span class="dot dok"   style="margin-left:14px"></span>OK
      <span class="dot dmuted" style="margin-left:14px"></span>Muted
    </div>

    <div class="controls">
      <strong>AI Summary</strong>
      <button onclick="runAISummary()" style="margin-left:8px;">Generate / Refresh</button>
      <span id="aiStatus" class="sub" style="margin-left:12px;"></span>
    </div>

    <div class="sub" id="aiTimestamp">Last generated: never</div>

    <div id="aiOutput" class="ai-output">
      <p>Click "Generate / Refresh" to build an AI overview of all tabs (Edge, Portal, Postgres, Servers Health). The summary will stay here until you regenerate it.</p>
    </div>
  </div>

  <!-- TENANTS -->
  <div id="tenants" class="tabpane" style="display:none">
    <div class="legend">
      <span class="dot dcrit"></span>Critical
      <span class="dot dwarn" style="margin-left:14px"></span>Warning
      <span class="dot dok" style="margin-left:14px"></span>OK
      <span class="dot dmuted" style="margin-left:14px"></span>Muted
    </div>
    <div class="controls">
      <strong>Tenants</strong>
      <input id="q_tenants" type="text" placeholder="Search tenants…" oninput="filterTableByInput('tenantsTable','q_tenants')" style="min-width:240px; margin-left:8px">
    </div>
    <div class="sub">File: <code>{{ tenants_src }}</code> &nbsp;•&nbsp; Updated: <span class="sub" data-local-time="{{ tenants_mtime }}">{{ tenants_mtime or '—' }}</span> {% if refresh_seconds|int>0 %}<span>· auto {{ refresh_seconds|int }}s</span>{% endif %}</div>

    <div class="viz-grid tenant-summary">
      <section class="viz-panel">
        <h3>Tenant Summary</h3>
        <div class="headline-metrics">
          <div class="headline-metric">
            <div class="metric-label">Tenants</div>
            <div class="metric-value">{{ tenant_summary.total }}</div>
          </div>
          <div class="headline-metric">
            <div class="metric-label">Active</div>
            <div class="metric-value ok">{{ tenant_summary.active }}</div>
          </div>
          <div class="headline-metric">
            <div class="metric-label">Deleted</div>
            <div class="metric-value warn">{{ tenant_summary.deleted }}</div>
          </div>
        </div>
      </section>
      {% if tenant_type_chart %}
      <section class="viz-panel">
        <h3>Portal Type</h3>
        <div class="bar-list">
          {% for item in tenant_type_chart %}
          <div class="bar-row">
            <span class="bar-label" title="{{ item.label }}">{{ item.label }}</span>
            <span class="bar-track"><span class="bar-fill palette-{{ loop.index0 % 6 }}" style="width:{{ item.pct }}%"></span></span>
            <span class="bar-value">{{ item.value }}</span>
          </div>
          {% endfor %}
        </div>
      </section>
      {% endif %}
    </div>

    <div class="table-wrap">
      <table id="tenantsTable">
        <thead>
          <tr>{% for h in tenants_headers %}<th title="{{ h }}">{{ h }}</th>{% endfor %}</tr>
        </thead>
        <tbody>
          {% for r in tenants_rows %}
          <tr>
            {% for h in tenants_headers %}
              {% set cell = r.get(h, '') %}
              {% set cls = style_tenants(h, cell, r) %}
              <td class="{{ cls }}">
                {% if h.lower() == 'deleted' %}
                  {% set b = (cell|string).lower() in ['true','1','yes','y','on'] %}
                  <span class="pill {{ 'pill-info' if b else 'pill-ok' }}">{{ 'Deleted' if b else 'Active' }}</span>
                {% else %}
                  {{ display_cell(h, cell) }}
                {% endif %}
              </td>
            {% endfor %}
          </tr>
          {% endfor %}
        </tbody>
      </table>
    </div>
  </div>

  <!-- EDGE -->
  <div id="edge" class="tabpane" style="display:none">
    <div class="legend">
      <span class="dot dcrit"></span>Critical
      <span class="dot dwarn" style="margin-left:14px"></span>Warning
      <span class="dot dok" style="margin-left:14px"></span>OK
      <span class="dot dmuted" style="margin-left:14px"></span>Muted
    </div>
    <div class="sub">File: <code>{{ csv_path }}</code> &nbsp;•&nbsp; Updated: <span class="sub" data-local-time="{{ csv_mtime }}">{{ csv_mtime or '—' }}</span> {% if refresh_seconds|int>0 %}<span>· auto {{ refresh_seconds|int }}s</span>{% endif %}</div>

    <div class="viz-grid">
      <section class="viz-panel">
        <h3>CloudSync Status</h3>
        <div class="bar-list">
          {% for item in edge_status_chart %}
          <div class="bar-row">
            <span class="bar-label" title="{{ item.label }}">{{ item.label }}</span>
            <span class="bar-track"><span class="bar-fill {{ item.tone }}" style="width:{{ item.pct }}%"></span></span>
            <span class="bar-value">{{ item.value }}</span>
          </div>
          {% endfor %}
        </div>
      </section>
      <section class="viz-panel">
        <h3>Top Tenants</h3>
        <div class="bar-list">
          {% for item in edge_tenant_chart %}
          <div class="bar-row">
            <span class="bar-label" title="{{ item.label }}">{{ item.label }}</span>
            <span class="bar-track"><span class="bar-fill palette-{{ loop.index0 % 6 }}" style="width:{{ item.pct }}%"></span></span>
            <span class="bar-value">{{ item.value }}</span>
          </div>
          {% endfor %}
        </div>
      </section>
      <section class="viz-panel">
        <h3>Performance Averages</h3>
        <div class="gauge-grid">
          {% for gauge in edge_gauges %}
          <div class="gauge">
            <div class="gauge-name">{{ gauge.label }}</div>
            <div class="gauge-value">{{ gauge.value }}%</div>
            <div class="gauge-track"><div class="gauge-fill {{ gauge.tone }}" style="width:{{ gauge.pct }}%"></div></div>
          </div>
          {% endfor %}
        </div>
      </section>
    </div>

    <div class="controls" style="margin-top:4px; margin-bottom:4px">
      <label for="edgeSeverityFilter" class="sub">Filter:</label>
      <select id="edgeSeverityFilter" onchange="filterEdgeSeverity()" style="min-width:180px; margin-left:6px;">
        <option value="all">All (no filter)</option>
        <option value="crit">Critical only</option>
        <option value="warn">Warning only (no critical)</option>
        <option value="critwarn">Critical + Warning</option>
        <option value="none">No Critical/Warning</option>
      </select>
    </div>

    <!-- top scrollbar synced to the edge table -->
    <div class="hscroll" id="edgeTopScroll"><div class="hscroll-inner" id="edgeTopInner"></div></div>
    <div class="table-wrap scrollshadow edge-table-shell" id="edgeWrap"><table id="edgeTable">
      <thead><tr>{% for h in headers %}<th title="{{ h }}">{{ h }}</th>{% endfor %}</tr></thead>
      <tbody>
        {% for r in rows %}
        <tr>
          {% for h in headers %}
            {% set cell = r.get(h, '') %}
            {% set cls = style_edge(h, cell, r) %}
            {% set sev = warn_edge(h, cell, r) %}
            <td class="{{ cls }} {{ 'sev-critical' if sev == 'bad' else ('sev-warning' if sev == 'warn' else '') }}">
              {% if clip_check(h, cell) %}
                <div class="clipcell" title="{{ cell|replace('\\n',' ') }}">{{ display_cell(h, cell) }}</div>
                <div class="cell-actions">
                  <button class="btn-xs" onclick="openViewer(`{{ cell|replace('`','\\`') }}`)">View</button>
                  <button class="btn-xs" onclick="copyText(`{{ cell|replace('`','\\`') }}`)">Copy</button>
                </div>
              {% else %}
                {{ display_cell(h, cell) }}
              {% endif %}
            </td>
          {% endfor %}
        </tr>
        {% endfor %}
      </tbody>
    </table></div>
  </div>

  <!-- PORTAL -->
  <div id="portal" class="tabpane" style="display:none">
    <div class="legend">
      <span class="dot dcrit"></span>Critical
      <span class="dot dwarn" style="margin-left:14px"></span>Warning
      <span class="dot dok" style="margin-left:14px"></span>OK
      <span class="dot dmuted" style="margin-left:14px"></span>Muted
    </div>

    <div class="section-cards">
      {% for card in portal_section_cards %}
      <div class="section-card">
        <strong>{{ card.label }}</strong>
        <div class="nums">
          <span>{{ card.rows }} rows</span>
          <span>{{ card.bad }} critical</span>
          <span>{{ card.warn }} warning</span>
        </div>
      </div>
      {% endfor %}
    </div>

    <div class="subtabs">
      <button class="portalsubbtn" data-portal-sub="portal_overview" onclick="showPortalTab('portal_overview')">Overview</button>
      <button class="portalsubbtn" data-portal-sub="portal_servers" onclick="showPortalTab('portal_servers')">Servers{% if c_servers.bad %}<span class="badge">{{ c_servers.bad }}</span>{% endif %}</button>
      <button class="portalsubbtn" data-portal-sub="portal_storage" onclick="showPortalTab('portal_storage')">Storage Nodes{% if c_storage.bad %}<span class="badge">{{ c_storage.bad }}</span>{% endif %}</button>
      <button class="portalsubbtn" data-portal-sub="portal_tasks" onclick="showPortalTab('portal_tasks')">Tasks{% if c_tasks.bad %}<span class="badge">{{ c_tasks.bad }}</span>{% endif %}</button>
      <button class="portalsubbtn" data-portal-sub="portal_licenses" onclick="showPortalTab('portal_licenses')">Licenses{% if c_licenses.bad %}<span class="badge">{{ c_licenses.bad }}</span>{% endif %}</button>
    </div>

    <div id="portal_overview" class="portalpane" style="display:none">
      <div class="table-wrap">
        <table>
          <thead><tr><th>Section</th><th>Rows</th><th>Critical</th><th>Warning</th></tr></thead>
          <tbody>
            {% for card in portal_section_cards %}
            <tr>
              <td>{{ card.label }}</td>
              <td>{{ card.rows }}</td>
              <td>{{ card.bad }}</td>
              <td>{{ card.warn }}</td>
            </tr>
            {% endfor %}
          </tbody>
        </table>
      </div>
      <div class="viz-grid two" style="margin-top:12px;">
        <section class="viz-panel">
          <h3>License Summary</h3>
          <div class="headline-metrics">
            <div class="headline-metric"><div class="metric-label">Rows</div><div class="metric-value">{{ licenses_rows|length }}</div></div>
            <div class="headline-metric"><div class="metric-label">Valid</div><div class="metric-value ok">{{ valid_licenses }}</div></div>
            <div class="headline-metric"><div class="metric-label">Expired</div><div class="metric-value {{ 'crit' if expired_licenses else '' }}">{{ expired_licenses }}</div></div>
            <div class="headline-metric"><div class="metric-label">Portal Licenses</div><div class="metric-value">{{ portal_license_rows }}</div></div>
          </div>
        </section>
      </div>
    </div>

    <div id="portal_servers" class="portalpane" style="display:none">
      <div class="controls">
        <strong>Servers</strong>
        <input id="q_servers" type="text" placeholder="Search servers?" oninput="filterTableByInput('serversTable','q_servers')" style="min-width:240px; margin-left:8px">
        <div class="sub">File: <code>{{ portal_servers_src }}</code> &nbsp;?&nbsp; Updated: <span class="sub" data-local-time="{{ portal_servers_mtime }}">{{ portal_servers_mtime or '?' }}</span></div>
      </div>
      <div class="table-wrap" style="margin-bottom:12px">
        <table id="serversTable">
          <thead><tr>{% for h in servers_headers %}<th>{{ h }}</th>{% endfor %}</tr></thead>
          <tbody>
            {% for r in servers_rows %}
            <tr>
              {% for h in servers_headers %}
                {% set cell = r.get(h, '') %}
                {% set cls = style_server_cell(h, cell, r) %}
                {% set sev = warn_server_cell(h, cell, r) %}
                <td class="{{ cls }} {{ 'sev-critical' if sev == 'bad' else ('sev-warning' if sev == 'warn' else '') }}">
                  {% set key = h|string %}
                  {% if key == 'Connected' %}
                    {% set b = (cell|string).lower() in ['true','1','yes','y','on'] %}
                    <span class="pill {{ 'pill-ok' if b else 'pill-bad' }}">{{ 'Connected' if b else 'Disconnected' }}</span>
                  {% elif key == 'IsApplicationServer' and ((cell|string).lower() in ['true','1','yes','y','on']) %}
                    <span class="pill pill-info">App</span>
                  {% elif key == 'IsMainDB' and ((cell|string).lower() in ['true','1','yes','y','on']) %}
                    <span class="pill pill-info">MainDB</span>
                  {% else %}
                    {{ display_cell(h, cell) }}
                  {% endif %}
                </td>
              {% endfor %}
            </tr>
            {% endfor %}
          </tbody>
        </table>
      </div>
    </div>

    <div id="portal_storage" class="portalpane" style="display:none">
      <div class="controls">
        <strong>Storage Nodes</strong>
        <input id="q_storage" type="text" placeholder="Search storage?" oninput="filterTableByInput('storageTable','q_storage')" style="min-width:240px; margin-left:8px">
        <div class="sub">File: <code>{{ portal_storage_src }}</code> &nbsp;?&nbsp; Updated: <span class="sub" data-local-time="{{ portal_storage_mtime }}">{{ portal_storage_mtime or '?' }}</span></div>
      </div>
      <div class="table-wrap">
        <table id="storageTable">
          <thead><tr>{% for h in storage_headers %}<th>{{ display_header(h) }}</th>{% endfor %}</tr></thead>
          <tbody>
            {% for r in storage_rows %}
            <tr>
              {% for h in storage_headers %}
                {% set cell = r.get(h, '') %}
                {% set cls = style_storage_cell(h, cell, r) %}
                {% set sev = warn_storage_cell(h, cell, r) %}
                <td class="{{ cls }} {{ 'sev-critical' if sev == 'bad' else ('sev-warning' if sev == 'warn' else '') }}">{{ display_cell(h, cell) }}</td>
              {% endfor %}
            </tr>
            {% endfor %}
          </tbody>
        </table>
      </div>
    </div>

    <div id="portal_tasks" class="portalpane" style="display:none">
      <div class="controls" style="margin-top:12px">
        <strong>Tasks</strong>
        <input id="q_tasks" type="text" placeholder="Search tasks..." oninput="filterTableByInput('tasksTable','q_tasks')" style="min-width:240px; margin-left:8px">
        <div class="sub">File: <code>{{ portal_tasks_src }}</code> &nbsp;?&nbsp; Updated: <span class="sub" data-local-time="{{ portal_tasks_mtime }}">{{ portal_tasks_mtime or '?' }}</span></div>
      </div>
      <div class="table-wrap">
        <table id="tasksTable">
          <thead><tr>{% for h in tasks_headers %}<th>{{ display_header(h) }}</th>{% endfor %}</tr></thead>
          <tbody>
            {% for r in tasks_rows %}
            <tr>
              {% for h in tasks_headers %}
                {% set cell = r.get(h, '') %}
                {% set cls = style_tasks_cell(h, cell, r) %}
                {% set sev = warn_task_cell(h, cell, r) %}
                <td class="{{ cls }} {{ 'sev-critical' if sev == 'bad' else ('sev-warning' if sev == 'warn' else '') }}">{{ display_cell(h, cell) }}</td>
              {% endfor %}
            </tr>
            {% endfor %}
          </tbody>
        </table>
      </div>
    </div>

    <div id="portal_licenses" class="portalpane" style="display:none">
      <div class="controls">
        <strong>Licenses</strong>
        <input id="q_licenses" type="text" placeholder="Search licenses?" oninput="filterTableByInput('licensesTable','q_licenses')" style="min-width:240px; margin-left:8px">
        <div class="sub">File: <code>{{ portal_licenses_src }}</code> &nbsp;?&nbsp; Updated: <span class="sub" data-local-time="{{ portal_licenses_mtime }}">{{ portal_licenses_mtime or '?' }}</span></div>
      </div>
      <div class="viz-grid two" style="margin-bottom:12px;">
        <section class="viz-panel">
          <h3>License Counts</h3>
          <div class="headline-metrics">
            <div class="headline-metric"><div class="metric-label">Rows</div><div class="metric-value">{{ licenses_rows|length }}</div></div>
            <div class="headline-metric"><div class="metric-label">Valid</div><div class="metric-value ok">{{ valid_licenses }}</div></div>
            <div class="headline-metric"><div class="metric-label">Expired</div><div class="metric-value {{ 'crit' if expired_licenses else '' }}">{{ expired_licenses }}</div></div>
            <div class="headline-metric"><div class="metric-label">Portal Licenses</div><div class="metric-value">{{ portal_license_rows }}</div></div>
          </div>
        </section>
      </div>
      <div class="table-wrap">
        <table id="licensesTable">
          <thead><tr>{% for h in licenses_display_headers %}<th>{{ display_header(h) }}</th>{% endfor %}</tr></thead>
          <tbody>
            {% for r in licenses_rows %}
            <tr>
              {% for h in licenses_display_headers %}
                {% set cell = r.get(h, '') %}
                {% set lower = (cell|string).lower() %}
                {% set is_expired = h == 'expired' and lower in ['true','1','yes','y','on'] %}
                {% set is_invalid = h == 'valid' and lower in ['false','0','no','n','off'] %}
                <td class="{{ 'sev-critical' if is_expired or is_invalid else '' }}">
                  {% if h in ['expired','valid','portal_license','antivirus','varonis','key_manager','dlp','global_file_lock'] and lower in ['true','false','1','0','yes','no','y','n','on','off'] %}
                    {% set b = lower in ['true','1','yes','y','on'] %}
                    <span class="pill {{ 'pill-ok' if b else 'pill-muted' }}">{{ 'Yes' if b else 'No' }}</span>
                  {% else %}
                    {{ display_cell(h, cell) }}
                  {% endif %}
                </td>
              {% endfor %}
            </tr>
            {% endfor %}
          </tbody>
        </table>
      </div>
    </div>
  </div>
  <!-- POSTGRES (with sub-tabs) -->
  <div id="pg" class="tabpane" style="display:none">
    <div class="legend">
      <span class="dot dcrit"></span>Critical
      <span class="dot dwarn" style="margin-left:14px"></span>Warning
      <span class="dot dok" style="margin-left:14px"></span>OK
      <span class="dot dmuted" style="margin-left:14px"></span>Muted
    </div>
    <div class="sub">Dir: <code>{{ pg_base_dir }}</code> {% if refresh_seconds|int>0 %}· auto {{ refresh_seconds|int }}s{% endif %}</div>

    <div class="viz-grid two">
      <section class="viz-panel">
        <h3>Postgres Impact By Topic</h3>
        <div class="bar-list">
          {% for item in pg_topic_chart %}
          <div class="bar-row">
            <span class="bar-label" title="{{ item.label }}">{{ item.label }}</span>
            <span class="bar-track"><span class="bar-fill {{ item.tone }}" style="width:{{ item.pct }}%"></span></span>
            <span class="bar-value">{{ item.value }}</span>
          </div>
          {% endfor %}
        </div>
      </section>
      <section class="viz-panel">
        <h3>Postgres Severity</h3>
        <div class="headline-metrics">
          <div class="headline-metric"><div class="metric-label">Topics</div><div class="metric-value">{{ pg_views|length }}</div></div>
          <div class="headline-metric"><div class="metric-label">Critical</div><div class="metric-value crit">{{ pg_counts.bad }}</div></div>
          <div class="headline-metric"><div class="metric-label">Warning</div><div class="metric-value warn">{{ pg_counts.warn }}</div></div>
        </div>
      </section>
    </div>

    <div class="subtabs">
      <button class="subbtn" data-sub="pg_overview" onclick="showPgTab('pg_overview')">Overview</button>
      {% for v in pg_views %}
        <button class="subbtn" data-sub="pg_{{ v.key }}" onclick="showPgTab('pg_{{ v.key }}')">
          {{ v.title }}
          {% if v.bad_rows_count and v.bad_rows_count > 0 %}
            <span class="badge" title="{{ v.bad_cells_count }} bad cells">{{ v.bad_rows_count }}</span>
          {% endif %}
        </button>
      {% endfor %}
    </div>

    <!-- overview pane -->
    <div id="pg_overview" class="pgpane" style="display:none">
      <div class="table-wrap">
        <table>
          <thead><tr><th>Topic</th><th>Rows</th><th>Critical Rows</th><th>Warning Rows</th><th>Warned Cells</th></tr></thead>
          <tbody>
            {% for v in pg_views %}
            <tr onclick="showPgTab('pg_{{ v.key }}')" style="cursor:pointer">
              <td>{{ v.title }}</td>
              <td>{{ v.rows|length }}</td>
              <td>{{ v.bad_rows_count }}</td>
              <td>{{ v.warn_rows_count }}</td>
              <td>{{ v.bad_cells_count }}</td>
            </tr>
            {% endfor %}
          </tbody>
        </table>
      </div>
      <div class="sub">Click a row to jump into that topic.</div>
    </div>

    <!-- one pane per topic -->
    {% for v in pg_views %}
      <div id="pg_{{ v.key }}" class="pgpane" style="display:none">
        <div class="controls">
          <input id="q_pg_{{ v.key }}" type="text" placeholder="Search {{ v.title }}…" oninput="filterTableByInput('pgTable_{{ v.key }}','q_pg_{{ v.key }}')" style="min-width:280px">
          <label for="pgSeverityFilter_{{ v.key }}" class="sub" style="margin-left:8px;">Filter:</label>
          <select id="pgSeverityFilter_{{ v.key }}" onchange="filterPgSeverity('{{ v.key }}')" style="min-width:180px; margin-left:4px;">
            <option value="all">All (no filter)</option>
            <option value="crit">Critical only</option>
            <option value="warn">Warning only (no critical)</option>
            <option value="critwarn">Critical + Warning</option>
            <option value="none">No Critical/Warning</option>
          </select>
        </div>
        <div class="table-wrap">
          <table id="pgTable_{{ v.key }}">
            <thead><tr>{% for h in v.headers %}<th>{{ display_header(h) }}</th>{% endfor %}</tr></thead>
            <tbody>
              {% for r in v.rows %}
              <tr>
                {% for h in v.headers %}
                  {% set cell = r.get(h, '') %}
                  {% set cls = style_pg(v.key, h, cell, r) %}
                  {% set sev = warn_pg(v.key, h, cell, r) %}
                  <td class="{{ cls }} {{ 'sev-critical' if sev == 'bad' else ('sev-warning' if sev == 'warn' else '') }}">{{ display_cell(h, cell) }}</td>
                {% endfor %}
              </tr>
              {% endfor %}
            </tbody>
          </table>
        </div>
      </div>
    {% endfor %}
  </div>

  <!-- SERVERS HEALTH -->
  <div id="svrhlth" class="tabpane" style="display:none">
    <div class="legend">
      <span class="dot dcrit"></span>Critical
      <span class="dot dwarn" style="margin-left:14px"></span>Warning
      <span class="dot dok" style="margin-left:14px"></span>OK
      <span class="dot dmuted" style="margin-left:14px"></span>Muted
    </div>
    <div class="subtabs">
      <button class="healthsubbtn" data-health-sub="health_overview" onclick="showHealthTab('health_overview')">Overview</button>
      <button class="healthsubbtn" data-health-sub="health_nomad" onclick="showHealthTab('health_nomad')">Nomad{% if nomad_summary.tone != 'ok' %}<span class="badge">{{ nomad_summary.sources_checked }}</span>{% endif %}</button>
      <button class="healthsubbtn" data-health-sub="health_consul" onclick="showHealthTab('health_consul')">Consul{% if consul_summary.tone != 'ok' %}<span class="badge">{{ consul_summary.sources_checked }}</span>{% endif %}</button>
    </div>

    <div id="health_overview" class="healthpane" style="display:none">
      <div class="sub">File: <code>{{ metrics_csv }}</code> &nbsp;?&nbsp; Updated: <span class="sub" data-local-time="{{ metrics_mtime }}">{{ metrics_mtime or '?' }}</span> {% if refresh_seconds|int>0 %}? auto {{ refresh_seconds|int }}s{% endif %}</div>
      <div class="viz-grid two">
        <section class="viz-panel">
          <h3>Average Utilization</h3>
          <div class="gauge-grid">
            {% for gauge in host_gauges %}
            <div class="gauge">
              <div class="gauge-name">{{ gauge.label }}</div>
              <div class="gauge-value">{{ gauge.value }}%</div>
              <div class="gauge-track"><div class="gauge-fill {{ gauge.tone }}" style="width:{{ gauge.pct }}%"></div></div>
            </div>
            {% endfor %}
          </div>
        </section>
        <section class="viz-panel">
          <h3>Host Status</h3>
          <div class="bar-list">
            {% for item in host_status_chart %}
            <div class="bar-row">
              <span class="bar-label">{{ item.label }}</span>
              <span class="bar-track"><span class="bar-fill {{ item.tone }}" style="width:{{ item.pct }}%"></span></span>
              <span class="bar-value">{{ item.value }}</span>
            </div>
            {% endfor %}
          </div>
        </section>
      </div>
      <div class="controls">
        <input id="q_hosts" type="text" placeholder="Search hosts?" oninput="filterTableByInput('hostsTable','q_hosts')" style="min-width:280px">
      </div>
      <div class="table-wrap">
        <table id="hostsTable">
          <thead><tr>{% for h in hosts_headers %}<th>{{ display_header(h) }}</th>{% endfor %}</tr></thead>
          <tbody>
            {% for r in hosts_rows %}
            <tr>
              {% for h in hosts_headers %}
                {% set cell = r.get(h, '') %}
                {% set cls = style_hosts(h, cell, r) %}
                {% set sev = warn_hosts(h, cell, r) %}
                <td class="{{ cls }} {{ 'sev-critical' if sev == 'bad' else ('sev-warning' if sev == 'warn' else '') }}">{{ display_cell(h, cell) }}</td>
              {% endfor %}
            </tr>
            {% endfor %}
          </tbody>
        </table>
      </div>
      <div class="sub">Notes: <code>Load1/5/15</code> are the same "load average" you see in <code>top</code>. Disk columns ending with <code>UsedPct</code> are percentage used.</div>
    </div>

    <div id="health_nomad" class="healthpane" style="display:none">
      <div class="sub">File: <code>{{ nomad_csv }}</code> &nbsp;?&nbsp; Updated: <span class="sub" data-local-time="{{ nomad_mtime }}">{{ nomad_mtime or '?' }}</span></div>
      <div class="viz-grid two">
        <section class="viz-panel">
          <h3>Nomad Consistency</h3>
          <div class="headline-metrics">
            <div class="headline-metric"><div class="metric-label">Status</div><div class="metric-value {{ nomad_summary.tone }}">{{ nomad_summary.label }}</div></div>
            <div class="headline-metric"><div class="metric-label">Sources</div><div class="metric-value">{{ nomad_summary.sources_checked }}</div></div>
            <div class="headline-metric"><div class="metric-label">Distinct Views</div><div class="metric-value">{{ nomad_summary.distinct_views }}</div></div>
            <div class="headline-metric"><div class="metric-label">Nodes In View</div><div class="metric-value">{{ nomad_summary.reference_members }}</div></div>
          </div>
          <div class="sub">All servers should report the same Nomad view. More than one view hash means the cluster picture is inconsistent.</div>
        </section>
        <section class="viz-panel">
          <h3>Nomad Node Status</h3>
          <div class="bar-list">
            {% for item in nomad_status_chart %}
            <div class="bar-row">
              <span class="bar-label">{{ item.label }}</span>
              <span class="bar-track"><span class="bar-fill {{ item.tone }}" style="width:{{ item.pct }}%"></span></span>
              <span class="bar-value">{{ item.value }}</span>
            </div>
            {% endfor %}
          </div>
        </section>
      </div>
      <div class="table-wrap" style="margin-bottom:12px">
        <table>
          <thead><tr><th>Source</th><th>Host</th><th>State</th><th>View Hash</th><th>Members Seen</th><th>Notes</th></tr></thead>
          <tbody>
            {% for row in nomad_summary.source_views %}
            <tr>
              <td>{{ row.source_name }}</td>
              <td>{{ row.source_host }}</td>
              <td class="{{ 'sev-critical' if row.state == 'error' else ('sev-warning' if row.state == 'mismatch' else 'sev-ok') }}">{{ row.state|title }}</td>
              <td><code>{{ row.view_hash or '?' }}</code></td>
              <td>{{ row.members_seen }}</td>
              <td>{{ row.errors or 'Matches reference view' }}</td>
            </tr>
            {% endfor %}
          </tbody>
        </table>
      </div>
      <div class="controls">
        <input id="q_nomad" type="text" placeholder="Search Nomad rows?" oninput="filterTableByInput('nomadTable','q_nomad')" style="min-width:280px">
      </div>
      <div class="table-wrap">
        <table id="nomadTable">
          <thead><tr>{% for h in nomad_headers %}<th>{{ display_header(h) }}</th>{% endfor %}</tr></thead>
          <tbody>
            {% for r in nomad_rows %}
            <tr>
              {% for h in nomad_headers %}
                {% set cell = r.get(h, '') %}
                {% set is_error = h == 'CollectionError' and cell %}
                {% set is_status_bad = h == 'Status' and (cell|string|lower) not in ['ready', ''] %}
                {% set is_view_warn = h == 'ViewHash' and nomad_summary.distinct_views > 1 %}
                <td class="{{ 'sev-critical' if is_error else ('sev-warning' if is_status_bad or is_view_warn else '') }}">{{ display_cell(h, cell) }}</td>
              {% endfor %}
            </tr>
            {% endfor %}
          </tbody>
        </table>
      </div>
    </div>

    <div id="health_consul" class="healthpane" style="display:none">
      <div class="sub">File: <code>{{ consul_csv }}</code> &nbsp;?&nbsp; Updated: <span class="sub" data-local-time="{{ consul_mtime }}">{{ consul_mtime or '?' }}</span></div>
      <div class="viz-grid two">
        <section class="viz-panel">
          <h3>Consul Consistency</h3>
          <div class="headline-metrics">
            <div class="headline-metric"><div class="metric-label">Status</div><div class="metric-value {{ consul_summary.tone }}">{{ consul_summary.label }}</div></div>
            <div class="headline-metric"><div class="metric-label">Sources</div><div class="metric-value">{{ consul_summary.sources_checked }}</div></div>
            <div class="headline-metric"><div class="metric-label">Distinct Views</div><div class="metric-value">{{ consul_summary.distinct_views }}</div></div>
            <div class="headline-metric"><div class="metric-label">Members In View</div><div class="metric-value">{{ consul_summary.reference_members }}</div></div>
          </div>
          <div class="sub">All servers should report the same Consul membership view. Any mismatch here is worth treating as a cluster communication problem.</div>
        </section>
        <section class="viz-panel">
          <h3>Consul Member Status</h3>
          <div class="bar-list">
            {% for item in consul_status_chart %}
            <div class="bar-row">
              <span class="bar-label">{{ item.label }}</span>
              <span class="bar-track"><span class="bar-fill {{ item.tone }}" style="width:{{ item.pct }}%"></span></span>
              <span class="bar-value">{{ item.value }}</span>
            </div>
            {% endfor %}
          </div>
        </section>
      </div>
      <div class="table-wrap" style="margin-bottom:12px">
        <table>
          <thead><tr><th>Source</th><th>Host</th><th>State</th><th>View Hash</th><th>Members Seen</th><th>Notes</th></tr></thead>
          <tbody>
            {% for row in consul_summary.source_views %}
            <tr>
              <td>{{ row.source_name }}</td>
              <td>{{ row.source_host }}</td>
              <td class="{{ 'sev-critical' if row.state == 'error' else ('sev-warning' if row.state == 'mismatch' else 'sev-ok') }}">{{ row.state|title }}</td>
              <td><code>{{ row.view_hash or '?' }}</code></td>
              <td>{{ row.members_seen }}</td>
              <td>{{ row.errors or 'Matches reference view' }}</td>
            </tr>
            {% endfor %}
          </tbody>
        </table>
      </div>
      <div class="controls">
        <input id="q_consul" type="text" placeholder="Search Consul rows?" oninput="filterTableByInput('consulTable','q_consul')" style="min-width:280px">
      </div>
      <div class="table-wrap">
        <table id="consulTable">
          <thead><tr>{% for h in consul_headers %}<th>{{ display_header(h) }}</th>{% endfor %}</tr></thead>
          <tbody>
            {% for r in consul_rows %}
            <tr>
              {% for h in consul_headers %}
                {% set cell = r.get(h, '') %}
                {% set is_error = h == 'CollectionError' and cell %}
                {% set is_status_bad = h == 'Status' and (cell|string|lower) not in ['alive', ''] %}
                {% set is_view_warn = h == 'ViewHash' and consul_summary.distinct_views > 1 %}
                <td class="{{ 'sev-critical' if is_error else ('sev-warning' if is_status_bad or is_view_warn else '') }}">{{ display_cell(h, cell) }}</td>
              {% endfor %}
            </tr>
            {% endfor %}
          </tbody>
        </table>
      </div>
    </div>
  </div>
  </div>
      </div>
    </main>
  </div>

  <div id="viewerBackdrop" class="viewer-backdrop" onclick="if(event.target===this) closeViewer()">
    <div class="viewer">
      <header>
        <h3>Cell contents</h3>
        <div>
          <button class="btn-xs" onclick="copyText(document.getElementById('viewerPre').textContent)">Copy</button>
          <button class="btn-xs" onclick="closeViewer()">Close</button>
        </div>
      </header>
      <pre id="viewerPre"></pre>
    </div>
  </div>
  <div id="environmentModalBackdrop" class="modal-backdrop" onclick="if(event.target===this) closeEnvironmentModal()">
    <div class="modal-panel">
      <div class="modal-head">
        <div>
          <div id="environmentEditorCrumb" class="portal-crumb">Portals <span>› New Portal Environment</span></div>
          <h3 id="environmentEditorTitle">Add Portal Environment</h3>
          <div class="modal-sub">Use the same core details as the original install flow. The initial SSH access mode is only for the first bootstrap step so the dashboard can install the SSH key and retrieve what it needs from MainDB, then use the installed key going forward.</div>
        </div>
        <button class="modal-close" type="button" aria-label="Close" onclick="closeEnvironmentModal()">x</button>
      </div>
      <div class="modal-body">
        <div class="portal-form-shell">
        <h4 class="portal-form-title">Details</h4>
        <div class="threshold-form-grid">
          <div class="threshold-field">
            <label for="environmentName">Environment Name</label>
            <input id="environmentName" class="threshold-input" type="text" placeholder="Production Portal">
          </div>
          <div class="threshold-field">
            <label for="envPortalFqdn">Portal FQDN</label>
            <input id="envPortalFqdn" class="threshold-input" type="text" placeholder="files.example.com">
          </div>
          <div class="threshold-field">
            <label for="envCteraUsername">CTERA Read-Only Username</label>
            <input id="envCteraUsername" class="threshold-input" type="text" placeholder="monitoring">
          </div>
          <div class="threshold-field">
            <label for="envCteraPassword">CTERA Password</label>
            <input id="envCteraPassword" class="threshold-input" type="password" placeholder="Leave blank to keep saved password">
            <div id="envCteraPasswordHint" class="env-secret-hint">Leave blank if you are not setting the secret yet.</div>
          </div>
          <div class="threshold-field" style="grid-column:1 / -1;">
            <label class="notify-checkbox" for="envUseJumpHost">
              <input id="envUseJumpHost" type="checkbox" onchange="renderJumpSshFields()">
              Use jump host to reach MainDB
            </label>
          </div>
          <div class="threshold-field" id="envMainDbViaJumpConfiguredWrap" style="display:none; grid-column:1 / -1;">
            <label class="notify-checkbox" for="envMainDbViaJumpConfigured">
              <input id="envMainDbViaJumpConfigured" type="checkbox" onchange="renderInitialSshFields()">
              MainDB access from jump host is already configured
            </label>
            <div id="envMainDbViaJumpConfiguredHint" class="env-secret-hint">Use this when the jump host can already SSH into MainDB using its own existing trust or SSH setup.</div>
          </div>
          <div id="envJumpHostSection" style="display:none; grid-column:1 / -1;">
            <div class="threshold-form-grid">
              <div class="threshold-field">
                <label for="envJumpHost">Jump Host IP / FQDN</label>
                <input id="envJumpHost" class="threshold-input" type="text" placeholder="10.10.10.10">
              </div>
              <div class="threshold-field">
                <label for="envJumpSshMode">Jump Host SSH Access Mode</label>
                <select id="envJumpSshMode" class="threshold-select" onchange="renderJumpSshFields()">
                  <option value="root_password">Root username and password</option>
                  <option value="user_password">Other username and password</option>
                  <option value="root_key">Root private key</option>
                  <option value="user_key">Other username and private key</option>
                </select>
                <div id="envJumpSshHelp" class="env-secret-hint">Optional. Use a jump host if this monitoring server cannot reach MainDB directly.</div>
              </div>
              <div class="threshold-field">
                <label for="envJumpSshUsername">Jump Host SSH Username</label>
                <input id="envJumpSshUsername" class="threshold-input" type="text" placeholder="root" value="root">
              </div>
              <div class="threshold-field" id="envJumpSshPasswordWrap">
                <label for="envJumpSshPassword">Jump Host SSH Password</label>
                <input id="envJumpSshPassword" class="threshold-input" type="password" placeholder="Enter jump-host SSH password">
                <div id="envJumpSshPasswordHint" class="env-secret-hint">Enter the jump-host SSH password only if this mode uses username and password.</div>
              </div>
              <div class="threshold-field" id="envJumpSshKeyWrap" style="display:none;">
                <label for="envJumpSshKey">Upload Jump Host Private Key</label>
                <input id="envJumpSshKey" class="threshold-input" type="file" accept=".pem,.ppk,.key,.txt,*/*">
                <div id="envJumpSshKeyHint" class="env-secret-hint">Upload the jump-host private key only if this mode uses private key bootstrap.</div>
              </div>
            </div>
          </div>
          <div class="threshold-field">
            <label for="envMainDbIp">MainDB IP</label>
            <input id="envMainDbIp" class="threshold-input" type="text" placeholder="10.10.10.20">
          </div>
          <div class="threshold-field" id="envMainDbJumpUsernameWrap" style="display:none;">
            <label for="envMainDbJumpUsername">MainDB SSH Username From Jump Host</label>
            <input id="envMainDbJumpUsername" class="threshold-input" type="text" placeholder="ctera" value="">
            <div id="envMainDbJumpUsernameHint" class="env-secret-hint">This is the SSH user the jump host will use when it connects onward to MainDB.</div>
          </div>
          <div id="envInitialSshSection" class="threshold-form-grid" style="grid-column:1 / -1; margin-top:0;">
            <div class="threshold-field">
              <label for="envInitialSshMode">Initial SSH Access Mode</label>
              <select id="envInitialSshMode" class="threshold-select" onchange="renderInitialSshFields()">
                <option value="root_password">Root username and password</option>
                <option value="user_password_sudo">Other username and password with sudo</option>
                <option value="root_key">Root private key</option>
                <option value="user_key_sudo">Other username with private key and sudo</option>
              </select>
              <div id="envInitialSshHelp" class="env-secret-hint">Used one time for bootstrap. After that the dashboard uses the installed SSH key going forward.</div>
            </div>
            <div class="threshold-field">
              <label for="envInitialSshUsername">Initial SSH Username</label>
              <input id="envInitialSshUsername" class="threshold-input" type="text" placeholder="root" value="root">
            </div>
            <div class="threshold-field" id="envInitialSshPasswordWrap">
              <label for="envInitialSshPassword">Initial SSH Password</label>
              <input id="envInitialSshPassword" class="threshold-input" type="password" placeholder="Enter bootstrap SSH password">
              <div id="envInitialSshPasswordHint" class="env-secret-hint">Enter the bootstrap SSH password only if this mode uses username and password.</div>
            </div>
            <div class="threshold-field" id="envInitialSshKeyWrap" style="display:none;">
              <label for="envInitialSshKey">Upload Initial SSH Private Key</label>
              <input id="envInitialSshKey" class="threshold-input" type="file" accept=".pem,.ppk,.key,.txt,*/*">
              <div id="envInitialSshKeyHint" class="env-secret-hint">Upload the initial private key only if this mode uses private key bootstrap.</div>
            </div>
          </div>
          <div class="threshold-field" style="display:none;">
            <label for="envOpenAiKey">OpenAI API Key</label>
            <input id="envOpenAiKey" class="threshold-input" type="password" placeholder="Optional">
            <div id="envOpenAiKeyHint" class="env-secret-hint">Optional. Only needed if this environment uses AI Summary.</div>
          </div>
          <div class="threshold-field">
            <label for="envPortalSchedule">Portal / MainDB Collectors (minutes)</label>
            <input id="envPortalSchedule" class="threshold-input" type="number" min="1" step="1" placeholder="60">
          </div>
          <div class="threshold-field">
            <label for="envFilerSchedule">Edge Filer Collectors (minutes)</label>
            <input id="envFilerSchedule" class="threshold-input" type="number" min="1" step="1" placeholder="60">
          </div>
          <div class="threshold-field" style="grid-column:1 / -1;">
            <label class="notify-checkbox" for="envEnabled">
              <input id="envEnabled" type="checkbox" checked>
              Enabled environment
            </label>
          </div>
        </div>
        </div>
        <div class="notify-actions" id="environmentActions" style="margin-top:16px;">
          <button class="ops-btn primary" onclick="saveEnvironment(true)">Save and Run Bootstrap</button>
          <button class="ops-btn" onclick="saveEnvironment(false)">Save Only</button>
          <button class="ops-btn" onclick="clearEnvironmentForm()">Clear</button>
          <button class="ops-btn" onclick="closeEnvironmentModal()">Cancel</button>
        </div>
        <div class="action-status" id="environmentFlash"></div>
      </div>
    </div>
  </div>
</body>
</html>
"""

LOGIN_HTML = """
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>CTERA Monitoring Dashboard Login</title>
  {% if login_icon %}<link rel="icon" type="image/png" href="{{ login_icon }}">{% endif %}
  <style>
    :root {
      --bg:#eef2f7;
      --card:#ffffff;
      --text:#20384f;
      --muted:#5b7286;
      --border:#d7dee8;
      --accent:#5860ea;
      --accent-strong:#3f48d8;
      --danger:#c62828;
    }
    * { box-sizing:border-box; }
    body {
      margin:0;
      min-height:100vh;
      font-family:"Open Sans","Segoe UI",Arial,sans-serif;
      background:var(--bg);
      color:var(--text);
      display:flex;
      align-items:center;
      justify-content:center;
      padding:24px;
    }
    .login-shell {
      width:min(440px, 100%);
      background:var(--card);
      border:1px solid var(--border);
      border-radius:12px;
      box-shadow:0 16px 42px rgba(15, 23, 42, 0.10);
      overflow:hidden;
    }
    .login-head {
      padding:22px 24px 18px;
      border-bottom:1px solid var(--border);
      background:#f8fafc;
    }
    .login-head h1 {
      margin:0 0 8px;
      font-size:28px;
      line-height:1.2;
      color:var(--accent);
    }
    .login-head p {
      margin:0;
      color:var(--muted);
      font-size:14px;
      line-height:1.6;
    }
    .login-body { padding:24px; display:grid; gap:14px; }
    label {
      display:grid;
      gap:6px;
      color:var(--muted);
      font-size:12px;
      font-weight:700;
      text-transform:uppercase;
    }
    input {
      width:100%;
      min-height:42px;
      border:1px solid var(--border);
      border-radius:8px;
      padding:10px 12px;
      font:inherit;
      color:var(--text);
      background:#fff;
    }
    input:focus {
      outline:none;
      border-color:var(--accent);
      box-shadow:0 0 0 2px rgba(88, 96, 234, 0.18);
    }
    .error {
      padding:10px 12px;
      border-radius:8px;
      border:1px solid #f5c2c7;
      background:#fff5f5;
      color:var(--danger);
      font-size:13px;
      font-weight:600;
    }
    button {
      min-height:44px;
      border:none;
      border-radius:8px;
      background:linear-gradient(135deg, var(--accent), var(--accent-strong));
      color:#fff;
      font:inherit;
      font-weight:700;
      cursor:pointer;
    }
    .hint {
      color:var(--muted);
      font-size:13px;
      line-height:1.6;
    }
  </style>
</head>
<body>
  <div class="login-shell">
    <div class="login-head">
      <h1>{{ product_name }}</h1>
      <p>Sign in with a local dashboard account to continue.</p>
    </div>
    <form class="login-body" method="post" action="/login">
      {% if error %}
      <div class="error">{{ error }}</div>
      {% endif %}
      <input type="hidden" name="next" value="{{ next_url }}">
      <label>
        Username
        <input type="text" name="username" autocomplete="username" required>
      </label>
      <label>
        Password
        <input type="password" name="password" autocomplete="current-password" required>
      </label>
      <button type="submit">Sign In</button>
      <div class="hint">Use the <strong>Username</strong> created under Access Control, not the Display Name.</div>
      <div class="hint">If you expected open access, switch Access Control back to <strong>No login required</strong> from an existing admin session.</div>
      <div class="hint">Version {{ app_version }}</div>
    </form>
  </div>
</body>
</html>
"""

# ---------------- app ----------------
app = Flask(__name__)


def _session_secret():
    env_secret = str(os.environ.get("FEATHERDASH_SECRET_KEY") or "").strip()
    if env_secret:
        return env_secret
    secret_path = os.path.join(_state_dir(), "session_secret.txt")
    if os.path.exists(secret_path):
        with open(secret_path, "r", encoding="utf-8") as handle:
            secret = handle.read().strip()
            if secret:
                return secret
    secret = base64.urlsafe_b64encode(os.urandom(48)).decode("ascii")
    with open(secret_path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(secret + "\n")
    try:
        os.chmod(secret_path, 0o600)
    except Exception:
        pass
    return secret


app.secret_key = _session_secret()
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"


def _auth_mode():
    return str(_load_app_settings().get("auth_mode") or "none").strip().lower() or "none"


def _login_exempt():
    return request.endpoint in {"healthz", "login", "login_post", "logout", "static"}


def _is_api_request():
    path = request.path or "/"
    return path != "/" and not path.startswith("/login")


@app.before_request
def _enforce_local_auth():
    if _auth_mode() != "local":
        return None
    if _login_exempt():
        return None
    if session.get("local_user_id"):
        return None
    if _is_api_request():
        return jsonify({"ok": False, "error": "Authentication required."}), 401
    next_url = request.full_path if request.query_string else request.path
    return redirect(url_for("login", next=next_url))


@app.get("/healthz")
def healthz():
    return jsonify(status="ok")


@app.get("/login")
def login():
    if _auth_mode() != "local":
        return redirect(url_for("index"))
    if session.get("local_user_id"):
        return redirect(request.args.get("next") or url_for("index"))
    brand = resolve_brand(load_conf())
    return render_template_string(
        LOGIN_HTML,
        error="",
        next_url=request.args.get("next") or "/",
        product_name=PRODUCT_NAME,
        app_version=APP_VERSION,
        login_icon=brand.get("icon"),
    )


@app.post("/login")
def login_post():
    if _auth_mode() != "local":
        return redirect(url_for("index"))
    username = str(request.form.get("username") or "").strip()
    password = str(request.form.get("password") or "")
    next_url = str(request.form.get("next") or "/").strip() or "/"
    brand = resolve_brand(load_conf())
    with _notifications_conn() as conn:
        row = conn.execute(
            "SELECT id, username, password_hash, enabled FROM local_users WHERE lower(username) = lower(?)",
            (username,),
        ).fetchone()
    if not row or not bool(row["enabled"]) or not check_password_hash(row["password_hash"], password):
        return render_template_string(
            LOGIN_HTML,
            error="Username or password is incorrect.",
            next_url=next_url,
            product_name=PRODUCT_NAME,
            app_version=APP_VERSION,
            login_icon=brand.get("icon"),
        ), 401
    session.clear()
    session["local_user_id"] = int(row["id"])
    session["local_username"] = row["username"]
    return redirect(next_url if next_url.startswith("/") else "/")


@app.get("/logout")
def logout():
    session.clear()
    return redirect(url_for("login") if _auth_mode() == "local" else url_for("index"))


@app.get("/job_status")
def job_status():
    return jsonify({name: _job_status(name) for name in JOB_NAMES})


@app.post("/run_job/<job_name>")
def run_job(job_name):
    try:
        payload = request.get_json(silent=True) or {}
        status, started = _launch_job(job_name, payload.get("environment_id"))
        return jsonify({"ok": True, "started": started, "job": status})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400


@app.get("/thresholds_catalog")
def thresholds_catalog():
    env_id = _request_environment_id()
    cfg = load_conf_for_environment(env_id)
    return jsonify(_build_threshold_catalog(cfg, env_id=env_id))


@app.post("/thresholds_save")
def thresholds_save():
    env_id = _request_environment_id()
    cfg = load_conf_for_environment(env_id)
    payload = request.get_json(force=True, silent=True) or {}
    dataset_key = str(payload.get("dataset") or "").strip()
    field = str(payload.get("field") or "").strip()
    if not dataset_key or not field:
        return jsonify({"ok": False, "error": "Dataset and field are required."}), 400

    doc, _ = _load_external_thresholds(cfg)
    rules = _dataset_rule_container(doc, dataset_key, create=True)

    def build_rule(prefix):
        op = str(payload.get(f"{prefix}_op") or "").strip().lower()
        value = payload.get(f"{prefix}_value")
        if not op:
            return None
        if op not in {"gt", "ge", "lt", "le", "eq", "ne"}:
            raise ValueError(f"Unsupported operator: {op}")
        coerced = _coerce_threshold_value(value)
        if coerced == "":
            raise ValueError(f"{prefix.title()} value is required when an operator is selected.")
        return {op: coerced}

    try:
        warn_rule = build_rule("warn")
        crit_rule = build_rule("crit")
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400

    new_rule = {}
    if warn_rule:
        new_rule["warn"] = warn_rule
    if crit_rule:
        new_rule["crit"] = crit_rule

    notify_enabled = _bool_setting(payload.get("notify_enabled"), False)
    if notify_enabled and not new_rule:
        return jsonify({"ok": False, "error": "Save a warning or critical threshold before enabling email notifications."}), 400

    if new_rule:
        rules[field] = new_rule
        notify_config = _save_threshold_notification(dataset_key, field, payload)
    else:
        rules.pop(field, None)
        _prune_empty_threshold_sections(doc, dataset_key)
        notify_config = _save_threshold_notification(dataset_key, field, {"notify_enabled": False})

    th_path = _save_external_thresholds(cfg, doc)
    catalog = _build_threshold_catalog(cfg)
    return jsonify({
        "ok": True,
        "path": th_path,
        "dataset": dataset_key,
        "field": field,
        "rule": _normalize_rule_for_editor(_dataset_rule_container(doc, dataset_key, create=False).get(field)),
        "notify": notify_config,
        "catalog": catalog,
    })


@app.get("/notifications_config")
def notifications_config():
    return jsonify(_notification_settings_payload())


@app.get("/auth_config")
def auth_config():
    return jsonify(_auth_settings_payload())


@app.get("/environments_config")
def environments_config():
    return jsonify(_environment_payload())


@app.post("/environments_save")
def environments_save():
    payload = request.get_json(force=True, silent=True) or {}
    try:
        items = _save_environment(payload)
        return jsonify({"ok": True, "items": items, "count": len(items)})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400


@app.post("/environments_bootstrap")
def environments_bootstrap():
    payload = request.get_json(force=True, silent=True) or {}
    try:
        items = _save_environment(payload)
        target_id = payload.get("id")
        if not target_id:
            target_name = str(payload.get("environment_name") or "").strip()
            saved = next((item for item in items if item.get("name") == target_name), None)
            target_id = saved.get("id") if saved else None
        if not target_id:
            raise ValueError("Could not determine which environment to bootstrap.")
        env = _bootstrap_environment_runtime(target_id)
        runtime_env_path = _write_runtime_env_file(env)
        portal_job_status, portal_job_started = _launch_job("portal", target_id)
        filer_job_status, filer_job_started = _launch_job("filer", target_id)
        refreshed = _list_environments(include_secret=False)
        return jsonify({
            "ok": True,
            "items": refreshed,
            "count": len(refreshed),
            "runtime_env_path": runtime_env_path,
            "state_dir": _state_dir(),
            "portal_job_started": bool(portal_job_started),
            "portal_job_already_running": not bool(portal_job_started) and portal_job_status.get("status") == "running",
            "portal_job": portal_job_status,
            "filer_job_started": bool(filer_job_started),
            "filer_job_already_running": not bool(filer_job_started) and filer_job_status.get("status") == "running",
            "filer_job": filer_job_status,
        })
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400


@app.post("/environments_delete")
def environments_delete():
    payload = request.get_json(force=True, silent=True) or {}
    try:
        items = _delete_environment(payload.get("id"))
        return jsonify({"ok": True, "items": items, "count": len(items)})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400


@app.post("/notifications_settings_save")
def notifications_settings_save():
    payload = request.get_json(force=True, silent=True) or {}
    try:
        saved = _save_email_settings(payload)
        return jsonify({"ok": True, "settings": saved, "db_path": _notifications_db_path()})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400


@app.post("/notifications_recipients_save")
def notifications_recipients_save():
    payload = request.get_json(force=True, silent=True) or {}
    try:
        recipients = _save_notification_recipient(payload)
        return jsonify({"ok": True, "recipients": recipients})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400


@app.post("/notifications_recipients_delete")
def notifications_recipients_delete():
    payload = request.get_json(force=True, silent=True) or {}
    try:
        recipients = _delete_notification_recipient(payload.get("id"))
        return jsonify({"ok": True, "recipients": recipients})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400


@app.post("/auth_settings_save")
def auth_settings_save():
    payload = request.get_json(force=True, silent=True) or {}
    try:
        settings = _save_app_settings(payload)
        return jsonify({"ok": True, "settings": settings})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400


@app.post("/auth_users_save")
def auth_users_save():
    payload = request.get_json(force=True, silent=True) or {}
    try:
        users = _save_local_user(payload)
        return jsonify({"ok": True, "users": users})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400


@app.post("/auth_users_delete")
def auth_users_delete():
    payload = request.get_json(force=True, silent=True) or {}
    try:
        users = _delete_local_user(payload.get("id"))
        return jsonify({"ok": True, "users": users})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400


@app.post("/notifications_test_email")
def notifications_test_email():
    payload = request.get_json(force=True, silent=True) or {}
    try:
        _send_test_email(payload)
        return jsonify({"ok": True})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400


@app.post("/notifications_run")
def notifications_run():
    try:
        env_id = _request_environment_id()
        result = run_threshold_notifications(env_id) if env_id else run_threshold_notifications_all_enabled()
        return jsonify({"ok": True, **result})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400


# ---- helpers to count crit/warn rows for main-tab badges
def _count_row_severity(rows, headers, warn_fn, topic=None):
    bad_rows = 0
    warn_rows = 0
    for r in rows:
        row_bad = False
        row_warn = False
        for h in headers:
            v = r.get(h, "")
            sev = warn_fn(h, v, r) if topic is None else warn_fn(topic, h, v, r)
            if sev == 'bad':
                row_bad = True
            elif sev == 'warn':
                row_warn = True
        if row_bad:
            bad_rows += 1
        elif row_warn:
            warn_rows += 1
    return {"bad": bad_rows, "warn": warn_rows}


def _overview_card(label, tab, rows_total, counts, updated_utc):
    bad = int((counts or {}).get("bad", 0) or 0)
    warn = int((counts or {}).get("warn", 0) or 0)
    total = int(rows_total or 0)
    ok = max(total - bad - warn, 0)
    denom = max(total, 1)
    if bad:
        status_text = "Critical"
        status_class = "crit"
    elif warn:
        status_text = "Warning"
        status_class = "warn"
    else:
        status_text = "OK"
        status_class = "ok"
    return {
        "label": label,
        "tab": tab,
        "rows": total,
        "bad": bad,
        "warn": warn,
        "ok": ok,
        "bad_pct": round((bad / denom) * 100, 2),
        "warn_pct": round((warn / denom) * 100, 2),
        "ok_pct": round((ok / denom) * 100, 2),
        "status_text": status_text,
        "status_class": status_class,
        "updated_utc": updated_utc or "",
    }


def _safe_float(value):
    if value is None:
        return None
    m = re.search(r"-?\d+(?:\.\d+)?", str(value).replace(",", ""))
    if not m:
        return None
    try:
        return float(m.group(0))
    except Exception:
        return None


def _top_counts(rows, columns, limit=6, empty_label="Unknown"):
    counts = {}
    for row in rows:
        val = ""
        for col in columns:
            val = (row.get(col) or "").strip()
            if val:
                break
        label = display_cell(col if val else "", val) if val else empty_label
        counts[label] = counts.get(label, 0) + 1
    total = max(sum(counts.values()), 1)
    items = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:limit]
    return [{"label": k, "value": v, "pct": round((v / total) * 100, 2)} for k, v in items]


def _status_tone(label):
    s = str(label or "").strip().lower()
    if s in {"synced", "ok", "connected", "active", "healthy", "true"}:
        return "sync"
    if any(token in s for token in ("stall", "fail", "error", "down", "disconnect", "critical")):
        return "stalled"
    if any(token in s for token in ("syncing", "running", "progress", "upload", "scan", "warning")):
        return "running"
    if any(token in s for token in ("deleted", "disabled", "nofolder", "none", "unknown")):
        return "deleted"
    return "info"


def _with_status_tones(items):
    out = []
    for item in items:
        enriched = dict(item)
        enriched["tone"] = _status_tone(item.get("label"))
        out.append(enriched)
    return out


def _boolean_state_counts(rows, columns, true_label, false_label):
    counts = {false_label: 0, true_label: 0}
    for row in rows:
        val = ""
        for col in columns:
            if col in row:
                val = str(row.get(col, "")).strip().lower()
                break
        is_true = val in {"true", "1", "yes", "y", "on", "deleted"}
        counts[true_label if is_true else false_label] += 1
    total = max(sum(counts.values()), 1)
    return [{"label": label, "value": value, "pct": round((value / total) * 100, 2), "tone": _status_tone(label)} for label, value in counts.items()]


def _tenant_summary(rows):
    deleted = 0
    for row in rows:
        val = str(row.get("Deleted", "")).strip().lower()
        if val in {"true", "1", "yes", "y", "on", "deleted"}:
            deleted += 1
    total = len(rows)
    return {"total": total, "active": max(total - deleted, 0), "deleted": deleted}


def _gauge(label, rows, columns):
    values = []
    for row in rows:
        for col in columns:
            num = _safe_float(row.get(col))
            if num is not None:
                values.append(num)
                break
    avg = round(sum(values) / len(values), 1) if values else 0
    if avg >= 90:
        tone = "crit"
    elif avg >= 75:
        tone = "warn"
    else:
        tone = "ok"
    return {"label": label, "value": avg, "pct": max(0, min(avg, 100)), "tone": tone}


def _cluster_consistency_summary(rows, key_field, ok_values=None):
    ok_values = {str(v).strip().lower() for v in (ok_values or set())}
    grouped = {}
    view_counts = Counter()
    member_rows = []
    for row in rows:
        source_host = (row.get("SourceHost") or "").strip()
        source_name = (row.get("SourceName") or "").strip()
        source_key = source_host or source_name or "Unknown"
        entry = grouped.setdefault(source_key, {
            "source_name": source_name or source_host or "Unknown",
            "source_host": source_host or source_name or "Unknown",
            "view_hashes": set(),
            "member_keys": set(),
            "errors": [],
        })
        view_hash = (row.get("ViewHash") or "").strip()
        if view_hash:
            entry["view_hashes"].add(view_hash)
            view_counts[view_hash] += 1
        member_key = (row.get(key_field) or "").strip()
        if member_key:
            entry["member_keys"].add(member_key)
        err = (row.get("CollectionError") or "").strip()
        if err:
            entry["errors"].append(err)
        member_rows.append(row)

    reference_hash = ""
    if view_counts:
        reference_hash = sorted(view_counts.items(), key=lambda item: (-item[1], item[0]))[0][0]
    reference_rows = [row for row in rows if (row.get("ViewHash") or "").strip() == reference_hash] if reference_hash else []
    reference_keys = {str(row.get(key_field) or "").strip() for row in reference_rows if str(row.get(key_field) or "").strip()}
    if not reference_keys:
        reference_keys = {str(row.get(key_field) or "").strip() for row in rows if str(row.get(key_field) or "").strip()}

    ok_count = 0
    bad_count = 0
    if ok_values:
        seen_keys = set()
        for row in reference_rows or rows:
            member_key = (row.get(key_field) or "").strip()
            if not member_key or member_key in seen_keys:
                continue
            seen_keys.add(member_key)
            status = str(row.get("Status") or "").strip().lower()
            if status in ok_values:
                ok_count += 1
            else:
                bad_count += 1

    source_views = []
    mismatch_sources = 0
    error_sources = 0
    for source_key, entry in sorted(grouped.items(), key=lambda item: (item[1]["source_name"].lower(), item[1]["source_host"].lower())):
        hashes = sorted(entry["view_hashes"])
        if entry["errors"]:
            state = "error"
            error_sources += 1
        elif len(hashes) > 1:
            state = "mismatch"
            mismatch_sources += 1
        elif reference_hash and hashes and hashes[0] != reference_hash:
            state = "mismatch"
            mismatch_sources += 1
        else:
            state = "ok"
        source_views.append({
            "source_key": source_key,
            "source_name": entry["source_name"],
            "source_host": entry["source_host"],
            "view_hash": ", ".join(hashes) if hashes else "",
            "members_seen": len(entry["member_keys"]),
            "errors": "; ".join(entry["errors"][:2]),
            "state": state,
        })

    if error_sources:
        tone = "crit"
        label = "Collection Error"
    elif mismatch_sources or len(view_counts) > 1:
        tone = "warn"
        label = "View Mismatch"
    else:
        tone = "ok"
        label = "Consistent"

    return {
        "sources_checked": len(grouped),
        "distinct_views": len(view_counts),
        "reference_view": reference_hash,
        "reference_members": len(reference_keys),
        "ok_members": ok_count,
        "bad_members": bad_count,
        "tone": tone,
        "label": label,
        "source_views": source_views,
    }


def _cluster_status_chart(rows, field_name):
    return _with_status_tones(_top_counts(rows, [field_name], limit=8, empty_label="Unknown"))


def _section_card(label, rows_total, counts):
    bad = int((counts or {}).get("bad", 0) or 0)
    warn = int((counts or {}).get("warn", 0) or 0)
    ok = max(int(rows_total or 0) - bad - warn, 0)
    return {"label": label, "rows": rows_total, "bad": bad, "warn": warn, "ok": ok}


def _pg_chart(pg_views):
    items = []
    for view in pg_views:
        bad = int(view.get("bad_rows_count", 0) or 0)
        warn = int(view.get("warn_rows_count", 0) or 0)
        total = max(len(view.get("rows", [])), 1)
        impacted = bad + warn
        items.append({
            "label": view.get("title", ""),
            "value": impacted,
            "pct": round((impacted / total) * 100, 2),
            "tone": "crit" if bad else ("warn" if warn else "ok"),
        })
    return sorted(items, key=lambda x: (-x["value"], x["label"]))[:8]


# ---- AI summary helpers ----
def build_ai_summary_data(env_id=None):
    cfg = load_conf_for_environment(env_id)

    # load thresholds.yaml (same logic as index)
    ext = {}
    th_src = cfg.get("thresholds_from") or {}
    th_path = th_src.get("path") or os.environ.get("FEATHERDASH_THRESHOLDS")
    if th_path:
        th_full = th_path if os.path.isabs(th_path) else os.path.join(APP_DIR, th_path)
        if os.path.exists(th_full):
            try:
                with open(th_full, "r") as f:
                    ext = yaml.safe_load(f) or {}
            except Exception:
                ext = {}

    # EDGE
    rows, headers = read_csv_rows(cfg["csv_path"])
    rows, headers = derive_fields(rows, headers, cfg)
    warn_edge = make_edge_warn_fn(cfg.get("thresholds"), ext)
    edge_counts = _count_row_severity(rows, headers, warn_edge)

    # PORTAL
    portal_cfg = cfg.get("portal") or {}
    servers_rows, servers_headers = read_csv_rows(portal_cfg.get("servers_csv"))
    storage_rows, storage_headers = read_csv_rows(portal_cfg.get("storage_csv"))
    tasks_rows, tasks_headers = read_csv_rows(portal_cfg.get("tasks_csv"))
    licenses_rows, licenses_headers = read_csv_rows(portal_cfg.get("licenses_csv"))
    tasks_rows = filter_dashboard_tasks(tasks_rows)

    warn_server_cell = make_portal_warn_fn(ext, "servers")
    warn_storage_cell = make_portal_warn_fn(ext, "storage")
    warn_task_cell = make_portal_warn_fn(ext, "tasks")

    c_servers = _count_row_severity(servers_rows, servers_headers, warn_server_cell)
    c_storage = _count_row_severity(storage_rows, storage_headers, warn_storage_cell)
    c_tasks = _count_row_severity(tasks_rows, tasks_headers, warn_task_cell)
    c_licenses_bad = 0
    for row in licenses_rows:
        expired = str(row.get("expired") or "").strip().lower() in {"true", "1", "yes", "y", "on"}
        valid = str(row.get("valid") or "").strip().lower()
        invalid = valid in {"false", "0", "no", "n", "off"}
        if expired or invalid:
            c_licenses_bad += 1

    portal_counts = {
        "bad": c_servers["bad"] + c_storage["bad"] + c_tasks["bad"] + c_licenses_bad,
        "warn": c_servers["warn"] + c_storage["warn"] + c_tasks["warn"],
    }

    # POSTGRES
    pg_cfg = cfg.get("postgres") or {}
    base_dir = pg_cfg.get("base_dir")
    topics = pg_cfg.get("topics") or {}

    warn_pg = make_pg_warn_fn(ext)
    pg_topics = []
    total_pg_bad = 0
    total_pg_warn = 0

    for key, fname in topics.items():
        path = os.path.join(base_dir, fname) if fname else ""
        r, h = read_csv_rows(path)
        counts = _count_row_severity(r, h, warn_pg, topic=key)
        total_pg_bad += counts["bad"]
        total_pg_warn += counts["warn"]
        pg_topics.append({
            "topic": key,
            "rows": len(r),
            "critical_rows": counts["bad"],
            "warning_rows": counts["warn"],
        })

    # SERVERS HEALTH
    sh_cfg = cfg.get("servers_health") or {}
    metrics_csv = sh_cfg.get("metrics_csv") or DEFAULT_CONF["servers_health"]["metrics_csv"]
    if metrics_csv and not os.path.isabs(metrics_csv):
        metrics_csv = os.path.join(APP_DIR, metrics_csv)
    hosts_rows, hosts_headers = read_csv_rows(metrics_csv)
    warn_hosts = make_servers_health_warn_fn(ext)
    hosts_counts = _count_row_severity(hosts_rows, hosts_headers, warn_hosts)

    summary = {
        "edge": {
            "rows": len(rows),
            "critical_rows": edge_counts["bad"],
            "warning_rows": edge_counts["warn"],
        },
        "portal": {
            "servers_rows": len(servers_rows),
            "storage_rows": len(storage_rows),
            "tasks_rows": len(tasks_rows),
            "licenses_rows": len(licenses_rows),
            "critical_rows": portal_counts["bad"],
            "warning_rows": portal_counts["warn"],
        },
        "postgres": {
            "topics": pg_topics,
            "total_critical_rows": total_pg_bad,
            "total_warning_rows": total_pg_warn,
        },
        "servers_health": {
            "rows": len(hosts_rows),
            "critical_rows": hosts_counts["bad"],
            "warning_rows": hosts_counts["warn"],
        },
    }
    return summary


def get_ai_summary_text(env_id=None):
    if client is None:
        return "<p class='ai-muted'>OpenAI API key is not configured on the server.</p>"

    data = build_ai_summary_data(env_id)
    context_json = json.dumps(data, indent=2)


    prompt = f"""
You are an SRE / production-ops assistant for a CTERA monitoring dashboard called CTERA Monitoring Dashboard.

Here is a JSON summary of the current system state:

{context_json}

Return an HTML fragment (no <html> or <body> tags) that will be injected into a <div>.

Formatting requirements:
- Start with a small <table> giving a one-row-per-subsystem overview
  (Edge, Portal, Postgres, Servers Health) with columns:
  Subsystem, Critical, Warning, Total. Use the counts from the JSON.
- After the table, add an <h3>Overall health</h3> heading followed by a <ul> of 4–6 <li> bullets.
  - Mix high-level statements with a bit of concrete detail.
  - When helpful, use the counts from the JSON explicitly, e.g. “3 critical edge rows out of 47 devices” or “table_bloat has 5 critical tables”.
- Then add an <h3>Key issues to investigate</h3> heading followed by a <ul>.
  - Include one bullet per subsystem that has non-zero critical or warning rows:
    * Edge devices
    * Portal (servers, storage, tasks)
    * Postgres (per-topic, e.g. table_bloat, long_running_queries, wraparound_*)
    * Servers Health
  - For each bullet, explain:
    * what is wrong (capacity, performance, data-loss, configuration, etc.),
    * roughly how big the problem is (use the counts),
    * what the operator should look at next.
- Optionally finish with an <h3>Recommended next actions</h3> heading and a <ul> of 3 short bullets grouped by priority (e.g. “Immediate / Today / This week”).
- Optionally add a final <p class="ai-muted"> note about residual risk (for example, no immediate data-loss risk, or replication looking healthy).
- Use <span class="ai-critical">Critical</span>, <span class="ai-warning">Warning</span>, and <span class="ai-ok">OK</span> inside bullets where appropriate to visually tag severity.
- Do NOT include any <html>, <head>, or <body> tags.
- Do NOT use markdown or backticks—only plain HTML elements.

Style:
- Professional but direct.
- 1–3 sentences per bullet (not just fragments).
- Be explicit about overall risk level (high/medium/low) for the estate.
- Avoid repeating the raw JSON verbatim; summarize it into clear guidance.
"""


    resp = client.chat.completions.create(
        model="gpt-4.1-mini",
        messages=[
            {"role": "system", "content": "You are a concise, practical SRE/ops assistant."},
            {"role": "user", "content": prompt},
        ],
        temperature=0.25,
    )
    return resp.choices[0].message.content.strip()


@app.get("/ai_summary")
def ai_summary():
    try:
        summary = get_ai_summary_text(_request_environment_id())
        return jsonify({"summary": summary})
    except Exception as e:
        # send back the error as text so it shows in the UI
        return jsonify({"summary": f"<p class='ai-muted'>Error generating AI summary: {e}</p>"}), 500


@app.get("/")
def index():
    global os
    current_env_id = _request_environment_id()
    cfg = load_conf_for_environment(current_env_id)
    theme = resolve_theme(cfg)
    brand = resolve_brand(cfg)
    refresh_seconds = cfg.get("refresh_seconds", 0)
    clip_check, max_cell_px = make_clip_check(cfg.get("ui") or {})

    # load external thresholds
    ext = {}
    th_src = cfg.get("thresholds_from") or {}
    th_path = th_src.get("path") or os.environ.get("FEATHERDASH_THRESHOLDS")
    if th_path:
        th_full = th_path if os.path.isabs(th_path) else os.path.join(APP_DIR, th_path)
        if os.path.exists(th_full):
            try:
                with open(th_full, "r") as f:
                    ext = yaml.safe_load(f) or {}
            except Exception:
                ext = {}

    # TENANTS
    tenants_src = cfg.get("tenants_csv") or ""
    if tenants_src and not os.path.isabs(tenants_src):
        tenants_src = os.path.join(APP_DIR, tenants_src)
    tenants_src = os.path.normpath(os.path.abspath(tenants_src)) if tenants_src else ""
    tenants_mtime = _file_mtime_iso(tenants_src)
    tenants_rows, tenants_headers = read_csv_rows(tenants_src)
    style_tenants = make_tenants_style_fn(ext)
    # Tenants don't use warn() thresholds today; just show 0/0
    tenants_counts = {"bad": 0, "warn": 0}
    tenant_summary = _tenant_summary(tenants_rows)
    tenant_type_chart = _top_counts(tenants_rows, ["PortalType", "Type"], limit=4)

    # EDGE
    rows, headers = read_csv_rows(cfg["csv_path"])
    csv_mtime = _file_mtime_iso(cfg["csv_path"])
    rows, headers = derive_fields(rows, headers, cfg)
    warn_edge = make_edge_warn_fn(cfg.get("thresholds"), ext)
    style_edge = make_edge_style_fn(cfg.get("thresholds"), ext)
    edge_counts = _count_row_severity(rows, headers, warn_edge)
    edge_status_chart = _with_status_tones(_top_counts(rows, ["CloudSync Status", "Status"], limit=6))
    edge_tenant_chart = _top_counts(rows, ["Tenant"], limit=6)
    edge_gauges = [
        _gauge("CPU", rows, ["CPU_Current", "CPUUserPct", "Max CPU"]),
        _gauge("Memory", rows, ["Mem_Current", "MemUsedPct", "Max Memory"]),
    ]

    # PORTAL
    servers_rows, servers_headers = read_csv_rows(cfg["portal"]["servers_csv"])
    storage_rows, storage_headers = read_csv_rows(cfg["portal"]["storage_csv"])
    tasks_rows, tasks_headers = read_csv_rows((cfg.get("portal") or {}).get("tasks_csv"))
    licenses_rows, licenses_headers = read_csv_rows((cfg.get("portal") or {}).get("licenses_csv"))
    if not tasks_rows:
        alt = os.path.join(APP_DIR, "..", "task.csv")
        tr, th = read_csv_rows(alt)
        if tr:
            tasks_rows, tasks_headers = tr, th
    tasks_rows = filter_dashboard_tasks(tasks_rows)
    hidden_license_headers = {"db_id", "index", "key", "pg_host", "pg_port", "pg_db", "cluster", "collected_at"}
    preferred_license_headers = [
        "original_key",
        "valid",
        "expired",
        "expiration_date",
        "portal_license",
        "vgateways4",
        "vgateways8",
        "vgateways32",
        "vgateways64",
        "vgateways128",
        "vgateways256",
        "storage",
        "cloud_drives",
        "cloud_drives_lite",
        "appliances",
        "server_agents",
        "workstation_agents",
    ]
    licenses_visible_set = [h for h in licenses_headers if h not in hidden_license_headers]
    licenses_display_headers = [h for h in preferred_license_headers if h in licenses_visible_set]
    licenses_display_headers.extend([h for h in licenses_visible_set if h not in licenses_display_headers])

    warn_server_cell = make_portal_warn_fn(ext, "servers")
    warn_storage_cell = make_portal_warn_fn(ext, "storage")
    warn_task_cell = make_portal_warn_fn(ext, "tasks")
    style_server_cell = make_portal_style_fn(ext, "servers")
    style_storage_cell = make_portal_style_fn(ext, "storage")
    style_tasks_cell = make_portal_style_fn(ext, "tasks")

    # aggregate Portal badge counts across the three sections
    c_servers = _count_row_severity(servers_rows, servers_headers, warn_server_cell)
    c_storage = _count_row_severity(storage_rows, storage_headers, warn_storage_cell)
    c_tasks = _count_row_severity(tasks_rows, tasks_headers, warn_task_cell)
    licenses_bad = 0
    for row in licenses_rows:
        expired = str(row.get("expired") or "").strip().lower() in {"true", "1", "yes", "y", "on"}
        valid = str(row.get("valid") or "").strip().lower()
        invalid = valid in {"false", "0", "no", "n", "off"}
        if expired or invalid:
            licenses_bad += 1
    c_licenses = {"bad": licenses_bad, "warn": 0}
    valid_licenses = sum(1 for row in licenses_rows if str(row.get("valid") or "").strip().lower() in {"true", "1", "yes", "y", "on"})
    expired_licenses = sum(1 for row in licenses_rows if str(row.get("expired") or "").strip().lower() in {"true", "1", "yes", "y", "on"})
    portal_license_rows = sum(1 for row in licenses_rows if str(row.get("portal_license") or "").strip().lower() in {"true", "1", "yes", "y", "on"})
    portal_counts = {
        "bad": c_servers["bad"] + c_storage["bad"] + c_tasks["bad"] + c_licenses["bad"],
        "warn": c_servers["warn"] + c_storage["warn"] + c_tasks["warn"] + c_licenses["warn"],
    }
    portal_section_cards = [
        _section_card("Servers", len(servers_rows), c_servers),
        _section_card("Storage", len(storage_rows), c_storage),
        _section_card("Tasks", len(tasks_rows), c_tasks),
        _section_card("Licenses", len(licenses_rows), c_licenses),
    ]

    # POSTGRES — build topic views + compute warn counts
    pg_cfg = cfg.get("postgres") or {}
    base_dir = pg_cfg.get("base_dir")
    topics = pg_cfg.get("topics") or {}
    warn_pg = make_pg_warn_fn(ext)
    style_pg = make_pg_style_fn(ext)
    pg_views = []
    total_pg_bad = 0
    total_pg_warn = 0
    for key, fname in topics.items():
        path = os.path.join(base_dir, fname) if fname else ""
        r, h = read_csv_rows(path)
        bad_rows = 0
        warn_rows = 0
        bad_cells = 0
        for row in r:
            row_bad = False
            row_warn = False
            for col in h:
                val = row.get(col, "")
                sev = warn_pg(key, col, val, row)
                if sev == 'bad':
                    bad_cells += 1
                    row_bad = True
                elif sev == 'warn':
                    row_warn = True
            if row_bad:
                bad_rows += 1
            elif row_warn:
                warn_rows += 1
        total_pg_bad += bad_rows
        total_pg_warn += warn_rows
        pg_views.append({
            "key": key,
            "title": re.sub(r'[_]+', ' ', key).title(),
            "path": path,
            "rows": r,
            "headers": h,
            "bad_rows_count": bad_rows,
            "warn_rows_count": warn_rows,
            "bad_cells_count": bad_cells
        })
    pg_counts = {"bad": total_pg_bad, "warn": total_pg_warn}
    pg_topic_chart = _pg_chart(pg_views)

    # SERVERS HEALTH
    metrics_csv = (cfg.get("servers_health") or {}).get("metrics_csv") or DEFAULT_CONF["servers_health"]["metrics_csv"]
    if metrics_csv:
        if not os.path.isabs(metrics_csv):
            metrics_csv = os.path.join(APP_DIR, metrics_csv)
        metrics_csv = os.path.normpath(os.path.abspath(metrics_csv))
    metrics_mtime = _file_mtime_iso(metrics_csv)
    hosts_rows, hosts_headers = read_csv_rows(metrics_csv)
    warn_hosts = make_servers_health_warn_fn(ext)
    style_hosts = make_servers_health_style_fn(ext)
    hosts_counts = _count_row_severity(hosts_rows, hosts_headers, warn_hosts)
    host_gauges = [
        _gauge("Memory", hosts_rows, ["MemUsedPct"]),
        _gauge("Root Disk", hosts_rows, ["RootDiskUsedPct"]),
        _gauge("Data Pool", hosts_rows, ["DataPoolUsedPct"]),
        _gauge("DB Archive", hosts_rows, ["DBArchivePoolUsedPct"]),
    ]
    host_status_chart = _with_status_tones(_top_counts(hosts_rows, ["Status", "Connected"], limit=6))
    nomad_csv = (cfg.get("servers_health") or {}).get("nomad_csv") or ""
    consul_csv = (cfg.get("servers_health") or {}).get("consul_csv") or ""
    if nomad_csv and not os.path.isabs(nomad_csv):
        nomad_csv = os.path.join(APP_DIR, nomad_csv)
    if consul_csv and not os.path.isabs(consul_csv):
        consul_csv = os.path.join(APP_DIR, consul_csv)
    nomad_csv = os.path.normpath(os.path.abspath(nomad_csv)) if nomad_csv else ""
    consul_csv = os.path.normpath(os.path.abspath(consul_csv)) if consul_csv else ""
    nomad_mtime = _file_mtime_iso(nomad_csv)
    consul_mtime = _file_mtime_iso(consul_csv)
    nomad_rows, nomad_headers = read_csv_rows(nomad_csv)
    consul_rows, consul_headers = read_csv_rows(consul_csv)
    nomad_summary = _cluster_consistency_summary(nomad_rows, "NodeID", ok_values={"ready"})
    consul_summary = _cluster_consistency_summary(consul_rows, "Node", ok_values={"alive"})
    nomad_status_chart = _cluster_status_chart(nomad_rows, "Status")
    consul_status_chart = _cluster_status_chart(consul_rows, "Status")

    # portal CSV sources (for "File: ...")
    portal_cfg = cfg.get("portal") or {}
    portal_servers_src = portal_cfg.get("servers_csv", "")
    portal_storage_src = portal_cfg.get("storage_csv", "")
    portal_tasks_src = portal_cfg.get("tasks_csv", "")

    def _norm_src(pth):
        if not pth:
            return ""
        try:
            if not os.path.isabs(pth):
                pth = os.path.join(APP_DIR, pth)
            return os.path.normpath(os.path.abspath(pth))
        except Exception:
            return pth

    portal_servers_src = _norm_src(portal_servers_src)
    portal_storage_src = _norm_src(portal_storage_src)
    portal_tasks_src = _norm_src(portal_tasks_src)
    portal_licenses_src = _norm_src(portal_cfg.get("licenses_csv", ""))

    portal_servers_mtime = _file_mtime_iso(portal_servers_src)
    portal_storage_mtime = _file_mtime_iso(portal_storage_src)
    portal_tasks_mtime = _file_mtime_iso(portal_tasks_src)
    portal_licenses_mtime = _file_mtime_iso(portal_licenses_src)

    portal_rows_total = len(servers_rows) + len(storage_rows) + len(tasks_rows) + len(licenses_rows)
    pg_rows_total = sum(len(v.get("rows", [])) for v in pg_views)
    overview_cards = [
        _overview_card("Tenants", "tenants", len(tenants_rows), tenants_counts, tenants_mtime),
        _overview_card("Edge Filers", "edge", len(rows), edge_counts, csv_mtime),
        _overview_card("Portal", "portal", portal_rows_total, portal_counts, portal_servers_mtime),
        _overview_card("Postgres", "pg", pg_rows_total, pg_counts, _file_mtime_iso(os.path.join(base_dir, "wraparound_summary.csv"))),
        _overview_card("Servers Health", "svrhlth", len(hosts_rows), hosts_counts, metrics_mtime),
    ]
    overall_rows = sum(card["rows"] for card in overview_cards)
    overall_bad = sum(card["bad"] for card in overview_cards)
    overall_warn = sum(card["warn"] for card in overview_cards)
    if overall_bad:
        overall_risk_label = "High Risk"
        overall_risk_class = "risk-high"
    elif overall_warn:
        overall_risk_label = "Medium Risk"
        overall_risk_class = "risk-medium"
    else:
        overall_risk_label = "Healthy"
        overall_risk_class = "risk-low"
    freshness_items = [
        {"label": "Tenants", "updated_utc": tenants_mtime},
        {"label": "Edge Filers", "updated_utc": csv_mtime},
        {"label": "Portal Servers", "updated_utc": portal_servers_mtime},
        {"label": "Portal Storage", "updated_utc": portal_storage_mtime},
        {"label": "Portal Tasks", "updated_utc": portal_tasks_mtime},
        {"label": "Portal Licenses", "updated_utc": portal_licenses_mtime},
        {"label": "Postgres", "updated_utc": _file_mtime_iso(os.path.join(base_dir, "wraparound_summary.csv"))},
        {"label": "Servers Health", "updated_utc": metrics_mtime},
    ]

    return render_template_string(
        HTML,
        product_name=PRODUCT_NAME,
        app_version=APP_VERSION,
        project_dir=PROJECT_DIR,
        default_config_file=DEFAULT_CONFIG_FILE,
        default_data_dir=DEFAULT_DATA_DIR,
        default_db_dir=DEFAULT_DB_DIR,
        default_log_dir=DEFAULT_LOG_DIR,
        default_state_dir=DEFAULT_STATE_DIR,
        dashboard_port=os.environ.get("PORT", "8080"),
        auth_mode=_auth_mode(),
        current_username=session.get("local_username", ""),
        clip_check=clip_check, max_cell_px=max_cell_px,
        # common
        refresh_seconds=refresh_seconds, theme=theme, brand=brand,
        display_cell=display_cell, display_header=display_header,
        overview_cards=overview_cards, freshness_items=freshness_items,
        overall_rows=overall_rows, overall_bad=overall_bad, overall_warn=overall_warn,
        overall_risk_label=overall_risk_label, overall_risk_class=overall_risk_class,
        # edge
        csv_path=cfg["csv_path"], csv_mtime=csv_mtime, rows=rows, headers=headers,
        warn_edge=warn_edge, style_edge=style_edge, edge_counts=edge_counts,
        edge_status_chart=edge_status_chart, edge_tenant_chart=edge_tenant_chart, edge_gauges=edge_gauges,
        # portal
                # portal
        servers_rows=servers_rows, servers_headers=servers_headers,
        storage_rows=storage_rows, storage_headers=storage_headers,
        tasks_rows=tasks_rows, tasks_headers=tasks_headers,
        licenses_rows=licenses_rows, licenses_headers=licenses_headers,
        licenses_display_headers=licenses_display_headers,
        c_servers=c_servers, c_storage=c_storage, c_tasks=c_tasks, c_licenses=c_licenses,
        warn_server_cell=warn_server_cell, warn_storage_cell=warn_storage_cell, warn_task_cell=warn_task_cell,
        style_server_cell=style_server_cell, style_storage_cell=style_storage_cell, style_tasks_cell=style_tasks_cell,
        portal_counts=portal_counts, portal_section_cards=portal_section_cards,
        valid_licenses=valid_licenses, expired_licenses=expired_licenses, portal_license_rows=portal_license_rows,
        # postgres (sub-tabs)
        pg_base_dir=base_dir, pg_views=pg_views, warn_pg=warn_pg, style_pg=style_pg, pg_counts=pg_counts, pg_topic_chart=pg_topic_chart,
        # servers health
        metrics_csv=metrics_csv, metrics_mtime=metrics_mtime, hosts_rows=hosts_rows, hosts_headers=hosts_headers,
        warn_hosts=warn_hosts, style_hosts=style_hosts, hosts_counts=hosts_counts,
        host_gauges=host_gauges, host_status_chart=host_status_chart,
        nomad_csv=nomad_csv, nomad_mtime=nomad_mtime, nomad_rows=nomad_rows, nomad_headers=nomad_headers,
        nomad_summary=nomad_summary, nomad_status_chart=nomad_status_chart,
        consul_csv=consul_csv, consul_mtime=consul_mtime, consul_rows=consul_rows, consul_headers=consul_headers,
        consul_summary=consul_summary, consul_status_chart=consul_status_chart,
        # portal sources
        portal_servers_src=portal_servers_src, portal_storage_src=portal_storage_src, portal_tasks_src=portal_tasks_src, portal_licenses_src=portal_licenses_src,
        portal_servers_mtime=portal_servers_mtime, portal_storage_mtime=portal_storage_mtime, portal_tasks_mtime=portal_tasks_mtime, portal_licenses_mtime=portal_licenses_mtime,
        # tenants
        tenants_src=tenants_src, tenants_mtime=tenants_mtime,
        tenants_rows=tenants_rows, tenants_headers=tenants_headers, style_tenants=style_tenants,
        tenants_counts=tenants_counts, tenant_summary=tenant_summary, tenant_type_chart=tenant_type_chart,
    )


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8080")), debug=False)
    #app.run(host="127.0.0.1", port=int(os.environ.get("PORT","8080")), debug=False)

