#!/bin/bash

# Shared dangerous-command guard for Claude, Codex, Cursor, Antigravity, and Hermes.
# The policy is intentionally lexical. Keep lane parsing and responses separate from
# match_policy so every host enforces the same regexes.

LANE=${1-}

case "$LANE" in
  claude|codex|cursor|antigravity|hermes) : ;;
  "")
    printf '%s\n' "Usage: dangerous-command.sh <claude|codex|cursor|antigravity|hermes>" >&2
    exit 2
    ;;
  *)
    printf '%s\n' "Unknown lane '$LANE'. Expected claude, codex, cursor, antigravity, or hermes." >&2
    exit 2
    ;;
esac

INPUT=$(cat)
COMMAND=""
BLOCK_REASON=""

extract_command() {
  local filter

  case "$LANE" in
    claude|codex|hermes)
      filter='select(type == "object")
        | .tool_input
        | select(type == "object")
        | .command
        | select(type == "string" and length > 0)'
      ;;
    cursor)
      filter='select(type == "object")
        | .command
        | select(type == "string" and length > 0)'
      ;;
    antigravity)
      # manage_task send_input types text into a running shell, so that text is
      # checked like a command; its other actions carry no command (2026-10-13).
      filter='select(type == "object")
        | .toolCall
        | select(type == "object")
        | . as $call
        | .args
        | select(type == "object")
        | if ($call.name // "") == "manage_task" then
            if .Action == "send_input" then
              (.Input | if . == "" then $none else . end)
            else $none end
          else .CommandLine end
        | select(type == "string" and length > 0)'
      ;;
  esac

  COMMAND=$(printf '%s' "$INPUT" | jq -er --arg none "$NO_COMMAND" "$filter" 2>/dev/null)
}

# Sentinel for a manage_task call that runs nothing; it is allowed unchecked.
NO_COMMAND="__zs_guard_no_command__"

emit_allow() {
  case "$LANE" in
    claude|codex|hermes)
      exit 0
      ;;
    cursor)
      printf '%s\n' '{"permission":"allow"}'
      exit 0
      ;;
    antigravity)
      printf '%s\n' '{"decision":"allow"}'
      exit 0
      ;;
  esac
}

emit_block() {
  case "$LANE" in
    claude|codex)
      printf '%s\n' "$BLOCK_REASON" >&2
      exit 2
      ;;
    cursor)
      jq -nc --arg m "$BLOCK_REASON" \
        '{permission:"deny",user_message:$m,agent_message:$m}'
      exit 0
      ;;
    antigravity)
      jq -nc --arg m "$BLOCK_REASON" '{decision:"deny",reason:$m}'
      exit 0
      ;;
    hermes)
      jq -nc --arg m "$BLOCK_REASON" '{action:"block",message:$m}'
      exit 0
      ;;
  esac
}

# Absolute patterns have no override. These are preserved from the patched
# ~/.claude/hooks/block-dangerous-commands.sh source policy.
DANGEROUS_PATTERNS=(
  # Catastrophic rm: only bare root/home targets; subdirectories remain allowed.
  # Trailing globs (/*), split flags (-r -f), the option terminator (--) and quoted
  # targets match too, as in the Claude hook (GPT-6.1 Sol audit, 2026-10-13).
  "rm([[:space:]]+-[-a-zA-Z]*)+[[:space:]]+[\"']?//?\\*?/?[\"']?($|[[:space:]])"
  "rm([[:space:]]+-[-a-zA-Z]*)+[[:space:]]+[\"']?~/?\\*?/?[\"']?($|[[:space:]])"
  "rm([[:space:]]+-[-a-zA-Z]*)+[[:space:]]+[\"']?\\\$HOME/?\\*?/?[\"']?($|[[:space:]])"
  "rm([[:space:]]+-[-a-zA-Z]*)+[[:space:]]+[\"']?/Users/zero-suminc\\./?\\*?/?[\"']?($|[[:space:]])"
  "rm([[:space:]]+-[-a-zA-Z]*)+[[:space:]]+[\"']?\\.\\./?\\*?/?[\"']?($|[[:space:]])"

  # Pipe-to-shell supply-chain execution.
  "(curl|wget)[[:space:]].*\\|[[:space:]]*(sudo[[:space:]]+)?(bash|sh|zsh|fish)([[:space:]]|\$)"

  # Privilege, device, and filesystem destruction. Path-qualified sudo and the
  # Keychain password dump came over from the Claude hook on 2026-10-13 (drift).
  "(^|[[:space:]])([^[:space:]]*/)?sudo[[:space:]]"
  "chmod[[:space:]]+([-a-zA-Z]+[[:space:]]+)*0?777"
  "dd[[:space:]]+.*of=/dev/"
  "(^|[^[:alnum:]_.-])([^[:space:]]*/)?security[[:space:]]+(find-(generic|internet)-password|export)([[:space:]]+[^[:space:]]+)*[[:space:]]+[\"']?-[a-zA-Z]*[gw]"
  "(^|[[:space:]])mkfs(\\.|[[:space:]])"
  ">[[:space:]]*/dev/(sd|disk|nvme|hd)"
  ":[[:space:]]*\\(\\)[[:space:]]*\\{[[:space:]]*:[[:space:]]*\\|[[:space:]]*:"

  # Direct secret readers. The list stays narrow to avoid blocking legitimate
  # grep/awk/sed work on non-secret environment files.
  "(cat|less|more|head|tail|cp|scp|rsync|nl|xxd|base64|strings|od|hexdump|sort|uniq)[[:space:]].*(/\\.ssh/|\\.aws/|zs-api-keys\\.env|\\.credentials\\.json|\\.zsvault/(unlocked\\.env|fingerprint-backup\\.key|se-identity)|mcp_oauth_tokens\\.json|oauth_creds\\.json|antigravity-oauth-token|jetski-standalone-oauth-token)"
  # curl/wget uploads of a protected file (@file, --post-file), 2026-10-13.
  "(curl|wget)[[:space:]].*(@|--post-file=?)[^[:space:]]*(/\\.ssh/|\\.aws/|zs-api-keys\\.env|\\.credentials\\.json|\\.zsvault/|mcp_oauth_tokens\\.json|oauth_creds\\.json|antigravity-oauth-token|jetski-standalone-oauth-token)"

  # Infra, container and cron destruction (state outside any project tree), as in
  # the Claude hook since 2026-07-21; added here 2026-10-13.
  "(^|[[:space:]])(terraform|tofu|pulumi)[[:space:]]+destroy([[:space:]]|$)"
  "docker[[:space:]]+(system|volume|container)[[:space:]]+prune"
  "(^|[[:space:]])crontab[[:space:]]+-[a-zA-Z]*r"
)

INTERPRETER_COMMAND_PATTERN="(^|[;&|(])[[:space:]]*((command|builtin|exec|nohup|nice|time|xargs|env)[[:space:]]+)?([A-Za-z_][A-Za-z0-9_]*=[^[:space:]]*[[:space:]]+)*([^[:space:];&|()]*/)?(python([0-9]+(\.[0-9]+)*)?|node|ruby|perl|php|deno|bun)([[:space:]]|$)"
PROTECTED_SECRET_PATH_PATTERN="(/\.ssh/|\.aws/|zs-api-keys\.env|\.credentials\.json|\.zsvault/(unlocked\.env|fingerprint-backup\.key|se-identity)|mcp_oauth_tokens\.json|oauth_creds\.json|antigravity-oauth-token|jetski-standalone-oauth-token)"

GIT_DESTRUCTIVE_PATTERNS=(
  # 2026-09-22: branch force-delete and update-ref -d allowed (reflog recovers them).
  "git reset --hard"
  "git clean -fd"
  "git clean -f"
  "git checkout \."
  "git restore \."
  "push --force"
  "push([[:space:]]+[^[:space:];&|]+)*[[:space:]]+-[a-zA-Z]*f[a-zA-Z]*([[:space:];&|]|$)"
  "reset --hard"
)

OP_PATTERNS=(
  "(^|[;&|(])[[:space:]]*((command|builtin|exec|nohup|nice|time|xargs|env)[[:space:]]+)?([A-Za-z_][A-Za-z0-9_]*=[^[:space:]]*[[:space:]]+)*op([[:space:]]|$)"
  "(^|[;&|(])[[:space:]]*[^[:space:];&|()]*/op([[:space:]]|$)"
  "(^|[;&|(])[[:space:]]*\\\\op([[:space:]]|$)"
)

# One grep per category instead of one per pattern (perf, 2026-10-13; the Claude
# hook has done this since 2026-07-31). `grep -e p1 -e p2 ...` matches iff some
# single pattern matches, with the same engine and flags, so it is an exact
# pre-filter: a safe command skips the loops, and a match still runs the loop
# so the BLOCKED message names the pattern.
any_match() {
  local args=() p
  for p in "$@"; do args+=(-e "$p"); done
  printf '%s' "$COMMAND" | grep -qE "${args[@]}"
}

match_policy() {
  local pattern

  # One gate over every category (Argon review): the interpreter rule also needs
  # a protected path, so a command that matches none of these cannot be blocked.
  any_match "${DANGEROUS_PATTERNS[@]}" "$PROTECTED_SECRET_PATH_PATTERN" \
    "${GIT_DESTRUCTIVE_PATTERNS[@]}" "${OP_PATTERNS[@]}" || return 0

  any_match "${DANGEROUS_PATTERNS[@]}" && for pattern in "${DANGEROUS_PATTERNS[@]}"; do
    if printf '%s' "$COMMAND" | grep -qE "$pattern"; then
      BLOCK_REASON="BLOCKED: '$COMMAND' matches dangerous pattern '$pattern'. The user has prevented you from doing this."
      return 1
    fi
  done

  # Interpreters are blocked only when the same command also names a protected
  # secret path. Normal Python, Node, Ruby, and similar work remains allowed.
  # The rare protected-path test runs first so ordinary interpreter use costs one grep.
  if printf '%s' "$COMMAND" | grep -qE "$PROTECTED_SECRET_PATH_PATTERN" \
     && printf '%s' "$COMMAND" | grep -qE "$INTERPRETER_COMMAND_PATTERN"; then
    BLOCK_REASON="BLOCKED: '$COMMAND' matches interpreter pattern '$INTERPRETER_COMMAND_PATTERN' and protected-secret-path pattern '$PROTECTED_SECRET_PATH_PATTERN'. Use ZS Vault metadata/access paths instead of reading credential files."
    return 1
  fi

  # Destructive git is default-deny unless the command carries the literal
  # ZS_ALLOW_GIT=1 token (agents may self-apply it after stashing work that is not theirs, 2026-09-22). Absolute patterns still win above.
  if any_match "${GIT_DESTRUCTIVE_PATTERNS[@]}" \
     && ! printf '%s' "$COMMAND" | grep -qE 'ZS_ALLOW_GIT=1([[:space:]]|$)'; then
    for pattern in "${GIT_DESTRUCTIVE_PATTERNS[@]}"; do
      if printf '%s' "$COMMAND" | grep -qE "$pattern"; then
        BLOCK_REASON="BLOCKED: '$COMMAND' matches git-destructive pattern '$pattern'. Deliberate use (ONLY when the user explicitly asked this session): prefix with ZS_ALLOW_GIT=1"
        return 1
      fi
    done
  fi

  # 1Password CLI is default-deny unless the command carries the literal
  # human-authorized ZS_ALLOW_OP=1 token. ZS Vault remains the secrets source.
  if any_match "${OP_PATTERNS[@]}" \
     && ! printf '%s' "$COMMAND" | grep -qE 'ZS_ALLOW_OP=1([[:space:]]|$)'; then
    for pattern in "${OP_PATTERNS[@]}"; do
      if printf '%s' "$COMMAND" | grep -qE "$pattern"; then
        BLOCK_REASON="BLOCKED: '$COMMAND' matches op/1Password pattern '$pattern'. Deliberate use: prefix with ZS_ALLOW_OP=1 (e.g. ZS_ALLOW_OP=1 op item get \"GitHub\"). Secrets SoT: ZS Vault."
        return 1
      fi
    done
  fi

  return 0
}

if ! extract_command; then
  BLOCK_REASON="BLOCKED: malformed $LANE hook payload or missing/non-string/empty command."
  emit_block
fi

[ "$COMMAND" = "$NO_COMMAND" ] && emit_allow

if ! match_policy; then
  emit_block
fi

emit_allow
