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

# 2026-10-27 (hardening pass 2): the whole ~/.zsvault directory is secret; the command-position
# rules (sudo, op, an interpreter) read a quote-masked copy (mask_cmd), so a separator inside
# a quoted argument starts no command; the gh merge gate and the environment-dump rule are
# ported from the Claude hook with the same override tokens (ZS_ALLOW_GIT=1, ZS_ALLOW_ENV=1).
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
  SECRET_DIRS='\.ssh|\.aws|\.zsvault'
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
# The script of `env -S` / `--split-string` is a command too, quoted, joined or bare:
# `env -S 'sudo id'`, `env -S'sudo id'`, `env --split-string='sudo id'`, `env -iS 'sudo id'`
# (2026-10-28). That env must itself sit in command position, and -S counts only among its own
# options and assignments, so `git commit -m 'env -S sudo'` and `env printf x -S sudo` stay free.
CMD_WRAPS="((command|builtin|exec|nohup|nice|time|xargs|env|doas)(([[:space:]]+-[^[:space:]]+)([[:space:]]+[^-[:space:]][^[:space:]]*)?)*[[:space:]]+|[A-Za-z_][A-Za-z0-9_]*=[^[:space:]]*[[:space:]]+)*"
ENV_SPLIT_POS="(^|[;&|()\`{!]|(-[a-zA-Z]*c|eval)[[:space:]]+[\"'])[[:space:]]*((command|builtin|exec|nohup|nice|time|doas)[[:space:]]+|[A-Za-z_][A-Za-z0-9_]*=[^[:space:]]*[[:space:]]+)*([^[:space:];&|()]*/)?env([[:space:]]+(-[uPC][[:space:]]+[^-[:space:];&|][^[:space:];&|]*|-[^-[:space:];&|][^[:space:];&|]*|--[^[:space:];&|]+|[A-Za-z_][A-Za-z0-9_]*=[^[:space:];&|]*))*[[:space:]]+(-[a-zA-Z]*S|--split-string)(=|[[:space:]]*)[\"']?"
CMD_POS="(^|[;&|()\`{!]|(-[a-zA-Z]*c|eval)[[:space:]]+[\"']|${ENV_SPLIT_POS}|(^|[;&|()\`{])[[:space:]]*(then|else|do|elif|if|while|until))[[:space:]]*"
SHELL_WORDS="(bash|sh|zsh|fish|dash|ksh)"

# ── quote-masked copy of the command, for the command-position anchors (2026-10-27) ──
# The anchored rules below (sudo, op, the environment dumps, an interpreter in command
# position) read `; | & ( ) { ! newline` as the start of a command. Inside the quoted
# DATA argument of a command that only prints or matches text, those characters start
# nothing: `git commit -m 'document (sudo path) detection'`, `grep -E 'ENV|env' f`,
# `echo 'Set $API_KEY'`. In this copy they are replaced by `_` (and `$` in a single-quoted
# echo/printf argument, and `\$` in an echo/printf argument, which the shell never expands).
# Only these strings are touched: the message of `git -m`, a `gh` body or title, any
# argument of echo, printf, grep, rg, jq and curl. Everything else stays as typed: the
# script of `-c`, eval, ssh, su, xargs, watch and `env -S`, `$(...)`, backticks, a double-
# quoted string that holds `$(` or a backtick (and everything after it), a here-doc (the
# copy is skipped for any line with `<<`), and every command that has such an indirect
# runner anywhere in it. An unbalanced quote leaves the rest as typed.
# Lexical, not a shell. The raw command is still what every other rule reads.
AWK_MASK='function endword() { if (inw) { if (isexec(wbase(cur))) risky = 1; nw++; wd[nw] = cur; cur = ""; inw = 0 } }
function inert(b) {
  return (b ~ /^(cat|head|tail|wc|sort|uniq|grep|egrep|fgrep|rg|jq|cut|tr|nl|column|fold|rev|xxd|base64|od|diff|cmp|pbcopy|tac|paste)$/)
}
function segend(   ci, b, i, pos) {
  if (nw > 0) {
    if (pipedin) {
      ci = cmdidx()
      if (ci == 0) risky = 1
      else {
        b = wbase(wd[ci])
        if (!inert(b)) risky = 1
        else if (b == "sort") { for (i = ci + 1; i <= nw; i++) if (wd[i] ~ /^(-[A-Za-z]*o|--output)/) risky = 1 }
        else if (b == "base64") { for (i = ci + 1; i <= nw; i++) if (wd[i] ~ /^(-[A-Za-z]*o|--output)/) risky = 1 }
        else if (b == "uniq" || b == "xxd") {
          pos = 0
          for (i = ci + 1; i <= nw; i++) if (wd[i] !~ /^-./) pos++
          if (pos > 1) risky = 1
        }
      }
    }
    pipedin = 0
  }
}
function wbase(x,   n, a) { n = split(x, a, "/"); return tolower(a[n]) }
function isexec(b) {
  return (b ~ /^(eval|source|xargs|exec|sh|bash|zsh|dash|ksh|fish|ssh|su|watch|parallel|osascript|function|alias|trap)$/ || b == "." || b ~ /^-S/ || b == "--split-string")
}
function cmdidx(   i, x, envw) {
  for (i = 1; i <= nw; i++) {
    x = wd[i]
    if (x ~ /^[A-Za-z_][A-Za-z0-9_]*=/) continue
    x = wbase(x)
    if (x ~ /^(command|builtin|nohup|time|noglob|then|else|do|elif|if|while|until|[{]|!)$/) continue
    # env and its flags sit in front of the real command (`env LC_ALL=C printf …`); -S is risky above
    if (x == "env") { envw = 1; continue }
    if (envw && wd[i] ~ /^(-[A-Za-z]*S|--split-string)/) risky = 1
    if (envw && wd[i] ~ /^-[A-Za-z]*[uPC]$/) { i++; continue }
    if (envw && x ~ /^-/) continue
    return i
  }
  return 0
}
# 0 = leave the string alone; 1 = argument that is plain data; 2 = echo/printf argument;
# 3 = message argument of git or gh
function kind(   ci, c, p, i, sk, subc) {
  ci = cmdidx()
  if (ci == 0) return 0
  c = wbase(wd[ci])
  if (c == "echo") return 2
  if (c == "printf") {
    for (i = ci + 1; i <= nw; i++) if (wd[i] ~ /^-v/) return 0
    return 2
  }
  if (c ~ /^(grep|egrep|fgrep|jq|curl)$/) return 1
  p = (cur == "") ? wd[nw] : cur
  if (c == "rg") return (p ~ /^--(pre|hostname-bin)(=.*)?$/) ? 0 : 1
  if (nw <= ci && cur == "") return 0
  if (c == "git") {
    subc = ""; sk = 0
    for (i = ci + 1; i <= nw; i++) {
      if (sk) { sk = 0; continue }
      if (wd[i] == "-C") { sk = 1; continue }
      if (wd[i] ~ /^(-c|--config-env)/) return 0
      if (wd[i] !~ /^-/) { subc = wd[i]; break }
    }
    if (subc !~ /^(commit|tag|merge|notes|revert|cherry-pick|stash|log)$/) return 0
  }
  if (c == "gh") for (i = ci + 1; i <= nw; i++) if (wd[i] ~ /^(alias|extension)$/) return 0
  if (c == "git") return (p ~ /^(-[A-Za-z]*m|--message|--grep|--author|--committer)$/ || p ~ /^(--message|--grep|--author|--committer)=$/ || (cur != "" && cur ~ /^-[A-Za-z]*m$/)) ? 3 : 0
  if (c == "gh") return (p ~ /^(-b|-t|-n|--body|--title|--notes|--comment)$/ || p ~ /^(--body|--title|--notes)=$/) ? 3 : 0
  return 0
}
function maskstr(str, k, q,   out, i, n, ch, d) {
  n = length(str); out = ""
  for (i = 1; i <= n; i++) {
    ch = substr(str, i, 1)
    if (q == "\"" && ch == "\\") {
      d = substr(str, i + 1, 1); i++
      if (d == "$" && k == 2) d = "_"
      else if (d != "" && index(";&|(){}!`<>\n\r", d) > 0) d = (d == "\n" || d == "\r") ? " " : "_"
      out = out ch d
      continue
    }
    if (q == sq && ch == "$" && k == 2) ch = "_"
    else if (index(";&|(){}!`<>\n\r", ch) > 0) ch = (ch == "\n" || ch == "\r") ? " " : "_"
    out = out ch
  }
  return out
}
BEGIN { RS = "\001"; sq = "\047"; dq = "\042"; bt = "\140" }
{
  s = $0; n = length(s); out = ""; nw = 0; cur = ""; inw = 0; risky = 0; taint = 0; pipedin = 0
  for (i = 1; i <= n; i++) {
    c = substr(s, i, 1)
    if (c == "\\") {
      d = substr(s, i + 1, 1)
      out = out c d; cur = cur d; inw = 1; i++
      continue
    }
    if ((c == "$" && substr(s, i + 1, 1) == "(") || c == bt) taint = 1
    if (c == "$" && substr(s, i + 1, 1) == sq) {
      j = i + 2
      while (j <= n) { e = substr(s, j, 1); if (e == "\\") { j += 2; continue } if (e == sq) break; j++ }
      if (j > n) { out = out substr(s, i); break }
      out = out substr(s, i, j - i + 1); cur = cur "$"; inw = 1; risky = 1; i = j
      continue
    }
    if (c == sq || c == dq) {
      j = i + 1; closed = 0
      while (j <= n) {
        e = substr(s, j, 1)
        if (c == dq && e == "\\") { j += 2; continue }
        if (e == c) { closed = 1; break }
        j++
      }
      if (!closed) { out = out substr(s, i); break }
      body = substr(s, i + 1, j - i - 1)
      rawbody = body
      if (c == dq && (index(body, "$(") > 0 || index(body, bt) > 0)) taint = 1
      k = taint ? 0 : kind()
      if (k > 0) body = maskstr(body, k, c)
      out = out c body c; cur = cur rawbody; inw = 1; i = j
      continue
    }
    if (c == "#" && !inw) {
      j = index(substr(s, i), "\n")
      if (j == 0) { out = out substr(s, i); break }
      out = out substr(s, i, j - 1); i = i + j - 2
      continue
    }
    if (c == "(") { t = substr(s, i + 1, 200); if (t ~ /^[ \t]*\)/) risky = 1 }
    if (c == "<" && substr(s, i + 1, 1) == "(") risky = 1
    if (c == ">") {
      r = substr(s, i + 1, 24)
      if (substr(r, 1, 1) == "(") risky = 1
      else if (r !~ /^[ \t]*\/dev\/null([ \t;&|)]|$)/ && r !~ /^&[12]([ \t;&|)]|$)/) risky = 1
    }
    if (index(";&|()\n" bt, c) > 0) {
      endword(); segend(); nw = 0; out = out c
      if (c == "|" && substr(s, i + 1, 1) != "|" && (i == 1 || substr(s, i - 1, 1) != "|")) pipedin = 1
      continue
    }
    if (c == " " || c == "\t") { endword(); out = out c; continue }
    cur = cur c; inw = 1; out = out c
  }
  endword(); segend()
  printf "%s", (risky ? s : out)
}'
# ── here-doc bodies that are only text (2026-10-28) ──
# A here-doc fed to a command that only reads or stores text (`cat > notes.md <<'EOF'`,
# `git commit -m "$(cat <<'EOF' …)"`) runs nothing, so a body line that starts with `sudo`
# starts no command. This copy drops such bodies, the operator and the end word. All or
# nothing: the command is left as typed when any here-doc feeds another command, a body with
# an unquoted end word holds `$(` or a backtick, a here-string or unended body appears, or
# what is left runs a shell, an interpreter, eval/source/xargs/ssh or a path-named program
# (which could run a file the here-doc just wrote). Lexical, like the mask below.
AWK_HEREDOC='function sink(before,   i, c, n, w, a, b) {
  for (i = length(before); i > 0; i--) { c = substr(before, i, 1); if (index(";&|(" bt, c) > 0) break }
  n = split(substr(before, i + 1), w, /[ \t]+/)
  for (i = 1; i <= n; i++) {
    if (w[i] == "" || w[i] ~ /^[A-Za-z_][A-Za-z0-9_]*=/ || w[i] ~ /^(command|builtin|nohup|time|noglob|!|[{])$/) continue
    b = w[i]; sub(/.*\//, "", b)
    if (b == "git") return msgfile(w, i, n, "^(commit|tag|notes)$", "^(-[A-Za-z]*F|--file(=.*)?)$")
    if (b == "gh") return msgfile(w, i, n, "^(pr|issue|release|api)$", "^(-F|--body-file(=.*)?|--notes-file(=.*)?|--input)$")
    return (b ~ /^(cat|tee|head|tail|wc|sort|uniq|grep|egrep|fgrep|rg|jq|cut|tr|nl|column|fold|rev|diff|cmp|pbcopy|base64|od|xxd)$/)
  }
  return 0
}
# git commit/tag/notes -F - and gh --body-file - read the body as a message; git -c can define
# an alias that runs a shell, so it never counts
function msgfile(w, ci, n, subre, flagre,   i, sub1, fl) {
  sub1 = ""; fl = 0
  for (i = ci + 1; i <= n; i++) {
    if (w[i] ~ /^(-c|--config-env|--exec-path)/) return 0
    if (sub1 == "" && w[i] !~ /^-/ && w[i - 1] != "-C" && w[i - 1] != "-R") sub1 = w[i]
    if (w[i] ~ flagre) fl = 1
  }
  return (sub1 ~ subre && fl)
}
BEGIN { RS = "\001"; sq = "\047"; dq = "\042"; bt = "\140" }
{
  s = $0; n = split(s, L, "\n"); out = ""; ok = 1; qh = 1; nq = 0; st = ""; sp = 0
  for (li = 1; li <= n; li++) {
    line = L[li]
    if (qh <= nq) {
      t = line; if (QT[qh]) sub(/^\t+/, "", t)
      if (t == QD[qh]) { qh++; if (qh > nq) { qh = 1; nq = 0 } }
      else if (!QQ[qh] && (index(line, "$(") > 0 || index(line, bt) > 0 || index(line, "\\") > 0)) ok = 0
      out = out (li > 1 ? "\n" : "")
      continue
    }
    # one scan across lines keeps quote and $( state, so an operator inside a string, a
    # comment or a continued line is never read as one; escapes, ANSI-C quotes and backticks decline
    o = ""; seg = ""; m = length(line)
    for (i = 1; i <= m; i++) {
      c = substr(line, i, 1)
      if (st == sq) { if (c == sq) st = ""; o = o c; seg = seg c; continue }
      if (c == "\\" || c == bt) { ok = 0; o = o c; seg = seg c; continue }
      if (c == "(" && substr(line, i + 1, 1) == "(") ok = 0
      if (c == "$" && substr(line, i + 1, 1) == "{") ok = 0
      if (c == "$" && substr(line, i + 1, 1) == "(") { if (substr(line, i + 2, 1) == "(") ok = 0; STK[++sp] = st; st = ""; o = o "$("; seg = ""; i++; continue }
      if (st == dq) { if (c == dq) st = ""; o = o c; seg = seg c; continue }
      if (c == sq) { if (i > 1 && substr(line, i - 1, 1) == "$") ok = 0; st = sq }
      else if (c == dq) st = dq
      else if (c == "#") ok = 0
      else if (c == ")" && sp > 0) { st = STK[sp--]; o = o c; seg = ""; continue }
      else if (c == "<" && substr(line, i + 1, 1) == "<") {
        r = substr(line, i)
        if (substr(r, 3, 1) == "<" || !match(r, /^<<-?[ \t]*(\\?[A-Za-z0-9_.-]+|\047[A-Za-z0-9_.-]+\047|"[A-Za-z0-9_.-]+")/)) { ok = 0; o = o c; continue }
        nx = substr(r, RLENGTH + 1, 1)
        if (nx != "" && nx !~ /[ \t;&|)<>]/) ok = 0
        d = substr(r, 3, RLENGTH - 2); dash = (substr(d, 1, 1) == "-"); if (dash) d = substr(d, 2)
        sub(/^[ \t]+/, "", d)
        q = (substr(d, 1, 1) == sq || substr(d, 1, 1) == dq || substr(d, 1, 1) == "\\")
        gsub(/[\047"\\]/, "", d)
        if (!sink(seg)) ok = 0
        nq++; QD[nq] = d; QT[nq] = dash; QQ[nq] = q
        i += RLENGTH - 1
        continue
      }
      else if (index(";&|(", c) > 0) { o = o c; seg = ""; continue }
      o = o c; seg = seg c
    }
    if (line ~ /\\$/) ok = 0
    # a body starts on the next line, so the operator line must not end inside a quote
    if (qh <= nq && st != "") ok = 0
    out = out (li > 1 ? "\n" : "") o
  }
  if (nq > 0 || st != "" || sp > 0) ok = 0
  if (out ~ /(^|[^[:alnum:]_.-])(bash|sh|zsh|dash|ksh|fish|source|eval|exec|xargs|ssh|su|watch|parallel|osascript|python[0-9.]*|node|perl|ruby|deno|bun|php)([^[:alnum:]_.-]|$)/) ok = 0
  # what is left runs a path-named program, a $-expanded command word or the output of a substitution
  if (out ~ /(^|[;&|(`{!\n])[ \t]*(([A-Za-z_][A-Za-z0-9_]*=[^ \t]*|command|builtin|nohup|time|noglob|then|do|else)[ \t]+)*["\047]?([.~$]|[^ \t;&|()"\047]*\/)/) ok = 0
  if (out ~ /[A-Za-z_][A-Za-z0-9_]*=["\047]?\$\(/) ok = 0
  printf "%s", (ok ? out : s)
}'
mask_cmd() {  # mask_cmd <command>: the masked copy, or the command itself when nothing applies
  local m
  case "$1" in
    *'<<'*)
      m=$(printf '%s' "$1" | awk "$AWK_HEREDOC" 2>/dev/null) || m=""
      case "$m" in ''|*'<<'*) printf '%s' "$1"; return ;; esac
      set -- "$m"
      case "$1" in *[\'\"]*) : ;; *) printf '%s' "$1"; return ;; esac ;;
    *[\'\"]*) : ;;
    *) printf '%s' "$1"; return ;;
  esac
  if m=$(printf '%s' "$1" | awk "$AWK_MASK" 2>/dev/null) && [ -n "$m" ]; then
    printf '%s' "$m"
  else
    printf '%s' "$1"
  fi
}

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
  # (sudo is command-position anchored: ANCHORED_PATTERNS below, scanned on the masked copy)
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

# Command-position patterns run on the quote-masked copy (see mask_cmd).
ANCHORED_PATTERNS=(
  "${CMD_POS}${CMD_WRAPS}([^[:space:]]*/)?sudo([[:space:]]|\$)"
)
# The whole ~/.zsvault directory is secret (2026-10-27): every file and subdirectory,
# tmp/ included, in every spelling (~, $HOME, ${HOME}, the absolute path). No override.
# A command line that names it is refused, a quoted message included (the raw command is
# read, not the masked copy). The zsvault CLI is called by name and stays allowed.
VAULT_DIR_PATTERN="(^|[^[:alnum:]_.-])\\.zsvault([^[:alnum:]_.-]|\$)"

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
  # gh pr merge: the literal form; the flag-order forms and the merge API are judged
  # token-wise in gh_check (same rules as the Claude hook). zs-land stays the merge path.
  "gh[[:space:]]+pr[[:space:]]+merge"
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
  CI=0 CW=""
  local t w="" n=${#TOK[@]} k tmo=0
  while [ "$CI" -lt "$n" ]; do
    t=${TOK[$CI]}
    case "$t" in
      [A-Za-z_]*=*) CI=$((CI + 1)) ;;
      timeout|gtimeout|*/timeout|*/gtimeout) w=timeout; tmo=1; CI=$((CI + 1)) ;;
      env|command|builtin|exec|nohup|time|nice|noglob|rtk|xargs|npx|bunx|uvx|doas|*/env|*/command|*/builtin|*/exec|*/nohup|*/time|*/nice|*/rtk|*/xargs|*/npx|*/bunx|*/uvx|*/doas) w=${t##*/}; tmo=0; CI=$((CI + 1)) ;;
      then|else|do|elif|if|while|until|'{'|'!') w=""; CI=$((CI + 1)) ;;
      mise)
        if [ "${TOK[$((CI + 1))]:-}" = exec ] || [ "${TOK[$((CI + 1))]:-}" = x ]; then
          k=$((CI + 2))
          while [ "$k" -lt "$n" ] && [ "${TOK[$k]}" != "--" ]; do k=$((k + 1)); done
          if [ "$k" -lt "$n" ]; then CI=$((k + 1)); else CI=$((CI + 2)); fi
        else break; fi ;;
      uv) if [ "${TOK[$((CI + 1))]:-}" = run ]; then w=uv; CI=$((CI + 2)); else break; fi ;;
      pnpm|npm|yarn) case "${TOK[$((CI + 1))]:-}" in exec|dlx) w=$t; CI=$((CI + 2)) ;; *) break ;; esac ;;
      --) CI=$((CI + 1)) ;;
      -*)
        case "$w:$t" in
          env:-u|env:-C|env:-S|nice:-n|xargs:-[nIPLsdE]|exec:-a|doas:-u|timeout:-s|timeout:-k|timeout:--signal|timeout:--kill-after|uv:--project|uv:--directory|uv:--with|uv:-p|uv:--python|uv:--package) CI=$((CI + 2)) ;;
          *) CI=$((CI + 1)) ;;
        esac ;;
      *)
        # the duration of `timeout 5 cmd`
        if [ "$tmo" -eq 1 ]; then tmo=0; CI=$((CI + 1)); else break; fi ;;
    esac
  done
  CW=$w
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

gh_check() {  # gh_check <index of the word after "gh">
  local j=$1 n=${#TOK[@]} t s1 s2 a k method="" fields=0 endpoint=0 gql=0
  while [ "$j" -lt "$n" ]; do
    t=${TOK[$j]}
    case "$t" in
      -R|--repo|--hostname) j=$((j + 2)) ;;
      -*) j=$((j + 1)) ;;
      *) break ;;
    esac
  done
  [ "$j" -ge "$n" ] && return 0
  s1=${TOK[$j]}; s2=${TOK[$((j + 1))]:-}
  if [ "$s1" = "pr" ]; then
    k=$((j + 1))  # flags may sit between pr and merge: gh pr -R o/r merge 3
    while [ "$k" -lt "$n" ]; do
      case "${TOK[$k]}" in
        -R|--repo|--hostname) k=$((k + 2)) ;;
        -*) k=$((k + 1)) ;;
        *) break ;;
      esac
    done
    s2=${TOK[$k]:-}
  fi
  if [ "$s1" = "pr" ] && [ "$s2" = "merge" ]; then
    git_deny "gh pr merge; zs-land is the sanctioned merge path"
  fi
  if [ "$s1" = "api" ]; then
    j=$((j + 1))
    while [ "$j" -lt "$n" ]; do
      a=${TOK[$j]}; j=$((j + 1))
      case "$a" in
        -X|--method) method=${TOK[$j]:-}; j=$((j + 1)) ;;
        -X*) method=${a#-X} ;;
        --method=*) method=${a#--method=} ;;
        -f|-F|--field|--raw-field|--input)
          fields=1
          case "${TOK[$j]:-}" in *mergePullRequest*|*enablePullRequestAutoMerge*) gql=1 ;; esac
          j=$((j + 1)) ;;
        -f*|-F*|--field=*|--raw-field=*|--input=*) fields=1 ;;
      esac
      case "$a" in
        */pulls/[0-9]*/merge|pulls/[0-9]*/merge|*/pulls/[0-9]*/merge\?*|pulls/[0-9]*/merge\?*) endpoint=1 ;;
      esac
      case "$a" in *mergePullRequest*|*enablePullRequestAutoMerge*) gql=1 ;; esac
    done
    method=$(printf '%s' "$method" | tr '[:lower:]' '[:upper:]')
    if [ "$gql" -eq 1 ]; then
      git_deny "gh api GraphQL merge mutation"
    fi
    if [ "$endpoint" -eq 1 ] && { { [ -n "$method" ] && [ "$method" != "GET" ]; } || { [ -z "$method" ] && [ "$fields" -eq 1 ]; }; }; then
      git_deny "gh api pull-request merge endpoint; zs-land is the sanctioned merge path"
    fi
  fi
  return 0
}


# ── environment dumps (added 2026-10-23, widened 2026-10-27) ──────────────
# Every shell and launchd job carries the exported API keys and proxy tokens,
# so a whole-environment dump prints them into the transcript. On 2026-10-22 a
# process-env grep printed a trainer passcode and a subagent printed the agy
# proxy token. Blocked: env/printenv (also path-qualified and with flags or
# assignments, e.g. /usr/bin/env, env -0), printenv of a secret-looking name,
# `export` / `export -p`, `declare -x`, echo/printf of a $…KEY / $…TOKEN /
# $…SECRET variable, ps with the environment flag (BSD `e` or -E, matched
# case-SENSITIVELY so the common `ps -e`/`ps -ef` stay allowed), and launchctl
# print/getenv/export. A python `os.environ` print is not covered.
# The same words are caught behind wrappers and inside nested scripts (scan_command_string
# below): `bash -c env`, `sh -c "printenv FOO_TOKEN"`, `zsh -lc env`, `eval env`,
# `mise exec -- env`, `uv run env`, `pnpm exec env`, `npm exec env`, `timeout 5 env`.
# Allowed, because they print nothing: a secret variable piped straight into a stdin
# consumer (`printf '%s' "$NEW_SECRET" | zsvault add k --value-stdin`, `gh secret set`,
# `<tool> login --password-stdin`) and `printenv NAME` with stdout sent to /dev/null.
# Anything that tees, pipes further or writes to a file stays blocked.
# Check a variable is set with `[ -n "$VAR" ] && echo set`; check a job with
# `launchctl list | grep <label>`. Override only when the user asked:
#   ZS_ALLOW_ENV=1 <command>
ENV_DUMP_PATTERNS=(
  "(^|[;&|(])[[:space:]]*((command|builtin|exec|nohup|time)[[:space:]]+|[A-Za-z_][A-Za-z0-9_]*=[^[:space:]]*[[:space:]]+)*([^[:space:];&|()]*/)?(env|printenv)([[:space:]]+(-[uU][[:space:]]+[A-Za-z_][A-Za-z0-9_]*|-[A-Za-z0-9-]+|[A-Za-z_][A-Za-z0-9_]*=[^[:space:];&|()]*))*([[:space:]]+#|[[:space:]]*(\$|[|;&)>]))"
  "(^|[;&|(])[[:space:]]*((command|builtin|exec|nohup|time)[[:space:]]+|[A-Za-z_][A-Za-z0-9_]*=[^[:space:]]*[[:space:]]+)*([^[:space:];&|()]*/)?printenv([[:space:]]+[^;&|<>[:space:]]+)*[[:space:]]+[\"']?[A-Za-z0-9_]*(KEY|TOKEN|SECRET|PASSWORD|PASSCODE|CREDENTIAL)"
  "(^|[;&|(])[[:space:]]*([^[:space:];&|()]*/)?launchctl[[:space:]]+(print|getenv|export)([[:space:]]|\$)"
  "(^|[;&|(])[[:space:]]*export([[:space:]]+(-[A-Za-z]*p[A-Za-z]*|--))*([[:space:]]+#|[[:space:]]*(\$|[|;&)>]))"
  "(^|[;&|(])[[:space:]]*(declare|typeset)([[:space:]]+-[a-zA-Z]+)*[[:space:]]+-[a-zA-Z]*x[a-zA-Z]*([[:space:]]+(-[a-zA-Z]+|--))*([[:space:]]+#|[[:space:]]*(\$|[|;&)>]))"
)
# echo/printf of $API_KEY-style names: upper case only, so $key, $input_tokens and
# $cache_key stay free (real environment variables are upper case)
ECHO_ENV_PATTERN="(^|[;&|(])[[:space:]]*([^[:space:];&|()]*/)?(echo|printf)[[:space:]][^;&|]*\\\$\\{?[A-Za-z0-9_]*(KEY|TOKEN|SECRET|PASSWORD|PASSCODE|CREDENTIAL)([^:]|:[^+]|\$)"
# BSD form: the first argument has no dash and contains e (ps eww, ps auxe).
# Dash form: any -...E flag. `ps -o etime` and `ps -ef` stay allowed.
PS_ENV_PATTERN="(^|[;&|(])[[:space:]]*([^[:space:];&|()]*/)?ps[[:space:]]+([a-zA-Z]*e[a-zA-Z]*([[:space:]]|\$|[;&|])|([^;&|]*[[:space:]])?-[A-Za-z]*E)"
# Allowed credential consumers (see above). Built from pieces; each stops at ; & | < > so a
# tee, a second pipe or a file redirect leaves the construct unmatched, and so blocked.
ENV_SECVAR="\\\$\\{?[A-Za-z0-9_]*(KEY|TOKEN|SECRET|PASSWORD|PASSCODE|CREDENTIAL)"
ENV_SINK="((([^[:space:];&|<>]*/)?zsvault[[:space:]]+(add|set|edit)([[:space:]]+[^;&|<>]*)?[[:space:]]--(value|totp|fields)-stdin)|(gh[[:space:]]+(-R[[:space:]]+[^[:space:];&|<>]+[[:space:]]+)?secret[[:space:]]+set)|(([^[:space:];&|<>]*/)?(docker|podman|nerdctl|buildah|skopeo|helm|oras|crane|gh|npm|pnpm|yarn|vault|az|twine|zsvault)([[:space:]]+[a-z][a-z-]*){0,1}[[:space:]]+login[[:space:]]+([^;&|<>]*[[:space:]])?--password-stdin))([[:space:]]+[^;&|<>]*)?"
ENV_TRAIL="(([[:space:]]+(&>|[12]?>)[[:space:]]*/dev/null)|([[:space:]]+2>&1))*[[:space:]]*(\$|[;)]|&&|\\|\\|)"
ENV_ALLOW_ECHO="(^|[;&|(])[[:space:]]*([^[:space:];&|()]*/)?(echo|printf)[[:space:]]+[^;&|<>]*${ENV_SECVAR}[^;&|<>]*\\|[[:space:]]*${ENV_SINK}${ENV_TRAIL}"
ENV_ALLOW_PRINTENV="(^|[;&|(])[[:space:]]*printenv([[:space:]]+-[A-Za-z0-9-]+)*([[:space:]]+[\"']?[A-Za-z_][A-Za-z0-9_]*[\"']?)+[[:space:]]*(&>|1?>)[[:space:]]*/dev/null(([[:space:]]+2>&1)|([[:space:]]+2>[[:space:]]*/dev/null))?[[:space:]]*(\$|[;)]|&&|\\|\\|)"

env_deny() {
  BLOCK_REASON="BLOCKED: '$COMMAND' prints environment values, which hold API keys and tokens. Test a variable with [ -n \"\$VAR\" ] && echo set, or a job with launchctl list | grep <label>. Override only if the user asked: ZS_ALLOW_ENV=1 <command>"
  emit_block
}

# env_strip_allowed <text>: sets ENV_T to the text with each allowed consumer replaced by ':'
env_strip_allowed() {
  local re m pre term before after
  ENV_T="$1"
  for re in "$ENV_ALLOW_ECHO" "$ENV_ALLOW_PRINTENV"; do
    while [[ $ENV_T =~ $re ]]; do
      m=${BASH_REMATCH[0]}; pre=${BASH_REMATCH[1]}
      case "$m" in *'&&') term='&&' ;; *'||') term='||' ;; *';') term=';' ;; *')') term=')' ;; *) term='' ;; esac
      before=${ENV_T%%"$m"*}; after=${ENV_T#*"$m"}
      ENV_T="${before}${pre} : ${term}${after}"
    done
  done
}

# env_text_check <text> [noecho]: exits 2 when the text dumps environment values. The text is
# a command line as the caller wants it read (the masked copy, a nested script, or the words
# of a command behind wrappers; the last passes `noecho`, because it lost its quotes).
env_text_check() {
  local t="$1" pattern
  [ -n "$t" ] || return 0
  case "$t" in *-stdin*|*'secret set'*|*/dev/null*) env_strip_allowed "$t"; t=$ENV_T ;; esac
  # one grep for the common no-match path (the case-insensitive scan is a superset of the loop)
  printf '%s' "$t" | grep -qiE -e "${ENV_DUMP_PATTERNS[0]}" -e "${ENV_DUMP_PATTERNS[1]}" -e "${ENV_DUMP_PATTERNS[2]}" \
    -e "${ENV_DUMP_PATTERNS[3]}" -e "${ENV_DUMP_PATTERNS[4]}" -e "$ECHO_ENV_PATTERN" -e "$PS_ENV_PATTERN" || return 0
  for pattern in "${ENV_DUMP_PATTERNS[@]}"; do
    printf '%s' "$t" | grep -qiE "$pattern" && env_deny
  done
  [ "${2:-}" = noecho ] || { printf '%s' "$t" | grep -qE "$ECHO_ENV_PATTERN" && env_deny; }
  if printf '%s' "$t" | grep -qE "$PS_ENV_PATTERN"; then
    BLOCK_REASON="BLOCKED: '$COMMAND' runs ps with the environment flag (e or -E), which prints every process's API keys and tokens. Drop the e/-E flag. Override only if the user asked: ZS_ALLOW_ENV=1 <command>"
    emit_block
  fi
  return 0
}


# env_end_check: a simple command made only of wrappers and assignments. If the last wrapper
# is `env`, it prints the environment (`env`, `env -0`, `env FOO=1`, `mise exec -- env`,
# `timeout 5 env`); `env -S 'script'` runs a script, which is scanned instead.
env_end_check() {
  [ "$CW" = env ] || return 0
  local i n=${#TOK[@]} body="" has_body=0 before
  local -a saved=("${TOK[@]}")
  # `command -v env` asks where env is; it runs nothing
  for ((i = 0; i < n; i++)); do
    case "${TOK[$i]}" in -v|-V) case " ${TOK[*]} " in *" command "*|*"/command "*) return 0 ;; esac ;; esac
  done
  for ((i = 0; i < n; i++)); do
    case "${TOK[$i]}" in
      -S|--split-string) body=${TOK[$((i + 1))]:-}; has_body=1; break ;;
      --split-string=*) body=${TOK[$i]#--split-string=}; has_body=1; break ;;
      -S?*) body=${TOK[$i]#-S}; has_body=1; break ;;
    esac
  done
  if [ "$has_body" -eq 0 ]; then
    [ "$do_env" = 1 ] && env_deny
    return 0
  fi
  if [ "$depth" -lt 3 ]; then
    SCAN_GIT=$do_git SCAN_ENV=$do_env scan_command_string "$body" $((depth + 1))
    TOK=("${saved[@]}")
  fi
  return 0
}

# env_seg_check: the command word of a simple command behind wrappers, read as text.
env_seg_check() {
  [ "$CI" -gt 0 ] || return 0
  local IFS=' ' tail i
  # `command -v printenv` asks where printenv is; it runs nothing
  if [ "$CW" = command ]; then
    for ((i = 0; i < CI; i++)); do case "${TOK[$i]}" in -v|-V) return 0 ;; esac; done
  fi
  tail="${TOK[*]:$CI}"
  env_text_check "$tail" noecho
}

# subst_live <line>: succeeds when the line holds `$(` or a backtick outside single quotes
subst_live() {
  printf '%s' "$1" | awk 'BEGIN { RS = "\001"; sq = "\047"; dq = "\042"; bt = "\140" }
    { s = $0; n = length(s); q = ""; hit = 0
      for (i = 1; i <= n; i++) {
        c = substr(s, i, 1)
        if (q == sq) { if (c == sq) q = ""; continue }
        if (c == "\\") { i++; continue }
        if (q == dq) { if (c == dq) { q = ""; continue } }
        else if (c == sq || c == dq) { q = c; continue }
        if (c == bt || (c == "$" && substr(s, i + 1, 1) == "(")) { hit = 1; break }
      }
      exit hit ? 0 : 1 }'
}

# Scan one command string: old literal patterns on each simple command, then the
# flag-set checks for git and gh (SCAN_GIT), and the environment-dump words behind
# wrappers (SCAN_ENV). `sh -c "…"`, `eval "…"` and `env -S "…"` arguments are scanned
# too (3 levels), each as a command line of its own. Exits 2 on a hit.
scan_command_string() {  # scan_command_string <string> <depth>
  local str="$1" depth="$2" line segtext p k base t inner bt=$'`' do_git do_env
  local -a mine
  if [ "$depth" -gt 0 ] && [ "$SCAN_ENV" = 1 ]; then
    env_text_check "$(mask_cmd "$str")"
  fi
  while IFS= read -r line; do
    [ -z "$line" ] && continue
    if [ "$line" = "$UNBAL" ]; then
      scan_command_string "$(unquote "$str")" "$depth"
      return
    fi
    set_tok "$line"
    if [ "$depth" -lt 3 ] && subst_live "$str"; then
      # A quoted $(...) or `...` runs on its own, outside any override on this command.
      mine=("${TOK[@]}")
      for t in "${mine[@]}"; do
        case "$t" in *'$('*) inner=${t#*'$('}; scan_command_string "${inner%)*}" $((depth + 1)) ;; esac
        case "$t" in *"$bt"*) inner=${t#*"$bt"}; scan_command_string "${inner%"$bt"*}" $((depth + 1)) ;; esac
      done
      TOK=("${mine[@]}")
    fi
    do_git=$SCAN_GIT; do_env=$SCAN_ENV
    if [ "$do_git" = 1 ] && seg_auth ZS_ALLOW_GIT=1; then do_git=0; fi
    if [ "$do_env" = 1 ] && seg_auth ZS_ALLOW_ENV=1; then do_env=0; fi
    [ "$do_git" = 0 ] && [ "$do_env" = 0 ] && continue
    cmd_index
    if [ "$CI" -ge "${#TOK[@]}" ]; then
      env_end_check
      continue
    fi
    base=${TOK[$CI]##*/}
    [ "$do_env" = 1 ] && env_seg_check
    if [ "$do_git" = 1 ]; then
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
          # an agent CLI is given a brief that may quote the merge command; it runs under its own guard
          case "$p" in
            gh*merge*) case "$base" in codex|claude|agy|argon|kimi-k3|hermes|cursor-agent|grok|grok-subscription) continue ;; esac ;;
          esac
          if printf '%s' "$segtext" | grep -qE "$p"; then
            BLOCK_REASON="BLOCKED: '$COMMAND' matches git-destructive pattern '$p'.
To proceed: run git status, stash any changes that are not yours, then prefix that command with ZS_ALLOW_GIT=1 (as a leading assignment: ZS_ALLOW_GIT=1 git ...)."
            emit_block
          fi
        done
      fi
      case "$base" in
        [gG][iI][tT]) git_check $((CI + 1)) ;;
        [gG][hH]) gh_check $((CI + 1)) ;;
      esac
    fi
    case "$base" in
      bash|sh|zsh|dash|ksh|fish)
        if [ "$depth" -lt 3 ]; then
          k=$((CI + 1))
          while [ "$k" -lt "${#TOK[@]}" ]; do
            case "${TOK[$k]}" in
              --rcfile|--init-file|-o|+o|-O|+O) k=$((k + 2)) ;;
              --*|+*) k=$((k + 1)) ;;
              -*c*) SCAN_GIT=$do_git SCAN_ENV=$do_env scan_command_string "${TOK[$((k + 1))]:-}" $((depth + 1)); break ;;
              -*) k=$((k + 1)) ;;
              *) break ;;
            esac
          done
        fi
        ;;
      eval)
        if [ "$depth" -lt 3 ]; then
          p=${line#*eval$SEP}; SCAN_GIT=$do_git SCAN_ENV=$do_env scan_command_string "${p//$SEP/ }" $((depth + 1))
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
  local pattern CMD_OP CMD_M CMD_ENV
  CMD_M=$(mask_cmd "$COMMAND")

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
  any_match "${DANGEROUS_PATTERNS[@]}" "${ANCHORED_PATTERNS[@]}" "$VAULT_DIR_PATTERN" \
    "$PROTECTED_SECRET_PATH_PATTERN" "${GIT_DESTRUCTIVE_PATTERNS[@]}" "${OP_PATTERNS[@]}" \
    '\.zs' '(^|[^[:alnum:]_.-])(git|gh)([^[:alnum:]_.-]|$)' \
    '(^|[^[:alnum:]_.-])(env|printenv|launchctl|ps|export|declare|typeset)([^[:alnum:]_-]|$)' \
    '\$\{?[A-Za-z0-9_]*(KEY|TOKEN|SECRET|PASSWORD|PASSCODE|CREDENTIAL)' || return 0

  any_match "${DANGEROUS_PATTERNS[@]}" && for pattern in "${DANGEROUS_PATTERNS[@]}"; do
    if printf '%s' "$COMMAND" | grep -qiE "$pattern"; then
      BLOCK_REASON="BLOCKED: '$COMMAND' matches dangerous pattern '$pattern'. The user has prevented you from doing this."
      return 1
    fi
  done

  # Command-position patterns read the quote-masked copy.
  for pattern in "${ANCHORED_PATTERNS[@]}"; do
    if printf '%s' "$CMD_M" | grep -qiE "$pattern"; then
      BLOCK_REASON="BLOCKED: '$COMMAND' matches dangerous pattern '$pattern'. The user has prevented you from doing this."
      return 1
    fi
  done
  if printf '%s' "$COMMAND" | grep -qiE "$VAULT_DIR_PATTERN" \
     || printf '%s' "$COMMAND" | tr -d "'\"\\\\\n\r" | grep -qiE "$VAULT_DIR_PATTERN"; then
    BLOCK_REASON="BLOCKED: '$COMMAND' names the ZS Vault directory (~/.zsvault). Every file in it is secret. Use the zsvault CLI by name: zsvault get <id>, zsvault search <query>, zsvault add <id> --value-stdin."
    return 1
  fi

  # Interpreters are blocked only when the same command also names a protected
  # secret path. Normal Python, Node, Ruby, and similar work remains allowed.
  # The rare protected-path test runs first so ordinary interpreter use costs one grep.
  if printf '%s' "$COMMAND" | grep -qiE "$PROTECTED_SECRET_PATH_PATTERN" \
     && printf '%s' "$CMD_M" | grep -qiE "$INTERPRETER_COMMAND_PATTERN"; then
    BLOCK_REASON="BLOCKED: '$COMMAND' matches interpreter pattern '$INTERPRETER_COMMAND_PATTERN' and protected-secret-path pattern '$PROTECTED_SECRET_PATH_PATTERN'. Use ZS Vault metadata/access paths instead of reading credential files."
    return 1
  fi

  # Environment dumps: ZS_ALLOW_ENV=1 as a leading assignment of the same simple command
  # is the only override; see the comments above ENV_DUMP_PATTERNS.
  CMD_ENV="$COMMAND"
  if printf '%s' "$COMMAND" | grep -qE 'ZS_ALLOW_ENV=1([[:space:]]|$)'; then
    CMD_ENV=$(without_authorized ZS_ALLOW_ENV=1)
  fi
  if [ -n "$CMD_ENV" ]; then
    if [ "$CMD_ENV" = "$COMMAND" ]; then env_text_check "$CMD_M"; else env_text_check "$CMD_ENV"; fi
  fi

  # Destructive git and the gh merge are default-deny unless the SAME simple command starts
  # with the env assignment ZS_ALLOW_GIT=1 (agents may self-apply it after `git status` and
  # stashing work that is not theirs, 2026-09-22; anchored per command 2026-10-27).
  # The token in a comment, an echo, an argument or another command of the chain
  # does nothing. Absolute patterns still win above.
  # The gate keeps ordinary git (status, log, diff, commit) off the tokenizer: only a
  # command that names a word the rules look at is split and analysed.
  SCAN_GIT=0 SCAN_ENV=0
  if { [[ $COMMAND =~ (^|[^[:alnum:]_.-])[gG][iI][tT]([^[:alnum:]_.-]|$) ]] \
       && [[ $COMMAND =~ (clean|checkout|restore|reset|push|ZS_ALLOW_GIT) ]]; } \
     || [[ $COMMAND =~ (^|[^[:alnum:]_.-])[gG][hH]([^[:alnum:]_.-]|$) ]] \
     || printf '%s' "$COMMAND" | grep -qE "${GIT_GREP_ARGS[@]}"; then
    SCAN_GIT=1
  fi
  # The environment words behind a wrapper or inside a nested script need the walker; a
  # command with no env-ish word, or with nothing that runs another command, cannot hold one.
  if [ -n "$CMD_ENV" ] \
     && printf '%s' "$COMMAND" | grep -qiE '(^|[^[:alnum:]_.-])(env|printenv|launchctl|ps|echo|printf|export|declare|typeset)([^[:alnum:]_-]|$)' \
     && printf '%s' "$COMMAND" | grep -qiE '(^|[^[:alnum:]_.-])(bash|sh|zsh|dash|ksh|fish|eval|exec|mise|uv|pnpm|npm|yarn|timeout|gtimeout|xargs|npx|bunx|uvx|nice|nohup|command|builtin|time|doas|env)([^[:alnum:]_-]|$)|\$\(|`|#'; then
    SCAN_ENV=1
  fi
  if [ "$SCAN_GIT" = 1 ] || [ "$SCAN_ENV" = 1 ]; then
    scan_command_string "$COMMAND" 0
  fi

  # 1Password CLI is default-deny unless the same simple command starts with the
  # human-authorized ZS_ALLOW_OP=1 assignment. ZS Vault remains the secrets source.
  CMD_OP="$CMD_M"
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
