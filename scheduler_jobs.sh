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
  (( now - epoch_last >= interval_minutes * 60 ))
}

run_due_job() {
  local env_id="$1"
  local env_name="$2"
  local job_name="$3"
  local interval_minutes="$4"
  local env_file="${RUNTIME_DIR}/environment-${env_id}.env"
  local script_path="${SCRIPT_DIR}/${job_name}_jobs.sh"
  local log_path="${LOG_DIR}/${job_name}.log"

  interval_minutes="$(sanitize_minutes "${interval_minutes}")"

  if ! is_due "${job_name}" "${env_id}" "${interval_minutes}"; then
    log "Skipping ${job_name} for ${env_name} (id=${env_id}); not due yet (${interval_minutes} min interval)."
    return 0
  fi

  if [[ ! -f "${env_file}" ]]; then
    log "Skipping ${job_name} for ${env_name} (id=${env_id}); runtime env file not found at ${env_file}."
    mark_run "${job_name}" "${env_id}"
    return 0
  fi

  append_job_log "${job_name}" "Scheduler launching ${job_name}_jobs.sh for environment ${env_name} (id=${env_id})."
  log "Running ${job_name} for ${env_name} (id=${env_id}) using ${env_file}"
  if FEATHERDASH_CONFIG="${env_file}" "${script_path}" >> "${log_path}" 2>&1; then
    append_job_log "${job_name}" "Scheduler completed ${job_name}_jobs.sh for environment ${env_name} (id=${env_id})."
    log "Completed ${job_name} for ${env_name} (id=${env_id})"
  else
    append_job_log "${job_name}" "Scheduler saw ${job_name}_jobs.sh fail for environment ${env_name} (id=${env_id})."
    log "Failed ${job_name} for ${env_name} (id=${env_id})"
  fi
  mark_run "${job_name}" "${env_id}"
}

sql="SELECT id, environment_name, COALESCE(portal_schedule_minutes,60), COALESCE(filer_schedule_minutes,60) FROM environments WHERE enabled = 1 ORDER BY lower(environment_name), id;"
rows="$(sqlite3 -tabs "${DB_PATH}" "${sql}")"
if [[ -z "${rows}" ]]; then
  log "No enabled portal environments found. Scheduler idle."
  exit 0
fi

while IFS=$'\t' read -r env_id env_name portal_minutes filer_minutes; do
  [[ -z "${env_id:-}" ]] && continue
  run_due_job "${env_id}" "${env_name}" "portal" "${portal_minutes}"
  run_due_job "${env_id}" "${env_name}" "filer" "${filer_minutes}"
done <<< "${rows}"
