# Usage Tracker

Unified usage, cost, and performance tracking for fast-agent. Combines per-turn
cost display, turn latency/phase bars, ETag-updated pricing catalogs, and a
central machine-wide SQLite ledger (`~/.fast-agent/stats/stats.sqlite3`) with
social-ready ASCII charts.

Install from the card-packs registry:

```bash
fast-agent plugins add usage-tracker
```

## What It Tracks

At the end of every interactive turn, `usage-tracker` writes a single WAL
transaction to `~/.fast-agent/stats/stats.sqlite3` containing:

- **Provider calls**: session ID, timestamp, provider/upstream provider, model,
  service tier, input/uncached/cache-read/cache-write (including Anthropic 5m/1h
  and OpenAI 30m/12h splits), output/reasoning tokens, estimated USD cost,
  pricing catalog version, duration, TTFT, streaming time, and burst flag.
- **Turn wall-time phases**: `wait`, `stream`, `tools`, `process`, and `other`
  phase attribution (using the same priority overlap rules as `llm-perf`).
- **Tool runs & PTC sandboxes**: per-tool execution time, run counts,
  process-wait flags, and nested `exec_sandbox` tool calls (`ptc_tool_calls`)
  with per-model sandbox efficiency and script error classification (syntax,
  typecheck, runtime).

All rows use deterministic content hashes (`INSERT OR IGNORE`), so live
recording, session resumes, forked sessions, and repeated `/stats sweep` runs
never double-count.

## Commands

### Machine-Wide Analytics (`/stats`)

- `/stats [7d|30d|90d|all|24h]` — 68-column ASCII dashboard with headline KPIs,
  top models bar chart, 4-hour UTC (or `--local`) rhythm heatmap, 2-row daily
  token sparkline, cache hit bar, and wall-time phase breakdown.
  - Options: `--no-cost` (tokens-only for sharing), `--plain` (pure ASCII/Unicode
    without Rich color markup), `--utc` / `--local`, `--model <substring>`.
- `/stats share [7d|30d|90d|all] [--no-cost]` — clean fenced card optimised for
  copy-paste and social sharing (never displays paths or session IDs).
- `/stats perf [7d|30d|90d|all]` — aggregate wall-time phases, per-model TTFT
  p50/p90 & decode `tok/s`, and top tools by time.
- `/stats tools [7d|30d|90d|all] [--raw] [--model <substring>]` — detailed tool
  execution breakdown (`runs`, `total`, `avg`, `max`, process-wait share). In
  the default view, tool names are case-normalised (e.g. `Bash`/`bash`,
  `Process`/`process`), blocking process waits (`poll_process`,
  `process(wait)`) are summarised separately with wait percentiles (`p50`,
  `p90`, `avg`, `max`), and PTC (`exec_sandbox`) runs are unwrapped alongside
  a per-model sandbox efficiency & script-error breakdown; pass `--raw` to
  inspect exact raw outer tool calls.
- `/stats sweep <dir> [--dry-run] [--all]` — safely scan a directory of
  fast-agent sessions with live progress to backfill historical tokens, costs,
  turn phases, and tool runs. By default, sweeps ordinary `.fast-agent/sessions`
  sidecar trees (plus directly targeted session folders), skipping caches,
  symlinks, and `history_*_previous.json` snapshots; pass `--all` to scan
  arbitrary nested trees (such as benchmark or custom-env exports). Unchanged
  files are skipped on repeat sweeps.
- `/stats pricing [status|update]` — inspect active pricing catalog or force an
  ETag conditional refresh (and automatically backfill previously unpriced calls).
- `/stats reprice` — backfill `cost_usd` for any stored unpriced calls using the
  active pricing catalog.

### Current Session Reports

- `/cost [summary|detail]` — current session cost rollup by user turn, or full
  provider-call ledger (same workflow as `price-calculator`).
- `/performance [summary|detail]` — current session wall-time phases, TTFT
  p50/p90, and `tok/s` (same workflow as `llm-perf`).

## Automatic Pricing Updates

`usage-tracker` ships with a bundled `pricing_catalog.json` and checks for
updates in a non-blocking background thread using HTTP `If-None-Match` (ETag) +
gzip:

- `304 Not Modified` transfers zero body bytes.
- Cached updates are stored in `~/.fast-agent/stats/pricing_catalog.json` (never
  modifying the installed plugin directory).
- Checks run at most once every 24 hours, or once per hour while unpriced calls
  exist.
- If a remote catalog requires a newer pricing schema or `min_plugin_version`,
  the current valid catalog stays active and `/stats` shows a notice to run
  `/plugins update usage-tracker`.

## Post-Turn Display & Herdr Status

After each successful interactive turn, `usage-tracker` prints the compact cost
line and performance phase bar (both configurable) and reports compact session
cost via Herdr OSC 7501 `session_usage` and legacy `$cost` pane metadata.

## Configuration

```yaml
plugins:
  config:
    usage-tracker:
      record_usage: true          # persist turn metrics to SQLite
      show_cost: true             # show post-turn cost line
      show_perf: true             # show post-turn perf bar & TTFT sparkline
      timezone: utc               # utc | local (default for 4h rhythm heatmap)
      db_path: ~/.fast-agent/stats/stats.sqlite3
      pricing_auto_update: true   # ETag background catalog refresh
      pricing_url: https://raw.githubusercontent.com/fast-agent-ai/card-packs/main/plugins/stats-tracker/pricing_catalog.json
      # Performance display options (/performance also falls back to plugins.config.llm-perf):
      session: false
      tps: decode                 # decode | e2e
      burst_ms: 500
      min_duration_ms: 500
```
