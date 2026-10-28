# Price Calculator

Displays estimated model cost after each successful top-level interactive turn:

```text
Cost: $0.002400 last · $0.0137 session
```

When running inside Herdr, the plugin also reports a compact custom `$cost`
pane token. Fully priced usage shows the last turn followed by the session
total, for example `$0.002400 ($0.0137)`. If either cost is incomplete because
of unpriced model calls, the token falls back to cumulative input/output usage,
for example `12.1M in · 56,028 out`. An empty session clears the token. Add
`$cost` to a Herdr sidebar row to display it.

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

The bundled, versioned `pricing_catalog.json` contains USD-per-million-token
rates. Catalog rules can vary by fast-agent provider, upstream provider,
service tier, effective date, recurring UTC weekday/time range, and prompt-token band.
This is important for Hugging Face routes, where the same model may have a
different tariff through different upstream inference providers, and for
DeepSeek's peak/off-peak schedule. Provider-specific rules take precedence over
provider-neutral fallbacks.

The bundled catalog currently includes:

- GPT-6 Astra: Standard rates of `$10` input, `$1` cache reads, `$12.50`
  cache writes, and `$50` output per million tokens. Above 272,000 input
  tokens, input/cache rates double and output is 1.5× for the full request.
  Batch/Flex cost 50% and Fast costs 2× the applicable rates. Codex OAuth
  costs are API-equivalent estimates, not subscription charges.
- GPT-6 Sol (`$2` input, `$10` output) and GPT-6 Luna (`$0.10` input, `$0.50`
  output) per million tokens, from OpenAI's published list rates as of
  September 22, 2026. Only input/output rates were published; cache reads
  (10% of input), cache writes (125% of input), the >272,000-token long-context
  band, Batch/Flex (50%) and Fast (2×) rates are derived with the same ratios
  as GPT-6 Astra. On September 22, 2026 these rates were cross-checked
  against the `billing.token_prices` the GitHub Copilot `/models` catalog
  reports for both models, including cache and long-context rates (Copilot
  rounds Luna's cache write to `$0.12`). Codex OAuth costs are
  API-equivalent estimates.
- GPT-6.1 Sol: OpenAI's published list rates as of September 29, 2026 —
  `$2` input, `$0.10` cache reads (5% of input), `$2.50` cache writes, and
  `$10` output per million tokens, with the same >272,000-token long-context
  band, Batch/Flex (50%) and Fast (2×) rules as GPT-6 Sol. Cross-checked the
  same day against the Copilot `/models` `billing.token_prices` (including
  long-context rates). Codex OAuth costs are API-equivalent estimates.
- GPT-6.1 Terra: OpenAI's published list rates, checked October 26, 2026 —
  `$1` input, `$0.05` cache reads, `$1.25` cache writes (30-minute retention),
  and `$5` output per million tokens. Prompts above 272,000 total input tokens
  (including cached tokens) double input/cache rates and multiply output by
  1.5 for the full request. The same rates and threshold apply to OpenAI API
  (`openai`/`responses`), Codex plan (`codexresponses`), and Copilot routes;
  Terra has no Codex-specific threshold. Batch/Flex cost 50% and Fast costs
  2× the applicable rates; Copilot uses Standard, as it exposes no service tiers.
  Codex and Copilot costs are API-equivalent estimates, not subscription charges.
  The published 12-hour cache-write rate is `$2.50` before tier/context
  adjustments, but the calculator does not distinguish OpenAI cache retention
  durations and uses the 30-minute tariff for reported cache writes. Regional
  processing premiums are not represented.
- GPT-5.6 Sol, Terra, and Luna, with Standard, Flex, and Fast short/long-context
  rates.
- GPT-5.6 and later bill prompt-cache writes at 1.25x the uncached input rate on
  every route, including Copilot (OpenAI prompt-caching guide, "GPT-5.6 and later";
  Copilot `/models` `billing.token_prices` agrees). Caching is implicit by default,
  so `input_tokens_details.cache_write_tokens` is billed without any opt-in. Models
  before GPT-5.6 have no write charge.
- First-party Anthropic Claude Fable 5, Mythos 5, Opus 5.5, 5 and 4.x, Sonnet 5.5, 5 and
  4.x, and Haiku 5.5 and 4.5. The catalog includes Standard/Priority token rates,
  5-minute and 1-hour cache writes, cache reads, and Fast mode for Opus 5 and
  Opus 4.8.
- GitHub Copilot (`provider=copilot`) calls are priced at the list rates of the
  associated upstream model, covering Copilot's GPT-6 Astra, Sol, and Luna, GPT-6.1 Sol and Terra, GPT-5.6 Sol, Terra,
  and Luna, and Claude Opus 5.5, Opus 5, Sonnet 5.5, Sonnet 5, Fable 5, Fable 5.1, Haiku 5.5, and Haiku 4.5
  routes. The gateway does not expose service tiers, so Standard rates apply.
  These are API-equivalent estimates, not Copilot subscription or premium-request
  charges. The pre-5.5 Claude route rates (Opus 5.5 and Sonnet 5.5 use published list rates) were verified on September 20, 2026 against the
  `copilot_usage.token_details` rates the gateway reports on its Anthropic-wire
  responses; the gateway reports no cost metadata for its GPT (Responses-wire)
  routes, which use published OpenAI list rates.
- Kimi K3 through the Moonshot provider.
- Z.ai GLM-5.3, GLM-5.2, and GLM-5.3-Flash. Flash uses its 50%-discounted
  tariff until September 9, 2026 at 16:00 UTC, then switches to list pricing.
  Published cached-input storage is currently free; new input processing still
  bills at the ordinary input rate, including tokens written to the cache.
- Hugging Face GLM-5.3 and GLM-5.3-Flash through the `zai-org`, `together`, and
  `novita` upstreams: `$1.40` input, `$0.26` cached input, and `$4.40` output
  per million tokens, with Flash at `$0.15`, `$0.03`, and `$0.50`. Hugging Face
  passes provider rates through with no markup, and all three upstreams publish
  the same list tariff. Cache writes use the ordinary input rate on all three
  upstreams: [Z.ai's caching documentation](https://docs.z.ai/guides/capabilities/cache#billing-information)
  distinguishes free cache storage from new content tokens billed at standard
  input prices. Together and Novita publish no separate cache-write tariff, so
  the calculator uses its input-rate fallback. Provider list rates were
  verified on October 27, 2026 against [Z.ai's pricing
  page](https://docs.z.ai/guides/overview/pricing), [Together's GLM-5.3
  guide](https://docs.together.ai/docs/glm-5.3-quickstart), and [Novita's GLM-5.3
  model page](https://novita.ai/models/model-detail/zai-org-glm-5.3) (Flash
  equivalently). HF's router model list independently confirms Novita's input
  and output rates; it does not expose every provider or cached-input tariff.
- Hugging Face GLM-5.2 through `zai-org` and `novita` at the same `$1.40`,
  `$0.26`, and `$4.40` tariff, and through `deepinfra` at `$0.75` input, `$0.14`
  cached input, and `$2.40` output.
- Hugging Face Kimi K3 through `together` and `baseten` at `$3.00` input, `$0.30`
  cached input, and `$15.00` output, and through `deepinfra` at `$2.85`, `$0.285`,
  and `$14.25`. No upstream publishes a cache-write tariff, so writes use the
  input rate.
- GLM-5.2 and Kimi K3 HF rates were verified on October 28, 2026 against
  [Novita's GLM-5.2 model page](https://novita.ai/models/model-detail/zai-org-glm-5.2),
  [Together's pricing page](https://www.together.ai/pricing),
  [Baseten's Kimi K3 page](https://www.baseten.co/library/kimi-k3/), and
  DeepInfra's [OpenAI-compatible model list](https://api.deepinfra.com/v1/openai/models)
  (`metadata.pricing`); input and output agree with HF's router model list.
- HF calls without a known upstream (including `auto`/`fastest`) remain
  unpriced, as do GLM-5.3 via nscale, baseten, or deepinfra, GLM-5.2 via
  baseten or scaleway, and Kimi K3 via featherless-ai.
- Muse Glimmer 30B through Hugging Face and Together (`$0.35` input, `$0.04`
  cached input, `$1.50` output).
- DeepSeek V4.1 Flash (API model `deepseek-flash`), verified against the official
  pricing page on September 10, 2026. Legacy `deepseek-v4-flash` and
  `deepseek-v4-flash-vision-exp` route to `DeepSeek-V4.1-Flash` at the same rates.
  Peak USD per million tokens: `$0.30` input, `$0.006` cached input, `$1.20`
  output; off-peak: `$0.15`, `$0.003`, `$0.60`. Peak windows are Monday–Friday
  `01:00–04:00` and `06:00–10:00` UTC (start inclusive, end exclusive);
  weekends are off-peak.
- Hugging Face `deepseek-ai/DeepSeek-V4.1-Flash:novita`: `$0.30` input,
  `$0.006` cached input, and `$1.20` output per million tokens (confirmed by the
  user on October 11, 2026; matches the HF router's listed Novita input/output
  rates). The earlier `$0.03` input rate was an error.
  They apply only to `provider=hf` with `upstream_provider=novita`, without
  peak/off-peak windows. Native DeepSeek and other upstream tariffs are unchanged.
- DeepSeek V4 Pro retains its existing rates and UTC time windows. Announced
  September 14 routing changes are not applied in this catalog update.
- Muse Spark 1.1–1.3 Standard (`$1.25` input, `$0.15` cached input, `$4.25`
  output) and Muse Spark 1.2–1.3 Contributor (`$0.10` input, `$0.002` cached
  input, `$0.20` output).
- Grok 4.3 and 4.20 (`$1.25` input, `$0.20` cached input, `$2.50` output),
  Grok 4.5 (`$2.00`, `$0.30`, `$6.00`), Grok 4.6 (`$2.00`, `$0.50`, `$6.00`),
  and Grok Build (`$1.00`, `$0.20`, `$2.00`). Prompts at or above 200,000
  tokens use each model's long-context rates.

Claude Opus 5.5 (`claude-opus-5-5`, Copilot `claude-opus-5.5`) costs `$4`
input, `$20` output, `$5` for 5-minute cache writes, `$8` for 1-hour cache
writes, and `$0.20` for cache reads per million tokens. These rates come from
the user-supplied official Anthropic pricing documentation/screenshot on
September 22, 2026. Anthropic Batch input/output rates are 50% off (`$2`/`$10`);
cache tariffs are unchanged. Copilot uses Standard API-equivalent estimates,
not subscription charges. Older Opus model rates are unchanged.

Claude Sonnet 5.5 (`claude-sonnet-5-5`, Copilot `claude-sonnet-5.5`) costs `$2`
input, `$10` output, `$2.50` for 5-minute cache writes, `$4` for 1-hour cache
writes, and `$0.20` for cache reads per million tokens. These rates come from the
official Anthropic Claude Sonnet 5.5 overview published September 28, 2026.
Anthropic Batch input/output rates are 50% off (`$1`/`$5`); cache tariffs are
unchanged. Copilot uses Standard API-equivalent estimates, not subscription
charges. Sonnet 5 rates are unchanged.

Claude Haiku 5.5 (`claude-haiku-5-5`, Copilot `claude-haiku-5.5`) is priced by prompt size. For prompts up to
100,000 tokens it costs `$0.10` input, `$0.50` output, `$0.125` for 5-minute cache
writes, `$0.20` for 1-hour cache writes, and `$0.01` for cache reads per million
tokens; above 100,000 prompt tokens these are `$0.50`, `$2.50`, `$0.625`, `$1`, and
`$0.05`. These rates come from the official Anthropic Claude Haiku 5.5 overview
published October 7, 2026. Anthropic Batch input/output rates are 50% off
(`$0.05`/`$0.25` and `$0.25`/`$1.25`); cache tariffs are unchanged. Priority Tier is
not offered for Haiku 5.5. Copilot uses Standard API-equivalent estimates, not
subscription charges. Haiku 4.5 rates
are unchanged.

Grok 4.7 pricing (USD per million tokens, global endpoint):

| Model | Prompt tokens | Input | Cached input | Output |
| --- | --- | --- | --- | --- |
| `grok-4.7` | ≤200,000 | $2.00 | $0.50 | $6.00 |
| `grok-4.7` | >200,000 | $4.00 | $1.00 | $12.00 |
| `grok-4.7-build-fast` (OAuth) | ≤200,000 | $4.00 | $1.00 | $12.00 |
| `grok-4.7-build-fast` (OAuth) | >200,000 | $6.00 | $1.50 | $18.00 |

Source: [xAI pricing](https://docs.x.ai/developers/pricing), checked September
21, 2026. The page says long-context pricing starts when the prompt **exceeds**
200k tokens; existing older-model thresholds are unchanged. Fast uses the
explicit Fast table, which differs from the page's general “twice standard”
claim for long contexts. Fast is not available with public API keys or in Grok
Build's free tier. The US regional endpoint's 10% premium is not represented
by these global-endpoint rules.

GPT-5.6 prompts over 272,000 tokens use long-context rates. Where a provider
does not publish a separate cache-write tariff, cache-write tokens use the
normal input rate. The effective service tier reported by the provider takes
precedence over the requested tier, so Fast requests processed at Standard
speed use Standard rates. Unknown models and incomplete token partitions are
labeled unpriced instead of being counted as free.

Anthropic cache-creation usage includes separate 5-minute and 1-hour token
counts. The calculator applies each tariff when that provider detail is
complete; older usage records without a complete split use the default
5-minute cache-write tariff. Anthropic Vertex and Bedrock calls remain unpriced
because their cloud-platform tariffs are provider-specific.

Hugging Face routes require a catalog rule for the specific
`provider=hf`/`upstream_provider` combination. When that upstream tariff is not
listed, the call remains unpriced rather than inheriting another provider's
rate.

Primary pricing sources:

- [OpenAI API pricing](https://developers.openai.com/api/docs/pricing)
- [GPT-6.1 Terra model pricing](https://developers.openai.com/api/docs/models/gpt-6.1-terra)
  (Terra model and API pricing pages checked October 26, 2026)
- [Anthropic pricing](https://platform.claude.com/docs/en/about-claude/pricing)
- [Kimi K3 pricing](https://platform.kimi.ai/docs/pricing/chat-k3)
- [Z.ai model pricing](https://docs.z.ai/guides/overview/pricing)
- [DeepSeek pricing](https://api-docs.deepseek.com/quick_start/pricing/)
- [xAI models and pricing](https://docs.x.ai/developers/models)
- [Hugging Face inference provider pricing](https://huggingface.co/docs/inference-providers/pricing)
