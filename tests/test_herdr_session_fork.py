from __future__ import annotations

import asyncio
import importlib.util
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

_PLUGIN_SOURCE = (
    Path(__file__).resolve().parents[1]
    / "plugins"
    / "herdr-session-fork"
    / "fork_pane.py"
)


@pytest.fixture
def plugin():
    spec = importlib.util.spec_from_file_location(
        "card_packs_tests.herdr_session_fork",
        _PLUGIN_SOURCE,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(spec.name, None)
        raise
    return module


class _Manager:
    def __init__(self) -> None:
        self.source = SimpleNamespace(
            info=SimpleNamespace(
                name="source-session",
                metadata={"title": "Original branch"},
            )
        )
        self.forked = SimpleNamespace(
            info=SimpleNamespace(name="fork-session", metadata={})
        )
        self.current_session = self.source
        self.workspace_dir = Path("/tmp/work space")
        self.base_dir = Path("/tmp/home dir/sessions")
        self.title: str | None = None

    def fork_current_session(self, title: str | None = None):
        self.title = title
        self.forked.info.metadata = dict(self.source.info.metadata)
        if title is not None:
            self.forked.info.metadata["title"] = title
        self.current_session = self.forked
        return self.forked


def _context(
    manager: _Manager,
    *,
    arguments: str = "",
    model: str | None = "provider.current-model?reasoning=high",
    plugin_config: dict[str, object] | None = None,
) -> SimpleNamespace:
    config = {"direction": "down", "startup_timeout_ms": 0}
    if plugin_config is not None:
        config.update(plugin_config)
    return SimpleNamespace(
        is_tui=True,
        arguments=arguments,
        agent=SimpleNamespace(config=SimpleNamespace(model=model)),
        context=SimpleNamespace(session_manager=manager),
        settings=SimpleNamespace(
            plugins=SimpleNamespace(config={"herdr-session-fork": config})
        ),
        session_cwd=Path("/tmp/shell cwd"),
    )


def test_run_herdr_accepts_empty_success_output(
    plugin, monkeypatch: pytest.MonkeyPatch
) -> None:
    def run(*_arguments, **_kwargs):
        return subprocess.CompletedProcess(
            args=["herdr", "pane", "run"],
            returncode=0,
            stdout="",
            stderr="",
        )

    monkeypatch.setattr(plugin.subprocess, "run", run)

    result = asyncio.run(
        plugin._run_herdr(
            "/usr/bin/herdr",
            "pane",
            "run",
            "w1:p2",
            "fast-agent go",
            expect_json=False,
        )
    )

    assert result is None


def test_auto_direction_uses_current_pane_geometry(plugin) -> None:
    payload = {
        "result": {
            "layout": {
                "panes": [
                    {
                        "pane_id": "w1:p1",
                        "rect": {"width": 160, "height": 40},
                    },
                    {
                        "pane_id": "w1:p2",
                        "rect": {"width": 70, "height": 40},
                    },
                ]
            }
        }
    }

    assert plugin._auto_direction(payload, "w1:p1") == "right"
    assert plugin._auto_direction(payload, "w1:p2") == "down"


def test_windows_resume_command_uses_windows_quoting(plugin, monkeypatch) -> None:
    monkeypatch.setattr(
        plugin, "_fast_agent_binary", lambda: r"C:\Program Files\fast-agent.exe"
    )
    command = plugin._shell_join(
        [
            plugin._fast_agent_binary(),
            "go",
            "--workspace",
            r"C:\work space",
        ],
        windows=True,
    )

    assert (
        command == '"C:\\Program Files\\fast-agent.exe" go --workspace "C:\\work space"'
    )


@pytest.mark.asyncio
async def test_forks_current_pane_and_launches_original_session(
    plugin, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = _Manager()
    calls: list[tuple[str, tuple[str, ...], bool]] = []

    async def run_herdr(binary: str, *arguments: str, expect_json: bool = True):
        calls.append((binary, arguments, expect_json))
        if arguments[:2] == ("pane", "current"):
            return {"result": {"pane": {"pane_id": "w1:p1"}}}
        if arguments[:2] == ("pane", "split"):
            return {"result": {"pane": {"pane_id": "w1:p2"}}}
        return {"result": {}} if expect_json else None

    monkeypatch.setenv("HERDR_ENV", "1")
    monkeypatch.setattr(plugin, "_herdr_binary", lambda: "/usr/bin/herdr")
    monkeypatch.setattr(plugin, "_fast_agent_binary", lambda: "/venv/bin/fast-agent")
    monkeypatch.setattr(plugin, "_run_herdr", run_herdr)

    result = await plugin.fork_pane(_context(manager, arguments='"Alternative branch"'))

    assert manager.title == "Alternative branch"
    assert manager.current_session is manager.forked
    assert calls[0] == (
        "/usr/bin/herdr",
        ("pane", "current", "--current"),
        True,
    )
    current_metadata = calls[1]
    assert current_metadata[0] == "/usr/bin/herdr"
    assert current_metadata[1][:3] == ("pane", "report-metadata", "w1:p1")
    assert ("--title", "Alternative branch") == current_metadata[1][
        current_metadata[1].index("--title") : current_metadata[1].index("--title") + 2
    ]
    assert ("--display-agent", "Alternative branch") == current_metadata[1][
        current_metadata[1].index("--display-agent") : current_metadata[1].index(
            "--display-agent"
        )
        + 2
    ]
    assert "--agent" in current_metadata[1]
    assert "session=fork-session" in current_metadata[1]
    assert current_metadata[2] is False

    assert calls[2] == (
        "/usr/bin/herdr",
        (
            "pane",
            "split",
            "--current",
            "--direction",
            "down",
            "--cwd",
            "/tmp/shell cwd",
            "--focus",
        ),
        True,
    )
    original_metadata = calls[3]
    assert original_metadata[1][:3] == ("pane", "report-metadata", "w1:p2")
    assert "--agent" not in original_metadata[1]
    assert "Original branch" in original_metadata[1]
    assert "session=source-session" in original_metadata[1]
    assert original_metadata[2] is False

    run_call = calls[4]
    assert run_call[1][:3] == ("pane", "run", "w1:p2")
    assert run_call[2] is False
    command = run_call[1][3]
    assert "--resume source-session" in command
    assert "--resume fork-session" not in command
    assert "--workspace '/tmp/work space'" in command
    assert "--home '/tmp/home dir'" in command
    assert "--model 'provider.current-model?reasoning=high'" in command
    assert (
        "Current pane forked into session `fork-session` titled `Alternative branch`"
        in result.markdown
    )


@pytest.mark.asyncio
async def test_inherited_session_title_follows_the_forked_pane(
    plugin, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = _Manager()
    calls: list[tuple[str, tuple[str, ...], bool]] = []

    async def run_herdr(binary: str, *arguments: str, expect_json: bool = True):
        calls.append((binary, arguments, expect_json))
        if arguments[:2] == ("pane", "current"):
            return {"result": {"pane": {"pane_id": "w1:p1"}}}
        if arguments[:2] == ("pane", "split"):
            return {"result": {"pane": {"pane_id": "w1:p2"}}}
        return {"result": {}} if expect_json else None

    monkeypatch.setenv("HERDR_ENV", "1")
    monkeypatch.setattr(plugin, "_herdr_binary", lambda: "/usr/bin/herdr")
    monkeypatch.setattr(plugin, "_fast_agent_binary", lambda: "/venv/bin/fast-agent")
    monkeypatch.setattr(plugin, "_run_herdr", run_herdr)

    result = await plugin.fork_pane(_context(manager))

    assert manager.title is None
    assert manager.forked.info.metadata["title"] == "Original branch"
    current_metadata = calls[1][1]
    assert ("--title", "Original branch") == current_metadata[
        current_metadata.index("--title") : current_metadata.index("--title") + 2
    ]
    assert ("--display-agent", "Original branch") == current_metadata[
        current_metadata.index("--display-agent") : current_metadata.index(
            "--display-agent"
        )
        + 2
    ]
    assert (
        "Current pane forked into session `fork-session` titled `Original branch`"
        in result.markdown
    )


@pytest.mark.asyncio
async def test_supports_ratio_and_keeps_focus_on_fork(
    plugin, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = _Manager()
    calls: list[tuple[str, tuple[str, ...], bool]] = []

    async def run_herdr(binary: str, *arguments: str, expect_json: bool = True):
        calls.append((binary, arguments, expect_json))
        if arguments[:2] == ("pane", "current"):
            return {"result": {"pane": {"pane_id": "w1:p1"}}}
        if arguments[:2] == ("pane", "split"):
            return {"result": {"pane": {"pane_id": "w1:p2"}}}
        return {"result": {}} if expect_json else None

    monkeypatch.setenv("HERDR_ENV", "1")
    monkeypatch.setattr(plugin, "_herdr_binary", lambda: "/usr/bin/herdr")
    monkeypatch.setattr(plugin, "_fast_agent_binary", lambda: "/venv/bin/fast-agent")
    monkeypatch.setattr(plugin, "_run_herdr", run_herdr)

    await plugin.fork_pane(
        _context(
            manager,
            plugin_config={"focus": "fork", "ratio": 0.4},
        )
    )

    split_call = next(call for call in calls if call[1][:2] == ("pane", "split"))
    assert split_call[1][-3:] == ("--ratio", "0.4", "--no-focus")


@pytest.mark.asyncio
async def test_confirms_child_startup_from_herdr_agent_detection(
    plugin, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[str, ...]] = []

    async def run_herdr(_binary: str, *arguments: str, expect_json: bool = True):
        del expect_json
        calls.append(arguments)
        return {"result": {"pane": {"agent": "fast-agent"}}}

    monkeypatch.setattr(plugin, "_run_herdr", run_herdr)

    warning = await plugin._confirm_child_startup(
        "/usr/bin/herdr",
        pane_id="w1:p2",
        timeout_ms=1_000,
    )

    assert warning is None
    assert calls == [("pane", "get", "w1:p2")]


@pytest.mark.asyncio
async def test_startup_confirmation_failure_does_not_close_running_pane(
    plugin, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = _Manager()
    calls: list[tuple[str, tuple[str, ...], bool]] = []

    async def run_herdr(binary: str, *arguments: str, expect_json: bool = True):
        calls.append((binary, arguments, expect_json))
        if arguments[:2] == ("pane", "current"):
            return {"result": {"pane": {"pane_id": "w1:p1"}}}
        if arguments[:2] == ("pane", "split"):
            return {"result": {"pane": {"pane_id": "w1:p2"}}}
        return {"result": {}} if expect_json else None

    async def fail_confirmation(*_arguments, **_kwargs):
        raise RuntimeError("status unavailable")

    monkeypatch.setenv("HERDR_ENV", "1")
    monkeypatch.setattr(plugin, "_herdr_binary", lambda: "/usr/bin/herdr")
    monkeypatch.setattr(plugin, "_fast_agent_binary", lambda: "/venv/bin/fast-agent")
    monkeypatch.setattr(plugin, "_run_herdr", run_herdr)
    monkeypatch.setattr(plugin, "_confirm_child_startup", fail_confirmation)

    result = await plugin.fork_pane(_context(manager))

    assert not any(call[1][:2] == ("pane", "close") for call in calls)
    assert "startup confirmation: status unavailable" in result.markdown


@pytest.mark.asyncio
async def test_preflight_failure_does_not_fork(
    plugin, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = _Manager()

    async def run_herdr(_binary: str, *_arguments: str, expect_json: bool = True):
        del expect_json
        raise RuntimeError("not connected")

    monkeypatch.setenv("HERDR_ENV", "1")
    monkeypatch.setattr(plugin, "_herdr_binary", lambda: "/usr/bin/herdr")
    monkeypatch.setattr(plugin, "_run_herdr", run_herdr)

    result = await plugin.fork_pane(_context(manager))

    assert manager.current_session is manager.source
    assert result.message == "Could not connect to Herdr: not connected"


@pytest.mark.asyncio
async def test_launch_failure_keeps_fork_and_closes_created_pane(
    plugin, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = _Manager()
    calls: list[tuple[str, tuple[str, ...], bool]] = []

    async def run_herdr(binary: str, *arguments: str, expect_json: bool = True):
        calls.append((binary, arguments, expect_json))
        if arguments[:2] == ("pane", "current"):
            return {"result": {"pane": {"pane_id": "w1:p1"}}}
        if arguments[:2] == ("pane", "split"):
            return {"result": {"pane": {"pane_id": "w1:p2"}}}
        if arguments[:2] == ("pane", "run"):
            raise RuntimeError("launch failed")
        return {"result": {}} if expect_json else None

    monkeypatch.setenv("HERDR_ENV", "1")
    monkeypatch.setattr(plugin, "_herdr_binary", lambda: "/usr/bin/herdr")
    monkeypatch.setattr(plugin, "_fast_agent_binary", lambda: "/venv/bin/fast-agent")
    monkeypatch.setattr(plugin, "_run_herdr", run_herdr)

    result = await plugin.fork_pane(_context(manager))

    assert manager.current_session is manager.forked
    assert calls[-1] == (
        "/usr/bin/herdr",
        ("pane", "close", "w1:p2"),
        True,
    )
    assert "could not open original session `source-session`" in result.markdown
    assert "--resume source-session" in result.markdown


@pytest.mark.asyncio
async def test_missing_model_does_not_fork(
    plugin, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = _Manager()
    monkeypatch.setenv("HERDR_ENV", "1")
    monkeypatch.setattr(plugin, "_herdr_binary", lambda: "/usr/bin/herdr")

    result = await plugin.fork_pane(_context(manager, model=None))

    assert manager.current_session is manager.source
    assert result.message == "/fork-pane could not determine the active agent model."


@pytest.mark.asyncio
async def test_requires_herdr_before_accessing_session(
    plugin, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = _Manager()
    monkeypatch.delenv("HERDR_ENV", raising=False)

    result = await plugin.fork_pane(_context(manager))

    assert manager.current_session is manager.source
    assert (
        result.message == "/fork-pane requires fast-agent to be running inside Herdr."
    )
