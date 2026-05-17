"""Agent Finder plugin command example."""

from __future__ import annotations

import asyncio
import json
import re
import shutil
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from prompt_toolkit.application import Application
from prompt_toolkit.application.current import get_app_or_none
from prompt_toolkit.data_structures import Point
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import HSplit, Layout, Window
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.layout.dimension import Dimension
from prompt_toolkit.layout.margins import ScrollbarMargin
from prompt_toolkit.widgets import Frame

from fast_agent.command_actions import PluginCommandActionContext, PluginCommandActionResult
from fast_agent.config import MCPServerSettings
from fast_agent.ui.picker_theme import build_picker_style
from fast_agent.utils.async_utils import suppress_known_runtime_warnings

AGENT_FINDER_URL = "https://evalstate-hf-agentfinder.hf.space/search"
AI_SKILL_MEDIA_TYPE = "application/ai-skill"
MCP_SERVER_MEDIA_TYPE = "application/mcp-server+json"


@dataclass(frozen=True, slots=True)
class FinderResult:
    index: int
    identifier: str
    display_name: str
    media_type: str
    description: str
    score: float
    url: str | None
    data: dict[str, Any] | None

    @property
    def kind(self) -> str:
        if self.media_type == MCP_SERVER_MEDIA_TYPE:
            return "mcp"
        if self.media_type == AI_SKILL_MEDIA_TYPE:
            return "skill"
        return self.media_type.rsplit("/", 1)[-1][:12]


async def find(ctx: PluginCommandActionContext) -> PluginCommandActionResult:
    """Search Agent Finder and interactively apply one selected result."""
    query = ctx.arguments.strip()
    if not query:
        return PluginCommandActionResult(message="Usage: /find <thing you need>")

    try:
        results = await _search_agent_finder(query)
    except Exception as exc:  # noqa: BLE001
        return PluginCommandActionResult(message=f"Agent Finder search failed: {exc}")

    if not results:
        return PluginCommandActionResult(message=f"No Agent Finder results for: {query}")

    if not ctx.is_tui:
        return PluginCommandActionResult(markdown=_render_results_markdown(query, results))

    try:
        selected = await _select_result(query, results)
    except KeyboardInterrupt:
        return PluginCommandActionResult()

    if selected is None:
        return PluginCommandActionResult()

    if selected.media_type == MCP_SERVER_MEDIA_TYPE:
        return await _attach_mcp_result(ctx, selected)

    if selected.media_type == AI_SKILL_MEDIA_TYPE:
        return await _prefill_skill_result(query, selected)

    return PluginCommandActionResult(
        message=f"Selected result has unsupported media type: {selected.media_type}"
    )


async def _search_agent_finder(query: str) -> list[FinderResult]:
    payload = {
        "query": {
            "text": query,
            "federation": "none",
        },
        "pageSize": 10,
    }
    body = await asyncio.to_thread(_post_json, AGENT_FINDER_URL, payload)
    raw_results = body.get("results", [])
    if not isinstance(raw_results, list):
        return []

    results: list[FinderResult] = []
    for index, item in enumerate(raw_results, start=1):
        if not isinstance(item, dict):
            continue
        media_type = _str(item.get("mediaType"))
        if media_type not in {AI_SKILL_MEDIA_TYPE, MCP_SERVER_MEDIA_TYPE}:
            continue
        results.append(
            FinderResult(
                index=index,
                identifier=_str(item.get("identifier")),
                display_name=_str(item.get("displayName")) or _str(item.get("identifier")),
                media_type=media_type,
                description=_str(item.get("description")),
                score=_float(item.get("score")),
                url=_optional_str(item.get("url")),
                data=item.get("data") if isinstance(item.get("data"), dict) else None,
            )
        )
    return results


def _post_json(url: str, payload: dict[str, Any]) -> dict[str, Any]:
    data = json.dumps(payload).encode("utf-8")
    request = Request(
        url,
        data=data,
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "fast-agent-agentfinder-plugin/0.1",
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=30) as response:  # noqa: S310 - user-invoked registry URL
            raw = response.read().decode("utf-8")
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"HTTP {exc.code}: {detail or exc.reason}") from exc
    except URLError as exc:
        raise RuntimeError(str(exc.reason)) from exc

    parsed = json.loads(raw)
    if not isinstance(parsed, dict):
        raise RuntimeError("Agent Finder returned a non-object JSON response.")
    return parsed


async def _select_result(query: str, results: list[FinderResult]) -> FinderResult | None:
    picker = _FinderPicker(query=query, results=results)
    return await picker.run_async()


def _row_label(result: FinderResult) -> str:
    score = f"{result.score:5.1f}"
    kind = result.kind[:5].ljust(5)
    name = _truncate(result.display_name, 32).ljust(32)
    description = _truncate(result.description, 82)
    return f"{score}  {kind}  {name}  {description}"


class _FinderPicker:
    VISIBLE_ROWS = 10
    DETAILS_ROWS = 5

    def __init__(self, *, query: str, results: list[FinderResult]) -> None:
        self.query = query
        self.results = results
        self.index = 0

        self.selection_control = FormattedTextControl(
            self._render_results,
            focusable=True,
            show_cursor=False,
            get_cursor_position=self._cursor_position,
        )
        self.details_control = FormattedTextControl(self._render_details)

        selection_window = Window(
            self.selection_control,
            wrap_lines=False,
            height=Dimension.exact(min(self.VISIBLE_ROWS, max(1, len(results)))),
            dont_extend_height=True,
            ignore_content_width=True,
            always_hide_cursor=True,
            right_margins=[ScrollbarMargin(display_arrows=False)],
        )
        details_window = Window(
            self.details_control,
            height=Dimension.exact(self.DETAILS_ROWS),
            dont_extend_height=True,
        )
        body = HSplit(
            [
                Frame(selection_window, title=f"Agent Finder: {query}"),
                details_window,
            ]
        )
        self.app: Application[FinderResult | None] = Application(
            layout=Layout(body, focused_element=selection_window),
            key_bindings=self._create_key_bindings(),
            style=build_picker_style(),
            full_screen=False,
            mouse_support=False,
            erase_when_done=True,
        )

    @property
    def selected(self) -> FinderResult:
        self.index = max(0, min(self.index, len(self.results) - 1))
        return self.results[self.index]

    def _cursor_position(self) -> Point | None:
        if not self.results:
            return None
        return Point(x=0, y=self.index)

    def _terminal_cols(self) -> int:
        app = get_app_or_none()
        if app is not None:
            try:
                return max(1, app.output.get_size().columns)
            except Exception:
                pass
        return max(1, shutil.get_terminal_size((100, 20)).columns)

    def _render_results(self) -> list[tuple[str, str]]:
        width = max(60, self._terminal_cols() - 4)
        name_width = max(18, min(32, width // 3))
        description_width = max(20, width - name_width - 18)
        fragments: list[tuple[str, str]] = [
            ("class:muted", f"  {'score':>5}  {'type':<5}  {'name':<{name_width}}  description\n")
        ]

        for index, result in enumerate(self.results):
            selected = index == self.index
            cursor = "❯ " if selected else "  "
            style = "class:selected" if selected else ""
            fragments.append(
                (
                    style,
                    f"{cursor}{result.score:5.1f}  "
                    f"{result.kind[:5]:<5}  "
                    f"{_truncate(result.display_name, name_width):<{name_width}}  "
                    f"{_truncate(result.description, description_width)}\n",
                )
            )
        return fragments

    def _render_details(self) -> list[tuple[str, str]]:
        result = self.selected
        action = (
            "Enter: connect MCP server"
            if result.media_type == MCP_SERVER_MEDIA_TYPE
            else "Enter: insert skill into prompt"
            if result.media_type == AI_SKILL_MEDIA_TYPE
            else "Enter: select"
        )
        location = result.url or _str((result.data or {}).get("url")) or result.identifier
        return [
            ("class:focus", f"{result.display_name} · {result.kind} · score {result.score:.1f}\n"),
            ("", f"{_truncate(result.description, self._terminal_cols() - 2)}\n"),
            ("class:muted", f"{_truncate(location, self._terminal_cols() - 2)}\n"),
            ("class:muted", f"Keys: ↑/↓ move · {action} · q/Esc/Ctrl-C cancel"),
        ]

    def _move(self, delta: int) -> None:
        if not self.results:
            return
        self.index = (self.index + delta) % len(self.results)

    def _create_key_bindings(self) -> KeyBindings:
        kb = KeyBindings()

        @kb.add("up")
        def _up(event) -> None:
            self._move(-1)
            event.app.invalidate()

        @kb.add("down")
        def _down(event) -> None:
            self._move(1)
            event.app.invalidate()

        @kb.add("enter")
        def _accept(event) -> None:
            event.app.exit(result=self.selected)

        @kb.add("q")
        @kb.add("escape")
        @kb.add("c-c")
        def _quit(event) -> None:
            event.app.exit(result=None)

        return kb

    async def run_async(self) -> FinderResult | None:
        with suppress_known_runtime_warnings():
            return await self.app.run_async()


async def _attach_mcp_result(
    ctx: PluginCommandActionContext,
    result: FinderResult,
) -> PluginCommandActionResult:
    if ctx.runtime is None:
        return PluginCommandActionResult(message="Runtime MCP capabilities are not available.")

    if not result.data:
        return PluginCommandActionResult(message="Selected MCP result did not include server data.")

    server_name = _server_name(result)
    server_config = _mcp_server_settings(result, server_name)
    await ctx.runtime.attach_mcp_server(
        server_name=server_name,
        server_config=server_config,
    )
    return PluginCommandActionResult(
        message=f"Connected MCP server: {server_name}\n\n{result.description}"
    )


def _mcp_server_settings(result: FinderResult, server_name: str) -> MCPServerSettings:
    data = dict(result.data or {})
    data.setdefault("name", server_name)
    data.setdefault("description", result.description or result.display_name)
    return MCPServerSettings.model_validate(data)


def _server_name(result: FinderResult) -> str:
    data_name = _str((result.data or {}).get("name"))
    if data_name:
        return _slug(data_name)
    return _slug(result.display_name or result.identifier or "agentfinder-mcp")


async def _prefill_skill_result(query: str, result: FinderResult) -> PluginCommandActionResult:
    if not result.url:
        return PluginCommandActionResult(message="Selected skill did not include a download URL.")

    try:
        skill_markdown = await asyncio.to_thread(_get_text, result.url)
    except Exception as exc:  # noqa: BLE001
        return PluginCommandActionResult(message=f"Failed to download selected skill: {exc}")

    prefill = (
        "Use this discovered skill to help with the task below.\n\n"
        f"Task: {query}\n\n"
        "Discovered skill:\n\n"
        f"{skill_markdown.strip()}\n"
    )
    return PluginCommandActionResult(
        message=f"Downloaded skill: {result.display_name}",
        buffer_prefill=prefill,
    )


def _get_text(url: str) -> str:
    request = Request(url, headers={"User-Agent": "fast-agent-agentfinder-plugin/0.1"})
    try:
        with urlopen(request, timeout=30) as response:  # noqa: S310 - Agent Finder result URL
            return response.read().decode("utf-8")
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"HTTP {exc.code}: {detail or exc.reason}") from exc
    except URLError as exc:
        raise RuntimeError(str(exc.reason)) from exc


def _render_results_markdown(query: str, results: list[FinderResult]) -> str:
    lines = [f"# Agent Finder results for `{query}`", "", "| score | type | result |", "| ---: | --- | --- |"]
    for result in results:
        description = result.description.replace("|", "\\|")
        name = result.display_name.replace("|", "\\|")
        lines.append(f"| {result.score:.1f} | {result.kind} | **{name}** — {description} |")
    lines.append("")
    lines.append("Interactive selection is only available from the terminal UI.")
    return "\n".join(lines)


def _str(value: object) -> str:
    return value if isinstance(value, str) else ""


def _optional_str(value: object) -> str | None:
    text = _str(value).strip()
    return text or None


def _float(value: object) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    return 0.0


def _slug(value: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9_.-]+", "-", value.strip()).strip("-")
    return slug or "agentfinder-mcp"


def _truncate(value: str, max_len: int) -> str:
    text = " ".join(value.split())
    if len(text) <= max_len:
        return text
    return text[: max_len - 1] + "…"
