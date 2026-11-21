# Mermaid Render

Renders fenced `mermaid` blocks in assistant replies locally using
[`merman-cli`](https://github.com/Latias94/merman) (pure Rust, no browser or
Node.js required) as either terminal PNGs (Kitty/Sixel) or Unicode box-drawing
text.

## Quick start

1. Copy this directory into your fast-agent home's `plugins/` directory and
   enable `mermaid-render`.
2. Run `/mermaid install` inside fast-agent to preview the install command, then
   `/mermaid install --yes` to install `merman-cli`.

## Commands

- `/mermaid` — render all diagrams from the latest matching assistant reply
- `/mermaid 2` — render only diagram 2 (1-based)
- `/mermaid image [n]` / `/mermaid text [n]` — one-shot mode override
- `/mermaid on` / `/mermaid off` — toggle automatic post-turn rendering for this session
- `/mermaid mode image|text` — switch default output mode for this session
- `/mermaid theme dark|default|forest|neutral` — switch diagram theme for this session
- `/mermaid background <color>` — switch PNG background color for this session
- `/mermaid status` — show `merman-cli` status and active settings
- `/mermaid install [--yes]` — preview or run the `merman-cli` installer

## Configuration

```yaml
plugins:
  enabled: [mermaid-render]
  config:
    mermaid-render:
      mode: image                    # image (PNG) | text (Unicode)
      theme: dark                    # dark | default | forest | neutral
      background: "#1e1e2e"          # PNG background (or "transparent")
      auto_render: true
      max_diagrams: 3                # automatic renders per turn; 0 disables
      max_width: null                # column cap for text mode (defaults to terminal width)
      timeout_seconds: 15.0          # per diagram
      cache_dir: ~/.cache/fast-agent/mermaid
```

PNGs are cached by content hash (`source`, `theme`, `background`). Automatic
image rendering requires a fast-agent build with post-turn plugin image support;
text mode works in any terminal.
