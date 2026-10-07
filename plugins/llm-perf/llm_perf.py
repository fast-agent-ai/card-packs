"""End-of-turn LLM latency and throughput from canonical fast-agent usage."""

from __future__ import annotations

import json
import math
import shutil
import statistics
from collections import defaultdict
from dataclasses import dataclass, fields
from itertools import pairwise
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, Sequence

    from fast_agent.command_actions import (
        PluginCommandActionContext,
        PluginCommandActionResult,
    )
    from fast_agent.llm.usage_tracking import TurnUsage
    from fast_agent.plugins import PluginPostUserTurnContext
    from fast_agent.types import PromptMessageExtended

_SPARK = "▁▂▃▄▅▆▇█"
_SPIKE_FACTOR = 2.0
_GRAPH_CELLS = 24
_LABEL_WIDTH = 6
_COMPACT_CALLS = 3
_COVERAGE_NOTE_BELOW = 0.8
_SHOW_OTHER_MS = 1000.0

type Phase = Literal["wait", "stream", "tools", "process", "other"]
PHASES: tuple[Phase, ...] = ("wait", "stream", "tools", "process", "other")
_PRIORITY: tuple[Phase, ...] = ("stream", "wait", "tools", "process")
"""Overlapping spans count once, as the first active phase in this order."""
_STYLES: dict[Phase, str] = {
    "wait": "yellow",
    "stream": "green",
    "tools": "bright_blue",
    "process": "bright_magenta",
    "other": "dim",
}
_GLYPHS: dict[Phase, str] = {"other": "─"}

type TpsMode = Literal["decode", "e2e"]
_TPS_MODES: dict[str, TpsMode] = {"decode": "decode", "e2e": "e2e"}
type Segment = tuple[str, str | None]
"""Plain text and an optional Rich style; widths are counted on the text."""


# --------------------------------------------------------------------------- settings


@dataclass(frozen=True, slots=True)
class Settings:
    session: bool = False
    tps: TpsMode = "decode"
    burst_ms: float = 500.0
    min_duration_ms: float = 500.0

    @classmethod
    def from_config(cls, config: Mapping[str, object]) -> Settings:
        defaults = cls()
        session = config.get("session", defaults.session)
        if not isinstance(session, bool):
            raise TypeError("llm-perf: 'session' must be true or false")
        tps = _TPS_MODES.get(str(config.get("tps", defaults.tps)))
        if tps is None:
            raise ValueError("llm-perf: 'tps' must be 'decode' or 'e2e'")
        return cls(
            session=session,
            tps=tps,
            burst_ms=_ms_setting(config, "burst_ms", defaults.burst_ms),
            min_duration_ms=_ms_setting(
                config, "min_duration_ms", defaults.min_duration_ms
            ),
        )


def _ms_setting(config: Mapping[str, object], name: str, default: float) -> float:
    value = _number(config.get(name, default))
    if value is None or value < 0:
        raise ValueError(f"llm-perf: '{name}' must be a non-negative number")
    return value


# --------------------------------------------------------------------------- turn model


@dataclass(frozen=True, slots=True)
class Call:
    """One timed LLM request: the final provider attempt plus client-observed timing."""

    model: str
    end: float
    """Request end in seconds; only compared with spans from the same turn."""
    duration_ms: float
    ttft_ms: float | None
    tokens: int | None

    @property
    def start(self) -> float:
        return self.end - self.duration_ms / 1000

    @property
    def first_activity_ms(self) -> float:
        """Time to first streamed activity; the whole call when nothing streamed."""
        if self.ttft_ms is None:
            return self.duration_ms
        return min(self.ttft_ms, self.duration_ms)

    def gen_ms(self, burst_ms: float) -> float | None:
        """Streaming time, or ``None`` when the output arrived in a single burst."""
        if self.ttft_ms is None:
            return None
        gen = self.duration_ms - self.ttft_ms
        return gen if gen >= burst_ms else None

    def tps(self, burst_ms: float) -> float | None:
        gen = self.gen_ms(burst_ms)
        return self.tokens / gen * 1000 if gen and self.tokens else None


@dataclass(frozen=True, slots=True)
class ToolRun:
    """One tool execution; ``process_wait`` marks blocking waits on managed processes."""

    name: str
    start: float
    duration_ms: float
    process_wait: bool

    @property
    def end(self) -> float:
        return self.start + self.duration_ms / 1000


@dataclass(frozen=True, slots=True)
class TurnRecord:
    calls: tuple[Call, ...]
    tools: tuple[ToolRun, ...] = ()


def record_from_attempts(attempts: Iterable[TurnUsage]) -> TurnRecord:
    """Timed calls from canonical usage; untimed attempts (failed retries) are skipped."""
    return TurnRecord(
        calls=tuple(
            Call(
                model=attempt.model,
                end=attempt.timestamp,
                duration_ms=attempt.timing.duration_ms,
                ttft_ms=attempt.timing.ttft_ms,
                tokens=attempt.completion.total,
            )
            for attempt in attempts
            if attempt.timing is not None
        )
    )


def is_process_wait(name: str, arguments: Mapping[str, object]) -> bool:
    from fast_agent.utils.tool_names import (
        POLL_PROCESS_TOOL_NAME,
        PROCESS_TOOL_NAME,
        matches_tool_name,
    )

    if matches_tool_name(name, POLL_PROCESS_TOOL_NAME):
        wait_sec = _number(arguments.get("wait_sec"))
        return wait_sec is not None and wait_sec > 0
    return (
        matches_tool_name(name, PROCESS_TOOL_NAME) and arguments.get("action") == "wait"
    )


def record_from_history(messages: Sequence[PromptMessageExtended]) -> TurnRecord:
    """Calls and tool runs from persisted timing channels.

    Folded process polling is restored first, so each poll keeps its own LLM call and
    process wait. Tool runs start when the requesting assistant message ended
    (parallel calls start together).
    """
    from fast_agent.constants import (
        FAST_AGENT_TIMING,
        FAST_AGENT_TOOL_TIMING,
        FAST_AGENT_USAGE,
    )
    from fast_agent.llm.usage_tracking import UsageReport

    calls: list[Call] = []
    tools: list[ToolRun] = []
    pending: dict[str, tuple[str, bool, float]] = {}
    for message in _restore_folds(messages):
        if message.role == "user":
            timings = _channel_json(message, FAST_AGENT_TOOL_TIMING)
            if not isinstance(timings, dict):
                continue
            for call_id, info in timings.items():
                request = pending.pop(call_id, None)
                duration_ms = (
                    _number(info.get("timing_ms")) if isinstance(info, dict) else None
                )
                if request is not None and duration_ms is not None:
                    name, process_wait, start = request
                    tools.append(ToolRun(name, start, duration_ms, process_wait))
            continue
        timing = _channel_json(message, FAST_AGENT_TIMING)
        if not isinstance(timing, dict):
            continue
        duration_ms = _number(timing.get("duration_ms"))
        end_time = _number(timing.get("end_time"))
        if duration_ms is None or end_time is None:
            continue
        for call_id, request in (message.tool_calls or {}).items():
            name = request.params.name
            process_wait = is_process_wait(name, request.params.arguments or {})
            pending[call_id] = (name, process_wait, end_time)
        model, tokens = "unknown", None
        try:
            final = UsageReport.model_validate(_channel_json(message, FAST_AGENT_USAGE))
        except ValueError:
            pass
        else:
            model, tokens = (
                final.final_attempt.model,
                final.final_attempt.completion.total,
            )
        calls.append(
            Call(
                model=model,
                end=end_time,
                duration_ms=duration_ms,
                ttft_ms=_number(timing.get("ttft_ms")),
                tokens=tokens,
            )
        )
    return TurnRecord(calls=tuple(calls), tools=tuple(tools))


def _restore_folds(
    messages: Sequence[PromptMessageExtended],
) -> Sequence[PromptMessageExtended]:
    try:
        from fast_agent.history.process_poll_fold_audit import (
            restore_process_poll_history,
        )
    except ImportError:  # fast-agent releases before fold restoration
        return messages
    try:
        return restore_process_poll_history(messages)
    except ValueError:  # damaged fold audit in persisted history: use it as stored
        return messages


def _channel_json(message: PromptMessageExtended, channel: str) -> object:
    from fast_agent.mcp.helpers.content_helpers import get_text

    blocks = (message.channels or {}).get(channel) or []
    text = get_text(blocks[0]) if blocks else None
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


# --------------------------------------------------------------------------- aggregation


@dataclass(frozen=True, slots=True)
class Perf:
    calls: int
    wall_ms: float
    phases: Mapping[Phase, float]
    """Wall time attributed to each phase; overlaps resolved by priority, sums to wall."""
    ttfts: tuple[float, ...]
    """Per-call time to first activity, in call order."""
    tps: float | None
    tps_calls: int

    def ttft(self, quantile: float) -> float:
        ordered = sorted(self.ttfts)
        return ordered[max(0, math.ceil(quantile * len(ordered)) - 1)]


def _spans(record: TurnRecord, burst_ms: float) -> list[tuple[float, float, Phase]]:
    spans: list[tuple[float, float, Phase]] = []
    for call in record.calls:
        split = call.end - (call.gen_ms(burst_ms) or 0.0) / 1000
        spans.append((call.start, split, "wait"))
        if split < call.end:
            spans.append((split, call.end, "stream"))
    for tool in record.tools:
        spans.append(
            (tool.start, tool.end, "process" if tool.process_wait else "tools")
        )
    return spans


def attribute_wall_time(
    spans: Sequence[tuple[float, float, Phase]],
) -> dict[Phase, float]:
    """Split the turn's wall time into phases; uncovered time is ``other``."""
    totals = dict.fromkeys(PHASES, 0.0)
    bounds = sorted({edge for start, end, _phase in spans for edge in (start, end)})
    for low, high in pairwise(bounds):
        active = {phase for start, end, phase in spans if start < high and end > low}
        phase = next((p for p in _PRIORITY if p in active), "other")
        totals[phase] += (high - low) * 1000
    return totals


def summarise(record: TurnRecord, settings: Settings) -> Perf | None:
    """Aggregate a turn; ``None`` when no call streamed (nothing meaningful to show)."""
    calls = sorted(record.calls, key=lambda call: call.start)
    if not any(call.ttft_ms is not None for call in calls):
        return None
    gens = [call.gen_ms(settings.burst_ms) for call in calls]
    if settings.tps == "e2e":
        pool = [(call.tokens, call.duration_ms) for call in calls if call.tokens]
    else:
        pool = [
            (call.tokens, gen)
            for call, gen in zip(calls, gens, strict=True)
            if gen and call.tokens
        ]
    pool_ms = sum(ms for _tokens, ms in pool)
    phases = attribute_wall_time(
        _spans(TurnRecord(tuple(calls), record.tools), settings.burst_ms)
    )
    return Perf(
        calls=len(calls),
        wall_ms=sum(phases.values()),
        phases=phases,
        ttfts=tuple(call.first_activity_ms for call in calls),
        tps=sum(tokens for tokens, _ms in pool) / pool_ms * 1000 if pool_ms else None,
        tps_calls=len(pool),
    )


# --------------------------------------------------------------------------- formatting


def fmt_ms(ms: float) -> str:
    seconds = ms / 1000
    if seconds < 1:
        return f"{ms:.0f}ms"
    if seconds < 10:
        return f"{seconds:.1f}s"
    if seconds < 60:
        return f"{seconds:.0f}s"
    minutes, secs = divmod(round(seconds), 60)
    if minutes < 60:
        return f"{minutes}m{secs:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h{minutes:02d}m"


def _markup(segments: Iterable[Segment]) -> str:
    return "".join(
        f"[{style}]{text}[/{style}]" if style else text for text, style in segments
    )


def _width(segments: Iterable[Segment]) -> int:
    return sum(len(text) for text, _style in segments)


def _join(parts: Sequence[Sequence[Segment]]) -> list[Segment]:
    joined: list[Segment] = []
    for index, part in enumerate(parts):
        if index:
            joined.append((" · ", "dim"))
        joined.extend(part)
    return joined


def bar(phases: Mapping[Phase, float], cells: int) -> list[Segment]:
    """Stacked bar of wall time, phases in fixed order, cells allotted by largest remainder."""
    total = sum(phases.values())
    shares = {
        phase: phases[phase] / total * cells for phase in PHASES if phases[phase] > 0
    }
    counts = {phase: int(share) for phase, share in shares.items()}
    by_remainder = sorted(
        shares, key=lambda phase: shares[phase] - counts[phase], reverse=True
    )
    for phase in by_remainder[: cells - sum(counts.values())]:
        counts[phase] += 1
    return [
        (_GLYPHS.get(phase, "━") * count, _STYLES[phase])
        for phase, count in counts.items()
        if count
    ]


def sparkline(values: Sequence[float], cells: int = _GRAPH_CELLS) -> list[Segment]:
    """Per-call values binned (max per bin) into at most ``cells``; spikes highlighted."""
    count = min(cells, len(values))
    bins = [
        max(values[i * len(values) // count : (i + 1) * len(values) // count])
        for i in range(count)
    ]
    peak = max(bins) or 1.0
    spike = _SPIKE_FACTOR * statistics.median(values)
    return [
        (
            _SPARK[round(value / peak * (len(_SPARK) - 1))],
            "yellow" if value > spike else "dim",
        )
        for value in bins
    ]


def _tps_part(perf: Perf, settings: Settings) -> list[Segment]:
    if perf.tps is None:
        return []
    label = " tok/s e2e" if settings.tps == "e2e" else " tok/s"
    segments: list[Segment] = [(f"{perf.tps:.0f}{label}", "cyan")]
    if perf.tps_calls < _COVERAGE_NOTE_BELOW * perf.calls:
        segments.append((f" ({perf.tps_calls}/{perf.calls})", "dim"))
    return segments


def _phase_parts(perf: Perf) -> list[tuple[float, list[Segment]]]:
    """Phase labels (doubling as the bar legend), with their time for drop-smallest-first."""
    return [
        (ms, [(f"{phase} ", _STYLES[phase]), (fmt_ms(ms), None)])
        for phase in PHASES
        if (ms := perf.phases[phase]) >= (_SHOW_OTHER_MS if phase == "other" else 1.0)
    ]


def _row(label: str, graphic: list[Segment], text: list[Segment]) -> list[Segment]:
    head: list[Segment] = [(f"{label:<{_LABEL_WIDTH}}", "dim")]
    if graphic:
        head += [*graphic, (" " * (_GRAPH_CELLS - _width(graphic) + 2), None)]
    return [*head, *text]


def _perf_row(perf: Perf, settings: Settings, width: int) -> list[Segment]:
    calls = f"{perf.calls} call" + ("" if perf.calls == 1 else "s")
    lead = [[(fmt_ms(perf.wall_ms), None)], [(calls, None)]]
    tail: list[list[Segment]] = []
    if perf.calls <= _COMPACT_CALLS:
        if perf.calls > 1:
            low, high = min(perf.ttfts), max(perf.ttfts)
            tail.append([("TTFT ", "dim"), (f"{fmt_ms(low)}–{fmt_ms(high)}", None)])
        if tps := _tps_part(perf, settings):
            tail.append(tps)
    phases = _phase_parts(perf)
    graphic = bar(perf.phases, _GRAPH_CELLS)
    while True:
        row = _row(
            "Perf", graphic, _join([*lead, *(part for _ms, part in phases), *tail])
        )
        if _width(row) <= width or not phases:
            return row
        phases.remove(min(phases, key=lambda item: item[0]))


def _ttft_row(perf: Perf, settings: Settings, width: int) -> list[Segment]:
    stats: list[list[Segment]] = [
        [("p50 ", "dim"), (fmt_ms(perf.ttft(0.5)), None)],
        [("p90 ", "dim"), (fmt_ms(perf.ttft(0.9)), None)],
        [("max ", "dim"), (fmt_ms(max(perf.ttfts)), None)],
    ]
    if tps := _tps_part(perf, settings):
        stats.append(tps)
    row = _row("TTFT", sparkline(perf.ttfts), _join(stats))
    return row if _width(row) <= width else _row("TTFT", [], _join(stats))


def _session_row(perf: Perf, settings: Settings) -> list[Segment]:
    parts: list[list[Segment]] = []
    if tps := _tps_part(perf, settings):
        parts.append(tps)
    parts.append([("TTFT p50 ", "dim"), (fmt_ms(perf.ttft(0.5)), None)])
    parts.append([(f"{perf.calls} calls", None)])
    return _row("Sess", [], _join(parts))


def format_perf(
    turn: Perf,
    settings: Settings,
    *,
    session: Perf | None = None,
    width: int = 100,
) -> list[list[Segment]]:
    """A wall-time row, plus a TTFT row for tool loops and an optional session row.

    Graphics share one column so the bar and sparkline align rather than abut.
    """
    lines = [_perf_row(turn, settings, width)]
    if turn.calls > _COMPACT_CALLS:
        lines.append(_ttft_row(turn, settings, width))
    if session is not None:
        lines.append(_session_row(session, settings))
    return lines


# --------------------------------------------------------------------------- post-turn hook


def _supports_turn_messages() -> bool:
    """Post-turn context with the turn's messages (and usage timing) landed together."""
    from fast_agent.plugins import PluginPostUserTurnContext

    return "turn_messages" in {
        field.name for field in fields(PluginPostUserTurnContext)
    }


def display_perf(ctx: PluginPostUserTurnContext) -> str | None:
    if not _supports_turn_messages():
        return None
    settings = Settings.from_config(ctx.config)
    turn = summarise(record_from_history(ctx.turn_messages), settings)
    if turn is None or turn.wall_ms < settings.min_duration_ms:
        return None
    session = (
        summarise(record_from_attempts(ctx.session_usage), settings)
        if settings.session
        else None
    )
    width = shutil.get_terminal_size().columns
    lines = format_perf(turn, settings, session=session, width=width)
    return "\n".join(_markup(line) for line in lines)


# --------------------------------------------------------------------------- /performance


def session_turns(ctx: PluginCommandActionContext) -> list[TurnRecord]:
    """Turns reconstructed from the active agent's history.

    History is the only source where LLM calls and tool runs share one clock, so
    subagent and parallel-child calls (absent from history) are not included.
    """
    from fast_agent.types.conversation_summary import split_into_turns

    turns = [
        record_from_history(turn) for turn in split_into_turns(ctx.message_history)
    ]
    return [record for record in turns if record.calls]


def _table(header: Sequence[str], rows: Iterable[Sequence[str]]) -> str:
    lines = [
        "| " + " | ".join(header) + " |",
        "|" + "|".join("---" if i == 0 else "---:" for i in range(len(header))) + "|",
    ]
    lines.extend("| " + " | ".join(row) + " |" for row in rows)
    return "\n".join(lines)


def _tps_cell(perf: Perf) -> str:
    if perf.tps is None:
        return "—"
    coverage = (
        "" if perf.tps_calls == perf.calls else f" ({perf.tps_calls}/{perf.calls})"
    )
    return f"{perf.tps:.0f}{coverage}"


_SUMMARY_HEADER = (
    "Turn",
    "Calls",
    "Wall",
    *(phase.capitalize() for phase in PHASES),
    "TTFT p50",
    "TTFT p90",
    "tok/s",
)


def _summary_row(
    label: str, perf: Perf, phases: Mapping[Phase, float]
) -> tuple[str, ...]:
    return (
        label,
        str(perf.calls),
        fmt_ms(sum(phases.values())),
        *(fmt_ms(phases[phase]) if phases[phase] else "—" for phase in PHASES),
        fmt_ms(perf.ttft(0.5)),
        fmt_ms(perf.ttft(0.9)),
        _tps_cell(perf),
    )


def format_summary(turns: Sequence[TurnRecord], settings: Settings) -> str:
    perfs = [
        perf for record in turns if (perf := summarise(record, settings)) is not None
    ]
    rows = [
        _summary_row(str(index), perf, perf.phases)
        for index, perf in enumerate(perfs, 1)
    ]
    every = TurnRecord(calls=tuple(call for record in turns for call in record.calls))
    if (total := summarise(every, settings)) is not None:
        # Phases sum per turn: time between turns is the user's, not the agent's.
        phases = {phase: sum(perf.phases[phase] for perf in perfs) for phase in PHASES}
        rows.append(
            tuple(f"**{cell}**" for cell in _summary_row("Cumulative", total, phases))
        )

    by_model: dict[str, list[Call]] = defaultdict(list)
    for call in every.calls:
        by_model[call.model].append(call)
    model_rows = [
        (
            model,
            str(perf.calls),
            fmt_ms(perf.ttft(0.5)),
            fmt_ms(perf.ttft(0.9)),
            _tps_cell(perf),
            f"{1 - perf.tps_calls / perf.calls:.0%}",
        )
        for model, calls in sorted(by_model.items())
        if (perf := summarise(TurnRecord(tuple(calls)), settings)) is not None
    ]
    sections = [_table(_SUMMARY_HEADER, rows)]
    if model_rows:
        sections.append(
            _table(
                ("Model", "Calls", "TTFT p50", "TTFT p90", "tok/s", "Burst"), model_rows
            )
        )
    sections.append(_LEGEND)
    return "\n\n".join(sections)


def format_detail(turns: Sequence[TurnRecord], settings: Settings) -> str:
    rows = []
    for turn_index, record in enumerate(turns, start=1):
        calls = sorted(record.calls, key=lambda c: c.start)
        for call_index, call in enumerate(calls, start=1):
            gen = call.gen_ms(settings.burst_ms)
            tps = call.tps(settings.burst_ms)
            rows.append(
                (
                    str(turn_index),
                    str(call_index),
                    call.model,
                    fmt_ms(call.first_activity_ms),
                    fmt_ms(gen) if gen is not None else "burst",
                    fmt_ms(call.duration_ms),
                    "—" if call.tokens is None else f"{call.tokens:,}",
                    "—" if tps is None else f"{tps:.0f}",
                )
            )
    header = ("Turn", "Call", "Model", "TTFT", "Stream", "Total", "Tokens", "tok/s")
    return f"{_table(header, rows)}\n\n{_LEGEND}"


_LEGEND = (
    "_Wall time per turn is split so phases add up: **wait** (time to first streamed "
    "activity, including queueing, prefill and hidden reasoning), **stream**, **tools**, "
    "**process** (blocking `process wait`/`poll_process` calls, including folded polls) "
    "and **other** (approvals, overhead). Overlaps count once, preferring stream > wait > "
    "tools > process. Calls whose output arrived in one burst count as wait and are "
    "excluded from tok/s._"
)


async def performance_report(
    ctx: PluginCommandActionContext,
) -> PluginCommandActionResult:
    from fast_agent.command_actions import PluginCommandActionResult

    mode = ctx.arguments.strip().casefold()
    if mode not in {"", "summary", "detail"}:
        return PluginCommandActionResult(message="Usage: /performance [summary|detail]")
    turns = session_turns(ctx)
    if not turns:
        return PluginCommandActionResult(
            markdown="_No timed model calls in this session._"
        )
    settings = Settings.from_config(_command_config(ctx))
    render = format_detail if mode == "detail" else format_summary
    return PluginCommandActionResult(markdown=render(turns, settings))


def _command_config(ctx: PluginCommandActionContext) -> Mapping[str, object]:
    if ctx.settings is None:
        return {}
    return ctx.settings.plugins.config.get("llm-perf", {})
