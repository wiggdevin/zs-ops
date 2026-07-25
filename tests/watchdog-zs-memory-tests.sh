#!/bin/bash
# Self-contained tests for watchdog.sh's zs-memory check.
# A real health server is spawned per case; launchctl is stubbed on PATH so no
# service is ever touched. Nothing here restarts, kills, or writes outside TMP.
set -u

SCRIPT_DIR=$(CDPATH= cd "$(dirname "$0")" && pwd)
WD="$SCRIPT_DIR/../watchdog.sh"
TMP_DIR="${TMPDIR:-/tmp}/watchdog-zs-memory-tests.$$.$RANDOM"
umask 077
mkdir -p "$TMP_DIR" || { echo "FAIL setup/temp-dir"; exit 1; }

fails=0
pass() { printf 'PASS %s\n' "$1"; }
fail() { printf 'FAIL %s expected=%s got=%s\n' "$1" "$2" "$3"; fails=$((fails + 1)); }

# ── stub launchctl: record calls, never execute ──────────────────────────────
mkdir -p "$TMP_DIR/bin"
cat > "$TMP_DIR/bin/launchctl" <<'STUB'
#!/bin/sh
printf '%s\n' "$*" >> "$LAUNCHCTL_CALLS"
exit 0
STUB
chmod +x "$TMP_DIR/bin/launchctl"
export PATH="$TMP_DIR/bin:$PATH"

# ── fake health server: MODE file drives the response ────────────────────────
start_server() {
  local mode="$1" port
  port=$(python3 -c 'import socket;s=socket.socket();s.bind(("127.0.0.1",0));print(s.getsockname()[1]);s.close()')
  MODE_FILE="$TMP_DIR/mode"; printf '%s' "$mode" > "$MODE_FILE"
  MODE_FILE="$MODE_FILE" python3 - "$port" <<'PY' &
import os, sys, time
from http.server import BaseHTTPRequestHandler, HTTPServer
mode_file = os.environ["MODE_FILE"]
class H(BaseHTTPRequestHandler):
    def do_GET(self):
        mode = open(mode_file).read().strip()
        if mode == "hang":
            time.sleep(30); return
        body = (b'{"status":"degraded","service":"zs-memory",'
                b'"components":{"sqlite":"ok","embedder":"down"}}'
                if mode == "degraded" else
                b'{"status":"ok","service":"zs-memory",'
                b'"components":{"sqlite":"ok","embedder":"ok"}}')
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers(); self.wfile.write(body)
    def log_message(self, *a): pass
HTTPServer(("127.0.0.1", int(sys.argv[1])), H).serve_forever()
PY
  SERVER_PID=$!
  MEMORY_API="http://127.0.0.1:$port"
  for _ in $(seq 1 50); do
    curl -fsS --max-time 1 "$MEMORY_API/health" >/dev/null 2>&1 && break
    sleep 0.1
  done
}
stop_server() { [ -n "${SERVER_PID:-}" ] && kill "$SERVER_PID" 2>/dev/null; wait "$SERVER_PID" 2>/dev/null; }

# ── harness: run check_zs_memory once in a clean subshell ────────────────────
# shellcheck disable=SC1090
run_check() {
  (
    export WATCHDOG_LOG="$TMP_DIR/events.jsonl"
    export LAUNCHCTL_CALLS="$TMP_DIR/launchctl.calls"
    export MEMORY_API="$1"
    export ZS_MEMORY_FAIL_STATE="$TMP_DIR/failcount"
    source "$WD" >/dev/null 2>&1
    check_zs_memory
  )
}

reset_state() { : > "$TMP_DIR/events.jsonl"; : > "$TMP_DIR/launchctl.calls"; rm -f "$TMP_DIR/failcount"; }
kickstarts() { grep -c 'kickstart' "$TMP_DIR/launchctl.calls" 2>/dev/null || true; }
last_status() { tail -1 "$TMP_DIR/events.jsonl" 2>/dev/null; }

# ── 1: healthy body → healthy, no restart ────────────────────────────────────
reset_state; start_server ok
run_check "$MEMORY_API"
n=$(kickstarts)
[ "$n" -eq 0 ] && pass "healthy/no-kickstart" || fail "healthy/no-kickstart" 0 "$n"
case "$(last_status)" in *healthy*) pass "healthy/event" ;; *) fail "healthy/event" healthy "$(last_status)" ;; esac
stop_server

# ── 2: degraded body (HTTP 200) → surfaced, still no restart ─────────────────
# This is the monitoring blind spot: embedder down is invisible to a 2xx check.
reset_state; start_server degraded
run_check "$MEMORY_API"
n=$(kickstarts)
[ "$n" -eq 0 ] && pass "degraded/no-kickstart" || fail "degraded/no-kickstart" 0 "$n"
case "$(last_status)" in *degraded*) pass "degraded/event" ;; *) fail "degraded/event" degraded "$(last_status)" ;; esac
stop_server

# ── 3: ONE unreachable sweep must NOT kickstart ──────────────────────────────
reset_state
run_check "http://127.0.0.1:1"   # nothing listening
n=$(kickstarts)
[ "$n" -eq 0 ] && pass "single-failure/no-kickstart" || fail "single-failure/no-kickstart" 0 "$n"

# ── 4: the SECOND consecutive failure kickstarts ─────────────────────────────
run_check "http://127.0.0.1:1"
n=$(kickstarts)
[ "$n" -ge 1 ] && pass "second-failure/kickstarts" || fail "second-failure/kickstarts" ">=1" "$n"

# ── 5: a healthy sweep clears the counter ────────────────────────────────────
reset_state; start_server ok
run_check "http://127.0.0.1:1"          # fail 1
run_check "$MEMORY_API"                 # recovery clears
run_check "http://127.0.0.1:1"          # fail 1 again, must not restart
n=$(kickstarts)
[ "$n" -eq 0 ] && pass "recovery/clears-counter" || fail "recovery/clears-counter" 0 "$n"
stop_server

rm -rf "$TMP_DIR"
[ "$fails" -eq 0 ] && { echo "--- all watchdog zs-memory tests passed ---"; exit 0; }
echo "--- $fails failed ---"; exit 1
