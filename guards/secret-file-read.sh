#!/bin/bash
# Antigravity PreToolUse guard for file tools (view_file and friends).
#
# dangerous-command.sh only sees run_command, so `agy` could still open a
# credential file with view_file. This guard mirrors the path rules of
# ~/.claude/hooks/block-secret-reads.sh: credential stores are always denied,
# project .env files are denied unless they are public templates.
#
# Usage (hooks.json): secret-file-read.sh antigravity
# Every string in toolCall.args is checked, so a renamed path argument still
# matches. Malformed payloads fail closed. Added 2026-10-13.
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
if ! STRINGS=$(printf '%s' "$INPUT" | jq -er '
    .toolCall | select(type == "object") | .args | select(type == "object")
    | [.. | strings] | .[]' 2>/dev/null); then
  # No string args at all is a valid call; anything unparsable is not.
  if printf '%s' "$INPUT" | jq -e '.toolCall.args | type == "object"' >/dev/null 2>&1; then
    jq -nc '{decision:"allow"}'
    exit 0
  fi
  deny "invalid PreToolUse payload for secret-file-read.sh"
fi

while IFS= read -r value; do
  # APFS is case-insensitive, so match a lowercased copy.
  v=$(printf '%s' "$value" | tr '[:upper:]' '[:lower:]')
  case "$v" in
    *.ssh/*|*/.ssh/*) deny "ssh keys" ;;
    */.aws/*|*.aws/credentials*) deny "aws credential tree" ;;
    *zs-api-keys.env) deny "zs-api-keys env" ;;
    */.zsvault/unlocked.env|*/.zsvault/fingerprint-backup.key|*/.zsvault/se-identity.txt) deny "zs vault plaintext" ;;
    *.credentials.json) deny "claude credentials" ;;
    *oauth_creds.json|*oauth-token|*/clerk-tokens.json) deny "oauth token store" ;;
    *.env.example|*.env.sample|*.env.template|*.env.dist) : ;;
    *.env|*.env.*|*/.envrc) deny "dotenv secrets file" ;;
  esac
done <<< "$STRINGS"

jq -nc '{decision:"allow"}'
