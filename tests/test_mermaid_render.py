from __future__ import annotations

import asyncio
import importlib.util
import json
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
def plugin(monkeypatch):
    monkeypatch.delenv("HC_TERMINAL_ID", raising=False)
    monkeypatch.delenv("HC_TERMINAL_SOCKET", raising=False)
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
        return SimpleNamespace(communicate=AsyncMock(return_value=(b"", b"")), returncode=0)

    monkeypatch.setattr(plugin.asyncio, "create_subprocess_exec", AsyncMock(side_effect=spawn))

    first = run_command(plugin, "", [message(fence(FLOW))], tmp_path)
    second = run_command(plugin, "1", [message(fence(FLOW))], tmp_path)
    assert calls == 1
    assert len(first.images) == 1
    assert first.images[0].source == second.images[0].source
    assert Path(first.images[0].source).read_bytes() == PNG


def test_merman_image_captures_stderr_and_font_fallback(plugin, monkeypatch, tmp_path):
    monkeypatch.setattr(plugin, "_find_merman", lambda: "/tools/merman-cli")
    monkeypatch.setattr(plugin, "_resolve_fallback_font", lambda: "/fonts/Fallback.ttf")

    # 1. system-cmap-bytes triggers automatic retry with --font-file
    seen_args: list[tuple[str, ...]] = []

    async def spawn_font_fallback(executable, subcommand, *args, **kwargs):
        seen_args.append(args)
        flags = dict(zip(args[::2], args[1::2], strict=True))
        if "--font-file" not in flags:
            return SimpleNamespace(
                communicate=AsyncMock(
                    return_value=(
                        b"",
                        b"I/O error: native font limit system-cmap-bytes exceeded: 16818872 > 16777216\n",
                    )
                ),
                returncode=3,
            )
        assert flags["--font-file"] == "/fonts/Fallback.ttf"
        Path(flags["-o"]).write_bytes(PNG)
        return SimpleNamespace(communicate=AsyncMock(return_value=(b"", b"")), returncode=0)

    monkeypatch.setattr(
        plugin.asyncio, "create_subprocess_exec", AsyncMock(side_effect=spawn_font_fallback)
    )
    ok = run_command(plugin, "", [message(fence(FLOW))], tmp_path)
    assert len(seen_args) == 2
    assert len(ok.images) == 1

    # 2. Other mmdc failures surface stderr detail (escaped for Rich)
    async def spawn_parse_error(executable, subcommand, *args, **kwargs):
        return SimpleNamespace(
            communicate=AsyncMock(
                return_value=(b"", b"Diagram parse error: Unterminated node label (missing `]`)\n")
            ),
            returncode=1,
        )

    monkeypatch.setattr(
        plugin.asyncio, "create_subprocess_exec", AsyncMock(side_effect=spawn_parse_error)
    )
    err = run_command(plugin, "", [message(fence("graph LR\n  A[bad"))], tmp_path)
    assert not err.images
    assert "Unterminated node label" in (err.message or "")


def test_herdr_theme_palette_detection(plugin, monkeypatch, tmp_path):
    herdr_cfg = tmp_path / "xdg" / "herdr-app"
    (herdr_cfg / "themes").mkdir(parents=True)
    (herdr_cfg / "settings.json").write_text(
        json.dumps({"theme": "omarchy", "font": "TestMono"}), encoding="utf-8"
    )
    (herdr_cfg / "themes" / "omarchy.json").write_text(
        json.dumps(
            {
                "background": "#060b1e",
                "foreground": "#ffcead",
                "colors": {"accent": "#7d82d9", "raised": "#131a3a", "surface": "#0b1129"},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("HC_TERMINAL_ID", "t1")
    monkeypatch.setattr(plugin, "_find_merman", lambda: "/tools/merman-cli")
    monkeypatch.setattr(plugin, "_resolve_font_via_fc_match", lambda pat: "/fonts/TestMono.ttf")

    captured_cfg: dict[str, object] = {}

    async def spawn(executable, subcommand, *args, **kwargs):
        flags = dict(zip(args[::2], args[1::2], strict=True))
        assert flags["-b"] == "#060b1e"
        assert flags["--font-file"] == "/fonts/TestMono.ttf"
        captured_cfg.update(json.loads(Path(flags["-c"]).read_text(encoding="utf-8")))
        Path(flags["-o"]).write_bytes(PNG)
        return SimpleNamespace(communicate=AsyncMock(return_value=(b"", b"")), returncode=0)

    monkeypatch.setattr(plugin.asyncio, "create_subprocess_exec", AsyncMock(side_effect=spawn))

    res = run_command(plugin, "", [message(fence(FLOW))], tmp_path / "cache")
    assert len(res.images) == 1
    assert captured_cfg["theme"] == "base"
    assert captured_cfg["themeVariables"]["background"] == "#060b1e"
    assert captured_cfg["themeVariables"]["primaryBorderColor"] == "#7d82d9"

    status = run_command(plugin, "status", [], tmp_path / "cache")
    assert "auto -> herdr (omarchy)" in (status.message or "")
    assert "#060b1e (from herdr)" in (status.message or "")


def test_merman_text_render_and_layout_profile_fallback(plugin, monkeypatch, tmp_path):
    monkeypatch.setattr(plugin, "_find_merman", lambda: "/tools/merman-cli")
    attempts = 0

    async def spawn(executable, subcommand, *args, **kwargs):
        nonlocal attempts
        attempts += 1
        assert executable == "/tools/merman-cli"
        assert subcommand == "render"
        assert "--format" in args and "unicode" in args
        if "--ascii-layout-profile" in args:
            return SimpleNamespace(
                communicate=AsyncMock(
                    return_value=(
                        b"",
                        b"invalid ASCII render option `layout_profile`: is not admitted for this diagram family\n",
                    )
                ),
                returncode=1,
            )
        box = "┌──────┐   ┌────┐\n│[bold]│──►│[B] │\n└──────┘   └────┘\n".encode("utf-8")
        return SimpleNamespace(communicate=AsyncMock(return_value=(box, b"")), returncode=0)

    monkeypatch.setattr(plugin.asyncio, "create_subprocess_exec", AsyncMock(side_effect=spawn))

    cmd = run_command(plugin, "text", [message(fence(FLOW))], tmp_path)
    assert attempts == 2
    assert not cmd.images
    assert "┌──────┐" in (cmd.message or "")

    out = asyncio.run(
        plugin.render_post_turn(turn_context([message(fence(FLOW))], tmp_path, mode="text"))
    )
    assert out is not None
    assert not out.images
    assert "\\[bold]" in (out.display or "")


def test_mermaid_test_subcommand_and_usage_escaping(plugin, monkeypatch, tmp_path):
    monkeypatch.setattr(plugin, "_find_merman", lambda: "/tools/merman-cli")

    async def spawn(executable, subcommand, *args, **kwargs):
        if subcommand == "mmdc":
            flags = dict(zip(args[::2], args[1::2], strict=True))
            Path(flags["-o"]).write_bytes(PNG)
            return SimpleNamespace(communicate=AsyncMock(return_value=(b"", b"")), returncode=0)
        return SimpleNamespace(
            communicate=AsyncMock(return_value=(b"[ok]\n", b"")),
            returncode=0,
        )

    monkeypatch.setattr(plugin.asyncio, "create_subprocess_exec", AsyncMock(side_effect=spawn))

    res_img = run_command(plugin, "test", [], tmp_path)
    assert len(res_img.images) == 3

    res_txt = run_command(plugin, "test text", [], tmp_path)
    assert not res_txt.images
    assert "Flowchart (test)" in (res_txt.message or "")
    assert "Sequence (test)" in (res_txt.message or "")
    assert "State (test)" in (res_txt.message or "")

    usage = run_command(plugin, "bogus", [], tmp_path)
    assert "\\[image|text]" in (usage.message or "")
    assert "\\[index]" in (usage.message or "")


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
    assert "Theme: forest; look: classic; background: transparent" in (status.message or "")
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
        return SimpleNamespace(communicate=AsyncMock(return_value=(b"", b"")), returncode=0)

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

    ctx_test = PluginCommandCompletionContext(
        command_name="mermaid",
        arguments="te",
        current_token="te",
        completed_tokens=(),
        agent=SimpleNamespace(message_history=[]),
    )
    assert set(c.value for c in plugin.complete_mermaid(ctx_test)) == {"test", "text"}

    ctx_mode = PluginCommandCompletionContext(
        command_name="mermaid",
        arguments="mode ",
        current_token="",
        completed_tokens=("mode",),
        agent=SimpleNamespace(message_history=[]),
    )
    assert [c.value for c in plugin.complete_mermaid(ctx_mode)] == ["image", "text"]

    ctx_test_mode = PluginCommandCompletionContext(
        command_name="mermaid",
        arguments="test ",
        current_token="",
        completed_tokens=("test",),
        agent=SimpleNamespace(message_history=[]),
    )
    assert [c.value for c in plugin.complete_mermaid(ctx_test_mode)] == ["image", "text"]

    ctx_open = PluginCommandCompletionContext(
        command_name="mermaid",
        arguments="op",
        current_token="op",
        completed_tokens=(),
        agent=SimpleNamespace(message_history=[]),
    )
    assert [c.value for c in plugin.complete_mermaid(ctx_open)] == ["open"]


def test_graphic_render_includes_clickable_links_and_open_subcommand(plugin, monkeypatch, tmp_path):
    monkeypatch.setattr(plugin, "_find_merman", lambda: "/tools/merman-cli")

    async def spawn(executable, subcommand, *args, **kwargs):
        flags = dict(zip(args[::2], args[1::2], strict=True))
        Path(flags["-o"]).write_bytes(PNG)
        return SimpleNamespace(communicate=AsyncMock(return_value=(b"", b"")), returncode=0)

    monkeypatch.setattr(plugin.asyncio, "create_subprocess_exec", AsyncMock(side_effect=spawn))

    # Post-turn graphic render emits clickable file:// link + /mermaid open hint
    out = asyncio.run(
        plugin.render_post_turn(turn_context([message(fence(FLOW, title="Flow"))], tmp_path))
    )
    assert out is not None
    assert len(out.images) == 1
    assert out.display is not None
    assert "[link=file://" in out.display
    assert "/mermaid open" in out.display

    # Internal _last_images must not leak into /mermaid status
    status = run_command(plugin, "status", [], tmp_path)
    assert "_last_images" not in (status.message or "")

    # /mermaid open opens the rendered PNG via _open_file
    opened: list[Path] = []
    monkeypatch.setattr(
        plugin,
        "_open_file",
        lambda path: (opened.append(path), (True, f"Opened `{path.name}`"))[1],
    )
    open_res = run_command(plugin, "open", [message(fence(FLOW, title="Flow"))], tmp_path)
    assert len(opened) == 1
    assert opened[0].is_file()
    assert "Opened" in (open_res.message or "")
    assert "[link=file://" in (open_res.message or "")

    # /mermaid open 2 also works against last /mermaid test images when history has no diagrams
    opened.clear()
    test_res = run_command(plugin, "test", [], tmp_path)
    assert len(test_res.images) == 3
    assert "[link=file://" in (test_res.message or "")
    open_second = run_command(plugin, "open 2", [], tmp_path)
    assert len(opened) == 1
    assert opened[0] == Path(test_res.images[1].source).with_suffix(".html")
    assert opened[0].is_file()
    assert ".png" in opened[0].read_text(encoding="utf-8")
    assert "Opened" in (open_second.message or "")

    # /mermaid look switches look override (default is classic, avoiding neo border gradients)
    look_res = run_command(plugin, "look handdrawn", [], tmp_path)
    assert "handDrawn" in (look_res.message or "")
    status_after = run_command(plugin, "status", [], tmp_path)
    assert "look: handDrawn" in (status_after.message or "")


def test_wsl_file_uri_and_open_command(plugin, monkeypatch, tmp_path):
    png = tmp_path / "my diagram.png"
    png.write_bytes(PNG)

    monkeypatch.setattr(plugin, "_is_wsl", lambda: True)
    monkeypatch.setattr(
        plugin,
        "_wsl_windows_path",
        lambda _p: r"\\wsl.localhost\Ubuntu\home\user\my diagram.png",
    )
    assert plugin._file_uri(png) == "file://wsl.localhost/Ubuntu/home/user/my%20diagram.png"

    monkeypatch.setattr(
        plugin,
        "_wsl_windows_path",
        lambda _p: r"C:\Users\user\my diagram.png",
    )
    assert plugin._file_uri(png) == "file:///C:/Users/user/my%20diagram.png"

    monkeypatch.setattr(plugin.shutil, "which", lambda name: None)
    cmd = plugin._open_path_command(png)
    if Path("/mnt/c/Windows/System32/cmd.exe").is_file():
        assert cmd == [
            "/mnt/c/Windows/System32/cmd.exe",
            "/c",
            "start",
            "",
            r"C:\Users\user\my diagram.png",
        ]
