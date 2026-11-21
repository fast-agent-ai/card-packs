"""Turn wall-time phases, TTFT, and tok/s from canonical fast-agent usage.

Adapted from the ``llm-perf`` plugin. History readers accept fast-agent
``PromptMessageExtended`` objects and raw ``history_*.json`` message dicts alike, so
session sweeps recover the same phases and tool runs as the live hook.
"""

from __future__ import annotations

import json
import math
import shutil
import statistics
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, fields
from itertools import pairwise
from typing import TYPE_CHECKING, Any, Literal, cast

from .models import PLUGIN_NAME

if TYPE_CHECKING:
    from collections.abc import Iterable

    from fast_agent.command_actions import (
        PluginCommandActionContext,
        PluginCommandActionResult,
    )
    from fast_agent.llm.usage_tracking import TurnUsage
    from fast_agent.plugins import PluginPostUserTurnContext
    from fast_agent.types import PromptMessageExtended

type HistoryMessage = PromptMessageExtended | Mapping[str, Any]
"""A fast-agent message, or its JSON form from a saved session history."""

# fast-agent channel names (stable wire constants, mirrored to avoid hard imports).
FAST_AGENT_TIMING = "fast-agent-timing"
FAST_AGENT_TOOL_TIMING = "fast-agent-tool-timing"
FAST_AGENT_USAGE = "fast-agent-usage"
FAST_AGENT_PROCESS_POLL_FOLD = "fast-agent-process-poll-fold"
FAST_AGENT_PTC_CALLS = "fast-agent-ptc-calls"
PTC_TOOL_NAME = "exec_sandbox"
_NON_PROMPT_CHANNELS = frozenset(
    {
        "fast-agent-compaction",
        "fast-agent-tool-loop-message",
        "fast-agent-tool-media-message",
    }
)
_TOOL_NAMESPACE_SEPARATORS = ("/", ".", ":", "__")
LEGACY_PLUGIN_NAME = "llm-perf"

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
            raise TypeError(f"{PLUGIN_NAME}: 'session' must be true or false")
        tps = _TPS_MODES.get(str(config.get("tps", defaults.tps)))
        if tps is None:
            raise ValueError(f"{PLUGIN_NAME}: 'tps' must be 'decode' or 'e2e'")
        return cls(
            session=session,
            tps=tps,
            burst_ms=_ms_setting(config, "burst_ms", defaults.burst_ms),
            min_duration_ms=_ms_setting(config, "min_duration_ms", defaults.min_duration_ms),
        )


def _ms_setting(config: Mapping[str, object], name: str, default: float) -> float:
    value = _number(config.get(name, default))
    if value is None or value < 0:
        raise ValueError(f"{PLUGIN_NAME}: '{name}' must be a non-negative number")
    return value


def plugin_config(plugins_config: Mapping[str, object] | None) -> Mapping[str, object]:
    """This plugin's config from ``plugins.config``.

    ``usage-tracker`` keys win; ``llm-perf`` keys are used as a fallback so existing
    perf settings carry over.
    """
    if not isinstance(plugins_config, Mapping):
        return {}
    own = plugins_config.get(PLUGIN_NAME)
    legacy = plugins_config.get(LEGACY_PLUGIN_NAME)
    merged: dict[str, object] = {}
    if isinstance(legacy, Mapping):
        merged.update(legacy)
    if isinstance(own, Mapping):
        merged.update(own)
    return merged


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
class PtcNestedCall:
    """One nested tool call recorded inside an ``exec_sandbox`` PTC run."""

    tool_name: str
    duration_ms: float | None = None
    start_offset_ms: float | None = None
    is_error: bool = False
    status: str = "completed"
    process_wait: bool = False


@dataclass(frozen=True, slots=True)
class ToolRun:
    """One tool execution; ``process_wait`` marks blocking waits on managed processes."""

    name: str
    start: float
    duration_ms: float
    process_wait: bool
    model: str | None = None
    is_error: bool = False
    error_kind: str | None = None
    nested_calls: tuple[PtcNestedCall, ...] = ()
    has_ptc_meta: bool = False

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


def _normalize_tool_name(name: str) -> str:
    normalized = name.strip().casefold()
    index, separator = max(
        (normalized.rfind(separator), separator) for separator in _TOOL_NAMESPACE_SEPARATORS
    )
    return normalized if index < 0 else normalized[index + len(separator) :]


def is_process_wait(name: str, arguments: Mapping[str, object]) -> bool:
    try:
        from fast_agent.utils.tool_names import (
            POLL_PROCESS_TOOL_NAME,
            PROCESS_TOOL_NAME,
            matches_tool_name,
        )
    except ImportError:  # isolated use without fast-agent

        def matches_tool_name(tool_name: str | None, canonical_name: str) -> bool:
            return bool(tool_name) and _normalize_tool_name(tool_name or "") == canonical_name

        POLL_PROCESS_TOOL_NAME, PROCESS_TOOL_NAME = "poll_process", "process"

    if matches_tool_name(name, POLL_PROCESS_TOOL_NAME):
        wait_sec = _number(arguments.get("wait_sec"))
        return wait_sec is not None and wait_sec > 0
    if matches_tool_name(name, PROCESS_TOOL_NAME):
        action = arguments.get("action")
        if action == "wait":
            return True
        if action is None:
            wait_sec = _number(arguments.get("wait_sec"))
            return wait_sec is not None and wait_sec > 0
    return False


def record_from_history(messages: Sequence[HistoryMessage]) -> TurnRecord:
    """Calls and tool runs from persisted timing channels.

    Accepts ``PromptMessageExtended`` objects or raw message dicts from saved
    ``history_*.json`` files. Folded process polling is restored first, so each poll
    keeps its own LLM call and process wait. Tool runs start when the requesting
    assistant message ended (parallel calls start together).
    """
    calls: list[Call] = []
    tools: list[ToolRun] = []
    pending: dict[str, tuple[str, bool, float, str | None]] = {}
    for message in restore_folds(messages):
        if message_role(message) == "user":
            timings = channel_json(message, FAST_AGENT_TOOL_TIMING)
            if not isinstance(timings, dict):
                continue
            results = _tool_results(message)
            for call_id, info in timings.items():
                request = pending.pop(call_id, None)
                duration_ms = _number(info.get("timing_ms")) if isinstance(info, dict) else None
                if request is not None and duration_ms is not None:
                    name, process_wait, start, model = request
                    is_error, error_kind, nested, has_ptc_meta = _inspect_tool_result(
                        results.get(call_id), name
                    )
                    effective_wait = process_wait or (
                        name == PTC_TOOL_NAME
                        and bool(nested)
                        and all(call.process_wait for call in nested)
                    )
                    tools.append(
                        ToolRun(
                            name=name,
                            start=start,
                            duration_ms=duration_ms,
                            process_wait=effective_wait,
                            model=model,
                            is_error=is_error,
                            error_kind=error_kind,
                            nested_calls=nested,
                            has_ptc_meta=has_ptc_meta,
                        )
                    )
            continue
        timing = channel_json(message, FAST_AGENT_TIMING)
        if not isinstance(timing, dict):
            continue
        duration_ms = _number(timing.get("duration_ms"))
        end_time = _number(timing.get("end_time"))
        if duration_ms is None or end_time is None:
            continue
        model, tokens = _final_attempt(channel_json(message, FAST_AGENT_USAGE))
        tool_model = None if model == "unknown" else model
        for call_id, name, arguments in tool_requests(message):
            pending[call_id] = (name, is_process_wait(name, arguments), end_time, tool_model)
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


def _final_attempt(usage: object) -> tuple[str, int | None]:
    """Model and completion tokens of a usage report's final attempt."""
    if not isinstance(usage, dict) or usage.get("schema") != "fast-agent.usage/v2":
        return "unknown", None
    attempts = usage.get("provider_attempts")
    if not isinstance(attempts, list) or not attempts or not isinstance(attempts[-1], dict):
        return "unknown", None
    final = attempts[-1]
    model = final.get("model")
    if not isinstance(model, str) or not model:
        return "unknown", None
    completion = final.get("completion")
    total = completion.get("total") if isinstance(completion, dict) else None
    if isinstance(total, bool) or not isinstance(total, int) or total < 0:
        total = None
    return model, total


# --------------------------------------------------------------------------- history access
# Saved histories are read as raw JSON during sweeps; live hooks pass fast-agent
# messages. These helpers read the few fields perf and pricing need from either.


def _field(message: HistoryMessage, name: str) -> Any:
    if isinstance(message, Mapping):
        return message.get(name)
    return getattr(message, name, None)


def message_role(message: HistoryMessage) -> str | None:
    role = _field(message, "role")
    return role if isinstance(role, str) else getattr(role, "value", None)


def message_channels(message: HistoryMessage) -> Mapping[str, Sequence[object]]:
    channels = _field(message, "channels")
    return channels if isinstance(channels, Mapping) else {}


def block_text(block: object) -> str | None:
    if isinstance(block, Mapping):
        text = block.get("text")
        return text if block.get("type", "text") == "text" and isinstance(text, str) else None
    try:
        from fast_agent.mcp.helpers.content_helpers import get_text
    except ImportError:
        text = getattr(block, "text", None)
        return text if isinstance(text, str) else None
    return get_text(block)


def channel_texts(message: HistoryMessage, channel: str) -> list[str]:
    blocks = message_channels(message).get(channel) or ()
    return [text for block in blocks if (text := block_text(block)) is not None]


def channel_json(message: HistoryMessage, channel: str) -> object:
    blocks = message_channels(message).get(channel) or []
    text = block_text(blocks[0]) if blocks else None
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


def tool_requests(message: HistoryMessage) -> list[tuple[str, str, Mapping[str, object]]]:
    """``(call_id, tool_name, arguments)`` for each tool call on an assistant message."""
    tool_calls = _field(message, "tool_calls")
    if not isinstance(tool_calls, Mapping):
        return []
    requests: list[tuple[str, str, Mapping[str, object]]] = []
    for call_id, request in tool_calls.items():
        if isinstance(request, Mapping):
            params = request.get("params")
            params = params if isinstance(params, Mapping) else {}
            name, arguments = params.get("name"), params.get("arguments")
        else:
            params = getattr(request, "params", None)
            name = getattr(params, "name", None)
            arguments = getattr(params, "arguments", None)
        if isinstance(name, str):
            requests.append(
                (str(call_id), name, arguments if isinstance(arguments, Mapping) else {})
            )
    return requests


def _tool_call_ids(message: HistoryMessage) -> list[str]:
    tool_calls = _field(message, "tool_calls")
    return [str(key) for key in tool_calls] if isinstance(tool_calls, Mapping) else []


def _tool_result_ids(message: HistoryMessage) -> list[str]:
    results = _field(message, "tool_results")
    return [str(key) for key in results] if isinstance(results, Mapping) else []


def _tool_results(message: HistoryMessage) -> Mapping[str, Any]:
    results = _field(message, "tool_results")
    return results if isinstance(results, Mapping) else {}


def _classify_ptc_error(result: Any, has_ptc_meta: bool) -> str:
    content = (
        result.get("content")
        if isinstance(result, Mapping)
        else getattr(result, "content", None)
    )
    first_text = ""
    if isinstance(content, Sequence) and not isinstance(content, (str, bytes)):
        for block in content:
            text = block_text(block)
            if text:
                first_text = text.lstrip()
                break
    if first_text.startswith("Syntax error;"):
        return "syntax"
    if first_text.startswith("Type check failed;"):
        return "typecheck"
    if not has_ptc_meta and first_text.startswith("Type check"):
        return "typecheck"
    return "runtime"


def _inspect_tool_result(
    result: Any,
    tool_name: str,
) -> tuple[bool, str | None, tuple[PtcNestedCall, ...], bool]:
    if result is None:
        return False, None, (), False
    if isinstance(result, Mapping):
        is_error = bool(result.get("isError") or result.get("is_error"))
        raw_meta = result.get("_meta")
        if not isinstance(raw_meta, Mapping):
            raw_meta = result.get("meta")
    else:
        is_error = bool(getattr(result, "isError", False) or getattr(result, "is_error", False))
        raw_meta = getattr(result, "meta", None)
        if not isinstance(raw_meta, Mapping):
            raw_meta = getattr(result, "_meta", None)

    meta = raw_meta if isinstance(raw_meta, Mapping) else {}
    raw_ptc = meta.get(FAST_AGENT_PTC_CALLS)
    has_ptc_meta = isinstance(raw_ptc, list)
    nested: list[PtcNestedCall] = []
    if has_ptc_meta:
        for entry in raw_ptc:
            if not isinstance(entry, Mapping):
                continue
            nested_name = entry.get("tool") or entry.get("function")
            if not isinstance(nested_name, str) or not nested_name.strip():
                continue
            nested_name = nested_name.strip()
            raw_args = entry.get("arguments")
            args: Mapping[str, object] = raw_args if isinstance(raw_args, Mapping) else {}
            timing_ms = _number(entry.get("timing_ms"))
            if timing_ms is not None and timing_ms < 0:
                timing_ms = 0.0
            start_offset_ms = _number(entry.get("start_offset_ms"))
            if start_offset_ms is not None and start_offset_ms < 0:
                start_offset_ms = 0.0
            raw_status = entry.get("status")
            status = raw_status if isinstance(raw_status, str) and raw_status else "completed"
            nested.append(
                PtcNestedCall(
                    tool_name=nested_name,
                    duration_ms=timing_ms,
                    start_offset_ms=start_offset_ms,
                    is_error=bool(entry.get("is_error")) or status == "raised",
                    status=status,
                    process_wait=is_process_wait(nested_name, args),
                )
            )
    error_kind = (
        _classify_ptc_error(result, has_ptc_meta)
        if is_error and tool_name == PTC_TOOL_NAME
        else None
    )
    return is_error, error_kind, tuple(nested), has_ptc_meta


def is_user_prompt(message: HistoryMessage) -> bool:
    """User input, as opposed to tool results and other messages fast-agent adds."""
    return (
        message_role(message) == "user"
        and not _field(message, "tool_results")
        and _NON_PROMPT_CHANNELS.isdisjoint(message_channels(message))
    )


def split_user_turns[M: HistoryMessage](messages: Sequence[M]) -> list[list[M]]:
    """Split history into user turns, like ``fast_agent.history.turns``.

    Prompts sent before any assistant reply belong to the same turn; messages before
    the first prompt form a leading turn of their own.
    """
    starts: list[int] = []
    awaiting_reply = False
    for index, message in enumerate(messages):
        if is_user_prompt(message):
            if not awaiting_reply:
                starts.append(index)
                awaiting_reply = True
        elif message_role(message) == "assistant":
            awaiting_reply = False
    if not messages:
        return []
    if not starts or starts[0] != 0:
        starts = [0, *starts]
    bounds = [*starts, len(messages)]
    return [list(messages[start:end]) for start, end in pairwise(bounds)]


def restore_folds[M: HistoryMessage](messages: Sequence[M]) -> Sequence[M]:
    """History with folded process polls restored; damaged folds are used as stored."""
    if any(isinstance(message, Mapping) for message in messages):
        try:
            return cast("list[M]", _restore_dict_folds(cast("Sequence[Any]", messages)))
        except ValueError:
            return messages
    try:
        from fast_agent.history.process_poll_fold_audit import (
            restore_process_poll_history,
        )
    except ImportError:  # fast-agent releases before fold restoration
        return messages
    try:
        return cast("list[M]", restore_process_poll_history(cast("Sequence[Any]", messages)))
    except ValueError:  # damaged fold audit in persisted history: use it as stored
        return messages


def _fold_exchanges(value: object, label: str) -> list[Mapping[str, Any]]:
    if not isinstance(value, list):
        raise ValueError(f"{label} must be a list")
    exchanges: list[Mapping[str, Any]] = []
    for exchange in value:
        if (
            not isinstance(exchange, Mapping)
            or not isinstance(exchange.get("call_id"), str)
            or not isinstance(exchange.get("request"), Mapping)
            or not isinstance(exchange.get("result"), Mapping)
        ):
            raise ValueError(f"{label} contains an invalid exchange")
        exchanges.append(exchange)
    return exchanges


def _dict_fold_audit(
    message: Mapping[str, Any],
) -> tuple[list[Mapping[str, Any]], list[Mapping[str, Any]]] | None:
    """``(removed, retained)`` exchanges of a folded poll result, mirroring fast-agent."""
    fold: Mapping[str, Any] | None = None
    for text in reversed(channel_texts(message, FAST_AGENT_PROCESS_POLL_FOLD)):
        try:
            value = json.loads(text)
        except json.JSONDecodeError:
            continue
        if isinstance(value, Mapping):
            fold = value
            break
    if fold is None:
        return None
    audit = fold.get("audit")
    if not isinstance(audit, Mapping):
        raise ValueError("Managed-process poll fold is missing its audit archive")
    removed = _fold_exchanges(audit.get("removed_exchanges"), "removed_exchanges")
    retained = _fold_exchanges(audit.get("retained_exchanges"), "retained_exchanges")
    if not retained:
        raise ValueError("Poll fold audit must retain at least one exchange")
    return removed, retained


def _restore_dict_folds(history: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    segments: list[Mapping[str, Any] | list[Mapping[str, Any]]] = []
    index = 0
    while index < len(history):
        message = history[index]
        audit = _dict_fold_audit(history[index + 1]) if index + 1 < len(history) else None
        if audit is None:
            segments.append(message)
            index += 1
            continue
        removed, retained = audit
        *earlier_retained, retained_call_id = [exchange["call_id"] for exchange in retained]
        if retained_call_id not in _tool_call_ids(message) or (
            retained_call_id not in _tool_result_ids(history[index + 1])
        ):
            raise ValueError("Managed-process poll fold retained exchange is invalid")
        retained_suffix = 2 * len(earlier_retained)
        if retained_suffix > len(segments):
            raise ValueError("Managed-process poll fold retained-step archive is inconsistent")
        if earlier_retained:
            suffix_call_ids = [
                call_id
                for segment in segments[-retained_suffix:]
                if isinstance(segment, Mapping)
                for call_id in _tool_call_ids(segment)
            ]
            if suffix_call_ids != earlier_retained:
                raise ValueError("Managed-process poll fold retained call IDs are inconsistent")
            del segments[-retained_suffix:]
        segments.append(
            [
                part
                for exchange in (*removed, *retained)
                for part in (exchange["request"], exchange["result"])
            ]
        )
        index += 2
    restored: list[Mapping[str, Any]] = []
    for segment in segments:
        if isinstance(segment, list):
            restored.extend(segment)
        else:
            restored.append(segment)
    return restored


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


def _merge_intervals(intervals: Sequence[tuple[float, float]]) -> list[tuple[float, float]]:
    ordered = sorted((start, end) for start, end in intervals if end > start)
    merged: list[tuple[float, float]] = []
    for start, end in ordered:
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def _subtract_intervals(
    base: tuple[float, float], cuts: Sequence[tuple[float, float]]
) -> list[tuple[float, float]]:
    pieces: list[tuple[float, float]] = []
    cursor, end = base
    for cut_start, cut_end in _merge_intervals(cuts):
        if cut_end <= cursor:
            continue
        if cut_start >= end:
            break
        if cut_start > cursor:
            pieces.append((cursor, cut_start))
        cursor = max(cursor, cut_end)
    if cursor < end:
        pieces.append((cursor, end))
    return pieces


def _tool_phase_spans(tool: ToolRun) -> list[tuple[float, float, Phase]]:
    if tool.process_wait:
        return [(tool.start, tool.end, "process")]
    if tool.name == PTC_TOOL_NAME and tool.nested_calls:
        wait_intervals: list[tuple[float, float]] = []
        other_intervals: list[tuple[float, float]] = []
        for nested in tool.nested_calls:
            if nested.start_offset_ms is None or nested.duration_ms is None:
                continue
            start = tool.start + nested.start_offset_ms / 1000
            end = min(tool.end, start + nested.duration_ms / 1000)
            if end <= start:
                continue
            if nested.process_wait:
                wait_intervals.append((start, end))
            else:
                other_intervals.append((start, end))
        if wait_intervals:
            pure_waits: list[tuple[float, float]] = []
            for interval in _merge_intervals(wait_intervals):
                pure_waits.extend(_subtract_intervals(interval, other_intervals))
            if pure_waits:
                spans: list[tuple[float, float, Phase]] = [
                    (start, end, "process") for start, end in pure_waits
                ]
                for start, end in _subtract_intervals((tool.start, tool.end), pure_waits):
                    spans.append((start, end, "tools"))
                return spans
    return [(tool.start, tool.end, "tools")]


def _spans(record: TurnRecord, burst_ms: float) -> list[tuple[float, float, Phase]]:
    spans: list[tuple[float, float, Phase]] = []
    for call in record.calls:
        split = call.end - (call.gen_ms(burst_ms) or 0.0) / 1000
        spans.append((call.start, split, "wait"))
        if split < call.end:
            spans.append((split, call.end, "stream"))
    for tool in record.tools:
        spans.extend(_tool_phase_spans(tool))
    return spans


def turn_phases(record: TurnRecord, burst_ms: float) -> dict[Phase, float]:
    """Wall time per phase for one turn (phases sum to the turn's wall time)."""
    calls = sorted(record.calls, key=lambda call: call.start)
    return attribute_wall_time(_spans(TurnRecord(tuple(calls), record.tools), burst_ms))


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
            (call.tokens, gen) for call, gen in zip(calls, gens, strict=True) if gen and call.tokens
        ]
    pool_ms = sum(ms for _tokens, ms in pool)
    phases = attribute_wall_time(_spans(TurnRecord(tuple(calls), record.tools), settings.burst_ms))
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


def markup(segments: Iterable[Segment]) -> str:
    return "".join(f"[{style}]{text}[/{style}]" if style else text for text, style in segments)


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
    shares = {phase: phases[phase] / total * cells for phase in PHASES if phases[phase] > 0}
    counts = {phase: int(share) for phase, share in shares.items()}
    by_remainder = sorted(shares, key=lambda phase: shares[phase] - counts[phase], reverse=True)
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
        max(values[i * len(values) // count : (i + 1) * len(values) // count]) for i in range(count)
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
        row = _row("Perf", graphic, _join([*lead, *(part for _ms, part in phases), *tail]))
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


def supports_turn_messages() -> bool:
    """Post-turn context with the turn's messages (and usage timing) landed together."""
    try:
        from fast_agent.plugins import PluginPostUserTurnContext
    except ImportError:
        return False

    return "turn_messages" in {field.name for field in fields(PluginPostUserTurnContext)}


def display_perf(ctx: PluginPostUserTurnContext) -> str | None:
    if not supports_turn_messages():
        return None
    settings = Settings.from_config(ctx.config)
    turn = summarise(record_from_history(ctx.turn_messages), settings)
    if turn is None or turn.wall_ms < settings.min_duration_ms:
        return None
    session = (
        summarise(record_from_attempts(ctx.session_usage), settings) if settings.session else None
    )
    width = shutil.get_terminal_size().columns
    lines = format_perf(turn, settings, session=session, width=width)
    return "\n".join(markup(line) for line in lines)


# --------------------------------------------------------------------------- /performance


def session_turns(ctx: PluginCommandActionContext) -> list[TurnRecord]:
    """Turns reconstructed from the active agent's history.

    History is the only source where LLM calls and tool runs share one clock, so
    subagent and parallel-child calls (absent from history) are not included.
    """
    return history_turns(ctx.message_history)


def history_turns(messages: Sequence[HistoryMessage]) -> list[TurnRecord]:
    """One record per user turn that made timed calls."""
    try:
        from fast_agent.types.conversation_summary import split_into_turns
    except ImportError:
        split_into_turns = None
    if split_into_turns is not None and not any(
        isinstance(message, Mapping) for message in messages
    ):
        groups: Sequence[Sequence[HistoryMessage]] = split_into_turns(
            cast("list[Any]", list(messages))
        )
    else:
        groups = split_user_turns(messages)
    turns = [record_from_history(turn) for turn in groups]
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
    coverage = "" if perf.tps_calls == perf.calls else f" ({perf.tps_calls}/{perf.calls})"
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


def _summary_row(label: str, perf: Perf, phases: Mapping[Phase, float]) -> tuple[str, ...]:
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
    perfs = [perf for record in turns if (perf := summarise(record, settings)) is not None]
    rows = [_summary_row(str(index), perf, perf.phases) for index, perf in enumerate(perfs, 1)]
    every = TurnRecord(calls=tuple(call for record in turns for call in record.calls))
    if (total := summarise(every, settings)) is not None:
        # Phases sum per turn: time between turns is the user's, not the agent's.
        phases = {phase: sum(perf.phases[phase] for perf in perfs) for phase in PHASES}
        rows.append(tuple(f"**{cell}**" for cell in _summary_row("Cumulative", total, phases)))

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
            _table(("Model", "Calls", "TTFT p50", "TTFT p90", "tok/s", "Burst"), model_rows)
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
        return PluginCommandActionResult(markdown="_No timed model calls in this session._")
    settings = Settings.from_config(_command_config(ctx))
    render = format_detail if mode == "detail" else format_summary
    return PluginCommandActionResult(markdown=render(turns, settings))


def _command_config(ctx: PluginCommandActionContext) -> Mapping[str, object]:
    if ctx.settings is None:
        return {}
    return plugin_config(ctx.settings.plugins.config)
