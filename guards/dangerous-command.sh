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
      # Known limit: each send_input is checked alone, so a command split across
      # two inputs ("su", then "do ls") is not seen whole. Like every pattern
      # here, this guard is lexical, not a sandbox.
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

# Absolute patterns have no override. These mirror ~/.claude/hooks/block-dangerous-commands.sh
# (the Claude hook is the reference; the 2026-10-27 hardening pass was ported here).

# Protected credential locations: one shared list, also read by the Claude hooks. If
# it cannot be sourced, fall back to the core stores so the guard never goes blind.
SECRET_DIRS='' SECRET_FILES='' SECRET_HOME_FILES=''
# The list is usable only when it is nonempty and every part is a valid ERE; an invalid
# pattern would make the combined greps below error out and match nothing (fail open).
secret_list_ok() {
  [ -n "$SECRET_DIRS" ] && [ -n "$SECRET_FILES" ] || return 1
  local args=(-e "($SECRET_DIRS)" -e "($SECRET_FILES)")
  [ -n "$SECRET_HOME_FILES" ] && args+=(-e "($SECRET_HOME_FILES)")
  printf '' | grep -qE "${args[@]}" 2>/dev/null
  [ $? -ne 2 ]
}
# shellcheck source=/dev/null
. "${ZS_SECRET_PATHS:-${HOME:-/Users/zero-suminc.}/.claude/hooks/secret-paths.sh}" 2>/dev/null
if ! secret_list_ok; then
  SECRET_DIRS='\.ssh|\.aws'
  SECRET_FILES='zs-api-keys\.env|\.credentials\.json|\.zsvault/unlocked\.env|\.zsvault/fingerprint-backup\.key|\.zsvault/se-identity|\.codex/auth\.json|mcp_oauth_tokens\.json|oauth_creds\.json|antigravity-oauth-token|jetski-standalone-oauth-token'
  SECRET_HOME_FILES='\.netrc|\.npmrc'
fi
# A directory name matches as a whole path element (x.ssh/ does not). Home-only files
# match under ~, $HOME, ${HOME} or /Users/<name>, so a project .npmrc stays readable.
# SECRET_PATH_NB has no leading boundary (the reader and upload patterns bring their
# own); SECRET_PATH_ALT is the same list with one, for scans of a whole command line.
# A directory ends at /, the end of the line, whitespace or a quote, so `cp -R ~/.aws /x`
# and `cat .aws/credentials` match, `~/.sshfoo` does not.
SECRET_DIR_NB="(${SECRET_DIRS})(/|\$|[[:space:]\"'])"
SECRET_REST_NB="(${SECRET_FILES})"
if [ -n "$SECRET_HOME_FILES" ]; then
  SECRET_REST_NB="${SECRET_REST_NB}|(~|\\\$HOME|\\\$\\{HOME\\}|/Users/[^/[:space:]]+)/(${SECRET_HOME_FILES})([[:space:]\"']|\$)"
fi
SECRET_PATH_NB="${SECRET_DIR_NB}|${SECRET_REST_NB}"
SECRET_PATH_ALT="(^|[^[:alnum:]_.-])${SECRET_DIR_NB}|${SECRET_REST_NB}"

# Catastrophic rm / find -delete: root, home (~, $HOME, ${HOME}, the real home), the
# parent dir and the system trees. Suffixes /  /*  /.  /.* match too. The command word
# needs a boundary, so `terraform -chdir ../ plan` is not an rm (2026-10-27).
RM_TARGET="(/|~|\\\$HOME|\\\$\\{HOME\\}|/Users/zero-suminc\\.|\\.\\.|/Users|/etc|/System|/Library|/Applications)"
RM_SUFFIX="/*(\\.\\*?|\\*)?/*"
RM_PATTERN="(^|[^[:alnum:]_.-])rm([[:space:]]+-[-a-zA-Z]*)+([[:space:]]+[^[:space:];&|()]+)*[[:space:]]+[\"']?${RM_TARGET}[\"']?${RM_SUFFIX}[\"']?(\$|[[:space:];&|)])"
FIND_DELETE_PATTERN="(^|[^[:alnum:]_.-])find[[:space:]]+[\"']?${RM_TARGET}[\"']?${RM_SUFFIX}[\"']?[[:space:]]+(-(depth|xdev|mindepth|maxdepth)([[:space:]]+[0-9]+)?[[:space:]]+)*-delete"

# Command position for sudo: start of line, after ; & | ( { ` ! or a quote, or after
# then/else/do; env assignments and common wrappers may sit in front. A bare word
# elsewhere (`git commit -m 'document sudo usage'`) is not a sudo call (2026-10-27).
CMD_POS="(^|[;&|()\`{!]|(-[a-zA-Z]*c|eval)[[:space:]]+[\"']|(^|[;&|()\`{])[[:space:]]*(then|else|do|elif|if|while|until))[[:space:]]*"
CMD_WRAPS="((command|builtin|exec|nohup|nice|time|xargs|env|doas)(([[:space:]]+-[^[:space:]]+)([[:space:]]+[^-[:space:]][^[:space:]]*)?)*[[:space:]]+|[A-Za-z_][A-Za-z0-9_]*=[^[:space:]]*[[:space:]]+)*"
SHELL_WORDS="(bash|sh|zsh|fish|dash|ksh)"

DANGEROUS_PATTERNS=(
  # Catastrophic rm / find -delete.
  "$RM_PATTERN"
  "$FIND_DELETE_PATTERN"

  # Pipe-to-shell supply-chain execution: `| bash`, `| /bin/bash`, `| sudo bash`,
  # `bash <(curl ...)`, `sh -c "$(curl ...)"`, `eval "$(curl ...)"`. Piping to an
  # interpreter (python3, node) is not covered; this rule is about shells.
  "(curl|wget)[[:space:]].*\\|[[:space:]]*(((([^[:space:]|;&]*/)?(sudo|env|command|builtin|exec|nohup|nice|time))[[:space:]]+(-[^[:space:]]+[[:space:]]+([^-[:space:]|;&][^[:space:]|;&]*[[:space:]]+)?|[A-Za-z_][A-Za-z0-9_]*=[^[:space:]]*[[:space:]]+)*)|[A-Za-z_][A-Za-z0-9_]*=[^[:space:]]*[[:space:]]+)*([^[:space:]|;&]*/)?${SHELL_WORDS}([[:space:]]|\$)"
  "(^|[^[:alnum:]_.-])(([^[:space:]]*/)?${SHELL_WORDS}([[:space:]]+-[a-zA-Z]+)*|source|eval|\\.)[[:space:]]+(<[[:space:]]+)?[\"']?(<\\(|\\\$\\(|\`)[[:space:]]*(curl|wget)([[:space:]]|\$)"

  # Privilege, device, and filesystem destruction. Path-qualified sudo and the
  # Keychain password dump came over from the Claude hook on 2026-10-13 (drift).
  # (dd to a /dev/ target is checked separately so of=/dev/null stays allowed.)
  "${CMD_POS}${CMD_WRAPS}([^[:space:]]*/)?sudo([[:space:]]|\$)"
  "chmod[[:space:]]+([-a-zA-Z]+[[:space:]]+)*0?777"
  "(^|[^[:alnum:]_.-])([^[:space:]]*/)?security[[:space:]]+(find-(generic|internet)-password|export)([[:space:]]+[^[:space:]]+)*[[:space:]]+[\"']?-[a-zA-Z]*[gw]"
  "(^|[[:space:]])mkfs(\\.|[[:space:]])"
  ">[[:space:]]*/dev/(sd|disk|nvme|hd)"
  ":[[:space:]]*\\(\\)[[:space:]]*\\{[[:space:]]*:[[:space:]]*\\|[[:space:]]*:"

  # Direct secret readers. The list stays narrow to avoid blocking legitimate
  # grep/awk/sed work on non-secret environment files.
  "(cat|less|more|head|tail|cp|scp|rsync|nl|xxd|base64|strings|od|hexdump|sort|uniq)[[:space:]]+(.*[^[:alnum:]_.-])?(${SECRET_PATH_NB})"
  # curl/wget uploads of a protected file (@file, --post-file), 2026-10-13.
  "(curl|wget)[[:space:]].*(@|--post-file=?)([^[:space:]]*[^[:alnum:]_.-])?(${SECRET_PATH_NB}|\\.zsvault/)"

  # Infra, container and cron destruction (state outside any project tree), as in
  # the Claude hook since 2026-07-21; added here 2026-10-13.
  "(^|[[:space:]])(terraform|tofu|pulumi)[[:space:]]+destroy([[:space:]]|$)"
  "docker[[:space:]]+(system|volume|container)[[:space:]]+prune"
  "(^|[[:space:]])crontab[[:space:]]+-[a-zA-Z]*r"
)

# The interpreter may sit behind env assignments and common wrappers (env, xargs,
# mise exec --, uv run, pnpm exec, npx, bunx, `bash -c "..."`; 2026-10-27).
INTERP_WRAPS="(([A-Za-z_][A-Za-z0-9_]*=[^[:space:]]*|(command|builtin|exec|nohup|nice|time|xargs|env|npx|bunx|uvx)(([[:space:]]+-[^[:space:]]+)([[:space:]]+[^-[:space:]][^[:space:]]*)?)*|mise[[:space:]]+exec(([[:space:]]+-[^[:space:]]+)([[:space:]]+[^-[:space:]][^[:space:]]*)?|[[:space:]]+[^-[:space:]][^[:space:]]*)*([[:space:]]+--)?|uv[[:space:]]+run(([[:space:]]+-[^[:space:]]+)([[:space:]]+[^-[:space:]][^[:space:]]*)?)*|(pnpm|npm|yarn)[[:space:]]+(exec|dlx)([[:space:]]+--)?|(bash|sh|zsh|dash)[[:space:]]+-[a-zA-Z]*c)[[:space:]]+[\"']?)*"
INTERPRETER_COMMAND_PATTERN="(^|[;&|(])[[:space:]]*${INTERP_WRAPS}([^[:space:];&|()]*/)?(python([0-9]+(\.[0-9]+)*)?|node|ruby|perl|php|deno|bun)([[:space:]]|\$)"
PROTECTED_SECRET_PATH_PATTERN="(${SECRET_PATH_ALT})"

GIT_DESTRUCTIVE_PATTERNS=(
  # 2026-09-22: branch force-delete and update-ref -d allowed (reflog recovers them).
  "git reset --hard"
  # (git clean is judged by flag set in git_check, so -n/--dry-run is honoured)
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

# Simple-command splitting (for the override tokens and the git rules). Splits the
# line on ; & | ( ) ` and newlines, honouring quotes, backslashes and # comments.
# One line out per command, its words joined by US (\037) so a quoted argument keeps
# its spaces. Lexical, not a shell. (Same tokenizer as the Claude hook.)
SEP=$'\037'
AWK_SPLIT='
function flush_tok() { if (intok) { seg = seg (nt ? SEP : "") tok; nt++; tok = ""; intok = 0 } }
function flush_seg() { flush_tok(); if (nt > 0) print seg; seg = ""; nt = 0 }
BEGIN { RS = "\001"; SEP = "\037"; sq = "\047"; dq = "\042"; bt = "\140" }
{
  s = $0; n = length(s); q = ""; tok = ""; intok = 0; seg = ""; nt = 0; esc = 0; ansi = 0
  for (i = 1; i <= n; i++) {
    c = substr(s, i, 1)
    if (esc) { if (c != "\n") { tok = tok c; intok = 1 } esc = 0; continue }
    if (q == sq) {
      if (ansi && c == "\\") { tok = tok c substr(s, i + 1, 1); i++ }
      else if (c == sq) { q = ""; ansi = 0 }
      else tok = tok (c == "\n" ? " " : c)
      continue
    }
    if (q == dq) {
      if (c == "\\") { d = substr(s, i + 1, 1); if (d == dq || d == "\\" || d == "$" || d == bt) { tok = tok d; i++ } else if (d == "\n") { i++ } else tok = tok c }
      else if (c == dq) q = ""
      else tok = tok (c == "\n" ? " " : c)
      continue
    }
    if (c == "\\") { esc = 1; continue }
    if (c == "$" && substr(s, i + 1, 1) == sq) { ansi = 1; continue }
    if (c == sq || c == dq) { q = c; intok = 1; continue }
    if (c == "#" && !intok) { while (i < n && substr(s, i + 1, 1) != "\n") i++; continue }
    if (c == " " || c == "\t") { flush_tok(); continue }
    if (c == ";" || c == "\n" || c == "|" || c == "&" || c == "(" || c == ")" || c == bt) { flush_seg(); continue }
    tok = tok c; intok = 1
  }
  flush_seg()
  if (q != "") print "\037UNBALANCED\037"
}'
UNBAL=$'\037UNBALANCED\037'
# Quote state the tokenizer cannot close (a quote that bash balances differently): no
# override is honoured on such a line. The callers re-run the text with every quote and
# backslash removed, so each ; | & ( ) is a real split and a leading override only counts
# where it really leads its command.
unquote() { printf '%s' "$1" | tr -d "\"'\\\\"; }
split_segments() { printf '%s' "$1" | awk "$AWK_SPLIT"; }
# set_tok <record>: split one record into TOK[] on US. Plain word splitting, so no
# here-string (bash 3.2 backs those with a temp file) and no fail-open if it cannot be made.
set_tok() {
  local IFS="$SEP"
  set -f
  TOK=($1)
  set +f
}

# seg_auth <assignment>: true when the simple command in TOK[] starts with the
# override as an env assignment (optionally through env/command/builtin/exec).
seg_auth() {
  local want="$1" t
  for t in "${TOK[@]}"; do
    case "$t" in
      "$want") return 0 ;;
      [A-Za-z_]*=*|env|*/env|command|*/command|builtin|exec|*/exec) : ;;
      *) return 1 ;;
    esac
  done
  return 1
}

# without_authorized <assignment>: the command text with every simple command that
# carries the override removed. Used only when the token appears in the line at all.
without_authorized() {
  local out="" line t
  while IFS= read -r line; do
    [ -z "$line" ] && continue
    if [ "$line" = "$UNBAL" ]; then
      local COMMAND="$COMMAND"
      COMMAND=$(unquote "$COMMAND")
      without_authorized "$1"
      return
    fi
    set_tok "$line"
    if seg_auth "$1"; then
      # the override covers this command, not a $(...) it runs first
      for t in "${TOK[@]}"; do
        case "$t" in *'$('*|*'`'*) out="${out}${out:+ ; }${t}" ;; esac
      done
      continue
    fi
    out="${out}${out:+ ; }${line//$SEP/ }"
  done < <(split_segments "$COMMAND")
  printf '%s' "$out"
}

GIT_GREP_ARGS=()
for _p in "${GIT_DESTRUCTIVE_PATTERNS[@]}"; do GIT_GREP_ARGS+=(-e "$_p"); done

git_deny() {  # git_deny <why>
  BLOCK_REASON="BLOCKED: '$COMMAND' is git-destructive ($1).
To proceed: run git status, stash any changes that are not yours, then prefix that command with ZS_ALLOW_GIT=1 (as a leading assignment: ZS_ALLOW_GIT=1 git ...)."
  emit_block
}

protected_branch() {  # protected_branch <ref or refspec side>
  local b="${1#+}"; b="${b#:}"; b="${b#refs/heads/}"
  case "$b" in HEAD|main|master|develop|development|trunk|production|release*) return 0 ;; esac
  return 1
}

# Index of the command word in TOK[]: past env assignments and runner words.
cmd_index() {
  CI=0
  local t w="" n=${#TOK[@]} k
  while [ "$CI" -lt "$n" ]; do
    t=${TOK[$CI]}
    case "$t" in
      [A-Za-z_]*=*) CI=$((CI + 1)) ;;
      env|command|builtin|exec|nohup|time|nice|noglob|rtk|xargs|npx|bunx|uvx|doas|*/env|*/command|*/builtin|*/exec|*/nohup|*/time|*/nice|*/rtk|*/xargs|*/npx|*/bunx|*/uvx|*/doas) w=${t##*/}; CI=$((CI + 1)) ;;
      then|else|do|elif|if|while|until|'{'|'!') w=""; CI=$((CI + 1)) ;;
      mise)
        if [ "${TOK[$((CI + 1))]:-}" = exec ]; then
          k=$((CI + 2))
          while [ "$k" -lt "$n" ] && [ "${TOK[$k]}" != "--" ]; do k=$((k + 1)); done
          if [ "$k" -lt "$n" ]; then CI=$((k + 1)); else CI=$((CI + 2)); fi
        else break; fi ;;
      uv) if [ "${TOK[$((CI + 1))]:-}" = run ]; then w=uv; CI=$((CI + 2)); else break; fi ;;
      pnpm|npm|yarn) case "${TOK[$((CI + 1))]:-}" in exec|dlx) w=$t; CI=$((CI + 2)) ;; *) break ;; esac ;;
      --) CI=$((CI + 1)) ;;
      -*)
        case "$w:$t" in
          env:-u|env:-C|env:-S|nice:-n|xargs:-[nIPLsdE]|exec:-a|doas:-u|uv:--project|uv:--directory|uv:--with|uv:-p|uv:--python|uv:--package) CI=$((CI + 2)) ;;
          *) CI=$((CI + 1)) ;;
        esac ;;
      *) break ;;
    esac
  done
}

is_dot_pathspec() {  # the whole tree: ., ./, ././, :/, :/.
  local p="$1"
  while [ "${p#./}" != "$p" ]; do p=${p#./}; done
  case "$p" in .|""|:/|:/.) return 0 ;; esac
  return 1
}

git_check() {  # git_check <index of the word after "git">
  local j=$1 n=${#TOK[@]} t sub a past=0 cc ch lease=0
  while [ "$j" -lt "$n" ]; do
    t=${TOK[$j]}
    case "$t" in
      -C|-c|--git-dir|--work-tree|--namespace) j=$((j + 2)) ;;
      -*) j=$((j + 1)) ;;
      *) break ;;
    esac
  done
  [ "$j" -ge "$n" ] && return 0
  sub=${TOK[$j]}; j=$((j + 1))
  local force=0 dry=0 staged=0 worktree=0 delete=0 hard=0 npos=0 dot=0 repo=0
  local -a pos=()
  while [ "$j" -lt "$n" ]; do
    a=${TOK[$j]}; j=$((j + 1))
    if [ "$past" -eq 1 ]; then
      pos+=("$a"); npos=$((npos + 1)); is_dot_pathspec "$a" && dot=1
      continue
    fi
    case "$a" in
      --) past=1 ;;
      --force) force=1 ;;
      --dry-run) dry=1 ;;
      --staged) staged=1 ;;
      --worktree) worktree=1 ;;
      --delete) delete=1 ;;
      --hard) hard=1 ;;
      --no-dry-run) dry=0 ;;
      --no-force) force=0 ;;
      --no-staged) staged=0 ;;
      --no-worktree) worktree=0 ;;
      --repo) repo=1; j=$((j + 1)) ;;
      --repo=*) repo=1 ;;
      --exclude) j=$((j + 1)) ;;
      --force-with-lease|--force-with-lease=*|--force-if-includes) lease=1 ;;
      --*) : ;;
      -*)
        # one letter at a time: in `git clean -fdenode_modules` the letters after e are the
        # exclude pattern, not flags, and a trailing e takes the next word
        cc=${a#-}
        while [ -n "$cc" ]; do
          ch=${cc:0:1}; cc=${cc:1}
          case "$ch" in
            f) force=1 ;;
            n) dry=1 ;;
            S) staged=1 ;;
            W) worktree=1 ;;
            d) delete=1 ;;
            e) if [ "$sub" = clean ]; then [ -z "$cc" ] && j=$((j + 1)); cc=""; fi ;;
          esac
        done
        ;;
      *) pos+=("$a"); npos=$((npos + 1)); is_dot_pathspec "$a" && dot=1 ;;
    esac
  done
  case "$sub" in
    clean)
      [ "$force" -eq 1 ] && [ "$dry" -eq 0 ] && git_deny "git clean with force"
      ;;
    checkout)
      [ "$dot" -eq 1 ] && git_deny "git checkout overwrites the whole working tree"
      ;;
    restore)
      if [ "$dot" -eq 1 ] && { [ "$staged" -eq 0 ] || [ "$worktree" -eq 1 ]; }; then
        git_deny "git restore overwrites the whole working tree"
      fi
      ;;
    reset)
      [ "$hard" -eq 1 ] && git_deny "git reset --hard"
      ;;
    push)
      [ "$force" -eq 1 ] && git_deny "force push"
      [ "$lease" -eq 1 ] && git_deny "force-with-lease push"
      local k r
      for ((k = 1 - repo; k < npos; k++)); do
        r=${pos[$k]}
        case "$r" in
          +*) git_deny "force push through a + refspec" ;;
          :*) protected_branch "${r#:}" && git_deny "deletes a protected remote branch" ;;
        esac
        if [ "$delete" -eq 1 ]; then
          protected_branch "${r##*:}" && git_deny "deletes a protected remote branch"
        fi
      done
      ;;
  esac
  return 0
}

# Scan one command string: the literal patterns on each simple command, then the
# flag-set checks for git. `sh -c "..."` and `eval "..."` arguments are scanned too
# (3 levels). Blocks (exits through emit_block) on a hit.
scan_git_string() {  # scan_git_string <string> <depth>
  local str="$1" depth="$2" line segtext p k base t inner bt=$'`'
  local -a mine
  while IFS= read -r line; do
    [ -z "$line" ] && continue
    if [ "$line" = "$UNBAL" ]; then
      scan_git_string "$(unquote "$str")" "$depth"
      return
    fi
    set_tok "$line"
    if [ "$depth" -lt 3 ]; then
      # A quoted $(...) or `...` runs on its own, outside any override on this command.
      mine=("${TOK[@]}")
      for t in "${mine[@]}"; do
        case "$t" in
          *'$('*) inner=${t#*'$('}; scan_git_string "${inner%)*}" $((depth + 1)) ;;
          *"$bt"*) inner=${t#*"$bt"}; scan_git_string "${inner%"$bt"*}" $((depth + 1)) ;;
        esac
      done
      TOK=("${mine[@]}")
    fi
    seg_auth ZS_ALLOW_GIT=1 && continue
    cmd_index
    [ "$CI" -ge "${#TOK[@]}" ] && continue
    base=${TOK[$CI]##*/}
    segtext=${line//$SEP/ }
    case "$base" in
      [gG][iI][tT]|[gG][hH])
        # a git or gh command: judge its words, not the free text of a quoted message or body
        segtext=""
        for t in "${TOK[@]}"; do
          case "$t" in *[[:space:]]*) : ;; *) segtext="${segtext}${segtext:+ }${t}" ;; esac
        done ;;
    esac
    if printf '%s' "$segtext" | grep -qE "${GIT_GREP_ARGS[@]}"; then
      for p in "${GIT_DESTRUCTIVE_PATTERNS[@]}"; do
        if printf '%s' "$segtext" | grep -qE "$p"; then
          BLOCK_REASON="BLOCKED: '$COMMAND' matches git-destructive pattern '$p'.
To proceed: run git status, stash any changes that are not yours, then prefix that command with ZS_ALLOW_GIT=1 (as a leading assignment: ZS_ALLOW_GIT=1 git ...)."
          emit_block
        fi
      done
    fi
    case "$base" in
      [gG][iI][tT]) git_check $((CI + 1)) ;;
      bash|sh|zsh|dash|ksh|fish)
        if [ "$depth" -lt 3 ]; then
          k=$((CI + 1))
          while [ "$k" -lt "${#TOK[@]}" ]; do
            case "${TOK[$k]}" in
              --rcfile|--init-file|-o|+o|-O|+O) k=$((k + 2)) ;;
              --*|+*) k=$((k + 1)) ;;
              -*c*) scan_git_string "${TOK[$((k + 1))]:-}" $((depth + 1)); break ;;
              -*) k=$((k + 1)) ;;
              *) break ;;
            esac
          done
        fi
        ;;
      eval)
        if [ "$depth" -lt 3 ]; then
          p=${line#*eval$SEP}; scan_git_string "${p//$SEP/ }" $((depth + 1))
        fi
        ;;
    esac
  done < <(split_segments "$str")
}

# One grep per category instead of one per pattern (perf, 2026-10-13; the Claude
# hook has done this since 2026-07-31). `grep -e p1 -e p2 ...` matches iff some
# single pattern matches, with the same engine and flags, so it is an exact
# pre-filter: a safe command skips the loops, and a match still runs the loop
# so the BLOCKED message names the pattern. Scans are case-insensitive for the
# absolute and op patterns (APFS is case-insensitive); git stays case-sensitive
# because git flags are case-semantic (-d versus -D).
any_match() {
  local args=() p
  for p in "$@"; do args+=(-e "$p"); done
  printf '%s' "$COMMAND" | grep -qiE "${args[@]}"
}

match_policy() {
  local pattern CMD_OP

  # dd onto a device. /dev/null, /dev/stdout and /dev/stderr are harmless sinks, so
  # they are blanked out before the match (`dd ... of=/dev/null` is a benchmark idiom).
  if [[ $COMMAND == *[dD][dD]* ]] && printf '%s' "$COMMAND" \
     | sed -E 's#of=/dev/(null|stdout|stderr)([^[:alnum:]_/-]|$)# #g' \
     | grep -qiE "(^|[^[:alnum:]_.-])dd[[:space:]]+.*of=/dev/"; then
    BLOCK_REASON="BLOCKED: '$COMMAND' writes to a device with dd. The user has prevented you from doing this."
    return 1
  fi

  # One gate over every category (Argon review): the interpreter rule also needs
  # a protected path, so a command that matches none of these cannot be blocked.
  any_match "${DANGEROUS_PATTERNS[@]}" "$PROTECTED_SECRET_PATH_PATTERN" \
    "${GIT_DESTRUCTIVE_PATTERNS[@]}" "${OP_PATTERNS[@]}" \
    '(^|[^[:alnum:]_.-])git([^[:alnum:]_.-]|$)' || return 0

  any_match "${DANGEROUS_PATTERNS[@]}" && for pattern in "${DANGEROUS_PATTERNS[@]}"; do
    if printf '%s' "$COMMAND" | grep -qiE "$pattern"; then
      BLOCK_REASON="BLOCKED: '$COMMAND' matches dangerous pattern '$pattern'. The user has prevented you from doing this."
      return 1
    fi
  done

  # Interpreters are blocked only when the same command also names a protected
  # secret path. Normal Python, Node, Ruby, and similar work remains allowed.
  # The rare protected-path test runs first so ordinary interpreter use costs one grep.
  if printf '%s' "$COMMAND" | grep -qiE "$PROTECTED_SECRET_PATH_PATTERN" \
     && printf '%s' "$COMMAND" | grep -qiE "$INTERPRETER_COMMAND_PATTERN"; then
    BLOCK_REASON="BLOCKED: '$COMMAND' matches interpreter pattern '$INTERPRETER_COMMAND_PATTERN' and protected-secret-path pattern '$PROTECTED_SECRET_PATH_PATTERN'. Use ZS Vault metadata/access paths instead of reading credential files."
    return 1
  fi

  # Destructive git is default-deny unless the SAME simple command starts with the
  # env assignment ZS_ALLOW_GIT=1 (agents may self-apply it after `git status` and
  # stashing work that is not theirs, 2026-09-22; anchored per command 2026-10-27).
  # The token in a comment, an echo, an argument or another command of the chain
  # does nothing. Absolute patterns still win above.
  # The gate keeps ordinary git (status, log, diff, commit) off the tokenizer: only a
  # command that names a word the rules look at is split and analysed.
  if { [[ $COMMAND =~ (^|[^[:alnum:]_.-])[gG][iI][tT]([^[:alnum:]_.-]|$) ]] \
       && [[ $COMMAND =~ (clean|checkout|restore|reset|push|ZS_ALLOW_GIT) ]]; } \
     || printf '%s' "$COMMAND" | grep -qE "${GIT_GREP_ARGS[@]}"; then
    scan_git_string "$COMMAND" 0
  fi

  # 1Password CLI is default-deny unless the same simple command starts with the
  # human-authorized ZS_ALLOW_OP=1 assignment. ZS Vault remains the secrets source.
  CMD_OP="$COMMAND"
  if printf '%s' "$COMMAND" | grep -qE 'ZS_ALLOW_OP=1([[:space:]]|$)'; then
    CMD_OP=$(without_authorized ZS_ALLOW_OP=1)
  fi
  if [ -n "$CMD_OP" ] && printf '%s' "$CMD_OP" | grep -qiE -e "${OP_PATTERNS[0]}" -e "${OP_PATTERNS[1]}" -e "${OP_PATTERNS[2]}"; then
    for pattern in "${OP_PATTERNS[@]}"; do
      if printf '%s' "$CMD_OP" | grep -qiE "$pattern"; then
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
