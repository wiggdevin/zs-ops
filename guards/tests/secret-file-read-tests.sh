#!/bin/bash
# Cases for secret-file-read.sh (Antigravity view_file guard).
cd "$(dirname "$0")/.." || exit 1
pass=0; fail=0
check() { # expected path
  local got
  got=$(jq -nc --arg p "$2" '{toolCall:{name:"view_file",args:{AbsolutePath:$p}}}' | ./secret-file-read.sh antigravity | jq -r .decision)
  if [ "$got" = "$1" ]; then pass=$((pass+1)); else fail=$((fail+1)); echo "FAIL $2 expected=$1 got=$got"; fi
}
H=/Users/someone
check deny  "$H/.ssh/id_ed25519"
check deny  "$H/.SSH/id_rsa"
check deny  "$H/.aws/credentials"
check deny  "$H/.config/zs-api-keys.env"
check deny  "$H/.zsvault/unlocked.env"
check deny  "$H/.claude/.credentials.json"
check deny  "$H/.gemini/oauth_creds.json"
check deny  "$H/.gemini/antigravity-cli/antigravity-oauth-token"
check allow "$H/project/.env"
check allow "$H/project/.env.local"
check allow "$H/project/.env.example"
check allow "$H/project/src/index.ts"
check allow "$H/project/README.md"
got=$(printf '%s' 'not json' | ./secret-file-read.sh antigravity | jq -r .decision)
[ "$got" = deny ] && pass=$((pass+1)) || { fail=$((fail+1)); echo "FAIL malformed got=$got"; }
got=$(jq -nc '{toolCall:{name:"view_file",args:{}}}' | ./secret-file-read.sh antigravity | jq -r .decision)
[ "$got" = allow ] && pass=$((pass+1)) || { fail=$((fail+1)); echo "FAIL empty-args got=$got"; }
echo "SUMMARY pass=$pass fail=$fail"
[ "$fail" -eq 0 ]
