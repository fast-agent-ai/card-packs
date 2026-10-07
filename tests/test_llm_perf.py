import importlib.util
import sys
import time
import unittest
from pathlib import Path

PLUGIN = Path(__file__).resolve().parents[1] / "plugins" / "llm-perf" / "llm_perf.py"


def _load_plugin():
    spec = importlib.util.spec_from_file_location("test_llm_perf_plugin", PLUGIN)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


perf = _load_plugin()


def _fast_agent_has_timing() -> bool:
    try:
        from fast_agent.llm.usage_tracking import TurnUsage
    except ImportError:
        return False
    return "timing" in TurnUsage.model_fields


def _call(start, duration_ms, ttft_ms, tokens, model="m"):
    return perf.Call(
        model=model,
        end=start + duration_ms / 1000,
        duration_ms=duration_ms,
        ttft_ms=ttft_ms,
        tokens=tokens,
    )


def _tool(start, duration_ms, process_wait=False):
    return perf.ToolRun("tool", start, duration_ms, process_wait)


def _turn(*calls, tools=()):
    return perf.TurnRecord(tuple(calls), tuple(tools))


def _plain(line) -> str:
    return "".join(text for text, _style in line)


def _tool_loop(calls=69, spike_at=40):
    """Calls each followed by a 10s tool and a 5s process wait; one TTFT spike."""
    loop, tools, clock = [], [], 0.0
    for index in range(calls):
        ttft = 20_000.0 if index == spike_at else 3_000.0 + (index % 5) * 400
        call = _call(clock, ttft + 5_000.0, ttft, 300)
        loop.append(call)
        tools += [
            _tool(call.end, 10_000),
            _tool(call.end + 10, 5_000, process_wait=True),
        ]
        clock = call.end + 15.0
    return _turn(*loop, tools=tools)


SETTINGS = perf.Settings()


class WallTimeTests(unittest.TestCase):
    def test_phases_partition_wall_time_with_tools_and_process_waits(self):
        turn = perf.summarise(
            _turn(
                _call(0, 4_000, 1_000, 300),
                _call(10, 2_000, 500, 150),
                tools=[_tool(4, 3_000), _tool(7, 2_000, process_wait=True)],
            ),
            SETTINGS,
        )

        self.assertEqual(
            {
                "wait": 1_500,
                "stream": 4_500,
                "tools": 3_000,
                "process": 2_000,
                "other": 1_000,
            },
            {phase: round(ms) for phase, ms in turn.phases.items()},
        )
        self.assertAlmostEqual(12_000, turn.wall_ms)

    def test_overlaps_count_once_by_priority(self):
        # A subagent tool spanning a child LLM call: the child's wait/stream win.
        turn = perf.summarise(
            _turn(_call(1, 4_000, 1_000, 300), tools=[_tool(0, 6_000)]), SETTINGS
        )

        self.assertEqual(
            {"wait": 1_000, "stream": 3_000, "tools": 2_000, "process": 0, "other": 0},
            {phase: round(ms) for phase, ms in turn.phases.items()},
        )
        self.assertAlmostEqual(6_000, turn.wall_ms)

    def test_burst_calls_count_as_wait_and_are_excluded_from_tps(self):
        turn = perf.summarise(
            _turn(_call(0, 5_000, 1_000, 400), _call(10, 10_000, 9_990, 3_000)),
            SETTINGS,
        )

        self.assertEqual(100, turn.tps)
        self.assertEqual(1, turn.tps_calls)
        self.assertAlmostEqual(1_000 + 10_000, turn.phases["wait"])
        self.assertEqual((1_000, 9_990), turn.ttfts)

    def test_e2e_tps_includes_every_call_with_tokens(self):
        settings = perf.Settings(tps="e2e")
        turn = perf.summarise(
            _turn(_call(0, 5_000, 1_000, 400), _call(10, 5_000, 4_990, 600)), settings
        )

        self.assertEqual(100, turn.tps)
        self.assertIn("100 tok/s e2e", _plain(perf.format_perf(turn, settings)[0]))

    def test_turn_without_streamed_calls_is_hidden(self):
        self.assertIsNone(perf.summarise(_turn(_call(0, 5_000, None, 400)), SETTINGS))


class LayoutTests(unittest.TestCase):
    def test_single_call_is_one_row(self):
        turn = perf.summarise(_turn(_call(0, 10_000, 2_000, 800)), SETTINGS)

        (row,) = perf.format_perf(turn, SETTINGS)

        self.assertRegex(
            _plain(row),
            r"^Perf  ━{24}  10s · 1 call · wait 2\.0s · stream 8\.0s · 100 tok/s$",
        )

    def test_tool_loop_adds_aligned_ttft_row(self):
        turn = perf.summarise(_tool_loop(), SETTINGS)

        bar_row, ttft_row = perf.format_perf(turn, SETTINGS, width=160)

        self.assertIn("tools 11m30s · process 5m45s", _plain(bar_row))
        self.assertIn("p50 3.8s · p90 4.6s · max 20s · 60 tok/s", _plain(ttft_row))
        self.assertIn(("█", "yellow"), ttft_row, "TTFT spike is highlighted")
        # Graphics share a column, so the text after them starts at the same offset.
        text_column = len("Perf  ") + 24 + 2
        self.assertRegex(_plain(bar_row)[text_column:], r"^27m38s · 69 calls")
        self.assertRegex(_plain(ttft_row)[text_column:], r"^p50 ")

    def test_bar_fills_its_cells_in_phase_order(self):
        turn = perf.summarise(_tool_loop(), SETTINGS)

        segments = perf.bar(turn.phases, 24)

        self.assertEqual(24, sum(len(text) for text, _style in segments))
        styles = [style for _text, style in segments]
        self.assertEqual(
            ["yellow", "green", "bright_blue", "bright_magenta"], styles[:4]
        )

    def test_rows_fit_terminal_width_dropping_smallest_phases_first(self):
        turn = perf.summarise(_tool_loop(), SETTINGS)
        for width in (70, 80, 100, 160):
            rows = perf.format_perf(turn, SETTINGS, session=turn, width=width)
            for row in rows:
                self.assertLessEqual(len(_plain(row)), width, (width, _plain(row)))
        narrow = _plain(perf.format_perf(turn, SETTINGS, width=80)[0])
        self.assertIn("tools 11m30s", narrow)
        self.assertNotIn("wait", narrow)

    def test_sparkline_bins_to_cell_limit_keeping_peaks(self):
        values = [1.0] * 69
        values[40] = 9.0

        cells = perf.sparkline(values, 24)

        self.assertEqual(24, len(cells))
        self.assertEqual(1, sum(1 for glyph, _style in cells if glyph == "█"))


class SettingsTests(unittest.TestCase):
    def test_defaults_and_validation(self):
        self.assertEqual(SETTINGS, perf.Settings.from_config({}))
        self.assertEqual("e2e", perf.Settings.from_config({"tps": "e2e"}).tps)
        for bad in (
            {"tps": "fast"},
            {"session": "yes"},
            {"burst_ms": -1},
            {"burst_ms": True},
        ):
            with self.assertRaises((TypeError, ValueError)):
                perf.Settings.from_config(bad)

    def test_format_ms(self):
        self.assertEqual(
            ["420ms", "4.2s", "42s", "10m03s", "1h02m"],
            [perf.fmt_ms(ms) for ms in (420, 4_200, 42_000, 603_000, 3_720_000)],
        )


def _usage(**kwargs):
    from fast_agent.llm.provider_types import Provider
    from fast_agent.llm.usage_tracking import (
        CompletionTokenUsage,
        PromptTokenUsage,
        TurnUsage,
        UsageSchema,
    )

    return TurnUsage(
        provider=Provider.ANTHROPIC,
        usage_schema=UsageSchema.ANTHROPIC,
        model="claude-opus-5.5",
        prompt=PromptTokenUsage(
            total=1_000, uncached=1_000, cache_read=0, cache_write=0
        ),
        completion=CompletionTokenUsage(total=400),
        **kwargs,
    )


@unittest.skipUnless(
    _fast_agent_has_timing(), "requires fast-agent with InferenceTiming"
)
class LiveUsageTests(unittest.TestCase):
    def test_live_usage_skips_untimed_retry_attempts(self):
        from fast_agent.llm.usage_tracking import InferenceTiming

        timed = _usage(timing=InferenceTiming(duration_ms=5_000, ttft_ms=1_000))

        (call,) = perf.record_from_attempts([_usage(), timed]).calls

        self.assertEqual(
            ("claude-opus-5.5", 400, 1_000), (call.model, call.tokens, call.ttft_ms)
        )


@unittest.skipUnless(importlib.util.find_spec("fast_agent"), "requires fast-agent")
class HistoryTests(unittest.TestCase):
    def test_process_wait_classification(self):
        self.assertTrue(
            perf.is_process_wait("process", {"action": "wait", "wait_sec": 250})
        )
        self.assertTrue(perf.is_process_wait("shell__poll_process", {"wait_sec": 30}))
        self.assertFalse(perf.is_process_wait("process", {"action": "status"}))
        self.assertFalse(perf.is_process_wait("poll_process", {"wait_sec": 0}))
        self.assertFalse(perf.is_process_wait("bash", {"command": "sleep 5"}))

    def test_history_channels_reconstruct_calls_and_tool_runs(self):
        from fast_agent.agents.tool_result_channels import build_tool_result_message
        from fast_agent.llm.response_telemetry import (
            add_timing_channel,
            append_usage_channel,
        )
        from fast_agent.llm.usage_tracking import UsageAccumulator
        from fast_agent.mcp.prompt import Prompt
        from mcp_types import CallToolRequest, CallToolRequestParams, CallToolResult

        def reply(start, *, tool_calls=None):
            accumulator = UsageAccumulator()
            accumulator.add_turn(_usage())
            message = Prompt.assistant("done")
            message.tool_calls = tool_calls
            add_timing_channel(message, start, start + 5, ttft_ms=1_000.0)
            append_usage_channel(message, accumulator)
            return message

        def call(name, **arguments):
            params = CallToolRequestParams(name=name, arguments=arguments)
            return CallToolRequest(method="tools/call", params=params)

        start = time.perf_counter()
        calls = {
            "a": call("process", action="wait", wait_sec=60),
            "b": call("bash", command="ls"),
        }
        results = build_tool_result_message(
            {key: CallToolResult(content=[]) for key in calls},
            tool_timings={
                "a": {"timing_ms": 3_000.0, "transport_channel": None},
                "b": {"timing_ms": 200.0, "transport_channel": None},
            },
        )

        record = perf.record_from_history(
            [
                Prompt.user("hi"),
                reply(start, tool_calls=calls),
                results,
                reply(start + 9),
            ]
        )

        self.assertEqual(2, len(record.calls))
        self.assertEqual(
            [("process", True, 3_000.0), ("bash", False, 200.0)],
            [(tool.name, tool.process_wait, tool.duration_ms) for tool in record.tools],
        )
        turn = perf.summarise(record, SETTINGS)
        self.assertEqual(
            # bash runs inside the process wait; tools outrank process for the overlap.
            {
                "wait": 2_000,
                "stream": 8_000,
                "tools": 200,
                "process": 2_800,
                "other": 1_000,
            },
            {phase: round(ms) for phase, ms in turn.phases.items()},
        )


def _fast_agent_has_turn_messages() -> bool:
    if importlib.util.find_spec("fast_agent") is None:
        return False
    from dataclasses import fields

    from fast_agent.plugins import PluginPostUserTurnContext

    return "turn_messages" in {
        field.name for field in fields(PluginPostUserTurnContext)
    }


def _fast_agent_restores_folds() -> bool:
    return importlib.util.find_spec("fast_agent") is not None and hasattr(
        importlib.import_module("fast_agent.history.process_poll_fold_audit"),
        "restore_process_poll_history",
    )


POLLS = 8
FOLDED_TURN_PHASES = {
    "wait": 8_000,
    "stream": 8_000,
    "tools": 0,
    "process": 240_000,
    "other": 56_000,
}


def _folded_poll_turn():
    """A turn of 30s ``poll_process`` waits, folded by fast-agent's real tool-loop folding.

    Poll ``i`` starts at ``40 * i`` seconds: 1s to first token, 1s streaming, a 30s
    process wait, then 8s until the next poll.
    """
    import json

    from fast_agent.constants import (
        FAST_AGENT_SHELL_PROCESS_METADATA,
        FAST_AGENT_TIMING,
        FAST_AGENT_TOOL_TIMING,
    )
    from fast_agent.history.process_poll_folding import (
        fold_managed_process_poll_history,
    )
    from fast_agent.mcp.prompt_message_extended import PromptMessageExtended
    from fast_agent.types.llm_stop_reason import LlmStopReason
    from mcp_types import (
        CallToolRequest,
        CallToolRequestParams,
        CallToolResult,
        TextContent,
    )

    def channel(payload):
        return [TextContent(type="text", text=json.dumps(payload))]

    history = [
        PromptMessageExtended(
            role="user", content=[TextContent(type="text", text="go")]
        )
    ]
    unfolded_length = 1
    for index in range(1, POLLS + 1):
        call_id = f"call-{index}"
        start = 40.0 * index
        request = PromptMessageExtended(
            role="assistant",
            tool_calls={
                call_id: CallToolRequest(
                    method="tools/call",
                    params=CallToolRequestParams(
                        name="poll_process",
                        arguments={"process_id": "process-1", "wait_sec": 30},
                    ),
                )
            },
            stop_reason=LlmStopReason.TOOL_USE,
            channels={
                FAST_AGENT_TIMING: channel(
                    {
                        "start_time": start,
                        "end_time": start + 2,
                        "duration_ms": 2_000.0,
                        "ttft_ms": 1_000.0,
                    }
                )
            },
        )
        status = "completed" if index == POLLS else "running"
        result = CallToolResult(
            content=[TextContent(type="text", text=f"poll {index}")]
        )
        result.meta = {
            FAST_AGENT_SHELL_PROCESS_METADATA: {
                "process_id": "process-1",
                "process_status": status,
                "process_yield_reason": "deadline"
                if status == "running"
                else "completion",
                "process_elapsed_seconds": index * 30.0,
                "output_line_count": 0,
                "total_output_bytes": 0,
            }
        }
        result_message = PromptMessageExtended(
            role="user",
            tool_results={call_id: result},
            channels={
                FAST_AGENT_TOOL_TIMING: channel({call_id: {"timing_ms": 30_000.0}})
            },
        )
        unfolded_length += 2
        folded = fold_managed_process_poll_history([*history, request], result_message)
        history = (
            [*history, request, result_message]
            if folded is None
            else [*folded.history, folded.tool_message]
        )
    assert len(history) < unfolded_length, "fast-agent did not fold the polls"
    return history


@unittest.skipUnless(
    _fast_agent_restores_folds(), "requires fast-agent fold restoration"
)
class FoldedHistoryTests(unittest.TestCase):
    def test_folded_polls_keep_their_calls_and_process_waits(self):
        turn = perf.summarise(perf.record_from_history(_folded_poll_turn()), SETTINGS)

        self.assertEqual(POLLS, turn.calls)
        self.assertEqual(
            FOLDED_TURN_PHASES, {phase: round(ms) for phase, ms in turn.phases.items()}
        )

    @unittest.skipUnless(
        _fast_agent_has_turn_messages(), "requires post-turn turn_messages"
    )
    def test_post_turn_hook_renders_folded_turn_through_fast_agent_runtime(self):
        import asyncio

        from fast_agent.plugins.models import PluginPostUserTurnSpec
        from fast_agent.plugins.post_user_turn import (
            load_plugin_post_user_turn_handlers,
            run_plugin_post_user_turn,
        )

        handlers = load_plugin_post_user_turn_handlers(
            [PluginPostUserTurnSpec("llm-perf", f"{PLUGIN}:display_perf")]
        )
        displayed: list[str] = []

        asyncio.run(
            run_plugin_post_user_turn(
                handlers,
                agent_name="dev",
                turn_usage=(),
                session_usage=(),
                config={},
                display=displayed.append,
                turn_messages=tuple(_folded_poll_turn()),
            )
        )

        from rich.text import Text

        (output,) = displayed
        plain = Text.from_markup(output).plain
        self.assertIn("5m12s · 8 calls", plain)
        self.assertIn("process 4m00s", plain)


if __name__ == "__main__":
    unittest.main()
