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
- `/mermaid theme auto|herdr|dark|default|forest|neutral` — switch diagram theme for this session
- `/mermaid background <color|auto>` — switch PNG background color for this session
- `/mermaid test [image|text]` — render 3 sample diagrams (flowchart, sequence, state) with active settings
- `/mermaid status` — show `merman-cli` status and active settings
- `/mermaid install [--yes]` — preview or run the `merman-cli` installer

## Herdr theme awareness

With `theme: auto` (the default) or `theme: herdr`, when running inside Herdr
(`HC_TERMINAL_ID` / `HC_TERMINAL_SOCKET`), the plugin reads Herdr's active theme
from `~/.config/herdr-app/settings.json` and `~/.config/herdr-app/themes/<theme>.json`
(mapping its background, foreground, surfaces, borders, accent, and ANSI palette
into Mermaid `themeVariables`, plus matching Herdr's configured font via `fc-match`).
Switching Herdr's theme automatically updates future renders and cache keys.

## Configuration

```yaml
plugins:
  enabled: [mermaid-render]
  config:
    mermaid-render:
      mode: image                    # image (PNG) | text (Unicode)
      theme: auto                    # auto | herdr | dark | default | forest | neutral
      background: auto               # auto | hex color | "transparent"
      font_file: null                # optional explicit TTF/OTF path (auto-falls back on font limits)
      auto_render: true
      max_diagrams: 3                # automatic renders per turn; 0 disables
      max_width: null                # column cap for text mode (defaults to terminal width)
      timeout_seconds: 15.0          # per diagram
      cache_dir: ~/.cache/fast-agent/mermaid
```

PNGs are cached by content hash (`source`, `theme`, `background`, resolved palette,
and font). Automatic image rendering requires a fast-agent build with post-turn
plugin image support; text mode works in any terminal.
