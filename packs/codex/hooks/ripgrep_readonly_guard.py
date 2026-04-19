"""Loop guard hook for ripgrep tools.

Goals:
- Keep ripgrep helpers bounded and predictable
- Avoid invalid rg flags (`-R/--recursive`)
- Avoid duplicate / repetitive count passes
- Enforce shell usage constraints for this subagent
"""

from __future__ import annotations

import json
import os
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
_DEFAULT_COMMAND_BUDGET = 5
_MAX_REPEATED_COUNT_SIGNATURE = 2
_DEFAULT_BROAD_SEARCH_EXCLUDES = (
    "!.git/**",
    "!node_modules/**",
    "!__pycache__/**",
    "!.venv/**",
    "!venv/**",
    "!.pytest_cache/**",
    "!dist/**",
    "!build/**",
    "!coverage/**",
)
_ENVIRONMENT_DIR_PATTERN = re.compile(r"^\s*environment_dir\s*:\s*(.+?)\s*$")

_ALLOWED_SHELL_BINARIES = {
    "rg",
    "ripgrep",
    "rg.exe",
    "ripgrep.exe",
    "find",
    "fd",
    "fdfind",
    "ls",
    "wc",
    "sort",
    "head",
    "tail",
    "cut",
    "uniq",
    "tr",
    "grep",
    "xargs",
    "awk",
    "sed",
    "printf",
    "echo",
}

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


def _shell_segments(command: str) -> list[str] | None:
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars="|&;")
        lexer.whitespace_split = True
        tokens = list(lexer)
    except ValueError:
        return None

    segments: list[str] = []
    current: list[str] = []
    for token in tokens:
        if token in {"|", "||", "&&", ";"}:
            if current:
                segments.append(shlex.join(current))
                current = []
            continue
        current.append(token)

    if current:
        segments.append(shlex.join(current))
    return segments


def _contains_shell_delimiters(command: str) -> bool:
    segments = _shell_segments(command)
    return bool(segments and len(segments) > 1)


def _is_allowed_shell_command(command: str) -> bool:
    """Allow simple rg/find/fd/ls/wc command chains for the ripgrep helper."""
    if not command.strip():
        return False

    # Keep this mode intentionally narrow: no redirection/subshell expansion.
    if any(token in command for token in (">", "<", "$(", "`")):
        return False

    segments = _shell_segments(command)
    if not segments:
        return False

    for segment in segments:
        first = _first_token(segment)
        if not first:
            return False
        if Path(first).name.lower() not in _ALLOWED_SHELL_BINARIES:
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
    payload (e.g., structured JSON containing ``roots`` / ``paths`` / ``repo_root`` /
    ``max_commands``),
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


def _extract_structured_payloads(ctx: "HookContext") -> list[dict[str, Any]]:
    payloads: list[dict[str, Any]] = []

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
            if isinstance(payload, dict):
                payloads.append(payload)

    return payloads


def _dedupe_paths(paths: list[Path]) -> list[Path]:
    deduped: list[Path] = []
    seen: set[str] = set()
    for path in paths:
        key = str(path.resolve())
        if key in seen:
            continue
        seen.add(key)
        deduped.append(path.resolve())
    return deduped


def _extract_repo_root(ctx: "HookContext") -> Path | None:
    for payload in _extract_structured_payloads(ctx):
        value = payload.get("repo_root")
        if not isinstance(value, str):
            continue
        path = Path(value)
        if path.is_absolute() and path.exists() and path.is_dir():
            return path.resolve()

    return None


def _extract_explicit_roots(ctx: "HookContext") -> list[Path]:
    """Extract explicit absolute search roots from recent user messages."""
    paths: list[Path] = []
    payloads = _extract_structured_payloads(ctx)

    for payload in payloads:
        for key in ("roots", "paths"):
            value = payload.get(key)
            if not isinstance(value, list):
                continue
            for item in value:
                if not isinstance(item, str):
                    continue
                candidate = Path(item)
                if candidate.is_absolute() and candidate.exists():
                    paths.append(candidate.resolve())
            if paths:
                return _dedupe_paths(paths)

    if payloads:
        return []

    path_pattern = re.compile(r"(/[^\s'\"]+)")

    for message in reversed(_recent_messages(ctx)):
        if getattr(message, "role", None) != "user":
            continue
        for text in _extract_text_items(getattr(message, "content", None)):
            for match in path_pattern.findall(text):
                candidate = Path(match.rstrip(".,:;)"))
                if candidate.exists():
                    paths.append(candidate.resolve())
        if paths:
            break

    return _dedupe_paths(paths)


def _extract_excludes(ctx: "HookContext") -> list[str]:
    for payload in _extract_structured_payloads(ctx):
        value = payload.get("exclude")
        if not isinstance(value, list):
            continue

        excludes: list[str] = []
        for item in value:
            if not isinstance(item, str):
                continue
            candidate = item.strip()
            if not candidate or Path(candidate).is_absolute():
                continue
            if not candidate.startswith("!"):
                candidate = f"!{candidate}"
            excludes.append(candidate)
        return excludes

    return []


def _parse_yaml_scalar(raw: str) -> str | None:
    try:
        lexer = shlex.shlex(raw, posix=True)
        lexer.whitespace_split = True
        lexer.commenters = "#"
        tokens = list(lexer)
    except ValueError:
        return None
    return tokens[0] if tokens else None


def _resolve_environment_dir_value(value: str, repo_root: Path) -> Path:
    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        candidate = repo_root / candidate
    return candidate.resolve()


def _read_environment_dir_from_config(repo_root: Path) -> Path | None:
    config_path = repo_root / "fastagent.config.yaml"
    if not config_path.is_file():
        return None

    try:
        lines = config_path.read_text().splitlines()
    except OSError:
        return None

    for line in lines:
        match = _ENVIRONMENT_DIR_PATTERN.match(line)
        if match is None:
            continue
        value = _parse_yaml_scalar(match.group(1))
        if not value:
            return None
        return _resolve_environment_dir_value(value, repo_root)

    return None


def _resolve_environment_dir(repo_root: Path | None) -> Path | None:
    if repo_root is None:
        return None

    override = os.getenv("ENVIRONMENT_DIR")
    if isinstance(override, str) and override.strip():
        return _resolve_environment_dir_value(override.strip(), repo_root)

    configured = _read_environment_dir_from_config(repo_root)
    if configured is not None:
        return configured

    return (repo_root / ".fast-agent").resolve()


def _default_broad_search_excludes(repo_root: Path | None) -> list[str]:
    excludes = list(_DEFAULT_BROAD_SEARCH_EXCLUDES)
    environment_dir = _resolve_environment_dir(repo_root)
    if environment_dir is None:
        return excludes

    sessions_dir = environment_dir / "sessions"
    try:
        relative_sessions = sessions_dir.relative_to(repo_root.resolve())
    except ValueError:
        return excludes

    excludes.append(f"!{relative_sessions.as_posix()}/**")
    return excludes


def _search_base_roots(explicit_roots: list[Path], repo_root: Path | None) -> list[Path]:
    if explicit_roots:
        return _dedupe_paths(explicit_roots)
    if repo_root is not None:
        return [repo_root.resolve()]
    return []


def _is_within_root(candidate: Path, root: Path) -> bool:
    try:
        candidate.relative_to(root.resolve())
    except ValueError:
        return False
    return True


def _fallback_root_operand(base_roots: list[Path]) -> str | None:
    if not base_roots:
        return None
    return str(base_roots[0])


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


def _normalize_relative_path_tokens(command: str, base_roots: list[Path]) -> str:
    """Normalize likely path operands against the inferred search base.

    Only adjusts rg command tokens that look like path operands (contain '/').
    """
    if not base_roots or not _is_ripgrep_command(command):
        return command
    if _contains_shell_delimiters(command):
        return command

    try:
        tokens = shlex.split(command)
    except ValueError:
        return command

    rewritten: list[str] = []
    skip_next = False
    for idx, token in enumerate(tokens):
        if skip_next:
            rewritten.append(token)
            skip_next = False
            continue
        if idx == 0 or token.startswith("-") or "/" not in token:
            if token in {"-g", "--glob"}:
                skip_next = True
            rewritten.append(token)
            continue

        token_path = Path(token)
        existing_candidate = token_path.resolve() if token_path.is_absolute() or token_path.exists() else None
        if existing_candidate is not None:
            if any(_is_within_root(existing_candidate, root) for root in base_roots):
                rewritten.append(str(existing_candidate) if token_path.is_absolute() else token)
            else:
                fallback = _fallback_root_operand(base_roots)
                rewritten.append(fallback if fallback is not None else token)
            continue

        for root in base_roots:
            candidate = (root / token).resolve()
            if not _is_within_root(candidate, root):
                continue
            if candidate.exists():
                rewritten.append(str(candidate))
                break

            trimmed = candidate
            while trimmed != root and not trimmed.exists():
                trimmed = trimmed.parent
            if trimmed.exists() and _is_within_root(trimmed, root):
                rewritten.append(str(trimmed))
                break
        else:
            fallback = _fallback_root_operand(base_roots)
            rewritten.append(fallback if fallback is not None else token)

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


def _add_ripgrep_globs(command: str, globs: list[str]) -> str:
    if not globs or _contains_shell_delimiters(command):
        return command

    try:
        tokens = shlex.split(command)
    except ValueError:
        return command

    if not tokens or Path(tokens[0]).name.lower() not in _RIPGREP_BINARIES:
        return command

    existing: set[str] = set()
    i = 0
    while i < len(tokens):
        token = tokens[i]
        if token in {"--glob", "-g"}:
            if i + 1 < len(tokens):
                existing.add(tokens[i + 1])
                i += 2
                continue
            i += 1
            continue
        if token.startswith("--glob="):
            existing.add(token.split("=", 1)[1])
        i += 1

    pending = [glob for glob in globs if glob not in existing]
    if not pending:
        return command

    insert_at = tokens.index("--") if "--" in tokens else len(tokens)
    rewritten = list(tokens[:insert_at])
    for glob in pending:
        rewritten.extend(["-g", glob])
    rewritten.extend(tokens[insert_at:])
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

    repo_root = _extract_repo_root(ctx)
    explicit_roots = _extract_explicit_roots(ctx)
    excludes = _extract_excludes(ctx)
    base_roots = _search_base_roots(explicit_roots, repo_root)
    default_excludes = [] if explicit_roots else _default_broad_search_excludes(repo_root)
    supports_pcre2 = _supports_pcre2(ctx)
    seen_commands: set[str] = getattr(ctx.runner, "_ripgrep_seen_commands", set())
    command_count: int = getattr(ctx.runner, "_ripgrep_command_count", 0)
    count_signatures: dict[str, int] = getattr(ctx.runner, "_ripgrep_count_signatures", {})
    command_budget: int = getattr(ctx.runner, "_ripgrep_command_budget", 0)
    if command_budget <= 0:
        command_budget = _extract_max_commands(ctx) or _DEFAULT_COMMAND_BUDGET
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
        cleaned = _add_ripgrep_globs(cleaned, default_excludes + excludes)
        cleaned = _normalize_relative_path_tokens(cleaned, base_roots)
        normalized = " ".join(cleaned.split())

        # Restrict shell usage.
        if not _is_allowed_shell_command(normalized):
            blocked = (
                "Blocked command: "
                f"{normalized}\n"
                "Only simple rg/find/fd/ls/wc command chains are allowed in this ripgrep helper; "
                "summarize with existing results.\n"
            )
            args["command"] = f"printf {shlex.quote(blocked)}"
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
