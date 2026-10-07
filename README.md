# fast-agent card packs

Registry and reference card packs for `fast-agent`.

## Available packs

- `hf-dev` — developer-focused Hugging Face card pack with rg-first search.
- `codex` — GPT-5.6 developer card with the `ripgrep_spark` search subagent.
- `smart` — a minimal single-card test pack.
- `mcp-working` — cross-repo MCP workspace conductor bundle (spec + WG + python-sdk + typescript-sdk).
- `hf-codemode` — production-style Hugging Face Hub codemode pack with normal, raw, and selectable passthrough variants.

## Install with CLI

```bash
fast-agent cards --registry https://github.com/fast-agent-ai/card-packs add smart
fast-agent cards --registry https://github.com/fast-agent-ai/card-packs add hf-dev
fast-agent cards --registry https://github.com/fast-agent-ai/card-packs add codex
fast-agent cards --registry https://github.com/fast-agent-ai/card-packs add mcp-working
fast-agent cards --registry https://github.com/fast-agent-ai/card-packs add hf-codemode
```

## Install in interactive mode

```text
/cards registry https://github.com/fast-agent-ai/card-packs
/cards add smart
/cards add hf-dev
/cards add codex
/cards add hf-codemode
```

## Plugins

Install reusable plugins from the same registry:

```bash
fast-agent plugins add agent-finder
fast-agent plugins add edit-assistant
fast-agent plugins add session-html
fast-agent plugins add discover
fast-agent plugins add price-calculator
fast-agent plugins add llm-perf
```

### Maintaining plugins

`marketplace.json` publishes each plugin's `version`, `path_oid` (the git tree id
of the plugin directory) and `requires_fast_agent` so fast-agent can check for
updates with a single fetch. After changing a plugin:

1. Bump `version` in its `plugin.yaml` (the sync refuses changed contents without a bump).
2. Raise `requires_fast_agent` if it now needs newer fast-agent APIs.
3. Run `python scripts/sync_marketplace.py` and commit the result.

CI fails if `marketplace.json` is stale, and weekly checks that the latest
fast-agent release still imports every plugin.

`plugin_bundles` groups plugins for one-step installs; `recommended` is the
starter set offered to new users.
