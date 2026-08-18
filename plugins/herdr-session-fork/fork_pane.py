from __future__ import annotations

import asyncio
import json
import os
import shlex
import shutil
import subprocess
from pathlib import Path
from typing import Any

from fast_agent.command_actions import (
    PluginCommandActionContext,
    PluginCommandActionResult,
)

_PLUGIN_NAME = "herdr-session-fork"
_VALID_DIRECTIONS = {"auto", "right", "down"}
_VALID_FOCUS_TARGETS = {"fork", "original"}


def _title(arguments: str) -> str | None:
    parts = shlex.split(arguments)
    return " ".join(parts) or None


def _herdr_binary() -> str | None:
    configured = os.environ.get("HERDR_BIN_PATH", "").strip()
    return configured or shutil.which("herdr")


def _fast_agent_binary() -> str:
    return shutil.which("fast-agent") or "fast-agent"


def _config(ctx: PluginCommandActionContext) -> dict[str, Any]:
    if ctx.settings is None:
        return {}
    config = ctx.settings.plugins.config.get(_PLUGIN_NAME, {})
    return config if isinstance(config, dict) else {}


def _direction(ctx: PluginCommandActionContext) -> str:
    direction = _config(ctx).get("direction", "auto")
    return (
        direction
        if isinstance(direction, str) and direction in _VALID_DIRECTIONS
        else "auto"
    )


def _focus_target(ctx: PluginCommandActionContext) -> str:
    target = _config(ctx).get("focus", "original")
    return (
        target
        if isinstance(target, str) and target in _VALID_FOCUS_TARGETS
        else "original"
    )


def _split_ratio(ctx: PluginCommandActionContext) -> float | None:
    ratio = _config(ctx).get("ratio")
    if isinstance(ratio, int | float) and not isinstance(ratio, bool):
        value = float(ratio)
        return value if 0.1 <= value <= 0.9 else None
    return None


def _startup_timeout_ms(ctx: PluginCommandActionContext) -> int:
    timeout = _config(ctx).get("startup_timeout_ms", 5_000)
    if isinstance(timeout, int) and not isinstance(timeout, bool):
        return min(max(timeout, 0), 30_000)
    return 5_000


def _decode_response(
    command: list[str], result: subprocess.CompletedProcess[str]
) -> dict[str, Any]:
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "unknown Herdr error"
        raise RuntimeError(detail)
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"Herdr returned invalid JSON for {shlex.join(command)}"
        ) from exc
    if not isinstance(payload, dict):
        raise TypeError(
            f"Herdr returned an unexpected response for {shlex.join(command)}"
        )
    return payload


async def _run_herdr(
    herdr: str,
    *arguments: str,
    expect_json: bool = True,
) -> dict[str, Any] | None:
    command = [herdr, *arguments]
    result = await asyncio.to_thread(
        subprocess.run,
        command,
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    if not expect_json:
        if result.returncode != 0:
            detail = (
                result.stderr.strip() or result.stdout.strip() or "unknown Herdr error"
            )
            raise RuntimeError(detail)
        return None
    return _decode_response(command, result)


def _pane_id(payload: dict[str, Any]) -> str:
    try:
        pane_id = payload["result"]["pane"]["pane_id"]
    except (KeyError, TypeError) as exc:
        raise RuntimeError("Herdr split response did not include a pane ID") from exc
    if not isinstance(pane_id, str) or not pane_id:
        raise RuntimeError("Herdr split response included an invalid pane ID")
    return pane_id


def _auto_direction(payload: dict[str, Any], pane_id: str) -> str:
    try:
        panes = payload["result"]["layout"]["panes"]
    except (KeyError, TypeError) as exc:
        raise RuntimeError("Herdr layout response did not include panes") from exc
    if not isinstance(panes, list):
        raise TypeError("Herdr layout response included invalid panes")
    for pane in panes:
        if not isinstance(pane, dict) or pane.get("pane_id") != pane_id:
            continue
        rect = pane.get("rect")
        if not isinstance(rect, dict):
            break
        width = rect.get("width")
        height = rect.get("height")
        if (
            isinstance(width, int)
            and isinstance(height, int)
            and width > 0
            and height > 0
        ):
            return "right" if width >= height * 2 else "down"
        break
    raise RuntimeError(
        "Herdr layout response did not include the current pane geometry"
    )


async def _split_direction(
    herdr: str,
    ctx: PluginCommandActionContext,
    pane_id: str,
) -> str:
    configured = _direction(ctx)
    if configured != "auto":
        return configured
    layout = await _run_herdr(herdr, "pane", "layout", "--pane", pane_id)
    assert layout is not None
    return _auto_direction(layout, pane_id)


def _shell_join(arguments: list[str], *, windows: bool | None = None) -> str:
    use_windows_quoting = os.name == "nt" if windows is None else windows
    if use_windows_quoting:
        return subprocess.list2cmdline(arguments)
    return shlex.join(arguments)


def _resume_command(
    *,
    workspace: Path,
    home: Path,
    session_id: str,
    model: str,
) -> str:
    return _shell_join(
        [
            _fast_agent_binary(),
            "go",
            "--workspace",
            str(workspace),
            "--home",
            str(home),
            "--model",
            model,
            "--resume",
            session_id,
        ]
    )


def _session_title(session: Any) -> str | None:
    metadata = session.info.metadata
    if not isinstance(metadata, dict):
        return None
    title = metadata.get("title")
    return title.strip() if isinstance(title, str) and title.strip() else None


async def _report_session_presentation(
    herdr: str,
    *,
    pane_id: str,
    session_id: str,
    title: str | None,
    guard_agent: bool,
) -> None:
    arguments = [
        "pane",
        "report-metadata",
        pane_id,
        "--source",
        "herdr:fast-agent:fork-pane",
    ]
    if guard_agent:
        arguments.extend(["--agent", "fast-agent"])
    arguments.extend(["--token", f"session={session_id}"])
    if title is not None:
        arguments.extend(["--title", title, "--display-agent", title])
    else:
        arguments.extend(["--title", session_id])
    await _run_herdr(herdr, *arguments, expect_json=False)


def _pane_agent(payload: dict[str, Any]) -> str | None:
    try:
        agent = payload["result"]["pane"].get("agent")
    except (KeyError, TypeError, AttributeError):
        return None
    return agent if isinstance(agent, str) else None


def _pane_output(payload: dict[str, Any]) -> str | None:
    try:
        output = payload["result"]["output"]
    except (KeyError, TypeError):
        return None
    if not isinstance(output, str):
        return None
    normalized = " / ".join(
        line.strip() for line in output.splitlines() if line.strip()
    )
    return normalized[-400:] or None


async def _confirm_child_startup(
    herdr: str,
    *,
    pane_id: str,
    timeout_ms: int,
) -> str | None:
    if timeout_ms == 0:
        return None
    deadline = asyncio.get_running_loop().time() + (timeout_ms / 1_000)
    while True:
        pane = await _run_herdr(herdr, "pane", "get", pane_id)
        assert pane is not None
        if _pane_agent(pane) == "fast-agent":
            return None
        if asyncio.get_running_loop().time() >= deadline:
            break
        await asyncio.sleep(0.1)

    output = await _run_herdr(
        herdr,
        "pane",
        "read",
        pane_id,
        "--source",
        "recent-unwrapped",
        "--lines",
        "20",
    )
    detail = _pane_output(output) if output is not None else None
    suffix = f"; recent output: {detail}" if detail else ""
    return f"fast-agent startup was not confirmed within {timeout_ms}ms{suffix}"


async def fork_pane(ctx: PluginCommandActionContext) -> PluginCommandActionResult:
    if not ctx.is_tui:
        return PluginCommandActionResult(
            message="/fork-pane is only available in the interactive fast-agent TUI."
        )
    if os.environ.get("HERDR_ENV") != "1":
        return PluginCommandActionResult(
            message="/fork-pane requires fast-agent to be running inside Herdr."
        )

    herdr = _herdr_binary()
    if herdr is None:
        return PluginCommandActionResult(
            message="Could not locate the Herdr executable."
        )

    context = ctx.context
    manager = context.session_manager if context is not None else None
    if manager is None:
        return PluginCommandActionResult(message="Session persistence is unavailable.")

    try:
        title = _title(ctx.arguments)
    except ValueError as exc:
        return PluginCommandActionResult(message=f"Invalid /fork-pane title: {exc}")

    source = manager.current_session
    if source is None:
        return PluginCommandActionResult(
            message="No active session is available to fork."
        )
    model = ctx.agent.config.model
    if not model:
        return PluginCommandActionResult(
            message="/fork-pane could not determine the active agent model."
        )

    try:
        current = await _run_herdr(herdr, "pane", "current", "--current")
        assert current is not None
        current_pane_id = _pane_id(current)
        direction = await _split_direction(herdr, ctx, current_pane_id)
    except (RuntimeError, OSError, subprocess.TimeoutExpired, TypeError) as exc:
        return PluginCommandActionResult(message=f"Could not connect to Herdr: {exc}")

    source_id = source.info.name
    source_title = _session_title(source)
    forked = manager.fork_current_session(title=title)
    if forked is None:
        return PluginCommandActionResult(
            message="No active session is available to fork."
        )
    forked_title = _session_title(forked)

    workspace = manager.workspace_dir
    home = manager.base_dir.parent
    command = _resume_command(
        workspace=workspace,
        home=home,
        session_id=source_id,
        model=model,
    )
    presentation_warnings: list[str] = []
    try:
        await _report_session_presentation(
            herdr,
            pane_id=current_pane_id,
            session_id=forked.info.name,
            title=forked_title,
            guard_agent=True,
        )
    except (RuntimeError, OSError, subprocess.TimeoutExpired, TypeError) as exc:
        presentation_warnings.append(f"fork pane metadata: {exc}")

    pane_id: str | None = None
    try:
        split_arguments = [
            "pane",
            "split",
            "--current",
            "--direction",
            direction,
            "--cwd",
            str(ctx.session_cwd or workspace),
        ]
        ratio = _split_ratio(ctx)
        if ratio is not None:
            split_arguments.extend(["--ratio", str(ratio)])
        split_arguments.append(
            "--focus" if _focus_target(ctx) == "original" else "--no-focus"
        )
        split = await _run_herdr(
            herdr,
            *split_arguments,
        )
        assert split is not None
        pane_id = _pane_id(split)
        try:
            await _report_session_presentation(
                herdr,
                pane_id=pane_id,
                session_id=source_id,
                title=source_title,
                guard_agent=False,
            )
        except (RuntimeError, OSError, subprocess.TimeoutExpired, TypeError) as exc:
            presentation_warnings.append(f"original pane metadata: {exc}")
        await _run_herdr(
            herdr,
            "pane",
            "run",
            pane_id,
            command,
            expect_json=False,
        )
        try:
            startup_warning = await _confirm_child_startup(
                herdr,
                pane_id=pane_id,
                timeout_ms=_startup_timeout_ms(ctx),
            )
            if startup_warning is not None:
                presentation_warnings.append(startup_warning)
        except (RuntimeError, OSError, subprocess.TimeoutExpired, TypeError) as exc:
            presentation_warnings.append(f"startup confirmation: {exc}")
    except (RuntimeError, OSError, subprocess.TimeoutExpired, TypeError) as exc:
        cleanup_notice = ""
        if pane_id is not None:
            try:
                await _run_herdr(herdr, "pane", "close", pane_id)
            except (
                RuntimeError,
                OSError,
                subprocess.TimeoutExpired,
                TypeError,
            ) as cleanup_exc:
                cleanup_notice = (
                    f"\n\nCould not close created Herdr pane `{pane_id}`: {cleanup_exc}"
                )
        return PluginCommandActionResult(
            markdown=(
                f"Forked the current pane into session `{forked.info.name}`, but could not "
                f"open original session `{source_id}` in Herdr: {exc}\n\n"
                f"Resume it manually with:\n\n```sh\n{command}\n```{cleanup_notice}"
            )
        )

    title_notice = f" titled `{forked_title}`" if forked_title is not None else ""
    presentation_notice = (
        f" Herdr metadata warnings: {'; '.join(presentation_warnings)}"
        if presentation_warnings
        else ""
    )
    return PluginCommandActionResult(
        markdown=(
            f"Current pane forked into session `{forked.info.name}`{title_notice}. "
            f"Opened original session `{source_id}` in Herdr pane `{pane_id}`."
            f"{presentation_notice}"
        )
    )
