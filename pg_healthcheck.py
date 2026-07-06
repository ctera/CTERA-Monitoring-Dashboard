#!/usr/bin/env python3
"""
pg_healthcheck.py

Collects:
  1) Long-running queries
  2) Wraparound statistics (DB list + top tables + summary)
  3) Last VACUUM/ANALYZE timestamps per table
  4) Table sizes (table/index/total/TOAST)
  5) Table bloat (community SQL by default; no extensions needed)
  6) Index bloat (community SQL by default; no extensions needed)
  7) Licenses

Bloat methods:
  - community  : uses community SQL from pg_stats/pg_class (no extensions needed)
  - pgstattuple: uses pgstattuple/pgstatindex extension (more accurate, heavier)

Options:
  --min-size-bytes   Skip tables/indexes smaller than this in sizes & bloat
  --bloat-method     community | pgstattuple
  --create-pgstattuple  Try to CREATE EXTENSION pgstattuple (if permitted)

Requires:
  pip install psycopg2-binary
"""

import argparse
import csv
import getpass
import json
import os
import re
import sys
from datetime import timedelta, datetime, timezone

try:
    import psycopg2
    import psycopg2.extras
except Exception:
    print("ERROR: psycopg2 not installed. Run: pip install psycopg2-binary", file=sys.stderr)
    raise

# ----------------- CLI -----------------

def parse_args():
    p = argparse.ArgumentParser(description="Postgres health check: queries, wraparound, vacuum/analyze, sizes, bloat.")
    p.add_argument("--host", required=True, help="DB host or IP")
    p.add_argument("--port", type=int, default=5432, help="DB port (default: 5432)")
    p.add_argument("--dbname", default="postgres", help="Database name (default: postgres)")
    p.add_argument("--user", required=True, help="DB user")
    p.add_argument("--password", help="DB password (will prompt if omitted)")
    p.add_argument("--sslmode", default="prefer",
                   choices=["disable","allow","prefer","require","verify-ca","verify-full"],
                   help="SSL mode (default: prefer)")

    p.add_argument("--min-age-seconds", type=int, default=60,
                   help="Min runtime for 'long-running' queries (default: 60s)")
    p.add_argument("--top-wrap-tables", type=int, default=20,
                   help="Top N tables by wraparound age (default: 20)")
    p.add_argument("--max-query-len", type=int, default=2000,
                   help="Max characters of query text to return (default: 2000)")

    # Sizes & bloat
    p.add_argument("--min-size-bytes", type=int, default=0,
                   help="Ignore tables/indexes smaller than this in sizes & bloat (default: 0)")
    p.add_argument("--bloat-method", choices=["community", "pgstattuple"], default="community",
                   help="'community' uses pg_stats-based estimates; 'pgstattuple' uses extension")
    p.add_argument("--create-pgstattuple", action="store_true",
                   help="Attempt CREATE EXTENSION IF NOT EXISTS pgstattuple (if using --bloat-method=pgstattuple)")

    # Output
    p.add_argument("--format", choices=["csv","json","table"], default="csv",
                   help="Output format (default: csv)")
    p.add_argument("--cluster", default="", help="Optional cluster/group label (e.g., prod-east)")
    p.add_argument("--outdir", help="Required for --format=csv or --format=json (writes files into this directory)")
    return p.parse_args()

# ----------------- DB helpers -----------------

def connect(args):
    pw = args.password or getpass.getpass("Password: ")
    dsn = (
        f"host={args.host} port={args.port} dbname={args.dbname} user={args.user} "
        f"password={pw} application_name=pg_health_check sslmode={args.sslmode}"
    )
    return psycopg2.connect(dsn)

def _escape_percent_for_psycopg(sql: str) -> str:
    """
    Escape literal % that are NOT parameter placeholders (%s or %(name)s).
    Converts: %foo -> %%foo, leaves %s and %(name)s intact.
    """
    return re.sub(r"%(?!(\([^)]+\))?s)", "%%", sql)

def fetch_dicts(cur, sql, params=None):
    """
    Execute a query and return a list of dicts.
    If psycopg2 complains about stray % in SQL, auto-escape and retry.
    """
    try:
        cur.execute(sql, params or ())
    except Exception:
        esc = _escape_percent_for_psycopg(sql)
        if esc != sql:
            cur.execute(esc, params or ())
        else:
            raise
    cols = [d.name for d in cur.description]
    rows = cur.fetchall()
    return [{cols[i]: r[i] for i in range(len(cols))} for r in rows]


def get_table_columns(cur, table_name, schema_name="public"):
    sql = """
    SELECT column_name
    FROM information_schema.columns
    WHERE table_schema = %s AND table_name = %s
    ORDER BY ordinal_position
    """
    cur.execute(sql, (schema_name, table_name))
    return [row[0] for row in cur.fetchall()]

# ----------------- Formatting helpers -----------------

def fmt_timedelta(td):
    if td is None:
        return None
    if isinstance(td, timedelta):
        total = int(td.total_seconds())
        h = total // 3600
        m = (total % 3600) // 60
        s = total % 60
        return f"{h:02d}:{m:02d}:{s:02d}"
    return str(td)

def print_table(rows, headers):
    if not rows:
        print("(no rows)")
        return
    widths = {h: len(h) for h in headers}
    for r in rows:
        for h in headers:
            s = "" if r.get(h) is None else str(r.get(h))
            widths[h] = max(widths[h], len(s))
    print(" | ".join(h.ljust(widths[h]) for h in headers))
    print("-+-".join("-"*widths[h] for h in headers))
    for r in rows:
        print(" | ".join(("" if r.get(h) is None else str(r.get(h))).ljust(widths[h]) for h in headers))

def export_csv(path, rows, headers):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=headers, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({h: r.get(h) for h in headers})

def export_json(path, payload):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, default=str)

# ----- Metadata stamping for portability/multi-host -----
META_COLS = ["pg_host","pg_port","pg_db","cluster","collected_at"]

def stamp_rows(rows, meta):
    if not rows:
        return rows
    for r in rows:
        if isinstance(r, dict):
            r.update(meta)
    return rows

# ----------------- Collectors -----------------

# 1) Long-running queries
def get_long_running_queries(cur, min_age_seconds, max_query_len):
    sql = """
    SELECT
        pid, usename, application_name, client_addr, state,
        wait_event_type, wait_event,
        backend_start, xact_start, query_start,
        now() - query_start AS runtime,
        LEFT(query, %s) AS query
    FROM pg_catalog.pg_stat_activity
    WHERE state <> 'idle'
      AND (now() - query_start) >= make_interval(secs => %s)
    ORDER BY runtime DESC;
    """
    rows = fetch_dicts(cur, sql, (max_query_len, min_age_seconds))
    for r in rows:
        r["runtime"] = fmt_timedelta(r.get("runtime"))
    return rows

# 2) Wraparound
def get_wraparound_db_list(cur):
    sql = """
    WITH max_age AS (
      SELECT 2000000000 as max_old_xid,
             setting AS autovacuum_freeze_max_age
      FROM pg_catalog.pg_settings WHERE name = 'autovacuum_freeze_max_age'
    )
    SELECT d.datname,
           age(d.datfrozenxid) AS age,
           (SELECT autovacuum_freeze_max_age FROM max_age)::int AS autovacuum_freeze_max_age,
           ROUND(100 * age(d.datfrozenxid)::float /
                  (SELECT max_old_xid FROM max_age))::int AS pct_of_max,
           ROUND(100 * age(d.datfrozenxid)::float /
                  (SELECT autovacuum_freeze_max_age FROM max_age)::float)::int AS pct_of_emergency_autovac
    FROM pg_catalog.pg_database d
    WHERE d.datallowconn
    ORDER BY pct_of_max DESC, age DESC;
    """
    return fetch_dicts(cur, sql)

def get_wraparound_top_tables(cur, top_n):
    sql = """
    WITH max_age AS (
      SELECT 2000000000 as max_old_xid,
             setting AS autovacuum_freeze_max_age
      FROM pg_catalog.pg_settings WHERE name = 'autovacuum_freeze_max_age'
    )
    SELECT n.nspname AS schema,
           c.relname AS table,
           age(c.relfrozenxid) AS age,
           (SELECT autovacuum_freeze_max_age FROM max_age)::int AS autovacuum_freeze_max_age,
           ROUND(100 * age(c.relfrozenxid)::float /
                  (SELECT max_old_xid FROM max_age))::int AS pct_of_max,
           ROUND(100 * age(c.relfrozenxid)::float /
                  (SELECT autovacuum_freeze_max_age FROM max_age)::float)::int AS pct_of_emergency_autovac,
           pg_total_relation_size(c.oid) AS size_bytes
    FROM pg_class c
    JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE c.relkind = 'r'
    ORDER BY pct_of_max DESC, size_bytes DESC
    LIMIT %s;
    """
    return fetch_dicts(cur, sql, (top_n,))

def get_wraparound_summary(cur):
    sql = """
    WITH max_age AS (
      SELECT 2000000000 as max_old_xid,
             setting AS autovacuum_freeze_max_age
      FROM pg_catalog.pg_settings WHERE name = 'autovacuum_freeze_max_age'
    ), per_database_stats AS (
      SELECT datname,
             m.max_old_xid::int,
             m.autovacuum_freeze_max_age::int,
             age(d.datfrozenxid) AS oldest_current_xid
      FROM pg_catalog.pg_database d
      JOIN max_age m ON (true)
      WHERE d.datallowconn
    )
    SELECT max(oldest_current_xid) AS oldest_current_xid,
           max(ROUND(100*(oldest_current_xid/max_old_xid::float))) AS percent_towards_wraparound,
           max(ROUND(100*(oldest_current_xid/autovacuum_freeze_max_age::float))) AS percent_towards_emergency_autovac
    FROM per_database_stats;
    """
    return fetch_dicts(cur, sql)

# 3) VACUUM / ANALYZE per table
def get_vacuum_analyze_stats(cur):
    sql = """
    SELECT
        n.nspname AS schema,
        c.relname AS table,
        s.n_live_tup,
        s.n_dead_tup,
        s.last_vacuum,
        s.last_autovacuum,
        s.last_analyze,
        s.last_autoanalyze,
        s.vacuum_count,
        s.autovacuum_count,
        s.analyze_count,
        s.autoanalyze_count
    FROM pg_catalog.pg_stat_user_tables s
    JOIN pg_catalog.pg_class c ON c.oid = s.relid
    JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
    ORDER BY n.nspname, c.relname;
    """
    return fetch_dicts(cur, sql)


def get_licenses(cur):
    preferred_columns = [
        "db_id",
        "index",
        "key",
        "original_key",
        "expired",
        "expiration_date",
        "appliances",
        "server_agents",
        "workstation_agents",
        "cloud_drives",
        "cloud_drives_lite",
        "valid",
        "antivirus",
        "varonis",
        "key_manager",
        "dlp",
        "portal_license",
        "vgateways4",
        "vgateways8",
        "vgateways32",
        "vgateways64",
        "vgateways128",
        "vgateways256",
        "storage",
        "comment",
        "global_file_lock",
    ]
    existing = set(get_table_columns(cur, "licenses"))
    selected = [col for col in preferred_columns if col in existing]
    if not selected:
        return []
    sql = f"""
    SELECT
        {", ".join(selected)}
    FROM licenses
    ORDER BY expiration_date NULLS LAST, index;
    """
    rows = fetch_dicts(cur, sql)
    for row in rows:
        for col in preferred_columns:
            row.setdefault(col, "")
    return rows

# 4) Table sizes
def get_table_sizes(cur, min_bytes):
    sql = """
    SELECT
        n.nspname AS schema,
        c.relname AS table,
        pg_relation_size(c.oid) AS table_bytes,
        pg_indexes_size(c.oid)  AS indexes_bytes,
        (pg_total_relation_size(c.oid) - pg_relation_size(c.oid) - pg_indexes_size(c.oid)) AS toast_and_other_bytes,
        pg_total_relation_size(c.oid) AS total_bytes
    FROM pg_catalog.pg_class c
    JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
    WHERE c.relkind = 'r'
      AND pg_total_relation_size(c.oid) >= %s
    ORDER BY total_bytes DESC;
    """
    return fetch_dicts(cur, sql, (min_bytes,))

# 5) Table bloat — COMMUNITY SQL (fixed; no extension)
def get_table_bloat_community(cur, min_bytes):
    sql = """
    WITH constants AS (
      SELECT
        current_setting('block_size')::numeric AS bs,
        23::numeric AS hdr,
        8::numeric  AS ma
    ),
    table_estimates AS (
      SELECT
        c.oid AS tblid,
        n.nspname AS schemaname,
        c.relname AS tblname,
        c.reltuples,
        c.relpages,
        COALESCE(SUBSTRING(array_to_string(c.reloptions, ' ') FROM 'fillfactor=([0-9]+)')::int, 100) AS fillfactor
      FROM pg_class c
      JOIN pg_namespace n ON n.oid = c.relnamespace
      WHERE c.relkind = 'r'
    ),
    attr_data AS (
      SELECT
        te.tblid,
        SUM((1 - s.null_frac) * s.avg_width) AS datawidth
      FROM table_estimates te
      JOIN pg_stats s
        ON s.schemaname = te.schemaname
       AND s.tablename  = te.tblname
      GROUP BY te.tblid
    ),
    calc AS (
      SELECT
        te.schemaname,
        te.tblname,
        te.reltuples,
        te.relpages,
        te.fillfactor,
        c.bs, c.hdr, c.ma,
        COALESCE(ad.datawidth, 24)::numeric AS datawidth
      FROM table_estimates te
      LEFT JOIN attr_data ad ON ad.tblid = te.tblid
      CROSS JOIN constants c
    ),
    bloat AS (
      SELECT
        schemaname,
        tblname,
        relpages,
        fillfactor,
        bs,
        GREATEST(
          CEIL(
            (reltuples * ( (datawidth + hdr + ma - MOD(hdr, ma)) )) /
            ((bs - 20) * (fillfactor / 100.0))
          ),
          1
        )::numeric AS expected_pages
      FROM calc
    )
    SELECT
      current_database() AS current_database,
      schemaname,
      tblname,
      pg_size_pretty(relpages*bs) AS real_size,
      (relpages*bs)::bigint AS real_size_bytes,
      pg_size_pretty(GREATEST(relpages-expected_pages,0)*bs) AS extra_size,
      ROUND(CASE WHEN relpages > 0
                 THEN 100.0 * GREATEST(relpages-expected_pages,0) / relpages
                 ELSE NULL END, 2) AS extra_ratio,
      fillfactor,
      pg_size_pretty(GREATEST(relpages-expected_pages,0)*bs) AS bloat_size,
      ROUND(CASE WHEN relpages > 0
                 THEN 100.0 * GREATEST(relpages-expected_pages,0) / relpages
                 ELSE NULL END, 2) AS bloat_ratio,
      0::int AS is_na
    FROM bloat
    WHERE (relpages*bs) >= %s
    ORDER BY bloat_ratio DESC, real_size_bytes DESC;
    """
    return fetch_dicts(cur, sql, (min_bytes,))

def get_index_bloat_community(cur, min_bytes):
    sql = """
    WITH index_data AS (
        SELECT
            n.nspname AS schemaname,
            c.relname AS tblname,
            i.relname AS idxname,
            i.reltuples, i.relpages,
            coalesce(SUBSTRING(array_to_string(i.reloptions, ' ') FROM 'fillfactor=([0-9]+)')::smallint, 100) AS fillfactor
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        JOIN pg_index ix ON ix.indrelid = c.oid
        JOIN pg_class i ON i.oid = ix.indexrelid
        WHERE c.relkind = 'r'
    ), idx_calc AS (
        SELECT
            schemaname, tblname, idxname, reltuples, relpages, fillfactor,
            relpages * current_setting('block_size')::numeric AS real_size
        FROM index_data
        WHERE relpages * current_setting('block_size')::numeric >= %s
    )
    SELECT
        schemaname, tblname, idxname,
        pg_size_pretty(real_size) AS real_size, real_size AS real_size_bytes,
        NULL::text AS extra_size, NULL::numeric AS extra_ratio,
        fillfactor,
        NULL::text AS bloat_size, NULL::numeric AS bloat_ratio,
        0::int AS is_na
    FROM idx_calc
    ORDER BY real_size DESC;
    """
    return fetch_dicts(cur, sql, (min_bytes,))

# 6) Bloat — PGSTATTUPLE (extension)
def ensure_pgstattuple(cur, create_if_missing=False):
    try:
        cur.execute("SELECT true FROM pg_extension WHERE extname = 'pgstattuple'")
        if cur.fetchone():
            return True
        if not create_if_missing:
            return False
        cur.execute("CREATE EXTENSION IF NOT EXISTS pgstattuple")
        return True
    except Exception:
        return False

def get_table_bloat_pgstattuple(cur, min_bytes):
    sql = """
    SELECT
        current_database() AS current_database,
        n.nspname AS schemaname,
        c.relname AS tblname,
        pg_size_pretty(pg_relation_size(c.oid)) AS real_size,
        pg_relation_size(c.oid) AS real_size_bytes,
        pg_size_pretty((pgstattuple(c.oid)).approximate_free_space) AS extra_size,
        NULL::numeric AS extra_ratio,
        coalesce(SUBSTRING(array_to_string(c.reloptions, ' ') FROM 'fillfactor=([0-9]+)')::smallint, 100) AS fillfactor,
        pg_size_pretty((pgstattuple(c.oid)).approximate_free_space) AS bloat_size,
        ROUND(100.0 * (pgstattuple(c.oid)).approximate_free_space / pg_relation_size(c.oid), 2) AS bloat_ratio,
        1::int AS is_na
    FROM pg_class c
    JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE c.relkind = 'r'
      AND pg_relation_size(c.oid) >= %s
    ORDER BY bloat_ratio DESC, real_size_bytes DESC;
    """
    return fetch_dicts(cur, sql, (min_bytes,))

def get_index_bloat_pgstattuple(cur, min_bytes):
    sql = """
    SELECT
        n.nspname AS schemaname,
        t.relname AS tblname,
        i.relname AS idxname,
        pg_size_pretty(pg_relation_size(i.oid)) AS real_size,
        pg_relation_size(i.oid) AS real_size_bytes,
        NULL::text AS extra_size,
        100.0 - (pgstatindex(i.oid)).avg_leaf_density AS extra_ratio,
        coalesce(SUBSTRING(array_to_string(i.reloptions, ' ') FROM 'fillfactor=([0-9]+)')::smallint, 100) AS fillfactor,
        NULL::text AS bloat_size,
        NULL::numeric AS bloat_ratio,
        1::int AS is_na
    FROM pg_class t
    JOIN pg_namespace n ON n.oid = t.relnamespace
    JOIN pg_index ix ON ix.indrelid = t.oid
    JOIN pg_class i ON i.oid = ix.indexrelid
    WHERE t.relkind = 'r'
      AND pg_relation_size(i.oid) >= %s
    ORDER BY extra_ratio DESC, real_size_bytes DESC;
    """
    return fetch_dicts(cur, sql, (min_bytes,))

# ----------------- Main -----------------

def main():
    args = parse_args()

    if args.format in ("csv","json") and not args.outdir:
        print("ERROR: --outdir is required with --format=csv or --format=json", file=sys.stderr)
        sys.exit(2)

    try:
        conn = connect(args)
        conn.autocommit = True
    except Exception as e:
        print(f"Connection failed: {e}", file=sys.stderr)
        sys.exit(2)

    try:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            # Collect
            lrq = get_long_running_queries(cur, args.min_age_seconds, args.max_query_len)
            wrap_db = get_wraparound_db_list(cur)
            wrap_tbl = get_wraparound_top_tables(cur, args.top_wrap_tables)
            wrap_sum = get_wraparound_summary(cur)
            vac = get_vacuum_analyze_stats(cur)
            sizes = get_table_sizes(cur, args.min_size_bytes)
            licenses = get_licenses(cur)

            # Bloat with resilient fallback
            try:
                if args.bloat_method == "community":
                    try:
                        tbl_bloat = get_table_bloat_community(cur, args.min_size_bytes)
                        idx_bloat = get_index_bloat_community(cur, args.min_size_bytes)
                        bloat_mode = "community"
                    except Exception as e:
                        print(f"WARN: community bloat query failed: {e}", file=sys.stderr)
                        print("WARN: trying pgstattuple instead...", file=sys.stderr)
                        if ensure_pgstattuple(cur, create_if_missing=args.create_pgstattuple):
                            tbl_bloat = get_table_bloat_pgstattuple(cur, args.min_size_bytes)
                            idx_bloat = get_index_bloat_pgstattuple(cur, args.min_size_bytes)
                            bloat_mode = "pgstattuple"
                        else:
                            print("WARN: pgstattuple not available; skipping bloat.", file=sys.stderr)
                            tbl_bloat, idx_bloat, bloat_mode = [], [], "skipped"
                else:
                    if ensure_pgstattuple(cur, create_if_missing=args.create_pgstattuple):
                        tbl_bloat = get_table_bloat_pgstattuple(cur, args.min_size_bytes)
                        idx_bloat = get_index_bloat_pgstattuple(cur, args.min_size_bytes)
                        bloat_mode = "pgstattuple"
                    else:
                        print("NOTICE: pgstattuple not available; falling back to community.", file=sys.stderr)
                        tbl_bloat = get_table_bloat_community(cur, args.min_size_bytes)
                        idx_bloat = get_index_bloat_community(cur, args.min_size_bytes)
                        bloat_mode = "community"
            except Exception as e:
                print(f"WARN: all bloat methods failed: {e}", file=sys.stderr)
                tbl_bloat, idx_bloat, bloat_mode = [], [], "failed"

    finally:
        conn.close()

    # build collection metadata and stamp into each row set
    _collect_meta = {
        "pg_host": args.host,
        "pg_port": args.port,
        "pg_db": args.dbname,
        "cluster": getattr(args, "cluster", "") or "",
        "collected_at": datetime.now(timezone.utc).isoformat(),
    }
    lrq = stamp_rows(lrq, _collect_meta)
    wrap_db = stamp_rows(wrap_db, _collect_meta)
    wrap_tbl = stamp_rows(wrap_tbl, _collect_meta)
    wrap_sum = stamp_rows(wrap_sum, _collect_meta)
    vac = stamp_rows(vac, _collect_meta)
    sizes = stamp_rows(sizes, _collect_meta)
    licenses = stamp_rows(licenses, _collect_meta)
    tbl_bloat = stamp_rows(tbl_bloat, _collect_meta)
    idx_bloat = stamp_rows(idx_bloat, _collect_meta)

    payload = {
        "long_running_queries": lrq,
        "wraparound_database": wrap_db,
        "wraparound_top_tables": wrap_tbl,
        "wraparound_summary": wrap_sum,
        "vacuum_analyze_stats": vac,
        "table_sizes": sizes,
        "licenses": licenses,
        "table_bloat": tbl_bloat,
        "index_bloat": idx_bloat,
        "meta": {
            "bloat_method_effective": bloat_mode,
            "pg_host": args.host,
            "pg_port": args.port,
            "pg_db": args.dbname,
            "cluster": getattr(args, "cluster", "") or "",
            "collected_at": _collect_meta["collected_at"],
        }
    }

    if args.format == "json":
        os.makedirs(args.outdir, exist_ok=True)
        outp = os.path.join(args.outdir, "pg_health_check.json")
        export_json(outp, payload)
        print(f"Wrote JSON: {outp}")
        return

    if args.format == "csv":
        base = args.outdir.rstrip("/")
        os.makedirs(base, exist_ok=True)

        export_csv(f"{base}/long_running_queries.csv", lrq, [
            "pid","usename","application_name","client_addr","state","wait_event_type","wait_event",
            "backend_start","xact_start","query_start","runtime","query"
        ] + META_COLS)
        export_csv(f"{base}/wraparound_database.csv", wrap_db, [
            "datname","age","autovacuum_freeze_max_age","pct_of_max","pct_of_emergency_autovac"
        ] + META_COLS)
        export_csv(f"{base}/wraparound_top_tables.csv", wrap_tbl, [
            "schema","table","age","autovacuum_freeze_max_age","pct_of_max","pct_of_emergency_autovac","size_bytes"
        ] + META_COLS)
        export_csv(f"{base}/wraparound_summary.csv", wrap_sum, [
            "oldest_current_xid","percent_towards_wraparound","percent_towards_emergency_autovac"
        ] + META_COLS)
        export_csv(f"{base}/vacuum_analyze_stats.csv", vac, [
            "schema","table","n_live_tup","n_dead_tup","last_vacuum","last_autovacuum",
            "last_analyze","last_autoanalyze","vacuum_count","autovacuum_count","analyze_count","autoanalyze_count"
        ] + META_COLS)
        export_csv(f"{base}/table_sizes.csv", sizes, [
            "schema","table","table_bytes","indexes_bytes","toast_and_other_bytes","total_bytes"
        ] + META_COLS)
        export_csv(f"{base}/licenses.csv", licenses, [
            "db_id","index","key","original_key","expired","expiration_date","appliances",
            "server_agents","workstation_agents","cloud_drives","cloud_drives_lite","valid",
            "antivirus","varonis","key_manager","dlp","portal_license","vgateways4","vgateways8",
            "vgateways32","vgateways64","vgateways128","vgateways256","storage","comment","global_file_lock"
        ] + META_COLS)

        # Unified headers for both modes
        TABLE_BLOAT_HEADERS = [
            "current_database","schemaname","tblname",
            "real_size","real_size_bytes","extra_size","extra_ratio",
            "fillfactor","bloat_size","bloat_ratio","is_na"
        ]
        INDEX_BLOAT_HEADERS = [
            "schemaname","tblname","idxname",
            "real_size","real_size_bytes","extra_size","extra_ratio",
            "fillfactor","bloat_size","bloat_ratio","is_na"
        ]

        export_csv(f"{base}/table_bloat.csv", tbl_bloat, TABLE_BLOAT_HEADERS + META_COLS)
        export_csv(f"{base}/index_bloat.csv", idx_bloat, INDEX_BLOAT_HEADERS + META_COLS)

        print(f"Wrote CSVs under {base}/")
        return

    # Pretty-table fallback
    print("\n=== Long-running queries ===")
    print_table(lrq, ["pid","usename","application_name","client_addr","state",
                      "wait_event_type","wait_event","backend_start","xact_start","query_start","runtime","query"])
    print("\n=== Wraparound (DB) ===")
    print_table(wrap_db, ["datname","age","autovacuum_freeze_max_age","pct_of_max","pct_of_emergency_autovac"])
    print("\n=== Wraparound (Top tables) ===")
    print_table(wrap_tbl, ["schema","table","age","autovacuum_freeze_max_age","pct_of_max","pct_of_emergency_autovac","size_bytes"])
    print("\n=== Wraparound summary ===")
    print_table(wrap_sum, ["oldest_current_xid","percent_towards_wraparound","percent_towards_emergency_autovac"])
    print("\n=== Last VACUUM / ANALYZE per table ===")
    print_table(vac, ["schema","table","n_live_tup","n_dead_tup","last_vacuum","last_autovacuum",
                      "last_analyze","last_autoanalyze","vacuum_count","autovacuum_count","analyze_count","autoanalyze_count"])
    print("\n=== Table sizes ===")
    print_table(sizes, ["schema","table","table_bytes","indexes_bytes","toast_and_other_bytes","total_bytes"])
    print("\n=== Licenses ===")
    print_table(licenses, ["index","key","expired","expiration_date","appliances","server_agents","workstation_agents","cloud_drives","valid","portal_license"])
    print("\n=== Table bloat ===")
    if tbl_bloat and 'tblname' in tbl_bloat[0]:
        print_table(tbl_bloat, ["current_database","schemaname","tblname","real_size","real_size_bytes",
                                "extra_size","extra_ratio","fillfactor","bloat_size","bloat_ratio","is_na"])
    else:
        print("(no bloat rows)")
    print("\n=== Index bloat ===")
    if idx_bloat and 'idxname' in idx_bloat[0]:
        print_table(idx_bloat, ["schemaname","tblname","idxname","real_size","real_size_bytes",
                                "extra_size","extra_ratio","fillfactor","bloat_size","bloat_ratio","is_na"])
    else:
        print("(no index bloat rows)")

if __name__ == "__main__":
    main()
