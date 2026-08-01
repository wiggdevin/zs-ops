#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# watchdog-liveness.sh — watches the watchdog. Run by launchd every 15 min.
#
# com.zs.watchdog cannot report its own death: a sweep that never starts emits no
# event and sets no exit status, and one that HANGS never reaches either. The only
# evidence is $WATCHDOG_HEARTBEAT going stale. Until this job existed that file was
# read solely by ~/bin/system-healthcheck (manual) and the Claude session-start
# hook (event-driven) — so a watchdog that died on a Tuesday with no session opened
# stayed dead until someone looked, or until zs-os-doctor's WEEKLY pass.
#
# "But what watches THIS?" — the watchdog does, mutually: this job stamps its own
# $LIVENESS_HEARTBEAT every run and check_liveness_monitor() in watchdog.sh alerts
# when that goes stale. Each is the other's witness, so neither can disappear in
# silence; launchd remains the root of trust for both. That is also why this stays
# small and dependency-free — it shells out to nothing but stat/date/sysctl/launchctl.
#
# Exit code contract, same split as watchdog.sh: FINDINGS exit 0 (a dead watchdog
# is reported through the JSONL and a desktop alarm, not through this job's exit
# status — encoding it there would recreate the exact "found problems" vs "failed
# to run" conflation this whole change removed). This script's OWN execution
# failures — a missing shared lib, an unwritable log — exit nonzero, because
# otherwise its silence is indistinguishable from health.
# ─────────────────────────────────────────────────────────────────────────────
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WATCHDOG_HEARTBEAT="${WATCHDOG_HEARTBEAT:-$HOME/Library/Logs/zs-watchdog-heartbeat}"
LIVENESS_HEARTBEAT="${LIVENESS_HEARTBEAT:-$HOME/Library/Logs/zs-watchdog-liveness-heartbeat}"
WATCHDOG_LOG="${WATCHDOG_LOG:-$HOME/Library/Logs/zs-watchdog.jsonl}"
STALE_SECONDS="${STALE_SECONDS:-900}"
WATCHDOG_JOB="${WATCHDOG_JOB:-com.zs.watchdog}"
LIVENESS_HEAL="${LIVENESS_HEAL:-1}"
# After this many consecutive failed heals, stop kickstarting and only alert. A
# watchdog broken by a syntax error is never fixed by restarting it, and retrying
# every 15 minutes forever just accumulates noise (and processes).
HEAL_ATTEMPT_MAX="${HEAL_ATTEMPT_MAX:-3}"
HEAL_STATE="${HEAL_STATE:-$HOME/Library/Logs/zs-watchdog-liveness-heals}"

# Shared wall-clock bound. `osascript` blocks indefinitely against a wedged
# WindowServer and `launchctl kickstart` against a stuck job — either would strand
# this run and silently cancel the 15-minute cadence, which is the one thing the
# job exists to guarantee.
if ! . "$SCRIPT_DIR/lib/bounded.sh" 2>/dev/null; then
  # Exit NONZERO. The always-exit-0 rule below covers FINDINGS ("the watchdog is
  # down"), not this script's own execution failures. A missing shared lib breaks
  # both monitors at once, so it has to reach launchd — and from there the
  # watchdog's launchd scan and zs-os-doctor.
  printf 'watchdog-liveness: FATAL — cannot source %s/lib/bounded.sh\n' "$SCRIPT_DIR" >&2
  exit 1
fi

LOG_WRITE_FAILED=0
log_event() { # mirror watchdog.sh's JSONL shape so both land in one timeline
  local ts; ts="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  { printf '{"ts":"%s","component":"watchdog-liveness","status":"%s","action":"%s"}\n' \
      "$ts" "$1" "$2" >> "$WATCHDOG_LOG"; } 2>/dev/null || LOG_WRITE_FAILED=1
}

# Exit 0 for findings, nonzero when THIS script could not do its job. Being unable
# to record is the latter: silence would otherwise look identical to health.
finish() { [ "$LOG_WRITE_FAILED" -eq 1 ] && exit 1; exit 0; }

# Stamped every run so watchdog.sh can tell that THIS job is still alive.
# Failure is NOT silent: if the stamp never lands, watchdog.sh will correctly
# start reporting "nothing is watching the watchdog", and this job must agree by
# exiting nonzero rather than returning success and looking fine.
stamp_self() {
  local tmp
  tmp="$(mktemp "$LIVENESS_HEARTBEAT.XXXXXX" 2>/dev/null)" || { LOG_WRITE_FAILED=1; return 0; }
  if date -u +%Y-%m-%dT%H:%M:%SZ > "$tmp" 2>/dev/null \
     && mv -f "$tmp" "$LIVENESS_HEARTBEAT" 2>/dev/null; then
    return 0
  fi
  rm -f "$tmp" 2>/dev/null
  LOG_WRITE_FAILED=1
}
stamp_self

now=$(date +%s)

# launchd does not fire StartInterval timers while the machine sleeps, so a stamp
# is legitimately old for a moment after wake. Judge only once awake past the
# window. The pattern must stay ^-anchored: unanchored it matches `usec`.
woke=$(sysctl -n kern.waketime 2>/dev/null | sed -n 's/^{ sec = \([0-9]*\).*/\1/p')
if [ -n "$woke" ]; then
  awake=$(( now - woke ))
  if [ "$awake" -ge 0 ] && [ "$awake" -lt "$STALE_SECONDS" ]; then
    log_event healthy "post-wake grace (${awake}s awake)"
    finish
  fi
fi

if [ ! -r "$WATCHDOG_HEARTBEAT" ]; then
  age=-1
else
  # A stamp dated in the FUTURE (clock rollback, NTP correction) is not fresh.
  age=$(( now - $(stat -f %m "$WATCHDOG_HEARTBEAT" 2>/dev/null || echo 0) ))
  [ "$age" -lt 0 ] && age=$(( STALE_SECONDS + 1 ))
fi

if [ "$age" -ge 0 ] && [ "$age" -lt "$STALE_SECONDS" ]; then
  rm -f "$HEAL_STATE" 2>/dev/null            # recovered — reset the backoff
  log_event healthy "heartbeat ${age}s old"
  finish
fi

detail="heartbeat $( [ "$age" -lt 0 ] && echo missing || echo "${age}s old" )"
msg="zs-watchdog is NOT sweeping — $detail. Health monitoring is down."
bounded 10 osascript -e "display notification \"$msg\" with title \"zs-watchdog LIVENESS\"" >/dev/null 2>&1 || true
printf '\033[1;31m✗ %s\033[0m\n' "$msg" >&2

if [ "$LIVENESS_HEAL" != "1" ]; then
  log_event down "$detail — heal disabled"
  finish
fi

attempts=$(( $(cat "$HEAL_STATE" 2>/dev/null || echo 0) + 1 ))
if ! printf '%s' "$attempts" > "$HEAL_STATE" 2>/dev/null; then
  # FAIL CLOSED. Without persistence every run recomputes attempts=1, so the
  # give-up cap can never trip and a permanently broken watchdog would be
  # kickstarted every 15 minutes forever. Logging that the safety cap is broken
  # while still healing is not enforcing it — so do not heal at all.
  log_event down "$detail — heal counter NOT persistable ($HEAL_STATE); NOT healing, cap cannot be enforced"
  LOG_WRITE_FAILED=1
  finish
fi
if [ "$attempts" -gt "$HEAL_ATTEMPT_MAX" ]; then
  log_event down "$detail — heal GIVEN UP after $HEAL_ATTEMPT_MAX attempts; needs a human"
  finish
fi

# `kickstart -k` SIGTERMs a hung sweep and starts a fresh one, which is the
# failure this is most likely to see.
if bounded 30 launchctl kickstart -k "gui/$(id -u)/$WATCHDOG_JOB" >/dev/null 2>&1; then
  log_event down "$detail — kickstarted $WATCHDOG_JOB (attempt $attempts/$HEAL_ATTEMPT_MAX)"
else
  log_event down "$detail — kickstart FAILED (attempt $attempts/$HEAL_ATTEMPT_MAX)"
fi
finish
