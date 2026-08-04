"""Post-turn price estimates from canonical fast-agent usage."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from fast_agent.command_actions import PluginCommandActionContext, PluginCommandActionResult

if TYPE_CHECKING:
    from fast_agent.llm.usage_tracking import TurnUsage, UsageLedger, UserTurnUsage
    from fast_agent.plugins import PluginPostUserTurnContext

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


def _rates(turn: TurnUsage) -> Rates | None:
    model = turn.model.casefold()
    variant = _gpt_56_variant(model)
    if variant is not None:
        tier = _service_tier(turn)
        if tier is None or turn.prompt.total is None:
            return None
        context = "long" if turn.prompt.total > _GPT_56_LONG_CONTEXT_THRESHOLD else "short"
        return _GPT_56_RATES[(variant, tier, context)]
    kimi_model = model.partition(":")[0]
    if kimi_model == "kimi-k3" or kimi_model.endswith("/kimi-k3"):
        return _KIMI_K3
    if model == "deepseek-v4-flash" or model.endswith("/deepseek-v4-flash"):
        return _DEEPSEEK_V4_FLASH
    return None


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
        + cache_write * (rates.cache_write if rates.cache_write is not None else rates.input)
        + output * rates.output
    )
    return total / _TOKENS_PER_MILLION


def calculate_price(turns: tuple[TurnUsage, ...]) -> Price:
    costs = [_call_cost(turn) for turn in turns]
    return Price(
        usd=sum(cost for cost in costs if cost is not None),
        unpriced_calls=sum(cost is None for cost in costs),
    )


def _format_usd(value: float) -> str:
    if value >= 1:
        return f"${value:,.2f}"
    if value >= 0.01:
        return f"${value:.4f}"
    return f"${value:.6f}"


def _format_price(price: Price) -> str:
    if price.unpriced_calls and price.usd == 0:
        return "n/a"
    return _format_usd(price.usd)


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


def _cost_cell(attempts: tuple[TurnUsage, ...]) -> str:
    price = calculate_price(attempts)
    if price.unpriced_calls and price.usd == 0:
        return "unpriced"
    value = f"**{_format_usd(price.usd)}**"
    if price.unpriced_calls:
        value += f" + {price.unpriced_calls} unpriced"
    return value


def _summary_row(
    turn: str,
    ledger: str,
    attempts: tuple[TurnUsage, ...],
) -> str:
    return (
        "| "
        + " | ".join(
            (
                turn,
                ledger,
                str(len(attempts)),
                _format_tokens(
                    _complete_token_sum(tuple(attempt.prompt.total for attempt in attempts))
                ),
                _format_tokens(
                    _complete_token_sum(tuple(attempt.prompt.cache_read for attempt in attempts))
                ),
                _format_tokens(
                    _complete_token_sum(tuple(attempt.prompt.cache_write for attempt in attempts))
                ),
                _format_tokens(
                    _complete_token_sum(tuple(attempt.completion.total for attempt in attempts))
                ),
                _cost_cell(attempts),
            )
        )
        + " |"
    )


def format_turn_rollup(turns: tuple[CostUserTurn, ...]) -> str:
    lines = [
        "### Model cost by user turn",
        "",
        "| Turn | Ledger | Calls | Input | Cached | Cache write | Output | Cost |",
        "|---:|---|---:|---:|---:|---:|---:|---:|",
    ]
    all_attempts: list[TurnUsage] = []
    for index, turn in enumerate(turns, start=1):
        all_attempts.extend(turn.attempts)
        lines.append(
            _summary_row(
                str(index),
                f"`{_markdown_cell(turn.agent_name)}` total",
                turn.attempts,
            )
        )
        lines.extend(
            _summary_row("", f"{_markdown_cell(ledger.label)} (included)", ledger.attempts)
            for ledger in turn.ledgers
        )

    price = calculate_price(tuple(all_attempts))
    lines.extend(["", f"**Known subtotal:** {_format_usd(price.usd)}"])
    if price.unpriced_calls:
        lines[-1] += (
            f" · **Unpriced model {'call' if price.unpriced_calls == 1 else 'calls'}:** "
            f"{price.unpriced_calls}"
        )
    if any(turn.ledgers for turn in turns):
        lines.extend(["", "_Ledger rows are already included in their user-turn total._"])
    return "\n".join(lines)


def format_cost_breakdown(
    turns: tuple[tuple[int | None, TurnUsage], ...],
) -> str:
    lines = [
        "### Model cost detail",
        "",
        "| Turn | # | Model | Tier | Context | Input | Cached | Cache write | Output | Cost |",
        "|---:|---:|---|---|:---:|---:|---:|---:|---:|---:|",
    ]
    for index, (turn_number, turn) in enumerate(turns, start=1):
        cost = _call_cost(turn)
        lines.append(
            "| "
            + " | ".join(
                (
                    str(turn_number) if turn_number is not None else "—",
                    str(index),
                    f"`{_markdown_cell(turn.model)}`",
                    f"`{_markdown_cell(_tier_label(turn))}`",
                    f"_{_context_label(turn)}_",
                    _format_tokens(turn.prompt.total),
                    _format_tokens(turn.prompt.cache_read),
                    _format_tokens(turn.prompt.cache_write),
                    _format_tokens(turn.completion.total),
                    f"**{_format_usd(cost)}**" if cost is not None else "unpriced",
                )
            )
            + " |"
        )

    attempts = tuple(turn for _turn_number, turn in turns)
    price = calculate_price(attempts)
    lines.extend(["", f"**Known subtotal:** {_format_usd(price.usd)}"])
    if price.unpriced_calls:
        lines[-1] += (
            f" · **Unpriced model {'call' if price.unpriced_calls == 1 else 'calls'}:** "
            f"{price.unpriced_calls}"
        )
    if any(
        (turn.prompt.cache_write or 0) > 0
        and (rates := _rates(turn)) is not None
        and rates.cache_write is None
        for turn in attempts
    ):
        lines.extend(
            [
                "",
                "_Cache writes use the normal input rate when no separate write tariff exists._",
            ]
        )
    return "\n".join(lines)


async def cost_breakdown(ctx: PluginCommandActionContext) -> PluginCommandActionResult:
    mode = ctx.arguments.strip().casefold()
    if mode not in {"", "detail"}:
        return PluginCommandActionResult(message="Usage: /cost [detail]")

    if ctx.user_turn_usage:
        if mode == "detail":
            detailed = tuple(
                (turn_number, attempt)
                for turn_number, turn in enumerate(ctx.user_turn_usage, start=1)
                for attempt in turn.attempts
            )
            return PluginCommandActionResult(markdown=format_cost_breakdown(detailed))
        return PluginCommandActionResult(markdown=format_turn_rollup(ctx.user_turn_usage))

    usage = ctx.usage
    if usage is None or not usage.turns:
        return PluginCommandActionResult(markdown="_No model usage recorded for this session._")
    attempts = tuple(usage.turns)
    if mode == "detail":
        return PluginCommandActionResult(
            markdown=format_cost_breakdown(tuple((None, attempt) for attempt in attempts))
        )
    fallback = _FallbackUserTurn(agent_name=ctx.agent_name, attempts=attempts)
    return PluginCommandActionResult(markdown=format_turn_rollup((fallback,)))


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
