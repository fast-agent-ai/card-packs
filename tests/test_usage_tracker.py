"""End-to-end integration tests for the usage-tracker plugin entrypoint."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
PLUGIN_FILE = ROOT / "plugins" / "usage-tracker" / "usage_tracker.py"


def _load_entrypoint(module_name: str = "_test_usage_tracker_entry"):
    spec = importlib.util.spec_from_file_location(module_name, PLUGIN_FILE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def _has_fast_agent() -> bool:
    return importlib.util.find_spec("fast_agent") is not None


def _make_attempt(
    *,
    model: str = "gpt-6.2-sol",
    provider: str = "openai",
    timestamp: float | None = None,
    prompt_total: int = 10_000,
    cache_read: int = 8_000,
    cache_write: int = 0,
    completion_total: int = 1_000,
    reasoning: int = 200,
    duration_ms: float = 5_000.0,
    ttft_ms: float = 1_000.0,
    service_tier: str | None = "standard",
):
    from fast_agent.llm.provider_types import Provider
    from fast_agent.llm.usage_tracking import (
        CompletionTokenUsage,
        InferenceTiming,
        PromptTokenUsage,
        TurnUsage,
        UsageSchema,
    )

    ts = time.time() if timestamp is None else timestamp
    return TurnUsage(
        provider=Provider(provider),
        usage_schema=UsageSchema.OPENAI_RESPONSES,
        model=model,
        prompt=PromptTokenUsage(
            total=prompt_total,
            uncached=prompt_total - cache_read - cache_write,
            cache_read=cache_read,
            cache_write=cache_write,
        ),
        completion=CompletionTokenUsage(total=completion_total, reasoning=reasoning),
        service_tier=service_tier,
        timestamp=ts,
        timing=InferenceTiming(duration_ms=duration_ms, ttft_ms=ttft_ms),
    )


def _make_turn_messages(attempt, *, tool_name: str = "bash", tool_ms: float = 1_500.0):
    from mcp_types import CallToolRequest, CallToolRequestParams, CallToolResult, TextContent

    from fast_agent.mcp.prompt_message_extended import PromptMessageExtended

    usage_payload = {
        "schema": "fast-agent.usage/v2",
        "provider_attempts": [attempt.model_dump(mode="json")],
    }
    timing_start = attempt.timestamp - (attempt.timing.duration_ms / 1000.0)
    timing_payload = {
        "start_time": timing_start,
        "end_time": attempt.timestamp,
        "duration_ms": attempt.timing.duration_ms,
        "ttft_ms": attempt.timing.ttft_ms,
    }
    tool_timing_payload = {
        "call-1": {
            "timing_ms": tool_ms,
            "transport_channel": "local",
        }
    }
    user_msg = PromptMessageExtended(
        role="user",
        content=[TextContent(type="text", text="Run tests")],
    )
    assistant_msg = PromptMessageExtended(
        role="assistant",
        content=[TextContent(type="text", text="Running...")],
        tool_calls={
            "call-1": CallToolRequest(
                method="tools/call",
                params=CallToolRequestParams(name=tool_name, arguments={}),
            )
        },
        channels={
            "fast-agent-usage": [TextContent(type="text", text=json.dumps(usage_payload))],
            "fast-agent-timing": [TextContent(type="text", text=json.dumps(timing_payload))],
        },
    )
    tool_result_msg = PromptMessageExtended(
        role="user",
        content=[],
        tool_results={
            "call-1": CallToolResult(
                content=[TextContent(type="text", text="ok")],
                isError=False,
            )
        },
        channels={
            "fast-agent-tool-timing": [
                TextContent(type="text", text=json.dumps(tool_timing_payload))
            ]
        },
    )
    return (user_msg, assistant_msg, tool_result_msg)


def _make_command_ctx(
    arguments: str,
    *,
    db_path: Path,
    data_dir: Path,
    is_tui: bool = True,
    message_history: list | None = None,
    user_turn_usage: tuple = (),
    session_cwd: Path | None = None,
    extra_config: dict | None = None,
):
    cfg: dict[str, object] = {
        "db_path": str(db_path),
        "data_dir": str(data_dir),
        "pricing_auto_update": False,
    }
    if extra_config:
        cfg.update(extra_config)
    settings = SimpleNamespace(plugins=SimpleNamespace(config={"usage-tracker": cfg}))
    return SimpleNamespace(
        command_name="stats",
        arguments=arguments,
        agent_name="agent",
        message_history=message_history or [],
        user_turn_usage=user_turn_usage,
        usage=None,
        settings=settings,
        session_cwd=session_cwd,
        is_tui=is_tui,
        is_acp=not is_tui,
    )


@unittest.skipUnless(_has_fast_agent(), "fast-agent is not installed")
class UsageTrackerIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.mod = _load_entrypoint()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.data_dir = self.root / "data"
        self.db_path = self.data_dir / "usage.sqlite3"

    def test_post_user_turn_records_and_displays(self):
        from fast_agent.plugins import PluginPostUserTurnContext

        now = time.time()
        attempt = _make_attempt(timestamp=now)
        messages = _make_turn_messages(attempt, tool_name="ripgrep", tool_ms=800.0)
        ctx = PluginPostUserTurnContext(
            plugin_name="usage-tracker",
            agent_name="agent",
            turn_usage=(attempt,),
            session_usage=(attempt,),
            config={
                "db_path": str(self.db_path),
                "data_dir": str(self.data_dir),
                "pricing_auto_update": False,
            },
            turn_messages=messages,
            session_id="2611211000-AbCdEf",
        )

        with mock.patch.object(
            self.mod.pricing, "report_session_cost_to_herdr", new=mock.AsyncMock()
        ):
            out = asyncio.run(self.mod.on_post_user_turn(ctx))

        self.assertIsNotNone(out)
        self.assertIn("Cost:", out.display)
        self.assertIn("Perf", out.display)
        self.assertIsNotNone(out.session_usage)

        # Verify SQLite recorded call, turn phase, and tool run
        report = self.mod.store.query_report(self.db_path, window="7d")
        self.assertEqual(report.total_calls, 1)
        self.assertEqual(report.total_sessions, 1)
        self.assertGreater(report.total_cost_usd, 0.0)
        self.assertIsNotNone(report.phases)
        self.assertEqual(len(report.tools), 1)
        self.assertEqual(report.tools[0].tool_name, "ripgrep")

    def test_post_user_turn_respects_display_toggles(self):
        from fast_agent.plugins import PluginPostUserTurnContext

        attempt = _make_attempt()
        messages = _make_turn_messages(attempt)
        ctx = PluginPostUserTurnContext(
            plugin_name="usage-tracker",
            agent_name="agent",
            turn_usage=(attempt,),
            session_usage=(attempt,),
            config={
                "db_path": str(self.db_path),
                "data_dir": str(self.data_dir),
                "pricing_auto_update": False,
                "show_cost": False,
                "show_perf": False,
            },
            turn_messages=messages,
            session_id="silent-sess",
        )
        out = asyncio.run(self.mod.on_post_user_turn(ctx))
        # Quiet display still records to SQLite and preserves OSC session_usage token
        self.assertIsNone(out.display)
        report = self.mod.store.query_report(self.db_path, window="7d")
        self.assertEqual(report.total_calls, 1)

    def test_usage_commands_and_views(self):
        from fast_agent.plugins import PluginPostUserTurnContext

        attempt = _make_attempt(model="gpt-6.2-sol", prompt_total=20_000, completion_total=2_000)
        messages = _make_turn_messages(attempt, tool_name="execute", tool_ms=2_200.0)
        turn_ctx = PluginPostUserTurnContext(
            plugin_name="usage-tracker",
            agent_name="agent",
            turn_usage=(attempt,),
            session_usage=(attempt,),
            config={
                "db_path": str(self.db_path),
                "data_dir": str(self.data_dir),
                "pricing_auto_update": False,
            },
            turn_messages=messages,
            session_id="sess-views",
        )
        with mock.patch.object(
            self.mod.pricing, "report_session_cost_to_herdr", new=mock.AsyncMock()
        ):
            asyncio.run(self.mod.on_post_user_turn(turn_ctx))

        # /stats (TUI colored)
        res = asyncio.run(
            self.mod.stats_command(
                _make_command_ctx("7d", db_path=self.db_path, data_dir=self.data_dir, is_tui=True)
            )
        )
        self.assertIsNotNone(res.message)
        self.assertIn("gpt-6.2-sol", res.message)

        # /stats share --no-cost (markdown code fence, no $)
        share = asyncio.run(
            self.mod.stats_command(
                _make_command_ctx(
                    "share 30d --no-cost",
                    db_path=self.db_path,
                    data_dir=self.data_dir,
                    is_tui=True,
                )
            )
        )
        self.assertIsNotNone(share.markdown)
        self.assertTrue(share.markdown.startswith("```text\n"))
        self.assertNotIn("$", share.markdown)

        # /stats perf and /stats tools
        perf_res = asyncio.run(
            self.mod.stats_command(
                _make_command_ctx("perf 7d --plain", db_path=self.db_path, data_dir=self.data_dir)
            )
        )
        self.assertIn("fast-agent perf", perf_res.message)
        self.assertIn("execute", perf_res.message)

        tools_res = asyncio.run(
            self.mod.stats_command(
                _make_command_ctx("tools 7d --plain", db_path=self.db_path, data_dir=self.data_dir)
            )
        )
        self.assertIn("fast-agent tools", tools_res.message)
        self.assertIn("execute", tools_res.message)

        # /cost and /performance on current session
        cost_res = asyncio.run(
            self.mod.cost_command(
                _make_command_ctx(
                    "summary",
                    db_path=self.db_path,
                    data_dir=self.data_dir,
                    message_history=list(messages),
                )
            )
        )
        self.assertIsNotNone(cost_res.markdown)
        self.assertIn("Cumulative", cost_res.markdown)

        perf_sess = asyncio.run(
            self.mod.performance_command(
                _make_command_ctx(
                    "summary",
                    db_path=self.db_path,
                    data_dir=self.data_dir,
                    message_history=list(messages),
                )
            )
        )
        self.assertIsNotNone(perf_sess.markdown)
        self.assertIn("Cumulative", perf_sess.markdown)

    def test_usage_sweep_and_reprice(self):
        # Build a synthetic session dir on disk
        sessions_root = self.root / "projects"
        sess_dir = sessions_root / "app" / ".fast-agent" / "sessions" / "2611201000-Test01"
        sess_dir.mkdir(parents=True)

        attempt = _make_attempt(model="gpt-6.2-sol", prompt_total=12_000, completion_total=1_500)
        messages = _make_turn_messages(attempt, tool_name="edit_file", tool_ms=400.0)
        (sess_dir / "session.json").write_text(
            json.dumps(
                {
                    "schema_version": 7,
                    "session_id": "2611201000-Test01",
                    "continuation": {"agents": {"agent": {"history_file": "history_agent.json"}}},
                }
            ),
            encoding="utf-8",
        )
        (sess_dir / "history_agent.json").write_text(
            json.dumps({"messages": [m.model_dump(mode="json") for m in messages]}),
            encoding="utf-8",
        )

        # Dry run first
        dry = asyncio.run(
            self.mod.stats_command(
                _make_command_ctx(
                    f"sweep {sessions_root} --dry-run --plain",
                    db_path=self.db_path,
                    data_dir=self.data_dir,
                )
            )
        )
        self.assertIn("dry run", dry.message.casefold())
        self.assertEqual(self.mod.store.query_report(self.db_path, window="all").total_calls, 0)

        # Real sweep
        real = asyncio.run(
            self.mod.stats_command(
                _make_command_ctx(
                    f"sweep {sessions_root} --plain",
                    db_path=self.db_path,
                    data_dir=self.data_dir,
                )
            )
        )
        self.assertIn("fast-agent sweep", real.message)
        report = self.mod.store.query_report(self.db_path, window="all")
        self.assertEqual(report.total_calls, 1)
        self.assertEqual(report.tools[0].tool_name, "edit_file")

        # Pricing status & reprice command
        status_res = asyncio.run(
            self.mod.stats_command(
                _make_command_ctx(
                    "pricing status --plain",
                    db_path=self.db_path,
                    data_dir=self.data_dir,
                )
            )
        )
        self.assertIn("fast-agent pricing", status_res.message)

        reprice_res = asyncio.run(
            self.mod.stats_command(
                _make_command_ctx("reprice", db_path=self.db_path, data_dir=self.data_dir)
            )
        )
        self.assertIn("Repriced 0", reprice_res.message)

    def test_completers(self):
        ctx_empty = SimpleNamespace(completed_tokens=(), current_token="")
        items = asyncio.run(self.mod.complete_stats(ctx_empty))
        values = {item.value for item in items}
        self.assertTrue(
            {"7d", "30d", "share", "perf", "tools", "sweep", "pricing"}.issubset(values)
        )

        tools_items = asyncio.run(
            self.mod.complete_stats(SimpleNamespace(completed_tokens=("tools",), current_token=""))
        )
        self.assertIn("--raw", {item.value for item in tools_items})

        perf_items = asyncio.run(
            self.mod.complete_stats(SimpleNamespace(completed_tokens=("perf",), current_token=""))
        )
        self.assertNotIn("--raw", {item.value for item in perf_items})

        sweep_items = asyncio.run(
            self.mod.complete_stats(SimpleNamespace(completed_tokens=("sweep",), current_token=""))
        )
        self.assertTrue({"--dry-run", "--all"}.issubset({item.value for item in sweep_items}))

        cost_items = asyncio.run(self.mod.complete_cost(ctx_empty))
        self.assertEqual([i.value for i in cost_items], ["summary", "detail"])

        perf_items = asyncio.run(self.mod.complete_performance(ctx_empty))
        self.assertEqual([i.value for i in perf_items], ["summary", "detail"])

    def test_usage_tools_raw_flag(self):
        # Record a turn with a PTC exec_sandbox cell containing a nested shell call.
        history = [
            {
                "role": "assistant",
                "channels": {
                    "fast-agent-timing": [
                        {
                            "type": "text",
                            "text": json.dumps(
                                {
                                    "start_time": 1.0,
                                    "end_time": 2.0,
                                    "duration_ms": 1000.0,
                                }
                            ),
                        }
                    ],
                    "fast-agent-usage": [
                        {
                            "type": "text",
                            "text": json.dumps(
                                {
                                    "schema": "fast-agent.usage/v2",
                                    "provider_attempts": [{"model": "gemini-4-neon"}],
                                }
                            ),
                        }
                    ],
                },
                "tool_calls": {
                    "c1": {"params": {"name": "exec_sandbox", "arguments": {"code": "shell()"}}}
                },
            },
            {
                "role": "user",
                "channels": {
                    "fast-agent-tool-timing": [
                        {"type": "text", "text": json.dumps({"c1": {"timing_ms": 500.0}})}
                    ]
                },
                "tool_results": {
                    "c1": {
                        "isError": False,
                        "content": [{"type": "text", "text": "ok"}],
                        "_meta": {
                            "fast-agent-ptc-calls": [
                                {
                                    "tool": "shell",
                                    "arguments": {"command": "ls"},
                                    "status": "completed",
                                    "start_offset_ms": 0.0,
                                    "timing_ms": 480.0,
                                }
                            ]
                        },
                    }
                },
            },
        ]
        turn_record = self.mod.perf.record_from_history(history)
        catalog, _ = self.mod.pricing_updater.get_active_catalog(self.data_dir)
        with self.mod.store.UsageStore(self.db_path) as usage_store:
            usage_store.record_turn(
                session_id="s-raw",
                attempts=(_make_attempt(model="gemini-4-neon"),),
                turn_record=turn_record,
                catalog=catalog,
            )

        unwrapped = asyncio.run(
            self.mod.stats_command(
                _make_command_ctx("tools all --plain", db_path=self.db_path, data_dir=self.data_dir)
            )
        )
        self.assertIn("unwrapped", unwrapped.message)
        self.assertIn("shell", unwrapped.message)
        self.assertIn("sandbox by model", unwrapped.message)

        raw = asyncio.run(
            self.mod.stats_command(
                _make_command_ctx(
                    "tools all --raw --plain", db_path=self.db_path, data_dir=self.data_dir
                )
            )
        )
        self.assertIn("▸ raw", raw.message)
        self.assertIn("exec_sandbox", raw.message)
        self.assertNotIn("sandbox by model", raw.message)

        bad = asyncio.run(
            self.mod.stats_command(
                _make_command_ctx("perf --raw", db_path=self.db_path, data_dir=self.data_dir)
            )
        )
        self.assertIn("Unknown option: --raw", bad.message)


if __name__ == "__main__":
    unittest.main()
