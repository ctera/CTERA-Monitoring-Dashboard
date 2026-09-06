#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BASE_CONFIG="${FEATHERDASH_CONFIG:-/etc/ctera-monitoring-dashboard.env}"
STATE_DIR="${FEATHERDASH_STATE_DIR:-${SCRIPT_DIR}/state}"
DB_PATH="${FEATHERDASH_NOTIFICATIONS_DB:-${STATE_DIR}/notifications.sqlite}"
RUNTIME_DIR="${STATE_DIR}/runtime_env"
SCHED_STATE_DIR="${STATE_DIR}/scheduler"
LOG_DIR="${FEATHERDASH_LOG_DIR:-/var/log/ctera-monitoring-dashboard}"

mkdir -p "${SCHED_STATE_DIR}" "${RUNTIME_DIR}" "${LOG_DIR}"

timestamp() {
  date '+%Y-%m-%d %H:%M:%S'
}

log() {
  printf '%s %s\n' "$(timestamp)" "$*"
}

append_job_log() {
  local job_name="$1"
  shift
  printf '%s %s\n' "$(timestamp)" "$*" >> "${LOG_DIR}/${job_name}.log"
}

if ! command -v sqlite3 >/dev/null 2>&1; then
  log "sqlite3 is required for scheduler_jobs.sh"
  exit 1
fi

exec 9>"${SCHED_STATE_DIR}/scheduler.lock"
if ! flock -n 9; then
  log "Scheduler already running. Exiting."
  exit 0
fi

if [[ ! -f "${DB_PATH}" ]]; then
  log "Notifications database not found at ${DB_PATH}. Nothing to schedule yet."
  exit 0
fi

if [[ "${FEATHERDASH_SCHEDULER_PAUSED:-false}" =~ ^(1|true|yes|on)$ ]]; then
  log "Scheduler paused by FEATHERDASH_SCHEDULER_PAUSED. Skipping automatic jobs."
  exit 0
fi

sanitize_minutes() {
  local value="${1:-}"
  if [[ "${value}" =~ ^[0-9]+$ ]] && [[ "${value}" -gt 0 ]]; then
    printf '%s' "${value}"
  else
    printf '60'
  fi
}

mark_run() {
  local job_name="$1"
  local env_id="$2"
  date +%s > "${SCHED_STATE_DIR}/${job_name}-${env_id}.last"
}

mark_success() {
  local job_name="$1"
  local env_id="$2"
  date +%s > "${SCHED_STATE_DIR}/${job_name}-${env_id}.last_ok"
}

is_due() {
  local job_name="$1"
  local env_id="$2"
  local interval_minutes="$3"
  local last_file="${SCHED_STATE_DIR}/${job_name}-${env_id}.last"
  local now epoch_last
  now="$(date +%s)"
  if [[ ! -f "${last_file}" ]]; then
    return 0
  fi
  epoch_last="$(cat "${last_file}" 2>/dev/null || echo 0)"
  [[ ! "${epoch_last}" =~ ^[0-9]+$ ]] && return 0
  # Avoid bare ((...)) as the function's last command under "set -e"
  # (a false comparison would otherwise abort the whole scheduler).
  if (( now - epoch_last >= interval_minutes * 60 )); then
    return 0
  fi
  return 1
}

job_run_lock_path() {
  local job_name="$1"
  local env_id="$2"
  printf '%s/%s-%s.run.lock' "${SCHED_STATE_DIR}" "${job_name}" "${env_id}"
}

# True if another scheduler tick already holds this collector's run lock.
is_job_running() {
  local run_lock="$1"
  exec 8>"${run_lock}"
  if flock -n 8; then
    flock -u 8 || true
    exec 8>&- || true
    return 1
  fi
  exec 8>&- || true
  return 0
}

# Queue due work under the global lock, then release before long collectors run.
# Holding the global flock for an entire filer job made cron print "already running"
# for hours/days whenever one portal was slow or wedged.
claim_due_job() {
  local env_id="$1"
  local env_name="$2"
  local job_name="$3"
  local interval_minutes="$4"
  local env_file="${RUNTIME_DIR}/environment-${env_id}.env"
  local run_lock

  interval_minutes="$(sanitize_minutes "${interval_minutes}")"
  run_lock="$(job_run_lock_path "${job_name}" "${env_id}")"

  if ! is_due "${job_name}" "${env_id}" "${interval_minutes}"; then
    log "Skipping ${job_name} for ${env_name} (id=${env_id}); not due yet (${interval_minutes} min interval)."
    return 0
  fi

  if is_job_running "${run_lock}"; then
    log "Skipping ${job_name} for ${env_name} (id=${env_id}); already running."
    return 0
  fi

  if [[ ! -f "${env_file}" ]]; then
    log "Skipping ${job_name} for ${env_name} (id=${env_id}); runtime env file not found at ${env_file}."
    mark_run "${job_name}" "${env_id}"
    return 0
  fi

  # Do not mark_run here: stamp .last only after this tick holds the per-job run lock,
  # otherwise a later cron tick can queue a second overlapping collector.
  DUE_JOBS+=("${env_id}"$'\t'"${env_name}"$'\t'"${job_name}"$'\t'"${env_file}")
  log "Queued ${job_name} for ${env_name} (id=${env_id}); will run after scheduler lock is released."
}

run_claimed_job() {
  local env_id="$1"
  local env_name="$2"
  local job_name="$3"
  local env_file="$4"
  local script_path="${SCRIPT_DIR}/${job_name}_jobs.sh"
  local log_path="${LOG_DIR}/${job_name}.log"
  local run_lock
  run_lock="$(job_run_lock_path "${job_name}" "${env_id}")"

  exec 8>"${run_lock}"
  if ! flock -n 8; then
    log "Skipping ${job_name} for ${env_name} (id=${env_id}); already running."
    exec 8>&- || true
    return 0
  fi

  # Hold fd 8 for the whole collector so overlapping cron ticks cannot start a second copy.
  # The child inherits fd 8; if this scheduler tick is killed, the collector keeps the
  # per-job lock and still blocks a duplicate start (global scheduler.lock stays free).
  mark_run "${job_name}" "${env_id}"
  append_job_log "${job_name}" "Scheduler launching ${job_name}_jobs.sh for environment ${env_name} (id=${env_id})."
  log "Running ${job_name} for ${env_name} (id=${env_id}) using ${env_file}"
  if FEATHERDASH_CONFIG="${env_file}" "${script_path}" >> "${log_path}" 2>&1; then
    append_job_log "${job_name}" "Scheduler completed ${job_name}_jobs.sh for environment ${env_name} (id=${env_id})."
    log "Completed ${job_name} for ${env_name} (id=${env_id})"
    mark_success "${job_name}" "${env_id}"
  else
    append_job_log "${job_name}" "Scheduler saw ${job_name}_jobs.sh fail for environment ${env_name} (id=${env_id})."
    log "Failed ${job_name} for ${env_name} (id=${env_id})"
  fi

  flock -u 8 || true
  exec 8>&- || true
}

paused_setting="$(sqlite3 -tabs "${DB_PATH}" "SELECT COALESCE((SELECT setting_value FROM app_settings WHERE setting_key = 'scheduler_paused' LIMIT 1),'false');" 2>/dev/null || echo false)"
if [[ "${paused_setting}" =~ ^(1|true|yes|on)$ ]]; then
  log "Scheduler paused in app settings. Skipping automatic jobs."
  exit 0
fi

sql="SELECT id, environment_name, COALESCE(portal_schedule_minutes,60), COALESCE(filer_schedule_minutes,60) FROM environments WHERE enabled = 1 ORDER BY lower(environment_name), id;"
rows="$(sqlite3 -tabs "${DB_PATH}" "${sql}")"
if [[ -z "${rows}" ]]; then
  log "No enabled portal environments found. Scheduler idle."
  exit 0
fi

DUE_JOBS=()
while IFS=$'\t' read -r env_id env_name portal_minutes filer_minutes; do
  [[ -z "${env_id:-}" ]] && continue
  claim_due_job "${env_id}" "${env_name}" "portal" "${portal_minutes}"
  claim_due_job "${env_id}" "${env_name}" "filer" "${filer_minutes}"
done <<< "${rows}"

# Drop the global lock before collectors run so the next cron tick can schedule other work.
flock -u 9 || true
exec 9>&- || true

if [[ "${#DUE_JOBS[@]}" -eq 0 ]]; then
  log "No due collector jobs this tick."
  exit 0
fi

for entry in "${DUE_JOBS[@]}"; do
  IFS=$'\t' read -r env_id env_name job_name env_file <<< "${entry}"
  run_claimed_job "${env_id}" "${env_name}" "${job_name}" "${env_file}"
done
