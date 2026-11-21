# Usage Tracker

Unified usage, cost, and performance tracking for fast-agent. Combines per-turn
cost display, turn latency/phase bars, ETag-updated pricing catalogs, and a
central machine-wide SQLite ledger (`~/.fast-agent/usage/usage.sqlite3`) with
share-worthy terminal charts, aggregated across your machines.

Install from the card-packs registry:

```bash
fast-agent plugins add usage-tracker
```

Works with fast-agent 0.10.43 and later. From 0.10.64 fast-agent passes the
session ID to the post-turn hook and records per-call timings inside
`exec_sandbox` cells, so live turns carry both. On earlier versions live turns
are recorded without a session ID (and without nested sandbox timings) until a
`/stats sweep` of the sessions directory fills them in.

## What It Tracks

At the end of every interactive turn, `usage-tracker` writes a single WAL
transaction to `~/.fast-agent/usage/usage.sqlite3` containing:

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

- `/stats [7d|30d|90d|all|24h]` — 68-column dashboard: headline KPIs with
  `▲▼` change against the previous period and per-day trend sparklines, a
  token anatomy bar (cached / fresh / cache-write input, answer / reasoning
  output), daily tokens stacked by model, the model table, a weekday × hour
  heatmap, and (with remotes) a per-machine breakdown.
  - Options: `--no-cost` (tokens-only for sharing), `--plain` (no colour),
    `--utc` / `--local`, `--model <substring>`, `--machine <name|local>`.
- `/stats models [7d|4d|30d|...] [--by tokens|cost|calls] [--group] [--totals]`
  — the model mix: one filled 100% bar per day showing each model's share
  (last 7 days by default), the overall split, and each model's share trend.
  The card shows proportions; pass `--totals` for raw token counts as well.
- `--group` (on `/stats`, `/stats models` and `/stats share`) merges models
  into families by model line and major version: `gemini-4-neon` and
  `gemini-4-carbon` become `gemini-4`, every Claude model becomes `claude`,
  and `gpt-6-astra`, `gpt-6.1-sol`, `gpt-6.2-sol` become `gpt-6.*`.
- `/stats rhythm [30d|90d|all]` — when you work: a contribution-style calendar
  (windows of 15+ days), the weekday × hour heatmap, and streaks.
- `/stats share [7d|30d|90d|all] [--no-cost]` — clean fenced card optimised for
  copy-paste and social sharing (never displays paths, session IDs, or machine
  names).
- `/stats perf [7d|30d|90d|all]` — aggregate wall-time phases, per-model TTFT
  p50→p90 range plot & decode `tok/s`, and top tools by time.
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

### Rendering

In the fast-agent TUI the cards are drawn in the 16 standard ANSI colours only
(no 256-colour or RGB values), so they follow your terminal theme. Solid
stacked bars resolve an eighth of a cell: a cell straddling two models is the
lower model's partial block on the upper model's colour. Each model family has
a preferred colour (Claude red, GPT green, Gemini blue, …); siblings take the
family's next choice. With `--plain`, in `/stats share`, and in non-TUI clients
the same layout is drawn without colour, using a distinct texture per series
(`█ ▓ ▒ ░ # = +`).

The calendar and the weekday × hour view are heatmaps of solid cells in four
steps (dim cyan, cyan, bright cyan, bright white); without colour the steps
are the shades `░ ▒ ▓ █`.

### Other Machines (`/stats remote`, `/stats sync`)

Reports can include the ledgers of other machines running fast-agent with this
plugin, so `/stats` shows your usage everywhere:

```text
/stats remote add workstation        # an SSH host, as in ~/.ssh/config (or user@host)
/stats remote add workstation --fallback workstation-vpn   # same machine, second address
/stats remote add /mnt/nas/fast-agent/usage.sqlite3 --name nas   # or a mounted path
/stats remote                        # list remotes and when they last synced
/stats sync [name]                   # refresh now
/stats 7d --machine workstation      # one machine only (or --machine local)
/stats remote remove workstation     # forget it and delete its local mirror
```

- **How it works**: a sync takes a consistent snapshot of the remote ledger
  (SQLite's online backup, safe while fast-agent is running there), streams it
  gzip-compressed over `ssh`, and stores it as a private mirror in
  `~/.fast-agent/usage/remotes/<name>.sqlite3`. Reports read the local ledger
  and the mirrors together. Nothing is written on the remote, and the local
  ledger is never modified.
- **No double counting**: rows are content-hashed, so a session that exists on
  two machines (a synced sessions folder, a sweep of copied history) counts
  once.
- **Freshness**: reports refresh mirrors older than `remote_sync_minutes`
  (default 30) before rendering; nothing is transferred when the remote has
  not changed.
- **Offline is fine**: remotes are only contacted by `/stats` reports and
  `/stats sync`, never while recording a turn. An unreachable machine cannot
  break or stall a report: each address gets 5 seconds to connect during the
  automatic refresh, a failed machine is not retried for another
  `remote_sync_minutes`, and its last mirror keeps being used (the card shows
  its age and the error). A mirror is only replaced by a complete, validated
  snapshot, so a connection dropped mid-transfer changes nothing.
- **Roaming**: give a machine several addresses with `--fallback` (repeatable),
  for example a LAN alias first and a VPN alias second; they are tried in
  order when a connection cannot be made.
- **Requirements**: non-interactive SSH access (keys or an agent; password
  prompts are disabled) and `python3`, `python` or `uv` on the remote. Remote
  ledgers written by a newer plugin version are refused until this machine's
  plugin is updated. Windows remotes over SSH are not supported; use a path.
- Pass `--db <path>` to `remote add` when the remote keeps its ledger somewhere
  other than `~/.fast-agent/usage/usage.sqlite3`.

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
- Cached updates are stored in `~/.fast-agent/usage/pricing_catalog.json` (never
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
      timezone: utc               # utc | local (days and the rhythm heatmap)
      db_path: ~/.fast-agent/usage/usage.sqlite3
      machine_name: desk          # this machine's name on cards (default: hostname)
      remote_sync_minutes: 30     # refresh remote mirrors older than this; 0 = only /stats sync
      pricing_auto_update: true   # ETag background catalog refresh
      pricing_url: https://raw.githubusercontent.com/fast-agent-ai/card-packs/main/plugins/usage-tracker/pricing_catalog.json
      # Performance display options (/performance also falls back to plugins.config.llm-perf):
      session: false
      tps: decode                 # decode | e2e
      burst_ms: 500
      min_duration_ms: 500
```
