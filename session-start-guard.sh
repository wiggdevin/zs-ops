#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# session-start-guard.sh — fast, ADVISORY pre-flight for an agent session.
# Wire as a Claude Code SessionStart hook. Its stdout becomes session context.
# It NEVER blocks the session (always exits 0).
#
# 2026-06-10: memory stack replaced — zs-memory (native launchd, :8765, SQLite)
# supersedes OpenMemory/Qdrant/docker. The config-drift and write-policy
# container-patch checks are gone: the guard is compiled into the service.
#
# Wire it (~/.claude/settings.json):
#   { "hooks": { "SessionStart": [ { "hooks": [
#       { "type": "command", "command": "$HOME/.config/zs-ops/session-start-guard.sh" }
#   ] } ] } }
# ─────────────────────────────────────────────────────────────────────────────
set -uo pipefail

MEMORY_API="${MEMORY_API:-http://localhost:8765}"
OLLAMA_API="${OLLAMA_API:-http://localhost:11434}"

offline=0
ok()  { printf '  ✓ %s\n' "$1"; }
bad() { printf '  ✗ %s\n' "$1"; offline=1; }
http_ok() { curl -fsS --max-time 3 "$1" >/dev/null 2>&1; }

echo "── memory stack pre-flight ──"

if http_ok "$MEMORY_API/health"; then ok "zs-memory :8765"
else bad "zs-memory :8765 (launchctl kickstart -k gui/\$(id -u)/com.zs.memory)"; fi
if http_ok "$OLLAMA_API/api/tags"; then ok "ollama :11434"
else bad "ollama :11434 (launchctl kickstart -k gui/\$(id -u)/com.zs.ollama)"; fi

# Self-heal the op-guard PATH shim (default-deny for the 1Password CLI). The shim shadows
# `op` on PATH so by-name invocations refuse unless ZS_ALLOW_OP=1; the PATH wiring lives in
# ~/.zshenv + ~/.zshrc + ~/.zprofile. Recreate the shim if deleted; advise (do not auto-edit)
# if the PATH line is gone from all three.
OP_GUARD_DIR="$HOME/.config/zs-ops/path-guards"
OP_SHIM="$OP_GUARD_DIR/op"
if [ -x "$OP_SHIM" ]; then
  ok "op-guard shim present"
else
  mkdir -p "$OP_GUARD_DIR" 2>/dev/null
  cat > "$OP_SHIM" <<'OPSHIM' 2>/dev/null
#!/bin/sh
# ZS op-guard — PATH shadow for the 1Password CLI (default-deny for autonomous use).
# Deliberate use: ZS_ALLOW_OP=1 op ...  (one-shot prefix; passes through to the real binary).
# Real binary intact at the Homebrew path. Decisions logged to ~/.config/zs-ops/op-guard.log.
REAL=/opt/homebrew/bin/op
LOG="$HOME/.config/zs-ops/op-guard.log"
_ts() { date -u +%Y-%m-%dT%H:%M:%SZ 2>/dev/null; }
if [ "${ZS_ALLOW_OP:-}" = "1" ]; then
  { printf '%s ALLOW-OVERRIDE op %s\n' "$(_ts)" "${1:-}" >> "$LOG"; } 2>/dev/null
  [ -x "$REAL" ] && exec "$REAL" "$@"
  echo "op-guard: ZS_ALLOW_OP=1 set but real op not found at $REAL" >&2; exit 127
fi
{ printf '%s DENY op %s\n' "$(_ts)" "${1:-}" >> "$LOG"; } 2>/dev/null
echo "BLOCKED: 'op' / 1Password CLI is default-denied for autonomous use (CLAUDE.md rule)." >&2
echo "Deliberate use: prefix with ZS_ALLOW_OP=1 (one-shot, do NOT export). Secrets SoT: ZS Vault." >&2
exit 1
OPSHIM
  if chmod +x "$OP_SHIM" 2>/dev/null && [ -x "$OP_SHIM" ]; then
    ok "op-guard shim recreated (had been deleted)"
  else
    printf '  ✗ %s\n' "op-guard shim MISSING and could not recreate at $OP_SHIM"
  fi
fi
if ! grep -qs 'zs-ops/path-guards' "$HOME/.zshenv" "$HOME/.zshrc" "$HOME/.zprofile"; then
  printf '  ⚠ %s\n' 'op-guard PATH line missing from ~/.zshenv & ~/.zshrc & ~/.zprofile (add export PATH=".../zs-ops/path-guards:$PATH")'
fi

if [ "$offline" = "1" ]; then
  echo "⚠️  MEMORY STACK DEGRADED — reads/writes to the shared bus may fail until resolved."
fi
echo "── namespace: user_id=zero-fleet · write decisions, not code ──"
exit 0
