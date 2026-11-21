"""Unified fast-agent usage, cost, and performance tracking plugin.

Combines per-turn cost display, turn latency/phase breakdown, ETag-updated
pricing tables, and a central SQLite WAL ledger (~/.fast-agent/usage/usage.sqlite3)
with social-ready ASCII dashboards and safe historical session sweeping.
"""

from __future__ import annotations

import asyncio
import importlib
import importlib.util
import logging
import shlex
import sys
import threading
from contextlib import nullcontext
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from fast_agent.command_actions import (
        PluginCommandActionContext,
        PluginCommandActionResult,
        PluginCommandCompletion,
        PluginCommandCompletionContext,
    )
    from fast_agent.plugins import PluginPostUserTurnContext, PluginPostUserTurnOutput

try:
    from fast_agent.plugins import PluginPostUserTurnOutput as _PostUserTurnOutput
except ImportError:
    _PostUserTurnOutput = None  # type: ignore[assignment]


_LOGGER = logging.getLogger(__name__)
_PLUGIN_DIR = Path(__file__).resolve().parent
_PACKAGE_DIR = _PLUGIN_DIR / "_usage_tracker"
_PACKAGE_NAME = "_usage_tracker"


def _ensure_package() -> None:
    """Register the sibling ``_usage_tracker`` package without mutating ``sys.path``."""
    existing = sys.modules.get(_PACKAGE_NAME)
    existing_file = getattr(existing, "__file__", None)
    expected_init = (_PACKAGE_DIR / "__init__.py").resolve()
    if existing is not None and existing_file is not None:
        try:
            if Path(existing_file).resolve() == expected_init:
                return
        except OSError:
            pass

    spec = importlib.util.spec_from_file_location(
        _PACKAGE_NAME,
        expected_init,
        submodule_search_locations=[str(_PACKAGE_DIR)],
    )
    if spec is None or spec.loader is None:
        raise ImportError(f"Could not load {_PACKAGE_NAME} from {_PACKAGE_DIR}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[_PACKAGE_NAME] = module
    spec.loader.exec_module(module)


_ensure_package()

charts = importlib.import_module(f"{_PACKAGE_NAME}.charts")
models = importlib.import_module(f"{_PACKAGE_NAME}.models")
perf = importlib.import_module(f"{_PACKAGE_NAME}.perf")
pricing = importlib.import_module(f"{_PACKAGE_NAME}.pricing")
pricing_updater = importlib.import_module(f"{_PACKAGE_NAME}.pricing_updater")
store = importlib.import_module(f"{_PACKAGE_NAME}.store")
sweep = importlib.import_module(f"{_PACKAGE_NAME}.sweep")

PLUGIN_NAME: str = models.PLUGIN_NAME
PLUGIN_VERSION: str = models.PLUGIN_VERSION

_WINDOWS = ("24h", "7d", "30d", "90d", "all")
_USAGE_SUBCOMMANDS = ("share", "perf", "tools", "sweep", "pricing", "reprice", "help")
_USAGE_FLAGS = (
    "--no-cost",
    "--plain",
    "--utc",
    "--local",
    "--model",
    "--raw",
    "--dry-run",
    "--all",
)

_USAGE_HELP = """Usage:
  /stats [7d|30d|90d|all|24h] [--no-cost] [--plain] [--utc|--local] [--model <filter>]
  /stats share [7d|30d|90d|all] [--no-cost] [--utc|--local] [--model <filter>]
  /stats perf [7d|30d|90d|all] [--plain] [--utc|--local] [--model <filter>]
  /stats tools [7d|30d|90d|all] [--raw] [--plain] [--utc|--local] [--model <filter>]
  /stats sweep <dir> [--dry-run] [--all]
  /stats pricing [status|update]
  /stats reprice"""


def _bool_setting(config: Mapping[str, object], key: str, default: bool) -> bool:
    value = config.get(key)
    return value if isinstance(value, bool) else default


def _default_tz_mode(config: Mapping[str, object]) -> Literal["utc", "local"]:
    value = config.get("timezone")
    if isinstance(value, str) and value.strip().casefold() == "local":
        return "local"
    return "utc"


def _command_config(ctx: PluginCommandActionContext) -> Mapping[str, object]:
    settings = getattr(ctx, "settings", None)
    plugins = getattr(settings, "plugins", None)
    raw = getattr(plugins, "config", None)
    return perf.plugin_config(raw if isinstance(raw, dict) else None)


def _configure_runtime(config: Mapping[str, object]) -> tuple[Path, Path]:
    data_dir = pricing_updater.resolve_data_dir(config)
    pricing.set_active_data_dir(data_dir)
    db_path = store.resolve_db_path(config)
    return data_dir, db_path


def _split_args(raw: str) -> list[str]:
    stripped = raw.strip()
    if not stripped:
        return []
    try:
        return shlex.split(stripped)
    except ValueError:
        return stripped.split()


def _parse_report_flags(
    tokens: Sequence[str],
    *,
    default_tz: Literal["utc", "local"],
    allow_raw: bool = False,
) -> tuple[str, bool, bool, bool, Literal["utc", "local"], str | None, str | None]:
    """Parse ``[window] [--no-cost] [--plain] [--raw] [--utc|--local] [--model <x>]``.

    Returns ``(window, show_cost, plain, raw, tz_mode, model_filter, error)``.
    """
    window = "30d"
    window_set = False
    show_cost = True
    plain = False
    raw = False
    tz_mode: Literal["utc", "local"] = default_tz
    model_filter: str | None = None

    idx = 0
    while idx < len(tokens):
        tok = tokens[idx]
        low = tok.casefold()
        if low in {"--no-cost", "--tokens-only"}:
            show_cost = False
        elif low == "--plain":
            plain = True
        elif low == "--raw" and allow_raw:
            raw = True
        elif low == "--utc":
            tz_mode = "utc"
        elif low == "--local":
            tz_mode = "local"
        elif low == "--model":
            if idx + 1 >= len(tokens):
                return (
                    window,
                    show_cost,
                    plain,
                    raw,
                    tz_mode,
                    model_filter,
                    "Missing value for --model.",
                )
            idx += 1
            model_filter = tokens[idx]
        elif low.startswith("--model="):
            value = tok.split("=", 1)[1].strip()
            if not value:
                return (
                    window,
                    show_cost,
                    plain,
                    raw,
                    tz_mode,
                    model_filter,
                    "Missing value for --model.",
                )
            model_filter = value
        elif tok.startswith("-"):
            return window, show_cost, plain, raw, tz_mode, model_filter, f"Unknown option: {tok}"
        else:
            if window_set:
                return (
                    window,
                    show_cost,
                    plain,
                    raw,
                    tz_mode,
                    model_filter,
                    f"Unexpected argument: {tok}",
                )
            try:
                store.parse_window(low)
            except ValueError as exc:
                return window, show_cost, plain, raw, tz_mode, model_filter, str(exc)
            window = low
            window_set = True
        idx += 1

    return window, show_cost, plain, raw, tz_mode, model_filter, None


def _make_result(**kwargs: Any) -> PluginCommandActionResult:
    from fast_agent.command_actions import PluginCommandActionResult

    return PluginCommandActionResult(**kwargs)


def _format_card_result(
    card: str,
    *,
    is_tui: bool,
    color: bool,
) -> PluginCommandActionResult:
    if is_tui:
        return _make_result(message=card)
    if color:
        return _make_result(message=card)
    return _make_result(markdown=f"```text\n{card}\n```")


async def on_post_user_turn(
    ctx: PluginPostUserTurnContext,
) -> str | PluginPostUserTurnOutput | None:
    """Record turn usage/cost/perf to SQLite and emit optional post-turn summary."""
    config = ctx.config if isinstance(ctx.config, dict) else dict(ctx.config)
    data_dir, db_path = _configure_runtime(config)
    perf_settings = perf.Settings.from_config(config)

    turn_usage = tuple(ctx.turn_usage)
    session_usage = tuple(ctx.session_usage)
    turn_messages = tuple(getattr(ctx, "turn_messages", ()))
    session_id = getattr(ctx, "session_id", None)

    auto_update = _bool_setting(config, "pricing_auto_update", True)
    has_unpriced = False
    if _bool_setting(config, "record_usage", True) and (turn_usage or turn_messages):
        try:
            turn_record = perf.record_from_history(turn_messages) if turn_messages else None
            catalog, _source = pricing_updater.get_active_catalog(data_dir)
            with store.UsageStore(db_path) as usage_store:
                record_result = usage_store.record_turn(
                    session_id=session_id if isinstance(session_id, str) else None,
                    attempts=turn_usage,
                    turn_record=turn_record,
                    catalog=catalog,
                    source="live",
                    burst_ms=perf_settings.burst_ms,
                )
                if auto_update:
                    has_unpriced = record_result.unpriced_inserted > 0 or usage_store.has_unpriced()
        except Exception:
            # Persistence must never break an interactive turn.
            _LOGGER.debug("usage-tracker failed to record turn", exc_info=True)

    if auto_update:
        try:
            pricing_updater.maybe_schedule_background_update(
                data_dir=data_dir,
                config=config,
                has_unpriced=has_unpriced,
            )
        except Exception:
            _LOGGER.debug("usage-tracker failed to schedule pricing update", exc_info=True)

    lines: list[str] = []
    usage_token: str | None = None

    if not turn_usage:
        if _bool_setting(config, "show_cost", True):
            await pricing.report_session_cost_to_herdr(None)
    else:
        cost_display = pricing.format_post_turn_cost(turn_usage, session_usage)
        usage_token = cost_display.usage_token if cost_display is not None else None
        if _bool_setting(config, "show_cost", True):
            await pricing.report_session_cost_to_herdr(usage_token)
            if cost_display is not None and cost_display.display:
                lines.append(cost_display.display)

    if _bool_setting(config, "show_perf", True):
        perf_line = perf.display_perf(ctx)
        if perf_line:
            lines.append(perf_line)

    display = "\n".join(lines) if lines else None
    if display is None and usage_token is None:
        return None
    if _PostUserTurnOutput is None:
        return display
    return _PostUserTurnOutput(display=display, session_usage=usage_token)


async def complete_stats(
    ctx: PluginCommandCompletionContext,
) -> list[PluginCommandCompletion]:
    from fast_agent.command_actions import PluginCommandCompletion

    completed = tuple(token.casefold() for token in ctx.completed_tokens)
    current = ctx.current_token.casefold()

    if not completed:
        items = [
            PluginCommandCompletion("7d", detail="Last 7 days usage card"),
            PluginCommandCompletion("30d", detail="Last 30 days usage card (default)"),
            PluginCommandCompletion("90d", detail="Last 90 days usage card"),
            PluginCommandCompletion("all", detail="All-time usage card"),
            PluginCommandCompletion("share", detail="Copy-pasteable social sharing card"),
            PluginCommandCompletion("perf", detail="Latency, throughput, and phase breakdown"),
            PluginCommandCompletion("tools", detail="Tool execution time and run counts"),
            PluginCommandCompletion("sweep", detail="Scan a directory of fast-agent sessions"),
            PluginCommandCompletion("pricing", detail="Check or update the pricing catalog"),
            PluginCommandCompletion("reprice", detail="Backfill costs for unpriced calls"),
            PluginCommandCompletion("help", detail="Show /stats options"),
        ]
        return [item for item in items if not current or item.value.startswith(current)]

    first = completed[0]
    if first == "pricing" and len(completed) == 1:
        items = [
            PluginCommandCompletion("status", detail="Show active pricing catalog status"),
            PluginCommandCompletion("update", detail="Fetch latest pricing catalog (ETag)"),
        ]
        return [item for item in items if not current or item.value.startswith(current)]

    if first == "sweep":
        items = [
            PluginCommandCompletion("--dry-run", detail="Preview sweep without writing"),
            PluginCommandCompletion(
                "--all", detail="Scan arbitrary nested trees (not only .fast-agent/sessions)"
            ),
        ]
        return [item for item in items if not current or item.value.startswith(current)]

    if first in {"share", "perf", "tools"} or first in _WINDOWS:
        candidates = [
            PluginCommandCompletion("7d", detail="Last 7 days"),
            PluginCommandCompletion("30d", detail="Last 30 days"),
            PluginCommandCompletion("90d", detail="Last 90 days"),
            PluginCommandCompletion("all", detail="All-time"),
            PluginCommandCompletion("--no-cost", detail="Hide cost figures"),
            PluginCommandCompletion("--plain", detail="Plain ASCII without Rich colors"),
            PluginCommandCompletion("--utc", detail="4h rhythm heatmap in UTC"),
            PluginCommandCompletion("--local", detail="4h rhythm heatmap in local time"),
            PluginCommandCompletion("--model", detail="Filter by model substring"),
        ]
        if first == "tools":
            candidates.append(
                PluginCommandCompletion(
                    "--raw", detail="Show raw outer tool calls without unwrapping PTC sandboxes"
                )
            )
        return [
            item
            for item in candidates
            if item.value not in completed and (not current or item.value.startswith(current))
        ]

    return []


async def stats_command(ctx: PluginCommandActionContext) -> PluginCommandActionResult:
    """Handle ``/stats`` subcommands and dashboards."""
    config = _command_config(ctx)
    data_dir, db_path = _configure_runtime(config)
    perf_settings = perf.Settings.from_config(config)
    is_tui = bool(getattr(ctx, "is_tui", False))
    tokens = _split_args(ctx.arguments)

    sub = tokens[0].casefold() if tokens else ""
    if sub in {"help", "-h", "--help"}:
        return _make_result(message=_USAGE_HELP)

    if sub == "sweep":
        return await _handle_sweep(
            tokens[1:],
            data_dir=data_dir,
            db_path=db_path,
            session_cwd=getattr(ctx, "session_cwd", None),
            is_tui=is_tui,
            burst_ms=perf_settings.burst_ms,
        )

    if sub == "pricing":
        return await _handle_pricing(
            tokens[1:],
            data_dir=data_dir,
            db_path=db_path,
            config=config,
            is_tui=is_tui,
        )

    if sub == "reprice":
        catalog, _source = pricing_updater.get_active_catalog(data_dir)
        updated = await asyncio.to_thread(store.reprice_unpriced, db_path, catalog)
        return _make_result(
            message=(
                f"Repriced {updated:,} previously unpriced "
                f"{'call' if updated == 1 else 'calls'} using pricing catalog "
                f"{catalog.catalog_version}."
            )
        )

    mode = "usage"
    rest = tokens
    if sub in {"share", "perf", "tools"}:
        mode = sub
        rest = tokens[1:]

    window, show_cost, plain, raw, tz_mode, model_filter, error = _parse_report_flags(
        rest,
        default_tz=_default_tz_mode(config),
        allow_raw=(mode == "tools"),
    )
    if error is not None:
        return _make_result(message=f"{error}\n\n{_USAGE_HELP}")

    status = pricing_updater.get_status(data_dir=data_dir, config=config)
    report = await asyncio.to_thread(
        store.query_report,
        db_path,
        window=window,
        model_filter=model_filter,
        tz_mode=tz_mode,
        pricing_version=status.active_version,
        upgrade_notice=status.upgrade_notice,
    )

    if mode == "share":
        return _make_result(markdown=charts.render_share_card(report, show_cost=show_cost))

    use_color = is_tui and not plain
    if mode == "perf":
        card = charts.render_perf_card(report, color=use_color)
        return _format_card_result(card, is_tui=is_tui, color=use_color)
    if mode == "tools":
        card = charts.render_tools_card(report, color=use_color, raw=raw)
        return _format_card_result(card, is_tui=is_tui, color=use_color)

    card = charts.render_usage_card(report, color=use_color, show_cost=show_cost)
    return _format_card_result(card, is_tui=is_tui, color=use_color)


def _short_progress_path(current_dir: str, root: Path, *, max_len: int = 36) -> str:
    try:
        rel = Path(current_dir).resolve().relative_to(root)
        label = "." if str(rel) == "." else rel.as_posix()
    except (OSError, ValueError):
        label = current_dir
    if len(label) > max_len:
        label = f"…{label[-(max_len - 1) :]}"
    return label.replace("[", r"\[")


def _format_sweep_progress(
    progress: Any,
    *,
    root: Path,
    dry_run: bool,
) -> str:
    prefix = "Sweeping (dry run)" if dry_run else "Sweeping"
    loc = _short_progress_path(progress.current_dir, root)
    return (
        f"[cyan]{prefix}[/cyan] · "
        f"{progress.dirs_scanned:,} dirs · "
        f"{progress.sessions_scanned:,} sessions · "
        f"{progress.files_scanned:,} files · "
        f"{progress.calls_imported:,} calls "
        f"[dim]({loc})[/dim]"
    )


def _tui_status_context(initial_message: str, *, is_tui: bool) -> Any:
    if not is_tui:
        return nullcontext(None)
    try:
        from fast_agent.ui.console import console
    except ImportError:
        return nullcontext(None)
    return console.status(initial_message, spinner="dots")


async def _handle_sweep(
    tokens: Sequence[str],
    *,
    data_dir: Path,
    db_path: Path,
    session_cwd: Path | None,
    is_tui: bool,
    burst_ms: float,
) -> PluginCommandActionResult:
    dry_run = False
    include_all = False
    plain = False
    paths: list[str] = []
    for tok in tokens:
        low = tok.casefold()
        if low == "--dry-run":
            dry_run = True
        elif low == "--all":
            include_all = True
        elif low == "--plain":
            plain = True
        elif tok.startswith("-"):
            return _make_result(message=f"Unknown option for /stats sweep: {tok}")
        else:
            paths.append(tok)

    if len(paths) != 1:
        return _make_result(message="Usage: /stats sweep <dir> [--dry-run] [--all]")

    raw_path = Path(paths[0]).expanduser()
    if not raw_path.is_absolute() and session_cwd is not None:
        target = (session_cwd / raw_path).resolve()
    else:
        target = raw_path.resolve()

    catalog, _source = pricing_updater.get_active_catalog(data_dir)
    cancel_event = threading.Event()
    initial = "[cyan]Sweeping (dry run)…[/cyan]" if dry_run else "[cyan]Sweeping…[/cyan]"
    try:
        with _tui_status_context(initial, is_tui=is_tui) as status:

            def _on_progress(progress: Any) -> None:
                if status is not None:
                    status.update(_format_sweep_progress(progress, root=target, dry_run=dry_run))

            summary = await asyncio.to_thread(
                sweep.sweep_directory,
                target,
                db_path=db_path,
                catalog=catalog,
                dry_run=dry_run,
                include_all=include_all,
                burst_ms=burst_ms,
                on_progress=_on_progress if status is not None else None,
                cancel_event=cancel_event,
            )
    except ValueError as exc:
        return _make_result(message=str(exc))
    finally:
        cancel_event.set()

    use_color = is_tui and not plain
    card = charts.render_sweep_summary(summary, color=use_color)
    return _format_card_result(card, is_tui=is_tui, color=use_color)


async def _handle_pricing(
    tokens: Sequence[str],
    *,
    data_dir: Path,
    db_path: Path,
    config: Mapping[str, object],
    is_tui: bool,
) -> PluginCommandActionResult:
    plain = False
    action = "status"
    for tok in tokens:
        low = tok.casefold()
        if low == "--plain":
            plain = True
        elif low in {"status", "update"}:
            action = low
        else:
            return _make_result(message="Usage: /stats pricing [status|update]")

    repriced = 0
    if action == "update":
        pricing_url = config.get("pricing_url")
        url = (
            pricing_url.strip()
            if isinstance(pricing_url, str) and pricing_url.strip()
            else models.DEFAULT_PRICING_URL
        )
        status = await asyncio.to_thread(
            pricing_updater.check_for_update,
            data_dir=data_dir,
            pricing_url=url,
            force=True,
        )
        if not status.upgrade_required and status.last_error is None:
            catalog, _source = pricing_updater.get_active_catalog(data_dir)
            repriced = await asyncio.to_thread(store.reprice_unpriced, db_path, catalog)
    else:
        status = pricing_updater.get_status(data_dir=data_dir, config=config)

    use_color = is_tui and not plain
    card = charts.render_pricing_status(status, color=use_color)
    if repriced > 0:
        note = (
            f"\nRepriced {repriced:,} previously unpriced {'call' if repriced == 1 else 'calls'}."
        )
        card = f"{card}{note}"
    return _format_card_result(card, is_tui=is_tui, color=use_color)


async def complete_cost(
    ctx: PluginCommandCompletionContext,
) -> list[PluginCommandCompletion]:
    from fast_agent.command_actions import PluginCommandCompletion

    if ctx.completed_tokens:
        return []
    current = ctx.current_token.casefold()
    items = [
        PluginCommandCompletion("summary", detail="Cost by user turn (default)"),
        PluginCommandCompletion("detail", detail="Full provider-call ledger"),
    ]
    return [item for item in items if not current or item.value.startswith(current)]


async def cost_command(ctx: PluginCommandActionContext) -> PluginCommandActionResult:
    """Show current session model cost by user turn or provider-call ledger."""
    config = _command_config(ctx)
    _configure_runtime(config)

    mode = ctx.arguments.strip().casefold()
    usage = ctx.usage
    session_attempts = tuple(usage.turns) if usage is not None and usage.turns else ()
    report = pricing.cost_report(
        mode,
        message_history=ctx.message_history,
        agent_name=ctx.agent_name,
        user_turn_usage=ctx.user_turn_usage,
        session_attempts=session_attempts,
    )
    if report is None:
        return _make_result(message="Usage: /cost [summary|detail]")
    return _make_result(markdown=report.markdown, markdown_styles=report.markdown_styles)


async def complete_performance(
    ctx: PluginCommandCompletionContext,
) -> list[PluginCommandCompletion]:
    from fast_agent.command_actions import PluginCommandCompletion

    if ctx.completed_tokens:
        return []
    current = ctx.current_token.casefold()
    items = [
        PluginCommandCompletion("summary", detail="Timing phases by user turn (default)"),
        PluginCommandCompletion("detail", detail="Per-call TTFT, streaming, and tok/s"),
    ]
    return [item for item in items if not current or item.value.startswith(current)]


async def performance_command(
    ctx: PluginCommandActionContext,
) -> PluginCommandActionResult:
    """Show current session timing breakdown (wait, stream, tools, process)."""
    return await perf.performance_report(ctx)
