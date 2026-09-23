#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# watchdog.sh — ONE health sweep + best-effort auto-heal.
# Run by launchd every ~5 min (launchd owns the cadence; NOT an infinite loop).
# Emits one JSONL line per component to $WATCHDOG_LOG; heal failures leave a
# breadcrumb (stderr first line) in $WATCHDOG_LOG.heal so incidents are diagnosable.
#
# 2026-06-10: memory stack replaced — zs-memory (native launchd service, :8765,
# SQLite) supersedes the OpenMemory/Qdrant docker stack; qdrant/mem0-config
# stanzas removed.
# 2026-08-01: docker runtime moved Colima → OrbStack (both Supabase stacks
# migrated); the container-runtime check now heals OrbStack, not Colima.
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
WATCHDOG_LABEL="${WATCHDOG_LABEL:-com.zs.watchdog}"   # this script's own launchd label
DREAM_ALERT_STAMP_DIR="${DREAM_ALERT_STAMP_DIR:-/tmp}"
SCHED_STAMP_DIR="${SCHED_STAMP_DIR:-$HOME/Library/Logs/zs-watchdog-stamps}"
SCHED_DEDUP_SECONDS="${SCHED_DEDUP_SECONDS:-86400}"
HERMES_ENV="${HERMES_ENV:-$HOME/.hermes/.env}"
DRY_RUN=0
SCHED_ONLY=0
REPORT_ONLY=0
# The memory stack is native launchd now and must not wake a container runtime.
# Container projects are started explicitly by the project that needs them.
# Opt back into the legacy runtime monitoring only for a deliberate session.
WATCHDOG_CHECK_CONTAINER_RUNTIME="${WATCHDOG_CHECK_CONTAINER_RUNTIME:-0}"
# Stamped once per COMPLETED full sweep. This is the only artefact that can
# distinguish "the watchdog ran and everything is broken" from "the watchdog
# itself never ran" — an exit status cannot, because a sweep that never starts
# never sets one. External staleness checks read it; see main().
WATCHDOG_HEARTBEAT="${WATCHDOG_HEARTBEAT:-$HOME/Library/Logs/zs-watchdog-heartbeat}"

case ":$PATH:" in
  *":$HOME/.local/bin:"*) ;;
  *) PATH="$HOME/.local/bin:$PATH" ;;
esac
export PATH

mkdir -p "$(dirname "$WATCHDOG_LOG")"
unhealthy=()

# Set when ANY event failed to reach the log. Previously these writes were
# discarded with `|| true`, so an unwritable Logs dir produced a sweep with a
# fresh heartbeat, no error events, and exit 0 — every signal saying "fine" while
# structured monitoring had silently stopped. Being unable to record is an
# execution failure and main() exits nonzero on it.
LOG_WRITE_FAILED=0
# Set when a Telegram alert could not be delivered (see check_scheduled_jobs).
ALERT_DELIVERY_FAILED=0

log_event() { # log_event <component> <status> <action> — write one JSONL line, no alarm tracking
  local ts; ts="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  if command -v jq >/dev/null 2>&1; then
    { jq -cn --arg ts "$ts" --arg c "$1" --arg s "$2" --arg a "$3" \
      '{ts:$ts,component:$c,status:$s,action:$a}' >> "$WATCHDOG_LOG"; } 2>/dev/null || LOG_WRITE_FAILED=1
  else
    { printf '{"ts":"%s","component":"%s","status":"%s","action":"%s"}\n' "$ts" "$1" "$2" "$3" >> "$WATCHDOG_LOG"; } 2>/dev/null || LOG_WRITE_FAILED=1
  fi
}

event() { # event <component> <status> <action> — log + flag for the desktop alarm
  log_event "$1" "$2" "$3"
  [ "$2" = "healthy" ] || unhealthy+=("$1")
}

# Run a heal command; on failure, persist the stderr first-line as a breadcrumb
# (instead of silencing it with `|| true`). Returns the command's exit code.
heal() { # heal <label> <cmd...>
  local label="$1"; shift; local err rc errf
  # stderr goes to a FILE, not `$( )`. Command substitution waits for EOF on a
  # pipe, so a background grandchild that inherits stderr keeps heal() blocked
  # long after the bounded command returned — measured at 4s against a 1s bound,
  # which would defeat every bound in this file at the capture layer.
  errf="$(mktemp "${TMPDIR:-/tmp}/heal.XXXXXX" 2>/dev/null)" || errf=""
  if [ -n "$errf" ]; then
    { "$@" >/dev/null; } 2>"$errf"; rc=$?
    err="$(head -1 "$errf" 2>/dev/null)"; rm -f "$errf" 2>/dev/null
  else
    { "$@" >/dev/null; } 2>/dev/null; rc=$?; err="(stderr unavailable)"
  fi
  if [ $rc -ne 0 ]; then
    printf '%s [%s] rc=%d: %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$label" "$rc" \
      "$err" >> "$WATCHDOG_LOG.heal"
  fi
  return $rc
}

http_ok() { curl -fsS --max-time 5 "$1" >/dev/null 2>&1; }

# bounded() / bounded_matches() — the single wall-clock bound, shared by every ops
# script so the copies cannot drift apart again. See lib/bounded.sh for why.
# shellcheck source=lib/bounded.sh
if ! . "$SCRIPT_DIR/lib/bounded.sh" 2>/dev/null; then
  printf 'watchdog: FATAL — cannot source %s/lib/bounded.sh; refusing to run unbounded\n' \
    "$SCRIPT_DIR" >&2
  exit 1
fi

# The three ways "docker is not working" happen are not interchangeable, and only
# one of them is fixed by restarting OrbStack — the old blanket `heal-deferred`
# action claimed a heal that would never arrive for the other two.
#   timed out      → the socket is wedged; `orb start` does not clear that.
#   context drift  → the engine is fine and the CLI is pointed elsewhere. This is
#                    what `colima delete` did: it removed its own context and left
#                    the CLI on `default`, a dead socket, while every container
#                    kept serving. Reported, deliberately NOT auto-healed — the
#                    context is a user-facing setting and silently flipping it back
#                    every 5 minutes would fight anyone working on another context.
#                    `zs-os up` repairs it on explicit request.
#   unreachable    → genuinely down; the OrbStack heal above is the right answer.
check_docker_engine() {
  local rc ctx
  bounded 10 docker info >/dev/null 2>&1; rc=$?
  if [ "$rc" -eq 0 ]; then event docker healthy none; return; fi
  if [ "$rc" -eq 124 ]; then
    event docker unresponsive "docker info timed out — socket wedged, not fixable by restarting OrbStack"
    return
  fi
  if bounded 10 docker --context orbstack info >/dev/null 2>&1; then
    ctx="$(bounded 5 docker context show 2>/dev/null || echo unknown)"
    event docker context-drift "CLI context '$ctx' cannot reach the engine but 'orbstack' can — run: zs-os up"
    return
  fi
  event docker down engine-unreachable   # the OrbStack heal is what brings this back
}

# The other half of the mutual watch. com.zs.watchdog-liveness is what notices a
# dead watchdog on a schedule, so if IT quietly stops (unloaded, hung, crashing at
# startup) the heartbeat can go stale with nothing left to look at it. The launchd
# scan cannot see that: an unloaded job has no row at all, and a hung one holds a
# PID, which the scan deliberately skips. Its own heartbeat is the signal.
# Threshold is 2x its 900s interval, so one skipped run is not an alarm.
LIVENESS_HEARTBEAT="${LIVENESS_HEARTBEAT:-$HOME/Library/Logs/zs-watchdog-liveness-heartbeat}"
LIVENESS_STALE_SECONDS="${LIVENESS_STALE_SECONDS:-1800}"

check_liveness_monitor() {
  local now age woke awake
  now=$(date +%s)
  # Same post-wake grace as everywhere else: launchd skips StartInterval timers
  # during sleep, so a stamp is legitimately old right after a lid-open.
  woke=$(sysctl -n kern.waketime 2>/dev/null | sed -n 's/^{ sec = \([0-9]*\).*/\1/p')
  if [ -n "$woke" ]; then
    awake=$(( now - woke ))
    if [ "$awake" -ge 0 ] && [ "$awake" -lt "$LIVENESS_STALE_SECONDS" ]; then
      event liveness-monitor healthy "post-wake grace"; return
    fi
  fi
  if [ ! -r "$LIVENESS_HEARTBEAT" ]; then
    event liveness-monitor down "no heartbeat — com.zs.watchdog-liveness is not running"
    return
  fi
  age=$(( now - $(stat -f %m "$LIVENESS_HEARTBEAT" 2>/dev/null || echo 0) ))
  if [ "$age" -ge 0 ] && [ "$age" -lt "$LIVENESS_STALE_SECONDS" ]; then
    event liveness-monitor healthy "heartbeat ${age}s old"
  else
    event liveness-monitor down "heartbeat ${age}s old — nothing is watching the watchdog"
  fi
}

# 2026-08-01: the docker runtime moved Colima → OrbStack, so this heals OrbStack.
# Colima is deliberately NOT healed any more: its only stated reason to exist was
# zs-ai-os-postgres, which is long gone, and the 5-minute heal kept resurrecting an
# 8-CPU/16GB VM with nothing running in it. The Colima VM has since been DELETED
# (`colima delete --data`, 41GB reclaimed). NOTE (verified 2026-09-22): no
# ~/Backups/colima/ archive exists, and a new colima VM was created 2026-09-13
# (~/.colima, 8.7 GB datadisk, stopped). Never re-add a colima heal here — with no
# instance left, `colima start` does not restore anything, it provisions a brand
# new empty VM.
# Every orb call is bounded: a wedged OrbStack IPC channel makes `orb status`
# block forever, which would hang the sweep before it ever reaches the heartbeat.
# `orb start` gets a longer budget because booting a VM legitimately takes time.
check_orbstack() {
  local rc
  bounded_matches 10 "*[Rr]unning*" orb status; rc=$?
  if [ "$rc" -eq 0 ]; then event orbstack healthy none; return; fi
  if [ "$rc" -eq 125 ]; then
    # Could not run the probe at all (e.g. no writable temp dir). "Unknown" is
    # not "stopped" — starting a healthy VM every 5 min would be worse than the
    # gap in reporting.
    event orbstack unknown "status probe could not run — not restarting on an inconclusive result"
    return
  fi
  if [ "$rc" -eq 124 ]; then
    # A wedged control channel is not a stopped VM. `orb start` against a hung
    # daemon achieves nothing and can make things worse; report and leave it.
    event orbstack unresponsive "orb status timed out — control channel wedged, not restarting"
    return
  fi
  heal orbstack-start bounded 90 orb start
  # `orb start` can report a start timeout while the VM does come up, so trust the
  # follow-up status probe rather than the heal command's exit code.
  if bounded_matches 10 "*[Rr]unning*" orb status; then event orbstack healthy restarted
  else event orbstack down heal-failed; fi
}

# zs-memory restart policy. `kickstart -k` SIGTERMs the running process, so a
# single slow /health used to cost a live restart (and aborted every in-flight
# request). Two things changed:
#   1. Restart only after ZS_MEMORY_FAIL_THRESHOLD *consecutive* failed sweeps.
#      One slow probe is not a dead service; a genuinely dead one still gets
#      healed on the next sweep (~5 min later).
#   2. Read the BODY. /health deliberately stays 200 when the embedder is down
#      (200 avoids kickstart flapping on an Ollama blip) — so a 2xx-only check
#      could never see embedder failure. "degraded" is now surfaced as its own
#      event and still never restarts.
ZS_MEMORY_FAIL_STATE="${ZS_MEMORY_FAIL_STATE:-$HOME/Library/Logs/zs-watchdog-zs-memory-fails}"
ZS_MEMORY_FAIL_THRESHOLD="${ZS_MEMORY_FAIL_THRESHOLD:-2}"
# Above the service's own worst case: bounded ping lock wait (1s) + embedder
# probe (5s). The old 5s left zero margin and timed out on a healthy service.
ZS_MEMORY_HEALTH_TIMEOUT="${ZS_MEMORY_HEALTH_TIMEOUT:-8}"

check_zs_memory() {
  local body
  if body="$(curl -fsS --max-time "$ZS_MEMORY_HEALTH_TIMEOUT" "$MEMORY_API/health" 2>/dev/null)"; then
    rm -f "$ZS_MEMORY_FAIL_STATE"
    case "$body" in
      *degraded*) event zs-memory degraded no-restart ;;
      *)          event zs-memory healthy none ;;
    esac
    return
  fi

  local n
  n=$(( $(cat "$ZS_MEMORY_FAIL_STATE" 2>/dev/null || echo 0) + 1 ))
  mkdir -p "$(dirname "$ZS_MEMORY_FAIL_STATE")" 2>/dev/null
  printf '%s' "$n" > "$ZS_MEMORY_FAIL_STATE"
  if [ "$n" -lt "$ZS_MEMORY_FAIL_THRESHOLD" ]; then
    event zs-memory unreachable "no-restart-yet-$n-of-$ZS_MEMORY_FAIL_THRESHOLD"
    return
  fi

  heal zs-memory-kickstart bounded 20 launchctl kickstart -k "gui/$(id -u)/com.zs.memory"
  sleep 3
  if http_ok "$MEMORY_API/health"; then
    rm -f "$ZS_MEMORY_FAIL_STATE"; event zs-memory healthy restarted
  else event zs-memory down heal-failed; fi
}

check_ollama() {
  if http_ok "$OLLAMA_API/api/tags"; then event ollama healthy none; return; fi
  # Ollama runs as the APP cask via the com.zs.ollama LaunchAgent (the brew FORMULA ships
  # no llama-server runner). Heal by kicking that agent, NOT `brew services start ollama`.
  heal ollama-start bounded 20 launchctl kickstart -k "gui/$(id -u)/com.zs.ollama"
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

# No `| grep -q`: pipefail would turn grep's early exit into 141, and here a
# nonzero pipeline reads as "not dataless" — i.e. an evicted placeholder could be
# accepted as a valid backup. Same reason every other pipeline here was removed.
is_dataless() { case "$(ls -lO "$1" 2>/dev/null)" in *dataless*) return 0 ;; *) return 1 ;; esac; }

check_mirror_freshness() { # same contract as check_backup_freshness, for the off-box mirror
  # Only count mirror files that pass a quick integrity stat — size > 0 and not
  # a dataless File Provider placeholder — so a corrupt/evicted newest file
  # can't mask older valid mirrors as "fresh".
  local newest="" f
  while IFS= read -r f; do
    [ -s "$f" ] || continue
    is_dataless "$f" && continue
    newest="$f"
    break
  done < <(ls -t "$MIRROR_BACKUP_DIR"/zs-memory-backup-*.db.gz 2>/dev/null)
  # TCC blocks dir enumeration of Mobile Documents for launchd-context bash
  # (direct-path stat still works) — fall back to the basename stamped by
  # backup-zs-memory.sh after its last verified mirror.
  if [ -z "$newest" ] && [ -r "$BACKUP_DIR/.mirror-stamp" ]; then
    f="$MIRROR_BACKUP_DIR/$(head -1 "$BACKUP_DIR/.mirror-stamp")"
    if [ -s "$f" ] && ! is_dataless "$f"; then newest="$f"; fi
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
  info="$(bounded 10 launchctl list "$DREAM_LABEL" 2>/dev/null)" || { event dream absent none; return; }
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
      --report-only) REPORT_ONLY=1 ;;
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
  # Only record the alert as delivered if it actually was. The old code ignored
  # notify_telegram's result, stamped the dedup file anyway and logged "alerted" —
  # so one failed send (no token, no network) suppressed every retry for 24h while
  # the log claimed the alert had gone out. On failure: no stamp, so the next sweep
  # tries again, and the log says plainly that nothing was delivered.
  if notify_telegram "$message"; then
    if [ "$DRY_RUN" -eq 0 ] && ! touch "$stamp" 2>/dev/null; then
      # Delivered but the 24h dedup stamp did not persist: without this the same
      # alert re-sends every sweep and reads as a storm rather than a stuck job.
      log_event "sched:$job" failing "alerted (dedup stamp FAILED to persist — expect repeats)"
    else
      log_event "sched:$job" failing alerted
    fi
  else
    ALERT_DELIVERY_FAILED=1
    log_event "sched:$job" failing alert-delivery-failed
  fi
}

check_launchd_jobs() {
  local rows
  # Failing OPEN here would silently retire scheduled-job monitoring: no rows to
  # scan means no job is ever flagged, and the sweep would still report healthy.
  # Surface it as its own unhealthy component instead of returning quietly.
  rows="$(bounded 10 launchctl list 2>/dev/null)" || {
    event launchd-scan unreachable "launchctl list failed — scheduled-job monitoring is blind"
    return
  }
  while IFS=$'\t' read -r label status; do
    [ -n "$label" ] || continue
    flag_scheduled_failure launchd "$label" "exit=$status"
  done < <(printf '%s\n' "$rows" | awk -v self="$WATCHDOG_LABEL" '
    $3 ~ /^(com\.zs\.|com\.zerosum|ai\.hermes\.)/ &&
    $3 != self &&
    $1 == "-" &&
    $2 ~ /^[0-9]+$/ &&
    ($2 + 0) > 0 &&
    ($2 + 0) != 143 {
      print $3 "\t" $2
    }
  ')
  # $1 == "-": a KeepAlive daemon that is RUNNING still reports its last
  # (possibly nonzero) exit status in column 2 forever — e.g. a healthy
  # gateway that once lost the network. Only flag jobs that are down.
  #
  # $3 != self (2026-08-01): never flag THIS watchdog on its own exit status.
  # main() exits 1 whenever a sweep FINDS an unhealthy component, so a nonzero
  # LastExitStatus on com.zs.watchdog means "the sweep ran and found problems",
  # NOT "the sweep failed to run" — and those problems were already reported by
  # the very same sweep. Under launchd the `$1 == "-"` guard hid this (the job
  # holds a PID while it runs), but any out-of-band run — a manual invocation,
  # a debugging session — saw the idle job's stale status and raised a Telegram
  # alert naming the watchdog itself. That is a false positive with no signal in
  # it: a watchdog cannot witness its own hard failure (a syntax error or a
  # missing interpreter means nothing runs to do the reporting), so self-scanning
  # can only ever produce noise. Genuine watchdog death is caught from OUTSIDE,
  # by two independent means:
  #   1. $WATCHDOG_HEARTBEAT staleness — read by ~/bin/system-healthcheck and by
  #      the session-start pre-flight. This is the one that catches a sweep which
  #      never ran or hung, a state no exit status can ever report.
  #   2. launchd's LastExitStatus — read weekly by zs-os-doctor. Only meaningful
  #      now that --report-only stops "found problems" from setting it (see main).
}

check_hermes_cron_jobs() {
  local output rc first_line
  # Bounded: a hung hermes gateway must not strand the whole sweep.
  output="$(bounded 15 hermes cron list 2>&1)"; rc=$?
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
  # An alert that never went out is a monitoring failure, not just a log line —
  # scheduled-job failures would otherwise be discovered by nobody. Raised once
  # per sweep rather than once per job, so a broken bot token is not a storm.
  if [ "$ALERT_DELIVERY_FAILED" -eq 1 ]; then
    # Same once-a-day pattern as check_dream. A permanently absent bot token plus
    # any permanently failing job would otherwise post a desktop notification
    # every 5 minutes forever — true, but it would train the alarm to be ignored.
    local astamp="$DREAM_ALERT_STAMP_DIR/zs-alerting-alerted-$(date -u +%F)"
    if [ -e "$astamp" ]; then
      log_event alerting degraded "telegram delivery failing (already alerted today)"
    else
      : > "$astamp" 2>/dev/null || true
      event alerting degraded "telegram delivery failed — scheduled-job alerts are not reaching anyone"
    fi
  fi
}

main() {
  parse_args "$@"
  if [ "$SCHED_ONLY" -eq 1 ]; then
    check_scheduled_jobs
    # Same rule as the full sweep: being unable to RECORD is an execution failure.
    [ "$LOG_WRITE_FAILED" -eq 1 ] && exit 1
    return
  fi

  if [ "$WATCHDOG_CHECK_CONTAINER_RUNTIME" -eq 1 ]; then
    if command -v orb >/dev/null 2>&1; then check_orbstack; else event orbstack absent none; fi
    if command -v docker >/dev/null 2>&1; then check_docker_engine; fi
  fi
  check_zs_memory
  check_ollama
  check_backup_freshness
  check_mirror_freshness
  check_dream
  check_liveness_monitor
  check_scheduled_jobs

  # Exercise the telemetry sampler if present. Do NOT silence its stderr — a broken
  # sampler should leave evidence in launchd's captured stderr.
  if [ -x "$SCRIPT_DIR/telemetry.sh" ]; then
    bounded 30 "$SCRIPT_DIR/telemetry.sh" || printf 'telemetry sampler failed (rc=%d)\n' "$?" >&2
  fi

  # Reaching here means the sweep COMPLETED — regardless of what it found. Stamp
  # the heartbeat before the verdict below, so "everything is unhealthy" and "the
  # watchdog is dead" stay distinguishable from the outside.
  #
  # mktemp + rename, not a fixed ".tmp": the rename is atomic because both paths
  # share a filesystem, but a fixed name lets two concurrent sweeps (launchd plus
  # a manual run) share and truncate one inode. A unique temp makes them disjoint.
  #
  # A write failure here is NOT swallowed. If Logs is unwritable — disk full, bad
  # permissions, TCC — the sweep can persist neither its JSONL events nor this
  # stamp, so every downstream signal reads "fine": no error events, and a
  # staleness reader still sees the previous stamp. Being unable to record IS an
  # execution failure, so it must set a nonzero exit even under --report-only,
  # which is the one channel launchd and zs-os-doctor can still see.
  local hb_tmp hb_failed=0
  hb_tmp="$(mktemp "$WATCHDOG_HEARTBEAT.XXXXXX" 2>/dev/null)" || hb_tmp=""
  if [ -n "$hb_tmp" ] \
     && date -u +%Y-%m-%dT%H:%M:%SZ > "$hb_tmp" 2>/dev/null \
     && mv -f "$hb_tmp" "$WATCHDOG_HEARTBEAT" 2>/dev/null; then
    :
  else
    [ -n "$hb_tmp" ] && rm -f "$hb_tmp" 2>/dev/null
    hb_failed=1
    printf '\033[1;31m✗ watchdog could not write its heartbeat (%s) — health monitoring is not being recorded\033[0m\n' \
      "$WATCHDOG_HEARTBEAT" >&2
  fi

  if [ "${#unhealthy[@]}" -gt 0 ]; then
    local msg="memory stack unhealthy: ${unhealthy[*]}"
    bounded 10 osascript -e "display notification \"${msg//\"/\'}\" with title \"zs-watchdog\"" >/dev/null 2>&1 || true
    printf '\033[1;31m✗ %s\033[0m\n' "$msg" >&2
    # --report-only (how launchd invokes this) exits 0 here on purpose. A sweep
    # that ran correctly and FOUND problems is not a failed sweep, but launchd
    # records only an exit status, and zs-os-doctor reads that status back as
    # "the job crashed" — which is where the standing false P2 finding
    # "'com.zs.watchdog' last exit status 1" came from. Findings are reported
    # through the JSONL and the desktop alarm above, which is the actual alerting
    # path; keeping the exit code for them made LastExitStatus useless as a
    # signal. Interactive and CI callers get the old contract unchanged.
    # hb_failed / LOG_WRITE_FAILED are the exception: being unable to RECORD is an
    # execution failure, not a finding, so it must show up in the exit status even
    # under --report-only. Otherwise an unwritable Logs dir looks perfectly healthy.
    { [ "$hb_failed" -eq 1 ] || [ "$LOG_WRITE_FAILED" -eq 1 ]; } && exit 1
    [ "$REPORT_ONLY" -eq 1 ] || exit 1
    return
  fi
  if [ "$hb_failed" -eq 1 ] || [ "$LOG_WRITE_FAILED" -eq 1 ]; then
    printf '\033[1;31m✗ watchdog could not record its results — monitoring output is being lost\033[0m\n' >&2
    exit 1
  fi
  printf '\033[1;32m✓ memory stack healthy\033[0m\n' >&2
}
# Sourceable for tests (tests/watchdog-zs-memory-tests.sh calls the check
# functions directly); still runs normally when executed by launchd.
if [ "${BASH_SOURCE[0]}" = "${0}" ]; then main "$@"; fi
