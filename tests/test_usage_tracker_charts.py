"""Alignment, privacy and rendering tests for usage-tracker ASCII charts."""

from __future__ import annotations

import math
import re
import sys
import unicodedata
from dataclasses import replace
from datetime import date, timedelta
from pathlib import Path

import pytest

PLUGIN_DIR = Path(__file__).resolve().parents[1] / "plugins" / "usage-tracker"
if str(PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(PLUGIN_DIR))

from _usage_tracker import charts  # noqa: E402
from _usage_tracker.models import (  # noqa: E402
    DailyUsagePoint,
    ModelUsageRow,
    PhaseBreakdown,
    PricingUpdateStatus,
    RhythmHeatmap,
    SweepSummary,
    ToolUsageRow,
    UsageReportData,
)

try:  # Rich ships with fast-agent; used to verify the real rendered width.
    from rich.text import Text
except ImportError:  # pragma: no cover - exercised only without fast-agent
    Text = None  # type: ignore[assignment]

WIDTH = charts.CARD_WIDTH
BORDER_CHARS = set("╭╰│├")
BRIEF_MARKUP = re.compile(r"\[/?[^\]]+\]")
END = date(2026, 11, 21)
WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
SLOTS = ("00", "04", "08", "12", "16", "20")


# -----------------------------------------------------------------------------
# Fixture builders
# -----------------------------------------------------------------------------


def _model(
    name: str,
    tokens: int,
    *,
    calls: int = 40,
    cost: float | None = None,
    unpriced: int = 0,
    tps: float | None = 78.0,
    ttft: tuple[float | None, float | None] = (1240.0, 3100.0),
) -> ModelUsageRow:
    inp = int(tokens * 0.82)
    out = tokens - inp
    cache = int(inp * 0.87)
    priced = calls - unpriced
    return ModelUsageRow(
        model=name,
        provider="anthropic",
        calls=calls,
        input_tokens=inp,
        uncached_tokens=inp - cache,
        cache_read_tokens=cache,
        cache_write_tokens=int(inp * 0.03),
        output_tokens=out,
        reasoning_tokens=int(out * 0.3),
        cost_usd=(tokens / 1_000_000 * 1.45) if cost is None else cost,
        priced_calls=priced,
        unpriced_calls=unpriced,
        ttft_p50_ms=ttft[0],
        ttft_p90_ms=ttft[1],
        tps=tps,
        streamed_calls=int(calls * 0.9),
        burst_calls=calls // 10,
    )


def _daily(days: int, scale: int) -> tuple[DailyUsagePoint, ...]:
    points = []
    for i in range(days):
        day = END - timedelta(days=days - 1 - i)
        if days > 3 and i % 7 == 5:  # leave gaps: rest days are not recorded
            continue
        wave = 0.35 + 0.65 * abs(math.sin(i / 3.1))
        spike = 6.0 if i == days // 2 else 1.0
        tokens = int(scale * wave * spike)
        points.append(
            DailyUsagePoint(
                date_iso=day.isoformat(),
                total_tokens=tokens,
                input_tokens=int(tokens * 0.8),
                output_tokens=tokens - int(tokens * 0.8),
                cost_usd=tokens / 1_000_000,
                calls=20,
                sessions=2,
            )
        )
    return tuple(points)


def _rhythm(monster: bool = True, empty: bool = False) -> RhythmHeatmap:
    cells = []
    for r in range(7):
        row = []
        for c in range(6):
            value = 0 if empty else max(0, (r * 3 + c * 5) % 11 - 2) * 1000
            if monster and r == 1 and c == 4:
                value = 900_000
            row.append(value)
        cells.append(tuple(row))
    return RhythmHeatmap(
        timezone_label="UTC",
        window_days=30,
        weekday_labels=WEEKDAYS,
        slot_labels=SLOTS,
        cells=tuple(cells),
    )


def _tools() -> tuple[ToolUsageRow, ...]:
    return (
        ToolUsageRow("read_text_file", 412, 724_000, 1757, 18_000),
        ToolUsageRow("execute", 233, 2_380_000, 10_214, 240_000, process_wait_runs=41),
        ToolUsageRow("edit_file", 188, 90_000, 478, 3_000),
        ToolUsageRow("hc_wait", 21, 1_260_000, 60_000, 250_000, process_wait_runs=21),
        ToolUsageRow("web_search", 9, 45_000, 5_000, 9_000),
    )


def _data(
    *,
    days: int = 30,
    window: str = "30d",
    models: tuple[ModelUsageRow, ...] | None = None,
    scale: int = 5_000_000,
    phases: bool = True,
    tools: tuple[ToolUsageRow, ...] | None = None,
    model_filter: str | None = None,
    notice: str | None = None,
) -> UsageReportData:
    if models is None:
        models = (
            _model("claude-sonnet-4-5-20250929", 92_100_000, calls=812, cost=132.40),
            _model("gpt-5.1-codex", 31_400_000, calls=301, cost=48.10, tps=112.0),
            _model("kimi-k2-thinking", 14_200_000, calls=97, cost=21.70, unpriced=3, tps=41.5),
            _model("haiku-4-5", 8_100_000, calls=64, cost=9.85, tps=164.0),
            _model("qwen3-coder-480b", 1_900_000, calls=8, cost=0.0, unpriced=8, tps=None),
            _model("glm-4.6", 400_000, calls=2, cost=2.55),
        )
    inp = sum(m.input_tokens for m in models)
    out = sum(m.output_tokens for m in models)
    start = END - timedelta(days=days - 1)
    return UsageReportData(
        window_label=window,
        start_date=None if window == "all" else start.isoformat(),
        end_date=END.isoformat(),
        model_filter=model_filter,
        total_calls=sum(m.calls for m in models),
        total_sessions=64,
        active_days=min(days, 21),
        current_streak_days=5,
        longest_streak_days=12,
        input_tokens=inp,
        uncached_tokens=sum(m.uncached_tokens for m in models),
        cache_read_tokens=sum(m.cache_read_tokens for m in models),
        cache_write_tokens=sum(m.cache_write_tokens for m in models),
        output_tokens=out,
        reasoning_tokens=sum(m.reasoning_tokens for m in models),
        total_cost_usd=sum(m.cost_usd for m in models),
        priced_calls=sum(m.priced_calls for m in models),
        unpriced_calls=sum(m.unpriced_calls for m in models),
        models=models,
        daily=_daily(days, scale),
        rhythm=_rhythm(),
        phases=(
            PhaseBreakdown(
                turns=412,
                wall_ms=11_520_000,
                wait_ms=3_720_000,
                stream_ms=4_680_000,
                tools_ms=2_304_000,
                process_ms=806_000,
                other_ms=10_000,
            )
            if phases
            else None
        ),
        tools=_tools() if tools is None else tools,
        pricing_version="2026-11-01",
        upgrade_notice=notice,
    )


def _empty() -> UsageReportData:
    return UsageReportData(
        window_label="7d",
        start_date="2026-11-15",
        end_date="2026-11-21",
        model_filter=None,
        total_calls=0,
        total_sessions=0,
        active_days=0,
        current_streak_days=0,
        longest_streak_days=0,
        input_tokens=0,
        uncached_tokens=0,
        cache_read_tokens=0,
        cache_write_tokens=0,
        output_tokens=0,
        reasoning_tokens=0,
        total_cost_usd=0.0,
        priced_calls=0,
        unpriced_calls=0,
        models=(),
        daily=(),
        rhythm=_rhythm(empty=True),
    )


NASTY_MODELS = (
    _model("[bold]evil[/bold]-model-with-a-really-long-name-v2", 3_000_000, calls=9),
    _model("acme/[red]x[/] \\", 2_000_000, calls=5),
    _model("weird:fire:name:smile:", 1_000_000, calls=3),
    _model("wide-模型-🚀-e\u0301", 500_000, calls=2),
)
NASTY_TOOLS = (
    ToolUsageRow("mcp__[server]__a_very_long_tool_name_that_never_ends", 12, 9_000, 750, 2_000),
    ToolUsageRow("[/]\\", 3, 1_000, 333, 600),
    ToolUsageRow("emoji:thumbs_up:tool", 1, 10, 10, 10),
)


def _single(tokens: int) -> UsageReportData:
    model = _model("solo-model", tokens, calls=1, cost=0.0 if tokens == 0 else None)
    data = _data(days=1, window="today", models=(model,), scale=tokens, tools=())
    return replace(data, daily=_daily(1, tokens), active_days=1)


SCENARIOS: dict[str, UsageReportData] = {
    "rich-30d": _data(),
    "today": _data(days=1, window="today"),
    "7d": _data(days=7, window="7d", notice="Pricing catalog v2 requires fast-agent >= 0.4."),
    "90d": _data(days=90, window="90d", model_filter="claude-sonnet-4-5"),
    "all-400d": _data(days=400, window="all", scale=40_000_000),
    "no-phases": _data(phases=False, tools=()),
    "nasty-names": _data(models=NASTY_MODELS, tools=NASTY_TOOLS, model_filter="[red]x[/red]:fire:"),
    "all-unpriced": _data(
        models=(
            _model("local-llama", 4_000_000, cost=0.0, unpriced=40),
            _model("local-qwen", 1_000_000, cost=0.0, unpriced=40, tps=None),
        )
    ),
    "tokens-0": _single(0),
    "tokens-42": _single(42),
    "tokens-999999": _single(999_999),
    "tokens-12.4B": _single(12_400_000_000),
    "empty": _empty(),
    "many-models": _data(
        models=tuple(_model(f"model-{i:02d}", 10_000_000 // (i + 1)) for i in range(14)),
        tools=tuple(
            ToolUsageRow(f"tool_{i}", i + 1, 1000.0 * (i + 1), 1000, 1000) for i in range(20)
        ),
    ),
}


# -----------------------------------------------------------------------------
# Width helpers
# -----------------------------------------------------------------------------


def _visible(line: str) -> str:
    """The brief's naive strip (valid when no dynamic text contains brackets)."""
    return BRIEF_MARKUP.sub("", line)


def _rich_plain(line: str) -> str:
    assert Text is not None
    return Text.from_markup(line).plain


def _bordered(text: str) -> list[str]:
    lines = [line for line in text.splitlines() if line and line[0] in BORDER_CHARS]
    return lines


def _bordered_color(text: str) -> list[str]:
    # Color lines start with the border style tag.
    return [line for line in text.splitlines() if _visible(line)[:1] in BORDER_CHARS]


def _assert_card(plain: str, colored: str) -> None:
    plain_lines = plain.splitlines()
    bordered = _bordered(plain)
    assert bordered, plain
    assert bordered[0].startswith("╭") and bordered[-1].startswith("╰")
    for line in bordered:
        assert len(line) == WIDTH, f"{len(line)}: {line!r}"
        for ch in line:
            assert unicodedata.east_asian_width(ch) not in ("W", "F"), (ch, line)
            assert not unicodedata.combining(ch), (ch, line)
    # Colour mode carries the identical visible text, line for line.
    colored_lines = colored.splitlines()
    assert len(colored_lines) == len(plain_lines)
    for c_line, p_line in zip(colored_lines, plain_lines, strict=True):
        if Text is not None:
            rendered = Text.from_markup(c_line)
            assert rendered.plain == p_line, (c_line, p_line)
            if p_line[:1] in BORDER_CHARS:
                assert rendered.cell_len == WIDTH, c_line
        # The brief's naive strip is exact whenever the visible text itself has
        # no brackets (literal brackets are escaped and verified via Rich above).
        if p_line[:1] in BORDER_CHARS and not set("[]") & set(p_line):
            assert len(_visible(c_line)) == WIDTH, c_line


def _brackets_in_dynamic(data: UsageReportData) -> bool:
    names = [m.model for m in data.models] + [t.tool_name for t in data.tools]
    names.append(data.model_filter or "")
    return any("[" in name or "]" in name for name in names)


# -----------------------------------------------------------------------------
# Tests
# -----------------------------------------------------------------------------


@pytest.mark.parametrize("name", sorted(SCENARIOS))
@pytest.mark.parametrize("show_cost", [True, False])
def test_usage_card_alignment(name: str, show_cost: bool) -> None:
    data = SCENARIOS[name]
    plain = charts.render_usage_card(data, color=False, show_cost=show_cost)
    colored = charts.render_usage_card(data, color=True, show_cost=show_cost)
    _assert_card(plain, colored)
    if not _brackets_in_dynamic(data):
        # The brief's naive markup strip works whenever names have no brackets.
        for line in _bordered_color(colored):
            assert len(_visible(line)) == WIDTH, line


@pytest.mark.parametrize("name", sorted(SCENARIOS))
@pytest.mark.parametrize("show_cost", [True, False])
def test_share_card_is_plain_fenced_and_aligned(name: str, show_cost: bool) -> None:
    data = SCENARIOS[name]
    share = charts.render_share_card(data, show_cost=show_cost)
    lines = share.splitlines()
    assert lines[0] == "```text" and lines[-1] == "```"
    body = "\n".join(lines[1:-1])
    assert "fast-agent · /stats" in body
    assert body == charts.render_usage_card(
        replace(data, upgrade_notice=None), color=False, show_cost=show_cost
    )
    for line in lines[1:-1]:
        assert line[0] in BORDER_CHARS and len(line) == WIDTH, line
    if not _brackets_in_dynamic(data):
        assert "[" not in share and "]" not in share


@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_perf_card_alignment(name: str) -> None:
    data = SCENARIOS[name]
    _assert_card(
        charts.render_perf_card(data, color=False), charts.render_perf_card(data, color=True)
    )


@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_tools_card_alignment(name: str) -> None:
    data = SCENARIOS[name]
    _assert_card(
        charts.render_tools_card(data, color=False), charts.render_tools_card(data, color=True)
    )


def test_no_cost_hides_every_dollar_figure() -> None:
    for name, data in SCENARIOS.items():
        plain = charts.render_usage_card(data, color=False, show_cost=False)
        share = charts.render_share_card(data, show_cost=False)
        assert "$" not in plain, name
        assert "$" not in share, name
        assert "est. cost" not in plain


def test_cost_kpi_and_partial_pricing_marker() -> None:
    plain = charts.render_usage_card(SCENARIOS["rich-30d"], color=False)
    assert "~$214.60" in plain  # 132.40 + 48.10 + 21.70 + 9.85 + 0 + 2.55
    assert "11 of 1,284 calls unpriced" in plain
    assert "$132.40" in plain
    unpriced = charts.render_usage_card(SCENARIOS["all-unpriced"], color=False)
    assert "n/a" in unpriced and "no pricing data" in unpriced
    assert "$" not in unpriced


def test_usage_card_content() -> None:
    plain = charts.render_usage_card(SCENARIOS["rich-30d"], color=False)
    assert plain.splitlines()[0].startswith("╭─ fast-agent usage · 30d · Oct 23 - Nov 21, 2026 ")
    for needle in ("tokens", "cache hit", "sessions", "models", "rhythm · UTC", "daily tokens"):
        assert needle in plain
    assert "148.1M" in plain or "148.2M" in plain
    assert "78 t/s" in plain
    assert "less ·· ░░ ▒▒ ▓▓ ██ more" in plain
    assert any(block in plain for block in "▁▂▃▄▅▆▇")
    # Model filter badge.
    badge = charts.render_usage_card(SCENARIOS["90d"], color=False).splitlines()[0]
    assert "▸ claude-sonnet-4-5" in badge
    assert "3d per bar" in charts.render_usage_card(SCENARIOS["90d"], color=False)


def test_tail_models_collapse_into_other() -> None:
    plain = charts.render_usage_card(SCENARIOS["many-models"], color=False, max_models=6)
    assert "other (9)" in plain
    assert "model-04" in plain and "model-05" not in plain
    tools = charts.render_tools_card(SCENARIOS["many-models"], color=False, max_tools=12)
    assert "other (9)" in tools


def test_upgrade_notice_is_below_the_card() -> None:
    plain = charts.render_usage_card(SCENARIOS["7d"], color=False)
    lines = plain.splitlines()
    bottom = max(i for i, line in enumerate(lines) if line.startswith("╰"))
    assert any("requires fast-agent" in line for line in lines[bottom + 1 :])
    assert all(len(line) <= WIDTH for line in lines)
    assert "requires fast-agent" not in charts.render_share_card(SCENARIOS["7d"])


def test_empty_state_invites_sweep() -> None:
    plain = charts.render_usage_card(SCENARIOS["empty"], color=False)
    assert "No usage recorded yet" in plain
    assert "/stats sweep <dir>" in plain
    colored = charts.render_usage_card(SCENARIOS["empty"], color=True)
    if Text is not None:
        assert "/stats sweep <dir>" in Text.from_markup(colored).plain


def test_rich_markup_in_names_is_escaped() -> None:
    data = SCENARIOS["nasty-names"]
    colored = charts.render_usage_card(data, color=True)
    if Text is None:
        pytest.skip("rich not installed")
    rendered = Text.from_markup(colored).plain
    assert "[bold]evil" in rendered
    assert "weird:fire:name" in rendered  # emoji codes were not substituted
    assert "[red]x[/red]" in rendered  # filter badge kept literal


def test_privacy_no_paths_or_session_ids() -> None:
    data = replace(SCENARIOS["rich-30d"], window_label="30d")
    for text in (
        charts.render_usage_card(data, color=False),
        charts.render_share_card(data),
        charts.render_perf_card(data, color=False),
        charts.render_tools_card(data, color=False),
    ):
        assert "/home" not in text and "\\Users" not in text


def test_heatmap_scaling_keeps_quiet_slots_visible() -> None:
    levels = charts._heat_levels(_rhythm(monster=True).cells)
    flat = [lvl for row in levels for lvl in row]
    assert max(flat) == 4 and min(flat) == 0
    # With one monster slot, other active slots are still spread over levels 1..3.
    assert len({lvl for lvl in flat if 0 < lvl < 4}) >= 2


def test_sparkline_resamples_to_fit() -> None:
    for days in (1, 7, 30, 90, 400):
        top, bottom, per = charts._sparkline([i % 9 for i in range(days)])
        assert charts._width(top) == charts._width(bottom) <= charts.SPARK_W
        assert per == max(1, math.ceil(days / charts.SPARK_W))


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (0, "0"),
        (42, "42"),
        (999, "999"),
        (999_999, "1.0M"),
        (148_200_000, "148.2M"),
        (12_400_000_000, "12.4B"),
    ],
)
def test_compact_numbers(value: int, expected: str) -> None:
    assert charts._compact(value) == expected


def test_formatters() -> None:
    assert charts._fmt_cost(214.6) == "$214.60"
    assert charts._fmt_cost(12_345) == "$12,345"
    assert charts._fmt_cost(0.001) == "<$0.01"
    assert charts._fmt_dur(850) == "850ms"
    assert charts._fmt_dur(1240) == "1.24s"
    assert charts._fmt_dur(724_000) == "12m 04s"
    assert charts._fmt_dur(11_520_000) == "3h 12m"
    assert charts._fmt_tps(78.4) == "78 t/s"
    assert charts._fmt_pct(0.999) == "99%"


def test_perf_card_content() -> None:
    plain = charts.render_perf_card(SCENARIOS["rich-30d"], color=False)
    for needle in (
        "agent time",
        "3h 12m",
        "412",
        "ttft p50",
        "ttft p90",
        "tok/s",
        "burst",
        "top tools",
        "execute",
        "wait",
        "stream",
        "process",
    ):
        assert needle in plain, needle
    no_phase = charts.render_perf_card(SCENARIOS["no-phases"], color=False)
    assert "No turn timing recorded yet" in no_phase


def test_tools_card_content() -> None:
    plain = charts.render_tools_card(SCENARIOS["rich-30d"], color=False)
    for needle in ("tool time", "runs", "process wait", "execute", "max", "avg"):
        assert needle in plain
    assert "No tool runs recorded yet" in charts.render_tools_card(
        SCENARIOS["no-phases"], color=False
    )


def test_ptc_tools_raw_and_perf_cards_alignment_and_content() -> None:
    from _usage_tracker.models import ProcessWaitSummary, PtcModelRow, PtcSummary

    base = SCENARIOS["rich-30d"]
    ptc_data = UsageReportData(
        window_label=base.window_label,
        start_date=base.start_date,
        end_date=base.end_date,
        model_filter=None,
        total_calls=base.total_calls,
        total_sessions=base.total_sessions,
        active_days=base.active_days,
        current_streak_days=base.current_streak_days,
        longest_streak_days=base.longest_streak_days,
        input_tokens=base.input_tokens,
        uncached_tokens=base.uncached_tokens,
        cache_read_tokens=base.cache_read_tokens,
        cache_write_tokens=base.cache_write_tokens,
        output_tokens=base.output_tokens,
        reasoning_tokens=base.reasoning_tokens,
        total_cost_usd=base.total_cost_usd,
        priced_calls=base.priced_calls,
        unpriced_calls=base.unpriced_calls,
        models=base.models,
        daily=base.daily,
        rhythm=base.rhythm,
        phases=base.phases,
        tools=(
            ToolUsageRow("shell", 42, 84_000.0, 2_800.0, 14_000.0, 0, timed_runs=30),
            ToolUsageRow("sandbox (python)", 12, 600.0, 50.0, 120.0, 0, timed_runs=12),
            ToolUsageRow("edit_file", 5, 0.0, 0.0, 0.0, 0, timed_runs=0),
        ),
        raw_tools=(
            ToolUsageRow("exec_sandbox", 50, 120_000.0, 2_400.0, 20_000.0, 8, timed_runs=50),
        ),
        process_waits=ProcessWaitSummary(
            waits=8,
            timed_waits=6,
            total_ms=60_000.0,
            avg_ms=10_000.0,
            p50_ms=8_000.0,
            p90_ms=19_000.0,
            max_ms=20_000.0,
            by_tool=(("process", 5), ("poll_process", 3)),
        ),
        ptc=PtcSummary(
            cells=50,
            nested_calls=55,
            pure_python_cells=12,
            total_cell_ms=120_000.0,
            error_cells=4,
            syntax_errors=1,
            typecheck_errors=2,
            runtime_errors=1,
            nested_error_calls=2,
            has_parallel_overlap=True,
            unmeasured_nested_calls=17,
            by_model=(
                PtcModelRow("gemini-4-neon", 30, 35, 9, 2, 0, 1, 1),
                PtcModelRow("gpt-6.2-sol", 20, 20, 3, 2, 1, 1, 0),
            ),
        ),
    )

    unwrapped_plain = charts.render_tools_card(ptc_data, color=False)
    unwrapped_rich = charts.render_tools_card(ptc_data, color=True)
    _assert_card(unwrapped_plain, unwrapped_rich)
    for needle in (
        "2m 00s",  # wall-clock tool time from raw_tools, not inflated by parallel nested sum
        "sandbox 50 cells",
        "errors  4 cells",
        "top tools · 3 total · unwrapped",
        "sandbox (python)",
        "* partial time",
        "nested times may exceed wall time",
        "process waits · 8 waits · 1m 00s*",
        "p50 8.00s · p90 19.0s · avg 10.0s · max 20.0s",
        "process (5) · poll_process (3)",
        "sandbox by model",
        "gemini-4-neon",
    ):
        assert needle in unwrapped_plain, needle

    raw_plain = charts.render_tools_card(ptc_data, color=False, raw=True)
    raw_rich = charts.render_tools_card(ptc_data, color=True, raw=True)
    _assert_card(raw_plain, raw_rich)
    assert "▸ raw" in raw_plain.splitlines()[0]
    assert "exec_sandbox" in raw_plain
    assert "sandbox by model" not in raw_plain
    assert "process waits" not in raw_plain

    perf_plain = charts.render_perf_card(ptc_data, color=False)
    perf_rich = charts.render_perf_card(ptc_data, color=True)
    _assert_card(perf_plain, perf_rich)
    assert "top tools · unwrapped" in perf_plain
    assert "* partial time" in perf_plain


def _sweep(dry_run: bool) -> SweepSummary:
    return SweepSummary(
        root_dir="/home/someone/.fast-agent/sessions",
        dry_run=dry_run,
        sessions_scanned=64,
        files_scanned=340,
        files_skipped_unchanged=300,
        files_skipped_invalid=2,
        calls_imported=1204,
        calls_duplicate=14,
        turns_imported=412,
        tool_runs_imported=2341,
        tokens_imported=148_200_000,
        cost_usd_imported=214.6,
        unpriced_calls_imported=3,
    )


@pytest.mark.parametrize("dry_run", [True, False])
def test_sweep_summary(dry_run: bool) -> None:
    summary = _sweep(dry_run)
    plain = charts.render_sweep_summary(summary, color=False)
    _assert_card(plain, charts.render_sweep_summary(summary, color=True))
    assert "/home/someone" not in plain
    assert "148.2M" in plain and "~$214.60" in plain
    assert ("dry run" in plain) is dry_run


@pytest.mark.parametrize("variant", ["ok", "upgrade", "error"])
def test_pricing_status(variant: str) -> None:
    status = PricingUpdateStatus(
        active_version="2026-11-01",
        active_source="cached",
        bundled_version="2026-10-01",
        cached_version="2026-11-01",
        last_checked_at=1_795_000_000.0,
        etag='W/"abc123"',
        upgrade_required=variant == "upgrade",
        upgrade_notice=(
            "Pricing catalog schema fast-agent.pricing/v2 needs a newer plugin; "
            "run `fast-agent plugins update usage-tracker` to keep costs accurate."
            if variant == "upgrade"
            else None
        ),
        auto_update=True,
        pricing_url=charts_url(),
        last_error="HTTP 503 [Service Unavailable]" if variant == "error" else None,
    )
    plain = charts.render_pricing_status(status, color=False)
    _assert_card(plain, charts.render_pricing_status(status, color=True))
    assert "2026-11-01" in plain and "2026-10-01" in plain
    badge = {"ok": "up to date", "upgrade": "upgrade required", "error": "error"}[variant]
    assert badge in plain.splitlines()[0]


def charts_url() -> str:
    from _usage_tracker.models import DEFAULT_PRICING_URL

    return DEFAULT_PRICING_URL


def test_print_samples_for_visual_inspection(capsys: pytest.CaptureFixture[str]) -> None:
    data = SCENARIOS["rich-30d"]
    samples = [
        charts.render_usage_card(data, color=False),
        charts.render_share_card(SCENARIOS["90d"], show_cost=False),
        charts.render_perf_card(data, color=False),
        charts.render_tools_card(data, color=False),
        charts.render_sweep_summary(_sweep(True), color=False),
    ]
    with capsys.disabled():
        print()
        for sample in samples:
            print(sample)
            print()
    assert all(samples)
