# hf-dev

Developer-focused card pack for the HF/GPT-OSS path.

Includes:

- `dev` smart card (`agent-cards/dev.md`)
- LSP function tools helper (`agent-cards/multilspy_tools.py`)
- `ripgrep_search` tool card (`tool-cards/ripgrep-search.md`)
- Search guard hook (`hooks/fix_ripgrep_tool_calls.py`)
- Default config (`fastagent.config.yaml`) with GitHub MCP bearer auth via `${GITHUB_TOKEN}`

Search model:

- `ripgrep_search` uses `$system.fast`

## Publish target

Intended marketplace repo path: `packs/hf-dev` in `https://github.com/fast-agent-ai/card-packs`.
