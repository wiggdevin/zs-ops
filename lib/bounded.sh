# shellcheck shell=bash
# ─────────────────────────────────────────────────────────────────────────────
# bounded.sh — ONE wall-clock bound for every ops script. Source, don't execute.
#
# Sourced by: watchdog.sh, watchdog-liveness.sh, ~/bin/system-healthcheck, ~/bin/zs-os.
# It lives in one file because it was briefly copy-pasted into three, and the copies
# immediately drifted — two got hardened and one kept a fail-open fallback that
# silently disarmed every bound in it. One definition, one place to fix.
#
# Why any of this: `docker info`, `orb`, `launchctl` and `osascript` have no native
# timeout and block forever against a wedged socket or a hung WindowServer. In a
# watchdog that is fatal — a hung sweep never reaches its heartbeat, so the thing
# meant to detect failure becomes the failure, silently.
#
# Contract: `bounded <seconds> <cmd...>` returns the command's exit status, or 124
# if it was killed for exceeding the bound (matching coreutils `timeout`).
#
# Caveat worth knowing: 124 is indistinguishable from a command that genuinely
# exits 124. Callers that care should confirm with a follow-up probe rather than
# treating 124 as proof of a hang.
# ─────────────────────────────────────────────────────────────────────────────

bounded() { # bounded <seconds> <cmd...>
  local secs="$1"; shift

  # `timeout` is Homebrew coreutils, NOT macOS. -k sends KILL if the command
  # ignores the initial TERM (without it, a TERM-ignoring child outlives its
  # bound entirely — measured at 5.22s against a nominal 1s). coreutils runs the
  # command in its own process group and signals the GROUP, so grandchildren die
  # with it; that matters because a surviving grandchild holds the capture pipe
  # open and blocks command substitution long past the timeout.
  if command -v timeout >/dev/null 2>&1; then
    timeout -k 5 "$secs" "$@"
    return $?
  fi

  # Fallback. It must be a REAL bound: degrading to an unbounded call would let
  # one uninstalled package disarm every caller.
  local pid pgid waited=0 limit=$(( secs * 10 ))
  # Job control gives the child its own process group so the whole tree can be
  # killed, matching coreutils. Restored immediately — it changes signal and job
  # handling for the rest of the shell otherwise.
  set -m 2>/dev/null
  "$@" & pid=$!
  set +m 2>/dev/null

  while kill -0 "$pid" 2>/dev/null; do
    if [ "$waited" -ge "$limit" ]; then
      # Only ever group-kill when the child genuinely leads its own group. If
      # `set -m` was unavailable the child shares OUR group, and `kill -9 -$pid`
      # would then take down this script too.
      pgid="$(ps -o pgid= -p "$pid" 2>/dev/null | tr -d ' ')"
      if [ -n "$pgid" ] && [ "$pgid" = "$pid" ]; then
        kill -9 -"$pgid" 2>/dev/null
      else
        kill -9 "$pid" 2>/dev/null
      fi
      wait "$pid" 2>/dev/null
      return 124
    fi
    sleep 0.1
    waited=$(( waited + 1 ))
  done
  wait "$pid"
}

# Run a bounded command and match its OUTPUT, without a pipeline.
#
# `bounded 10 orb status | grep -qi running` is unsafe under `set -o pipefail`:
# grep -q exits at the first match, the producer takes SIGPIPE, and the pipeline
# reports 141 — so a RUNNING OrbStack reads as down and triggers a spurious heal.
# Reproduced: rc=141 on a producer that outlives grep.
#
# Output is captured through a TEMP FILE, not `$(...)`. Command substitution waits
# for EOF on a pipe, so if the command exits while a background grandchild still
# holds stdout, the capture blocks for as long as the grandchild lives — past the
# bound, and past any signal we sent. Measured: `bounded 1 sh -c 'sleep 4 & exit 0'`
# took 4.01s under BOTH coreutils timeout and the builtin fallback. A file has no
# such reader/writer rendezvous.
#
# Return codes are deliberately distinct, because callers MUTATE on them:
#   0   matched
#   1   ran fine, did not match          → caller may act ("it really is stopped")
#   124 exceeded the bound               → hung, NOT stopped
#   125 could not probe at all           → UNKNOWN, caller must do nothing
#
# 125 exists because collapsing "I couldn't run the probe" into 1 is dangerous:
# an unwritable TMPDIR would have made check_orbstack read a healthy OrbStack as
# stopped and fire `orb start` at it every five minutes. Reproduced with
# TMPDIR=/definitely/nonexistent. "I don't know" must never be actioned as "it's
# broken" by anything whose response is to start, stop or restart something.
BOUNDED_UNKNOWN=125

bounded_matches() { # bounded_matches <seconds> <glob-pattern> <cmd...>
  local secs="$1" pattern="$2"; shift 2
  local tmp rc out
  tmp="$(mktemp "${TMPDIR:-/tmp}/bounded.XXXXXX" 2>/dev/null)" \
    || tmp="$(mktemp /tmp/bounded.XXXXXX 2>/dev/null)" \
    || return 125
  bounded "$secs" "$@" >"$tmp" 2>/dev/null; rc=$?
  out="$(cat "$tmp" 2>/dev/null)"
  rm -f "$tmp" 2>/dev/null
  [ "$rc" -eq 124 ] && return 124
  [ "$rc" -ne 0 ] && return 1
  case "$out" in
    $pattern) return 0 ;;
    *) return 1 ;;
  esac
}
