"""Render Mermaid code fences via merman-cli as terminal PNGs or Unicode text."""

from __future__ import annotations

import asyncio
import hashlib
import html
import json
import logging
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import types
from dataclasses import dataclass, fields, replace
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import quote

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
_THEMES = ("auto", "herdr", "dark", "default", "forest", "neutral")
_LOOKS = ("classic", "neo", "handDrawn")
_LOOK_BY_KEY = {look.casefold(): look for look in _LOOKS}
_MODES = ("image", "text")
_FALLBACK_FONT_CANDIDATES = (
    "/usr/share/fonts/liberation/LiberationSans-Regular.ttf",
    "/usr/share/fonts/TTF/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/noto/NotoSans-Regular.ttf",
    "/System/Library/Fonts/Supplemental/Arial.ttf",
)


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
class _HerdrPalette:
    name: str
    background: str
    foreground: str
    dark_mode: bool
    theme_variables: dict[str, object]
    font_name: str | None = None


@dataclass(frozen=True)
class _Settings:
    mode: str = "image"
    theme: str = "auto"
    look: str = "classic"
    background: str = "auto"
    auto_render: bool = True
    max_diagrams: int = 3
    max_width: int | None = None
    timeout_seconds: float = 15.0
    cache_dir: Path = Path("~/.cache/fast-agent/mermaid")
    font_file: Path | None = None

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
            raise ValueError(f"theme must be one of: {', '.join(_THEMES)}")

        raw_look = merged.get("look", defaults.look)
        if not isinstance(raw_look, str) or raw_look.casefold() not in _LOOK_BY_KEY:
            raise ValueError(f"look must be one of: {', '.join(_LOOKS)}")
        look = _LOOK_BY_KEY[raw_look.casefold()]

        background = merged.get("background", defaults.background)
        if not isinstance(background, str) or not background.strip():
            raise ValueError("background must be a non-empty color string or 'auto'")

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

        raw_font = merged.get("font_file")
        font_file: Path | None = None
        if raw_font is not None:
            if not isinstance(raw_font, str) or not raw_font.strip():
                raise ValueError("font_file must be a non-empty path or null")
            try:
                font_file = Path(raw_font.strip()).expanduser().absolute()
            except RuntimeError as exc:
                raise ValueError("could not expand font_file") from exc

        return cls(
            mode=mode,
            theme=theme,
            look=look,
            background=background.strip(),
            auto_render=auto_render,
            max_diagrams=limit,
            max_width=max_width,
            timeout_seconds=float(timeout),
            cache_dir=cache.absolute(),
            font_file=font_file,
        )


class _RenderError(Exception):
    """A short, user-facing rendering error without diagram source."""


def _in_herdr() -> bool:
    return bool(os.environ.get("HC_TERMINAL_ID") or os.environ.get("HC_TERMINAL_SOCKET"))


def _is_wsl() -> bool:
    if os.environ.get("WSL_DISTRO_NAME") or os.environ.get("WSL_INTEROP"):
        return True
    try:
        return (
            "microsoft"
            in Path("/proc/version").read_text(encoding="utf-8", errors="ignore").lower()
        )
    except OSError:
        return False


def _short_display_path(path: Path) -> str:
    try:
        rel = path.relative_to(Path.home())
        return f"~/{rel.as_posix()}"
    except ValueError:
        return str(path)


def _wsl_windows_path(path: Path) -> str | None:
    wslpath = shutil.which("wslpath")
    if not wslpath:
        return None
    try:
        proc = subprocess.run(
            [wslpath, "-w", str(path)],
            capture_output=True,
            text=True,
            timeout=5.0,
            check=False,
        )
    except OSError:
        return None
    if proc.returncode == 0 and proc.stdout.strip():
        return proc.stdout.strip()
    return None


def _windows_path_to_file_uri(win_path: str) -> str | None:
    normalized = win_path.strip()
    if not normalized:
        return None
    if normalized.startswith("\\\\"):
        parts = normalized[2:].split("\\", 1)
        if len(parts) == 2 and parts[0]:
            host, rest = parts
            posix_rest = rest.replace("\\", "/")
            return f"file://{host}/{quote(posix_rest, safe='/')}"
        return None
    if len(normalized) >= 3 and normalized[1] == ":" and normalized[2] in ("\\", "/"):
        posix_path = normalized.replace("\\", "/")
        return f"file:///{quote(posix_path, safe='/:')}"
    return None


def _file_uri(path: Path) -> str:
    resolved = path.expanduser().resolve()
    if _is_wsl():
        win_path = _wsl_windows_path(resolved)
        if win_path:
            win_uri = _windows_path_to_file_uri(win_path)
            if win_uri:
                return win_uri
    return resolved.as_uri()


def _browser_view_path(png_path: Path) -> Path:
    resolved = png_path.expanduser().resolve()
    html_path = resolved.with_suffix(".html")
    return html_path if html_path.is_file() else resolved


def _write_browser_view(png_path: Path, *, title: str, background: str) -> Path:
    html_path = png_path.with_suffix(".html")
    bg_css = "#1e1e2e" if background.lower() == "transparent" else background
    safe_title = html.escape(title, quote=True)
    safe_src = html.escape(png_path.name, quote=True)
    safe_bg = html.escape(bg_css, quote=True)
    doc = (
        "<!doctype html>\n"
        '<html lang="en">\n'
        "<head>\n"
        '  <meta charset="utf-8">\n'
        f"  <title>{safe_title}</title>\n"
        "  <style>\n"
        f"    html, body {{ margin: 0; min-height: 100%; background: {safe_bg}; }}\n"
        "    body { display: grid; place-items: center; padding: 24px; box-sizing: border-box; }\n"
        "    img { max-width: 100%; height: auto; cursor: zoom-in; display: block; }\n"
        "    img.full { max-width: none; cursor: zoom-out; }\n"
        "  </style>\n"
        "</head>\n"
        "<body>\n"
        f'  <img src="{safe_src}" alt="{safe_title}" '
        "onclick=\"this.classList.toggle('full')\">\n"
        "</body>\n"
        "</html>\n"
    )
    html_path.write_text(doc, encoding="utf-8")
    return html_path


def _format_image_links(images: list[PluginCommandActionImage]) -> str:
    if not images:
        return ""
    lines: list[str] = []
    for idx, image in enumerate(images, start=1):
        view_path = _browser_view_path(Path(image.source))
        uri = _file_uri(view_path)
        label = escape_markup(image.label or f"Diagram {idx}")
        shown_path = escape_markup(_short_display_path(view_path))
        prefix = f"{idx}. " if len(images) > 1 else ""
        lines.append(
            f"[dim]{prefix}{label}: [/dim][link={uri}][cyan]Open in browser[/cyan][/link]"
            f" [dim]({shown_path})[/dim]"
        )
    open_hint = "/mermaid open <n>" if len(images) > 1 else "/mermaid open"
    lines.append(f"[dim]Click to open in browser, or run {open_hint}[/dim]")
    return "\n".join(lines)


def _remember_rendered_images(images: list[PluginCommandActionImage]) -> None:
    if images:
        _session_overrides()["_last_images"] = [
            (image.source, image.label or "") for image in images
        ]


def _last_rendered_images() -> list[tuple[str, str]]:
    raw = _session_overrides().get("_last_images")
    if isinstance(raw, list):
        return [(str(src), str(lbl)) for src, lbl in raw]
    return []


def _open_path_command(path: Path) -> list[str] | None:
    resolved = path.expanduser().resolve()
    if sys.platform == "darwin":
        opener = shutil.which("open")
        return [opener, str(resolved)] if opener else None
    if _is_wsl():
        if wslview := shutil.which("wslview"):
            return [wslview, str(resolved)]
        win_path = _wsl_windows_path(resolved) or str(resolved)
        cmd_exe = shutil.which("cmd.exe") or (
            "/mnt/c/Windows/System32/cmd.exe"
            if Path("/mnt/c/Windows/System32/cmd.exe").is_file()
            else None
        )
        if cmd_exe:
            return [cmd_exe, "/c", "start", "", win_path]
        explorer = shutil.which("explorer.exe") or (
            "/mnt/c/Windows/explorer.exe" if Path("/mnt/c/Windows/explorer.exe").is_file() else None
        )
        if explorer:
            return [explorer, win_path]
    if xdg := shutil.which("xdg-open"):
        return [xdg, str(resolved)]
    return None


def _open_file(path: Path) -> tuple[bool, str]:
    resolved = path.expanduser().resolve()
    if not resolved.is_file():
        return False, f"Diagram file not found: `{resolved}`"
    if sys.platform == "win32":
        startfile = getattr(os, "startfile", None)
        if callable(startfile):
            try:
                startfile(str(resolved))
                return True, f"Opened `{_short_display_path(resolved)}`"
            except OSError as exc:
                return False, f"Could not open `{_short_display_path(resolved)}`: {exc}"
    cmd = _open_path_command(resolved)
    if not cmd:
        return False, (
            f"No system opener found for `{_short_display_path(resolved)}`. "
            f"File is at `{resolved}`."
        )
    try:
        subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError as exc:
        return False, f"Could not open `{_short_display_path(resolved)}`: {exc}"
    return True, f"Opened `{_short_display_path(resolved)}`"


def _herdr_config_dir() -> Path:
    xdg = os.environ.get("XDG_CONFIG_HOME")
    if xdg:
        return Path(xdg) / "herdr-app"
    return Path.home() / ".config" / "herdr-app"


def _hex_luminance(color: str) -> float | None:
    cleaned = color.strip().lstrip("#")
    if len(cleaned) == 3:
        cleaned = "".join(c * 2 for c in cleaned)
    if len(cleaned) < 6 or not re.fullmatch(r"[0-9a-fA-F]{6,8}", cleaned):
        return None
    r = int(cleaned[0:2], 16) / 255.0
    g = int(cleaned[2:4], 16) / 255.0
    b = int(cleaned[4:6], 16) / 255.0
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _pick_color(*candidates: object, default: str) -> str:
    for item in candidates:
        if isinstance(item, str) and item.strip().startswith("#"):
            return item.strip()
    return default


def _load_herdr_palette() -> _HerdrPalette | None:
    """Read the active Herdr theme palette from ~/.config/herdr-app/."""
    config_dir = _herdr_config_dir()
    settings_path = config_dir / "settings.json"
    if not settings_path.is_file():
        return None
    try:
        app_settings = json.loads(settings_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(app_settings, dict):
        return None

    theme_name = app_settings.get("theme")
    font_name = app_settings.get("font")
    if not isinstance(font_name, str) or not font_name.strip():
        font_name = None
    else:
        font_name = font_name.strip()

    if not isinstance(theme_name, str) or not theme_name.strip():
        return None
    theme_name = theme_name.strip()

    theme_path = config_dir / "themes" / f"{theme_name}.json"
    if not theme_path.is_file():
        return None
    try:
        theme_data = json.loads(theme_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(theme_data, dict):
        return None

    colors = theme_data.get("colors")
    if not isinstance(colors, dict):
        colors = {}
    ansi = theme_data.get("ansi")
    if not isinstance(ansi, list):
        ansi = []

    def ansi_at(idx: int) -> str | None:
        if 0 <= idx < len(ansi) and isinstance(ansi[idx], str):
            return ansi[idx]
        return None

    bg = _pick_color(theme_data.get("background"), colors.get("canvas"), default="#1e1e2e")
    fg = _pick_color(theme_data.get("foreground"), colors.get("foreground"), default="#cdd6f4")
    lum = _hex_luminance(bg)
    dark_mode = True if lum is None else lum < 0.5

    accent = _pick_color(colors.get("accent"), ansi_at(4), ansi_at(6), default="#7d82d9")
    surface = _pick_color(
        colors.get("raised"),
        colors.get("surface"),
        theme_data.get("selection"),
        default="#2a2d3e" if dark_mode else "#f1f5f9",
    )
    secondary = _pick_color(
        colors.get("surface"),
        colors.get("selected"),
        theme_data.get("selection"),
        default="#1f2335" if dark_mode else "#e2e8f0",
    )
    tertiary = _pick_color(
        colors.get("selected"),
        colors.get("hover"),
        surface,
        default="#292e42" if dark_mode else "#e5e7eb",
    )
    muted = _pick_color(
        ansi_at(8),
        colors.get("muted"),
        colors.get("border"),
        accent,
        default="#6c7086",
    )
    secondary_border = _pick_color(ansi_at(6), ansi_at(5), accent, default=accent)
    tertiary_border = _pick_color(ansi_at(2), ansi_at(3), accent, default=accent)
    note_border = _pick_color(ansi_at(3), accent, default=accent)

    theme_vars: dict[str, object] = {
        "darkMode": dark_mode,
        "background": bg,
        "primaryColor": surface,
        "primaryTextColor": fg,
        "primaryBorderColor": accent,
        "secondaryColor": secondary,
        "secondaryTextColor": fg,
        "secondaryBorderColor": secondary_border,
        "tertiaryColor": tertiary,
        "tertiaryTextColor": fg,
        "tertiaryBorderColor": tertiary_border,
        "lineColor": accent,
        "textColor": fg,
        "mainBkg": surface,
        "nodeBorder": accent,
        "clusterBkg": secondary,
        "clusterBorder": muted,
        "edgeLabelBackground": bg,
        "actorBkg": surface,
        "actorBorder": accent,
        "actorTextColor": fg,
        "actorLineColor": muted,
        "signalColor": fg,
        "signalTextColor": fg,
        "noteBkgColor": tertiary,
        "noteBorderColor": note_border,
        "noteTextColor": fg,
    }
    return _HerdrPalette(
        name=theme_name,
        background=bg,
        foreground=fg,
        dark_mode=dark_mode,
        theme_variables=theme_vars,
        font_name=font_name,
    )


def _resolve_font_via_fc_match(pattern: str) -> str | None:
    fc_match = shutil.which("fc-match")
    if not fc_match:
        return None
    try:
        result = subprocess.run(
            [fc_match, "-f", "%{file}", pattern],
            capture_output=True,
            text=True,
            timeout=2.0,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    path = result.stdout.strip()
    if path and Path(path).is_file():
        return path
    return None


def _resolve_fallback_font(preferred_family: str | None = None) -> str | None:
    if preferred_family:
        matched = _resolve_font_via_fc_match(preferred_family)
        if matched:
            return matched
    matched = _resolve_font_via_fc_match("sans-serif")
    if matched:
        return matched
    for candidate in _FALLBACK_FONT_CANDIDATES:
        if Path(candidate).is_file():
            return candidate
    return None


def _resolve_render_style(
    settings: _Settings,
) -> tuple[str, str, dict[str, object] | None, str | None, str | None]:
    """Return (theme_label, background, mermaid_config, font_file, herdr_theme_name)."""
    palette: _HerdrPalette | None = None
    if settings.theme == "herdr" or (settings.theme == "auto" and _in_herdr()):
        palette = _load_herdr_palette()

    explicit_font = str(settings.font_file) if settings.font_file is not None else None

    if palette is not None:
        bg = palette.background if settings.background == "auto" else settings.background
        mermaid_cfg: dict[str, object] = {
            "theme": "base",
            "look": settings.look,
            "themeVariables": {**palette.theme_variables, "background": bg},
        }
        font_file = explicit_font
        if font_file is None and palette.font_name:
            font_file = _resolve_font_via_fc_match(palette.font_name)
        return "base", bg, mermaid_cfg, font_file, palette.name

    effective_theme = "dark" if settings.theme in ("auto", "herdr") else settings.theme
    if settings.background == "auto":
        bg = "#1e1e2e" if effective_theme == "dark" else "#ffffff"
    else:
        bg = settings.background
    mermaid_cfg = {"look": settings.look}
    return effective_theme, bg, mermaid_cfg, explicit_font, None


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
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout)
    except (TimeoutError, asyncio.CancelledError):
        try:
            process.kill()
        except ProcessLookupError:
            pass
        await process.wait()
        raise
    if process.returncode:
        detail = (stderr or b"").decode("utf-8", errors="replace").strip().splitlines()
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
    theme_name, background, mermaid_cfg, font_file, _ = _resolve_render_style(settings)
    key = json.dumps(
        [code, theme_name, background, mermaid_cfg, font_file, "merman"],
        sort_keys=True,
    )
    cached = settings.cache_dir / f"{hashlib.sha256(key.encode('utf-8')).hexdigest()[:6]}.png"
    if not _is_png(cached):
        settings.cache_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=settings.cache_dir) as staging:
            source = Path(staging) / "diagram.mmd"
            output = Path(staging) / "diagram.png"
            source.write_text(code, encoding="utf-8")

            base_cmd = [
                executable,
                "mmdc",
                "-i",
                str(source),
                "-o",
                str(output),
                "-b",
                background,
            ]
            if mermaid_cfg is not None:
                cfg_path = Path(staging) / "mermaid-config.json"
                cfg_path.write_text(json.dumps(mermaid_cfg), encoding="utf-8")
                base_cmd.extend(["-c", str(cfg_path)])
                if "theme" not in mermaid_cfg:
                    base_cmd.extend(["-t", theme_name])
            else:
                base_cmd.extend(["-t", theme_name])

            cmd = list(base_cmd)
            if font_file:
                cmd.extend(["--font-file", font_file])

            try:
                await _run_subprocess(cmd, timeout=settings.timeout_seconds)
            except _RenderError as exc:
                if font_file is None and "system-cmap-bytes" in str(exc):
                    fallback_font = _resolve_fallback_font()
                    if fallback_font:
                        await _run_subprocess(
                            [*base_cmd, "--font-file", fallback_font],
                            timeout=settings.timeout_seconds,
                        )
                    else:
                        raise
                else:
                    raise

            if not _is_png(output):
                raise _RenderError("merman-cli did not produce a PNG")
            output.replace(cached)

    label = diagram.title or detect_diagram_type(code)
    if not cached.with_suffix(".html").is_file():
        _write_browser_view(cached, title=label, background=background)

    return PluginCommandActionImage(
        source=str(cached),
        label=label,
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
        base_cmd = [
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
        ]
        try:
            stdout, _ = await _run_subprocess(
                [*base_cmd, "--ascii-layout-profile", "auto"],
                timeout=settings.timeout_seconds,
                capture_output=True,
            )
        except _RenderError as exc:
            if "layout_profile" in str(exc):
                stdout, _ = await _run_subprocess(
                    base_cmd,
                    timeout=settings.timeout_seconds,
                    capture_output=True,
                )
            else:
                raise
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
    if images:
        _remember_rendered_images(images)
        return PluginPostUserTurnOutput(
            display=_format_image_links(images),
            images=tuple(images),
        )
    return None


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
        return PluginCommandActionResult(message=f"Install failed: {escape_markup(str(exc))}")
    except TimeoutError:
        return PluginCommandActionResult(message="Install timed out after 180s.")
    except OSError as exc:
        return PluginCommandActionResult(message=f"Install failed: {escape_markup(str(exc))}")

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

    theme_name, bg, _, font_file, herdr_theme = _resolve_render_style(settings)
    if herdr_theme:
        theme_desc = f"{settings.theme} -> herdr ({herdr_theme})"
    elif settings.theme != theme_name:
        theme_desc = f"{settings.theme} -> {theme_name}"
    else:
        theme_desc = settings.theme

    bg_desc = f"{bg} (from herdr)" if settings.background == "auto" and herdr_theme else bg
    font_desc = font_file if font_file else "system default (auto-fallback on font limit)"
    width_desc = (
        str(settings.max_width)
        if settings.max_width is not None
        else f"auto ({_effective_text_width(settings)})"
    )

    active_overrides = {k: v for k, v in _session_overrides().items() if not k.startswith("_")}
    overrides = (
        f"\nSession overrides: {', '.join(f'{k}={v}' for k, v in active_overrides.items())}"
        if active_overrides
        else ""
    )
    return (
        f"merman-cli: {binary_line}\n"
        f"Mode: {settings.mode} (text width: {width_desc})\n"
        f"Theme: {theme_desc}; look: {settings.look}; background: {bg_desc}\n"
        f"Font: {font_desc}\n"
        f"Auto-render: {auto} (max {settings.max_diagrams} per turn)\n"
        f"Timeout: {settings.timeout_seconds:g}s per diagram\n"
        f"Cache: {settings.cache_dir}{overrides}"
    )


_TEST_DIAGRAMS: tuple[MermaidDiagram, ...] = (
    MermaidDiagram(
        content=(
            "flowchart LR\n"
            "  User([User]) --> CLI[fast-agent]\n"
            "  CLI --> Hook{mermaid-render}\n"
            "  Hook -->|image| PNG[(PNG Cache)]\n"
            "  Hook -->|text| ASCII[Unicode Box]\n"
        ),
        title="Flowchart (test)",
    ),
    MermaidDiagram(
        content=(
            "sequenceDiagram\n"
            "  autonumber\n"
            "  participant U as User\n"
            "  participant A as Agent\n"
            "  participant M as merman-cli\n"
            "  U->>A: Explain flow\n"
            "  A->>M: Render mermaid\n"
            "  Note over M: Pure Rust, local\n"
            "  M-->>U: Inline diagram\n"
        ),
        title="Sequence (test)",
    ),
    MermaidDiagram(
        content=(
            "stateDiagram-v2\n"
            "  [*] --> Idle\n"
            "  Idle --> Rendering: mermaid fence\n"
            "  Rendering --> Cached: success\n"
            "  Cached --> Idle\n"
        ),
        title="State (test)",
    ),
)

_USAGE = (
    "Usage: /mermaid [image|text] [index] | open [index] | "
    "test [image|text] | on | off | mode <image|text> | "
    "theme <auto|herdr|dark|default|forest|neutral> | "
    "look <classic|neo|handDrawn> | "
    "background <color|auto> | status | install [--yes]"
)


async def mermaid_command(ctx: PluginCommandActionContext) -> PluginCommandActionResult:
    try:
        config = ctx.settings.plugins.config.get(_NAME, {}) if ctx.settings is not None else {}
        settings = _Settings.from_config(config, overrides=_session_overrides())
    except ValueError as exc:
        return PluginCommandActionResult(message=f"{_NAME} config: {escape_markup(str(exc))}")

    tokens = ctx.arguments.strip().split()
    lowered = [t.casefold() for t in tokens]

    if lowered == ["status"]:
        return PluginCommandActionResult(message=escape_markup(_format_status(settings)))

    if lowered == ["on"]:
        _session_overrides()["auto_render"] = True
        return PluginCommandActionResult(message="Mermaid auto-render enabled for this session.")

    if lowered == ["off"]:
        _session_overrides()["auto_render"] = False
        return PluginCommandActionResult(message="Mermaid auto-render disabled for this session.")

    if lowered and lowered[0] == "mode":
        if len(lowered) != 2 or lowered[1] not in _MODES:
            return PluginCommandActionResult(
                message=escape_markup("Usage: /mermaid mode <image|text>")
            )
        _session_overrides()["mode"] = lowered[1]
        return PluginCommandActionResult(
            message=f"Mermaid mode set to `{lowered[1]}` for this session."
        )

    if lowered and lowered[0] == "theme":
        if len(lowered) != 2 or lowered[1] not in _THEMES:
            return PluginCommandActionResult(
                message=escape_markup(f"Usage: /mermaid theme <{'|'.join(_THEMES)}>")
            )
        _session_overrides()["theme"] = lowered[1]
        return PluginCommandActionResult(
            message=f"Mermaid theme set to `{lowered[1]}` for this session."
        )

    if lowered and lowered[0] == "look":
        if len(lowered) != 2 or lowered[1] not in _LOOK_BY_KEY:
            return PluginCommandActionResult(
                message=escape_markup(f"Usage: /mermaid look <{'|'.join(_LOOKS)}>")
            )
        chosen_look = _LOOK_BY_KEY[lowered[1]]
        _session_overrides()["look"] = chosen_look
        return PluginCommandActionResult(
            message=f"Mermaid look set to `{chosen_look}` for this session."
        )

    if lowered and lowered[0] == "background":
        if len(tokens) != 2 or not tokens[1].strip():
            return PluginCommandActionResult(
                message=escape_markup("Usage: /mermaid background <color|auto>")
            )
        _session_overrides()["background"] = tokens[1].strip()
        return PluginCommandActionResult(
            message=f"Mermaid background set to `{escape_markup(tokens[1].strip())}` for this session."
        )

    if lowered and lowered[0] == "install":
        if len(lowered) == 1:
            return await _handle_install(confirm=False)
        if len(lowered) == 2 and lowered[1] in ("--yes", "--confirm", "-y"):
            return await _handle_install(confirm=True)
        return PluginCommandActionResult(message=escape_markup("Usage: /mermaid install [--yes]"))

    if lowered and lowered[0] == "open":
        open_index = 1
        if len(lowered) == 2 and re.fullmatch(r"[0-9]+", lowered[1]):
            open_index = int(lowered[1])
        elif len(lowered) != 1:
            return PluginCommandActionResult(message=escape_markup("Usage: /mermaid open [index]"))

        diagrams = next(_message_diagrams(reversed(ctx.message_history)), [])
        target_path: Path | None = None
        if diagrams:
            if not 1 <= open_index <= len(diagrams):
                return PluginCommandActionResult(
                    message=f"Choose a Mermaid diagram index from 1 to {len(diagrams)}."
                )
            images, _, errors = await _render_diagrams(
                [diagrams[open_index - 1]],
                replace(settings, mode="image"),
            )
            if errors or not images:
                detail = "; ".join(errors) if errors else "no PNG produced"
                return PluginCommandActionResult(
                    message="Mermaid rendering failed: " + escape_markup(detail)
                )
            _remember_rendered_images(images)
            target_path = Path(images[0].source)
        else:
            last_images = _last_rendered_images()
            if not last_images:
                return PluginCommandActionResult(
                    message="No rendered Mermaid diagrams available to open."
                )
            if not 1 <= open_index <= len(last_images):
                return PluginCommandActionResult(
                    message=f"Choose a Mermaid diagram index from 1 to {len(last_images)}."
                )
            target_path = Path(last_images[open_index - 1][0])

        view_path = _browser_view_path(target_path)
        _, status_msg = _open_file(view_path)
        uri = _file_uri(view_path)
        link = f"[link={uri}][cyan]{escape_markup(_short_display_path(view_path))}[/cyan][/link]"
        return PluginCommandActionResult(message=f"{escape_markup(status_msg)} ({link})")

    if lowered and lowered[0] == "test":
        test_mode: str | None = None
        if len(lowered) == 2 and lowered[1] in _MODES:
            test_mode = lowered[1]
        elif len(lowered) != 1:
            return PluginCommandActionResult(
                message=escape_markup("Usage: /mermaid test [image|text]")
            )
        effective = replace(settings, mode=test_mode) if test_mode else settings
        images, texts, errors = await _render_diagrams(_TEST_DIAGRAMS, effective)
        parts: list[str] = []
        if images:
            _remember_rendered_images(images)
            parts.append(_format_image_links(images))
        if texts:
            parts.append(escape_markup("\n\n".join(texts)))
        if errors:
            parts.append("Mermaid rendering failed: " + escape_markup("; ".join(errors)))
        return PluginCommandActionResult(
            message="\n\n".join(parts) if parts else None,
            images=images,
        )

    # Render latest matching reply: `/mermaid [image|text] [index]`
    mode_override: str | None = None
    index: int | None = None
    for token in lowered:
        if token in _MODES and mode_override is None:
            mode_override = token
        elif re.fullmatch(r"[0-9]+", token) and index is None:
            index = int(token)
        else:
            return PluginCommandActionResult(message=escape_markup(_USAGE))

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
    if images:
        _remember_rendered_images(images)
        parts.append(_format_image_links(images))
    if texts:
        parts.append(escape_markup("\n\n".join(texts)))
    if errors:
        parts.append("Mermaid rendering failed: " + escape_markup("; ".join(errors)))
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
            ("open", "Open rendered Mermaid diagram in browser"),
            ("test", "Render 3 sample diagrams (flowchart, sequence, state)"),
            ("status", "Show merman-cli status and active settings"),
            ("install", "Preview/install merman-cli"),
            ("on", "Enable auto-render for this session"),
            ("off", "Disable auto-render for this session"),
            ("mode", "Set default output mode (image|text)"),
            ("theme", "Set diagram theme"),
            ("look", "Set diagram look (classic|neo|handDrawn)"),
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
        if first in ("mode", "test"):
            return [PluginCommandCompletion(value=m) for m in _MODES if m.startswith(prefix)]
        if first == "theme":
            return [PluginCommandCompletion(value=t) for t in _THEMES if t.startswith(prefix)]
        if first == "look":
            return [
                PluginCommandCompletion(value=look)
                for look in _LOOKS
                if look.casefold().startswith(prefix)
            ]
        if first == "background":
            return [
                PluginCommandCompletion(value=b)
                for b in ("auto", "transparent", "#1e1e2e", "#ffffff")
                if b.startswith(prefix)
            ]
        if first == "install":
            return (
                [PluginCommandCompletion(value="--yes", detail="Run the merman-cli installer")]
                if "--yes".startswith(prefix)
                else []
            )
    return []
