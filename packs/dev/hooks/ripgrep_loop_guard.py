"""Loop guard hook for ripgrep tools.

Goals:
- Keep ripgrep helpers bounded and predictable
- Avoid invalid rg flags (`-R/--recursive`)
- Avoid duplicate / repetitive count passes
- Enforce shell usage constraints for this subagent
"""

from __future__ import annotations

import json
import re
import shlex
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from fast_agent.hooks.hook_context import HookContext

_INVALID_RIPGREP_FLAGS = {"-R", "--recursive"}
_RIPGREP_BINARIES = {"rg", "ripgrep", "rg.exe", "ripgrep.exe"}
_MAX_RIPGREP_COMMANDS = 6
_MAX_REPEATED_COUNT_SIGNATURE = 2
_ALLOWED_NON_RG_PREFIXES = ("printf", "echo")

# Experiment: relax shell restrictions only for this specific card.
_RELAXED_SHELL_AGENT_NAMES = {"ripgrep_search2"}
_ALLOWED_RELAXED_BINARIES = {
    "rg",
    "ripgrep",
    "rg.exe",
    "ripgrep.exe",
    "find",
    "fd",
    "fdfind",
    "ls",
    "wc",
    "printf",
    "echo",
}

def _is_relaxed_shell_agent(agent_name: str) -> bool:
    """Return True when the agent should allow relaxed shell binaries.

    Tool-agent names may be suffixed by fast-agent instance markers, e.g.
    ``ripgrep_search2[1]``. Treat these as equivalent to the base card name.
    """

    normalized = agent_name.strip()
    for base in _RELAXED_SHELL_AGENT_NAMES:
        if normalized == base or normalized.startswith(f"{base}["):
            return True
    return False



def _first_token(command: str) -> str | None:
    try:
        tokens = shlex.split(command)
    except ValueError:
        return None
    return tokens[0] if tokens else None


def _is_ripgrep_command(command: str) -> bool:
    first = _first_token(command)
    if not first:
        return False
    return Path(first).name.lower() in _RIPGREP_BINARIES


def _contains_shell_delimiters(command: str) -> bool:
    return bool(re.search(r"\|\||&&|[|;]", command))


def _is_allowed_relaxed_shell_command(command: str) -> bool:
    """Allow simple chained/pipelined commands for selected experiment agents."""
    if not command.strip():
        return False

    # Keep this mode intentionally narrow: no redirection/subshell expansion.
    if any(token in command for token in (">", "<", "$(", "`")):
        return False

    segments = [segment.strip() for segment in re.split(r"\|\||&&|[|;]", command) if segment.strip()]
    if not segments:
        return False

    for segment in segments:
        first = _first_token(segment)
        if not first:
            return False
        if Path(first).name.lower() not in _ALLOWED_RELAXED_BINARIES:
            return False

    return True


def _extract_text_items(content: Any) -> list[str]:
    texts: list[str] = []
    if not isinstance(content, list):
        return texts
    for item in content:
        if isinstance(item, dict):
            if item.get("type") == "text" and isinstance(item.get("text"), str):
                texts.append(item["text"])
            continue
        item_type = getattr(item, "type", None)
        item_text = getattr(item, "text", None)
        if item_type == "text" and isinstance(item_text, str):
            texts.append(item_text)
    return texts


def _recent_messages(ctx: "HookContext", *, limit: int = 8) -> list[Any]:
    """Collect recent message candidates from history and current runner delta.

    When cards run with ``use_history: false``, user turns are not persisted in
    ``ctx.message_history``. In that mode we still need access to the current user
    payload (e.g., structured JSON containing ``repo_root`` / ``max_commands``),
    so we also inspect ``runner.delta_messages``.
    """

    recent: list[Any] = []

    history = list(ctx.message_history[-limit:])
    recent.extend(history)

    delta_messages = getattr(ctx.runner, "delta_messages", None)
    if isinstance(delta_messages, list):
        for message in delta_messages[-limit:]:
            if message not in recent:
                recent.append(message)

    return recent[-limit:]


def _extract_repo_roots(ctx: "HookContext") -> list[Path]:
    """Extract explicit absolute repo paths from recent user messages."""
    roots: list[Path] = []
    path_pattern = re.compile(r"(/[^\s'\"]+)")

    for message in reversed(_recent_messages(ctx)):
        if getattr(message, "role", None) != "user":
            continue
        for text in _extract_text_items(getattr(message, "content", None)):
            for match in path_pattern.findall(text):
                candidate = Path(match.rstrip(".,:;)"))
                if candidate.exists() and candidate.is_dir():
                    roots.append(candidate)
        if roots:
            break

    deduped: list[Path] = []
    seen: set[str] = set()
    for root in roots:
        key = str(root.resolve())
        if key in seen:
            continue
        seen.add(key)
        deduped.append(root)
    return deduped


def _supports_pcre2(ctx: "HookContext") -> bool:
    cached = getattr(ctx.runner, "_ripgrep_supports_pcre2", None)
    if isinstance(cached, bool):
        return cached

    try:
        result = subprocess.run(
            ["rg", "--pcre2-version"],
            check=False,
            capture_output=True,
            text=True,
            timeout=2,
        )
        supported = result.returncode == 0
    except Exception:
        supported = False

    setattr(ctx.runner, "_ripgrep_supports_pcre2", supported)
    return supported


def _strip_invalid_ripgrep_flags(command: str, *, supports_pcre2: bool) -> str:
    """Strip only known-invalid flags for rg commands.

    For complex shell pipelines/chains, avoid mutation to preserve shell semantics.
    """
    if _contains_shell_delimiters(command):
        return command

    try:
        tokens = shlex.split(command)
    except ValueError:
        return command

    if not tokens or Path(tokens[0]).name.lower() not in _RIPGREP_BINARIES:
        return command

    rewritten: list[str] = []
    for token in tokens:
        if token in _INVALID_RIPGREP_FLAGS:
            continue
        if token in {"-P", "--pcre2"} and not supports_pcre2:
            continue
        rewritten.append(token)

    return shlex.join(rewritten)


def _normalize_relative_path_tokens(command: str, repo_roots: list[Path]) -> str:
    """Normalize likely path operands against repo root when they are invalid.

    Only adjusts rg command tokens that look like path operands (contain '/').
    """
    if not repo_roots or not _is_ripgrep_command(command):
        return command
    if _contains_shell_delimiters(command):
        return command

    root = repo_roots[0]
    try:
        tokens = shlex.split(command)
    except ValueError:
        return command

    rewritten: list[str] = []
    for idx, token in enumerate(tokens):
        if idx == 0 or token.startswith("-") or "/" not in token:
            rewritten.append(token)
            continue

        token_path = Path(token)
        if token_path.is_absolute() and token_path.exists():
            rewritten.append(token)
            continue
        if token_path.exists():
            rewritten.append(token)
            continue

        candidate = (root / token).resolve()
        if candidate.exists():
            rewritten.append(str(candidate))
            continue

        trimmed = candidate
        while trimmed != root and not trimmed.exists():
            trimmed = trimmed.parent
        if trimmed.exists() and (trimmed == root or root in trimmed.parents):
            rewritten.append(str(trimmed))
            continue

        rewritten.append(token)

    return shlex.join(rewritten)


def _strip_absolute_glob_operands(command: str) -> str:
    """Remove invalid absolute-path globs (e.g. `--glob /abs/path`).

    ripgrep `--glob`/`-g` expects a glob expression, not an absolute path.
    """
    if _contains_shell_delimiters(command):
        return command

    try:
        tokens = shlex.split(command)
    except ValueError:
        return command

    if not tokens or Path(tokens[0]).name.lower() not in _RIPGREP_BINARIES:
        return command

    rewritten: list[str] = []
    i = 0
    while i < len(tokens):
        token = tokens[i]

        if token in {"--glob", "-g"}:
            if i + 1 < len(tokens):
                operand = tokens[i + 1]
                if Path(operand).is_absolute():
                    i += 2
                    continue
                rewritten.extend([token, operand])
                i += 2
                continue

            rewritten.append(token)
            i += 1
            continue

        if token.startswith("--glob="):
            operand = token.split("=", 1)[1]
            if Path(operand).is_absolute():
                i += 1
                continue

        rewritten.append(token)
        i += 1

    return shlex.join(rewritten)


def _count_signature(normalized_command: str) -> str | None:
    """Canonical signature for `rg -c` commands (ignore pattern payloads)."""
    try:
        tokens = shlex.split(normalized_command)
    except ValueError:
        return None

    if not tokens or Path(tokens[0]).name.lower() not in _RIPGREP_BINARIES:
        return None
    if "-c" not in tokens and "--count" not in tokens:
        return None

    signature_tokens: list[str] = []
    skip_next = False
    for token in tokens:
        if skip_next:
            skip_next = False
            continue
        if token in {"-e", "--regexp"}:
            signature_tokens.append(token)
            skip_next = True
            continue
        signature_tokens.append(token)

    return " ".join(signature_tokens)


def _extract_max_commands(ctx: "HookContext") -> int | None:
    """Read optional `max_commands` from structured user JSON input."""
    for message in reversed(_recent_messages(ctx)):
        if getattr(message, "role", None) != "user":
            continue

        for text in _extract_text_items(getattr(message, "content", None)):
            candidate = text.strip()
            if not (candidate.startswith("{") and candidate.endswith("}")):
                continue
            try:
                payload = json.loads(candidate)
            except Exception:
                continue
            if not isinstance(payload, dict):
                continue

            value = payload.get("max_commands")
            if isinstance(value, int):
                if value < 1:
                    return 1
                return min(value, _MAX_RIPGREP_COMMANDS)

    return None


async def ripgrep_loop_guard(ctx: "HookContext") -> None:
    """Guard ripgrep commands before execution."""
    if ctx.hook_type != "before_tool_call":
        return

    message = ctx.message
    if not message.tool_calls:
        return

    repo_roots = _extract_repo_roots(ctx)
    supports_pcre2 = _supports_pcre2(ctx)
    relaxed_mode = _is_relaxed_shell_agent(ctx.agent_name)

    seen_commands: set[str] = getattr(ctx.runner, "_ripgrep_seen_commands", set())
    command_count: int = getattr(ctx.runner, "_ripgrep_command_count", 0)
    count_signatures: dict[str, int] = getattr(ctx.runner, "_ripgrep_count_signatures", {})
    command_budget: int = getattr(ctx.runner, "_ripgrep_command_budget", 0)
    if command_budget <= 0:
        command_budget = _extract_max_commands(ctx) or _MAX_RIPGREP_COMMANDS
    budget_exhausted: bool = bool(getattr(ctx.runner, "_ripgrep_budget_exhausted", False))

    for tool_call in message.tool_calls.values():
        if tool_call.params.name != "execute":
            continue

        args = tool_call.params.arguments
        if not isinstance(args, dict):
            continue

        command = args.get("command")
        if not isinstance(command, str):
            continue

        if budget_exhausted:
            args["command"] = (
                "printf 'Search command budget reached; STOP. Do not call tools again; return final best-effort summary now.\n'"
            )
            continue

        cleaned = _strip_invalid_ripgrep_flags(command, supports_pcre2=supports_pcre2)
        cleaned = _strip_absolute_glob_operands(cleaned)
        cleaned = _normalize_relative_path_tokens(cleaned, repo_roots)
        normalized = " ".join(cleaned.split())

        # Restrict shell usage.
        if relaxed_mode:
            if not _is_allowed_relaxed_shell_command(normalized):
                args["command"] = (
                    "printf 'Only simple rg/find/fd/ls/wc command chains are allowed in this ripgrep helper; summarize with existing results.\\n'"
                )
                continue
        elif not _is_ripgrep_command(normalized):
            if not normalized.startswith(_ALLOWED_NON_RG_PREFIXES):
                args["command"] = (
                    "printf 'Only rg commands are allowed in this ripgrep helper; summarize with existing results.\\n'"
                )
                continue

        # Exact dedupe
        if normalized in seen_commands:
            args["command"] = "printf 'Skipped duplicate rg command to avoid loop.\\n'"
            continue

        # Limit repeated count-only exploration signatures
        signature = _count_signature(normalized)
        if signature is not None:
            seen_count = count_signatures.get(signature, 0)
            if seen_count >= _MAX_REPEATED_COUNT_SIGNATURE:
                args["command"] = "printf 'Count-query budget reached; summarize current findings.\\n'"
                continue
            count_signatures[signature] = seen_count + 1

        # Hard overall command budget
        if command_count >= command_budget:
            args["command"] = (
                "printf 'Search command budget reached; STOP. Do not call tools again; return final best-effort summary now.\n'"
            )
            budget_exhausted = True
            continue
        command_count += 1

        seen_commands.add(normalized)
        args["command"] = cleaned

    setattr(ctx.runner, "_ripgrep_seen_commands", seen_commands)
    setattr(ctx.runner, "_ripgrep_command_count", command_count)
    setattr(ctx.runner, "_ripgrep_count_signatures", count_signatures)
    setattr(ctx.runner, "_ripgrep_command_budget", command_budget)
    setattr(ctx.runner, "_ripgrep_budget_exhausted", budget_exhausted)
