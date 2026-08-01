#!/bin/bash
# Self-contained tests for watchdog.sh's check_launchd_jobs scan.
# `launchctl` is stubbed on PATH so no real job is ever inspected or touched,
# and DRY_RUN=1 keeps notify_telegram off the network and off the stamp dir.
set -u

SCRIPT_DIR=$(CDPATH= cd "$(dirname "$0")" && pwd)
WD="$SCRIPT_DIR/../watchdog.sh"
TMP_DIR="${TMPDIR:-/tmp}/watchdog-sched-tests.$$.$RANDOM"
umask 077
mkdir -p "$TMP_DIR" || { echo "FAIL setup/temp-dir"; exit 1; }

fails=0
pass() { printf 'PASS %s\n' "$1"; }
fail() { printf 'FAIL %s — %s\n' "$1" "$2"; fails=$((fails + 1)); }

# ── stub launchctl: `list` prints a fixed table, everything else is a no-op ───
# Columns are PID, LastExitStatus, Label — same shape as the real `launchctl list`.
mkdir -p "$TMP_DIR/bin"
cat > "$TMP_DIR/bin/launchctl" <<'STUB'
#!/bin/sh
# STUB_LAUNCHCTL_FAIL makes `list` fail, standing in for a launchctl that cannot
# be queried at all.
if [ "${1:-}" = "list" ] && [ "$#" -eq 1 ]; then
  [ -n "${STUB_LAUNCHCTL_FAIL:-}" ] && exit 1
  printf 'PID\tStatus\tLabel\n'
  printf -- '-\t256\tcom.zs.watchdog\n'    # the watchdog itself, idle, "failed"
  printf -- '-\t256\tcom.zs.brokenjob\n'   # a genuinely failed idle job
  printf -- '1234\t1\tcom.zs.runningjob\n' # running: stale status, not a failure
  printf -- '-\t143\tcom.zs.sigtermjob\n'  # 143 = SIGTERM, deliberately ignored
  printf -- '-\t0\tcom.zs.healthyjob\n'    # clean exit
  printf -- '-\t256\tcom.other.notours\n'  # outside the monitored namespaces
  exit 0
fi
exit 0
STUB
# 256 is what launchd actually reports for `exit 1` — the value measured on the
# live com.zs.watchdog row — so the fixture matches the real numeric branch.
chmod +x "$TMP_DIR/bin/launchctl"
export PATH="$TMP_DIR/bin:$PATH"

# ── harness: run check_launchd_jobs once in a clean subshell ─────────────────
# shellcheck disable=SC1090
run_scan() {
  (
    export WATCHDOG_LOG="$TMP_DIR/events.jsonl"
    export SCHED_STAMP_DIR="$TMP_DIR/stamps"
    export HERMES_ENV="$TMP_DIR/nonexistent.env"
    source "$WD" >/dev/null 2>&1
    DRY_RUN=1                      # notify_telegram prints instead of curling
    check_launchd_jobs >/dev/null 2>&1
  )
}

flagged() { grep -q "\"component\":\"sched:$1\"" "$TMP_DIR/events.jsonl" 2>/dev/null; }

: > "$TMP_DIR/events.jsonl"
run_scan

# ── 1: the watchdog must never flag ITSELF ───────────────────────────────────
# Its exit status is nonzero by design whenever a sweep FINDS an unhealthy
# component, so reading that status back as "this job is failing" is a false
# positive that fires a Telegram alert about the monitor rather than the fault.
if flagged com.zs.watchdog; then
  fail "self/not-flagged" "watchdog flagged its own launchd exit status"
else
  pass "self/not-flagged"
fi

# ── 2: a genuinely failed idle job is still flagged (no over-suppression) ────
if flagged com.zs.brokenjob; then pass "other/flagged"
else fail "other/flagged" "a real failed job was missed"; fi

# ── 3-5: pre-existing guards must survive the change ─────────────────────────
if flagged com.zs.runningjob; then fail "running/ignored" "flagged a RUNNING job"
else pass "running/ignored"; fi

if flagged com.zs.sigtermjob; then fail "sigterm/ignored" "flagged a 143 (SIGTERM) exit"
else pass "sigterm/ignored"; fi

if flagged com.zs.healthyjob; then fail "healthy/ignored" "flagged a clean exit"
else pass "healthy/ignored"; fi

if flagged com.other.notours; then fail "namespace/ignored" "flagged a foreign label"
else pass "namespace/ignored"; fi

# ── 7: an unusable launchctl must FAIL LOUD, not fail open ───────────────────
# Returning quietly on a failed `launchctl list` retires scheduled-job monitoring
# silently: nothing to scan means nothing is ever flagged, and the sweep still
# reports healthy. It has to surface as its own unhealthy component.
: > "$TMP_DIR/events.jsonl"
(
  export WATCHDOG_LOG="$TMP_DIR/events.jsonl"
  export SCHED_STAMP_DIR="$TMP_DIR/stamps"
  export HERMES_ENV="$TMP_DIR/nonexistent.env"
  export STUB_LAUNCHCTL_FAIL=1
  # shellcheck disable=SC1090
  source "$WD" >/dev/null 2>&1
  DRY_RUN=1
  check_launchd_jobs >/dev/null 2>&1
)
if grep -q '"component":"launchd-scan"' "$TMP_DIR/events.jsonl" 2>/dev/null; then
  pass "launchctl-failure/reported"
else
  fail "launchctl-failure/reported" "a failed launchctl list produced no event"
fi

rm -rf "$TMP_DIR"
[ "$fails" -eq 0 ] && { echo "--- all watchdog sched tests passed ---"; exit 0; }
echo "--- $fails failed ---"; exit 1
