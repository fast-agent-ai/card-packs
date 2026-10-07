# LLM Perf

Shows where each turn's time went, after each successful top-level interactive
turn. A stacked bar splits wall time into **wait** (yellow), **stream** (green),
**tools** (blue), **process** waits (magenta) and **other** (dim); the coloured
labels double as the legend and add up to the wall time.

```text
Perf  ━━━━━━━━━━━━━━━━━━━━━━━━  42s · 1 call · wait 8.3s · stream 33s · 40 tok/s
```

Tool loops (more than three LLM calls) add a TTFT row: a sparkline of each call's
time to first token, in call order, with spikes (more than twice the median) in
the wait colour. The bar and sparkline share a column:

```text
Perf  ━━━━━━━━━━━━━━━━━━━━━━━━  29m11s · 69 calls · wait 4m47s · stream 5m16s · tools 15m44s · process 3m16s
TTFT  █▅▅▅▄▅▆▅▄█▅▄▄▅▄▆▇▄▄▇▄▅▄▆  p50 3.8s · p90 6.7s · max 9.6s · 68 tok/s
```

On narrow terminals the smallest phase labels are dropped first; the bar stays.

Install it from the card-packs registry:

```bash
fast-agent plugins add llm-perf
```

The post-turn display requires a fast-agent release that passes the turn's
messages to post-turn plugins (`PluginPostUserTurnContext.turn_messages`); on
earlier releases it shows nothing. `/performance` also works on earlier releases,
because it reads the timing fast-agent already persists in message history.

Both read the same data: the `fast-agent-timing`, `fast-agent-tool-timing` and
`fast-agent-usage` channels in the active agent's history. Folded managed-process
polling is first restored from its audit archive, so every poll keeps its own LLM
call and process wait.

## Metrics

All timings are client-observed, including network and provider queueing.

- **wait** — time to first streamed activity (reasoning, text, or tool call),
  including queueing, prompt prefill and any reasoning the provider does not
  stream.
- **stream** — time spent streaming after the first activity.
- **tools** — tool execution, including subagent calls.
- **process** — blocking waits on managed processes: `process` with
  `action: wait`, or `poll_process` with a positive `wait_sec`.
- **other** — the rest of the turn's wall time: approvals and fast-agent
  overhead.
- **burst calls** — a call whose output arrives within `burst_ms` (500ms) of the
  first activity. Some providers buffer output or only stream a reasoning summary
  just before the answer. Their whole duration counts as wait and they are
  excluded from tok/s, which would otherwise read in the tens of thousands.
  When fewer than 80% of calls contribute, tok/s shows its coverage, for example
  `43 tok/s (16/29)`.
- **tok/s** — completion tokens (including reasoning) divided by streaming time,
  over streamed calls only. Set `tps: e2e` for completion tokens divided by
  whole-call duration instead, which is lower but immune to bursts.

Overlapping time counts once, in priority order stream > wait > tools > process.
So a subagent tool call shows the child's LLM time as wait/stream and only the
remainder as tools, and parallel tool calls are not double counted. Failed retry
attempts carry no timing; their elapsed time falls inside the successful
attempt's wait.

## Commands

- `/performance` or `/performance summary` — one row per user turn (calls, wall,
  wait, stream, tools, process, other, TTFT p50/p90, tok/s) with a cumulative
  row, then a per-model table including the share of burst calls.
- `/performance detail` — one row per LLM call.

`/performance` covers the whole session, including resumed turns. Subagent and
parallel-child LLM calls are not in the active agent's history, so in both the
display and `/performance` a subagent call appears as tool time.

## Configuration

```yaml
plugins:
  config:
    llm-perf:
      session: false        # add a "Sess" row with cumulative tok/s and TTFT
      tps: decode           # decode | e2e
      burst_ms: 500         # streaming shorter than this counts as a burst
      min_duration_ms: 500  # hide turns with less LLM time than this
```
