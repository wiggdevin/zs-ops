#!/bin/bash
# Tests for watchdog.sh's exit-code contract and the heartbeat it leaves behind.
#
# The distinction under test: "the sweep RAN and found unhealthy components" is
# not the same event as "the sweep FAILED to run", but launchd records both as a
# nonzero LastExitStatus. Under --report-only (how launchd invokes it) only the
# second may be nonzero, which is what makes LastExitStatus a usable signal for
# zs-os-doctor. Interactive callers keep the old nonzero-means-unhealthy contract.
#
# Every check is stubbed on PATH; no real service is probed, restarted or killed.
set -u

SCRIPT_DIR=$(CDPATH= cd "$(dirname "$0")" && pwd)
WD="$SCRIPT_DIR/../watchdog.sh"
TMP_DIR="${TMPDIR:-/tmp}/watchdog-exit-tests.$$.$RANDOM"
umask 077
mkdir -p "$TMP_DIR/bin" || { echo "FAIL setup/temp-dir"; exit 1; }

fails=0
pass() { printf 'PASS %s\n' "$1"; }
fail() { printf 'FAIL %s — %s\n' "$1" "$2"; fails=$((fails + 1)); }

# ── stubs: keep the sweep entirely offline ───────────────────────────────────
cat > "$TMP_DIR/bin/launchctl" <<'STUB'
#!/bin/sh
[ "${1:-}" = "list" ] && { printf 'PID\tStatus\tLabel\n'; exit 0; }
exit 0
STUB
# `orb status` prints "Running" so check_orbstack passes without touching OrbStack.
cat > "$TMP_DIR/bin/orb" <<'STUB'
#!/bin/sh
[ "${1:-}" = "status" ] && { echo Running; exit 0; }
exit 0
STUB
# docker/curl are driven by $STUB_FAIL so a case can force an unhealthy sweep.
cat > "$TMP_DIR/bin/docker" <<'STUB'
#!/bin/sh
[ -n "${STUB_FAIL:-}" ] && exit 1
exit 0
STUB
cat > "$TMP_DIR/bin/osascript" <<'STUB'
#!/bin/sh
exit 0
STUB
cat > "$TMP_DIR/bin/hermes" <<'STUB'
#!/bin/sh
exit 0
STUB
# telemetry.sh shells out to `ollama ps`; stub it so no real daemon is queried.
cat > "$TMP_DIR/bin/ollama" <<'STUB'
#!/bin/sh
exit 0
STUB
chmod +x "$TMP_DIR/bin"/*
export PATH="$TMP_DIR/bin:$PATH"

# watchdog.sh runs telemetry.sh by ABSOLUTE path, so a PATH stub cannot hide it.
# Left alone it appends to the LIVE ~/Library/Logs/zs-telemetry.jsonl and probes
# the real zs-memory and ollama. It honours these env vars, so redirect them
# here — exported once at the top so every subshell below inherits them.
export TELEMETRY_LOG="$TMP_DIR/telemetry.jsonl"
export MEM0_API="http://127.0.0.1:1"

# Snapshot the live monitoring outputs so the suite can PROVE it stayed offline.
LIVE_FILES="$HOME/Library/Logs/zs-telemetry.jsonl $HOME/Library/Logs/zs-watchdog.jsonl $HOME/Library/Logs/zs-watchdog-heartbeat"
live_sig() { for f in $LIVE_FILES; do printf '%s:%s ' "$(basename "$f")" "$(stat -f %m "$f" 2>/dev/null || echo none)"; done; }
LIVE_BEFORE="$(live_sig)"

# Run a full sweep offline. MEMORY_API/OLLAMA_API point at a closed port, so
# those checks fail — which is exactly the "ran fine, found problems" state.
run_sweep() {
  (
    export WATCHDOG_LOG="$TMP_DIR/events.jsonl"
    export WATCHDOG_HEARTBEAT="$TMP_DIR/heartbeat"
    export SCHED_STAMP_DIR="$TMP_DIR/stamps"
    export HERMES_ENV="$TMP_DIR/nonexistent.env"
    export ZS_MEMORY_FAIL_STATE="$TMP_DIR/failcount"
    export MEMORY_API="http://127.0.0.1:1" OLLAMA_API="http://127.0.0.1:1"
    export BACKUP_DIR="$TMP_DIR/backups" MIRROR_BACKUP_DIR="$TMP_DIR/mirror"
    export DREAM_ALERT_STAMP_DIR="$TMP_DIR"
    bash "$WD" "$@" >/dev/null 2>&1
  )
}

rm -f "$TMP_DIR/heartbeat"

# ── 1: interactive default keeps the nonzero-means-unhealthy contract ────────
run_sweep; rc=$?
[ "$rc" -ne 0 ] && pass "interactive/nonzero-when-unhealthy" \
  || fail "interactive/nonzero-when-unhealthy" "expected nonzero, got $rc"

# ── 2: --report-only exits 0 for the SAME unhealthy sweep ────────────────────
# This is what launchd runs, so LastExitStatus stops meaning "found problems".
run_sweep --report-only; rc=$?
[ "$rc" -eq 0 ] && pass "report-only/zero-when-unhealthy" \
  || fail "report-only/zero-when-unhealthy" "expected 0, got $rc"

# ── 3: --report-only still fails loudly on a genuine execution error ─────────
# An unknown flag stands in for "the script could not do its job".
run_sweep --report-only --nonsense-flag; rc=$?
[ "$rc" -ne 0 ] && pass "report-only/nonzero-on-real-failure" \
  || fail "report-only/nonzero-on-real-failure" "expected nonzero, got $rc"

# ── 4: a completed sweep stamps the heartbeat ────────────────────────────────
# The heartbeat is the only evidence that survives a watchdog that never runs,
# so external staleness detection depends on it being written every sweep.
[ -f "$TMP_DIR/heartbeat" ] && pass "heartbeat/written" \
  || fail "heartbeat/written" "no heartbeat file after a sweep"

# ── 5: the heartbeat is written even when components are UNHEALTHY ───────────
# If it were only stamped on a clean sweep, "everything is broken" would look
# identical to "the watchdog is dead" — the exact confusion being removed.
rm -f "$TMP_DIR/heartbeat"
run_sweep --report-only
[ -f "$TMP_DIR/heartbeat" ] && pass "heartbeat/written-when-unhealthy" \
  || fail "heartbeat/written-when-unhealthy" "heartbeat missing after unhealthy sweep"

# ── 6: --sched-only must not stamp the heartbeat ─────────────────────────────
# It is a partial sweep; treating it as a full one would let a hung main sweep
# masquerade as fresh.
rm -f "$TMP_DIR/heartbeat"
run_sweep --sched-only
[ ! -f "$TMP_DIR/heartbeat" ] && pass "heartbeat/not-written-by-sched-only" \
  || fail "heartbeat/not-written-by-sched-only" "partial sweep stamped the heartbeat"

# ── 7: an UNWRITABLE heartbeat must fail loud even under --report-only ───────
# The dangerous case: the Logs dir goes read-only (disk full, permissions, TCC).
# The sweep then persists neither events nor heartbeat, so the JSONL shows nothing
# wrong and the staleness readers see an old stamp — while launchd is told exit 0.
# Monitoring is completely blind and every signal says "fine". A failure to record
# IS an execution failure, so it must set a nonzero status.
RO_DIR="$TMP_DIR/readonly"
mkdir -p "$RO_DIR" && chmod 500 "$RO_DIR"
(
  # BOTH outputs land in the unwritable dir — the realistic case. Pointing only
  # the heartbeat at it would leave the JSONL working and understate the failure.
  export WATCHDOG_LOG="$RO_DIR/events.jsonl"
  export WATCHDOG_HEARTBEAT="$RO_DIR/heartbeat"
  export SCHED_STAMP_DIR="$TMP_DIR/stamps"
  export HERMES_ENV="$TMP_DIR/nonexistent.env"
  export ZS_MEMORY_FAIL_STATE="$TMP_DIR/failcount"
  export MEMORY_API="http://127.0.0.1:1" OLLAMA_API="http://127.0.0.1:1"
  export BACKUP_DIR="$TMP_DIR/backups" MIRROR_BACKUP_DIR="$TMP_DIR/mirror"
  export DREAM_ALERT_STAMP_DIR="$TMP_DIR"
  bash "$WD" --report-only >/dev/null 2>&1
); rc=$?
chmod 700 "$RO_DIR" 2>/dev/null
[ "$rc" -ne 0 ] && pass "report-only/nonzero-when-heartbeat-unwritable" \
  || fail "report-only/nonzero-when-heartbeat-unwritable" "exit 0 while unable to record anything"

# ── 7b: an unwritable JSONL alone must also fail loud ────────────────────────
# Separate path from the heartbeat: here the stamp writes fine, so every staleness
# reader says "healthy" while the structured event log silently goes nowhere. The
# flag has to survive being set inside the `while read` / process-substitution
# loops in check_launchd_jobs and reach main().
RO2="$TMP_DIR/readonly2"
mkdir -p "$RO2" && chmod 500 "$RO2"
(
  export WATCHDOG_LOG="$RO2/events.jsonl"
  export WATCHDOG_HEARTBEAT="$TMP_DIR/hb-writable"
  export SCHED_STAMP_DIR="$TMP_DIR/stamps"
  export HERMES_ENV="$TMP_DIR/nonexistent.env"
  export ZS_MEMORY_FAIL_STATE="$TMP_DIR/failcount"
  export MEMORY_API="http://127.0.0.1:1" OLLAMA_API="http://127.0.0.1:1"
  export BACKUP_DIR="$TMP_DIR/backups" MIRROR_BACKUP_DIR="$TMP_DIR/mirror"
  export DREAM_ALERT_STAMP_DIR="$TMP_DIR"
  bash "$WD" --report-only >/dev/null 2>&1
); rc=$?
chmod 700 "$RO2" 2>/dev/null
{ [ "$rc" -ne 0 ] && [ -f "$TMP_DIR/hb-writable" ]; } \
  && pass "report-only/nonzero-when-log-unwritable" \
  || fail "report-only/nonzero-when-log-unwritable" "rc=$rc; events were lost but the sweep reported success"

# ── 8: bounded() must bound even with no `timeout` binary ────────────────────
# Degrading to an unbounded call reintroduces the exact catastrophic hang the
# helper exists to prevent — one missing Homebrew package should not silently
# disarm it.
# /usr/bin:/bin genuinely has no `timeout` on macOS (it ships via Homebrew
# coreutils), so `command -v timeout` really fails here — a stub would not test
# the fallback at all, it would just be found and executed.
start=$(date +%s)
(
  export PATH="/usr/bin:/bin"
  # shellcheck disable=SC1090
  source "$WD" >/dev/null 2>&1
  bounded 2 sleep 30
) >/dev/null 2>&1; rc=$?
elapsed=$(( $(date +%s) - start ))
{ [ "$elapsed" -lt 10 ] && [ "$rc" -eq 124 ]; } && pass "bounded/enforced-without-timeout-binary" \
  || fail "bounded/enforced-without-timeout-binary" "took ${elapsed}s rc=$rc; want <10s and rc=124"

# ── 9: the bound must survive a GRANDCHILD holding the capture pipe ──────────
# Killing only the direct child leaves a grandchild owning the stdout pipe, so a
# command substitution blocks until the grandchild exits — the bound is reported
# but not actually enforced. Both paths are exercised: the active Homebrew
# `timeout` (which group-kills) and the builtin fallback.
for mode in with-timeout without-timeout; do
  start=$(date +%s)
  (
    [ "$mode" = without-timeout ] && export PATH="/usr/bin:/bin"
    # shellcheck disable=SC1090
    source "$WD" >/dev/null 2>&1
    out="$(bounded 2 sh -c 'sleep 25 & wait')"
  ) >/dev/null 2>&1
  elapsed=$(( $(date +%s) - start ))
  [ "$elapsed" -lt 10 ] && pass "bounded/grandchild-$mode" \
    || fail "bounded/grandchild-$mode" "took ${elapsed}s; grandchild outlived the 2s bound"
done

# ── 10: a TERM-ignoring command must still die ───────────────────────────────
# Plain `timeout` only sends TERM; a command that traps it runs to completion,
# so the bound is advisory rather than real. -k escalates to KILL.
start=$(date +%s)
(
  # shellcheck disable=SC1090
  source "$WD" >/dev/null 2>&1
  bounded 2 sh -c 'trap "" TERM; sleep 25'
) >/dev/null 2>&1
elapsed=$(( $(date +%s) - start ))
[ "$elapsed" -lt 15 ] && pass "bounded/term-ignoring-command-killed" \
  || fail "bounded/term-ignoring-command-killed" "took ${elapsed}s; TERM-ignoring command outlived the bound"

# ── 11: bounded_matches — the OrbStack health gate ───────────────────────────
# check_orbstack lives or dies on this. If it wrongly returned non-zero for a
# RUNNING OrbStack, every sweep would fire `orb start` at a healthy VM every five
# minutes, and nothing else in the suite would notice. It had no coverage at all
# until this case; the timeout arm matters just as much, because collapsing
# "timed out" into "did not match" is what makes a wedged control channel look
# like a stopped VM.
match_case() { # match_case <label> <expect-rc> <cmd...>
  local label="$1" want="$2"; shift 2
  local got
  ( # shellcheck disable=SC1090
    source "$WD" >/dev/null 2>&1
    bounded_matches 2 "*[Rr]unning*" "$@" ) >/dev/null 2>&1
  got=$?
  [ "$got" -eq "$want" ] && pass "bounded_matches/$label" \
    || fail "bounded_matches/$label" "rc=$got, want $want"
}
match_case "matching-output-is-0"   0 printf 'Running\n'
match_case "case-insensitive"       0 printf 'running\n'
match_case "extra-text-still-0"     0 printf 'OrbStack is Running now\n'
match_case "non-matching-is-1"      1 printf 'Stopped\n'
match_case "empty-output-is-1"      1 true
match_case "failing-command-is-1"   1 false
# A hang must be 124, NOT 1 — check_orbstack branches on that to avoid restarting
# a VM whose control channel is merely wedged.
match_case "timeout-is-124"       124 sleep 20
# 125 = "could not probe at all". It MUST stay distinct from 1: check_orbstack and
# zs-os both mutate on 1 ("really stopped") and must do nothing on 125, otherwise
# an unwritable temp dir restarts a healthy VM every five minutes.
probe_unknown=$(
  TMPDIR=/definitely/nonexistent/zzz bash -c '
    . '"$HOME"'/.config/zs-ops/lib/bounded.sh
    # also deny the /tmp fallback by pointing HOME-free mktemp at a bad path
    mktemp() { return 1; }
    bounded_matches 2 "*[Rr]unning*" printf "Running\n"
    echo $?' 2>/dev/null | tail -1)
[ "$probe_unknown" = "125" ] && pass "bounded_matches/unprobeable-is-125" \
  || fail "bounded_matches/unprobeable-is-125" "got $probe_unknown, want 125"

# ── the suite must never have touched live monitoring state ──────────────────
# It runs the REAL watchdog, which invokes telemetry.sh by absolute path — a PATH
# stub cannot hide that, so only the env redirects above keep it offline. Assert
# it rather than trusting it.
[ "$(live_sig)" = "$LIVE_BEFORE" ] && pass "isolation/live-state-untouched" \
  || fail "isolation/live-state-untouched" "a live log changed: $LIVE_BEFORE -> $(live_sig)"

rm -rf "$TMP_DIR"
[ "$fails" -eq 0 ] && { echo "--- all watchdog exit/heartbeat tests passed ---"; exit 0; }
echo "--- $fails failed ---"; exit 1
