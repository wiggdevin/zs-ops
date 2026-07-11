#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# watchdog.sh — ONE health sweep + best-effort auto-heal.
# Run by launchd every ~5 min (launchd owns the cadence; NOT an infinite loop).
# Emits one JSONL line per component to $WATCHDOG_LOG; heal failures leave a
# breadcrumb (stderr first line) in $WATCHDOG_LOG.heal so incidents are diagnosable.
#
# 2026-06-10: memory stack replaced — zs-memory (native launchd service, :8765,
# SQLite) supersedes the OpenMemory/Qdrant docker stack. Colima/docker checks
# remain ONLY for zs-ai-os-postgres; qdrant/mem0-config stanzas removed.
#
# `set -e` is intentionally OFF — a watchdog must run EVERY check even when one
# fails. Errors are handled explicitly.
# ─────────────────────────────────────────────────────────────────────────────
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WATCHDOG_LOG="${WATCHDOG_LOG:-$HOME/Library/Logs/zs-watchdog.jsonl}"
MEMORY_API="${MEMORY_API:-http://localhost:8765}"
OLLAMA_API="${OLLAMA_API:-http://localhost:11434}"
BACKUP_DIR="${BACKUP_DIR:-$HOME/Backups/zs-mem0-backups}"
# iCloud mirror written by zs-memory/ops/backup-zs-memory.sh (default since 2026-07-09)
MIRROR_BACKUP_DIR="${MIRROR_BACKUP_DIR:-$HOME/Library/Mobile Documents/com~apple~CloudDocs/Backups/zs-mem0-backups}"
BACKUP_MAX_AGE_HOURS="${BACKUP_MAX_AGE_HOURS:-26}"
DREAM_LABEL="${DREAM_LABEL:-com.zs.dream}"
DREAM_ALERT_STAMP_DIR="${DREAM_ALERT_STAMP_DIR:-/tmp}"
SCHED_STAMP_DIR="${SCHED_STAMP_DIR:-$HOME/Library/Logs/zs-watchdog-stamps}"
SCHED_DEDUP_SECONDS="${SCHED_DEDUP_SECONDS:-86400}"
HERMES_ENV="${HERMES_ENV:-$HOME/.hermes/.env}"
DRY_RUN=0
SCHED_ONLY=0

case ":$PATH:" in
  *":$HOME/.local/bin:"*) ;;
  *) PATH="$HOME/.local/bin:$PATH" ;;
esac
export PATH

mkdir -p "$(dirname "$WATCHDOG_LOG")"
unhealthy=()

log_event() { # log_event <component> <status> <action> — write one JSONL line, no alarm tracking
  local ts; ts="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  if command -v jq >/dev/null 2>&1; then
    { jq -cn --arg ts "$ts" --arg c "$1" --arg s "$2" --arg a "$3" \
      '{ts:$ts,component:$c,status:$s,action:$a}' >> "$WATCHDOG_LOG"; } 2>/dev/null || true
  else
    { printf '{"ts":"%s","component":"%s","status":"%s","action":"%s"}\n' "$ts" "$1" "$2" "$3" >> "$WATCHDOG_LOG"; } 2>/dev/null || true
  fi
}

event() { # event <component> <status> <action> — log + flag for the desktop alarm
  log_event "$1" "$2" "$3"
  [ "$2" = "healthy" ] || unhealthy+=("$1")
}

# Run a heal command; on failure, persist the stderr first-line as a breadcrumb
# (instead of silencing it with `|| true`). Returns the command's exit code.
heal() { # heal <label> <cmd...>
  local label="$1"; shift; local err rc
  err="$( { "$@" >/dev/null; } 2>&1 )"; rc=$?
  if [ $rc -ne 0 ]; then
    printf '%s [%s] rc=%d: %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$label" "$rc" \
      "$(printf '%s' "$err" | head -1)" >> "$WATCHDOG_LOG.heal"
  fi
  return $rc
}

http_ok() { curl -fsS --max-time 5 "$1" >/dev/null 2>&1; }

check_docker_engine() {
  if docker info >/dev/null 2>&1; then event docker healthy none; return; fi
  event docker down heal-deferred   # Colima heal (below) is what brings the engine back
}

check_colima() {
  if colima status >/dev/null 2>&1; then event colima healthy none; return; fi
  # No --disk: the VM already exists; colima cannot shrink it and errors on a mismatch.
  heal colima-start colima start --cpu 8 --memory 16 --vm-type vz --mount-type virtiofs
  if colima status >/dev/null 2>&1; then event colima healthy restarted
  else event colima down heal-failed; fi
}

check_zs_memory() {
  if http_ok "$MEMORY_API/health"; then event zs-memory healthy none; return; fi
  heal zs-memory-kickstart launchctl kickstart -k "gui/$(id -u)/com.zs.memory"
  sleep 3
  if http_ok "$MEMORY_API/health"; then event zs-memory healthy restarted
  else event zs-memory down heal-failed; fi
}

check_ollama() {
  if http_ok "$OLLAMA_API/api/tags"; then event ollama healthy none; return; fi
  # Ollama runs as the APP cask via the com.zs.ollama LaunchAgent (the brew FORMULA ships
  # no llama-server runner). Heal by kicking that agent, NOT `brew services start ollama`.
  heal ollama-start launchctl kickstart -k "gui/$(id -u)/com.zs.ollama"
  sleep 3
  if http_ok "$OLLAMA_API/api/tags"; then event ollama healthy restarted
  else event ollama down heal-failed; fi
}

check_backup_freshness() {
  local newest
  newest="$(ls -t "$BACKUP_DIR"/zs-memory-backup-*.db.gz 2>/dev/null | head -1)"
  if [ -z "$newest" ]; then event backup missing none; return; fi
  local age_h=$(( ( $(date +%s) - $(stat -f %m "$newest") ) / 3600 ))
  if [ "$age_h" -le "$BACKUP_MAX_AGE_HOURS" ]; then event backup healthy none
  else event backup stale "newest=${age_h}h"; fi
}

check_mirror_freshness() { # same contract as check_backup_freshness, for the off-box mirror
  # Only count mirror files that pass a quick integrity stat — size > 0 and not
  # a dataless File Provider placeholder — so a corrupt/evicted newest file
  # can't mask older valid mirrors as "fresh".
  local newest="" f
  while IFS= read -r f; do
    [ -s "$f" ] || continue
    ls -lO "$f" 2>/dev/null | grep -q dataless && continue
    newest="$f"
    break
  done < <(ls -t "$MIRROR_BACKUP_DIR"/zs-memory-backup-*.db.gz 2>/dev/null)
  # TCC blocks dir enumeration of Mobile Documents for launchd-context bash
  # (direct-path stat still works) — fall back to the basename stamped by
  # backup-zs-memory.sh after its last verified mirror.
  if [ -z "$newest" ] && [ -r "$BACKUP_DIR/.mirror-stamp" ]; then
    f="$MIRROR_BACKUP_DIR/$(head -1 "$BACKUP_DIR/.mirror-stamp")"
    if [ -s "$f" ] && ! ls -lO "$f" 2>/dev/null | grep -q dataless; then newest="$f"; fi
  fi
  if [ -z "$newest" ]; then event backup-mirror missing none; return; fi
  local age_h=$(( ( $(date +%s) - $(stat -f %m "$newest") ) / 3600 ))
  if [ "$age_h" -le "$BACKUP_MAX_AGE_HOURS" ]; then event backup-mirror healthy none
  else event backup-mirror stale "newest=${age_h}h"; fi
}

# The nightly dream pass (com.zs.dream, 04:00) exits non-zero when a consolidation
# run is degraded (LLM/embedding failure) — otherwise a "0 proposals" digest can read
# as a healthy quiet night when the model actually failed. It is a once-a-day batch the
# watchdog can't self-heal, so log every sweep but raise the desktop alarm only once/day.
check_dream() {
  local info status
  info="$(launchctl list "$DREAM_LABEL" 2>/dev/null)" || { event dream absent none; return; }
  status="$(printf '%s' "$info" | sed -n 's/.*"LastExitStatus" = \([0-9-]*\).*/\1/p')"
  if [ -z "$status" ] || [ "$status" = "0" ]; then event dream healthy none; return; fi
  local stamp="$DREAM_ALERT_STAMP_DIR/zs-dream-alerted-$(date -u +%F)"
  if [ -e "$stamp" ]; then
    log_event dream degraded "exit=$status (already alerted today)"   # logged, no re-alarm
  else
    : > "$stamp"
    event dream degraded "exit=$status"                                # one desktop alarm/day
  fi
}

parse_args() {
  while [ "$#" -gt 0 ]; do
    case "$1" in
      --dry-run) DRY_RUN=1 ;;
      --sched-only) SCHED_ONLY=1 ;;
      *)
        printf 'unknown flag: %s\n' "$1" >&2
        exit 2
        ;;
    esac
    shift
  done
}

read_env_key() {
  local key="$1" value
  [ -f "$HERMES_ENV" ] || return 1
  value="$(awk -v key="$key" -F= '
    {
      name=$1
      sub(/^[[:space:]]*export[[:space:]]+/, "", name)
      gsub(/^[[:space:]]+|[[:space:]]+$/, "", name)
      if (name == key) {
        sub(/^[^=]*=/, "", $0)
        print $0
        exit
      }
    }
  ' "$HERMES_ENV")"
  printf '%s' "$value" | sed 's/^[[:space:]]*//;s/[[:space:]]*$//;s/^"//;s/"$//'
}

notify_telegram() {
  local message="$1" token allowed chat_id
  token="$(read_env_key TELEGRAM_BOT_TOKEN)"
  allowed="$(read_env_key TELEGRAM_ALLOWED_USERS)"
  chat_id="${allowed%%,*}"
  chat_id="$(printf '%s' "$chat_id" | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')"

  if [ "$DRY_RUN" -eq 1 ]; then
    printf 'DRY RUN telegram chat_id=%s\n%s\n' "${chat_id:-missing}" "$message"
    return 0
  fi
  if [ -z "$token" ] || [ -z "$chat_id" ]; then
    log_event sched:telegram degraded missing-config
    return 1
  fi

  printf 'url = "https://api.telegram.org/bot%s/sendMessage"\n' "$token" |
    curl -fsS --max-time 10 --config - \
      --data-urlencode "chat_id=$chat_id" \
      --data-urlencode "text=$message" >/dev/null 2>&1
}

sched_signature() {
  printf '%s' "$1|$2|$3" | shasum -a 256 | awk '{print substr($1, 1, 16)}'
}

stamp_is_fresh() {
  local stamp="$1" mtime now age
  [ -f "$stamp" ] || return 1
  mtime="$(stat -f %m "$stamp" 2>/dev/null)" || return 1
  now="$(date +%s)"
  age=$(( now - mtime ))
  [ "$age" -lt "$SCHED_DEDUP_SECONDS" ]
}

flag_scheduled_failure() {
  local source="$1" job="$2" detail="$3" sig stamp message
  mkdir -p "$SCHED_STAMP_DIR" 2>/dev/null || true
  sig="$(sched_signature "$source" "$job" "$detail")"
  stamp="$SCHED_STAMP_DIR/$sig"

  if stamp_is_fresh "$stamp"; then
    log_event "sched:$job" failing deduped
    return
  fi

  message="$(printf 'zs-watchdog scheduled job failure\nsource: %s\njob: %s\ndetail: %s' "$source" "$job" "$detail")"
  notify_telegram "$message" || true
  if [ "$DRY_RUN" -eq 0 ]; then touch "$stamp" 2>/dev/null || true; fi
  log_event "sched:$job" failing alerted
}

check_launchd_jobs() {
  local rows
  rows="$(launchctl list 2>/dev/null)" || return
  while IFS=$'\t' read -r label status; do
    [ -n "$label" ] || continue
    flag_scheduled_failure launchd "$label" "exit=$status"
  done < <(printf '%s\n' "$rows" | awk '
    $3 ~ /^(com\.zs\.|com\.zerosum|ai\.hermes\.)/ &&
    $2 ~ /^[0-9]+$/ &&
    ($2 + 0) > 0 &&
    ($2 + 0) != 143 {
      print $3 "\t" $2
    }
  ')
}

check_hermes_cron_jobs() {
  local output rc first_line
  output="$(hermes cron list 2>&1)"; rc=$?
  if [ "$rc" -ne 0 ]; then
    first_line="$(printf '%s\n' "$output" | sed -n '1p')"
    flag_scheduled_failure hermes-cron hermes-cron-unreachable "rc=$rc ${first_line:-hermes cron list failed}"
    return
  fi

  while IFS=$'\t' read -r job error_line; do
    [ -n "$job" ] || continue
    flag_scheduled_failure hermes-cron "$job" "$error_line"
  done < <(printf '%s\n' "$output" | awk '
    /^[[:space:]]*[[:xdigit:]]+[[:space:]]+\[/ { name=""; next }
    /^[[:space:]]*Name:[[:space:]]*/ {
      name=$0
      sub(/^[[:space:]]*Name:[[:space:]]*/, "", name)
      next
    }
    /^[[:space:]]*Last run:[[:space:]]*/ && tolower($0) ~ /error/ {
      line=$0
      sub(/^[[:space:]]*Last run:[[:space:]]*/, "", line)
      if (name == "") name="unknown-hermes-job"
      print name "\t" line
    }
  ')
}

check_scheduled_jobs() {
  check_launchd_jobs
  check_hermes_cron_jobs
}

main() {
  parse_args "$@"
  if [ "$SCHED_ONLY" -eq 1 ]; then
    check_scheduled_jobs
    return
  fi

  if command -v colima >/dev/null 2>&1; then check_colima; else event colima absent none; fi
  if command -v docker >/dev/null 2>&1; then check_docker_engine; fi
  check_zs_memory
  check_ollama
  check_backup_freshness
  check_mirror_freshness
  check_dream
  check_scheduled_jobs

  # Exercise the telemetry sampler if present. Do NOT silence its stderr — a broken
  # sampler should leave evidence in launchd's captured stderr.
  if [ -x "$SCRIPT_DIR/telemetry.sh" ]; then
    "$SCRIPT_DIR/telemetry.sh" || printf 'telemetry sampler failed (rc=%d)\n' "$?" >&2
  fi

  if [ "${#unhealthy[@]}" -gt 0 ]; then
    local msg="memory stack unhealthy: ${unhealthy[*]}"
    osascript -e "display notification \"${msg//\"/\'}\" with title \"zs-watchdog\"" >/dev/null 2>&1 || true
    printf '\033[1;31m✗ %s\033[0m\n' "$msg" >&2
    exit 1
  fi
  printf '\033[1;32m✓ memory stack healthy\033[0m\n' >&2
}
main "$@"
