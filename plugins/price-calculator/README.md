# Price Calculator

Displays estimated model cost after each successful top-level interactive turn:

```text
Cost: $0.002400 last · $0.0137 session
```

Requires fast-agent 0.10.1 or later for post-user-turn plugin hooks and command
usage access.

Install it from the card-packs registry:

```bash
fast-agent plugins add price-calculator
```

Use `/cost` for one rollup per top-level user turn, with included subagent or
parallel-child ledgers. Use `/cost detail` for the full provider-attempt table,
including model, service tier, context band, token/cache partitions, and cost.

The plugin includes every provider attempt attributed to the user turn,
including tool-loop calls, retries, parallel fan-out/fan-in calls, and subagent
calls merged into the parent agent's canonical usage. It displays once for the
top-level turn rather than once per subagent.

Hardcoded USD-per-million-token rates are included for:

- GPT-5.6 Sol, Terra, and Luna, with Standard and Flex short/long-context rates.
- Kimi K3 across providers.
- DeepSeek V4 Flash (`$0.14` input, `$0.002` cached input, `$0.28` output).

GPT-5.6 prompts over 272,000 tokens use long-context rates. Where a provider
does not publish a separate cache-write tariff, cache-write tokens use the
normal input rate. Fast-tier calls, unknown models, and incomplete token
partitions are labeled unpriced instead of being counted as free.
