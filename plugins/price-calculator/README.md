# Price Calculator

Displays estimated model cost after each successful top-level interactive turn:

```text
Cost: $0.002400 last · $0.0137 session
```

Requires fast-agent 0.10.2 or later for post-user-turn plugin hooks, command
usage access, and Rich presentation styles.

Install it from the card-packs registry:

```bash
fast-agent plugins add price-calculator
```

Use `/cost` or `/cost summary` for one rollup per top-level user turn, with
included subagent or parallel-child ledgers. When a session uses multiple
models, the summary also groups calls, tokens, and cost by model. Use
`/cost detail` for the full provider-attempt table, including model, service
tier, context band, token/cache partitions, and cost.

Cost columns use one report-wide precision so decimal places align. Reports use
two decimals for dollar-scale costs, four when sub-dollar precision matters,
and six when displaying sub-cent costs. Summary and detail tables end with a
bold `Cumulative` row calculated from canonical provider attempts.

Cache-read input is shown with its share of total input, for example
`12,461,824 (91%)`. Cache-hit rates below 50% render red in the Rich terminal
UI while remaining ordinary percentage text in portable clients. The
cache-write column is omitted unless the report contains a positive
provider-reported cache-write count.

After a session resume, `/cost` reconstructs user-turn usage from canonical
`fast-agent-usage/v2` records in message history. Tool calls and their follow-up
model calls remain in the enclosing user turn. New live turns replace their
reconstructed counterparts, preserving richer subagent and parallel ledgers
without double counting. Historical child-agent ledger labels are available
only when their usage is represented in the active agent history.

The plugin includes every provider attempt attributed to the user turn,
including tool-loop calls, retries, parallel fan-out/fan-in calls, and subagent
calls merged into the parent agent's canonical usage. It displays once for the
top-level turn rather than once per subagent.

Hardcoded USD-per-million-token rates are included for:

- GPT-5.6 Sol, Terra, and Luna, with Standard and Flex short/long-context rates.
- Kimi K3 across providers.
- DeepSeek V4 Flash (`$0.14` input, `$0.002` cached input, `$0.28` output).
- Muse Spark 1.1 and 1.2 Standard (`$1.25` input, `$0.15` cached input,
  `$4.25` output) and Muse Spark 1.2 Contributor (`$0.10` input, `$0.002`
  cached input, `$0.20` output).
- Grok 4.3 and 4.5 (`$2.00` input, `$0.30` cached input, `$6.00` output);
  prompts over 200,000 tokens use `$4.00` input, `$0.60` cached input, and
  `$12.00` output.

GPT-5.6 prompts over 272,000 tokens use long-context rates. Where a provider
does not publish a separate cache-write tariff, cache-write tokens use the
normal input rate. Fast-tier calls, unknown models, and incomplete token
partitions are labeled unpriced instead of being counted as free.
