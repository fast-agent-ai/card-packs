import asyncio
import importlib.util
import json
import os
import shlex
import sys
import tempfile
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


def _install_fast_agent_logger_stub() -> None:
    if "fast_agent.core.logging.logger" in sys.modules:
        return

    fast_agent = types.ModuleType("fast_agent")
    core = types.ModuleType("fast_agent.core")
    logging_pkg = types.ModuleType("fast_agent.core.logging")
    logger_mod = types.ModuleType("fast_agent.core.logging.logger")

    class _Logger:
        def info(self, *args, **kwargs):
            return None

        def warning(self, *args, **kwargs):
            return None

    logger_mod.get_logger = lambda name: _Logger()

    fast_agent.core = core
    core.logging = logging_pkg
    logging_pkg.logger = logger_mod

    sys.modules["fast_agent"] = fast_agent
    sys.modules["fast_agent.core"] = core
    sys.modules["fast_agent.core.logging"] = logging_pkg
    sys.modules["fast_agent.core.logging.logger"] = logger_mod


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec is not None and spec.loader is not None
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _extract_globs(command: str) -> list[str]:
    tokens = shlex.split(command)
    globs: list[str] = []
    i = 0
    while i < len(tokens):
        token = tokens[i]
        if token in {"-g", "--glob"} and i + 1 < len(tokens):
            globs.append(tokens[i + 1])
            i += 2
            continue
        if token.startswith("--glob="):
            globs.append(token.split("=", 1)[1])
        i += 1
    return globs


def _make_ctx(payload: dict[str, object], command: str, *, tool_name: str = "bash"):
    user_message = SimpleNamespace(
        role="user",
        content=[{"type": "text", "text": json.dumps(payload)}],
    )
    tool_call = SimpleNamespace(
        params=SimpleNamespace(name=tool_name, arguments={"command": command}),
    )
    ctx = SimpleNamespace(
        hook_type="before_tool_call",
        message=SimpleNamespace(tool_calls={"call-1": tool_call}),
        message_history=[],
        runner=SimpleNamespace(delta_messages=[user_message]),
    )
    return ctx, tool_call


class RipgrepHookTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _install_fast_agent_logger_stub()
        repo_root = Path(__file__).resolve().parents[1]
        cls.smart = _load_module(
            "test_smart_ripgrep_hook",
            repo_root / "packs/smart/hooks/fix_ripgrep_tool_calls.py",
        )
        cls.hf_dev = _load_module(
            "test_hf_dev_ripgrep_hook",
            repo_root / "packs/hf-dev/hooks/fix_ripgrep_tool_calls.py",
        )
        cls.codex = _load_module(
            "test_codex_ripgrep_hook",
            repo_root / "packs/codex/hooks/ripgrep_readonly_guard.py",
        )

    def test_explicit_roots_override_repo_root_for_normalization(self):
        for module in (self.smart, self.hf_dev):
            with self.subTest(module=module.__name__), tempfile.TemporaryDirectory() as tmpdir:
                repo_root = Path(tmpdir) / "repo"
                explicit_root = repo_root / "app"
                repo_only_target = repo_root / "tests" / "unit"
                explicit_root.mkdir(parents=True)
                repo_only_target.mkdir(parents=True)

                payload = {
                    "repo_root": str(repo_root),
                    "roots": [str(explicit_root)],
                }
                ctx, tool_call = _make_ctx(payload, "rg -n -F needle tests/unit")

                with mock.patch.dict(os.environ, {}, clear=True):
                    asyncio.run(module.fix_ripgrep_tool_calls(ctx))

                command = tool_call.params.arguments["command"]
                self.assertNotIn(str(repo_only_target), command)
                self.assertIn(str(explicit_root), command)

    def test_hallucinated_exec_tool_names_normalize_to_bash(self):
        payload = {"repo_root": "/tmp/repo"}
        for module in (self.smart, self.hf_dev):
            with self.subTest(module=module.__name__):
                ctx, tool_call = _make_ctx(payload, "rg -R needle /tmp/repo", tool_name="exec")

                asyncio.run(module.fix_ripgrep_tool_calls(ctx))

                self.assertEqual("bash", tool_call.params.name)
                self.assertNotIn(" -R ", f" {tool_call.params.arguments['command']} ")

    def test_native_execute_tool_remains_supported(self):
        payload = {"repo_root": "/tmp/repo"}
        for module in (self.smart, self.hf_dev):
            with self.subTest(module=module.__name__):
                ctx, tool_call = _make_ctx(
                    payload,
                    "rg -R needle /tmp/repo",
                    tool_name="execute",
                )

                asyncio.run(module.fix_ripgrep_tool_calls(ctx))

                self.assertEqual("execute", tool_call.params.name)
                self.assertNotIn(" -R ", f" {tool_call.params.arguments['command']} ")

    def test_explicit_roots_do_not_receive_broad_default_excludes(self):
        for module in (self.smart, self.hf_dev):
            with self.subTest(module=module.__name__), tempfile.TemporaryDirectory() as tmpdir:
                repo_root = Path(tmpdir) / "repo"
                explicit_root = repo_root / "src"
                explicit_root.mkdir(parents=True)

                payload = {
                    "repo_root": str(repo_root),
                    "roots": [str(explicit_root)],
                }
                ctx, tool_call = _make_ctx(payload, f"rg --files {explicit_root} -g '*hook*'")

                with mock.patch.dict(os.environ, {}, clear=True):
                    asyncio.run(module.fix_ripgrep_tool_calls(ctx))

                globs = _extract_globs(tool_call.params.arguments["command"])
                self.assertNotIn("!.git/**", globs)
                self.assertNotIn("!node_modules/**", globs)
                self.assertFalse(any(glob.endswith("/sessions/**") for glob in globs))

    def test_broad_repo_search_keeps_logs_but_excludes_sessions_and_noise(self):
        for module in (self.smart, self.hf_dev):
            with self.subTest(module=module.__name__), tempfile.TemporaryDirectory() as tmpdir:
                repo_root = Path(tmpdir) / "repo"
                repo_root.mkdir(parents=True)

                payload = {"repo_root": str(repo_root)}
                ctx, tool_call = _make_ctx(payload, f"rg --files {repo_root} -g '*failure*'")

                with mock.patch.dict(os.environ, {}, clear=True):
                    asyncio.run(module.fix_ripgrep_tool_calls(ctx))

                globs = _extract_globs(tool_call.params.arguments["command"])
                self.assertIn("!.git/**", globs)
                self.assertIn("!node_modules/**", globs)
                self.assertFalse(any(glob == "!*.log" for glob in globs))
                self.assertIn("!.fast-agent/sessions/**", globs)

    def test_explicit_session_root_is_not_filtered_out(self):
        for module in (self.smart, self.hf_dev):
            with self.subTest(module=module.__name__), tempfile.TemporaryDirectory() as tmpdir:
                repo_root = Path(tmpdir) / "repo"
                session_root = repo_root / ".fast-agent" / "sessions"
                session_root.mkdir(parents=True)

                payload = {
                    "repo_root": str(repo_root),
                    "roots": [str(session_root)],
                }
                ctx, tool_call = _make_ctx(payload, f"rg --files {session_root} -g '*session*'")

                with mock.patch.dict(os.environ, {}, clear=True):
                    asyncio.run(module.fix_ripgrep_tool_calls(ctx))

                globs = _extract_globs(tool_call.params.arguments["command"])
                self.assertFalse(any(glob.endswith("/sessions/**") for glob in globs))

    def test_existing_absolute_path_outside_explicit_roots_is_rewritten(self):
        for module in (self.smart, self.hf_dev):
            with self.subTest(module=module.__name__), tempfile.TemporaryDirectory() as tmpdir:
                repo_root = Path(tmpdir) / "repo"
                explicit_root = repo_root / "src"
                outside_file = repo_root / "logs" / "failure.log"
                explicit_root.mkdir(parents=True)
                outside_file.parent.mkdir(parents=True)
                outside_file.write_text("boom\n")

                normalized = module._normalize_relative_rg_paths(
                    f"rg -n -F boom {outside_file}",
                    [explicit_root],
                )

                self.assertNotIn(str(outside_file), normalized)
                self.assertIn(str(explicit_root), normalized)

    def test_codex_repo_root_only_search_gets_broad_excludes(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir) / "repo"
            repo_root.mkdir(parents=True)
            payload = {"repo_root": str(repo_root)}
            ctx, tool_call = _make_ctx(payload, f"rg --files {repo_root} -g '*hook*'")

            with mock.patch.dict(os.environ, {}, clear=True):
                asyncio.run(self.codex.ripgrep_loop_guard(ctx))

            globs = _extract_globs(tool_call.params.arguments["command"])
            self.assertIn("!.git/**", globs)
            self.assertIn("!node_modules/**", globs)
            self.assertIn("!.fast-agent/sessions/**", globs)
            self.assertFalse(any(glob == "!*.log" for glob in globs))

    def test_codex_existing_absolute_path_outside_explicit_roots_is_rewritten(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir) / "repo"
            explicit_root = repo_root / "src"
            outside_file = repo_root / "tmp" / "scratch.txt"
            explicit_root.mkdir(parents=True)
            outside_file.parent.mkdir(parents=True)
            outside_file.write_text("scratch\n")

            normalized = self.codex._normalize_relative_path_tokens(
                f"rg -n -F scratch {outside_file}",
                [explicit_root],
            )

            self.assertNotIn(str(outside_file), normalized)
            self.assertIn(str(explicit_root), normalized)

    def test_codex_prefers_fast_agent_home_for_session_exclude(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir) / "repo"
            repo_root.mkdir(parents=True)
            fast_agent_home = repo_root / ".custom-fast-agent"
            payload = {"repo_root": str(repo_root)}
            ctx, tool_call = _make_ctx(payload, f"rg --files {repo_root} -g '*hook*'")

            with mock.patch.dict(
                os.environ,
                {
                    "FAST_AGENT_HOME": str(fast_agent_home),
                    "ENVIRONMENT_DIR": str(repo_root / ".legacy"),
                },
                clear=True,
            ):
                asyncio.run(self.codex.ripgrep_loop_guard(ctx))

            globs = _extract_globs(tool_call.params.arguments["command"])
            self.assertIn("!.custom-fast-agent/sessions/**", globs)
            self.assertNotIn("!.legacy/sessions/**", globs)

    def test_codex_blocks_unbounded_broad_root_search(self):
        payload = {"repo_root": str(Path.home())}
        ctx, tool_call = _make_ctx(payload, f"rg -n -F needle {Path.home()}")

        asyncio.run(self.codex.ripgrep_loop_guard(ctx))

        command = tool_call.params.arguments["command"]
        self.assertIn("Blocked broad search:", command)
        self.assertIn("Search guard:", command)

    def test_codex_after_tool_call_marks_high_volume_output(self):
        result = SimpleNamespace(
            content=[
                {
                    "type": "text",
                    "text": "Shell to agent output reached limit; additional output omitted",
                }
            ]
        )
        runner = SimpleNamespace()
        ctx = SimpleNamespace(
            hook_type="after_tool_call",
            message=SimpleNamespace(tool_results={"call-1": result}),
            message_history=[],
            runner=runner,
        )

        asyncio.run(self.codex.ripgrep_loop_guard(ctx))

        self.assertTrue(runner._ripgrep_output_overflow)
        self.assertTrue(
            any("Search guard:" in getattr(item, "text", "") for item in result.content)
        )


if __name__ == "__main__":
    unittest.main()
