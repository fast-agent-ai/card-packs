"""Post-turn price estimates from canonical fast-agent usage."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING

from fast_agent.command_actions import (
    MarkdownTextStyle,
    PluginCommandActionContext,
    PluginCommandActionResult,
    PluginCommandCompletion,
    PluginCommandCompletionContext,
)
from fast_agent.constants import FAST_AGENT_SYNTHETIC_FINAL_CHANNEL, FAST_AGENT_USAGE
from fast_agent.llm.usage_tracking import UsageReport
from fast_agent.mcp.helpers.content_helpers import get_text
from fast_agent.types import LlmStopReason

if TYPE_CHECKING:
    from collections.abc import Sequence

    from fast_agent.llm.usage_tracking import TurnUsage, UsageLedger, UserTurnUsage
    from fast_agent.plugins import PluginPostUserTurnContext
    from fast_agent.types import PromptMessageExtended

_TOKENS_PER_MILLION = 1_000_000
_GPT_56_LONG_CONTEXT_THRESHOLD = 272_000


@dataclass(frozen=True, slots=True)
class Rates:
    input: float
    cached_input: float
    output: float
    cache_write: float | None = None


@dataclass(frozen=True, slots=True)
class Price:
    usd: float
    unpriced_calls: int


@dataclass(frozen=True, slots=True)
class _FallbackUserTurn:
    agent_name: str
    attempts: tuple[TurnUsage, ...]
    ledgers: tuple[UsageLedger, ...] = ()


type CostUserTurn = UserTurnUsage | _FallbackUserTurn


_GPT_56_RATES = {
    ("sol", "standard", "short"): Rates(5.00, 0.50, 30.00, 6.25),
    ("sol", "standard", "long"): Rates(10.00, 1.00, 45.00, 12.50),
    ("sol", "flex", "short"): Rates(2.50, 0.25, 15.00, 3.125),
    ("sol", "flex", "long"): Rates(5.00, 0.50, 22.50, 6.25),
    ("terra", "standard", "short"): Rates(2.00, 0.20, 12.00, 2.50),
    ("terra", "standard", "long"): Rates(4.00, 0.40, 18.00, 5.00),
    ("terra", "flex", "short"): Rates(1.00, 0.10, 6.00, 1.25),
    ("terra", "flex", "long"): Rates(2.00, 0.20, 9.00, 2.50),
    ("luna", "standard", "short"): Rates(0.20, 0.02, 1.20, 0.25),
    ("luna", "standard", "long"): Rates(0.40, 0.04, 1.80, 0.50),
    ("luna", "flex", "short"): Rates(0.10, 0.01, 0.60, 0.125),
    ("luna", "flex", "long"): Rates(0.20, 0.02, 0.90, 0.25),
}
_KIMI_K3 = Rates(3.00, 0.30, 15.00)
_DEEPSEEK_V4_FLASH = Rates(0.14, 0.002, 0.28)
_MUSE_SPARK_STANDARD = Rates(1.25, 0.15, 4.25)
_MUSE_SPARK_CONTRIBUTOR = Rates(0.10, 0.002, 0.20)


def _gpt_56_variant(model: str) -> str | None:
    if model in {"gpt-5.6", "gpt-5.6-sol"}:
        return "sol"
    for variant in ("terra", "luna"):
        if model == f"gpt-5.6-{variant}":
            return variant
    return None


def _service_tier(turn: TurnUsage) -> str | None:
    tier = turn.requested_service_tier or turn.service_tier
    if tier in (None, "default", "standard"):
        return "standard"
    if tier == "flex":
        return "flex"
    return None


def _gpt_56_rates(turn: TurnUsage) -> Rates | None:
    model = turn.model.casefold()
    variant = _gpt_56_variant(model)
    if variant is None:
        return None
    tier = _service_tier(turn)
    if tier is None or turn.prompt.total is None:
        return None
    context = "long" if turn.prompt.total > _GPT_56_LONG_CONTEXT_THRESHOLD else "short"
    return _GPT_56_RATES[(variant, tier, context)]


def _kimi_k3_rates(turn: TurnUsage) -> Rates | None:
    model = turn.model.casefold()
    kimi_model = model.partition(":")[0]
    return (
        _KIMI_K3 if kimi_model == "kimi-k3" or kimi_model.endswith("/kimi-k3") else None
    )


def _deepseek_v4_flash_rates(turn: TurnUsage) -> Rates | None:
    model = turn.model.casefold()
    if model == "deepseek-v4-flash" or model.endswith("/deepseek-v4-flash"):
        return _DEEPSEEK_V4_FLASH
    return None


def _muse_spark_rates(turn: TurnUsage) -> Rates | None:
    model = turn.model.casefold().removeprefix("metaai.")
    if model in {"muse-spark-1.1", "muse-spark-1.2"}:
        return _MUSE_SPARK_STANDARD
    if model == "muse-spark-1.2-contributor":
        return _MUSE_SPARK_CONTRIBUTOR
    return None


_RATE_RESOLVERS = (
    _gpt_56_rates,
    _kimi_k3_rates,
    _deepseek_v4_flash_rates,
    _muse_spark_rates,
)


def _rates(turn: TurnUsage) -> Rates | None:
    for resolve in _RATE_RESOLVERS:
        if rates := resolve(turn):
            return rates
    return None


def _usage_attempts(message: PromptMessageExtended) -> tuple[TurnUsage, ...]:
    channels = message.channels or {}
    if FAST_AGENT_SYNTHETIC_FINAL_CHANNEL in channels:
        return ()

    attempts: list[TurnUsage] = []
    for block in channels.get(FAST_AGENT_USAGE, ()):
        text = get_text(block)
        if text is None:
            continue
        try:
            payload = json.loads(text)
            if (
                not isinstance(payload, dict)
                or payload.get("schema") != "fast-agent.usage/v2"
            ):
                continue
            report = UsageReport.model_validate(payload)
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        attempts.extend(report.provider_attempts)
    return tuple(attempts)


def _is_external_user_message(message: PromptMessageExtended) -> bool:
    return (
        message.role == "user" and not message.tool_results and not message.is_template
    )


def _reconstruct_history_turns(
    messages: Sequence[PromptMessageExtended],
    *,
    agent_name: str,
) -> tuple[_FallbackUserTurn, ...]:
    turns: list[_FallbackUserTurn] = []
    current: list[TurnUsage] | None = None

    for message in messages:
        if _is_external_user_message(message):
            if current is None:
                current = []
            continue
        if current is None or message.role != "assistant":
            continue

        current.extend(_usage_attempts(message))
        if message.stop_reason not in (None, LlmStopReason.TOOL_USE):
            if current:
                turns.append(
                    _FallbackUserTurn(agent_name=agent_name, attempts=tuple(current))
                )
            current = None

    if current:
        turns.append(_FallbackUserTurn(agent_name=agent_name, attempts=tuple(current)))
    return tuple(turns)


def _attempt_signature(attempt: TurnUsage) -> str:
    return json.dumps(
        attempt.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
    )


def _ordered_subsequence(candidate: tuple[str, ...], source: tuple[str, ...]) -> bool:
    if not candidate:
        return False
    source_iter = iter(source)
    return all(any(item == expected for item in source_iter) for expected in candidate)


def _turns_correspond(historical: CostUserTurn, live: UserTurnUsage) -> bool:
    if historical.agent_name != live.agent_name:
        return False
    historical_attempts = tuple(
        _attempt_signature(attempt) for attempt in historical.attempts
    )
    live_attempts = tuple(_attempt_signature(attempt) for attempt in live.attempts)
    if historical_attempts == live_attempts:
        return True
    return bool(live.ledgers) and _ordered_subsequence(
        historical_attempts, live_attempts
    )


def _merge_live_turns(
    historical: tuple[CostUserTurn, ...],
    live: tuple[UserTurnUsage, ...],
) -> tuple[CostUserTurn, ...]:
    if not historical:
        return live
    if not live:
        return historical

    matches: list[tuple[int, int]] = []
    historical_start = 0
    for live_index, live_turn in enumerate(live):
        for historical_index in range(historical_start, len(historical)):
            if _turns_correspond(historical[historical_index], live_turn):
                matches.append((historical_index, live_index))
                historical_start = historical_index + 1
                break
    if not matches:
        return (*historical, *live)

    merged: list[CostUserTurn] = []
    historical_start = 0
    live_start = 0
    for historical_index, live_index in matches:
        merged.extend(historical[historical_start:historical_index])
        merged.extend(live[live_start:live_index])
        merged.append(live[live_index])
        historical_start = historical_index + 1
        live_start = live_index + 1
    merged.extend(historical[historical_start:])
    merged.extend(live[live_start:])
    return tuple(merged)


def _call_cost(turn: TurnUsage) -> float | None:
    if turn.cost_usd is not None:
        return turn.cost_usd

    rates = _rates(turn)
    prompt_total = turn.prompt.total
    output = turn.completion.total
    if rates is None or prompt_total is None or output is None:
        return None

    cache_read = turn.prompt.cache_read or 0
    cache_write = turn.prompt.cache_write or 0
    uncached = turn.prompt.uncached
    if uncached is None:
        uncached = prompt_total - cache_read - cache_write
    if uncached < 0 or uncached + cache_read + cache_write != prompt_total:
        return None

    total = (
        uncached * rates.input
        + cache_read * rates.cached_input
        + cache_write
        * (rates.cache_write if rates.cache_write is not None else rates.input)
        + output * rates.output
    )
    return total / _TOKENS_PER_MILLION


def calculate_price(turns: tuple[TurnUsage, ...]) -> Price:
    costs = [_call_cost(turn) for turn in turns]
    return Price(
        usd=sum(cost for cost in costs if cost is not None),
        unpriced_calls=sum(cost is None for cost in costs),
    )


def _format_adaptive_usd(value: float) -> str:
    if value >= 1:
        return f"${value:,.2f}"
    if value >= 0.01:
        return f"${value:.4f}"
    return f"${value:.6f}"


def _report_decimal_places(costs: tuple[float, ...]) -> int:
    positive = tuple(cost for cost in costs if cost > 0)
    if any(cost < 0.01 for cost in positive):
        return 6
    if any(cost < 1 for cost in positive):
        return 4
    return 2


def _format_report_usd(value: float, decimal_places: int) -> str:
    return f"${value:,.{decimal_places}f}"


def _format_price(price: Price) -> str:
    if price.unpriced_calls and price.usd == 0:
        return "n/a"
    return _format_adaptive_usd(price.usd)


def _tier_label(turn: TurnUsage) -> str:
    tier = turn.requested_service_tier or turn.service_tier
    return "standard" if tier in (None, "default", "standard") else tier


def _context_label(turn: TurnUsage) -> str:
    if _gpt_56_variant(turn.model.casefold()) is None or turn.prompt.total is None:
        return "—"
    return "long" if turn.prompt.total > _GPT_56_LONG_CONTEXT_THRESHOLD else "short"


def _format_tokens(value: int | None) -> str:
    return "—" if value is None else f"{value:,}"


def _markdown_cell(value: object) -> str:
    return str(value).replace("|", "\\|")


def _complete_token_sum(values: tuple[int | None, ...]) -> int | None:
    if any(value is None for value in values):
        return None
    return sum(value for value in values if value is not None)


def _cached_tokens(
    attempts: tuple[TurnUsage, ...],
) -> tuple[int | None, int | None]:
    return (
        _complete_token_sum(tuple(attempt.prompt.total for attempt in attempts)),
        _complete_token_sum(tuple(attempt.prompt.cache_read for attempt in attempts)),
    )


def _low_cache_percentage(attempts: tuple[TurnUsage, ...]) -> int | None:
    total, cached = _cached_tokens(attempts)
    if total is None or total <= 0 or cached is None or cached * 2 >= total:
        return None
    return cached * 100 // total


def _low_cache_styles(
    groups: Sequence[tuple[TurnUsage, ...]],
) -> tuple[MarkdownTextStyle, ...]:
    percentages = {
        percentage
        for group in groups
        if (percentage := _low_cache_percentage(group)) is not None
    }
    return tuple(
        MarkdownTextStyle(text=f"{percentage}%", style="red")
        for percentage in sorted(percentages)
    )


def _detail_groups(
    attempts: tuple[TurnUsage, ...],
) -> tuple[tuple[TurnUsage, ...], ...]:
    return tuple((attempt,) for attempt in attempts) + (attempts,)


def _cached_cell(attempts: tuple[TurnUsage, ...]) -> str:
    total, cached = _cached_tokens(attempts)
    if cached is None:
        return "—"
    if total is None:
        return f"{cached:,} (—)"
    if total == 0:
        return f"{cached:,} (n/a)"

    percentage = cached * 100 // total
    return f"{cached:,} ({percentage}%)"


def _show_cache_write(attempts: tuple[TurnUsage, ...]) -> bool:
    return any((attempt.prompt.cache_write or 0) > 0 for attempt in attempts)


def _uses_cache_write_fallback_rate(turn: TurnUsage) -> bool:
    if turn.cost_usd is not None or (turn.prompt.cache_write or 0) <= 0:
        return False
    rates = _rates(turn)
    return (
        rates is not None and rates.cache_write is None and _call_cost(turn) is not None
    )


def _table_header(columns: tuple[tuple[str, str], ...]) -> list[str]:
    return [
        "| " + " | ".join(label for label, _alignment in columns) + " |",
        "|" + "|".join(alignment for _label, alignment in columns) + "|",
    ]


def _cost_cell(
    attempts: tuple[TurnUsage, ...],
    decimal_places: int,
    *,
    cumulative: bool = False,
) -> str:
    price = calculate_price(attempts)
    if cumulative:
        if price.unpriced_calls and price.usd == 0:
            return "**unpriced**"
        value = _format_report_usd(price.usd, decimal_places)
        if price.unpriced_calls:
            value += f" + {price.unpriced_calls} unpriced"
        return f"**{value}**"

    if price.unpriced_calls and price.usd == 0:
        return "unpriced"
    value = f"**{_format_report_usd(price.usd, decimal_places)}**"
    if price.unpriced_calls:
        value += f" + {price.unpriced_calls} unpriced"
    return value


def _token_cells(
    attempts: tuple[TurnUsage, ...],
    *,
    show_cache_write: bool,
) -> tuple[str, ...]:
    cells = [
        _format_tokens(
            _complete_token_sum(tuple(attempt.prompt.total for attempt in attempts))
        ),
        _cached_cell(attempts),
    ]
    if show_cache_write:
        cells.append(
            _format_tokens(
                _complete_token_sum(
                    tuple(attempt.prompt.cache_write for attempt in attempts)
                )
            )
        )
    cells.append(
        _format_tokens(
            _complete_token_sum(tuple(attempt.completion.total for attempt in attempts))
        )
    )
    return tuple(cells)


def _usage_cells(
    attempts: tuple[TurnUsage, ...],
    decimal_places: int,
    *,
    show_cache_write: bool,
    cumulative: bool = False,
) -> tuple[str, ...]:
    cells = [
        str(len(attempts)),
        *_token_cells(attempts, show_cache_write=show_cache_write),
    ]
    if cumulative:
        cells = [f"**{cell}**" for cell in cells]
    cells.append(_cost_cell(attempts, decimal_places, cumulative=cumulative))
    return tuple(cells)


def _summary_row(
    turn: str,
    ledger: str,
    attempts: tuple[TurnUsage, ...],
    decimal_places: int,
    *,
    show_cache_write: bool,
    cumulative: bool = False,
) -> str:
    return (
        "| "
        + " | ".join(
            (
                turn,
                ledger,
                *_usage_cells(
                    attempts,
                    decimal_places,
                    show_cache_write=show_cache_write,
                    cumulative=cumulative,
                ),
            )
        )
        + " |"
    )


def _attempts_by_model(
    attempts: tuple[TurnUsage, ...],
) -> dict[str, tuple[TurnUsage, ...]]:
    by_model: dict[str, list[TurnUsage]] = {}
    for attempt in attempts:
        by_model.setdefault(attempt.model, []).append(attempt)
    return {model: tuple(model_attempts) for model, model_attempts in by_model.items()}


def _model_rollup(
    by_model: dict[str, tuple[TurnUsage, ...]],
    decimal_places: int,
    *,
    show_cache_write: bool,
) -> list[str]:
    if len(by_model) < 2:
        return []

    columns = [
        ("Model", "---"),
        ("Calls", "---:"),
        ("Input", "---:"),
        ("Cache read", "---:"),
        ("Output", "---:"),
        ("Cost", "---:"),
    ]
    if show_cache_write:
        columns.insert(4, ("Cache write", "---:"))
    lines = [
        "",
        "### Model cost by model",
        "",
        *_table_header(tuple(columns)),
    ]
    for model, model_attempts in by_model.items():
        cells = _usage_cells(
            model_attempts,
            decimal_places,
            show_cache_write=show_cache_write,
        )
        lines.append("| " + " | ".join((f"`{_markdown_cell(model)}`", *cells)) + " |")
    return lines


def _turn_rollup_groups(
    turns: tuple[CostUserTurn, ...],
    by_model: dict[str, tuple[TurnUsage, ...]],
) -> list[tuple[TurnUsage, ...]]:
    attempts = tuple(attempt for turn in turns for attempt in turn.attempts)
    groups = [
        group
        for turn in turns
        for group in (turn.attempts, *(ledger.attempts for ledger in turn.ledgers))
    ]
    groups.append(attempts)
    if len(by_model) > 1:
        groups.extend(by_model.values())
    return groups


def format_turn_rollup(turns: tuple[CostUserTurn, ...]) -> str:
    attempts = tuple(attempt for turn in turns for attempt in turn.attempts)
    by_model = _attempts_by_model(attempts)
    displayed_groups = _turn_rollup_groups(turns, by_model)
    price = calculate_price(attempts)
    show_cache_write = _show_cache_write(attempts)
    decimal_places = _report_decimal_places(
        tuple(calculate_price(group).usd for group in displayed_groups) + (price.usd,)
    )

    columns = [
        ("Turn", "---:"),
        ("Ledger", "---"),
        ("Calls", "---:"),
        ("Input", "---:"),
        ("Cache read", "---:"),
        ("Output", "---:"),
        ("Cost", "---:"),
    ]
    if show_cache_write:
        columns.insert(5, ("Cache write", "---:"))
    lines = [
        "### Model cost by user turn",
        "",
        *_table_header(tuple(columns)),
    ]
    for index, turn in enumerate(turns, start=1):
        lines.append(
            _summary_row(
                str(index),
                f"`{_markdown_cell(turn.agent_name)}` total",
                turn.attempts,
                decimal_places,
                show_cache_write=show_cache_write,
            )
        )
        lines.extend(
            _summary_row(
                "",
                f"{_markdown_cell(ledger.label)} (included)",
                ledger.attempts,
                decimal_places,
                show_cache_write=show_cache_write,
            )
            for ledger in turn.ledgers
        )
    lines.append(
        _summary_row(
            "",
            "**Cumulative**",
            attempts,
            decimal_places,
            show_cache_write=show_cache_write,
            cumulative=True,
        )
    )

    lines.extend(
        _model_rollup(
            by_model,
            decimal_places,
            show_cache_write=show_cache_write,
        )
    )
    if any(_uses_cache_write_fallback_rate(attempt) for attempt in attempts):
        lines.extend(
            [
                "",
                "_Cache writes use the normal input rate where no separate write tariff exists._",
            ]
        )
    if any(turn.ledgers for turn in turns):
        lines.extend(
            ["", "_Ledger rows are already included in their user-turn total._"]
        )
    return "\n".join(lines)


def format_cost_breakdown(
    turns: tuple[tuple[int | None, TurnUsage], ...],
) -> str:
    attempts = tuple(turn for _turn_number, turn in turns)
    costs = tuple(_call_cost(turn) for turn in attempts)
    price = calculate_price(attempts)
    show_cache_write = _show_cache_write(attempts)
    decimal_places = _report_decimal_places(
        tuple(cost for cost in costs if cost is not None) + (price.usd,)
    )
    columns = [
        ("Turn", "---:"),
        ("#", "---:"),
        ("Model", "---"),
        ("Tier", "---"),
        ("Context", ":---:"),
        ("Input", "---:"),
        ("Cache read", "---:"),
        ("Output", "---:"),
        ("Cost", "---:"),
    ]
    if show_cache_write:
        columns.insert(7, ("Cache write", "---:"))
    lines = [
        "### Model cost detail",
        "",
        *_table_header(tuple(columns)),
    ]
    for index, ((turn_number, turn), cost) in enumerate(
        zip(turns, costs, strict=True),
        start=1,
    ):
        cells = [
            str(turn_number) if turn_number is not None else "—",
            str(index),
            f"`{_markdown_cell(turn.model)}`",
            f"`{_markdown_cell(_tier_label(turn))}`",
            f"_{_context_label(turn)}_",
            _format_tokens(turn.prompt.total),
            _cached_cell((turn,)),
        ]
        if show_cache_write:
            cells.append(_format_tokens(turn.prompt.cache_write))
        cells.extend(
            (
                _format_tokens(turn.completion.total),
                (
                    f"**{_format_report_usd(cost, decimal_places)}**"
                    if cost is not None
                    else "unpriced"
                ),
            )
        )
        lines.append("| " + " | ".join(cells) + " |")

    cumulative_cells = [
        "",
        f"**{len(attempts)}**",
        "**Cumulative**",
        "",
        "",
        *(
            f"**{cell}**"
            for cell in _token_cells(attempts, show_cache_write=show_cache_write)
        ),
        _cost_cell(attempts, decimal_places, cumulative=True),
    ]
    lines.append("| " + " | ".join(cumulative_cells) + " |")
    if any(_uses_cache_write_fallback_rate(turn) for turn in attempts):
        lines.extend(
            [
                "",
                "_Cache writes use the normal input rate when no separate write tariff exists._",
            ]
        )
    return "\n".join(lines)


async def complete_cost(
    ctx: PluginCommandCompletionContext,
) -> list[PluginCommandCompletion]:
    if ctx.completed_tokens:
        return []
    return [
        PluginCommandCompletion("summary", detail="Cost by user turn (default)"),
        PluginCommandCompletion("detail", detail="Full provider-call ledger"),
    ]


async def cost_breakdown(ctx: PluginCommandActionContext) -> PluginCommandActionResult:
    mode = ctx.arguments.strip().casefold()
    if mode not in {"", "summary", "detail"}:
        return PluginCommandActionResult(message="Usage: /cost [summary|detail]")

    reconstructed = _reconstruct_history_turns(
        ctx.message_history,
        agent_name=ctx.agent_name,
    )
    user_turn_usage = _merge_live_turns(reconstructed, ctx.user_turn_usage)
    if user_turn_usage:
        if mode == "detail":
            detailed = tuple(
                (turn_number, attempt)
                for turn_number, turn in enumerate(user_turn_usage, start=1)
                for attempt in turn.attempts
            )
            attempts = tuple(attempt for _turn_number, attempt in detailed)
            return PluginCommandActionResult(
                markdown=format_cost_breakdown(detailed),
                markdown_styles=_low_cache_styles(_detail_groups(attempts)),
            )
        by_model = _attempts_by_model(
            tuple(attempt for turn in user_turn_usage for attempt in turn.attempts)
        )
        return PluginCommandActionResult(
            markdown=format_turn_rollup(user_turn_usage),
            markdown_styles=_low_cache_styles(
                _turn_rollup_groups(user_turn_usage, by_model)
            ),
        )

    usage = ctx.usage
    if usage is None or not usage.turns:
        return PluginCommandActionResult(
            markdown="_No model usage recorded for this session._"
        )
    attempts = tuple(usage.turns)
    if mode == "detail":
        return PluginCommandActionResult(
            markdown=format_cost_breakdown(
                tuple((None, attempt) for attempt in attempts)
            ),
            markdown_styles=_low_cache_styles(_detail_groups(attempts)),
        )
    fallback = _FallbackUserTurn(agent_name=ctx.agent_name, attempts=attempts)
    return PluginCommandActionResult(
        markdown=format_turn_rollup((fallback,)),
        markdown_styles=_low_cache_styles((attempts,)),
    )


async def display_cost(ctx: PluginPostUserTurnContext) -> str | None:
    if not ctx.turn_usage:
        return None

    turn = calculate_price(ctx.turn_usage)
    session = calculate_price(ctx.session_usage)
    unpriced = max(turn.unpriced_calls, session.unpriced_calls)
    suffix = (
        f" [dim]· {unpriced} unpriced model {'call' if unpriced == 1 else 'calls'}[/dim]"
        if unpriced
        else ""
    )
    return (
        f"[dim]Cost:[/dim] [cyan]{_format_price(turn)}[/cyan] last · "
        f"[cyan]{_format_price(session)}[/cyan] session{suffix}"
    )
