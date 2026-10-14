# Shared dangerous-command guard

`dangerous-command.sh` gives Claude, Codex, Cursor, Antigravity, and Hermes one lexical command policy. Each lane adapter validates its native JSON payload, extracts one command string, runs the shared matcher, and returns the response its host expects. Malformed payloads fail closed. The absolute tier has no override; destructive Git and 1Password CLI calls require their existing human-authorized tokens.

The guard depends on Bash 3.2 or newer, `jq`, and `grep`.

## Lane registration

Use the absolute path so the guard works from any project directory:

```text
/Users/zero-suminc./.config/zs-ops/guards/dangerous-command.sh
```

**Claude Code deliberately stays on its native hook** (`~/.claude/hooks/block-dangerous-commands.sh`): that hook carries the `ZS_AMEND_HEAD` amend-sha policy (decided C0, Claude-local by design), which this shared guard intentionally does not port. Do not point Claude at this script instead — the `claude` lane exists for parity testing. For reference, the registration would be, in `~/.claude/settings.json`:

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "Bash",
        "hooks": [
          {
            "type": "command",
            "command": "/Users/zero-suminc./.config/zs-ops/guards/dangerous-command.sh claude"
          }
        ]
      }
    ]
  }
}
```

Codex, in `~/.codex/hooks.json`:

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "Bash",
        "hooks": [
          {
            "type": "command",
            "command": "/Users/zero-suminc./.config/zs-ops/guards/dangerous-command.sh codex",
            "statusMessage": "Checking command policy"
          }
        ]
      }
    ]
  }
}
```

Open `/hooks` after adding or changing the Codex hook and trust the reviewed definition.

Cursor, in `~/.cursor/hooks.json`:

```json
{
  "version": 1,
  "hooks": {
    "beforeShellExecution": [
      {
        "command": "/Users/zero-suminc./.config/zs-ops/guards/dangerous-command.sh cursor",
        "failClosed": true
      }
    ]
  }
}
```

Antigravity global registration belongs in `~/.gemini/config/hooks.json` (the shared path used by the CLI backend). Do not keep the global guard in the legacy `~/.gemini/hooks.json`: Antigravity CLI 1.1.11 can list/load that legacy definition without dispatching it at the real tool boundary. Use `<workspace>/.agents/hooks.json` only for intentionally workspace-local hooks.

The same file also registers two Claude hooks in agy mode (2026-10-13): `~/.claude/hooks/filter-test-output.py antigravity` (PreToolUse `run_command`, rewrites plain test runs through agy's `overwrite`) and `~/.claude/hooks/check-done-claims.py antigravity` (Stop; Jev checks "done" claims and answers `{"decision": "continue"}` once per user turn). The Jev subagent router is not ported: agy subagents ran in 11 of 407 conversations and only ever chose Flash.

```json
{
  "zs-dangerous-command": {
    "PreToolUse": [
      {
        "matcher": "run_command",
        "hooks": [
          {
            "type": "command",
            "command": "/Users/zero-suminc./.config/zs-ops/guards/dangerous-command.sh antigravity",
            "timeout": 10
          }
        ]
      }
    ]
  }
}
```

Hermes, in `~/.hermes/config.yaml` and any profile config that replaces its hooks block:

```yaml
hooks:
  pre_tool_call:
    - matcher: "terminal"
      command: "/Users/zero-suminc./.config/zs-ops/guards/dangerous-command.sh hermes"
      timeout: 10
```

Approve the Hermes hook at its first TTY prompt, or start a reviewed non-interactive run with `--accept-hooks`.

Merge these fragments with existing hook entries. Do not replace unrelated hooks.

## Tests

```bash
guards/tests/run-tests.sh
```

The harness sends native payloads through every lane and never executes the command strings under test.

## Rollout gates

1. Require a green matrix and executable bits on both shell files.
2. Confirm `jq` and `grep` resolve in each host's hook environment.
3. Register one lane at a time. Smoke-test one safe command, one blocked command, and malformed input while watching that host's hook log.
4. Remove the old lane-local dangerous-command hook only after parity passes. Keep unrelated file-read, telemetry, and amend guards in place.
5. For Antigravity and Hermes, verify malformed input returns deny/block JSON with exit 0. A hook error can fail open in those hosts.

## Antigravity file-read guard

`secret-file-read.sh antigravity` covers what `dangerous-command.sh` cannot see: `agy`
opening a credential file with its `view_file` tool. It applies the same path rules as
`~/.claude/hooks/block-secret-reads.sh` (credential stores, OAuth token files, non-template
`.env` files) to every string in `toolCall.args` and answers `{decision:"deny"}`.
Registered in `~/.gemini/config/hooks.json` with matcher `view_file`. Tests:
`bash tests/secret-file-read-tests.sh`. Added 2026-10-13 after an audit found `agy` could
read secret files through `view_file`.
