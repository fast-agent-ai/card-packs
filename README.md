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
```
