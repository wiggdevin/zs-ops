#!/bin/zsh
# mem0 (zs-memory) MCP launcher for Claude Desktop.
# Same pattern as ~/bin/mem0-mcp.sh: the scoped claude token comes from ZS Vault,
# passed via env only — the single-quoted ${ZS_MEMORY_TOKEN} placeholder is
# substituted by mcp-remote itself, so the value never appears in argv.
# npx is referenced by absolute path because Claude Desktop's GUI launch
# context does not inherit the shell PATH (mise shims).
set -euo pipefail

# Scoped `claude` token, matching every other MCP client. The primary token
# bypasses scope checks entirely and can target any user_id including
# delete_all_memories — no interactive client should hold it.
# An inherited ZS_MEMORY_TOKEN is ignored on purpose: it may be the primary token.
ZS_MEMORY_TOKEN="$("$HOME/bin/zsvault" get zs_memory_token_claude)" || {
  print -u2 "mem0-mcp-claude-desktop.sh: zsvault get zs_memory_token_claude failed"; exit 1
}
[ -n "$ZS_MEMORY_TOKEN" ] || { print -u2 "mem0-mcp-claude-desktop.sh: empty claude token"; exit 1; }
export ZS_MEMORY_TOKEN

# Server URL from ~/bin/zs-memory-url (reads ~/.config/zs-memory/url; loopback when absent).
BASE_URL="$("$HOME/bin/zs-memory-url")" || exit 1
exec "$HOME/.local/share/mise/shims/npx" -y mcp-remote "${BASE_URL}/mcp" \
  --header 'Authorization: Bearer ${ZS_MEMORY_TOKEN}' \
  --header "X-Client-Name: claude-desktop"
