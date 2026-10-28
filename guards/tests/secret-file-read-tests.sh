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
# 2026-10-27: Codex login token, ~/.netrc and ~/.npmrc (home dir only; a project .npmrc stays readable).
check deny  "$H/.codex/auth.json"
check deny  "$H/.netrc"
check deny  "$H/.npmrc"
check deny  "$HOME/.npmrc"
check allow "$H/project/.npmrc"
check allow "$H/.codex/config.toml"
check allow "$H/.netrcx"
check deny  "$H/.gemini/antigravity-cli/mcp_oauth_tokens.json"
check deny  "$H/.gemini/oauth_creds.json.bak-1"
tool() { # expected tool args-json
  local got
  got=$(jq -nc --arg n "$2" --argjson a "$3" '{toolCall:{name:$n,args:$a}}' | ./secret-file-read.sh antigravity | jq -r .decision)
  if [ "$got" = "$1" ]; then pass=$((pass+1)); else fail=$((fail+1)); echo "FAIL $2 $3 expected=$1 got=$got"; fi
}
tool deny  grep_search  "{\"SearchPath\":\"$H/.ssh\",\"Query\":\"BEGIN\"}"
tool deny  list_dir     "{\"DirectoryPath\":\"$H/.aws/\"}"
tool deny  find_by_name "{\"SearchDirectory\":\"$H/.zsvault/unlocked.env\",\"Pattern\":\"*\"}"
tool deny  write_to_file "{\"TargetFile\":\"$H/.ssh/authorized_keys\",\"CodeContent\":\"x\"}"
tool allow write_to_file "{\"TargetFile\":\"$H/project/README.md\",\"CodeContent\":\"keys live in ~/.ssh/ and ~/.aws/\"}"
tool allow grep_search  "{\"SearchPath\":\"$H/project\",\"Query\":\".ssh/id_rsa\"}"
tool deny  grep_search  "{\"SearchPath\":\"$H/.zsvault/\",\"Query\":\".*\"}"
tool deny  list_dir     "{\"DirectoryPath\":\"$H/.ssh//\"}"
tool allow replace_file_content "{\"TargetFile\":\"/tmp/README.md\",\"TargetContent\":\"Use ~/.ssh/id_ed25519\",\"ReplacementContent\":\"Use ZS Vault\"}"
tool allow multi_replace_file_content "{\"TargetFile\":\"/tmp/README.md\",\"ReplacementChunks\":[{\"TargetContent\":\"Use ~/.ssh/id_ed25519\",\"ReplacementContent\":\"x\"}]}"
tool deny  replace_file_content "{\"TargetFile\":\"$H/.ssh/config\",\"TargetContent\":\"a\",\"ReplacementContent\":\"b\"}"
got=$(printf '%s' 'not json' | ./secret-file-read.sh antigravity | jq -r .decision)
[ "$got" = deny ] && pass=$((pass+1)) || { fail=$((fail+1)); echo "FAIL malformed got=$got"; }
got=$(jq -nc '{toolCall:{name:"view_file",args:{}}}' | ./secret-file-read.sh antigravity | jq -r .decision)
[ "$got" = allow ] && pass=$((pass+1)) || { fail=$((fail+1)); echo "FAIL empty-args got=$got"; }
echo "SUMMARY pass=$pass fail=$fail"
[ "$fail" -eq 0 ]
