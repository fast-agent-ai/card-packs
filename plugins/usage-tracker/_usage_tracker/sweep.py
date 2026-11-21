"""Import saved fast-agent sessions into the usage store.

Walks a directory for session folders (those holding ``session.json``), reads each
active ``history_*.json`` (never ``*_previous.json`` backups), and records every user
turn's provider attempts, phases, and tool runs. Content hashes make re-sweeps,
resumed sessions, and forked sessions idempotent.
"""

from __future__ import annotations

import json
import os
import stat as stat_module
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .models import SweepProgress, SweepSummary
from .perf import (
    FAST_AGENT_USAGE,
    channel_texts,
    message_channels,
    message_role,
    record_from_history,
    restore_folds,
    split_user_turns,
)
from .pricing import UsageAttempt, attempts_from_usage_payload
from .store import PreparedTurn, UsageStore, prepare_turn

if TYPE_CHECKING:
    import threading
    from collections.abc import Callable, Iterator, Mapping, Sequence

    from .pricing import PricingCatalog

PRUNED_DIRS = frozenset(
    {
        ".git",
        "node_modules",
        ".venv",
        "venv",
        "__pycache__",
        "dist",
        "build",
        "target",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".cache",
        ".local",
        ".cargo",
        ".rustup",
        ".npm",
        ".pnpm-store",
        ".yarn",
        ".pyenv",
        ".rbenv",
        ".nvm",
        ".conda",
        ".mamba",
        ".tox",
        ".nox",
        "site-packages",
        "snap",
        "go",
        ".Trash",
        "Trash",
        ".locks",
        ".staging",
    }
)
FAST_AGENT_DIR = ".fast-agent"
SESSIONS_DIR = "sessions"
DEFAULT_SEARCH_DEPTH = 4
SESSION_FILE = "session.json"
SWEEP_SOURCE = "sweep"
_SYNTHETIC_FINAL_CHANNEL = "fast-agent-synthetic-final"
_PROGRESS_INTERVAL_S = 0.1


class InvalidHistoryError(ValueError):
    """A file that is not a readable fast-agent session history."""


@dataclass(slots=True)
class _Totals:
    sessions_scanned: int = 0
    files_scanned: int = 0
    files_skipped_unchanged: int = 0
    files_skipped_invalid: int = 0
    calls_imported: int = 0
    calls_duplicate: int = 0
    turns_imported: int = 0
    tool_runs_imported: int = 0
    tokens_imported: int = 0
    cost_usd_imported: float = 0.0
    unpriced_calls_imported: int = 0


# --------------------------------------------------------------------------- discovery


def _is_safe_regular_file(path: Path, allowed_root: Path) -> bool:
    """True when ``path`` is a non-symlink regular file resolving inside ``allowed_root``."""
    try:
        st = path.lstat()
    except OSError:
        return False
    if not stat_module.S_ISREG(st.st_mode):
        return False
    try:
        return path.resolve().is_relative_to(allowed_root.resolve())
    except OSError:
        return False


def _parse_iso_timestamp(value: object) -> float | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.timestamp()


def _walk_session_tree(
    start_dir: Path,
    resolved_root: Path,
    *,
    on_dir: Callable[[Path], None] | None = None,
    cancel_event: threading.Event | None = None,
) -> Iterator[Path]:
    """Walk a confirmed sessions directory (or single session folder), without symlinks."""
    for current, dirnames, filenames in os.walk(start_dir, followlinks=False):
        if cancel_event is not None and cancel_event.is_set():
            return
        current_path = Path(current)
        if on_dir is not None:
            on_dir(current_path)
        dirnames[:] = sorted(
            name
            for name in dirnames
            if not name.startswith(".")
            and name not in PRUNED_DIRS
            and not (current_path / name).is_symlink()
        )
        if SESSION_FILE in filenames and _is_safe_regular_file(
            current_path / SESSION_FILE, resolved_root
        ):
            yield current_path


def _is_inside_fast_agent_sessions(resolved: Path) -> bool:
    parts = resolved.parts
    return any(
        parts[idx] == FAST_AGENT_DIR and parts[idx + 1] == SESSIONS_DIR
        for idx in range(len(parts) - 1)
    )


def _walk_all_sessions(
    root_dir: Path,
    resolved_root: Path,
    *,
    max_depth: int | None = None,
    on_dir: Callable[[Path], None] | None = None,
    cancel_event: threading.Event | None = None,
) -> Iterator[Path]:
    stack: list[tuple[Path, int]] = [(root_dir, 0)]
    while stack:
        if cancel_event is not None and cancel_event.is_set():
            return
        current_path, depth = stack.pop()
        if on_dir is not None:
            on_dir(current_path)
        try:
            with os.scandir(current_path) as it:
                entries = list(it)
        except OSError:
            continue
        has_session = False
        subdirs: list[Path] = []
        for entry in entries:
            name = entry.name
            if name == SESSION_FILE:
                has_session = True
                continue
            if name in PRUNED_DIRS:
                continue
            try:
                if entry.is_dir(follow_symlinks=False):
                    subdirs.append(Path(entry.path))
            except OSError:
                continue
        if has_session and _is_safe_regular_file(current_path / SESSION_FILE, resolved_root):
            yield current_path
        if max_depth is None or depth < max_depth:
            for subdir in sorted(subdirs, reverse=True):
                stack.append((subdir, depth + 1))


def iter_session_dirs(
    root_dir: Path,
    *,
    include_all: bool = False,
    max_depth: int | None = None,
    on_dir: Callable[[Path], None] | None = None,
    cancel_event: threading.Event | None = None,
) -> Iterator[Path]:
    """Session folders under ``root_dir`` (inclusive), without following symlinks.

    By default (``include_all=False``), discovers ordinary fast-agent sessions:
    direct session folders (depth 0 or 1 under ``root_dir``) and ``.fast-agent/sessions``
    sidecar trees up to ``DEFAULT_SEARCH_DEPTH`` levels deep. Pass ``include_all=True``
    to scan arbitrary nested subtrees (e.g., benchmark or custom-env exports).
    """
    resolved_root = root_dir.resolve()
    if include_all:
        yield from _walk_all_sessions(
            root_dir,
            resolved_root,
            max_depth=max_depth,
            on_dir=on_dir,
            cancel_event=cancel_event,
        )
        return

    # Already targeting .fast-agent or a directory inside .fast-agent/sessions.
    if resolved_root.name == FAST_AGENT_DIR:
        sessions_dir = root_dir / SESSIONS_DIR
        if sessions_dir.is_dir() and not sessions_dir.is_symlink():
            yield from _walk_session_tree(
                sessions_dir, resolved_root, on_dir=on_dir, cancel_event=cancel_event
            )
        return
    if _is_inside_fast_agent_sessions(resolved_root):
        yield from _walk_session_tree(
            root_dir, resolved_root, on_dir=on_dir, cancel_event=cancel_event
        )
        return
    if FAST_AGENT_DIR in resolved_root.parts:
        return

    # Direct session folder (depth 0).
    if _is_safe_regular_file(root_dir / SESSION_FILE, resolved_root):
        yield from _walk_session_tree(
            root_dir, resolved_root, on_dir=on_dir, cancel_event=cancel_event
        )
        return

    search_depth = DEFAULT_SEARCH_DEPTH if max_depth is None else max_depth
    stack: list[tuple[Path, int]] = [(root_dir, 0)]
    while stack:
        if cancel_event is not None and cancel_event.is_set():
            return
        current_path, depth = stack.pop()
        if on_dir is not None:
            on_dir(current_path)
        try:
            with os.scandir(current_path) as it:
                entries = list(it)
        except OSError:
            continue

        subdirs: list[tuple[str, Path]] = []
        for entry in entries:
            try:
                if entry.is_dir(follow_symlinks=False):
                    subdirs.append((entry.name, Path(entry.path)))
            except OSError:
                continue

        # Process in sorted order (push reversed onto stack for DFS order).
        next_level: list[tuple[Path, int]] = []
        for name, subdir in sorted(subdirs, key=lambda item: item[0]):
            if cancel_event is not None and cancel_event.is_set():
                return
            if name == FAST_AGENT_DIR:
                sessions_dir = subdir / SESSIONS_DIR
                if sessions_dir.is_dir() and not sessions_dir.is_symlink():
                    yield from _walk_session_tree(
                        sessions_dir,
                        resolved_root,
                        on_dir=on_dir,
                        cancel_event=cancel_event,
                    )
                continue
            if name in PRUNED_DIRS:
                continue
            # Direct child session folders (depth 1 under root_dir) are always honored
            # so pointing directly at a sessions folder works without --all.
            if depth == 0 and _is_safe_regular_file(subdir / SESSION_FILE, resolved_root):
                yield from _walk_session_tree(
                    subdir, resolved_root, on_dir=on_dir, cancel_event=cancel_event
                )
                continue
            if name.startswith("."):
                continue
            if depth < search_depth:
                next_level.append((subdir, depth + 1))

        for item in reversed(next_level):
            stack.append(item)


def _read_session(
    session_dir: Path, *, root_dir: Path | None = None
) -> tuple[str, list[str], float | None]:
    """Session ID (falling back to folder name), declared history files, and fallback timestamp."""
    session_id = session_dir.name
    declared: list[str] = []
    allowed_root = root_dir.resolve() if root_dir is not None else session_dir.resolve()
    session_file = session_dir / SESSION_FILE
    if not _is_safe_regular_file(session_file, allowed_root):
        return session_id, declared, None
    try:
        payload = json.loads(session_file.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        return session_id, declared, None
    if not isinstance(payload, dict):
        return session_id, declared, None
    value = payload.get("session_id")
    if isinstance(value, str) and value.strip():
        session_id = value.strip()
    fallback_ts = _parse_iso_timestamp(payload.get("last_activity")) or _parse_iso_timestamp(
        payload.get("created_at")
    )
    continuation = payload.get("continuation")
    agents = continuation.get("agents") if isinstance(continuation, dict) else None
    if isinstance(agents, dict):
        for agent in agents.values():
            history_file = agent.get("history_file") if isinstance(agent, dict) else None
            if isinstance(history_file, str) and history_file.strip():
                declared.append(history_file.strip())
    return session_id, declared, fallback_ts


def _is_active_history_name(name: str) -> bool:
    return name.endswith(".json") and not name.endswith("_previous.json")


def history_files(
    session_dir: Path,
    declared: Sequence[str] = (),
    *,
    root_dir: Path | None = None,
) -> list[Path]:
    """Active history files: declared ones plus ``history_*.json`` (no backups or symlinks)."""
    session_root = session_dir.resolve()
    outer_root = root_dir.resolve() if root_dir is not None else session_root
    found: dict[str, Path] = {}
    for name in declared:
        candidate = Path(name)
        # Only plain file names inside the session folder are trusted.
        if candidate.name != name or not _is_active_history_name(name):
            continue
        path = session_dir / name
        if _is_safe_regular_file(path, session_root) and _is_safe_regular_file(path, outer_root):
            found[name] = path
    for path in sorted(session_dir.glob("history_*.json")):
        if (
            _is_active_history_name(path.name)
            and _is_safe_regular_file(path, session_root)
            and _is_safe_regular_file(path, outer_root)
        ):
            found.setdefault(path.name, path)
    return [found[name] for name in sorted(found)]


# --------------------------------------------------------------------------- parsing


def load_history_messages(path: Path) -> list[Mapping[str, Any]]:
    """Raw messages of a saved history; ``InvalidHistoryError`` if it is not one."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError) as exc:
        raise InvalidHistoryError(f"{path}: {exc}") from exc
    messages = payload.get("messages") if isinstance(payload, dict) else None
    if not isinstance(messages, list):
        raise InvalidHistoryError(f"{path}: no messages list")
    if not all(
        isinstance(message, dict) and message.get("role") in {"user", "assistant"}
        for message in messages
    ):
        raise InvalidHistoryError(f"{path}: messages are not fast-agent messages")
    return messages


def message_attempts(message: Mapping[str, Any]) -> tuple[UsageAttempt, ...]:
    """Provider attempts recorded on one assistant message."""
    if message_role(message) != "assistant":
        return ()
    if _SYNTHETIC_FINAL_CHANNEL in message_channels(message):
        return ()
    attempts: list[UsageAttempt] = []
    for text in channel_texts(message, FAST_AGENT_USAGE):
        try:
            attempts.extend(attempts_from_usage_payload(json.loads(text)))
        except (ValueError, TypeError):
            continue
    return tuple(attempts)


# --------------------------------------------------------------------------- sweep


def sweep_directory(
    root_dir: Path,
    *,
    db_path: Path,
    catalog: PricingCatalog,
    dry_run: bool = False,
    include_all: bool = False,
    max_depth: int | None = None,
    burst_ms: float = 500.0,
    on_progress: Callable[[SweepProgress], None] | None = None,
    cancel_event: threading.Event | None = None,
) -> SweepSummary:
    """Import every session under ``root_dir``; ``dry_run`` only reports what is new."""
    root = Path(root_dir).expanduser()
    if not root.is_dir():
        raise ValueError(f"Not a directory: {root}")
    totals = _Totals()
    seen: set[str] = set()
    resolved_root = root.resolve()
    dirs_scanned = 0
    last_emit = 0.0

    def _emit(phase: str, current_dir: Path, *, force: bool = False) -> None:
        nonlocal last_emit
        if on_progress is None:
            return
        now = time.monotonic()
        if not force and (now - last_emit) < _PROGRESS_INTERVAL_S:
            return
        last_emit = now
        on_progress(
            SweepProgress(
                phase=phase,
                current_dir=str(current_dir),
                dirs_scanned=dirs_scanned,
                sessions_scanned=totals.sessions_scanned,
                files_scanned=totals.files_scanned,
                files_skipped_unchanged=totals.files_skipped_unchanged,
                calls_imported=totals.calls_imported,
            )
        )

    def _on_dir(current_dir: Path) -> None:
        nonlocal dirs_scanned
        dirs_scanned += 1
        _emit("scanning", current_dir)

    _emit("scanning", root, force=True)
    with UsageStore(db_path) as store:
        for session_dir in iter_session_dirs(
            root,
            include_all=include_all,
            max_depth=max_depth,
            on_dir=_on_dir,
            cancel_event=cancel_event,
        ):
            if cancel_event is not None and cancel_event.is_set():
                break
            totals.sessions_scanned += 1
            session_id, declared, fallback_ts = _read_session(session_dir, root_dir=resolved_root)
            for path in history_files(session_dir, declared, root_dir=resolved_root):
                if cancel_event is not None and cancel_event.is_set():
                    break
                totals.files_scanned += 1
                _sweep_file(
                    store,
                    path,
                    root_dir=resolved_root,
                    session_id=session_id,
                    fallback_timestamp=fallback_ts,
                    catalog=catalog,
                    dry_run=dry_run,
                    burst_ms=burst_ms,
                    seen=seen,
                    totals=totals,
                )
                _emit("importing", session_dir)
    _emit("importing", root, force=True)
    return SweepSummary(
        root_dir=str(root),
        dry_run=dry_run,
        sessions_scanned=totals.sessions_scanned,
        files_scanned=totals.files_scanned,
        files_skipped_unchanged=totals.files_skipped_unchanged,
        files_skipped_invalid=totals.files_skipped_invalid,
        calls_imported=totals.calls_imported,
        calls_duplicate=totals.calls_duplicate,
        turns_imported=totals.turns_imported,
        tool_runs_imported=totals.tool_runs_imported,
        tokens_imported=totals.tokens_imported,
        cost_usd_imported=totals.cost_usd_imported,
        unpriced_calls_imported=totals.unpriced_calls_imported,
        include_all=include_all,
    )


def _sweep_file(
    store: UsageStore,
    path: Path,
    *,
    root_dir: Path,
    session_id: str,
    fallback_timestamp: float | None,
    catalog: PricingCatalog,
    dry_run: bool,
    burst_ms: float,
    seen: set[str],
    totals: _Totals,
) -> None:
    try:
        st = path.lstat()
    except OSError:
        totals.files_skipped_invalid += 1
        return
    if not stat_module.S_ISREG(st.st_mode):
        totals.files_skipped_invalid += 1
        return
    try:
        resolved = path.resolve()
    except OSError:
        totals.files_skipped_invalid += 1
        return
    if not resolved.is_relative_to(root_dir):
        totals.files_skipped_invalid += 1
        return
    key = str(resolved)
    if store.file_unchanged(key, st.st_mtime_ns, st.st_size):
        totals.files_skipped_unchanged += 1
        return
    try:
        messages = load_history_messages(path)
    except InvalidHistoryError:
        totals.files_skipped_invalid += 1
        return

    # Parse, hash, and price all turns outside SQLite so write locks stay tiny
    # and dry-run never opens a write transaction.
    prepared = _prepare_turns(
        messages,
        session_id=session_id,
        fallback_timestamp=fallback_timestamp,
        catalog=catalog,
        burst_ms=burst_ms,
    )
    result = store.record_prepared_turns(
        prepared,
        dry_run=dry_run,
        seen=seen,
        swept_file=None if dry_run else (key, st.st_mtime_ns, st.st_size),
    )
    totals.calls_imported += result.calls_inserted
    totals.calls_duplicate += result.calls_duplicate
    totals.turns_imported += result.turns_inserted
    totals.tool_runs_imported += result.tool_runs_inserted
    totals.tokens_imported += result.tokens_inserted
    totals.cost_usd_imported += result.cost_usd_inserted
    totals.unpriced_calls_imported += result.unpriced_inserted


def _prepare_turns(
    messages: Sequence[Mapping[str, Any]],
    *,
    session_id: str,
    fallback_timestamp: float | None,
    catalog: PricingCatalog,
    burst_ms: float,
) -> list[PreparedTurn]:
    prepared: list[PreparedTurn] = []
    for turn in split_user_turns(restore_folds(messages)):
        attempts = tuple(attempt for message in turn for attempt in message_attempts(message))
        record = record_from_history(turn)
        if not attempts and not record.calls:
            continue
        prepared.append(
            prepare_turn(
                session_id=session_id,
                attempts=attempts,
                turn_record=record,
                catalog=catalog,
                source=SWEEP_SOURCE,
                burst_ms=burst_ms,
                timestamp=fallback_timestamp,
            )
        )
    return prepared
