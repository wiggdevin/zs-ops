#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# telemetry.sh — ONE latency/health sample of the local memory stack → JSONL.
# Run by launchd (or by watchdog.sh) on an interval. Cheap; non-destructive.
# Appends one JSON object per endpoint to $TELEMETRY_LOG:
#   {"ts","component","ok":bool,"latency_ms":int,"http":code}
# Plus a "resident_models" line from `ollama ps` (RAM pressure visibility).
#
# Cost telemetry (per-agent $ spend) is intentionally NOT scraped here — each
# tool owns its own usage ledger (Claude Code /cost, Codex usage). This samples
# only what's locally measurable: latency + which models are resident.
# ─────────────────────────────────────────────────────────────────────────────
set -uo pipefail

TELEMETRY_LOG="${TELEMETRY_LOG:-$HOME/Library/Logs/zs-telemetry.jsonl}"
MEM0_API="${MEM0_API:-http://localhost:8765}"
OLLAMA_API="${OLLAMA_API:-http://localhost:11434}"
OMLX_API="${OMLX_API:-http://127.0.0.1:8000}"
mkdir -p "$(dirname "$TELEMETRY_LOG")"

ts() { date -u +%Y-%m-%dT%H:%M:%SZ; }

sample() { # sample <component> <url>
  local name="$1" url="$2" t code ok ms
  # one curl: total time + http code, even on failure (-s, no -f)
  read -r code t < <(curl -s -o /dev/null -m 5 -w '%{http_code} %{time_total}' "$url" 2>/dev/null || echo "000 0")
  # Only 2xx is healthy. A 3xx on a health endpoint (no -L) means it's redirecting,
  # not serving — treat as not-ok so the sample isn't a false positive.
  case "$code" in 2*) ok=true ;; *) ok=false ;; esac
  ms="$(awk -v x="$t" 'BEGIN{printf "%d", x*1000}')"
  if command -v jq >/dev/null 2>&1; then
    jq -cn --arg ts "$(ts)" --arg c "$name" --argjson ok "$ok" --argjson ms "$ms" --arg http "$code" \
      '{ts:$ts,component:$c,ok:$ok,latency_ms:$ms,http:$http}' >> "$TELEMETRY_LOG"
  else
    printf '{"ts":"%s","component":"%s","ok":%s,"latency_ms":%s,"http":"%s"}\n' \
      "$(ts)" "$name" "$ok" "$ms" "$code" >> "$TELEMETRY_LOG"
  fi
}

sample mem0   "$MEM0_API/health"
sample omlx   "$OMLX_API/health"
sample ollama "$OLLAMA_API/api/tags"

# Resident models (RAM pressure) — best-effort. Distinguish daemon-down
# (OLLAMA_UNREACHABLE) from up-with-nothing-loaded (empty string).
if command -v ollama >/dev/null 2>&1; then
  if ps_out="$(ollama ps 2>/dev/null)"; then
    models="$(printf '%s\n' "$ps_out" | awk 'NR>1{print $1}' | paste -sd, - 2>/dev/null || true)"
  else
    models="OLLAMA_UNREACHABLE"
  fi
  if command -v jq >/dev/null 2>&1; then
    jq -cn --arg ts "$(ts)" --arg m "${models:-}" '{ts:$ts,component:"ollama_ps",resident_models:$m}' >> "$TELEMETRY_LOG"
  else
    printf '{"ts":"%s","component":"ollama_ps","resident_models":"%s"}\n' "$(ts)" "${models:-}" >> "$TELEMETRY_LOG"
  fi
fi
