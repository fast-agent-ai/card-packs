# hf-dev

Code with Hugging Face Inference providers.

[Create](https://huggingface.co/settings/tokens) or set your `HF_TOKEN` for inference providers (or use `hf auth login`).

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

