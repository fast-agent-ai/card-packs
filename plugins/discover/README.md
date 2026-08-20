# Agent Resource Discovery

Adds `/discover <query>` for discovering skills and MCP servers from Agent
Resource Discovery (ARD), then applying a selected result to the current
session.

It also accepts an absolute URL:

```text
/discover http://localhost:8765/page
/discover http://localhost:8765/page weather tools
```

URL mode fetches the URL with redirects, then presents a source-first picker.
It recognizes direct AI Catalogs, Agent Skills v0.2 indexes, MCP Server Cards,
`SKILL.md` files, and ARD `/search` endpoints. For normal HTML pages it looks
for an AI Catalog in `Link` headers, then HTML `<link>` tags, then
`/.well-known/ai-catalog.json`; independently it probes
`/.well-known/agent-skills/index.json`. The optional words after a URL are the
query used when selecting an advertised ARD registry.

The picker keeps source kind, discovery method, and document URL in its rows
and breadcrumbs. Use `b`, Left, or Backspace to return from a source or nested
catalog. Non-TUI invocations return Markdown grouped by top-level source.

## Local test and demo

From the `card-packs` checkout with `fast-agent` as a sibling:

```bash
uv run --project ../fast-agent pytest tests/test_discover_url.py
uv run --project ../fast-agent python -m py_compile plugins/discover/discover.py
uv run --project ../fast-agent python plugins/discover/demo_server.py
```

The demo prints a local URL. To use this checkout directly from a sibling
`fast-agent` development environment:

```bash
ln -sfn "$PWD/plugins/discover" ../fast-agent/.fast-agent/plugins/discover
```

Enable `discover` under `plugins.enabled` in
`../fast-agent/.fast-agent/fast-agent.yaml`, start `uv run fast-agent go` from
that checkout, and run `/discover <printed URL>`. The demo serves a linked
catalog with a nested catalog, MCP card, ARD endpoint, and a digest-verified
Agent Skills v0.2 skill. It uses only the Python standard library. The MCP card
is for navigation and prompt-prefill testing rather than a live MCP connection;
the demo registry intentionally returns an empty result set.

## Configuration

Add additional ARD registries with `plugins.config.discover.urls` in `fast-agent.yaml`:

```yaml
plugins:
  enabled:
    - discover
  config:
    discover:
      urls:
        - https://huggingface-hf-discover.hf.space/search
        - https://example.com/my-ard-registry/search
```

Registry URLs may be supplied with or without a trailing `/search`; the plugin
normalizes them before querying. Defaults are included unless disabled:

```yaml
plugins:
  config:
    discover:
      include_default_urls: false
      urls:
        - https://example.com/private-registry
```

For a local development install named `discover-dev`, use the same keys under
`plugins.config.discover-dev`.
