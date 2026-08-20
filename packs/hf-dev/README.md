# hf-dev

Code with Hugging Face Inference providers.

## Authentication

Use the Hugging Face CLI to authenticate:

```bash
hf auth login
hf auth whoami
```

fast-agent uses the active token managed by `huggingface_hub` for Hugging Face
Inference Providers, model-picker readiness, Hub URLs, Spaces, and the
preconfigured Hugging Face MCP servers. There is no separate
`fast-agent auth login hf` flow.

For automation, set `HF_TOKEN` or configure `hf.api_key`. An explicit configured
token takes precedence over the token from `hf auth login`. You can create a
token at [Hugging Face settings](https://huggingface.co/settings/tokens).

## CLI Commands 

- Start with `fast-agent go` 
- Update the System Prompt in `.fast-agent/agent-cards/dev.md`
- Use `fast-agent model llamacpp` to configure and use models with [llamacpp](https://llama-cpp.com/)

## Next Steps 

From the fast-agent prompt:

- Use `/skills` to view and manage skills. Use to configure hooks, compaction and automation - `/skills registry` to choose source.
- Type `/skills add` to list available skills from the current registry.
- **Recommended**: Use `/skills add lsp-setup` and ask your agent to configure LSP for this workspace.
- Create new agents or subagents in this environment by asking the assistant, or add markdown files to `.fast-agent/agent-cards/`. Switch agents with `@`. 
- Use `/connect` to connect to MCP Servers (Hugging Face preconfigured). Enter a URL, npx/uvx package or stdio command.
