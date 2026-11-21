"""Render Mermaid code fences via merman-cli as terminal PNGs or Unicode text."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import re
import shutil
import sys
import tempfile
import types
from dataclasses import dataclass, fields, replace
from pathlib import Path
from typing import TYPE_CHECKING

from rich.markup import escape as escape_markup

from fast_agent.command_actions import (
    PluginCommandActionContext,
    PluginCommandActionImage,
    PluginCommandActionResult,
    PluginCommandCompletion,
    PluginCommandCompletionContext,
)
from fast_agent.plugins import PluginPostUserTurnContext, PluginPostUserTurnOutput
from fast_agent.ui.mermaid_utils import (
    MermaidDiagram,
    detect_diagram_type,
    extract_mermaid_diagrams,
)

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator, Mapping

    from fast_agent.types import PromptMessageExtended

_NAME = "mermaid-render"
_MERMAN_BIN = "merman-cli"
_MERMAN_VERSION = "0.10.0"
_MERMAN_RELEASE_BASE = f"https://github.com/Latias94/merman/releases/download/v{_MERMAN_VERSION}"
_PNG = b"\x89PNG\r\n\x1a\n"
_POST_TURN_IMAGES = any(field.name == "images" for field in fields(PluginPostUserTurnOutput))
_LOG = logging.getLogger(__name__)
_THEMES = ("dark", "default", "forest", "neutral")
_MODES = ("image", "text")


def _session_overrides() -> dict[str, object]:
    """Share session overrides across command and hook module loads in this process."""
    key = "_fast_agent_plugin_state_mermaid_render"
    state = sys.modules.get(key)
    if state is None:
        state = types.ModuleType(key)
        state.__dict__["overrides"] = {}
        sys.modules[key] = state
    return state.__dict__["overrides"]


@dataclass(frozen=True)
class _Settings:
    mode: str = "image"
    theme: str = "dark"
    background: str = "#1e1e2e"
    auto_render: bool = True
    max_diagrams: int = 3
    max_width: int | None = None
    timeout_seconds: float = 15.0
    cache_dir: Path = Path("~/.cache/fast-agent/mermaid")

    @classmethod
    def from_config(
        cls,
        config: Mapping[str, object],
        *,
        overrides: Mapping[str, object] | None = None,
    ) -> _Settings:
        merged: dict[str, object] = dict(config)
        if overrides:
            merged.update(overrides)
        defaults = cls()

        mode = merged.get("mode", defaults.mode)
        if not isinstance(mode, str) or mode not in _MODES:
            raise ValueError("mode must be image or text")

        theme = merged.get("theme", defaults.theme)
        if not isinstance(theme, str) or theme not in _THEMES:
            raise ValueError("theme must be dark, default, forest, or neutral")

        background = merged.get("background", defaults.background)
        if not isinstance(background, str) or not background.strip():
            raise ValueError("background must be a non-empty color string")

        auto_render = merged.get("auto_render", defaults.auto_render)
        if not isinstance(auto_render, bool):
            raise ValueError("auto_render must be true or false")

        limit = merged.get("max_diagrams", defaults.max_diagrams)
        if not isinstance(limit, int) or isinstance(limit, bool) or limit < 0:
            raise ValueError("max_diagrams must be a non-negative integer")

        max_width = merged.get("max_width", defaults.max_width)
        if max_width is not None and (
            not isinstance(max_width, int) or isinstance(max_width, bool) or max_width < 20
        ):
            raise ValueError("max_width must be an integer >= 20 or null")

        timeout = merged.get("timeout_seconds", defaults.timeout_seconds)
        if (
            not isinstance(timeout, (int, float))
            or isinstance(timeout, bool)
            or not 0 < timeout < math.inf
        ):
            raise ValueError("timeout_seconds must be a finite positive number")

        cache_dir = merged.get("cache_dir")
        if cache_dir is not None and (not isinstance(cache_dir, str) or not cache_dir.strip()):
            raise ValueError("cache_dir must be a non-empty path or null")
        try:
            cache = (Path(cache_dir) if cache_dir is not None else defaults.cache_dir).expanduser()
        except RuntimeError as exc:
            raise ValueError("could not expand cache_dir") from exc

        return cls(
            mode=mode,
            theme=theme,
            background=background.strip(),
            auto_render=auto_render,
            max_diagrams=limit,
            max_width=max_width,
            timeout_seconds=float(timeout),
            cache_dir=cache.absolute(),
        )


class _RenderError(Exception):
    """A short, user-facing rendering error without diagram source."""


def _find_merman() -> str | None:
    found = shutil.which(_MERMAN_BIN)
    if found:
        return found
    for candidate_dir in (Path.home() / ".local" / "bin", Path.home() / ".cargo" / "bin"):
        candidate = candidate_dir / (
            f"{_MERMAN_BIN}.exe" if sys.platform == "win32" else _MERMAN_BIN
        )
        if candidate.is_file():
            return str(candidate)
    return None


def _install_plan() -> tuple[str, list[str], str]:
    """Return (display_command, argv, description) for installing merman-cli."""
    if shutil.which("brew"):
        cmd = "brew install merman-cli"
        return cmd, ["brew", "install", "merman-cli"], "Homebrew formula"
    if sys.platform == "win32":
        url = f"{_MERMAN_RELEASE_BASE}/merman-cli-installer.ps1"
        cmd = f'powershell -ExecutionPolicy Bypass -c "irm {url} | iex"'
        return (
            cmd,
            ["powershell", "-ExecutionPolicy", "Bypass", "-c", f"irm {url} | iex"],
            f"pinned GitHub release v{_MERMAN_VERSION} PowerShell installer",
        )
    url = f"{_MERMAN_RELEASE_BASE}/merman-cli-installer.sh"
    cmd = f"curl --proto '=https' --tlsv1.2 -LsSf {url} | sh"
    return (
        cmd,
        ["sh", "-c", cmd],
        f"pinned GitHub release v{_MERMAN_VERSION} shell installer (verifies SHA-256)",
    )


def _effective_text_width(settings: _Settings) -> int:
    if settings.max_width is not None:
        return settings.max_width
    columns = shutil.get_terminal_size((80, 24)).columns
    return max(40, min(columns - 2, 120))


async def _run_subprocess(
    args: list[str],
    *,
    timeout: float,
    capture_output: bool = False,
) -> tuple[bytes, bytes]:
    process = await asyncio.create_subprocess_exec(
        *args,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE if capture_output else asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE if capture_output else asyncio.subprocess.DEVNULL,
    )
    try:
        if capture_output:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout)
        else:
            await asyncio.wait_for(process.wait(), timeout)
            stdout, stderr = b"", b""
    except (TimeoutError, asyncio.CancelledError):
        try:
            process.kill()
        except ProcessLookupError:
            pass
        await process.wait()
        raise
    if process.returncode:
        detail = stderr.decode("utf-8", errors="replace").strip().splitlines()
        suffix = f": {detail[-1]}" if detail else ""
        raise _RenderError(f"{Path(args[0]).name} exited with status {process.returncode}{suffix}")
    return stdout or b"", stderr or b""


def _is_png(path: Path) -> bool:
    try:
        with path.open("rb") as file:
            return file.read(len(_PNG)) == _PNG
    except FileNotFoundError:
        return False


async def _render_image(
    diagram: MermaidDiagram,
    settings: _Settings,
    executable: str | None,
) -> PluginCommandActionImage:
    if executable is None:
        raise _RenderError(f"{_MERMAN_BIN} is not installed (run `/mermaid install`)")
    code = diagram.content
    key = json.dumps([code, settings.theme, settings.background, "merman"])
    cached = settings.cache_dir / f"{hashlib.sha256(key.encode('utf-8')).hexdigest()}.png"
    if not _is_png(cached):
        settings.cache_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=settings.cache_dir) as staging:
            source = Path(staging) / "diagram.mmd"
            output = Path(staging) / "diagram.png"
            source.write_text(code, encoding="utf-8")
            await _run_subprocess(
                [
                    executable,
                    "mmdc",
                    "-i",
                    str(source),
                    "-o",
                    str(output),
                    "-t",
                    settings.theme,
                    "-b",
                    settings.background,
                ],
                timeout=settings.timeout_seconds,
            )
            if not _is_png(output):
                raise _RenderError("merman-cli did not produce a PNG")
            output.replace(cached)

    return PluginCommandActionImage(
        source=str(cached),
        label=diagram.title or detect_diagram_type(code),
        mime_type="image/png",
    )


async def _render_text(
    diagram: MermaidDiagram,
    settings: _Settings,
    executable: str | None,
) -> str:
    if executable is None:
        raise _RenderError(f"{_MERMAN_BIN} is not installed (run `/mermaid install`)")
    width = _effective_text_width(settings)
    with tempfile.TemporaryDirectory() as staging:
        source = Path(staging) / "diagram.mmd"
        source.write_text(diagram.content, encoding="utf-8")
        stdout, _ = await _run_subprocess(
            [
                executable,
                "render",
                str(source),
                "--format",
                "unicode",
                "--ascii-max-width",
                str(width),
                "--ascii-color",
                "plain",
                "--output",
                "-",
            ],
            timeout=settings.timeout_seconds,
            capture_output=True,
        )
    rendered = stdout.decode("utf-8", errors="replace").rstrip()
    if not rendered:
        raise _RenderError("merman-cli produced empty text output")
    return rendered


def _message_diagrams(
    messages: Iterable[PromptMessageExtended],
) -> Iterator[list[MermaidDiagram]]:
    for message in messages:
        if message.role == "assistant":
            diagrams = extract_mermaid_diagrams(message.all_text())
            if diagrams:
                yield diagrams


async def _render_diagrams(
    diagrams: Iterable[MermaidDiagram],
    settings: _Settings,
) -> tuple[list[PluginCommandActionImage], list[str], list[str]]:
    executable = _find_merman()
    images: list[PluginCommandActionImage] = []
    texts: list[str] = []
    errors: list[str] = []
    for diagram in diagrams:
        try:
            if settings.mode == "text":
                rendered = await _render_text(diagram, settings, executable)
                label = diagram.title or detect_diagram_type(diagram.content)
                texts.append(f"{label}\n{rendered}")
            else:
                images.append(await _render_image(diagram, settings, executable))
        except _RenderError as exc:
            errors.append(str(exc))
        except TimeoutError:
            errors.append(f"render timed out after {settings.timeout_seconds:g}s")
        except OSError as exc:
            errors.append(type(exc).__name__)
    return images, texts, errors


async def render_post_turn(
    ctx: PluginPostUserTurnContext,
) -> PluginPostUserTurnOutput | None:
    try:
        settings = _Settings.from_config(ctx.config, overrides=_session_overrides())
    except ValueError as exc:
        _LOG.warning("%s config: %s", _NAME, exc)
        return None
    if not settings.auto_render or not settings.max_diagrams:
        return None
    if settings.mode == "image" and not _POST_TURN_IMAGES:
        return None

    diagrams: list[MermaidDiagram] = []
    for group in _message_diagrams(ctx.turn_messages):
        diagrams.extend(group[: settings.max_diagrams - len(diagrams)])
        if len(diagrams) >= settings.max_diagrams:
            break
    if not diagrams:
        return None

    images, texts, errors = await _render_diagrams(diagrams, settings)
    for error in errors:
        _LOG.warning("%s: %s", _NAME, error)
    if texts:
        return PluginPostUserTurnOutput(display=escape_markup("\n\n".join(texts)))
    return PluginPostUserTurnOutput(images=tuple(images)) if images else None


async def _handle_install(confirm: bool) -> PluginCommandActionResult:
    existing = _find_merman()
    display_cmd, argv, source_desc = _install_plan()
    if not confirm:
        already = (
            f"Note: `{_MERMAN_BIN}` is already installed at `{existing}`.\n\n" if existing else ""
        )
        return PluginCommandActionResult(
            message=(
                f"{already}Warning: `/mermaid install --yes` will run this shell command "
                f"({source_desc}):\n"
                f"  {display_cmd}\n\n"
                "Re-run `/mermaid install --yes` to proceed, or run the command yourself."
            )
        )

    try:
        stdout, _ = await _run_subprocess(argv, timeout=180.0, capture_output=True)
    except _RenderError as exc:
        return PluginCommandActionResult(message=f"Install failed: {exc}")
    except TimeoutError:
        return PluginCommandActionResult(message="Install timed out after 180s.")
    except OSError as exc:
        return PluginCommandActionResult(message=f"Install failed: {exc}")

    installed = _find_merman()
    tail = stdout.decode("utf-8", errors="replace").strip().splitlines()[-3:]
    summary = "\n".join(tail)
    if installed:
        extra = f"\n{summary}" if summary else ""
        return PluginCommandActionResult(
            message=f"Installed `{_MERMAN_BIN}` at `{installed}`.{extra}"
        )
    return PluginCommandActionResult(
        message=(
            f"Installer finished, but `{_MERMAN_BIN}` was not found on PATH yet. "
            "Restart your shell or add its install directory to PATH."
        )
    )


def _format_status(settings: _Settings) -> str:
    binary = _find_merman()
    binary_line = binary if binary else "not installed (run `/mermaid install`)"
    auto = "on" if settings.auto_render and settings.max_diagrams else "off"
    if settings.mode == "image" and not _POST_TURN_IMAGES:
        auto = "unavailable (fast-agent lacks post-turn images)"
    active_overrides = _session_overrides()
    overrides = (
        f"\nSession overrides: {', '.join(f'{k}={v}' for k, v in active_overrides.items())}"
        if active_overrides
        else ""
    )
    return (
        f"merman-cli: {binary_line}\n"
        f"Mode: {settings.mode}\n"
        f"Theme: {settings.theme}; background: {settings.background}\n"
        f"Auto-render: {auto} (max {settings.max_diagrams} per turn)\n"
        f"Timeout: {settings.timeout_seconds:g}s per diagram\n"
        f"Cache: {settings.cache_dir}{overrides}"
    )


_USAGE = (
    "Usage: /mermaid [image|text] [index] | "
    "on | off | mode <image|text> | theme <dark|default|forest|neutral> | "
    "background <color> | status | install [--yes]"
)


async def mermaid_command(ctx: PluginCommandActionContext) -> PluginCommandActionResult:
    try:
        config = ctx.settings.plugins.config.get(_NAME, {}) if ctx.settings is not None else {}
        settings = _Settings.from_config(config, overrides=_session_overrides())
    except ValueError as exc:
        return PluginCommandActionResult(message=f"{_NAME} config: {exc}")

    tokens = ctx.arguments.strip().split()
    lowered = [t.casefold() for t in tokens]

    if lowered == ["status"]:
        return PluginCommandActionResult(message=_format_status(settings))

    if lowered == ["on"]:
        _session_overrides()["auto_render"] = True
        return PluginCommandActionResult(message="Mermaid auto-render enabled for this session.")

    if lowered == ["off"]:
        _session_overrides()["auto_render"] = False
        return PluginCommandActionResult(message="Mermaid auto-render disabled for this session.")

    if lowered and lowered[0] == "mode":
        if len(lowered) != 2 or lowered[1] not in _MODES:
            return PluginCommandActionResult(message="Usage: /mermaid mode <image|text>")
        _session_overrides()["mode"] = lowered[1]
        return PluginCommandActionResult(
            message=f"Mermaid mode set to `{lowered[1]}` for this session."
        )

    if lowered and lowered[0] == "theme":
        if len(lowered) != 2 or lowered[1] not in _THEMES:
            return PluginCommandActionResult(
                message="Usage: /mermaid theme <dark|default|forest|neutral>"
            )
        _session_overrides()["theme"] = lowered[1]
        return PluginCommandActionResult(
            message=f"Mermaid theme set to `{lowered[1]}` for this session."
        )

    if lowered and lowered[0] == "background":
        if len(tokens) != 2 or not tokens[1].strip():
            return PluginCommandActionResult(message="Usage: /mermaid background <color>")
        _session_overrides()["background"] = tokens[1].strip()
        return PluginCommandActionResult(
            message=f"Mermaid background set to `{tokens[1].strip()}` for this session."
        )

    if lowered and lowered[0] == "install":
        if len(lowered) == 1:
            return await _handle_install(confirm=False)
        if len(lowered) == 2 and lowered[1] in ("--yes", "--confirm", "-y"):
            return await _handle_install(confirm=True)
        return PluginCommandActionResult(message="Usage: /mermaid install [--yes]")

    # Render latest matching reply: `/mermaid [image|text] [index]`
    mode_override: str | None = None
    index: int | None = None
    for token in lowered:
        if token in _MODES and mode_override is None:
            mode_override = token
        elif re.fullmatch(r"[0-9]+", token) and index is None:
            index = int(token)
        else:
            return PluginCommandActionResult(message=_USAGE)

    diagrams = next(_message_diagrams(reversed(ctx.message_history)), [])
    if not diagrams:
        return PluginCommandActionResult(message="No Mermaid diagrams in assistant history.")
    if index is not None:
        if not 1 <= index <= len(diagrams):
            return PluginCommandActionResult(
                message=f"Choose a Mermaid diagram index from 1 to {len(diagrams)}."
            )
        diagrams = [diagrams[index - 1]]

    effective = replace(settings, mode=mode_override) if mode_override else settings
    images, texts, errors = await _render_diagrams(diagrams, effective)
    parts: list[str] = []
    if texts:
        parts.append("\n\n".join(texts))
    if errors:
        parts.append("Mermaid rendering failed: " + "; ".join(errors))
    return PluginCommandActionResult(
        message="\n\n".join(parts) if parts else None,
        images=images,
    )


def complete_mermaid(
    ctx: PluginCommandCompletionContext,
) -> list[PluginCommandCompletion]:
    prefix = ctx.current_token.casefold()
    if not ctx.completed_tokens:
        options = [
            ("status", "Show merman-cli status and active settings"),
            ("install", "Preview/install merman-cli"),
            ("on", "Enable auto-render for this session"),
            ("off", "Disable auto-render for this session"),
            ("mode", "Set default output mode (image|text)"),
            ("theme", "Set diagram theme"),
            ("background", "Set PNG background color"),
            ("image", "Render latest reply as PNG"),
            ("text", "Render latest reply as Unicode text"),
        ]
        return [
            PluginCommandCompletion(value=val, detail=detail)
            for val, detail in options
            if val.startswith(prefix)
        ]

    first = ctx.completed_tokens[0].casefold()
    if len(ctx.completed_tokens) == 1:
        if first == "mode":
            return [PluginCommandCompletion(value=m) for m in _MODES if m.startswith(prefix)]
        if first == "theme":
            return [PluginCommandCompletion(value=t) for t in _THEMES if t.startswith(prefix)]
        if first == "install":
            return (
                [PluginCommandCompletion(value="--yes", detail="Run the merman-cli installer")]
                if "--yes".startswith(prefix)
                else []
            )
    return []
