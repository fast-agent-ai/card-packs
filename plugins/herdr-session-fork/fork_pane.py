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
_VALID_DIRECTIONS = {"right", "down"}


def _title(arguments: str) -> str | None:
    parts = shlex.split(arguments)
    return " ".join(parts) or None


def _herdr_binary() -> str | None:
    configured = os.environ.get("HERDR_BIN_PATH", "").strip()
    return configured or shutil.which("herdr")


def _fast_agent_binary() -> str:
    return shutil.which("fast-agent") or "fast-agent"


def _direction(ctx: PluginCommandActionContext) -> str:
    if ctx.settings is None:
        return "right"
    config = ctx.settings.plugins.config.get(_PLUGIN_NAME, {})
    direction = config.get("direction", "right")
    return (
        direction
        if isinstance(direction, str) and direction in _VALID_DIRECTIONS
        else "right"
    )


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


def _resume_command(
    *,
    workspace: Path,
    home: Path,
    session_id: str,
    model: str,
) -> str:
    return shlex.join(
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
        split = await _run_herdr(
            herdr,
            "pane",
            "split",
            "--current",
            "--direction",
            _direction(ctx),
            "--cwd",
            str(ctx.session_cwd or workspace),
            "--focus",
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
