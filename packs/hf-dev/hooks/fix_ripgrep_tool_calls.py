"""Lightweight guard for the smart card-pack ripgrep helper."""

from __future__ import annotations

import json
import os
import re
import shlex
from pathlib import Path
from typing import TYPE_CHECKING, Any

from fast_agent.core.logging.logger import get_logger

if TYPE_CHECKING:
    from fast_agent.hooks.hook_context import HookContext

logger = get_logger(__name__)

_TOOL_NAME_CORRECTIONS = {
    "exec": "execute",
    "executescript": "execute",
    "execscript": "execute",
    "executor": "execute",
    "exec_command": "execute",
}
_INVALID_RIPGREP_FLAGS = {"-R", "--recursive"}
_RIPGREP_BINARIES = {"rg", "ripgrep", "rg.exe", "ripgrep.exe"}
_ALLOWED_BINARIES = {
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
    "sed",
    "awk",
    "xargs",
    "printf",
    "echo",
}
_DEFAULT_COMMAND_BUDGET = 6
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


def _first_token(command: str) -> str | None:
    try:
        tokens = shlex.split(command)
    except ValueError:
        return None
    return tokens[0] if tokens else None


def _is_ripgrep_command(command: str) -> bool:
    first = _first_token(command)
    return bool(first and Path(first).name.lower() in _RIPGREP_BINARIES)


def _split_shell_segments(command: str) -> list[str] | None:
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


def _normalize_tool_name(name: str) -> tuple[str, bool]:
    corrected = _TOOL_NAME_CORRECTIONS.get(name)
    if corrected is not None:
        return corrected, True
    if name.startswith("exec") and name != "execute":
        return "execute", True
    return name, False


def _is_allowed_shell_command(command: str) -> bool:
    if not command.strip():
        return False
    if any(token in command for token in (">", "<", "$(", "`")):
        return False

    segments = _split_shell_segments(command)
    if not segments:
        return False

    for segment in segments:
        first = _first_token(segment)
        if not first or Path(first).name.lower() not in _ALLOWED_BINARIES:
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
    recent = list(ctx.message_history[-limit:])
    delta_messages = getattr(ctx.runner, "delta_messages", None)
    if isinstance(delta_messages, list):
        for message in delta_messages[-limit:]:
            if message not in recent:
                recent.append(message)
    return recent[-limit:]


def _extract_command_budget(ctx: "HookContext") -> int:
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
                return max(1, min(value, _DEFAULT_COMMAND_BUDGET))

    return _DEFAULT_COMMAND_BUDGET


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


def _extract_repo_root(ctx: "HookContext") -> Path | None:
    for payload in _extract_structured_payloads(ctx):
        value = payload.get("repo_root")
        if not isinstance(value, str):
            continue
        path = Path(value)
        if path.is_absolute() and path.exists() and path.is_dir():
            return path.resolve()

    return None


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


def _extract_explicit_roots(ctx: "HookContext") -> list[Path]:
    roots: list[Path] = []

    for payload in _extract_structured_payloads(ctx):
        for key in ("roots", "paths"):
            value = payload.get(key)
            if not isinstance(value, list):
                continue

            for item in value:
                if not isinstance(item, str):
                    continue
                candidate = Path(item)
                if candidate.is_absolute() and candidate.exists():
                    roots.append(candidate.resolve())
            if roots:
                return _dedupe_paths(roots)

    return []


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


def _strip_invalid_ripgrep_flags(command: str) -> tuple[str, bool]:
    if not _is_ripgrep_command(command):
        return command, False

    segments = _split_shell_segments(command)
    if not segments or len(segments) > 1:
        return command, False

    try:
        tokens = shlex.split(command)
    except ValueError:
        return command, False

    rewritten = [token for token in tokens if token not in _INVALID_RIPGREP_FLAGS]
    normalized = shlex.join(rewritten)
    return normalized, normalized != command


def _strip_absolute_glob_operands(command: str) -> tuple[str, bool]:
    if not _is_ripgrep_command(command):
        return command, False

    segments = _split_shell_segments(command)
    if not segments or len(segments) > 1:
        return command, False

    try:
        tokens = shlex.split(command)
    except ValueError:
        return command, False

    is_rg_files = "--files" in tokens
    rewritten: list[str] = []
    salvaged_paths: list[str] = []
    changed = False
    i = 0
    while i < len(tokens):
        token = tokens[i]
        if token in {"-g", "--glob"} and i + 1 < len(tokens):
            operand = tokens[i + 1]
            if Path(operand).is_absolute():
                changed = True
                if is_rg_files and Path(operand).exists():
                    salvaged_paths.append(operand)
                i += 2
                continue
            rewritten.extend([token, operand])
            i += 2
            continue

        if token.startswith("--glob="):
            operand = token.split("=", 1)[1]
            if Path(operand).is_absolute():
                changed = True
                if is_rg_files and Path(operand).exists():
                    salvaged_paths.append(operand)
                i += 1
                continue

        rewritten.append(token)
        i += 1

    if salvaged_paths:
        rewritten.extend(salvaged_paths)

    return shlex.join(rewritten), changed


def _add_ripgrep_globs(command: str, globs: list[str]) -> tuple[str, bool]:
    if not globs or not _is_ripgrep_command(command):
        return command, False

    segments = _split_shell_segments(command)
    if not segments or len(segments) > 1:
        return command, False

    try:
        tokens = shlex.split(command)
    except ValueError:
        return command, False

    existing: set[str] = set()
    i = 0
    while i < len(tokens):
        token = tokens[i]
        if token in {"-g", "--glob"} and i + 1 < len(tokens):
            existing.add(tokens[i + 1])
            i += 2
            continue
        if token.startswith("--glob="):
            existing.add(token.split("=", 1)[1])
        i += 1

    pending = [glob for glob in globs if glob not in existing]
    if not pending:
        return command, False

    insert_at = tokens.index("--") if "--" in tokens else len(tokens)
    rewritten = list(tokens[:insert_at])
    for glob in pending:
        rewritten.extend(["-g", glob])
    rewritten.extend(tokens[insert_at:])
    return shlex.join(rewritten), True


def _normalize_relative_rg_paths(command: str, base_roots: list[Path]) -> str:
    if not base_roots or not _is_ripgrep_command(command):
        return command

    segments = _split_shell_segments(command)
    if not segments or len(segments) > 1:
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
        else:
            fallback = _fallback_root_operand(base_roots)
            rewritten.append(fallback if fallback is not None else token)

    return shlex.join(rewritten)


async def fix_ripgrep_tool_calls(ctx: "HookContext") -> None:
    """Normalize tool calls and keep ripgrep search loops bounded."""
    if ctx.hook_type != "before_tool_call":
        return

    message = ctx.message
    if not message.tool_calls:
        return

    seen_commands: set[str] = getattr(ctx.runner, "_ripgrep_seen_commands", set())
    command_count: int = getattr(ctx.runner, "_ripgrep_command_count", 0)
    command_budget: int = getattr(ctx.runner, "_ripgrep_command_budget", 0) or _extract_command_budget(ctx)
    budget_exhausted: bool = bool(getattr(ctx.runner, "_ripgrep_budget_exhausted", False))
    repo_root = _extract_repo_root(ctx)
    explicit_roots = _extract_explicit_roots(ctx)
    excludes = _extract_excludes(ctx)
    base_roots = _search_base_roots(explicit_roots, repo_root)
    default_excludes = [] if explicit_roots else _default_broad_search_excludes(repo_root)

    for tool_id, tool_call in message.tool_calls.items():
        normalized_name, corrected = _normalize_tool_name(tool_call.params.name)
        if corrected:
            logger.info(
                "Corrected hallucinated tool name",
                data={"tool_id": tool_id, "original": tool_call.params.name, "corrected": normalized_name},
            )
            tool_call.params.name = normalized_name

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
                "printf 'Search command budget reached; STOP. Do not call tools again; return final best-effort summary now.\\n'"
            )
            continue

        cleaned, changed_flags = _strip_invalid_ripgrep_flags(command)
        cleaned, changed_globs = _strip_absolute_glob_operands(cleaned)
        cleaned, added_globs = _add_ripgrep_globs(cleaned, default_excludes + excludes)
        cleaned = _normalize_relative_rg_paths(cleaned, base_roots)
        normalized = " ".join(cleaned.split())

        if changed_flags:
            logger.info(
                "Removed invalid recursive flags from ripgrep command",
                data={"tool_id": tool_id, "original": command, "modified": cleaned},
            )
        elif changed_globs:
            logger.info(
                "Removed invalid absolute glob operand from ripgrep command",
                data={"tool_id": tool_id, "original": command, "modified": cleaned},
            )
        elif added_globs:
            logger.info(
                "Added ripgrep glob excludes",
                data={"tool_id": tool_id, "original": command, "modified": cleaned},
            )

        if not _is_allowed_shell_command(normalized):
            args["command"] = (
                "printf 'Only simple allowed read-only command chains are allowed in this ripgrep helper; summarize with existing results.\\n'"
            )
            continue

        if normalized in seen_commands:
            args["command"] = "printf 'Skipped duplicate rg command to avoid loop.\\n'"
            continue

        if command_count >= command_budget:
            args["command"] = (
                "printf 'Search command budget reached; STOP. Do not call tools again; return final best-effort summary now.\\n'"
            )
            budget_exhausted = True
            continue

        command_count += 1
        seen_commands.add(normalized)
        args["command"] = cleaned

    setattr(ctx.runner, "_ripgrep_seen_commands", seen_commands)
    setattr(ctx.runner, "_ripgrep_command_count", command_count)
    setattr(ctx.runner, "_ripgrep_command_budget", command_budget)
    setattr(ctx.runner, "_ripgrep_budget_exhausted", budget_exhausted)
