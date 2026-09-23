#!/bin/bash
# Tests for watchdog-liveness.sh — the monitor that watches the watchdog.
# launchctl and osascript are stubbed on PATH, so no job is ever kickstarted and
# no notification is ever posted.
set -u

SCRIPT_DIR=$(CDPATH= cd "$(dirname "$0")" && pwd)
LV="$SCRIPT_DIR/../watchdog-liveness.sh"
TMP_DIR="${TMPDIR:-/tmp}/watchdog-liveness-tests.$$.$RANDOM"
umask 077
mkdir -p "$TMP_DIR/bin" || { echo "FAIL setup/temp-dir"; exit 1; }

fails=0
pass() { printf 'PASS %s\n' "$1"; }
fail() { printf 'FAIL %s — %s\n' "$1" "$2"; fails=$((fails + 1)); }

cat > "$TMP_DIR/bin/launchctl" <<'STUB'
#!/bin/sh
printf '%s\n' "$*" >> "$LAUNCHCTL_CALLS"
exit 0
STUB
cat > "$TMP_DIR/bin/osascript" <<'STUB'
#!/bin/sh
exit 0
STUB
# Lets a case pretend the machine woke N seconds ago.
cat > "$TMP_DIR/bin/sysctl" <<'STUB'
#!/bin/sh
if [ "${2:-}" = kern.waketime ] && [ -n "${FAKE_WOKE:-}" ]; then
  echo "{ sec = $FAKE_WOKE, usec = 0 } faked"; exit 0
fi
exec /usr/sbin/sysctl "$@"
STUB
chmod +x "$TMP_DIR/bin"/*
export PATH="$TMP_DIR/bin:$PATH"

HB="$TMP_DIR/heartbeat"
# EVERY path the script writes to must be redirected into TMP_DIR. Omitting
# LIVENESS_HEARTBEAT and HEAL_STATE let an earlier version of this suite write to
# the REAL files: it refreshed the live liveness heartbeat (masking a genuinely
# dead job) and drove the live heal counter past HEAL_ATTEMPT_MAX, so the next
# real staleness would have given up instead of healing. A test must never be
# able to do that; assert the guard rather than trusting it.
run() {
  : > "$TMP_DIR/launchctl.calls"; : > "$TMP_DIR/events.jsonl"
  env LAUNCHCTL_CALLS="$TMP_DIR/launchctl.calls" \
      WATCHDOG_HEARTBEAT="$HB" WATCHDOG_LOG="$TMP_DIR/events.jsonl" \
      LIVENESS_HEARTBEAT="$TMP_DIR/liveness-hb" \
      HEAL_STATE="$TMP_DIR/heal-state" \
      ${FAKE_WOKE:+FAKE_WOKE="$FAKE_WOKE"} \
      bash "$LV" >/dev/null 2>&1
}

# Snapshot the real files so the suite can PROVE it left them alone.
REAL_LV_HB="$HOME/Library/Logs/zs-watchdog-liveness-heartbeat"
REAL_HEAL="$HOME/Library/Logs/zs-watchdog-liveness-heals"
real_sig() { printf '%s|%s' \
  "$(stat -f %m "$REAL_LV_HB" 2>/dev/null || echo none)" \
  "$(cat "$REAL_HEAL" 2>/dev/null || echo none)"; }
REAL_BEFORE="$(real_sig)"
kicked() { grep -q kickstart "$TMP_DIR/launchctl.calls" 2>/dev/null; }
status_is() { grep -q "\"status\":\"$1\"" "$TMP_DIR/events.jsonl" 2>/dev/null; }

# Machine awake long enough to judge, for every case except the wake test.
FAKE_WOKE=$(( $(date +%s) - 99999 ))

# ── 1: fresh heartbeat → healthy, no heal ────────────────────────────────────
date -u +%Y-%m-%dT%H:%M:%SZ > "$HB"
run
{ status_is healthy && ! kicked; } && pass "fresh/healthy-no-heal" \
  || fail "fresh/healthy-no-heal" "expected healthy with no kickstart"

# ── 2: stale heartbeat → down, and the watchdog is kickstarted ───────────────
# This is the whole point of the job: a hung sweep gets SIGTERMed and restarted
# without waiting for a human or the weekly doctor.
touch -t 202601010000 "$HB"
run
{ status_is down && kicked; } && pass "stale/down-and-healed" \
  || fail "stale/down-and-healed" "expected down + kickstart"

# ── 3: missing heartbeat → down (never silently healthy) ─────────────────────
rm -f "$HB"
run
status_is down && pass "missing/down" || fail "missing/down" "expected down"

# ── 4: FUTURE-dated stamp must not read as fresh ─────────────────────────────
# Clock rollback or a bad mtime would otherwise pass the age check forever.
touch -t 202701010000 "$HB"
run
status_is down && pass "future-dated/down" \
  || fail "future-dated/down" "a future stamp was treated as fresh"

# ── 5: just-woken machine is forgiven even with a stale stamp ────────────────
# launchd skips StartInterval timers during sleep, so every lid-open would
# otherwise raise a false "monitoring is down" alarm.
touch -t 202601010000 "$HB"
FAKE_WOKE=$(( $(date +%s) - 30 ))
run
{ status_is healthy && ! kicked; } && pass "just-woke/forgiven" \
  || fail "just-woke/forgiven" "false alarm right after wake"
FAKE_WOKE=$(( $(date +%s) - 99999 ))

# ── 6: always exits 0 ────────────────────────────────────────────────────────
# A nonzero status here would recreate the found-problems/failed-to-run
# conflation that this whole change removed.
rm -f "$HB"; run; rc=$?
[ "$rc" -eq 0 ] && pass "always/exit-zero" || fail "always/exit-zero" "exited $rc"

# ── 7: an unwritable log is an EXECUTION failure, so exit nonzero ────────────
# Findings exit 0 by design, but if this job cannot record anything its silence
# looks exactly like health — and it is the last witness in the chain.
RO="$TMP_DIR/ro"; mkdir -p "$RO"; chmod 500 "$RO"
date -u +%Y-%m-%dT%H:%M:%SZ > "$HB"
env LAUNCHCTL_CALLS="$TMP_DIR/launchctl.calls" \
    WATCHDOG_HEARTBEAT="$HB" WATCHDOG_LOG="$RO/events.jsonl" \
    LIVENESS_HEARTBEAT="$TMP_DIR/liveness-hb" HEAL_STATE="$TMP_DIR/heal-state" \
    FAKE_WOKE="$FAKE_WOKE" bash "$LV" >/dev/null 2>&1
rc=$?; chmod 700 "$RO"
[ "$rc" -ne 0 ] && pass "unwritable-log/nonzero" \
  || fail "unwritable-log/nonzero" "exited 0 while unable to record anything"

# ── the other half: watchdog.sh must notice when THIS job stops ──────────────
# Without it the pair is not mutual — the liveness job could be unloaded or hung
# and nothing would say so, which is the gap it was added to close. The launchd
# scan cannot cover it: an unloaded job has no row, and a hung one holds a PID
# that the scan deliberately skips.
WD="$SCRIPT_DIR/../watchdog.sh"
LVHB="$TMP_DIR/liveness-heartbeat"
run_wd_check() {
  (
    export WATCHDOG_LOG="$TMP_DIR/wd-events.jsonl"
    export LIVENESS_HEARTBEAT="$LVHB"
    # Force past the post-wake grace so the staleness branch is the one under test.
    export FAKE_WOKE=$(( $(date +%s) - 99999 ))
    # shellcheck disable=SC1090
    source "$WD" >/dev/null 2>&1
    check_liveness_monitor
  )
}
wd_says() { grep -q "\"component\":\"liveness-monitor\",\"status\":\"$1\"" "$TMP_DIR/wd-events.jsonl" 2>/dev/null; }

: > "$TMP_DIR/wd-events.jsonl"; date -u +%Y-%m-%dT%H:%M:%SZ > "$LVHB"; run_wd_check
wd_says healthy && pass "watchdog-sees-liveness/fresh-healthy" \
  || fail "watchdog-sees-liveness/fresh-healthy" "expected healthy"

: > "$TMP_DIR/wd-events.jsonl"; touch -t 202601010000 "$LVHB"; run_wd_check
wd_says down && pass "watchdog-sees-liveness/stale-down" \
  || fail "watchdog-sees-liveness/stale-down" "a stopped liveness job went unnoticed"

: > "$TMP_DIR/wd-events.jsonl"; rm -f "$LVHB"; run_wd_check
wd_says down && pass "watchdog-sees-liveness/missing-down" \
  || fail "watchdog-sees-liveness/missing-down" "an absent liveness job went unnoticed"

# ── the suite must not have touched the live control plane ───────────────────
[ "$(real_sig)" = "$REAL_BEFORE" ] && pass "isolation/live-state-untouched" \
  || fail "isolation/live-state-untouched" \
          "the real liveness heartbeat or heal counter changed during the run"

rm -rf "$TMP_DIR"
[ "$fails" -eq 0 ] && { echo "--- all watchdog liveness tests passed ---"; exit 0; }
echo "--- $fails failed ---"; exit 1
