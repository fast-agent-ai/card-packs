# codex

Developer-focused Codex pack for `fast-agent`.

This pack installs a `dev` smart card with LSP navigation helpers plus a
Codex-optimized `ripgrep_spark` repository search subagent for multi-step
rg-first search workflows.

## Install from marketplace

```bash
fast-agent cards add codex
```

## What gets installed

- `dev` smart card (`agent-cards/dev.md`)
- LSP function tools helper (`agent-cards/multilspy_tools.py`)
- `ripgrep_spark` tool card (`tool-cards/ripgrep_spark.md`)
- Read-only search guard hook (`hooks/ripgrep_readonly_guard.py`)
- Pack-local `fastagent.config.yaml`

## Model configuration

The included `fastagent.config.yaml` configures:

- `$system.default` → `codexplan`
- `$system.fast` → `codexspark`
- `$system.last_used` → `codexplan`

`ripgrep_spark` uses `$system.fast`, so the search helper resolves to
`codexspark` by default.

## MCP targets

The pack preconfigures default MCP targets for the `/connect` menu:

- `openai`
- `hf`
- `hf_docs_only`

These are connection targets, not expected startup connections. If
authentication is required, `fast-agent` should prompt when you connect to a
server rather than during normal startup.

## Next steps

- Start with `fast-agent go`
- Use the `dev` card for normal coding tasks
- Use `ripgrep_spark` for bounded multi-step repository search
- Use `/connect` to connect to the preconfigured MCP servers

If needed, authenticate with the relevant provider before connecting:

- OpenAI via the local OAuth/browser flow used by `fast-agent`
- Hugging Face via `HF_TOKEN` or `hf auth login`
