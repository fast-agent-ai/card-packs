import asyncio
import importlib.util
import json
import sys
import types
import unittest
from enum import Enum
from pathlib import Path
from types import SimpleNamespace


class _Turn(SimpleNamespace):
    def model_dump(self, *, mode):
        del mode
        prompt_total = self.prompt.total
        completion_total = self.completion.total
        return {
            "provider": self.provider,
            "upstream_provider": self.upstream_provider,
            "usage_schema": self.usage_schema,
            "model": self.model,
            "prompt": {
                "total": prompt_total,
                "uncached": self.prompt.uncached,
                "cache_read": self.prompt.cache_read,
                "cache_write": self.prompt.cache_write,
                "tool_use": self.prompt.tool_use,
            },
            "completion": {
                "total": completion_total,
                "reasoning": self.completion.reasoning,
            },
            "tool_calls": self.tool_calls,
            "reasoning_effort": self.reasoning_effort,
            "requested_service_tier": self.requested_service_tier,
            "service_tier": self.service_tier,
            "cost_usd": self.cost_usd,
            "timestamp": self.timestamp,
            "raw_usage": self.raw_usage,
            "total": (
                prompt_total + completion_total
                if prompt_total is not None and completion_total is not None
                else None
            ),
        }


def _turn_from_payload(value):
    prompt = value["prompt"]
    completion = value["completion"]
    return _Turn(
        provider=value["provider"],
        upstream_provider=value.get("upstream_provider"),
        usage_schema=value["usage_schema"],
        model=value["model"],
        requested_service_tier=value.get("requested_service_tier"),
        service_tier=value.get("service_tier"),
        cost_usd=value.get("cost_usd"),
        prompt=SimpleNamespace(
            total=prompt.get("total"),
            uncached=prompt.get("uncached"),
            cache_read=prompt.get("cache_read"),
            cache_write=prompt.get("cache_write"),
            tool_use=prompt.get("tool_use"),
        ),
        completion=SimpleNamespace(
            total=completion.get("total"),
            reasoning=completion.get("reasoning"),
        ),
        tool_calls=value.get("tool_calls", 0),
        reasoning_effort=value.get("reasoning_effort"),
        timestamp=value.get("timestamp", 0.0),
        raw_usage=value.get("raw_usage"),
    )


def _install_fast_agent_stub():
    try:
        import fast_agent.command_actions

        return
    except ImportError:
        pass

    fast_agent = types.ModuleType("fast_agent")
    command_actions = types.ModuleType("fast_agent.command_actions")
    constants = types.ModuleType("fast_agent.constants")
    llm = types.ModuleType("fast_agent.llm")
    usage_tracking = types.ModuleType("fast_agent.llm.usage_tracking")
    mcp = types.ModuleType("fast_agent.mcp")
    helpers = types.ModuleType("fast_agent.mcp.helpers")
    content_helpers = types.ModuleType("fast_agent.mcp.helpers.content_helpers")
    fast_agent_types = types.ModuleType("fast_agent.types")

    class PluginCommandActionContext:
        pass

    class PluginCommandActionResult:
        def __init__(self, *, markdown=None, message=None, markdown_styles=()):
            self.markdown = markdown
            self.message = message
            self.markdown_styles = markdown_styles

    class MarkdownTextStyle:
        def __init__(self, *, text, style):
            self.text = text
            self.style = style

    class PluginCommandCompletion:
        def __init__(self, value, *, display=None, detail=None):
            self.value = value
            self.display = display
            self.detail = detail

    class PluginCommandCompletionContext:
        pass

    class UsageReport:
        @classmethod
        def model_validate(cls, payload):
            if payload.get("schema") != "fast-agent.usage/v2":
                raise ValueError("unsupported usage schema")
            attempts = payload.get("provider_attempts")
            if not isinstance(attempts, list) or not attempts:
                raise ValueError("provider_attempts must not be empty")
            return SimpleNamespace(
                provider_attempts=[_turn_from_payload(attempt) for attempt in attempts]
            )

    class LlmStopReason(str, Enum):
        END_TURN = "endTurn"
        TOOL_USE = "toolUse"

    def get_text(block):
        return block.text if block.type == "text" else None

    command_actions.PluginCommandActionContext = PluginCommandActionContext
    command_actions.PluginCommandActionResult = PluginCommandActionResult
    command_actions.MarkdownTextStyle = MarkdownTextStyle
    command_actions.PluginCommandCompletion = PluginCommandCompletion
    command_actions.PluginCommandCompletionContext = PluginCommandCompletionContext
    constants.FAST_AGENT_SYNTHETIC_FINAL_CHANNEL = "fast-agent-synthetic-final"
    constants.FAST_AGENT_USAGE = "fast-agent-usage"
    usage_tracking.UsageReport = UsageReport
    content_helpers.get_text = get_text
    fast_agent_types.LlmStopReason = LlmStopReason
    fast_agent.command_actions = command_actions
    fast_agent.constants = constants
    fast_agent.llm = llm
    fast_agent.mcp = mcp
    fast_agent.types = fast_agent_types
    llm.usage_tracking = usage_tracking
    mcp.helpers = helpers
    helpers.content_helpers = content_helpers
    sys.modules["fast_agent"] = fast_agent
    sys.modules["fast_agent.command_actions"] = command_actions
    sys.modules["fast_agent.constants"] = constants
    sys.modules["fast_agent.llm"] = llm
    sys.modules["fast_agent.llm.usage_tracking"] = usage_tracking
    sys.modules["fast_agent.mcp"] = mcp
    sys.modules["fast_agent.mcp.helpers"] = helpers
    sys.modules["fast_agent.mcp.helpers.content_helpers"] = content_helpers
    sys.modules["fast_agent.types"] = fast_agent_types


def _load_plugin():
    _install_fast_agent_stub()
    path = (
        Path(__file__).resolve().parents[1]
        / "plugins"
        / "price-calculator"
        / "price_calculator.py"
    )
    spec = importlib.util.spec_from_file_location("test_price_calculator_plugin", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _turn(
    model,
    *,
    prompt,
    output,
    cached=0,
    cache_write=0,
    uncached=None,
    tier=None,
    cost_usd=None,
    tool_calls=0,
    timestamp=0.0,
    provider="codexresponses",
    upstream_provider=None,
):
    return _Turn(
        provider=provider,
        upstream_provider=upstream_provider,
        usage_schema="openai-responses",
        model=model,
        requested_service_tier=tier,
        service_tier=None,
        cost_usd=cost_usd,
        prompt=SimpleNamespace(
            total=prompt,
            uncached=uncached,
            cache_read=cached,
            cache_write=cache_write,
            tool_use=None,
        ),
        completion=SimpleNamespace(total=output, reasoning=None),
        tool_calls=tool_calls,
        reasoning_effort=None,
        timestamp=timestamp,
        raw_usage=None,
    )


def _text_block(text):
    try:
        from mcp_types import TextContent

        return TextContent(type="text", text=text)
    except ImportError:
        return SimpleNamespace(type="text", text=text)


def _message(
    role,
    *,
    attempts=(),
    stop_reason=None,
    tool_results=None,
    is_template=False,
    synthetic=False,
):
    channels = {}
    if attempts:
        channels["fast-agent-usage"] = [
            _text_block(
                json.dumps(
                    {
                        "schema": "fast-agent.usage/v2",
                        "provider_attempts": [
                            attempt.model_dump(mode="json") for attempt in attempts
                        ],
                    }
                )
            )
        ]
    if synthetic:
        channels["fast-agent-synthetic-final"] = [
            _text_block("tool_result_passthrough")
        ]
    return SimpleNamespace(
        role=role,
        channels=channels,
        stop_reason=stop_reason,
        tool_results=tool_results,
        is_template=is_template,
    )


class PriceCalculatorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.plugin = _load_plugin()

    def test_gpt_56_flex_cache_partitions(self):
        turn = _turn(
            "gpt-5.6-terra",
            prompt=130_000,
            output=10_000,
            cached=20_000,
            cache_write=10_000,
            tier="flex",
        )

        price = self.plugin.calculate_price((turn,))

        self.assertEqual(0, price.unpriced_calls)
        self.assertAlmostEqual(0.100 + 0.002 + 0.0125 + 0.060, price.usd)

    def test_bundled_pricing_catalog_loads(self):
        catalog_path = (
            Path(__file__).resolve().parents[1]
            / "plugins"
            / "price-calculator"
            / "pricing_catalog.json"
        )

        self.assertTrue(catalog_path.is_file())
        self.assertEqual("2026-08-06.1", self.plugin._PRICING_CATALOG.version)
        self.assertEqual(18, len(self.plugin._PRICING_CATALOG.rules))

    def test_bundled_catalog_does_not_cross_provider_boundaries(self):
        price = self.plugin.calculate_price(
            (
                _turn(
                    "gpt-5.6-terra",
                    prompt=100_000,
                    output=10_000,
                    provider="hf",
                    upstream_provider="fireworks-ai",
                ),
            )
        )

        self.assertEqual(0, price.usd)
        self.assertEqual(1, price.unpriced_calls)

    def test_pricing_catalog_prefers_hf_upstream_specific_rates(self):
        def rule(rule_id, input_rate, *, providers=None, upstream_providers=None):
            match = {"model": {"values": ["shared/model"]}}
            if providers is not None:
                match["providers"] = providers
            if upstream_providers is not None:
                match["upstream_providers"] = upstream_providers
            return {
                "id": rule_id,
                "match": match,
                "rates": {
                    "input": str(input_rate),
                    "cache_read": "0",
                    "output": "0",
                },
            }

        catalog = self.plugin._parse_catalog(
            {
                "schema": "fast-agent.pricing/v1",
                "catalog_version": "test",
                "currency": "USD",
                "unit": "usd_per_million_tokens",
                "rules": [
                    rule("generic", 1),
                    rule("hf", 2, providers=["hf"]),
                    rule(
                        "hf-fireworks",
                        3,
                        providers=["hf"],
                        upstream_providers=["fireworks-ai"],
                    ),
                ],
            }
        )

        fireworks = _turn(
            "shared/model",
            prompt=1,
            output=1,
            provider="hf",
            upstream_provider="fireworks-ai",
        )
        together = _turn(
            "shared/model",
            prompt=1,
            output=1,
            provider="hf",
            upstream_provider="together",
        )
        direct = _turn("shared/model", prompt=1, output=1, provider="openai")

        self.assertEqual(3, catalog.resolve(fireworks).input)
        self.assertEqual(2, catalog.resolve(together).input)
        self.assertEqual(1, catalog.resolve(direct).input)

    def test_pricing_catalog_resolves_effective_dates_and_service_tiers(self):
        def rule(rule_id, input_rate, tier, effective):
            return {
                "id": rule_id,
                "match": {
                    "model": {"values": ["dated-model"]},
                    "service_tiers": [tier],
                },
                "effective": effective,
                "rates": {
                    "input": str(input_rate),
                    "cache_read": "0",
                    "output": "0",
                },
            }

        catalog = self.plugin._parse_catalog(
            {
                "schema": "fast-agent.pricing/v1",
                "catalog_version": "test",
                "currency": "USD",
                "unit": "usd_per_million_tokens",
                "rules": [
                    {
                        "id": "baseline",
                        "match": {"model": {"values": ["dated-model"]}},
                        "rates": {
                            "input": "8",
                            "cache_read": "0",
                            "output": "0",
                        },
                    },
                    rule(
                        "old-standard",
                        4,
                        "standard",
                        {"until": "2026-01-01T00:00:00Z"},
                    ),
                    rule(
                        "new-standard",
                        2,
                        "standard",
                        {"from": "2026-01-01T00:00:00Z"},
                    ),
                    rule(
                        "new-flex",
                        1,
                        "flex",
                        {"from": "2026-01-01T00:00:00Z"},
                    ),
                    {
                        **rule(
                            "new-long-standard",
                            3,
                            "standard",
                            {"from": "2026-01-01T00:00:00Z"},
                        ),
                        "prompt_tokens": {"minimum": 100},
                    },
                ],
            }
        )

        old = _turn("dated-model", prompt=1, output=1, timestamp=0)
        standard = _turn("dated-model", prompt=1, output=1, timestamp=2_000_000_000)
        flex = _turn(
            "dated-model",
            prompt=1,
            output=1,
            tier="flex",
            timestamp=2_000_000_000,
        )
        long_standard = _turn(
            "dated-model",
            prompt=100,
            output=1,
            timestamp=2_000_000_000,
        )

        self.assertEqual(4, catalog.resolve(old).input)
        self.assertEqual(2, catalog.resolve(standard).input)
        self.assertEqual(1, catalog.resolve(flex).input)
        self.assertEqual(3, catalog.resolve(long_standard).input)

    def test_pricing_catalog_rejects_unknown_fields(self):
        with self.assertRaisesRegex(ValueError, "unknown fields"):
            self.plugin._parse_catalog(
                {
                    "schema": "fast-agent.pricing/v1",
                    "catalog_version": "test",
                    "currency": "USD",
                    "unit": "usd_per_million_tokens",
                    "rules": [],
                    "unexpected": True,
                }
            )

    def test_pricing_catalog_rejects_ambiguous_and_unscoped_rules(self):
        root = {
            "schema": "fast-agent.pricing/v1",
            "catalog_version": "test",
            "currency": "USD",
            "unit": "usd_per_million_tokens",
        }
        rule = {
            "match": {"model": {"values": ["model"]}},
            "rates": {"input": "1", "cache_read": "0", "output": "1"},
        }

        with self.assertRaisesRegex(ValueError, "Ambiguous pricing rules"):
            self.plugin._parse_catalog(
                {
                    **root,
                    "rules": [
                        {"id": "one", **rule},
                        {"id": "two", **rule},
                    ],
                }
            )

        with self.assertRaisesRegex(ValueError, "requires match.providers"):
            self.plugin._parse_catalog(
                {
                    **root,
                    "rules": [
                        {
                            "id": "unscoped",
                            **rule,
                            "match": {
                                "model": {"values": ["model"]},
                                "upstream_providers": ["fireworks-ai"],
                            },
                        }
                    ],
                }
            )

    def test_gpt_56_long_context_starts_above_272k(self):
        short = self.plugin.calculate_price(
            (_turn("gpt-5.6-sol", prompt=272_000, output=10_000),)
        )
        long = self.plugin.calculate_price(
            (_turn("gpt-5.6-sol", prompt=272_001, output=10_000),)
        )

        self.assertAlmostEqual(272_000 * 5 / 1_000_000 + 0.3, short.usd)
        self.assertAlmostEqual(272_001 * 10 / 1_000_000 + 0.45, long.usd)

    def test_kimi_provider_routes_and_deepseek(self):
        for model in ("kimi-k3", "moonshotai/kimi-k3"):
            with self.subTest(model=model):
                price = self.plugin.calculate_price(
                    (
                        _turn(
                            model,
                            prompt=100_000,
                            output=20_000,
                            provider="moonshot",
                        ),
                    )
                )
                self.assertAlmostEqual(0.6, price.usd)

        for upstream_provider in ("fireworks-ai", "together"):
            with self.subTest(upstream_provider=upstream_provider):
                price = self.plugin.calculate_price(
                    (
                        _turn(
                            "moonshotai/Kimi-K3",
                            prompt=100_000,
                            output=20_000,
                            provider="hf",
                            upstream_provider=upstream_provider,
                        ),
                    )
                )
                self.assertEqual(1, price.unpriced_calls)

        deepseek = self.plugin.calculate_price(
            (
                _turn(
                    "deepseek-v4-flash",
                    prompt=100_000,
                    output=20_000,
                    provider="deepseek",
                ),
            )
        )
        self.assertAlmostEqual(0.0196, deepseek.usd)

    def test_muse_spark_tier_rates(self):
        for model, expected in (
            ("muse-spark-1.1", 5.28),
            ("metaai.muse-spark-1.2", 5.28),
            ("muse-spark-1.2-contributor", 0.2804),
        ):
            with self.subTest(model=model):
                price = self.plugin.calculate_price(
                    (
                        _turn(
                            model,
                            prompt=1_000_000,
                            output=1_000_000,
                            cached=200_000,
                            provider="metaai",
                        ),
                    )
                )
                self.assertEqual(0, price.unpriced_calls)
                self.assertAlmostEqual(expected, price.usd)

    def test_grok_long_context_rates_start_above_200k(self):
        for model in ("grok-4.3", "xai.grok-4.5"):
            with self.subTest(model=model):
                short = _turn(
                    model,
                    prompt=200_000,
                    output=10_000,
                    cached=50_000,
                    provider="xai",
                )
                long = _turn(
                    model,
                    prompt=200_001,
                    output=10_000,
                    cached=50_000,
                    provider="xai",
                )

                short_price = self.plugin.calculate_price((short,))
                long_price = self.plugin.calculate_price((long,))

                self.assertEqual(0, short_price.unpriced_calls)
                self.assertAlmostEqual(0.375, short_price.usd)
                self.assertEqual("short", self.plugin._context_label(short))
                self.assertEqual(0, long_price.unpriced_calls)
                self.assertAlmostEqual(0.750004, long_price.usd)
                self.assertEqual("long", self.plugin._context_label(long))

    def test_cached_input_and_cache_write_fallback_rates(self):
        deepseek = self.plugin.calculate_price(
            (
                _turn(
                    "deepseek-v4-flash",
                    prompt=100_000,
                    output=20_000,
                    cached=20_000,
                    provider="deepseek",
                ),
            )
        )
        kimi_cached = self.plugin.calculate_price(
            (
                _turn(
                    "kimi-k3",
                    prompt=100_000,
                    output=20_000,
                    cached=20_000,
                    provider="moonshot",
                ),
            )
        )
        kimi_write = self.plugin.calculate_price(
            (
                _turn(
                    "kimi-k3",
                    prompt=100_000,
                    output=20_000,
                    cache_write=20_000,
                    provider="moonshot",
                ),
            )
        )

        self.assertAlmostEqual(0.01684, deepseek.usd)
        self.assertAlmostEqual(0.546, kimi_cached.usd)
        self.assertAlmostEqual(0.6, kimi_write.usd)

    def test_unknown_fast_and_incomplete_partitions_are_unpriced(self):
        turns = (
            _turn("unknown", prompt=1, output=1),
            _turn("gpt-5.6-sol", prompt=1, output=1, tier="fast"),
            _turn(
                "gpt-5.6-sol",
                prompt=100_000,
                output=10_000,
                cached=20_000,
                uncached=50_000,
            ),
        )

        price = self.plugin.calculate_price(turns)

        self.assertEqual(0, price.usd)
        self.assertEqual(3, price.unpriced_calls)

    def test_display_reports_last_and_session(self):
        first = _turn(
            "deepseek-v4-flash",
            prompt=100_000,
            output=20_000,
            provider="deepseek",
        )
        second = _turn("gpt-5.6-luna", prompt=100_000, output=10_000)
        ctx = SimpleNamespace(
            turn_usage=(second,),
            session_usage=(first, second),
        )

        line = asyncio.run(self.plugin.display_cost(ctx))

        self.assertIn("last", line)
        self.assertIn("session", line)
        self.assertNotIn("unpriced", line)

    def test_cost_command_returns_detailed_session_ledger(self):
        first = _turn(
            "gpt-5.6-terra",
            prompt=130_000,
            output=10_000,
            cached=20_000,
            cache_write=10_000,
            tier="flex",
        )
        second = _turn("unknown", prompt=10, output=2)
        ctx = SimpleNamespace(
            arguments="detail",
            usage=None,
            agent_name="dev",
            message_history=(),
            user_turn_usage=(
                SimpleNamespace(agent_name="dev", attempts=(first, second), ledgers=()),
            ),
        )

        result = asyncio.run(self.plugin.cost_breakdown(ctx))

        self.assertIn("### Model cost detail", result.markdown)
        self.assertIn("gpt-5.6-terra", result.markdown)
        self.assertIn("flex", result.markdown)
        self.assertIn("130,000", result.markdown)
        self.assertIn("unpriced", result.markdown)
        self.assertIn(
            "|  | **2** | **Cumulative** |  |  | **130,010** | **20,000 (15%)** | "
            "**10,000** | **10,002** | **$0.1745 + 1 unpriced** |",
            result.markdown,
        )
        self.assertNotIn("Cumulative tokens", result.markdown)
        self.assertNotIn("Known subtotal", result.markdown)
        self.assertNotIn("💰", result.markdown)
        self.assertNotIn("🟡", result.markdown)

    def test_cost_command_rolls_up_user_turns_with_subagent_ledgers(self):
        parent = _turn("gpt-5.6-terra", prompt=100_000, output=10_000)
        child = _turn(
            "deepseek-v4-flash",
            prompt=20_000,
            output=2_000,
            provider="deepseek",
        )
        ctx = SimpleNamespace(
            arguments="",
            usage=None,
            agent_name="dev",
            message_history=(),
            user_turn_usage=(
                SimpleNamespace(
                    agent_name="dev",
                    attempts=(child, parent),
                    ledgers=(SimpleNamespace(label="subagents", attempts=(child,)),),
                ),
            ),
        )

        result = asyncio.run(self.plugin.cost_breakdown(ctx))

        self.assertIn("### Model cost by user turn", result.markdown)
        self.assertIn("`dev` total", result.markdown)
        self.assertIn("subagents (included)", result.markdown)
        self.assertIn("### Model cost by model", result.markdown)
        self.assertIn(
            "| `deepseek-v4-flash` | 1 | 20,000 | 0 (0%) | 2,000 | **$0.003360** |",
            result.markdown,
        )
        self.assertIn(
            "| `gpt-5.6-terra` | 1 | 100,000 | 0 (0%) | 10,000 | **$0.320000** |",
            result.markdown,
        )
        self.assertNotIn("Cache write", result.markdown)
        self.assertEqual(
            [("0%", "red")], [(s.text, s.style) for s in result.markdown_styles]
        )
        self.assertNotIn("🔴", result.markdown)
        self.assertNotIn(" low", result.markdown)
        self.assertIn(
            "|  | **Cumulative** | **2** | **120,000** | **0 (0%)** | "
            "**12,000** | **$0.323360** |",
            result.markdown,
        )
        self.assertNotIn("Cumulative tokens", result.markdown)
        self.assertNotIn("Known subtotal", result.markdown)
        self.assertIn("Ledger rows are already included", result.markdown)
        self.assertNotIn("💰", result.markdown)

    def test_cost_summary_aligns_contextual_currency_precision(self):
        cases = (
            ((6.80, 1.72), ("$6.80", "$1.72", "$8.52")),
            ((6.80, 0.6271), ("$6.8000", "$0.6271", "$7.4271")),
            ((0.02, 0.00336), ("$0.020000", "$0.003360", "$0.023360")),
            ((0.0, 0.0), ("$0.00", "$0.00", "$0.00")),
        )
        for costs, expected in cases:
            with self.subTest(costs=costs):
                turns = tuple(
                    SimpleNamespace(
                        agent_name="dev",
                        attempts=(
                            _turn(
                                "gpt-5.6-terra",
                                prompt=1,
                                output=1,
                                cost_usd=cost,
                            ),
                        ),
                        ledgers=(),
                    )
                    for cost in costs
                )

                markdown = self.plugin.format_turn_rollup(turns)

                row_costs = [
                    line.rsplit(" | ", 1)[-1].removesuffix(" |").strip("*")
                    for line in markdown.splitlines()
                    if line.startswith("| ")
                    and ("`dev` total" in line or "**Cumulative**" in line)
                ]
                self.assertEqual(list(expected), row_costs)

    def test_cost_detail_aligns_to_smallest_displayed_cost(self):
        turns = (
            (1, _turn("gpt-5.6-terra", prompt=1, output=1, cost_usd=6.8)),
            (1, _turn("deepseek-v4-flash", prompt=1, output=1, cost_usd=0.00336)),
        )

        markdown = self.plugin.format_cost_breakdown(turns)

        self.assertIn("**$6.800000**", markdown)
        self.assertIn("**$0.003360**", markdown)
        self.assertIn(
            "|  | **2** | **Cumulative** |  |  | **2** | **0 (0%)** | "
            "**2** | **$6.803360** |",
            markdown,
        )

    def test_cache_percentage_and_optional_write_column(self):
        at_threshold = _turn(
            "gpt-5.6-terra",
            prompt=100,
            output=1,
            cached=50,
            cost_usd=1.0,
        )
        below_threshold = _turn(
            "gpt-5.6-terra",
            prompt=100,
            output=1,
            cached=40,
            cache_write=10,
            cost_usd=1.0,
        )
        without_writes = self.plugin.format_turn_rollup(
            (
                SimpleNamespace(
                    agent_name="dev",
                    attempts=(at_threshold,),
                    ledgers=(),
                ),
            )
        )
        with_writes = self.plugin.format_turn_rollup(
            (
                SimpleNamespace(
                    agent_name="dev",
                    attempts=(at_threshold,),
                    ledgers=(),
                ),
                SimpleNamespace(
                    agent_name="dev",
                    attempts=(below_threshold,),
                    ledgers=(),
                ),
            )
        )
        detail = self.plugin.format_cost_breakdown(
            ((1, at_threshold), (2, below_threshold))
        )

        self.assertNotIn("Cache write", without_writes)
        self.assertIn("50 (50%)", without_writes)
        self.assertNotIn("🔴", without_writes)
        self.assertIn("| Cache read | Cache write | Output |", with_writes)
        self.assertIn("40 (40%) | 10 |", with_writes)
        self.assertIn("| Cache read | Cache write | Output |", detail)
        self.assertIn("40 (40%) | 10 |", detail)
        self.assertIn(
            "|  | **Cumulative** | **2** | **200** | **90 (45%)** | "
            "**10** | **2** | **$2.00** |",
            with_writes,
        )
        self.assertNotIn("🔴", with_writes)
        self.assertNotIn("\x1b", with_writes)
        self.assertNotIn("[red]", with_writes)
        self.assertEqual(
            [("40%", "red"), ("45%", "red")],
            [
                (style.text, style.style)
                for style in self.plugin._low_cache_styles(
                    self.plugin._detail_groups((at_threshold, below_threshold))
                )
            ],
        )

    def test_cache_percentage_uses_aggregate_tokens(self):
        attempts = (
            _turn(
                "gpt-5.6-terra",
                prompt=100,
                output=1,
                cached=100,
                cost_usd=1.0,
            ),
            _turn(
                "gpt-5.6-terra",
                prompt=900,
                output=1,
                cached=0,
                cost_usd=1.0,
            ),
        )

        markdown = self.plugin.format_turn_rollup(
            (SimpleNamespace(agent_name="dev", attempts=attempts, ledgers=()),)
        )

        self.assertIn("100 (10%)", markdown)

    def test_unpriced_cache_write_does_not_claim_fallback_tariff(self):
        overlapping = _turn(
            "kimi-k3",
            prompt=100,
            output=10,
            cached=80,
            cache_write=70,
        )

        markdown = self.plugin.format_cost_breakdown(((1, overlapping),))

        self.assertIn("unpriced", markdown)
        self.assertNotIn("Cache writes use the normal input rate", markdown)

    def test_cost_summary_omits_model_rollup_for_one_model(self):
        attempts = (
            _turn("gpt-5.6-terra", prompt=100_000, output=10_000),
            _turn("gpt-5.6-terra", prompt=50_000, output=5_000),
        )
        turns = (SimpleNamespace(agent_name="dev", attempts=attempts, ledgers=()),)

        markdown = self.plugin.format_turn_rollup(turns)

        self.assertNotIn("### Model cost by model", markdown)

    def test_cost_reconstructs_resumed_tool_loop_from_history(self):
        first = _turn(
            "gpt-5.6-terra",
            prompt=100,
            output=10,
            cost_usd=1.0,
            tool_calls=1,
            timestamp=1.0,
        )
        second = _turn(
            "gpt-5.6-terra",
            prompt=200,
            output=20,
            cost_usd=2.0,
            timestamp=2.0,
        )
        third = _turn(
            "gpt-5.6-luna",
            prompt=300,
            output=30,
            cost_usd=3.0,
            timestamp=3.0,
        )
        ctx = SimpleNamespace(
            arguments="summary",
            usage=None,
            agent_name="dev",
            message_history=(
                _message("user"),
                _message("assistant", attempts=(first,), stop_reason="toolUse"),
                _message("user", tool_results={"call-1": object()}),
                _message("assistant", attempts=(second,), stop_reason="endTurn"),
                _message("user"),
                _message("assistant", attempts=(third,), stop_reason="endTurn"),
            ),
            user_turn_usage=(),
        )

        summary = asyncio.run(self.plugin.cost_breakdown(ctx))
        ctx.arguments = "detail"
        detail = asyncio.run(self.plugin.cost_breakdown(ctx))

        self.assertIn("| 1 | `dev` total | 2 |", summary.markdown)
        self.assertIn("| 2 | `dev` total | 1 |", summary.markdown)
        self.assertIn(
            "|  | **Cumulative** | **3** | **600** | **0 (0%)** | **60** | **$6.00** |",
            summary.markdown,
        )
        self.assertIn("| 1 | 1 | `gpt-5.6-terra`", detail.markdown)
        self.assertIn("| 1 | 2 | `gpt-5.6-terra`", detail.markdown)
        self.assertIn("| 2 | 3 | `gpt-5.6-luna`", detail.markdown)

    def test_cost_reconstruction_keeps_empty_tool_results_in_the_turn(self):
        first = _turn(
            "gpt-5.6-terra",
            prompt=100,
            output=10,
            cost_usd=1.0,
            timestamp=1.0,
        )
        second = _turn(
            "gpt-5.6-terra",
            prompt=200,
            output=20,
            cost_usd=2.0,
            timestamp=2.0,
        )
        turns = self.plugin._reconstruct_history_turns(
            (
                _message("user"),
                _message("assistant", attempts=(first,), stop_reason="toolUse"),
                _message("user", tool_results={}),
                _message("assistant", attempts=(second,), stop_reason="endTurn"),
            ),
            agent_name="dev",
        )

        self.assertEqual(1, len(turns))
        self.assertEqual(2, len(turns[0].attempts))

    def test_cost_reconstruction_ignores_invalid_and_synthetic_usage(self):
        attempt = _turn(
            "gpt-5.6-terra",
            prompt=100,
            output=10,
            cost_usd=1.0,
            timestamp=1.0,
        )
        provider_attempts = [attempt.model_dump(mode="json")]
        tool_use = _message("assistant", attempts=(attempt,), stop_reason="toolUse")
        tool_use.channels["fast-agent-usage"] = [
            _text_block("{"),
            _text_block("[]"),
            _text_block('{"schema": "legacy"}'),
            _text_block('{"schema": "fast-agent.usage/v2", "provider_attempts": []}'),
            _text_block(
                json.dumps(
                    {
                        "schema": "fast-agent.usage/v2",
                        "provider_attempts": provider_attempts,
                    }
                )
            ),
        ]
        turns = self.plugin._reconstruct_history_turns(
            (
                _message("user"),
                tool_use,
                _message("user", tool_results={"call-1": object()}),
                _message(
                    "assistant",
                    attempts=(attempt,),
                    stop_reason="endTurn",
                    synthetic=True,
                ),
            ),
            agent_name="dev",
        )

        self.assertEqual(1, len(turns))
        self.assertEqual(1, len(turns[0].attempts))

    def test_cost_replaces_reconstructed_suffix_with_rich_live_turn(self):
        old = _turn(
            "gpt-5.6-terra",
            prompt=100,
            output=10,
            cost_usd=1.0,
            timestamp=1.0,
        )
        parent_before = _turn(
            "gpt-5.6-terra",
            prompt=200,
            output=20,
            cost_usd=2.0,
            timestamp=2.0,
        )
        child = _turn(
            "gpt-5.6-luna",
            prompt=50,
            output=5,
            cost_usd=0.5,
            timestamp=2.5,
        )
        parent_after = _turn(
            "gpt-5.6-terra",
            prompt=300,
            output=30,
            cost_usd=3.0,
            timestamp=3.0,
        )
        ctx = SimpleNamespace(
            arguments="summary",
            usage=None,
            agent_name="dev",
            message_history=(
                _message("user"),
                _message("assistant", attempts=(old,), stop_reason="endTurn"),
                _message("user"),
                _message("assistant", attempts=(parent_before,), stop_reason="toolUse"),
                _message("user", tool_results={"call-1": object()}),
                _message("assistant", attempts=(parent_after,), stop_reason="endTurn"),
            ),
            user_turn_usage=(
                SimpleNamespace(
                    agent_name="dev",
                    attempts=(parent_before, child, parent_after),
                    ledgers=(SimpleNamespace(label="subagents", attempts=(child,)),),
                ),
            ),
        )

        result = asyncio.run(self.plugin.cost_breakdown(ctx))

        self.assertIn("| 1 | `dev` total | 1 |", result.markdown)
        self.assertIn("| 2 | `dev` total | 3 |", result.markdown)
        self.assertIn("subagents (included)", result.markdown)
        self.assertIn(
            "|  | **Cumulative** | **4** | **650** | **0 (0%)** | "
            "**65** | **$6.5000** |",
            result.markdown,
        )

    def test_cost_merges_interleaved_live_agent_turns_without_duplicates(self):
        first = _turn(
            "gpt-5.6-terra",
            prompt=100,
            output=10,
            cost_usd=1.0,
            timestamp=1.0,
        )
        second = _turn(
            "gpt-5.6-terra",
            prompt=200,
            output=20,
            cost_usd=2.0,
            timestamp=2.0,
        )
        other_agent = _turn(
            "gpt-5.6-luna",
            prompt=400,
            output=40,
            cost_usd=4.0,
            timestamp=1.5,
        )
        ctx = SimpleNamespace(
            arguments="summary",
            usage=None,
            agent_name="alpha",
            message_history=(
                _message("user"),
                _message("assistant", attempts=(first,), stop_reason="endTurn"),
                _message("user"),
                _message("assistant", attempts=(second,), stop_reason="endTurn"),
            ),
            user_turn_usage=(
                SimpleNamespace(agent_name="alpha", attempts=(first,), ledgers=()),
                SimpleNamespace(agent_name="beta", attempts=(other_agent,), ledgers=()),
                SimpleNamespace(agent_name="alpha", attempts=(second,), ledgers=()),
            ),
        )

        result = asyncio.run(self.plugin.cost_breakdown(ctx))

        self.assertIn("| 1 | `alpha` total | 1 |", result.markdown)
        self.assertIn("| 2 | `beta` total | 1 |", result.markdown)
        self.assertIn("| 3 | `alpha` total | 1 |", result.markdown)
        self.assertIn(
            "|  | **Cumulative** | **3** | **700** | **0 (0%)** | **70** | **$7.00** |",
            result.markdown,
        )

    def test_cost_summary_is_explicit_and_default_mode(self):
        turn = _turn("gpt-5.6-terra", prompt=100_000, output=10_000)
        ctx = SimpleNamespace(
            arguments="summary",
            usage=None,
            agent_name="dev",
            message_history=(),
            user_turn_usage=(
                SimpleNamespace(agent_name="dev", attempts=(turn,), ledgers=()),
            ),
        )

        explicit = asyncio.run(self.plugin.cost_breakdown(ctx))
        ctx.arguments = ""
        default = asyncio.run(self.plugin.cost_breakdown(ctx))
        ctx.arguments = "invalid"
        invalid = asyncio.run(self.plugin.cost_breakdown(ctx))

        self.assertEqual(default.markdown, explicit.markdown)
        self.assertEqual("Usage: /cost [summary|detail]", invalid.message)

    def test_cost_mode_completion_marks_summary_as_default(self):
        completions = asyncio.run(
            self.plugin.complete_cost(SimpleNamespace(completed_tokens=()))
        )

        self.assertEqual(
            [
                ("summary", "Cost by user turn (default)"),
                ("detail", "Full provider-call ledger"),
            ],
            [(completion.value, completion.detail) for completion in completions],
        )
        self.assertEqual(
            [],
            asyncio.run(
                self.plugin.complete_cost(
                    SimpleNamespace(completed_tokens=("summary",))
                )
            ),
        )

    def test_marketplace_registers_plugin(self):
        root = Path(__file__).resolve().parents[1]
        marketplace = json.loads(
            (root / "marketplace.json").read_text(encoding="utf-8")
        )

        entry = next(
            item
            for item in marketplace["command_plugins"]
            if item["name"] == "price-calculator"
        )

        self.assertEqual("plugins/price-calculator", entry["repo_path"])
        self.assertTrue((root / entry["repo_path"] / "plugin.yaml").is_file())


if __name__ == "__main__":
    unittest.main()
