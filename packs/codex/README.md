# codex - welcome to `fast-agent`

With GPT-5.6, `codexspark`, and other models. WebSockets are enabled by default,
and an `apply_patch` tool matching the Codex CLI tool signature is supplied. A
filesystem search subagent is active by default (powered by `codexspark`).

## Authentication

Authenticate Codex through fast-agent's shared provider credential store:

```bash
fast-agent auth login codex
fast-agent auth status codex
```

Credentials are stored in the OS keyring when available, with
`~/.fast-agent/auth.json` as the fallback. Set `FAST_AGENT_AUTH_FILE` to use an
explicit credential file for an isolated or portable environment; when set, that
file is authoritative and fast-agent does not fall back to the keyring or the
Codex CLI account.

Without `FAST_AGENT_AUTH_FILE`, fast-agent can read the Codex CLI's
`~/.codex/auth.json` (or the path selected by `CODEX_HOME` or
`CODEX_AUTH_JSON_PATH`) as an external fallback. That file is read-only:
fast-agent login, refresh, logout, and export never modify or overwrite it.

For API-key authentication, set `CODEX_API_KEY` or configure
`codexresponses.api_key`; an explicit API key takes precedence over OAuth.

When Codex is not statically configured, the model picker shows `○` and
`[auth on select]`. Select the model normally: the startup flow checks for an
existing credential, starts Codex OAuth only when needed, then continues with
the model you selected.

## CLI Commands 

- Start with `fast-agent go` 
- Update your System Prompt in `.fast-agent/agent-cards/dev.md`. `AGENTS.md` is included by default

## Next Steps 

From the fast-agent prompt:

- Use `/peek <message>` to ask a one-off question without keeping it in the active chat history.
- Use `/edit-last` (`c-x e`) to open the last assistant response in `$VISUAL`/`$EDITOR` and prefill your edited reply.
- Use `/annotate-last` (`c-x a`) to annotate the last assistant response with AnnoTUI
- Use `/skills` to view and manage skills. Use to configure hooks, compaction and automation - `/skills registry` to choose source.
- Optional: use `/skills add lsp-setup` and ask your agent to configure LSP for this workspace.
- Other skills available help you configure/design compaction if needed, set up agent hooks or automate `fast-agent`
- Create new agents in this environment  by asking the assistant, or adding markdown files to `.fast-agent/agent-cards/`. Switch agents with `@`. 
- Use `/connect` to connect to MCP Servers (Hugging Face and OpenAI preconfigured)
- Recommended: Set up a sandbox environment for development. Sample prompt:  `configure a docker execution environment (ubuntu) named docker-env, with a read-only mount of the current directory. make it the default`
