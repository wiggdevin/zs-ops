#!/bin/bash
# Antigravity PreToolUse guard for file tools (view_file and friends).
#
# dangerous-command.sh only sees run_command, so `agy` could still open a
# credential file with view_file. This guard mirrors the path rules of
# ~/.claude/hooks/block-secret-reads.sh: credential stores are always denied.
#
# Usage (hooks.json): secret-file-read.sh antigravity
# For view_file every string in toolCall.args is checked, so a renamed path
# argument still matches. Malformed payloads fail closed. Added 2026-10-13. Project .env reads
# are allowed, matching the Claude hook since 2026-09-22 (Devin: fewer gates).
set -u

LANE="${1:-}"
if [ "$LANE" != "antigravity" ]; then
  echo "usage: secret-file-read.sh antigravity" >&2
  exit 2
fi

deny() {
  jq -nc --arg m "BLOCKED: $1. Use ZS Vault (zsvault get / vault_get) instead of reading credential files." \
    '{decision:"deny",reason:$m}'
  exit 0
}

INPUT=$(cat)
# view_file: every string. Search, list and edit tools (2026-10-13, GPT-6.1 Sol audit):
# only path-like arguments, so file content or a search query that names a
# credential path is not mistaken for access to it.
if ! STRINGS=$(printf '%s' "$INPUT" | jq -er '
    .toolCall | select(type == "object") | . as $call | .args | select(type == "object")
    | if ($call.name // "") == "view_file" then [.. | strings]
      else [paths(strings) as $p
            | select(([$p[] | strings] | last // "") | test("(path|file|dir|directory|uri)$"; "i"))
            | getpath($p)]
      end
    | .[]' 2>/dev/null); then
  # No string args at all is a valid call; anything unparsable is not.
  if printf '%s' "$INPUT" | jq -e '.toolCall.args | type == "object"' >/dev/null 2>&1; then
    jq -nc '{decision:"allow"}'
    exit 0
  fi
  deny "invalid PreToolUse payload for secret-file-read.sh"
fi

# APFS is case-insensitive, so match a lowercased copy (one tr for all values).
STRINGS=$(printf '%s' "$STRINGS" | tr '[:upper:]' '[:lower:]')
HOME_LC=$(printf '%s' "${HOME:-/nonexistent}" | tr '[:upper:]' '[:lower:]')
while IFS= read -r v; do
  # Trailing separators (~/.zsvault/) must not dodge the directory rules.
  while [ "${#v}" -gt 1 ] && [ "${v%/}" != "$v" ]; do v=${v%/}; done
  # .netrc and .npmrc hold tokens only in the home dir; a project .npmrc stays readable.
  if [[ $v =~ ^(/users/[^/]+|~)/\.(netrc|npmrc)$ ]] || [ "$v" = "$HOME_LC/.netrc" ] || [ "$v" = "$HOME_LC/.npmrc" ]; then
    deny "netrc/npmrc credentials"
  fi
  case "$v" in
    *.ssh/*|*/.ssh/*|*/.ssh) deny "ssh keys" ;;
    */.aws/*|*/.aws|*.aws/credentials*) deny "aws credential tree" ;;
    */.zsvault) deny "zs vault directory" ;;
    *zs-api-keys.env) deny "zs-api-keys env" ;;
    */.zsvault/unlocked.env|*/.zsvault/fingerprint-backup.key|*/.zsvault/se-identity.txt) deny "zs vault plaintext" ;;
    *.credentials.json) deny "claude credentials" ;;
    *oauth_creds.json*|*oauth-token*|*mcp_oauth_tokens.json*|*/clerk-tokens.json) deny "oauth token store" ;;
    */.codex/auth.json*) deny "codex login token" ;;
  esac
done <<< "$STRINGS"

printf '%s\n' '{"decision":"allow"}'
