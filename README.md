# fast-agent card packs

Registry and reference card packs for `fast-agent`.

## Available packs

- `dev` — developer-focused smart card pack with LSP tools and rg-first search helper.
- `smart` — a minimal single-card test pack.
- `mcp-working` — cross-repo MCP workspace conductor bundle (spec + WG + python-sdk + typescript-sdk).

## Install with CLI

```bash
fast-agent cards --registry https://github.com/fast-agent-ai/card-packs add smart
fast-agent cards --registry https://github.com/fast-agent-ai/card-packs add dev
fast-agent cards --registry https://github.com/fast-agent-ai/card-packs add mcp-working
```

## Install in interactive mode

```text
/cards registry https://github.com/fast-agent-ai/card-packs
/cards add smart
/cards add dev
```
