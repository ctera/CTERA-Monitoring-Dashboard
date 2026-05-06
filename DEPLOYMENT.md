# CTERA Monitoring Dashboard Production Deployment

This procedure deploys CTERA Monitoring Dashboard as a Flask app on port 8080 behind a load balancer that terminates authentication and TLS.

## Recommended Layout

- App code: `/opt/monitoring/ctera-monitoring-dashboard`
- Admin config: `/etc/ctera-monitoring-dashboard.env`
- CSV data: `/var/lib/ctera-monitoring-dashboard/data`
- PostgreSQL CSV data: `/var/lib/ctera-monitoring-dashboard/data/db`
- Logs: `/var/log/ctera-monitoring-dashboard`
- Service user: `ctera-monitoring`
- Service manager: systemd service `ctera-monitoring-dashboard`

## Easy Install

Copy the archive to the server, then run:

```bash
sudo mkdir -p /opt/monitoring
sudo tar -xzf ctera-monitoring-dashboard.tgz -C /opt/monitoring
cd /opt/monitoring/ctera-monitoring-dashboard
sudo bash ./install.sh
```

The installer prompts for CTERA, MainDB, SSH collection setup, collector frequency, and optional OpenAI settings. It installs OS packages, creates the `ctera-monitoring` service user, creates the Python virtualenv, writes `/etc/ctera-monitoring-dashboard.env`, installs systemd and cron, starts the dashboard, and prints validation commands.

For SSH collection and MainDB password discovery, the installer walks through these options:

- SSH to `root` with username and password.
- SSH to another username with username and password, then `sudo` to root.
- SSH to `root` with a private key.
- SSH to another username with a private key, then `sudo` to root.

Passwords entered during install are used only for one-time key setup, sudo, and Postgres password retrieval. They are not saved. The saved config keeps only `SERVER_SSH_USER`, `ROOT_KEY`, and the retrieved Postgres password.

To install directly from an archive:

```bash
sudo bash ./install.sh --archive /path/to/ctera-monitoring-dashboard.tgz
```

To choose a different app directory:

```bash
sudo bash ./install.sh --install-dir /opt/monitoring/ctera-monitoring-dashboard
```

## One Place To Update

Admins update:

```bash
sudo nano /etc/ctera-monitoring-dashboard.env
sudo systemctl restart ctera-monitoring-dashboard
```

If `/etc/ctera-monitoring-dashboard.env` already exists during install, the installer asks whether to keep it or reconfigure it. If the admin chooses to reconfigure, the existing file is backed up before a new one is written.

Important variables:

```bash
CTERA_HOST=<portal-fqdn>
CTERA_USERNAME=<global-admin-user>
CTERA_PASSWORD=<password>
CTERA_VERIFY_SSL=false
PGHOST=<main-db-ip>
PGPORT=5432
PGDATABASE=postgres
PGUSER=postgres
PGPASSWORD=<db-password>
SERVER_SSH_USER=root
ROOT_KEY=/opt/monitoring/ctera-monitoring-dashboard/ssh/id_ed25519
SERVER_METRICS_MODE=jump
SERVER_METRICS_TARGET_USER=ctera
SERVER_METRICS_JUMP_HOST=<main-db-ip>
SERVER_METRICS_JUMP_USER=root
SERVER_METRICS_JUMP_RUN_AS_USER=ctera
SERVER_METRICS_SUDO=true
OPENAI_API_KEY=<optional, only needed for AI Summary>
PORT=8080
FEATHERDASH_DATA_DIR=/var/lib/ctera-monitoring-dashboard/data
FEATHERDASH_DB_DIR=/var/lib/ctera-monitoring-dashboard/data/db
FEATHERDASH_THRESHOLDS=/opt/monitoring/ctera-monitoring-dashboard/thresholds.yaml
PYTHONUNBUFFERED=1
```

Use a read-write global administrator if you want filer CloudSync DB size and filer CPU/memory shell metrics. A read-only global administrator still works for standard portal and filer collection, but those filer shell metrics will stay unavailable.

## One CSV Location

Collectors write normal dashboard CSVs to:

```bash
/var/lib/ctera-monitoring-dashboard/data
```

PostgreSQL collector CSVs go to:

```bash
/var/lib/ctera-monitoring-dashboard/data/db
```

The dashboard reads the same locations from `/etc/ctera-monitoring-dashboard.env`.

## Service And Cron

Systemd service:

```bash
systemctl status ctera-monitoring-dashboard --no-pager
journalctl -u ctera-monitoring-dashboard -n 200 --no-pager
```

Cron collectors:

```bash
cat /etc/cron.d/ctera-monitoring-dashboard
tail -200 /var/log/ctera-monitoring-dashboard/portal.log
tail -200 /var/log/ctera-monitoring-dashboard/filer.log
```

During install, the wizard asks how often to run:

- Portal/MainDB collectors, default every 60 minutes.
- Edge filer collectors, default every 60 minutes.

The cron jobs run as the `ctera-monitoring` service user, not as root.

To run collectors manually and generate CSV files immediately:

```bash
sudo -u ctera-monitoring /opt/monitoring/ctera-monitoring-dashboard/portal_jobs.sh
sudo -u ctera-monitoring /opt/monitoring/ctera-monitoring-dashboard/filer_jobs.sh
```

`portal_jobs.sh` generates storage, portal servers, tasks, Postgres health CSVs, and tenants. It also generates `server_metrics.csv` when `ROOT_KEY` points to a readable SSH private key. By default, server metrics use jump mode: CTERA Monitoring Dashboard connects to MainDB, then MainDB connects to the other portal servers as `ctera` and runs metric commands with `sudo -n`. If `ROOT_KEY` is missing, only SSH server metrics are skipped; the rest of the portal CSVs still update.

Collector output is written to:

```bash
/var/lib/ctera-monitoring-dashboard/data
/var/lib/ctera-monitoring-dashboard/data/db
```

Collector logs are written to:

```bash
/var/log/ctera-monitoring-dashboard/portal.log
/var/log/ctera-monitoring-dashboard/filer.log
```

## Validate

```bash
curl -I http://127.0.0.1:8080/
curl -I http://127.0.0.1:8080/healthz
ls -lh /var/lib/ctera-monitoring-dashboard/data/*.csv
ls -lh /var/lib/ctera-monitoring-dashboard/data/db/*.csv
```

From an internal network, test:

```text
http://<INSTANCE-IP>:8080/
```

## Load Balancer

Configure the load balancer target group with protocol `HTTP`, port `8080`, and target type `instance`. Use `/healthz` for health checks. Restrict instance port `8080` so only the load balancer can reach it.

End users should access CTERA Monitoring Dashboard through the HTTPS load balancer hostname:

```text
https://<LB-hostname>/
```

## Troubleshooting

- Service will not start: check `/etc/ctera-monitoring-dashboard.env`, permissions, variable names, and the virtualenv path in `/etc/systemd/system/ctera-monitoring-dashboard.service`.
- Port is not listening: verify `PORT=8080`, inspect `journalctl`, and check for another service on port `8080`.
- Cron is not running jobs: verify `/etc/cron.d/ctera-monitoring-dashboard`, log directory permissions, and that `/etc/ctera-monitoring-dashboard.env` exists.
- CSVs are missing: run the collectors manually, inspect collector logs, and validate CTERA, MainDB/Postgres, and SSH credentials. If only `server_metrics.csv` is missing, check that `ROOT_KEY` in `/etc/ctera-monitoring-dashboard.env` points to a readable private key for the `ctera-monitoring` service user.
- `ModuleNotFoundError: No module named 'cterasdk'`: install the missing SDK into the CTERA Monitoring Dashboard virtualenv with `sudo /opt/monitoring/ctera-monitoring-dashboard/venv/bin/pip install cterasdk`, then rerun the collector.
- `TLSError` or `CERTIFICATE_VERIFY_FAILED`: internal/self-signed portal certificates are ignored by default with `CTERA_VERIFY_SSL=false` in `/etc/ctera-monitoring-dashboard.env`. Set it to `true` only when the portal certificate chain is trusted by the host.
- LB health check fails: confirm security rules allow LB to instance port `8080` and `/healthz` returns quickly.
