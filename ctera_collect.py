#!/usr/bin/env python3
# t2_singlec.py  — filers + servers + storage nodes + server tasks
# Modes:
#   --mode filers    → filer_status_min.csv-style export
#   --mode servers   → portal servers
#   --mode storage   → storage nodes (buckets) with Driver + DirectIO
#   --mode infra     → unified CSV with both servers + storage nodes
#   --mode tasks     → background tasks per server to CSV
#
# Notes:
# - In "tasks" mode, default outfile becomes "tasks.csv" if the default "output.csv" wasn't overridden.
# - Tasks are gathered per server via admin.servers.tasks.background(<server_name>).

import argparse
import ast
import csv
import json
import logging
import os
import re
import socket
import ssl
import subprocess
import sys
import psycopg2
from urllib.parse import urlparse

from cterasdk.exceptions import CTERAException
from cterasdk import GlobalAdmin, ServicesPortal
import cterasdk.settings
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout
import time


# -------------------- Filer helpers --------------------
def get_filer(self, device=None, tenant=None):
    try:
        return self.devices.device(device, tenant)
    except CTERAException as error:
        logging.debug(error)
        logging.error("Device not found.")
        return None

# --- helpers (put once near the top of your file) ---
from datetime import datetime, timezone

TELNET_SECRET_HELPER = os.environ.get(
    "CTERA_TELNET_SECRET_HELPER",
    "/usr/local/bin/ctera-secret-helper",
)

def _g(obj, *names, default=""):
    """get first existing attribute from names"""
    for n in names:
        if hasattr(obj, n):
            v = getattr(obj, n)
            if v is not None:
                return v
    return default

def _to_iso(v):
    if not v:
        return ""
    # already ISO?
    if isinstance(v, str):
        return v
    if isinstance(v, datetime):
        if v.tzinfo is None:
            v = v.replace(tzinfo=timezone.utc)
        return v.isoformat()
    return str(v)  # last-resort
# ----------------------------------------------------



# One shared pool is fine; these are short tasks
_EXECUTOR = ThreadPoolExecutor(max_workers=8)

def _with_timeout(timeout_sec, label, fn):
    fut = _EXECUTOR.submit(fn)
    try:
        return fut.result(timeout=timeout_sec)
    except FuturesTimeout:
        logging.warning("%s timed out after %ss", label, timeout_sec)
        raise TimeoutError(f"{label} timed out after {timeout_sec}s")


# --- helpers (put near the top of the file once) ---
def _reauth(sess):
    user = getattr(sess, "_featherdash_user", None)
    password = getattr(sess, "_featherdash_password", None)
    global_admin = getattr(sess, "_featherdash_global_admin", False)
    if not user or password is None:
        logging.warning("Re-auth failed: original login credentials are not available.")
        return False
    try:
        sess.login(user, password)
        if global_admin:
            sess.portals.browse_global_admin()
        logging.info("Re-authenticated.")
        return True
    except Exception as e2:
        logging.warning("Re-auth failed: %s", e2)
        return False

def _with_reauth(sess, op, *, retries=1, label=""):
    for attempt in range(retries + 1):
        try:
            return op()
        except Exception as e:
            msg = str(e)
            if "Session expired" in msg and attempt < retries:
                logging.info("Session expired during %s. Re-authenticating and retrying...", label or op.__name__)
                if _reauth(sess):
                    continue
            raise

def _ensure_session_alive(sess):
    try:
        _ = sess.users.session().current_tenant()
    except Exception:
        _reauth(sess)
# --- end helpers ---


def _derive_telnet_secret(mac_addr, firmware):
    mac_addr = str(mac_addr or "").strip()
    firmware = str(firmware or "").strip()
    if not mac_addr or not firmware:
        raise ValueError("mac address and firmware are required")

    helper_path = os.environ.get("CTERA_TELNET_SECRET_HELPER", TELNET_SECRET_HELPER).strip() or TELNET_SECRET_HELPER
    if not os.path.exists(helper_path):
        raise FileNotFoundError(
            f"Telnet secret helper not found at {helper_path}. "
            "Re-run install/upgrade so the helper can be installed."
        )

    try:
        result = subprocess.run(
            [helper_path, "--mac", mac_addr, "--firmware", firmware],
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        )
    except subprocess.CalledProcessError as exc:
        stderr = (exc.stderr or "").strip()
        raise RuntimeError(
            f"Telnet secret helper failed with exit code {exc.returncode}: {stderr or 'no stderr'}"
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise TimeoutError("Telnet secret helper timed out after 10s") from exc

    secret = (result.stdout or "").strip()
    if not secret:
        raise RuntimeError("Telnet secret helper returned an empty secret")
    return secret



def get_filers(self, all_tenants=False, tenant=None):
    try:
        connected_filers = []
        if all_tenants:
            _with_reauth(self, lambda: self.portals.browse_global_admin(), retries=2, label="browse_global_admin")
            logging.info("Getting all Filers (all tenants)")
            tenants = _with_reauth(self, lambda: list(self.portals.tenants()), retries=2, label="list_tenants")
            for t in tenants:
                tenant_name = getattr(t, "name", "")
                try:
                    _with_reauth(self, lambda: self.portals.browse(tenant_name), retries=2, label=f"browse_tenant:{tenant_name}")
                    all_filers = _with_reauth(
                        self,
                        lambda: self.devices.filers(include=[
                            'deviceConnectionStatus.connected',
                            'deviceReportedStatus.config.hostname'
                        ]),
                        retries=2,
                        label=f"list_filers:{tenant_name}"
                    )
                    tenant_connected = [
                        f for f in (all_filers or [])
                        if getattr(getattr(f, "deviceConnectionStatus", None), "connected", False)
                    ]
                    connected_filers.extend(tenant_connected)
                    logging.info("Tenant %s: collected %s connected filers", tenant_name, len(tenant_connected))
                except Exception as tenant_error:
                    logging.warning("Skipping tenant %s during filer discovery: %s", tenant_name or "Unknown", tenant_error)
                    _reauth(self)
                    continue
        elif tenant is not None:
            logging.info("Getting Filers connected to %s", tenant)
            _with_reauth(self, lambda: self.portals.browse(tenant), retries=2, label=f"browse_tenant:{tenant}")
            tenant_filers = _with_reauth(
                self,
                lambda: self.devices.filers(include=[
                    'deviceConnectionStatus.connected',
                    'deviceReportedStatus.config.hostname'
                ]),
                retries=2,
                label=f"list_filers:{tenant}"
            )
            connected_filers.extend([f for f in tenant_filers if getattr(getattr(f, "deviceConnectionStatus", None), "connected", False)])
        else:
            try:
                current_tenant = self.users.session().current_tenant()
            except Exception:
                current_tenant = None
            logging.info("Getting Filers connected%s", f" to {current_tenant}" if current_tenant else "")
            tenant_filers = _with_reauth(
                self,
                lambda: self.devices.filers(include=[
                    'deviceConnectionStatus.connected',
                    'deviceReportedStatus.config.hostname'
                ]),
                retries=2,
                label="list_filers_current_tenant"
            )
            connected_filers.extend([f for f in tenant_filers if getattr(getattr(f, "deviceConnectionStatus", None), "connected", False)])
        logging.info("Discovered %s connected filers total", len(connected_filers))
        return connected_filers
    except CTERAException as error:
        logging.debug(error)
        logging.error("Error getting Filers.")
        return None
    except Exception as error:
        logging.warning("Unexpected error getting Filers: %s", error)
        return []

def _ensure_session_alive(self):
    """Lightweight poke; if session is gone, re-browse GA to refresh it."""
    try:
        # any cheap call that needs a valid session
        _ = self.users.session().current_tenant()
    except Exception as e:
        logging.info("Session looks expired (%s). Re-initializing context...", e)
        _reauth(self)


# -------------------- Filers CSV --------------------
def write_status(self, p_filename, all_tenants):
    get_list = ['config', 'status', 'proc/cloudsync', 'proc/time/', 'proc/storage/summary', 'proc/perfMonitor']
    logging.info("Gathering status for all filers...")

    # ---------- per-call timeouts + wrappers (signal-based, no threads) ----------
    import signal
    import time
    from contextlib import contextmanager

    # tune these in seconds
    TIMEOUT_API   = 12
    TIMEOUT_CLI   = 8
    TIMEOUT_SHELL = 8
    TIMEOUT_TEL   = 5
    BUDGET_PER_FILER = 30  # hard ceiling per filer

    @contextmanager
    def _timeout_after(seconds, label):
        # Use POSIX timer so we don't spawn threads
        def _handler(signum, frame):
            raise TimeoutError(f"{label} timed out after {seconds}s")
        prev_handler = signal.getsignal(signal.SIGALRM)
        signal.signal(signal.SIGALRM, _handler)
        # ITIMER_REAL uses real time, delivers SIGALRM
        signal.setitimer(signal.ITIMER_REAL, seconds)
        try:
            yield
        finally:
            # clear timer and restore handler
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, prev_handler)

    def _with_timeout(seconds, label, fn):
        with _timeout_after(seconds, label):
            return fn()

    def api_get_multi_safe(self, filer, path, lst, label="get_multi"):
        return _with_timeout(
            TIMEOUT_API, label,
            lambda: _with_reauth(self, lambda: filer.api.get_multi(path, lst), retries=3, label=label)
        )

    def cli_safe(self, filer, cmd):
        lab = f"cli:{cmd}"
        try:
            result = _with_timeout(
                TIMEOUT_CLI, lab,
                lambda: _with_reauth(self, lambda: filer.cli.run_command(cmd), retries=2, label=lab)
            )
            if isinstance(result, str):
                return result
            if hasattr(result, "value"):
                return str(result.value)
            if hasattr(result, "text"):
                return str(result.text)
            return str(result) if result is not None else "Not Applicable"
        except AttributeError:
            return "Not Applicable"
        except Exception as e:
            logging.debug("CLI command failed for %s: %s, error: %s", getattr(filer, "name", "?"), cmd, e)
            return "Not Applicable"

    def telnet_enable_safe(self, filer, secret):
        return _with_timeout(
            TIMEOUT_TEL, "telnet.enable",
            lambda: _with_reauth(self, lambda: filer.telnet.enable(secret), retries=2, label="telnet.enable")
        )

    def telnet_disable_safe(self, filer):
        return _with_timeout(
            TIMEOUT_TEL, "telnet.disable",
            lambda: _with_reauth(self, lambda: filer.telnet.disable(), retries=2, label="telnet.disable")
        )

    def shell_safe(self, filer, cmd):
        lab = f"shell:{cmd.split()[0]}"
        result = _with_timeout(
            TIMEOUT_SHELL, lab,
            lambda: _with_reauth(self, lambda: filer.shell.run_command(cmd), retries=2, label=lab)
        )
        if isinstance(result, str):
            return result
        if hasattr(result, "value"):
            return str(result.value)
        if hasattr(result, "text"):
            return str(result.text)
        return str(result) if result is not None else ""

    # ---------- your existing loop, now using the signal timeouts + per-filer budget ----------
    for filer in (get_filers(self, all_tenants) or []):
        try:
            start = time.monotonic()
            _ensure_session_alive(self)

            logging.info(f"Gathering status for {getattr(filer, 'name', '?')}...")

            def _budget_ok():
                return (time.monotonic() - start) < BUDGET_PER_FILER

            info = api_get_multi_safe(self, filer, '/', get_list, label="get_multi")

            # tenant label (timed)
            try:
                tenant = getattr(filer, 'portal', None) or _with_timeout(
                    4, "current_tenant",
                    lambda: _with_reauth(self, lambda: self.users.session().current_tenant(), label="current_tenant")
                )
            except Exception:
                tenant = 'Unknown'

            sync_id = info.proc.cloudsync.serviceStatus.id
            try:
                selfScanIntervalInHours = info.config.cloudsync.selfScanVerificationIntervalInHours
            except AttributeError:
                selfScanIntervalInHours = 'Not Applicable'
            uploadingFiles = info.proc.cloudsync.serviceStatus.uploadingFiles
            scanningFiles = info.proc.cloudsync.serviceStatus.scanningFiles
            try:
                selfVerificationscanningFiles = info.proc.cloudsync.serviceStatus.selfVerificationScanningFiles
            except AttributeError:
                selfVerificationscanningFiles = 'Not Applicable'
            CurrentFirmware = info.status.device.runningFirmware
            try:
                MetaLogMaxSize = info.config.logging.metalog.maxFileSizeMB
            except AttributeError:
                try:
                    MetaLogMaxSize = info.config.logging.log2File.maxFileSizeMB
                except AttributeError:
                    MetaLogMaxSize = 'Not Applicable'
            try:
                MetaLogMaxFiles = info.config.logging.metalog.maxfiles
            except AttributeError:
                try:
                    MetaLogMaxFiles = info.config.logging.log2File.maxfiles
                except AttributeError:
                    MetaLogMaxFiles = 'Not Applicable'
            try:
                AuditLogsStatus = cli_safe(self, filer, 'show /config/logging/files/mode')
            except AttributeError:
                AuditLogsStatus = 'Not Applicable'
            try:
                DeviceLocation = cli_safe(self, filer, 'show /config/device/location')
            except AttributeError:
                DeviceLocation = 'Not Applicable'
            try:
                AuditLogsPath = cli_safe(self, filer, 'show /config/logging/files/path')
            except AttributeError:
                AuditLogsPath = 'Not Applicable'
            try:
                MetaLogs = cli_safe(self, filer, 'dbg level')
                MetaLogs1 = MetaLogs[-28:-18]
            except AttributeError:
                MetaLogs1 = 'Not Applicable'
            try:
                ad_mapping = cli_safe(self, filer, 'show /config/fileservices/cifs/idMapping/map')
            except AttributeError:
                ad_mapping = 'Not Applicable'
            License = info.config.license if hasattr(info.config, 'license') else 'Not Applicable'
            SN = safe_attr(info, 'status.device.SerialNumber')
            MAC = first_scalar(safe_attr(info, 'status.device.MacAddress'))
            try:
                IP1 = info.status.network.ports[0].ip.address
                DNS1 = info.status.network.ports[0].ip.DNSServer1
                DNS2 = info.status.network.ports[0].ip.DNSServer2
            except (AttributeError, IndexError, TypeError):
                IP1 = DNS1 = DNS2 = 'N/A'
            try:
                storageThresholdPercentTrigger = info.config.cloudsync.cloudExtender.storageThresholdPercentTrigger
            except AttributeError:
                storageThresholdPercentTrigger = 'Not Applicable'
            uptime = safe_attr(info, 'proc.time.uptime')
            try:
                curr_cpu = info.proc.perfMonitor.current.cpu
                curr_mem = info.proc.perfMonitor.current.memUsage
                logging.info(
                    "SDK perf for %s: curr_cpu=%s curr_mem=%s",
                    getattr(filer, 'name', '?'),
                    curr_cpu,
                    curr_mem,
                )
            except (AttributeError, TypeError):
                curr_cpu = 'N/A'
                curr_mem = 'N/A'
                logging.info("SDK perf missing for %s; shell fallback may be used.", getattr(filer, 'name', '?'))
            _total = safe_attr(info, 'proc.storage.summary.totalVolumeSpace')
            _used = safe_attr(info, 'proc.storage.summary.usedVolumeSpace')
            _free = safe_attr(info, 'proc.storage.summary.freeVolumeSpace')
            volume = (f"Total: {_total} Used: {_used} Free: {_free}")
            Alerts = safe_attr(info, 'config.logging.alert')
            TimeServer = safe_attr(info, 'config.time', default=None)
            _mode = safe_attr(TimeServer, 'NTPMode')
            _zone = safe_attr(TimeServer, 'TimeZone')
            _servers = safe_attr(TimeServer, 'NTPServer')
            time_s = (f"Mode: {_mode} Zone: {_zone} Servers: {_servers}")

            def get_max_cpu():
                try:
                    samples = getattr(info.proc.perfMonitor, 'samples', None)
                    if samples is None:
                        return 'N/A'
                    cpu_history = [i.cpu for i in samples]
                    max_cpu = format(max(cpu_history))
                    return f"{max_cpu}%"
                except (AttributeError, TypeError, ValueError):
                    return 'N/A'

            def get_max_memory():
                try:
                    samples = getattr(info.proc.perfMonitor, 'samples', None)
                    if samples is None:
                        return 'N/A'
                    memory_history = [i.memUsage for i in samples]
                    max_memory = format(max(memory_history))
                    return f"{max_memory}%"
                except (AttributeError, TypeError, ValueError):
                    return 'N/A'

            def _format_pct(value):
                try:
                    return f"{float(value):.1f}%"
                except (TypeError, ValueError):
                    return 'N/A'

            def _display_pct(value):
                value = str(value or '').strip()
                if not value or value.upper() == 'N/A':
                    return 'N/A'
                if value.lower() == 'unsupported':
                    return 'Unsupported'
                return value if value.endswith('%') else f"{value}%"

            def _extract_first_number(text):
                if isinstance(text, (int, float)):
                    return str(text)
                m = re.search(r'([0-9]+(?:\.[0-9]+)?)', text or '')
                return m.group(1) if m else ''

            def get_shell_fallback_metrics():
                if not _budget_ok():
                    logging.warning("Per-filer budget exceeded before shell fallback; skipping.")
                    return {}

                mac_addr = first_scalar(safe_attr(info, 'status.device.MacAddress', default=''))
                firmware = first_scalar(safe_attr(info, 'status.device.runningFirmware', default=''))
                if not mac_addr or not firmware:
                    return {}

                secret = _derive_telnet_secret(mac_addr, firmware)
                metrics = {}

                def _run_numeric(cmd):
                    out = shell_safe(self, filer, cmd)
                    return _extract_first_number(out)

                try:
                    logging.info("Starting shell fallback for %s", getattr(filer, 'name', '?'))
                    telnet_enable_safe(self, filer, secret)
                    logging.info("Telnet enabled for %s", getattr(filer, 'name', '?'))
                    try:
                        db_bytes = _run_numeric('for f in /var/volumes/*/.ctera/cloudSync/CloudSync.db; do [ -f "$f" ] && stat -c %s "$f" && break; done')
                        if not db_bytes:
                            db_bytes = _run_numeric("""for f in /var/volumes/*/.ctera/cloudSync/CloudSync.db; do [ -f "$f" ] && ls -ln "$f" 2>/dev/null | awk 'NR==1 {print $5; exit}' && break; done""")
                        if db_bytes:
                            metrics['db_size'] = round(int(float(db_bytes)) / (1 << 30), 2)
                        logging.info("Shell fallback DB size for %s: raw=%s parsed=%s", getattr(filer, 'name', '?'), db_bytes or 'N/A', metrics.get('db_size', 'N/A'))

                        cpu_now = _run_numeric("""sar -u 1 1 2>/dev/null | awk 'BEGIN{col=0;found=0} /%idle/ {for(i=1;i<=NF;i++) if($i=="%idle") col=i} col && $1 ~ /^[0-9:]+$/ && $(col) ~ /^[0-9.]+$/ {last=100-$(col); found=1} END {if(found) printf "%.1f", last}'""")
                        if not cpu_now:
                            cpu_now = _run_numeric("""top -bn1 2>/dev/null | awk '/^%?Cpu/ {for(i=1;i<=NF;i++) if($i ~ /^id,?$/) {idle=$(i-1); gsub(/[% ,]/, "", idle); if(idle!="") printf "%.1f", 100-idle}}'""")
                        if cpu_now:
                            metrics['curr_cpu'] = _format_pct(cpu_now)
                        logging.info("Shell fallback current CPU for %s: raw=%s parsed=%s", getattr(filer, 'name', '?'), cpu_now or 'N/A', metrics.get('curr_cpu', 'N/A'))

                        mem_now = _run_numeric("""sar -r 1 1 2>/dev/null | awk 'BEGIN{col=0;found=0} /%memused/ {for(i=1;i<=NF;i++) if($i=="%memused") col=i} col && $1 ~ /^[0-9:]+$/ && $(col) ~ /^[0-9.]+$/ {last=$(col); found=1} END {if(found) printf "%.1f", last}'""")
                        if not mem_now:
                            mem_now = _run_numeric("""free 2>/dev/null | awk '/Mem:/ {if ($2 > 0) printf "%.1f", ($3/$2)*100}'""")
                        if not mem_now:
                            mem_now = _run_numeric("""awk '/MemTotal:/ {t=$2} /MemAvailable:/ {a=$2} END {if (t > 0 && a >= 0) printf "%.1f", 100-((a/t)*100)}' /proc/meminfo 2>/dev/null""")
                        if mem_now:
                            metrics['curr_mem'] = _format_pct(mem_now)
                        logging.info("Shell fallback current memory for %s: raw=%s parsed=%s", getattr(filer, 'name', '?'), mem_now or 'N/A', metrics.get('curr_mem', 'N/A'))

                        max_cpu = _run_numeric("""sar -u 2>/dev/null | awk 'BEGIN{col=0;found=0} /%idle/ {for(i=1;i<=NF;i++) if($i=="%idle") col=i} col && $1 ~ /^[0-9:]+$/ && $(col) ~ /^[0-9.]+$/ {v=100-$(col); if(!found || v>max) max=v; found=1} END {if(found) printf "%.1f", max}'""")
                        if not max_cpu:
                            max_cpu = cpu_now
                        if max_cpu:
                            metrics['max_cpu'] = _format_pct(max_cpu)
                        logging.info("Shell fallback max CPU for %s: raw=%s parsed=%s", getattr(filer, 'name', '?'), max_cpu or 'N/A', metrics.get('max_cpu', 'N/A'))

                        max_mem = _run_numeric("""sar -r 2>/dev/null | awk 'BEGIN{col=0;found=0} /%memused/ {for(i=1;i<=NF;i++) if($i=="%memused") col=i} col && $1 ~ /^[0-9:]+$/ && $(col) ~ /^[0-9.]+$/ {v=$(col); if(!found || v>max) max=v; found=1} END {if(found) printf "%.1f", max}'""")
                        if not max_mem:
                            max_mem = mem_now
                        if max_mem:
                            metrics['max_mem'] = _format_pct(max_mem)
                        logging.info("Shell fallback max memory for %s: raw=%s parsed=%s", getattr(filer, 'name', '?'), max_mem or 'N/A', metrics.get('max_mem', 'N/A'))
                    finally:
                        try:
                            telnet_disable_safe(self, filer)
                            logging.info("Telnet disabled for %s", getattr(filer, 'name', '?'))
                        except Exception:
                            pass
                except TimeoutError as te:
                    logging.warning("Shell fallback timed out for %s: %s", getattr(filer, 'name', '?'), te)
                except Exception as e:
                    reason = str(e).strip() or e.__class__.__name__
                    logging.warning(
                        "Shell fallback failed for %s: %s",
                        getattr(filer, 'name', '?'),
                        reason,
                    )
                    _ensure_session_alive(self)

                return metrics

            def get_ad_status(result=None):
                if result is None:
                    result = safe_attr(info, 'status.fileservices.cifs.joinStatus')
                if result == 0:
                    return 'Ok'
                if result == -1:
                    return 'Workgroup'
                if result == 2:
                    return 'Failed'
                return result

            max_cpu_value = get_max_cpu()
            max_mem_value = get_max_memory()
            shell_metrics = get_shell_fallback_metrics()
            if curr_cpu == 'N/A' and shell_metrics.get('curr_cpu'):
                curr_cpu = shell_metrics['curr_cpu'].rstrip('%')
            if curr_mem == 'N/A' and shell_metrics.get('curr_mem'):
                curr_mem = shell_metrics['curr_mem'].rstrip('%')
            if max_cpu_value == 'N/A' and shell_metrics.get('max_cpu'):
                max_cpu_value = shell_metrics['max_cpu']
            if max_mem_value == 'N/A' and shell_metrics.get('max_mem'):
                max_mem_value = shell_metrics['max_mem']
            db_size_value = shell_metrics.get('db_size')
            if db_size_value in ("", None):
                db_size_value = 'N/A'
            logging.info(
                "Final filer metrics for %s: current_perf='%s' max_cpu=%s max_mem=%s db_size=%s",
                getattr(filer, 'name', '?'),
                f"CPU: {_display_pct(curr_cpu)} Mem: {_display_pct(curr_mem)}",
                max_cpu_value,
                max_mem_value,
                db_size_value,
            )

            if not _budget_ok():
                raise TimeoutError(f"Per-filer budget {BUDGET_PER_FILER}s exceeded")

            with open(p_filename, mode='a', newline='', encoding="utf-8-sig") as f:
                w = csv.writer(f, dialect='excel', delimiter=',', quotechar='"', quoting=csv.QUOTE_MINIMAL)
                w.writerow([
                    tenant,
                    getattr(filer, 'name', '?'),
                    sync_id,
                    selfScanIntervalInHours,
                    uploadingFiles,
                    scanningFiles,
                    selfVerificationscanningFiles,
                    MetaLogs1,
                    AuditLogsStatus,
                    DeviceLocation,
                    AuditLogsPath,
                    MetaLogMaxSize,
                    MetaLogMaxFiles,
                    CurrentFirmware,
                    License,
                    storageThresholdPercentTrigger,
                    volume,
                    SN,
                    MAC,
                    IP1,
                    DNS1,
                    DNS2,
                    get_ad_status(),
                    ad_mapping,
                    Alerts,
                    time_s,
                    uptime,
                    f"CPU: {_display_pct(curr_cpu)} Mem: {_display_pct(curr_mem)}",
                    max_cpu_value,
                    max_mem_value,
                    db_size_value
                ])

        except Exception as e:
            logging.warning("Skipping filer %s due to error: %s", getattr(filer, 'name', '?'), e)
            try:
                telnet_disable_safe(self, filer)  # timed cleanup
            except Exception:
                pass
            _ensure_session_alive(self)
            continue





    
 
def write_filers_header(p_filename):
    try:
        with open(p_filename, mode='a', newline='', encoding="utf-8-sig") as f:
            w = csv.writer(f, dialect='excel', delimiter=',', quotechar='"', quoting=csv.QUOTE_MINIMAL)
            w.writerow(['Tenant','Filer Name','CloudSync Status','selfScanIntervalInHours','uploadingFiles','scanningFiles','selfVerificationscanningFiles','MetaLogsSetting','AuditLogsStatus','DeviceLocation','AuditLogsPath','MetaLogMaxSize','MetaLogMaxFiles','CurrentFirmware','License','EvictionPercentage','CurrentVolumeStorage','SN','MAC','IP Config','DNS Server1','DNS Server2','AD Domain Status','AD Mapping','Alerts','TimeServer','uptime','Current Performance','Max CPU','Max Memory','DB Size'])
    except FileNotFoundError as error:
        logging.error(error)
        sys.exit("Make sure you entered a valid file name and it exists")


def run_filers(self, filename, all_tenants):
    logging.info('Starting filers task')
    if os.path.exists(filename):
        logging.info('Appending to existing file.')
    else:
        write_filers_header(filename)
    try:
        write_status(self, filename, all_tenants)
    except Exception as e:
        logging.warning("An error occurred: " + str(e))
    logging.info('Finished filers task.')


# -------------------- Servers CSV --------------------
SERVER_FIELDS = ['name', 'connected', 'isApplicationServer', 'mainDB']


CERTIFICATE_HEADERS = ['Name', 'Host', 'Port', 'SubjectCN', 'IssuerCN', 'NotBefore', 'NotAfter', 'CertDaysLeft', 'Status', 'Error']


def _normalize_tls_target(host_value):
    raw = str(host_value or "").strip()
    if not raw:
        return "", 443
    parsed = urlparse(raw if "://" in raw else f"https://{raw}")
    host = parsed.hostname or raw.split("/")[0].split(":")[0].strip()
    port = parsed.port or 443
    return host, port


def _flatten_dn(parts):
    items = []
    for seq in parts or []:
        for key, value in seq:
            items.append((str(key or ""), str(value or "")))
    return items


def _find_dn_value(parts, wanted):
    wanted = str(wanted or "").strip().lower()
    for key, value in _flatten_dn(parts):
        if key.strip().lower() == wanted:
            return value
    return ""


def _fetch_certificate_details(host_value):
    host, port = _normalize_tls_target(host_value)
    if not host:
        return {
            "Name": "Portal Endpoint",
            "Host": "",
            "Port": "",
            "SubjectCN": "",
            "IssuerCN": "",
            "NotBefore": "",
            "NotAfter": "",
            "CertDaysLeft": "",
            "Status": "Error",
            "Error": "Missing portal host",
        }

    sock = None
    wrapped = None
    try:
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        sock = socket.create_connection((host, port), timeout=10)
        wrapped = context.wrap_socket(sock, server_hostname=host)
        cert = wrapped.getpeercert()
        if not cert:
            raise RuntimeError("No certificate returned")

        not_before = cert.get("notBefore") or ""
        not_after = cert.get("notAfter") or ""
        nb_dt = datetime.utcfromtimestamp(ssl.cert_time_to_seconds(not_before)).replace(tzinfo=timezone.utc) if not_before else None
        na_dt = datetime.utcfromtimestamp(ssl.cert_time_to_seconds(not_after)).replace(tzinfo=timezone.utc) if not_after else None
        now = datetime.now(timezone.utc)
        days_left = ""
        status = "OK"
        if na_dt is not None:
            days_left = str((na_dt - now).days)
            if (na_dt - now).days < 0:
                status = "Expired"
            elif (na_dt - now).days <= 30:
                status = "ExpiringSoon"

        return {
            "Name": "Portal Endpoint",
            "Host": host,
            "Port": str(port),
            "SubjectCN": _find_dn_value(cert.get("subject"), "commonName"),
            "IssuerCN": _find_dn_value(cert.get("issuer"), "commonName"),
            "NotBefore": nb_dt.isoformat() if nb_dt else "",
            "NotAfter": na_dt.isoformat() if na_dt else "",
            "CertDaysLeft": days_left,
            "Status": status,
            "Error": "",
        }
    except Exception as exc:
        return {
            "Name": "Portal Endpoint",
            "Host": host,
            "Port": str(port or 443),
            "SubjectCN": "",
            "IssuerCN": "",
            "NotBefore": "",
            "NotAfter": "",
            "CertDaysLeft": "",
            "Status": "Error",
            "Error": str(exc),
        }
    finally:
        try:
            if wrapped is not None:
                wrapped.close()
        except Exception:
            pass
        try:
            if sock is not None:
                sock.close()
        except Exception:
            pass


def write_certificate_header(filename):
    with open(filename, mode='a', newline='', encoding='utf-8-sig') as f:
        w = csv.writer(f, dialect='excel', delimiter=',', quotechar='"', quoting=csv.QUOTE_MINIMAL)
        w.writerow(CERTIFICATE_HEADERS)


def write_certificate(self, filename):
    details = _fetch_certificate_details(getattr(self, "_featherdash_tls_host", "") or getattr(self, "_baseurl", "") or "")
    with open(filename, mode='a', newline='', encoding='utf-8-sig') as f:
        w = csv.writer(f, dialect='excel', delimiter=',', quotechar='"', quoting=csv.QUOTE_MINIMAL)
        w.writerow([details.get(h, "") for h in CERTIFICATE_HEADERS])
    logging.info("Wrote portal certificate CSV to %s", filename)


def run_certificate(self, filename):
    logging.info('Starting portal certificate task')
    if os.path.exists(filename):
        logging.info('Appending to existing file.')
    else:
        write_certificate_header(filename)
    try:
        write_certificate(self, filename)
    except Exception as e:
        logging.warning("An error occurred: " + str(e))
    logging.info('Finished portal certificate task.')


def write_servers_header(filename):
    with open(filename, mode='a', newline='', encoding='utf-8-sig') as f:
        w = csv.writer(f, dialect='excel', delimiter=',', quotechar='"', quoting=csv.QUOTE_MINIMAL)
        w.writerow(['Name', 'Connected', 'IsApplicationServer', 'IsMainDB'])


def write_servers(self, filename):
    logging.info("Collecting Portal servers from Global Admin...")
    self.portals.browse_global_admin()
    servers = self.servers.list_servers(include=SERVER_FIELDS)
    with open(filename, mode='a', newline='', encoding='utf-8-sig') as f:
        w = csv.writer(f, dialect='excel', delimiter=',', quotechar='"', quoting=csv.QUOTE_MINIMAL)
        for s in servers:
            w.writerow([
                getattr(s, 'name', ''),
                getattr(s, 'connected', ''),
                getattr(s, 'isApplicationServer', ''),
                getattr(s, 'mainDB', ''),
            ])
    logging.info("Wrote servers CSV to %s", filename)


def run_servers(self, filename):
    logging.info('Starting servers task')
    if os.path.exists(filename):
        logging.info('Appending to existing file.')
    else:
        write_servers_header(filename)
    try:
        write_servers(self, filename)
    except Exception as e:
        logging.warning("An error occurred: " + str(e))
    logging.info('Finished servers task.')


# -------------------- Storage Nodes (Buckets) CSV --------------------
BUCKET_FIELDS = ['name', 'bucket', 'readOnly', 'dedicatedTo']  # 'direct' is fetched per-bucket with get()

# Map SDK bucket classes to friendly provider names
BUCKET_CLASS_MAP = {
    'AmazonS3': 'Amazon S3',
    'AzureBlob': 'Azure Blob',
    'GenericS3': 'S3-Compatible',
    'Wasabi': 'Wasabi',
    'NetAppStorageGRID': 'StorageGRID',
    'Google': 'Google Cloud Storage',
    'ICOS': 'IBM COS',
    'HTTPBucket': 'HTTP',
    'FileSystem': 'Local Filesystem',
}

LOCATION_STORAGE_MAP = {
    'S3': 'Amazon S3',
    'AmazonS3': 'Amazon S3',
    'Azure': 'Azure Blob',
    'AzureBlob': 'Azure Blob',
    'FS': 'Local Filesystem',
    'WasabiS3': 'Wasabi',
    'ScalityS3': 'Scality S3',
    'Scality': 'Scality S3',
    'Wasabi': 'Wasabi',
    'Google': 'Google Cloud Storage',
    'ICOS': 'IBM COS',
    'HTTP': 'HTTP',
    'FileSystem': 'Local Filesystem',
    'LocalFilesystem': 'Local Filesystem',
}

def _obj_get(obj, name, default=""):
    if isinstance(obj, dict):
        value = obj.get(name, default)
    else:
        value = getattr(obj, name, default)
    return default if value is None else value

def _extract_query_objects(payload):
    if payload is None:
        return []
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except Exception:
            return []
    if isinstance(payload, dict):
        objs = payload.get("objects")
        return objs if isinstance(objs, list) else []
    objs = getattr(payload, "objects", None)
    if isinstance(objs, list):
        return objs
    if hasattr(objs, "__iter__") and not isinstance(objs, (str, bytes)):
        return list(objs)
    if isinstance(payload, list):
        return payload
    return []

def _parse_portal_name(ref):
    ref = str(ref or "").strip()
    if not ref:
        return ""
    return ref.rstrip("/").split("/")[-1]

def _resolve_location_driver(location):
    storage = str(_obj_get(location, "storage", "") or "")
    if storage:
        return LOCATION_STORAGE_MAP.get(storage, storage)
    clsname = type(location).__name__
    if clsname and clsname not in ("dict",):
        return LOCATION_STORAGE_MAP.get(clsname, clsname)
    return ""

def _get_storage_locations_from_postgres():
    pg_host = str(os.environ.get("LOCAL_PGHOST") or os.environ.get("PGHOST") or "").strip()
    pg_port = str(os.environ.get("LOCAL_PGPORT") or os.environ.get("PGPORT") or "5432").strip() or "5432"
    pg_database = str(os.environ.get("PGDATABASE") or "postgres").strip() or "postgres"
    pg_user = str(os.environ.get("PGUSER") or "postgres").strip() or "postgres"
    pg_password = str(os.environ.get("PGPASSWORD") or "").strip()

    if not pg_host or not pg_password:
        return []

    conn = None
    try:
        conn = psycopg2.connect(
            host=pg_host,
            port=pg_port,
            dbname=pg_database,
            user=pg_user,
            password=pg_password,
            connect_timeout=5,
        )
        with conn.cursor() as cur:
            cur.execute(
                """
                select
                  bo_loc.name as name,
                  l.storage as storage,
                  l.bucket as bucket,
                  l.readonly as "readOnly",
                  l.is_direct_upload as "directUpload",
                  coalesce(bo_portal.name, '') as dedicated_portal_name
                from locations l
                join base_objects bo_loc
                  on bo_loc.uid = l.uid
                left join base_objects bo_portal
                  on bo_portal.uid = l.dedicated_portal_id
                where coalesce(bo_loc.is_deleted, false) = false
                order by bo_loc.name
                """
            )
            rows = cur.fetchall()
        return [
            {
                "name": row[0],
                "storage": row[1],
                "bucket": row[2],
                "readOnly": row[3],
                "directUpload": row[4],
                "dedicatedPortal": row[5],
            }
            for row in rows
        ]
    except Exception as exc:
        logging.debug("Storage locations fetch via Postgres failed: %s", exc)
        return []
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass

def _get_storage_locations(admin):
    pg_locations = _get_storage_locations_from_postgres()
    if pg_locations:
        return pg_locations

    for attempt in range(2):
        api_candidates = []
        core = getattr(admin, "_core", None)
        direct_api = getattr(admin, "api", None)
        if direct_api is not None:
            api_candidates.append(direct_api)
        if core is not None and getattr(core, "api", None) is not None:
            api_candidates.append(core.api)

        for api in api_candidates:
            raw_wrapper = getattr(api, "_session", None)
            raw_session = getattr(raw_wrapper, "session", None)
            raw_loop = getattr(raw_session, "_loop", None)
            baseurl = getattr(api, "baseurl", "").rstrip("/")
            if raw_session is not None and raw_loop is not None and baseurl:
                async def _fetch_locations_text():
                    async with raw_session.get(f"{baseurl}/locations?format=jsonext", ssl=False) as response:
                        return await response.text()

                try:
                    payload = json.loads(raw_loop.run_until_complete(_fetch_locations_text()))
                    objs = _extract_query_objects(payload)
                    if objs:
                        return objs
                except Exception as exc:
                    logging.debug("Storage locations fetch via raw session failed: %s", exc)
                    if "Session expired" in str(exc) and attempt == 0 and _reauth(admin):
                        try:
                            admin.portals.browse_global_admin()
                        except Exception:
                            pass
                        break

            for method_name in ("get", "raw_get"):
                method = getattr(api, method_name, None)
                if not callable(method):
                    continue
                try:
                    payload = method('/locations?format=jsonext')
                    objs = _extract_query_objects(payload)
                    if objs:
                        return objs
                except Exception as exc:
                    logging.debug("Storage locations fetch via %s.%s failed: %s", type(api).__name__, method_name, exc)
                    if "Session expired" in str(exc) and attempt == 0 and _reauth(admin):
                        try:
                            admin.portals.browse_global_admin()
                        except Exception:
                            pass
                        break
    return []

def resolve_bucket_driver(admin, name, bucket_value):
    """
    Return a human-friendly driver/vendor for a storage node.
    - If SDK returns a typed object for bucket → map its class name.
    - If SDK returns a plain str → classify by value:
        * startswith('/') → Local Filesystem
        * otherwise → Amazon S3 (default for your setup)
    """
    try:
        full = _with_reauth(admin, lambda: admin.buckets.get(name, include=['bucket']), retries=1, label=f"bucket_get_driver:{name}")
        clsname = type(getattr(full, 'bucket', None)).__name__
    except Exception:
        clsname = None

    if clsname and clsname not in ('NoneType', 'str'):
        return BUCKET_CLASS_MAP.get(clsname, clsname)

    b = (bucket_value or '')
    if isinstance(b, str) and b.startswith('/'):
        return 'Local Filesystem'
    return 'Amazon S3'

def _collect_bucket_rows(admin):
    rows = []
    try:
        buckets = _with_reauth(admin, lambda: list(admin.buckets.list_buckets(include=BUCKET_FIELDS)), retries=1, label="list_buckets")
    except Exception as exc:
        logging.warning("Bucket fallback list failed: %s", exc)
        return rows

    for b in buckets:
        name = getattr(b, 'name', '')
        bucket_value = getattr(b, 'bucket', '')
        read_only = getattr(b, 'readOnly', '')
        dedicated_to = getattr(b, 'dedicatedTo', '')
        driver = resolve_bucket_driver(admin, name, bucket_value)
        direct = ''
        try:
            full = _with_reauth(admin, lambda: admin.buckets.get(name, include=['bucket', 'direct']), retries=1, label=f"bucket_get_detail:{name}")
            direct = getattr(full, 'direct', '')
        except Exception as exc:
            logging.warning("Bucket detail lookup failed for %s: %s", name or '?', exc)
        rows.append([
            name,
            driver,
            bucket_value,
            read_only,
            dedicated_to,
            direct,
        ])
    return rows


def write_buckets_header(filename):
    with open(filename, mode='a', newline='', encoding='utf-8-sig') as f:
        w = csv.writer(f, dialect='excel', delimiter=',', quotechar='"', quoting=csv.QUOTE_MINIMAL)
        w.writerow(['Name', 'Driver', 'Bucket', 'ReadOnly', 'DedicatedTo', 'DirectIO'])


def write_buckets(self, filename):
    logging.info("Collecting Storage Nodes (Buckets) from Global Admin...")
    _with_reauth(self, lambda: self.portals.browse_global_admin(), retries=1, label="browse_global_admin_storage")

    with open(filename, mode='a', newline='', encoding='utf-8-sig') as f:
        w = csv.writer(f, dialect='excel', delimiter=',', quotechar='"', quoting=csv.QUOTE_MINIMAL)
        locations = _get_storage_locations(self)
        if locations:
            for loc in locations:
                w.writerow([
                    _obj_get(loc, 'name', ''),
                    _resolve_location_driver(loc),
                    _obj_get(loc, 'bucket', ''),
                    _obj_get(loc, 'readOnly', ''),
                    _parse_portal_name(_obj_get(loc, 'dedicatedPortal', '')),
                    _obj_get(loc, 'directUpload', ''),
                ])
        else:
            # Fallback for environments where the richer locations endpoint is unavailable.
            for row in _collect_bucket_rows(self):
                w.writerow(row)
    logging.info("Wrote storage nodes CSV to %s", filename)


def run_buckets(self, filename):
    logging.info('Starting storage nodes (buckets) task')
    if os.path.exists(filename):
        logging.info('Appending to existing file.')
    else:
        write_buckets_header(filename)
    try:
        write_buckets(self, filename)
    except Exception as e:
        logging.warning("An error occurred: " + str(e))
    logging.info('Finished storage nodes task.')


# -------------------- Unified "infra" CSV (servers + buckets) --------------------
INFRA_HEADER = ['Type', 'Name', 'Connected', 'IsApplicationServer', 'IsMainDB', 'Driver', 'Bucket', 'ReadOnly', 'DedicatedTo', 'DirectIO']


def write_infra_header(filename):
    with open(filename, mode='a', newline='', encoding='utf-8-sig') as f:
        w = csv.writer(f, dialect='excel', delimiter=',', quotechar='"', quoting=csv.QUOTE_MINIMAL)
        w.writerow(INFRA_HEADER)


def append_servers_to_infra(self, filename):
    self.portals.browse_global_admin()
    servers = self.servers.list_servers(include=SERVER_FIELDS)
    with open(filename, mode='a', newline='', encoding='utf-8-sig') as f:
        w = csv.writer(f, dialect='excel', delimiter=',', quotechar='"', quoting=csv.QUOTE_MINIMAL)
        for s in servers:
            w.writerow([
                'Server',
                getattr(s, 'name', ''),
                getattr(s, 'connected', ''),
                getattr(s, 'isApplicationServer', ''),
                getattr(s, 'mainDB', ''),
                '', '', '', '', ''  # bucket fields empty
            ])


def append_buckets_to_infra(self, filename):
    _with_reauth(self, lambda: self.portals.browse_global_admin(), retries=1, label="browse_global_admin_infra_storage")
    with open(filename, mode='a', newline='', encoding='utf-8-sig') as f:
        w = csv.writer(f, dialect='excel', delimiter=',', quotechar='"', quoting=csv.QUOTE_MINIMAL)
        locations = _get_storage_locations(self)
        if locations:
            for loc in locations:
                w.writerow([
                    'StorageNode',
                    _obj_get(loc, 'name', ''),
                    '', '', '',  # server columns empty
                    _resolve_location_driver(loc),
                    _obj_get(loc, 'bucket', ''),
                    _obj_get(loc, 'readOnly', ''),
                    _parse_portal_name(_obj_get(loc, 'dedicatedPortal', '')),
                    _obj_get(loc, 'directUpload', ''),
                ])
        else:
            for row in _collect_bucket_rows(self):
                w.writerow([
                    'StorageNode',
                    row[0],
                    '', '', '',  # server columns empty
                    row[1],
                    row[2],
                    row[3],
                    row[4],
                    row[5],
                ])


def run_infra(self, filename):
    logging.info('Starting infra task (servers + storage nodes)')
    if os.path.exists(filename):
        logging.info('Appending to existing file.')
    else:
        write_infra_header(filename)
    try:
        append_servers_to_infra(self, filename)
        append_buckets_to_infra(self, filename)
    except Exception as e:
        logging.warning("An error occurred: " + str(e))
    logging.info('Finished infra task.')


# -------------------- Server Tasks CSV --------------------
# Exports background tasks per server using admin.servers.tasks.background(<server_name>)
TASKS_HEADER = ['ServerName', 'TaskName', 'Enabled', 'State', 'LastRun', 'NextRun']

def write_tasks_header(filename):
    with open(filename, mode='a', newline='', encoding='utf-8-sig') as f:
        w = csv.writer(f, dialect='excel', delimiter=',', quotechar='"', quoting=csv.QUOTE_MINIMAL)
        w.writerow(TASKS_HEADER)


def write_server_tasks(self, filename, servers_filter=None):
    """
    Collect background tasks for each server and write ONLY 'running' tasks to CSV.

    Columns:
      ServerName, TaskName, Enabled, State, StartTime, EndTime, ElapsedSeconds, Message, TaskID
    """
    import csv
    import logging
    from datetime import datetime, timezone

    # --- tiny helpers (local so this is fully drop-in) ---
    def _g(obj, *names, default=""):
        """Return the first existing non-None attribute from names."""
        for n in names:
            if hasattr(obj, n):
                v = getattr(obj, n)
                if v is not None:
                    return v
        return default

    def _to_iso(v):
        """Normalize timestamps to ISO8601 string when possible."""
        if not v:
            return ""
        if isinstance(v, str):
            return v
        if isinstance(v, datetime):
            if v.tzinfo is None:
                v = v.replace(tzinfo=timezone.utc)
            return v.isoformat()
        try:
            return str(v)
        except Exception:
            return ""
    # -----------------------------------------------------

    logging.info("Collecting background tasks for each server (ONLY running)...")

    # Ensure we're in GA context if applicable
    try:
        self.portals.browse_global_admin()
    except Exception as e:
        logging.warning("browse_global_admin failed (continuing): %s", e)

    # Header (overwrite file)
    with open(filename, mode='w', newline='', encoding='utf-8-sig') as f:
        csv.writer(f, dialect='excel', delimiter=',', quotechar='"', quoting=csv.QUOTE_MINIMAL).writerow([
            'ServerName', 'TaskName', 'Enabled', 'State',
            'StartTime', 'EndTime', 'ElapsedSeconds', 'Message', 'TaskID'
        ])

    # Which servers to include
    try:
        servers = self.servers.list_servers(include=['name'])
    except Exception as e:
        logging.error("Could not list servers: %s", e)
        return

    if servers_filter:
        wanted = set(servers_filter)
        servers = [s for s in servers if getattr(s, 'name', None) in wanted]

    # Append rows
    with open(filename, mode='a', newline='', encoding='utf-8-sig') as f:
        w = csv.writer(f, dialect='excel', delimiter=',', quotechar='"', quoting=csv.QUOTE_MINIMAL)

        for s in servers:
            server_name = getattr(s, 'name', '') or ''
            if not server_name:
                continue

            try:
                tasks = self.servers.tasks.background(server_name)
            except Exception as e:
                logging.warning("Could not fetch tasks for server %s: %s", server_name, e)
                continue

            for t in tasks or []:
                # Map real attributes the SDK exposes (snake_case)
                name      = _g(t, 'name', 'taskName')
                enabled   = _g(t, 'enabled', 'isEnabled', 'active')
                state_raw = _g(t, 'status', 'state')  # UI label "State"; SDK returns 'status'
                state     = (str(state_raw) or "").strip().lower()

                # === OPTION A: keep ONLY running tasks ===
                # Add more active labels if you ever observe them (e.g., 'in_progress')
                if state != 'running':
                    continue

                start     = _g(t, 'start_time', 'startedAt')
                end       = _g(t, 'end_time', 'finishedAt')
                elapsed   = _g(t, 'elapsed_time', 'elapsed', 'duration', default="")
                message   = _g(t, 'message')
                task_id   = _g(t, 'id')

                start_iso = _to_iso(start)
                end_iso   = _to_iso(end)
                if isinstance(elapsed, (int, float)):
                    elapsed_s = str(int(elapsed))
                else:
                    elapsed_s = str(elapsed) if elapsed not in (None, "") else ""

                w.writerow([
                    server_name,
                    name,
                    enabled,
                    state_raw,
                    start_iso,
                    end_iso,
                    elapsed_s,
                    message,
                    task_id,
                ])

    logging.info("Wrote server tasks CSV to %s", filename)



def run_server_tasks(self, filename):
    logging.info('Starting server tasks collection')
    if os.path.exists(filename):
        logging.info('Appending to existing file.')
    else:
        write_tasks_header(filename)
    try:
        write_server_tasks(self, filename)
    except Exception as e:
        logging.warning("An error occurred: " + str(e))
    logging.info('Finished server tasks collection.')


# -------------------- Runner --------------------
def env_bool(name, default=False):
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def configure_ctera_tls(verify_ssl):
    if verify_ssl:
        return
    cterasdk.settings.core.syn.settings.connector.ssl = False
    try:
        cterasdk.settings.edge.syn.settings.connector.ssl = False
    except AttributeError:
        pass


def safe_attr(obj, path, default='N/A'):
    current = obj
    for part in path.split('.'):
        try:
            current = getattr(current, part)
        except (AttributeError, TypeError):
            return default
        if current is None:
            return default
    return current


def first_scalar(value, default=''):
    if value is None:
        return default
    if isinstance(value, (list, tuple)):
        return str(value[0]) if value else default
    text = str(value).strip()
    if text.startswith("[") and text.endswith("]"):
        try:
            parsed = ast.literal_eval(text)
            if isinstance(parsed, (list, tuple)):
                return str(parsed[0]) if parsed else default
        except Exception:
            pass
    return text


def main():
    ap = argparse.ArgumentParser(description="Collect CTERA portal data into CTERA Monitoring Dashboard CSV files")
    ap.add_argument("-H", "--host", required=True, help="Portal hostname (you can include :port, e.g. myportal8a.ctera.me:8443)")
    ap.add_argument("-u", "--user", required=True, help="Username")
    ap.add_argument("-p", "--password", required=True, help="Password")
    ap.add_argument("-t", "--tenant", help="Tenant name (required when using --global-admin if you want a single tenant)")
    ap.add_argument("--global-admin", action="store_true", help="Use GlobalAdmin session (default is tenant user via ServicesPortal)")
    ap.add_argument("--all-tenants", action="store_true", help="Scan across all tenants (Global Admin only; filers mode)")
    ap.add_argument("-o", "--outfile", default="output.csv", help="Output CSV file")
    ap.add_argument("--mode", choices=["filers", "servers", "storage", "infra", "tasks", "certificate"], default="filers", help="What to export to CSV")
    ap.add_argument("--ensure-remote", action="store_true", help="Open remote access for each filer before collection (filers mode)")
    ap.add_argument("--verify-ssl", action="store_true", default=env_bool("CTERA_VERIFY_SSL", False), help="Verify CTERA portal TLS certificates. Default is disabled for internal/self-signed portals.")
    ap.add_argument("-v", "--verbose", action="store_true", help="Verbose logging")
    args = ap.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(levelname)s %(message)s")
    configure_ctera_tls(args.verify_ssl)

    # If user didn't customize outfile and selected tasks, default to tasks.csv for convenience
    if args.mode == "tasks" and args.outfile == "output.csv":
        args.outfile = "tasks.csv"

    Session = GlobalAdmin if args.global_admin else ServicesPortal
    sess = Session(args.host)
    try:
        sess.login(args.user, args.password)
        sess._featherdash_user = args.user
        sess._featherdash_password = args.password
        sess._featherdash_global_admin = args.global_admin
        sess._featherdash_tls_host = args.host

        if args.mode == "filers":
            if args.global_admin and args.tenant and not args.all_tenants:
                sess.portals.browse(args.tenant)
            if args.ensure_remote:
                flist = get_filers(sess, all_tenants=args.all_tenants, tenant=args.tenant)
                for f in (flist or []):
                    try:
                        f.remote_access()
                    except Exception as e:
                        logging.warning("Remote access failed for a filer: %s", e)
            run_filers(sess, args.outfile, args.all_tenants)

        elif args.mode == "servers":
            sess.portals.browse_global_admin()
            run_servers(sess, args.outfile)

        elif args.mode == "storage":
            sess.portals.browse_global_admin()
            run_buckets(sess, args.outfile)

        elif args.mode == "certificate":
            run_certificate(sess, args.outfile)

        elif args.mode == "infra":
            sess.portals.browse_global_admin()
            run_infra(sess, args.outfile)

        else:  # tasks
            sess.portals.browse_global_admin()
            run_server_tasks(sess, args.outfile)

    finally:
        try:
            sess.logout()
        except Exception:
            pass


if __name__ == "__main__":
    main()
