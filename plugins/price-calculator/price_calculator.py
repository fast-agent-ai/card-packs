"""Post-turn price estimates from canonical fast-agent usage."""

from __future__ import annotations

import asyncio
import json
import math
import os
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime
from itertools import combinations
from pathlib import Path
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
_CATALOG_SCHEMA = "fast-agent.pricing/v1"
_CATALOG_PATH = Path(__file__).with_name("pricing_catalog.json")
_HERDR_SOURCE = "herdr:fast-agent:price-calculator"
_HERDR_AGENT_SOURCE = "herdr:fast-agent"
_HERDR_AGENT_LABEL = "fast-agent"
_HERDR_TIMEOUT_SECONDS = 1.0
_WINDOWS_CREATE_NO_WINDOW = 0x08000000


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


@dataclass(frozen=True, slots=True)
class _ModelMatcher:
    values: frozenset[str]
    path_suffix: bool = False
    strip_prefixes: tuple[str, ...] = ()
    strip_after: str | None = None

    def matches(self, model: str) -> bool:
        candidate = model.casefold()
        if self.strip_after is not None:
            candidate = candidate.partition(self.strip_after)[0]
        for prefix in self.strip_prefixes:
            candidate = candidate.removeprefix(prefix)
        return candidate in self.values or (
            self.path_suffix
            and any(candidate.endswith(f"/{value}") for value in self.values)
        )


@dataclass(frozen=True, slots=True)
class _PricingRule:
    id: str
    model: _ModelMatcher
    rates: Rates
    providers: frozenset[str] | None = None
    upstream_providers: frozenset[str] | None = None
    service_tiers: frozenset[str] | None = None
    prompt_min: int = 0
    prompt_max: int | None = None
    context: str | None = None
    effective_from: float | None = None
    effective_until: float | None = None

    @property
    def specificity(self) -> tuple[int, int, int, int, int, int, int]:
        return (
            int(self.providers is not None) + int(self.upstream_providers is not None),
            int(self.upstream_providers is not None),
            int(self.providers is not None),
            int(self.service_tiers is not None),
            int(self.prompt_min > 0) + int(self.prompt_max is not None),
            int(self.effective_from is not None)
            + int(self.effective_until is not None),
            int(not self.model.path_suffix),
        )

    def matches(
        self,
        turn: TurnUsage,
        *,
        include_tier: bool = True,
    ) -> bool:
        prompt_total = turn.prompt.total
        if prompt_total is None or not self.model.matches(turn.model):
            return False
        if self.providers is not None and _provider_name(turn) not in self.providers:
            return False
        upstream_provider = (
            turn.upstream_provider.casefold()
            if turn.upstream_provider is not None
            else None
        )
        if (
            self.upstream_providers is not None
            and upstream_provider not in self.upstream_providers
        ):
            return False
        if include_tier and self.service_tiers is not None:
            if _normalized_service_tier(turn) not in self.service_tiers:
                return False
        if prompt_total < self.prompt_min:
            return False
        if self.prompt_max is not None and prompt_total > self.prompt_max:
            return False
        if self.effective_from is not None and turn.timestamp < self.effective_from:
            return False
        return self.effective_until is None or turn.timestamp < self.effective_until


@dataclass(frozen=True, slots=True)
class _PricingCatalog:
    version: str
    rules: tuple[_PricingRule, ...]

    def resolve(self, turn: TurnUsage) -> Rates | None:
        matches = [rule for rule in self.rules if rule.matches(turn)]
        if not matches:
            return None
        return _select_rule(matches).rates

    def context_label(self, turn: TurnUsage) -> str:
        matches = [
            rule
            for rule in self.rules
            if rule.context is not None and rule.matches(turn)
        ]
        if not matches:
            matches = [
                rule
                for rule in self.rules
                if rule.context is not None and rule.matches(turn, include_tier=False)
            ]
        if not matches:
            return "—"
        best_specificity = max(rule.specificity for rule in matches)
        contexts = {
            rule.context for rule in matches if rule.specificity == best_specificity
        }
        return contexts.pop() if len(contexts) == 1 else "—"


def _select_rule(rules: list[_PricingRule]) -> _PricingRule:
    best_specificity = max(rule.specificity for rule in rules)
    best = [rule for rule in rules if rule.specificity == best_specificity]
    if len(best) != 1:
        ids = ", ".join(rule.id for rule in best)
        raise ValueError(f"Ambiguous pricing rules: {ids}")
    return best[0]


def _provider_name(turn: TurnUsage) -> str:
    provider = turn.provider
    if isinstance(provider, str):
        return provider.casefold()
    return provider.config_name.casefold()


def _normalized_service_tier(turn: TurnUsage) -> str:
    tier = turn.requested_service_tier or turn.service_tier
    return "standard" if tier in (None, "default", "standard") else tier.casefold()


def _require_object(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise ValueError(f"{label} must be an object")
    return value


def _check_keys(value: dict[str, object], allowed: set[str], label: str) -> None:
    unknown = value.keys() - allowed
    if unknown:
        raise ValueError(f"{label} has unknown fields: {', '.join(sorted(unknown))}")


def _require_string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a non-empty string")
    return value.strip()


def _string_set(value: object, label: str) -> frozenset[str]:
    if not isinstance(value, list) or not value:
        raise ValueError(f"{label} must be a non-empty list")
    return frozenset(_require_string(item, label).casefold() for item in value)


def _optional_string_set(
    value: object,
    label: str,
) -> frozenset[str] | None:
    return None if value is None else _string_set(value, label)


def _optional_token_limit(value: object, label: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{label} must be a non-negative integer or null")
    return value


def _rate(value: object, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise ValueError(f"{label} must be a non-negative number")
    try:
        parsed = float(value)
    except ValueError as exc:
        raise ValueError(f"{label} must be a non-negative number") from exc
    if not math.isfinite(parsed) or parsed < 0:
        raise ValueError(f"{label} must be a non-negative finite number")
    return parsed


def _effective_time(value: object, label: str) -> float | None:
    if value is None:
        return None
    text = _require_string(value, label)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{label} must be an ISO 8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{label} must include a timezone")
    return parsed.timestamp()


def _parse_model_matcher(value: object, label: str) -> _ModelMatcher:
    payload = _require_object(value, label)
    _check_keys(
        payload,
        {"values", "path_suffix", "strip_prefixes", "strip_after"},
        label,
    )
    strip_after = payload.get("strip_after")
    if strip_after is not None:
        strip_after = _require_string(strip_after, f"{label}.strip_after")
    path_suffix = payload.get("path_suffix", False)
    if not isinstance(path_suffix, bool):
        raise ValueError(f"{label}.path_suffix must be a boolean")
    strip_prefixes = payload.get("strip_prefixes", [])
    if not isinstance(strip_prefixes, list):
        raise ValueError(f"{label}.strip_prefixes must be a list")
    return _ModelMatcher(
        values=_string_set(payload.get("values"), f"{label}.values"),
        path_suffix=path_suffix,
        strip_prefixes=tuple(
            _require_string(prefix, f"{label}.strip_prefixes").casefold()
            for prefix in strip_prefixes
        ),
        strip_after=strip_after,
    )


def _parse_rates(value: object, label: str) -> Rates:
    payload = _require_object(value, label)
    _check_keys(payload, {"input", "cache_read", "cache_write", "output"}, label)
    for required in ("input", "cache_read", "output"):
        if required not in payload:
            raise ValueError(f"{label}.{required} is required")
    cache_write = payload.get("cache_write")
    return Rates(
        input=_rate(payload["input"], f"{label}.input"),
        cached_input=_rate(payload["cache_read"], f"{label}.cache_read"),
        cache_write=(
            None if cache_write is None else _rate(cache_write, f"{label}.cache_write")
        ),
        output=_rate(payload["output"], f"{label}.output"),
    )


def _parse_rule(value: object, index: int) -> _PricingRule:
    label = f"rules[{index}]"
    payload = _require_object(value, label)
    _check_keys(
        payload,
        {
            "id",
            "match",
            "prompt_tokens",
            "context",
            "effective",
            "rates",
        },
        label,
    )
    match = _require_object(payload.get("match"), f"{label}.match")
    _check_keys(
        match,
        {"model", "providers", "upstream_providers", "service_tiers"},
        f"{label}.match",
    )
    prompt = _require_object(payload.get("prompt_tokens", {}), f"{label}.prompt_tokens")
    _check_keys(prompt, {"minimum", "maximum"}, f"{label}.prompt_tokens")
    prompt_min = _optional_token_limit(
        prompt.get("minimum", 0),
        f"{label}.prompt_tokens.minimum",
    )
    prompt_max = _optional_token_limit(
        prompt.get("maximum"),
        f"{label}.prompt_tokens.maximum",
    )
    assert prompt_min is not None
    if prompt_max is not None and prompt_min > prompt_max:
        raise ValueError(f"{label}.prompt_tokens minimum exceeds maximum")
    effective = _require_object(payload.get("effective", {}), f"{label}.effective")
    _check_keys(effective, {"from", "until"}, f"{label}.effective")
    effective_from = _effective_time(effective.get("from"), f"{label}.effective.from")
    effective_until = _effective_time(
        effective.get("until"), f"{label}.effective.until"
    )
    if (
        effective_from is not None
        and effective_until is not None
        and effective_from >= effective_until
    ):
        raise ValueError(f"{label}.effective from must precede until")
    context = payload.get("context")
    if context is not None:
        context = _require_string(context, f"{label}.context")
    providers = _optional_string_set(
        match.get("providers"),
        f"{label}.match.providers",
    )
    upstream_providers = _optional_string_set(
        match.get("upstream_providers"),
        f"{label}.match.upstream_providers",
    )
    if upstream_providers is not None and providers is None:
        raise ValueError(f"{label}.match.upstream_providers requires match.providers")
    return _PricingRule(
        id=_require_string(payload.get("id"), f"{label}.id"),
        model=_parse_model_matcher(match.get("model"), f"{label}.match.model"),
        providers=providers,
        upstream_providers=upstream_providers,
        service_tiers=_optional_string_set(
            match.get("service_tiers"),
            f"{label}.match.service_tiers",
        ),
        prompt_min=prompt_min,
        prompt_max=prompt_max,
        context=context,
        effective_from=effective_from,
        effective_until=effective_until,
        rates=_parse_rates(payload.get("rates"), f"{label}.rates"),
    )


def _sets_overlap(
    left: frozenset[str] | None,
    right: frozenset[str] | None,
) -> bool:
    return left is None or right is None or bool(left & right)


def _ranges_overlap(
    left_min: int,
    left_max: int | None,
    right_min: int,
    right_max: int | None,
) -> bool:
    return (left_max is None or right_min <= left_max) and (
        right_max is None or left_min <= right_max
    )


def _times_overlap(left: _PricingRule, right: _PricingRule) -> bool:
    return (
        left.effective_until is None
        or right.effective_from is None
        or right.effective_from < left.effective_until
    ) and (
        right.effective_until is None
        or left.effective_from is None
        or left.effective_from < right.effective_until
    )


def _model_examples(matcher: _ModelMatcher) -> set[str]:
    examples = set(matcher.values)
    for value in matcher.values:
        examples.update(f"{prefix}{value}" for prefix in matcher.strip_prefixes)
        if matcher.path_suffix:
            examples.add(f"owner/{value}")
        if matcher.strip_after is not None:
            examples.add(f"{value}{matcher.strip_after}route")
    return examples


def _model_matchers_overlap(left: _ModelMatcher, right: _ModelMatcher) -> bool:
    examples = _model_examples(left) | _model_examples(right)
    return any(left.matches(example) and right.matches(example) for example in examples)


def _rules_overlap(left: _PricingRule, right: _PricingRule) -> bool:
    return (
        left.specificity == right.specificity
        and _model_matchers_overlap(left.model, right.model)
        and _sets_overlap(left.providers, right.providers)
        and _sets_overlap(left.upstream_providers, right.upstream_providers)
        and _sets_overlap(left.service_tiers, right.service_tiers)
        and _ranges_overlap(
            left.prompt_min,
            left.prompt_max,
            right.prompt_min,
            right.prompt_max,
        )
        and _times_overlap(left, right)
    )


def _validate_rule_overlaps(rules: tuple[_PricingRule, ...]) -> None:
    for left, right in combinations(rules, 2):
        if _rules_overlap(left, right):
            raise ValueError(f"Ambiguous pricing rules: {left.id}, {right.id}")


def _parse_catalog(payload: object) -> _PricingCatalog:
    root = _require_object(payload, "catalog")
    _check_keys(
        root,
        {"schema", "catalog_version", "currency", "unit", "rules"},
        "catalog",
    )
    if root.get("schema") != _CATALOG_SCHEMA:
        raise ValueError(f"catalog.schema must be {_CATALOG_SCHEMA}")
    if root.get("currency") != "USD":
        raise ValueError("catalog.currency must be USD")
    if root.get("unit") != "usd_per_million_tokens":
        raise ValueError("catalog.unit must be usd_per_million_tokens")
    rules_payload = root.get("rules")
    if not isinstance(rules_payload, list) or not rules_payload:
        raise ValueError("catalog.rules must be a non-empty list")
    rules = tuple(_parse_rule(rule, index) for index, rule in enumerate(rules_payload))
    ids = [rule.id for rule in rules]
    if len(ids) != len(set(ids)):
        raise ValueError("catalog rule IDs must be unique")
    _validate_rule_overlaps(rules)
    return _PricingCatalog(
        version=_require_string(root.get("catalog_version"), "catalog.catalog_version"),
        rules=rules,
    )


def _load_catalog(path: Path = _CATALOG_PATH) -> _PricingCatalog:
    return _parse_catalog(json.loads(path.read_text(encoding="utf-8")))


_PRICING_CATALOG = _load_catalog()


def _rates(turn: TurnUsage) -> Rates | None:
    return _PRICING_CATALOG.resolve(turn)


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


def _session_cost_token(price: Price, *, has_usage: bool) -> str | None:
    if not has_usage or (price.unpriced_calls and price.usd == 0):
        return None
    incomplete = "+" if price.unpriced_calls else ""
    return f"{_format_adaptive_usd(price.usd)}{incomplete} session"


def _herdr_metadata_command(value: str | None) -> list[str] | None:
    if os.environ.get("HERDR_ENV") != "1":
        return None

    pane_id = os.environ.get("HERDR_PANE_ID", "").strip()
    if not pane_id:
        return None

    command = [
        os.environ.get("HERDR_BIN_PATH", "").strip() or "herdr",
        "pane",
        "report-metadata",
        pane_id,
        "--source",
        _HERDR_SOURCE,
        "--applies-to-source",
        _HERDR_AGENT_SOURCE,
        "--agent",
        _HERDR_AGENT_LABEL,
    ]
    if value is None:
        command.extend(("--clear-token", "cost"))
    else:
        command.extend(("--token", f"cost={value}"))
    command.extend(("--seq", str(time.time_ns())))
    return command


def _run_herdr_metadata_command(command: list[str]) -> None:
    subprocess.run(
        command,
        check=False,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=_HERDR_TIMEOUT_SECONDS,
        creationflags=_WINDOWS_CREATE_NO_WINDOW if os.name == "nt" else 0,
    )


async def _report_session_cost_to_herdr(value: str | None) -> None:
    command = _herdr_metadata_command(value)
    if command is None:
        return
    try:
        await asyncio.to_thread(_run_herdr_metadata_command, command)
    except (OSError, RuntimeError, subprocess.SubprocessError):
        # Sidebar metadata is optional and must not suppress the normal cost line.
        return


def _tier_label(turn: TurnUsage) -> str:
    tier = turn.requested_service_tier or turn.service_tier
    return "standard" if tier in (None, "default", "standard") else tier


def _context_label(turn: TurnUsage) -> str:
    return _PRICING_CATALOG.context_label(turn)


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
        await _report_session_cost_to_herdr(None)
        return None

    turn = calculate_price(ctx.turn_usage)
    session = calculate_price(ctx.session_usage)
    await _report_session_cost_to_herdr(
        _session_cost_token(session, has_usage=bool(ctx.session_usage))
    )
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
