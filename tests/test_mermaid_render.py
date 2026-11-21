from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from mcp.types import TextContent

from fast_agent.command_actions import (
    PluginCommandActionContext,
    PluginCommandActionImage,
    PluginCommandCompletionContext,
)
from fast_agent.command_actions.loader import load_plugin_command_action_function
from fast_agent.plugins import PluginPostUserTurnContext
from fast_agent.plugins.manifest import load_plugin_manifest
from fast_agent.plugins.post_user_turn import (
    load_plugin_post_user_turn_handlers,
    run_plugin_post_user_turn,
)
from fast_agent.types import PromptMessageExtended

PLUGIN = Path(__file__).resolve().parents[1] / "plugins" / "mermaid-render"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
FLOW = "graph LR\n  A[User] --> B[fast-agent]"


@pytest.fixture
def plugin():
    spec = importlib.util.spec_from_file_location(
        "test_mermaid_render_plugin", PLUGIN / "mermaid_render.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    module._session_overrides().clear()
    yield module
    module._session_overrides().clear()
    sys.modules.pop(spec.name, None)


def fence(code: str, title: str | None = None) -> str:
    header = f"```mermaid title={{{title}}}" if title else "```mermaid"
    return f"{header}\n{code}\n```"


def message(*texts: str, role: str = "assistant") -> PromptMessageExtended:
    return PromptMessageExtended(
        role=role,
        content=[TextContent(type="text", text=t) for t in texts],
    )


def command_context(
    arguments: str, messages: list[PromptMessageExtended], cache_dir: Path, **config: object
) -> PluginCommandActionContext:
    merged = {"cache_dir": str(cache_dir), **config}
    settings = SimpleNamespace(plugins=SimpleNamespace(config={"mermaid-render": merged}))
    return PluginCommandActionContext(
        command_name="mermaid",
        arguments=arguments,
        agent=SimpleNamespace(message_history=messages),
        settings=settings,
    )


def run_command(
    plugin, arguments: str, messages: list[PromptMessageExtended], cache_dir: Path, **config: object
):
    return asyncio.run(
        plugin.mermaid_command(command_context(arguments, messages, cache_dir, **config))
    )


def turn_context(
    messages: list[PromptMessageExtended], cache_dir: Path, **config: object
) -> PluginPostUserTurnContext:
    return PluginPostUserTurnContext(
        plugin_name="mermaid-render",
        agent_name="assistant",
        turn_usage=(),
        session_usage=(),
        config={"cache_dir": str(cache_dir), **config},
        turn_messages=tuple(messages),
    )


def test_manifest_loads_real_handlers():
    manifest = load_plugin_manifest(PLUGIN)
    assert manifest.schema_version == 2
    assert manifest.name == "mermaid-render"
    command = manifest.commands["mermaid"]
    assert command.completer is not None
    assert callable(load_plugin_command_action_function(command.handler))
    assert manifest.post_user_turn is not None
    loaded = load_plugin_post_user_turn_handlers([manifest.post_user_turn])
    assert len(loaded) == 1


def test_merman_image_render_and_cache(plugin, monkeypatch, tmp_path):
    monkeypatch.setattr(plugin, "_find_merman", lambda: "/tools/merman-cli")
    calls = 0

    async def spawn(executable, subcommand, *args, **kwargs):
        nonlocal calls
        calls += 1
        assert executable == "/tools/merman-cli"
        assert subcommand == "mmdc"
        flags = dict(zip(args[::2], args[1::2], strict=True))
        assert Path(flags["-i"]).read_text(encoding="utf-8") == FLOW
        assert flags["-t"] == "dark"
        assert flags["-b"] == "#1e1e2e"
        Path(flags["-o"]).write_bytes(PNG)
        return SimpleNamespace(wait=AsyncMock(return_value=0), returncode=0)

    monkeypatch.setattr(plugin.asyncio, "create_subprocess_exec", AsyncMock(side_effect=spawn))

    first = run_command(plugin, "", [message(fence(FLOW))], tmp_path)
    second = run_command(plugin, "1", [message(fence(FLOW))], tmp_path)
    assert calls == 1
    assert len(first.images) == 1
    assert first.images[0].source == second.images[0].source
    assert Path(first.images[0].source).read_bytes() == PNG


def test_merman_text_render_and_post_turn_escaping(plugin, monkeypatch, tmp_path):
    monkeypatch.setattr(plugin, "_find_merman", lambda: "/tools/merman-cli")

    async def spawn(executable, subcommand, *args, **kwargs):
        assert executable == "/tools/merman-cli"
        assert subcommand == "render"
        assert "--format" in args and "unicode" in args
        box = "┌──────┐   ┌────┐\n│[bold]│──►│[B] │\n└──────┘   └────┘\n".encode("utf-8")
        return SimpleNamespace(communicate=AsyncMock(return_value=(box, b"")), returncode=0)

    monkeypatch.setattr(plugin.asyncio, "create_subprocess_exec", AsyncMock(side_effect=spawn))

    cmd = run_command(plugin, "text", [message(fence(FLOW))], tmp_path)
    assert not cmd.images
    assert "┌──────┐" in (cmd.message or "")

    out = asyncio.run(
        plugin.render_post_turn(turn_context([message(fence(FLOW))], tmp_path, mode="text"))
    )
    assert out is not None
    assert not out.images
    assert "\\[bold]" in (out.display or "")


def test_session_config_commands_and_status(plugin, monkeypatch, tmp_path):
    monkeypatch.setattr(plugin, "_find_merman", lambda: "/usr/local/bin/merman-cli")

    r_off = run_command(plugin, "off", [], tmp_path)
    assert "disabled" in (r_off.message or "")
    assert (
        asyncio.run(plugin.render_post_turn(turn_context([message(fence(FLOW))], tmp_path))) is None
    )

    assert "enabled" in (run_command(plugin, "on", [], tmp_path).message or "")
    assert "text" in (run_command(plugin, "mode text", [], tmp_path).message or "")
    assert "forest" in (run_command(plugin, "theme forest", [], tmp_path).message or "")
    assert "transparent" in (
        run_command(plugin, "background transparent", [], tmp_path).message or ""
    )

    status = run_command(plugin, "status", [], tmp_path)
    assert "Mode: text" in (status.message or "")
    assert "Theme: forest; background: transparent" in (status.message or "")
    assert "Session overrides:" in (status.message or "")


def test_install_warns_before_running_and_executes_with_yes(plugin, monkeypatch, tmp_path):
    monkeypatch.setattr(plugin, "_find_merman", lambda: None)
    monkeypatch.setattr(
        plugin,
        "_install_plan",
        lambda: ("brew install merman-cli", ["brew", "install", "merman-cli"], "Homebrew formula"),
    )
    spawn = AsyncMock(
        return_value=SimpleNamespace(
            communicate=AsyncMock(return_value=(b"installed ok\n", b"")),
            returncode=0,
        )
    )
    monkeypatch.setattr(plugin.asyncio, "create_subprocess_exec", spawn)

    # Without --yes: warns and does NOT run subprocess
    preview = run_command(plugin, "install", [], tmp_path)
    assert "Warning:" in (preview.message or "")
    assert "brew install merman-cli" in (preview.message or "")
    assert "--yes" in (preview.message or "")
    spawn.assert_not_called()

    # With --yes: runs installer
    monkeypatch.setattr(plugin, "_find_merman", lambda: "/opt/homebrew/bin/merman-cli")
    confirmed = run_command(plugin, "install --yes", [], tmp_path)
    spawn.assert_called_once()
    assert "Installed `merman-cli`" in (confirmed.message or "")


def test_post_turn_images_and_cross_loader_overrides(plugin, monkeypatch, tmp_path):
    import shutil

    monkeypatch.setattr(
        shutil, "which", lambda name: "/tools/merman-cli" if name == "merman-cli" else None
    )

    async def spawn(executable, subcommand, *args, **kwargs):
        flags = dict(zip(args[::2], args[1::2], strict=True))
        Path(flags["-o"]).write_bytes(PNG)
        return SimpleNamespace(wait=AsyncMock(return_value=0), returncode=0)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", AsyncMock(side_effect=spawn))

    manifest = load_plugin_manifest(PLUGIN)
    assert manifest.post_user_turn is not None
    cmd_fn = load_plugin_command_action_function(manifest.commands["mermaid"].handler)
    handlers = load_plugin_post_user_turn_handlers([manifest.post_user_turn])
    captured: list[list[PluginCommandActionImage]] = []

    asyncio.run(
        run_plugin_post_user_turn(
            handlers,
            agent_name="assistant",
            turn_usage=(),
            session_usage=(),
            config={"mermaid-render": {"cache_dir": str(tmp_path)}},
            display=lambda _: None,
            display_images=lambda imgs: captured.append(list(imgs)),
            turn_messages=(message(fence(FLOW, title="Flow")),),
        )
    )
    assert len(captured) == 1
    assert captured[0][0].label == "Flow"

    # Toggle off via the manifest-loaded command handler and verify the separate hook sees it
    asyncio.run(cmd_fn(command_context("off", [], tmp_path)))
    captured.clear()
    asyncio.run(
        run_plugin_post_user_turn(
            handlers,
            agent_name="assistant",
            turn_usage=(),
            session_usage=(),
            config={"mermaid-render": {"cache_dir": str(tmp_path)}},
            display=lambda _: None,
            display_images=lambda imgs: captured.append(list(imgs)),
            turn_messages=(message(fence(FLOW, title="Flow")),),
        )
    )
    assert captured == []


def test_completer(plugin):
    ctx = PluginCommandCompletionContext(
        command_name="mermaid",
        arguments="in",
        current_token="in",
        completed_tokens=(),
        agent=SimpleNamespace(message_history=[]),
    )
    assert [c.value for c in plugin.complete_mermaid(ctx)] == ["install"]

    ctx_mode = PluginCommandCompletionContext(
        command_name="mermaid",
        arguments="mode ",
        current_token="",
        completed_tokens=("mode",),
        agent=SimpleNamespace(message_history=[]),
    )
    assert [c.value for c in plugin.complete_mermaid(ctx_mode)] == ["image", "text"]
