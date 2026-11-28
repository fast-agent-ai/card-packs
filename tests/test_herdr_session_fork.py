from __future__ import annotations

import asyncio
import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

_PLUGIN_SOURCE = (
    Path(__file__).resolve().parents[1] / "plugins" / "herdr-session-fork" / "fork_pane.py"
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
        self.forked = SimpleNamespace(info=SimpleNamespace(name="fork-session", metadata={}))
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
    is_tui: bool = True,
) -> SimpleNamespace:
    config: dict[str, object] = {"direction": "down", "startup_timeout_ms": 0}
    if plugin_config is not None:
        config.update(plugin_config)
    return SimpleNamespace(
        is_tui=is_tui,
        arguments=arguments,
        agent=SimpleNamespace(config=SimpleNamespace(model=model)),
        context=SimpleNamespace(session_manager=manager),
        settings=SimpleNamespace(plugins=SimpleNamespace(config={"herdr-session-fork": config})),
        session_cwd=Path("/tmp/shell cwd"),
    )


def test_run_command_accepts_empty_success_output(plugin, monkeypatch: pytest.MonkeyPatch) -> None:
    def run(*_arguments, **_kwargs):
        return subprocess.CompletedProcess(
            args=["hc", "label"],
            returncode=0,
            stdout="",
            stderr="",
        )

    monkeypatch.setattr(plugin.subprocess, "run", run)

    result = asyncio.run(
        plugin._run_command(
            "/usr/bin/hc",
            "label",
            "t14q",
            "session=abc",
            expect_json=False,
        )
    )

    assert result == ""


def test_auto_direction_uses_terminal_cols_and_rows(plugin) -> None:
    assert plugin._auto_direction({"cols": 176, "rows": 61}) == "right"
    assert plugin._auto_direction({"cols": 80, "rows": 60}) == "down"
    assert plugin._auto_direction({}) == "right"


def test_windows_resume_command_uses_windows_quoting(plugin, monkeypatch) -> None:
    monkeypatch.setattr(plugin, "_fast_agent_binary", lambda: r"C:\Program Files\fast-agent.exe")
    command = plugin._shell_join(
        [
            plugin._fast_agent_binary(),
            "go",
            "--workspace",
            r"C:\work space",
        ],
        windows=True,
    )

    assert command == '"C:\\Program Files\\fast-agent.exe" go --workspace "C:\\work space"'


def test_requires_hc_terminal_id(plugin, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HC_TERMINAL_ID", raising=False)
    result = asyncio.run(plugin.fork_pane(_context(_Manager())))
    assert "HC_TERMINAL_ID" in (result.message or "")


def test_local_herdr_app_splits_and_focuses_fork_when_configured(
    plugin, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = _Manager()
    calls: list[tuple[str, tuple[str, ...], bool]] = []

    async def run_command(binary: str, *arguments: str, expect_json: bool = True):
        calls.append((binary, arguments, expect_json))
        if binary == "/usr/bin/hc" and arguments[:2] == ("get", "t14q"):
            return {
                "address": "t14q",
                "cols": 180,
                "rows": 50,
                "labels": {"herdr.space": "sp1", "herdr.tab": "tb1"},
                "state": "live",
            }
        if binary == "/usr/bin/herdr-app" and arguments == ("ls",):
            return {
                "spaces": [
                    {
                        "id": "sp1",
                        "tabs": [
                            {
                                "id": "tb1",
                                "terminals": [{"terminal": "t14q", "pane": "p_orig"}],
                            }
                        ],
                    }
                ]
            }
        if binary == "/usr/bin/herdr-app" and arguments[:2] == ("do", "pane.split"):
            return {"pane": "p_new", "terminal": "t99"}
        if binary == "/usr/bin/hc" and arguments[:2] == ("get", "t99"):
            return {
                "address": "t99",
                "state": "live",
                "status": {"app": "fast-agent"},
            }
        return "" if not expect_json else {}

    monkeypatch.setenv("HC_TERMINAL_ID", "t14q")
    monkeypatch.setattr(plugin, "_hc_binary", lambda: "/usr/bin/hc")
    monkeypatch.setattr(plugin, "_herdr_app_binary", lambda: "/usr/bin/herdr-app")
    monkeypatch.setattr(plugin, "_fast_agent_binary", lambda: "/usr/bin/fast-agent")
    monkeypatch.setattr(plugin, "_run_command", run_command)

    result = asyncio.run(
        plugin.fork_pane(
            _context(
                manager,
                arguments='"Alt branch"',
                plugin_config={
                    "direction": "auto",
                    "focus": "fork",
                    "startup_timeout_ms": 2000,
                },
            )
        )
    )

    assert manager.title == "Alt branch"
    assert "fork-session" in (result.markdown or "")
    assert "p_new" in (result.markdown or "")
    assert "t99" in (result.markdown or "")
    assert "warnings" not in (result.markdown or "").lower()

    split_calls = [
        c for c in calls if c[0] == "/usr/bin/herdr-app" and c[1][:2] == ("do", "pane.split")
    ]
    assert len(split_calls) == 1
    split_payload = json.loads(split_calls[0][1][2])
    assert split_payload["direction"] == "right"
    assert split_payload["pane"] == "p_orig"
    assert split_payload["argv"] == [
        "/usr/bin/fast-agent",
        "go",
        "--workspace",
        "/tmp/work space",
        "--home",
        "/tmp/home dir",
        "--model",
        "provider.current-model?reasoning=high",
        "--resume",
        "source-session",
    ]

    focus_calls = [
        c for c in calls if c[0] == "/usr/bin/herdr-app" and c[1][:2] == ("do", "pane.focus")
    ]
    assert len(focus_calls) == 1
    assert json.loads(focus_calls[0][1][2]) == {"pane": "p_orig"}

    label_calls = [c for c in calls if c[0] == "/usr/bin/hc" and c[1][0] == "label"]
    assert ("label", "t14q", "session=fork-session") in [c[1] for c in label_calls]
    assert ("label", "t99", "session=source-session") in [c[1] for c in label_calls]


def test_remote_hc_only_uses_label_adoption(plugin, monkeypatch: pytest.MonkeyPatch) -> None:
    manager = _Manager()
    calls: list[tuple[str, tuple[str, ...], bool]] = []

    async def run_command(binary: str, *arguments: str, expect_json: bool = True):
        calls.append((binary, arguments, expect_json))
        if arguments[:2] == ("get", "t7"):
            return {
                "address": "t7",
                "cols": 120,
                "rows": 40,
                "labels": {"herdr.space": "sp_remote", "herdr.tab": "tb_remote"},
                "state": "live",
            }
        if arguments[0] == "run":
            return {"address": "t8"}
        if arguments[:2] == ("get", "t8"):
            return {
                "address": "t8",
                "state": "live",
                "foreground": {"name": "fast-agent"},
            }
        return "" if not expect_json else {}

    monkeypatch.setenv("HC_TERMINAL_ID", "t7")
    monkeypatch.setattr(plugin, "_hc_binary", lambda: "/usr/bin/hc")
    monkeypatch.setattr(plugin, "_herdr_app_binary", lambda: None)
    monkeypatch.setattr(plugin, "_fast_agent_binary", lambda: "/usr/bin/fast-agent")
    monkeypatch.setattr(plugin, "_run_command", run_command)

    result = asyncio.run(
        plugin.fork_pane(_context(manager, plugin_config={"startup_timeout_ms": 2000}))
    )

    assert "Herdr terminal `t8`" in (result.markdown or "")
    assert "warnings" not in (result.markdown or "").lower()

    run_calls = [c[1] for c in calls if c[1][0] == "run"]
    assert len(run_calls) == 1
    args = run_calls[0]
    assert "--bg" in args
    assert "--no-parent" in args
    assert "--keep" in args
    assert "herdr.space=sp_remote" in args
    assert "herdr.tab=tb_remote" in args
    assert "session=source-session" in args


def test_early_exit_reports_warning_with_recent_output(
    plugin, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = _Manager()

    async def run_command(binary: str, *arguments: str, expect_json: bool = True):
        if arguments[:2] == ("get", "t1"):
            return {"address": "t1", "cols": 100, "rows": 40, "labels": {}, "state": "live"}
        if arguments[0] == "run":
            return {"address": "t2"}
        if arguments[:2] == ("get", "t2"):
            return {"address": "t2", "state": "finished", "exit_code": 2}
        if arguments[:2] == ("read", "t2"):
            return "error: bad model\n"
        return "" if not expect_json else {}

    monkeypatch.setenv("HC_TERMINAL_ID", "t1")
    monkeypatch.setattr(plugin, "_hc_binary", lambda: "/usr/bin/hc")
    monkeypatch.setattr(plugin, "_herdr_app_binary", lambda: None)
    monkeypatch.setattr(plugin, "_fast_agent_binary", lambda: "/usr/bin/fast-agent")
    monkeypatch.setattr(plugin, "_run_command", run_command)

    result = asyncio.run(
        plugin.fork_pane(_context(manager, plugin_config={"startup_timeout_ms": 2000}))
    )

    assert "exited early" in (result.markdown or "")
    assert "error: bad model" in (result.markdown or "")


def test_resume_argv_omits_default_workspace_home(plugin, monkeypatch) -> None:
    monkeypatch.setattr(plugin, "_fast_agent_binary", lambda: "/usr/bin/fast-agent")
    workspace = Path("/tmp/work space")
    argv = plugin._resume_argv(
        workspace=workspace,
        home=workspace / ".fast-agent",
        session_id="src-1",
        model="m1",
    )
    assert "--home" not in argv
    assert argv == [
        "/usr/bin/fast-agent",
        "go",
        "--workspace",
        "/tmp/work space",
        "--model",
        "m1",
        "--resume",
        "src-1",
    ]


def test_split_failure_returns_manual_resume_command(
    plugin, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = _Manager()

    async def run_command(binary: str, *arguments: str, expect_json: bool = True):
        if binary == "/usr/bin/hc" and arguments[:2] == ("get", "t14q"):
            return {"address": "t14q", "cols": 180, "rows": 50, "labels": {}, "state": "live"}
        if binary == "/usr/bin/herdr-app" and arguments == ("ls",):
            return {
                "spaces": [
                    {
                        "id": "sp1",
                        "tabs": [{"id": "tb1", "terminals": [{"terminal": "t14q", "pane": "p1"}]}],
                    }
                ]
            }
        if binary == "/usr/bin/herdr-app" and arguments[:2] == ("do", "pane.split"):
            raise RuntimeError("split failed")
        return "" if not expect_json else {}

    monkeypatch.setenv("HC_TERMINAL_ID", "t14q")
    monkeypatch.setattr(plugin, "_hc_binary", lambda: "/usr/bin/hc")
    monkeypatch.setattr(plugin, "_herdr_app_binary", lambda: "/usr/bin/herdr-app")
    monkeypatch.setattr(plugin, "_fast_agent_binary", lambda: "/usr/bin/fast-agent")
    monkeypatch.setattr(plugin, "_run_command", run_command)

    result = asyncio.run(plugin.fork_pane(_context(manager)))

    assert "Resume it manually with:" in (result.markdown or "")
    assert "--resume source-session" in (result.markdown or "")
    assert "split failed" in (result.markdown or "")
