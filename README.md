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

This installer currently supports Ubuntu/Debian based Linux servers.

| Platform | Status | Notes |
|---|---|---|
| Ubuntu | Supported | Recommended platform |
| Debian | Supported | Expected to work |
| RHEL / Rocky / AlmaLinux / Oracle Linux | Coming soon | Installer support for `dnf` and `yum` is planned |
| CentOS | Coming soon | Older CentOS versions may require additional package adjustments |
| Windows | Not supported | Windows can be used to download and upload files, but the dashboard service should run on Linux |

The current installer uses `apt`, so it should be run on Ubuntu or Debian based systems.

Support for RHEL-style systems is coming soon.

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

# Install

There are three supported install options.

## Install Options Summary

| Option | Method | Best For |
|---|---|---|
| Option 1 | Download ZIP from GitHub website and upload to server | Servers without internet access, or users who prefer WinSCP/SCP |
| Option 2 | Download package directly on the Linux server | Servers with internet access to GitHub |
| Option 3 | Clone repository with Git | Servers that should be updated later with `git pull` |

Most users should use **Install Option 2** or the quick tarball flow below.

Fresh package installs use the bundled helper automatically.

MainDB root access is required during setup. The initial connection can start with password or key authentication, either directly as `root` or through a user that can `sudo` to root, but the installer switches the ongoing workflow to certificate/key-based authentication.

---

## Recommended Quick Install (Tarball)

Use this when you want the shortest install path with the packaged installer:

```bash
cd /tmp && curl -L https://github.com/ctera/CTERA-Monitoring-Dashboard/archive/refs/heads/main.tar.gz -o ctera-monitoring-dashboard.tar.gz && rm -rf /tmp/ctera-monitoring-dashboard && mkdir -p /tmp/ctera-monitoring-dashboard && sudo tar -xzf /tmp/ctera-monitoring-dashboard.tar.gz -C /tmp/ctera-monitoring-dashboard --strip-components=1 && cd /tmp/ctera-monitoring-dashboard && sudo bash ./install.sh
```

This uses `/tmp/ctera-monitoring-dashboard` only as a temporary staging folder for the package.
The installer's default final application path is:

```text
/opt/monitoring/ctera-monitoring-dashboard
```

---

## Install Option 1: Download ZIP From GitHub Website and Upload to Server

Use this option when you want to download the package from the GitHub website on your computer, then upload it to the Linux server with WinSCP, SCP, or another file transfer tool.

### Step 1: Download the ZIP

Open the repository in your browser:

```text
https://github.com/ctera/CTERA-Monitoring-Dashboard
```

Click:

```text
Code -> Download ZIP
```

This downloads a file similar to:

```text
CTERA-Monitoring-Dashboard-main.zip
```

Rename the downloaded file to:

```text
ctera-monitoring-dashboard.zip
```

### Step 2: Upload the ZIP to the Linux server

Upload the ZIP file to:

```text
/tmp/ctera-monitoring-dashboard.zip
```

### Step 3: Install unzip if needed

Run this on the Linux server:

```bash
sudo apt update
sudo apt install -y unzip
```

### Step 4: Extract into the application directory

Run this on the Linux server:

```bash
sudo rm -rf /opt/monitoring/ctera-monitoring-dashboard
sudo mkdir -p /opt/monitoring/ctera-monitoring-dashboard

sudo rm -rf /tmp/ctera-monitoring-dashboard-unzip
sudo mkdir -p /tmp/ctera-monitoring-dashboard-unzip

sudo unzip -q /tmp/ctera-monitoring-dashboard.zip -d /tmp/ctera-monitoring-dashboard-unzip

sudo cp -a /tmp/ctera-monitoring-dashboard-unzip/CTERA-Monitoring-Dashboard-main/. /opt/monitoring/ctera-monitoring-dashboard/
```

### Step 5: Run the installer

```bash
cd /opt/monitoring/ctera-monitoring-dashboard
sudo bash ./install.sh
```

During the install, the packaged helper is installed automatically from the bundle in this release.

---

## Install Option 2: Download Package Directly on Server

Use this option when the Linux server has internet access and can reach GitHub.

### Step 1: Install wget if needed

```bash
sudo apt update
sudo apt install -y wget
```

### Step 2: Download the package

```bash
cd /tmp

sudo rm -f ctera-monitoring-dashboard.tar.gz
sudo wget -O ctera-monitoring-dashboard.tar.gz https://github.com/ctera/CTERA-Monitoring-Dashboard/archive/refs/heads/main.tar.gz
```

### Step 3: Extract into the application directory

```bash
sudo rm -rf /opt/monitoring/ctera-monitoring-dashboard
sudo mkdir -p /opt/monitoring/ctera-monitoring-dashboard

sudo tar -xzf /tmp/ctera-monitoring-dashboard.tar.gz \
  -C /opt/monitoring/ctera-monitoring-dashboard \
  --strip-components=1
```

### Step 4: Run the installer

```bash
cd /opt/monitoring/ctera-monitoring-dashboard
sudo bash ./install.sh
```

During the install, the packaged helper is installed automatically from the bundle in this release.

---

## Install Option 3: Clone Repository With Git

Use this option only if the installed server should use `git pull` directly.

### Step 1: Install Git

```bash
sudo apt update
sudo apt install -y git
```

### Step 2: Clone the repository

```bash
sudo mkdir -p /opt/monitoring
cd /opt/monitoring

sudo git clone https://github.com/ctera/CTERA-Monitoring-Dashboard.git ctera-monitoring-dashboard
cd ctera-monitoring-dashboard
```

### Step 3: Run the installer

```bash
sudo bash ./install.sh
```

During the install, the packaged helper is installed automatically from the bundle in this release.

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

There are three supported upgrade options.

## Upgrade Options Summary

| Option | Method | Best For |
|---|---|---|
| Option 1 | Download ZIP from GitHub website and upload to server | Servers without internet access, or users who prefer WinSCP/SCP |
| Option 2 | Download package directly on the Linux server | Servers with internet access to GitHub |
| Option 3 | Git pull from cloned repository | Servers originally installed with `git clone` |

Most users should use **Upgrade Option 2** or the one-command upgrade below.

---

## Recommended One-Command Upgrade

Use this when the Linux server can reach GitHub and you want the shortest supported upgrade path.

```bash
cd /tmp && sudo rm -rf /tmp/ctera-monitoring-dashboard && curl -L https://github.com/ctera/CTERA-Monitoring-Dashboard/archive/refs/heads/main.tar.gz -o /tmp/ctera-monitoring-dashboard.tar.gz && sudo mkdir -p /tmp/ctera-monitoring-dashboard && sudo tar -xzf /tmp/ctera-monitoring-dashboard.tar.gz -C /tmp/ctera-monitoring-dashboard --strip-components=1 && cd /tmp/ctera-monitoring-dashboard && sudo bash ./upgrade.sh --install-dir /opt/monitoring/ctera-monitoring-dashboard
```

What this does:
- downloads the latest package under `/tmp`
- extracts it into `/tmp/ctera-monitoring-dashboard`
- runs `upgrade.sh` from the new package directory
- creates a backup and restore script before changing the installed copy
- preserves customer settings and merges new default threshold entries into `thresholds.yaml`

---

## Upgrade Option 1: Download ZIP From GitHub Website and Upload to Server

Use this option when you downloaded a newer ZIP from GitHub and uploaded it to the Linux server.

### Step 1: Download the latest ZIP

Open the repository in your browser:

```text
https://github.com/ctera/CTERA-Monitoring-Dashboard
```

Click:

```text
Code -> Download ZIP
```

Rename the downloaded file to:

```text
ctera-monitoring-dashboard.zip
```

### Step 2: Upload the ZIP to the Linux server

Upload the ZIP file to:

```text
/tmp/ctera-monitoring-dashboard.zip
```

### Step 3: Install unzip if needed

Run this on the Linux server:

```bash
sudo apt update
sudo apt install -y unzip
```

### Step 4: Extract the upgrade package under `/tmp`

Run this on the Linux server:

```bash
sudo rm -rf /tmp/ctera-monitoring-dashboard
sudo rm -rf /tmp/ctera-monitoring-dashboard-unzip
sudo mkdir -p /tmp/ctera-monitoring-dashboard
sudo mkdir -p /tmp/ctera-monitoring-dashboard-unzip

sudo unzip -q /tmp/ctera-monitoring-dashboard.zip -d /tmp/ctera-monitoring-dashboard-unzip

sudo cp -a /tmp/ctera-monitoring-dashboard-unzip/CTERA-Monitoring-Dashboard-main/. /tmp/ctera-monitoring-dashboard/
```

### Step 5: Run the upgrade

```bash
cd /tmp/ctera-monitoring-dashboard
sudo bash ./upgrade.sh
```

The upgrade script updates the installed application under:

```text
/opt/monitoring/ctera-monitoring-dashboard
```

It also creates a backup before applying the update.

Current upgrade behavior:
- preserves existing runtime configuration
- preserves dashboard UI config
- merges `thresholds.yaml` by keeping installed values and adding any missing shipped defaults

---

## Upgrade Option 2: Download Package Directly on Server

Use this option when the Linux server has internet access and can reach GitHub.

### Step 1: Install wget if needed

```bash
sudo apt update
sudo apt install -y wget
```

### Step 2: Download the latest package

```bash
cd /tmp

sudo rm -f ctera-monitoring-dashboard.tar.gz
sudo wget -O ctera-monitoring-dashboard.tar.gz https://github.com/ctera/CTERA-Monitoring-Dashboard/archive/refs/heads/main.tar.gz
```

### Step 3: Extract under `/tmp`

```bash
sudo rm -rf /tmp/ctera-monitoring-dashboard
sudo mkdir -p /tmp/ctera-monitoring-dashboard

sudo tar -xzf /tmp/ctera-monitoring-dashboard.tar.gz \
  -C /tmp/ctera-monitoring-dashboard \
  --strip-components=1
```

### Step 4: Run the upgrade

```bash
cd /tmp/ctera-monitoring-dashboard
sudo bash ./upgrade.sh
```

The upgrade script updates the installed application under:

```text
/opt/monitoring/ctera-monitoring-dashboard
```

It also creates a backup before applying the update.

---

## Upgrade Option 3: Git Pull From Cloned Repository

Use this option only if the server was installed using `git clone`.

```bash
cd /opt/monitoring/ctera-monitoring-dashboard

sudo git pull
sudo bash ./upgrade.sh
```

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
