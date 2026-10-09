#!/bin/zsh
# mem0 (zs-memory) MCP launcher for Claude Desktop.
# Same pattern as ~/.codex/bin/mem0-mcp.sh: token resolved from env or ZS Vault,
# passed via env only — the single-quoted ${ZS_MEMORY_TOKEN} placeholder is
# substituted by mcp-remote itself, so the value never appears in argv.
# npx is referenced by absolute path because Claude Desktop's GUI launch
# context does not inherit the shell PATH (mise shims).
set -euo pipefail

# Scoped `claude` token, matching every other MCP client. The primary token
# bypasses scope checks entirely and can target any user_id including
# delete_all_memories — no interactive client should hold it.
if [ -z "${ZS_MEMORY_TOKEN:-}" ]; then
  ZS_MEMORY_TOKEN="$(/Users/zero-suminc./bin/zsvault get zs_memory_token_claude)"
fi
export ZS_MEMORY_TOKEN

exec /Users/zero-suminc./.local/share/mise/shims/npx -y mcp-remote http://127.0.0.1:8765/mcp \
  --header 'Authorization: Bearer ${ZS_MEMORY_TOKEN}' \
  --header "X-Client-Name: claude-desktop"
