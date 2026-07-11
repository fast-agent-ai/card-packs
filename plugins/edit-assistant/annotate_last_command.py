import asyncio
import os
import shlex
import subprocess
from pathlib import Path

from fast_agent.command_actions import (
    PluginCommandActionContext,
    PluginCommandActionResult,
)


async def annotatelast(ctx: PluginCommandActionContext) -> PluginCommandActionResult:
    """Annotate the last assistant message and prefill the resulting review."""
    for message in reversed(ctx.message_history):
        if message.role != "assistant" or (original := message.last_text()) is None:
            continue

        try:
            annotated = await asyncio.to_thread(
                _annotate_text,
                original,
                cwd=ctx.session_cwd,
            )
        except FileNotFoundError:
            return PluginCommandActionResult(
                message=(
                    "AnnotUI was not found. Install it from "
                    "https://github.com/dutifuldev/annotui or set $ANNOTUI."
                )
            )
        except subprocess.CalledProcessError as exc:
            detail = exc.stderr.strip() if exc.stderr else f"exit status {exc.returncode}"
            return PluginCommandActionResult(message=f"AnnotUI failed: {detail}")
        except (OSError, UnicodeError, ValueError) as exc:
            return PluginCommandActionResult(message=f"AnnotUI failed: {exc}")

        if not annotated:
            return PluginCommandActionResult(message="No annotations added.")

        return PluginCommandActionResult(
            message="Annotated last assistant message; review before sending.",
            buffer_prefill=annotated,
        )

    return PluginCommandActionResult(message="No assistant text found.")


def _annotate_text(initial_text: str, *, cwd: Path | None = None) -> str:
    result = subprocess.run(
        [*_annotui_args(), "--source-name", "last assistant message.md"],
        input=initial_text,
        text=True,
        capture_output=True,
        cwd=cwd,
        check=False,
    )
    result.check_returncode()
    return result.stdout.strip()


def _annotui_args() -> list[str]:
    command = os.environ.get("ANNOTUI", "annotui")
    args = shlex.split(command, posix=os.name != "nt")
    if not args:
        raise ValueError("AnnotUI command is empty")
    return args
