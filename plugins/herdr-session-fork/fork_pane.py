from __future__ import annotations

import asyncio
import json
import os
import shlex
import shutil
import subprocess
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path

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


def _hc_binary() -> str | None:
    return shutil.which("hc")


def _herdr_app_binary() -> str | None:
    return shutil.which("herdr-app")


def _fast_agent_binary() -> str:
    return shutil.which("fast-agent") or "fast-agent"


def _config(ctx: PluginCommandActionContext) -> dict[str, Any]:
    if ctx.settings is None:
        return {}
    config = ctx.settings.plugins.config.get(_PLUGIN_NAME, {})
    return config if isinstance(config, dict) else {}


def _direction(ctx: PluginCommandActionContext) -> str:
    direction = _config(ctx).get("direction", "auto")
    return direction if isinstance(direction, str) and direction in _VALID_DIRECTIONS else "auto"


def _focus_target(ctx: PluginCommandActionContext) -> str:
    target = _config(ctx).get("focus", "original")
    return target if isinstance(target, str) and target in _VALID_FOCUS_TARGETS else "original"


def _startup_timeout_ms(ctx: PluginCommandActionContext) -> int:
    timeout = _config(ctx).get("startup_timeout_ms", 5_000)
    if isinstance(timeout, int) and not isinstance(timeout, bool):
        return min(max(timeout, 0), 30_000)
    return 5_000


def _decode_json_output(
    command: list[str], result: subprocess.CompletedProcess[str]
) -> dict[str, Any]:
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "unknown Herdr error"
        raise RuntimeError(detail)
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Herdr returned invalid JSON for {shlex.join(command)}") from exc
    if not isinstance(payload, dict):
        raise TypeError(f"Herdr returned an unexpected response for {shlex.join(command)}")
    if isinstance(payload.get("error"), str) and payload["error"]:
        raise RuntimeError(payload["error"])
    return payload


async def _run_command(
    binary: str,
    *arguments: str,
    expect_json: bool = True,
) -> dict[str, Any] | str | None:
    command = [binary, *arguments]
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
            detail = result.stderr.strip() or result.stdout.strip() or "unknown Herdr error"
            raise RuntimeError(detail)
        return result.stdout
    return _decode_json_output(command, result)


def _auto_direction(terminal_record: dict[str, Any]) -> str:
    cols = terminal_record.get("cols")
    rows = terminal_record.get("rows")
    if isinstance(cols, int) and isinstance(rows, int) and cols > 0 and rows > 0:
        return "right" if cols >= rows * 2 else "down"
    return "right"


def _split_direction(ctx: PluginCommandActionContext, terminal_record: dict[str, Any]) -> str:
    configured = _direction(ctx)
    if configured != "auto":
        return configured
    return _auto_direction(terminal_record)


def _find_pane_for_terminal(ls_payload: dict[str, Any], terminal_id: str) -> str | None:
    spaces = ls_payload.get("spaces")
    if not isinstance(spaces, list):
        return None
    for space in spaces:
        if not isinstance(space, dict):
            continue
        tabs = space.get("tabs")
        if not isinstance(tabs, list):
            continue
        for tab in tabs:
            if not isinstance(tab, dict):
                continue
            terminals = tab.get("terminals")
            if not isinstance(terminals, list):
                continue
            for entry in terminals:
                if not isinstance(entry, dict):
                    continue
                if entry.get("terminal") == terminal_id:
                    pane = entry.get("pane")
                    if isinstance(pane, str) and pane:
                        return pane
    return None


def _terminal_labels(terminal_record: dict[str, Any]) -> dict[str, str]:
    raw = terminal_record.get("labels")
    if not isinstance(raw, dict):
        return {}
    return {k: v for k, v in raw.items() if isinstance(k, str) and isinstance(v, str) and v}


def _shell_join(arguments: list[str], *, windows: bool | None = None) -> str:
    use_windows_quoting = os.name == "nt" if windows is None else windows
    if use_windows_quoting:
        return subprocess.list2cmdline(arguments)
    return shlex.join(arguments)


def _resume_argv(
    *,
    workspace: Path,
    home: Path,
    session_id: str,
    model: str,
) -> list[str]:
    return [
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


def _session_title(session: Any) -> str | None:
    metadata = session.info.metadata
    if not isinstance(metadata, dict):
        return None
    title = metadata.get("title")
    return title.strip() if isinstance(title, str) and title.strip() else None


def _is_fast_agent_running(record: dict[str, Any]) -> bool:
    status = record.get("status")
    if isinstance(status, dict) and status.get("app") == "fast-agent":
        return True
    foreground = record.get("foreground")
    if isinstance(foreground, dict) and foreground.get("name") == "fast-agent":
        return True
    return False


def _normalize_output(text: str | None) -> str | None:
    if not isinstance(text, str):
        return None
    normalized = " / ".join(line.strip() for line in text.splitlines() if line.strip())
    return normalized[-400:] or None


async def _read_recent_output(hc: str, terminal_id: str) -> str | None:
    try:
        output = await _run_command(hc, "read", terminal_id, "--lines", "20", expect_json=False)
    except (RuntimeError, OSError, subprocess.TimeoutExpired, TypeError):
        return None
    return _normalize_output(output if isinstance(output, str) else None)


async def _confirm_child_startup(
    hc: str,
    *,
    terminal_id: str,
    timeout_ms: int,
) -> str | None:
    if timeout_ms == 0:
        return None
    deadline = asyncio.get_running_loop().time() + (timeout_ms / 1_000)
    while True:
        record = await _run_command(hc, "get", terminal_id, "--json")
        assert isinstance(record, dict)
        if _is_fast_agent_running(record):
            return None
        state = record.get("state")
        if isinstance(state, str) and state != "live":
            exit_code = record.get("exit_code")
            detail = await _read_recent_output(hc, terminal_id)
            suffix = f"; recent output: {detail}" if detail else ""
            return f"fast-agent exited early (state={state}, exit_code={exit_code}){suffix}"
        if asyncio.get_running_loop().time() >= deadline:
            break
        await asyncio.sleep(0.1)

    detail = await _read_recent_output(hc, terminal_id)
    suffix = f"; recent output: {detail}" if detail else ""
    return f"fast-agent startup was not confirmed within {timeout_ms}ms{suffix}"


async def _label_session(hc: str, terminal_id: str, session_id: str) -> None:
    await _run_command(hc, "label", terminal_id, f"session={session_id}", expect_json=False)


async def _rename_pane(herdr_app: str, pane_id: str, name: str) -> None:
    payload = json.dumps({"pane": pane_id, "name": name})
    await _run_command(herdr_app, "do", "pane.rename", payload, expect_json=False)


async def fork_pane(ctx: PluginCommandActionContext) -> PluginCommandActionResult:
    if not ctx.is_tui:
        return PluginCommandActionResult(
            message="/fork-pane is only available in the interactive fast-agent TUI."
        )
    terminal_id = os.environ.get("HC_TERMINAL_ID", "").strip()
    if not terminal_id:
        return PluginCommandActionResult(
            message="/fork-pane requires fast-agent to be running inside Herdr (HC_TERMINAL_ID is not set)."
        )

    hc = _hc_binary()
    if hc is None:
        return PluginCommandActionResult(
            message="Could not locate the `hc` (herdr-core) executable."
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
        return PluginCommandActionResult(message="No active session is available to fork.")
    model = ctx.agent.config.model
    if not model:
        return PluginCommandActionResult(
            message="/fork-pane could not determine the active agent model."
        )

    try:
        current_record = await _run_command(hc, "get", terminal_id, "--json")
        assert isinstance(current_record, dict)
    except (RuntimeError, OSError, subprocess.TimeoutExpired, TypeError) as exc:
        return PluginCommandActionResult(message=f"Could not connect to herdr-core: {exc}")

    herdr_app = _herdr_app_binary()
    current_pane_id: str | None = None
    if herdr_app is not None:
        try:
            ls_payload = await _run_command(herdr_app, "ls")
            if isinstance(ls_payload, dict):
                current_pane_id = _find_pane_for_terminal(ls_payload, terminal_id)
        except (RuntimeError, OSError, subprocess.TimeoutExpired, TypeError):
            current_pane_id = None

    direction = _split_direction(ctx, current_record)
    source_id = source.info.name
    source_title = _session_title(source)
    forked = manager.fork_current_session(title=title)
    if forked is None:
        return PluginCommandActionResult(message="No active session is available to fork.")
    forked_title = _session_title(forked)

    workspace = manager.workspace_dir
    home = manager.base_dir.parent
    argv = _resume_argv(
        workspace=workspace,
        home=home,
        session_id=source_id,
        model=model,
    )
    manual_command = _shell_join(argv)
    warnings: list[str] = []

    try:
        await _label_session(hc, terminal_id, forked.info.name)
    except (RuntimeError, OSError, subprocess.TimeoutExpired, TypeError) as exc:
        warnings.append(f"fork terminal label: {exc}")

    if herdr_app is not None and current_pane_id is not None and forked_title is not None:
        try:
            await _rename_pane(herdr_app, current_pane_id, forked_title)
        except (RuntimeError, OSError, subprocess.TimeoutExpired, TypeError) as exc:
            warnings.append(f"fork pane rename: {exc}")

    new_pane_id: str | None = None
    new_terminal_id: str | None = None
    try:
        if herdr_app is not None and current_pane_id is not None:
            split_args = json.dumps(
                {
                    "direction": direction,
                    "pane": current_pane_id,
                    "argv": argv,
                }
            )
            split = await _run_command(herdr_app, "do", "pane.split", split_args)
            assert isinstance(split, dict)
            raw_pane = split.get("pane")
            raw_terminal = split.get("terminal")
            if not isinstance(raw_pane, str) or not raw_pane:
                raise RuntimeError("herdr-app pane.split did not return a pane ID")
            if not isinstance(raw_terminal, str) or not raw_terminal:
                raise RuntimeError("herdr-app pane.split did not return a terminal ID")
            new_pane_id = raw_pane
            new_terminal_id = raw_terminal

            if _focus_target(ctx) == "fork":
                try:
                    await _run_command(
                        herdr_app,
                        "do",
                        "pane.focus",
                        json.dumps({"pane": current_pane_id}),
                        expect_json=False,
                    )
                except (RuntimeError, OSError, subprocess.TimeoutExpired, TypeError) as exc:
                    warnings.append(f"focus fork pane: {exc}")

            try:
                await _label_session(hc, new_terminal_id, source_id)
            except (RuntimeError, OSError, subprocess.TimeoutExpired, TypeError) as exc:
                warnings.append(f"original terminal label: {exc}")

            if source_title is not None:
                try:
                    await _rename_pane(herdr_app, new_pane_id, source_title)
                except (RuntimeError, OSError, subprocess.TimeoutExpired, TypeError) as exc:
                    warnings.append(f"original pane rename: {exc}")
        else:
            labels = _terminal_labels(current_record)
            run_args = [
                "run",
                "--bg",
                "--no-parent",
                "--keep",
                "--json",
                "--cwd",
                str(ctx.session_cwd or workspace),
                "--label",
                f"session={source_id}",
            ]
            if "herdr.space" in labels:
                run_args.extend(["--label", f"herdr.space={labels['herdr.space']}"])
            if "herdr.tab" in labels:
                run_args.extend(["--label", f"herdr.tab={labels['herdr.tab']}"])
            run_args.append("--")
            run_args.extend(argv)

            launched = await _run_command(hc, *run_args)
            assert isinstance(launched, dict)
            raw_terminal = launched.get("address")
            if not isinstance(raw_terminal, str) or not raw_terminal:
                raise RuntimeError("hc run did not return a terminal address")
            new_terminal_id = raw_terminal

        try:
            startup_warning = await _confirm_child_startup(
                hc,
                terminal_id=new_terminal_id,
                timeout_ms=_startup_timeout_ms(ctx),
            )
            if startup_warning is not None:
                warnings.append(startup_warning)
        except (RuntimeError, OSError, subprocess.TimeoutExpired, TypeError) as exc:
            warnings.append(f"startup confirmation: {exc}")
    except (RuntimeError, OSError, subprocess.TimeoutExpired, TypeError) as exc:
        cleanup_notice = ""
        if herdr_app is not None and new_pane_id is not None:
            try:
                await _run_command(
                    herdr_app,
                    "do",
                    "pane.close",
                    json.dumps({"pane": new_pane_id}),
                    expect_json=False,
                )
            except (RuntimeError, OSError, subprocess.TimeoutExpired, TypeError) as cleanup_exc:
                cleanup_notice = (
                    f"\n\nCould not close created Herdr pane `{new_pane_id}`: {cleanup_exc}"
                )
        elif new_terminal_id is not None:
            try:
                await _run_command(hc, "kill", new_terminal_id, expect_json=False)
            except (RuntimeError, OSError, subprocess.TimeoutExpired, TypeError) as cleanup_exc:
                cleanup_notice = (
                    f"\n\nCould not kill created Herdr terminal `{new_terminal_id}`: {cleanup_exc}"
                )
        return PluginCommandActionResult(
            markdown=(
                f"Forked the current pane into session `{forked.info.name}`, but could not "
                f"open original session `{source_id}` in Herdr: {exc}\n\n"
                f"Resume it manually with:\n\n```sh\n{manual_command}\n```{cleanup_notice}"
            )
        )

    title_notice = f" titled `{forked_title}`" if forked_title is not None else ""
    location = (
        f"Herdr pane `{new_pane_id}` (`{new_terminal_id}`)"
        if new_pane_id is not None
        else f"Herdr terminal `{new_terminal_id}`"
    )
    warning_notice = f" Herdr warnings: {'; '.join(warnings)}" if warnings else ""
    return PluginCommandActionResult(
        markdown=(
            f"Current pane forked into session `{forked.info.name}`{title_notice}. "
            f"Opened original session `{source_id}` in {location}."
            f"{warning_notice}"
        )
    )
