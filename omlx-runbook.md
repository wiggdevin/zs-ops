# oMLX local inference runbook

Verified 2026-08-17 on the M4 Max / 48 GB Mac.

## Architecture decision

oMLX is the primary opt-in local agent lane on loopback port 8000. Ollama remains
on loopback port 11434 for `zs-memory` embeddings (`qwen3-embedding:8b`, 4096d),
the nightly dream job, and legacy consumers. Replacing that embedder in place
would make existing stored vectors incompatible, so this is intentionally a
side-by-side migration rather than an Ollama uninstall.

Both `~/bin/ollama` and `/opt/homebrew/bin/ollama` launch the exact CLI shipped
in Ollama.app so the retained client and server stay version-aligned. The redundant
Homebrew formula was removed on 2026-08-17; the pre-migration formula version and
symlink target are recorded in the rollback snapshot and can be restored with
`brew install ollama` if ever needed.

Normal Claude Max and Codex OAuth commands are unchanged. Use the explicit local
launchers from a project directory:

```sh
codex-omlx
claude-omlx
```

## Service and configuration

- App: `/Applications/oMLX.app`
- CLI: `~/.omlx/bin/omlx` (`/opt/homebrew/bin/omlx` symlink)
- Health: `http://127.0.0.1:8000/health`
- OpenAI API: `http://127.0.0.1:8000/v1`
- Anthropic Messages API: `http://127.0.0.1:8000/v1/messages`
- Settings: `~/.omlx/settings.json`
- Per-model settings: `~/.omlx/model_settings.json`
- Model store: `~/.omlx/models`; Hugging Face cache discovery is enabled
- SSD cache: `~/.omlx/cache`, capped at 8 GB to keep the machine lean
- Memory guard: safe; distributed inference: disabled

The default verified model is `mlx-community--Qwen3-8B-8bit`, with thinking
disabled, 65,536-token configured context, 8,192 max output tokens, and
300-second TTL. The larger configured window satisfies Claude Code's 48K minimum;
oMLX's safe memory guard still bounds actual prefill pressure.

## Operations

```sh
omlx diagnose menubar
omlx start
omlx restart
omlx stop
curl -fsS http://127.0.0.1:8000/health | jq .
```

`system-healthcheck`, the session-start guard, telemetry sampler, and watchdog
all check oMLX separately from Ollama.

## Rollback

The pre-migration snapshot is:

`~/Backups/omlx/2026-08-17-pre-migration`

It includes the previous launch agents, health scripts, launchers, configuration,
and SHA-256 hashes. Keep it until oMLX has passed one full week of normal use.
Do not delete Ollama models while `zs-memory` uses its current 4096-dimensional
embedding index.
