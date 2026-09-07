# CTERA Monitoring Dashboard

CTERA Monitoring Dashboard is a lightweight Flask based dashboard for collecting and displaying CTERA Portal, Edge Filer, PostgreSQL, tenant, task, storage, and server health metrics.

The application runs on Linux as a systemd service and uses scheduled collector jobs to generate CSV files used by the dashboard.

## Reference Docs

- Collection and thresholds reference: [docs/collection-and-thresholds.md](docs/collection-and-thresholds.md)

## Repository

```text
https://github.com/ctera/CTERA-Monitoring-Dashboard
```

## Supported Platforms

This installer supports Ubuntu/Debian and RHEL-style systems (`dnf` / `yum`) **when package repos are available**.

| Platform | Status | Notes |
|---|---|---|
| Ubuntu | Supported | Recommended platform |
| Debian | Supported | Expected to work |
| Rocky Linux / AlmaLinux | Supported | Full package repos available without Red Hat subscription |
| CTERA Portal server OVA | Supported | Use the Portal OVA / portal Linux image when that is your standard host |
| RHEL | Supported only when registered | Unregistered RHEL often cannot install `nginx` / `sshpass`; installer stops with register steps |
| CentOS | Best effort | Older CentOS versions may require additional package adjustments |
| Windows | Not supported | Windows can be used to open docs or reach GitHub in a browser, but the dashboard service must run on Linux with outbound internet for install/upgrade |

Required packages include `nginx` and `sshpass`. If install fails on RHEL because repos are missing, register the host with Red Hat subscription-manager, or use Ubuntu / Rocky / Alma / a CTERA Portal server OVA.

## Default Layout

| Item | Default Path |
|---|---|
| Application directory | `/opt/monitoring/ctera-monitoring-dashboard` |
| Runtime environment file | `/etc/ctera-monitoring-dashboard.env` |
| Data directory | `/var/lib/ctera-monitoring-dashboard/data` |
| PostgreSQL data directory | `/var/lib/ctera-monitoring-dashboard/data/db` |
| Log directory | `/var/log/ctera-monitoring-dashboard` |
| Service user | `ctera-monitoring` |
| Systemd service | `ctera-monitoring-dashboard` |
| Cron file | `/etc/cron.d/ctera-monitoring-dashboard` |
| Default port | `8080` |

---

## Prerequisites

Review these requirements before adding a portal environment so bootstrap and collector jobs can complete cleanly.

### CTERA Access

- Create a Global Admin user for the dashboard before setup starts. We recommend naming that user `monitoring`.
- A **read-only** Global Admin can collect most portal and filer data.
- Use a **read/write** Global Admin if you need filer **CPU**, **memory**, **disk**, or CloudSync DB size metrics (those require shell access on the filer).
- Keep that administrator’s password available during portal environment setup.
- If **Global Administrators Access Control** (IP allowlist) is enabled, add this monitoring server’s IP:
  - Portal path: **Settings → Control Panel → Global Administrators Access Control**
  - Use the address the portal sees from that host (often its private/LAN IP). If the server is missing from this list, browser login from your PC can work while collectors on the server fail with HTTP 403 / `Authentication failed`.
- If Global Admin **SAML SSO** is enabled, the `monitoring` user must still have a **local password** set (required for `/admin/bypass` and API-style collection).
- If **Display consent page before login** is enabled, collectors accept it automatically; you can also disable that page under **Settings → Global Settings → Consent Page** if your policy allows.

### Network Ports

- Open port `22` between the monitoring server and MainDB.
- Open port `5432` between the monitoring server and MainDB.
- Open port `443` between the monitoring server and the Tomcat servers or portal endpoint used for login.

### MainDB SSH Access

- Have an SSH path to MainDB ready before bootstrap begins.
- The initial SSH mode can use a password or a private key.
- The bootstrap user can be `root`, or another user that can `sudo` to root.
- If a jump host is required, make sure the jump host path is already reachable from the monitoring server.

### Bootstrap Behavior

- MainDB root access is required during the initial bootstrap flow.
- The dashboard uses the initial SSH access mode one time to install its runtime SSH key and collect what it needs.
- After bootstrap, the dashboard switches ongoing access to the installed SSH key and saved runtime environment.

---

# Install

Install requires outbound internet on the monitoring server for:

- GitHub (download the package)
- OS package repos (`apt` / `dnf` / `yum`: `nginx`, `sshpass`, Python, etc.)
- PyPI (`pip install -r requirements.txt`)

There is **no offline / air-gapped installer**. Use the one-command install below.

MainDB root (or sudo-to-root) access is required during setup. The installer switches ongoing access to an installed SSH key afterward.

## One-command install

```bash
cd /tmp && curl -L https://github.com/ctera/CTERA-Monitoring-Dashboard/archive/refs/heads/main.tar.gz -o ctera-monitoring-dashboard.tar.gz && rm -rf /tmp/ctera-monitoring-dashboard && mkdir -p /tmp/ctera-monitoring-dashboard && sudo tar -xzf /tmp/ctera-monitoring-dashboard.tar.gz -C /tmp/ctera-monitoring-dashboard --strip-components=1 && cd /tmp/ctera-monitoring-dashboard && sudo bash ./install.sh
```

This stages the package under `/tmp/ctera-monitoring-dashboard` and installs to:

```text
/opt/monitoring/ctera-monitoring-dashboard
```

## Install behind an HTTP/HTTPS proxy

If the server reaches GitHub, OS repos, or PyPI only through a proxy, export the standard proxy variables **before** the install command (same shell). Include the username and password in the URL when the proxy requires authentication:

```bash
export http_proxy='http://PROXYUSER:PROXYPASS@proxy.example.com:8080'
export https_proxy='http://PROXYUSER:PROXYPASS@proxy.example.com:8080'
export HTTP_PROXY="$http_proxy"
export HTTPS_PROXY="$https_proxy"
# Optional: skip proxy for local/internal hosts
export no_proxy='localhost,127.0.0.1,.group.wan'
export NO_PROXY="$no_proxy"
```

Then run the same one-command install:

```bash
cd /tmp && curl -L https://github.com/ctera/CTERA-Monitoring-Dashboard/archive/refs/heads/main.tar.gz -o ctera-monitoring-dashboard.tar.gz && rm -rf /tmp/ctera-monitoring-dashboard && mkdir -p /tmp/ctera-monitoring-dashboard && sudo tar -xzf /tmp/ctera-monitoring-dashboard.tar.gz -C /tmp/ctera-monitoring-dashboard --strip-components=1 && cd /tmp/ctera-monitoring-dashboard && sudo -E bash ./install.sh
```

Notes:

- Use `sudo -E` so `http(s)_proxy` is preserved for `pip` and package managers under root.
- If the proxy password contains special characters (`@`, `:`, `/`, `#`, etc.), URL-encode them (for example `@` → `%40`).
- Example with encoded password: `http://myuser:p%40ssw%3Ard@10.0.0.5:3128`

---

## Open the Dashboard

After installation, open:

```text
http://<server-ip>:8080/
```

Health check:

```text
http://<server-ip>:8080/healthz
```

---

# Upgrade

Upgrade also requires outbound internet (GitHub + OS repos + PyPI when packages change).

## One-command upgrade

```bash
cd /tmp && sudo rm -rf /tmp/ctera-monitoring-dashboard && curl -L https://github.com/ctera/CTERA-Monitoring-Dashboard/archive/refs/heads/main.tar.gz -o /tmp/ctera-monitoring-dashboard.tar.gz && sudo mkdir -p /tmp/ctera-monitoring-dashboard && sudo tar -xzf /tmp/ctera-monitoring-dashboard.tar.gz -C /tmp/ctera-monitoring-dashboard --strip-components=1 && cd /tmp/ctera-monitoring-dashboard && sudo bash ./upgrade.sh --install-dir /opt/monitoring/ctera-monitoring-dashboard
```

Behind a proxy, export the same `http_proxy` / `https_proxy` variables as for install, then use `sudo -E bash ./upgrade.sh ...`.

What upgrade does:

- downloads the latest package under `/tmp`
- creates a backup and restore script before changing the installed copy
- preserves customer settings and merges new default threshold entries into `thresholds.yaml`

---

# Backup and Restore

During upgrade, a backup is created under:

```text
/opt/monitoring-backup
```

Example backup path:

```text
/opt/monitoring-backup/ctera-monitoring-dashboard-<version>-<timestamp>
```

The backup includes the previous application files and important runtime paths.

To restore, run the restore script printed by the upgrade output.

Example:

```bash
sudo bash /opt/monitoring-backup/ctera-monitoring-dashboard-<version>-<timestamp>/restore.sh
```

---

# Runtime Configuration

The main runtime configuration file is:

```text
/etc/ctera-monitoring-dashboard.env
```

Example values:

```bash
CTERA_HOST=<portal-fqdn>
CTERA_USERNAME=<global-admin-read-only-user>
CTERA_PASSWORD=<password>
CTERA_VERIFY_SSL=false

PGHOST=<main-db-ip>
PGPORT=5432
PGDATABASE=postgres
PGUSER=postgres
PGPASSWORD=<db-password>

SERVER_SSH_USER=root
ROOT_KEY=/opt/monitoring/ctera-monitoring-dashboard/ssh/id_ed25519

PORT=8080
FEATHERDASH_DATA_DIR=/var/lib/ctera-monitoring-dashboard/data
FEATHERDASH_DB_DIR=/var/lib/ctera-monitoring-dashboard/data/db
FEATHERDASH_THRESHOLDS=/opt/monitoring/ctera-monitoring-dashboard/thresholds.yaml
PYTHONUNBUFFERED=1
```

After changing the environment file:

```bash
sudo systemctl restart ctera-monitoring-dashboard
```

---

# Service Management

Check service status:

```bash
sudo systemctl status ctera-monitoring-dashboard --no-pager
```

Restart the service:

```bash
sudo systemctl restart ctera-monitoring-dashboard
```

View recent logs:

```bash
sudo journalctl -u ctera-monitoring-dashboard -n 200 --no-pager
```

Follow logs:

```bash
sudo journalctl -u ctera-monitoring-dashboard -f
```

---

# Collector Jobs

The cron file is installed here:

```text
/etc/cron.d/ctera-monitoring-dashboard
```

Collector scripts include:

```text
scheduler_jobs.sh
portal_jobs.sh
filer_jobs.sh
```

Run collectors manually:

```bash
sudo -u ctera-monitoring /opt/monitoring/ctera-monitoring-dashboard/portal_jobs.sh
sudo -u ctera-monitoring /opt/monitoring/ctera-monitoring-dashboard/filer_jobs.sh
```

Collector logs:

```bash
sudo tail -F /var/log/ctera-monitoring-dashboard/scheduler.log
sudo tail -F /var/log/ctera-monitoring-dashboard/portal.log
sudo tail -F /var/log/ctera-monitoring-dashboard/filer.log
```

---

# Data Output

Dashboard CSV files are stored in:

```text
/var/lib/ctera-monitoring-dashboard/data
```

PostgreSQL health CSV files are stored in:

```text
/var/lib/ctera-monitoring-dashboard/data/db
```

Check generated files:

```bash
ls -lh /var/lib/ctera-monitoring-dashboard/data
ls -lh /var/lib/ctera-monitoring-dashboard/data/db
```

---

# Validate Installation

```bash
curl -I http://127.0.0.1:8080/
curl -I http://127.0.0.1:8080/healthz

sudo systemctl status ctera-monitoring-dashboard --no-pager
sudo journalctl -u ctera-monitoring-dashboard -n 100 --no-pager
```

---

# Troubleshooting

## Dashboard does not load

Check the service:

```bash
sudo systemctl status ctera-monitoring-dashboard --no-pager
sudo journalctl -u ctera-monitoring-dashboard -n 200 --no-pager
```

Check if port `8080` is listening:

```bash
sudo ss -tulpn | grep 8080
```

## CSV files are missing

Run collectors manually:

```bash
sudo -u ctera-monitoring /opt/monitoring/ctera-monitoring-dashboard/portal_jobs.sh
sudo -u ctera-monitoring /opt/monitoring/ctera-monitoring-dashboard/filer_jobs.sh
```

Then check logs:

```bash
sudo tail -200 /var/log/ctera-monitoring-dashboard/portal.log
sudo tail -200 /var/log/ctera-monitoring-dashboard/filer.log
```

## Python package error

Reinstall requirements:

```bash
cd /opt/monitoring/ctera-monitoring-dashboard

sudo ./venv/bin/pip install -r requirements.txt
sudo systemctl restart ctera-monitoring-dashboard
```

## SSL certificate errors

For internal or self-signed CTERA Portal certificates, set:

```bash
CTERA_VERIFY_SSL=false
```

in:

```text
/etc/ctera-monitoring-dashboard.env
```

Then restart:

```bash
sudo systemctl restart ctera-monitoring-dashboard
```

---

# Uninstall

To fully remove the dashboard, service, cron job, helper, logs, data, config, and install directory, run:

```bash
sudo systemctl disable --now ctera-monitoring-dashboard || true; sudo rm -f /etc/systemd/system/ctera-monitoring-dashboard.service; sudo rm -f /etc/cron.d/ctera-monitoring-dashboard; sudo rm -f /usr/local/sbin/ctera-monitoring-dashboard-upgrade; sudo rm -f /etc/sudoers.d/ctera-monitoring-dashboard-upgrade; sudo rm -f /usr/local/bin/ctera-secret-helper; sudo rm -f /etc/ctera-monitoring-dashboard.env; sudo rm -rf /opt/monitoring/ctera-monitoring-dashboard; sudo rm -rf /var/lib/ctera-monitoring-dashboard; sudo rm -rf /var/log/ctera-monitoring-dashboard; sudo systemctl daemon-reload
```

If you also want to remove the service account:

```bash
sudo userdel ctera-monitoring || true
```
