import asyncio
import importlib.util
import json
import sys
import types
import unittest
from pathlib import Path
from types import SimpleNamespace


def _install_fast_agent_stub():
    try:
        import fast_agent.command_actions

        return
    except ImportError:
        pass

    fast_agent = types.ModuleType("fast_agent")
    command_actions = types.ModuleType("fast_agent.command_actions")

    class PluginCommandActionContext:
        pass

    class PluginCommandActionResult:
        def __init__(self, *, markdown=None):
            self.markdown = markdown

    command_actions.PluginCommandActionContext = PluginCommandActionContext
    command_actions.PluginCommandActionResult = PluginCommandActionResult
    fast_agent.command_actions = command_actions
    sys.modules["fast_agent"] = fast_agent
    sys.modules["fast_agent.command_actions"] = command_actions


def _load_plugin():
    _install_fast_agent_stub()
    path = (
        Path(__file__).resolve().parents[1] / "plugins" / "price-calculator" / "price_calculator.py"
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
):
    return SimpleNamespace(
        model=model,
        requested_service_tier=tier,
        service_tier=None,
        cost_usd=cost_usd,
        prompt=SimpleNamespace(
            total=prompt,
            uncached=uncached,
            cache_read=cached,
            cache_write=cache_write,
        ),
        completion=SimpleNamespace(total=output),
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

    def test_gpt_56_long_context_starts_above_272k(self):
        short = self.plugin.calculate_price((_turn("gpt-5.6-sol", prompt=272_000, output=10_000),))
        long = self.plugin.calculate_price((_turn("gpt-5.6-sol", prompt=272_001, output=10_000),))

        self.assertAlmostEqual(272_000 * 5 / 1_000_000 + 0.3, short.usd)
        self.assertAlmostEqual(272_001 * 10 / 1_000_000 + 0.45, long.usd)

    def test_kimi_provider_routes_and_deepseek(self):
        for model in (
            "kimi-k3",
            "moonshotai/kimi-k3",
            "moonshotai/Kimi-K3:fireworks-ai",
            "moonshotai/Kimi-K3:together",
        ):
            with self.subTest(model=model):
                price = self.plugin.calculate_price((_turn(model, prompt=100_000, output=20_000),))
                self.assertAlmostEqual(0.6, price.usd)

        deepseek = self.plugin.calculate_price(
            (_turn("deepseek-v4-flash", prompt=100_000, output=20_000),)
        )
        self.assertAlmostEqual(0.0196, deepseek.usd)

    def test_cached_input_and_cache_write_fallback_rates(self):
        deepseek = self.plugin.calculate_price(
            (
                _turn(
                    "deepseek-v4-flash",
                    prompt=100_000,
                    output=20_000,
                    cached=20_000,
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
        first = _turn("deepseek-v4-flash", prompt=100_000, output=20_000)
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
        self.assertIn("Known subtotal", result.markdown)
        self.assertNotIn("💰", result.markdown)
        self.assertNotIn("🟡", result.markdown)

    def test_cost_command_rolls_up_user_turns_with_subagent_ledgers(self):
        parent = _turn("gpt-5.6-terra", prompt=100_000, output=10_000)
        child = _turn("deepseek-v4-flash", prompt=20_000, output=2_000)
        ctx = SimpleNamespace(
            arguments="",
            usage=None,
            agent_name="dev",
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
        self.assertIn("Ledger rows are already included", result.markdown)
        self.assertNotIn("💰", result.markdown)

    def test_marketplace_registers_plugin(self):
        root = Path(__file__).resolve().parents[1]
        marketplace = json.loads((root / "marketplace.json").read_text(encoding="utf-8"))

        entry = next(
            item for item in marketplace["command_plugins"] if item["name"] == "price-calculator"
        )

        self.assertEqual("plugins/price-calculator", entry["repo_path"])
        self.assertTrue((root / entry["repo_path"] / "plugin.yaml").is_file())


if __name__ == "__main__":
    unittest.main()
