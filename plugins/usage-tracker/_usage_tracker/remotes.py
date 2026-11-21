"""Aggregate usage from other machines.

A *remote* is another machine's ``usage.sqlite3``, reached over SSH (or as a
plain path on a mounted or synced directory).  ``sync`` pulls a consistent
snapshot of it into a private local *mirror* under ``<data_dir>/remotes/``;
reports then read the local ledger and every mirror together (see
``store.open_report_connection``).  Nothing is ever written to the remote, and
the local ledger is never modified: forgetting a remote is deleting one file.

The snapshot is taken on the remote with SQLite's online backup API, so it is
consistent even while fast-agent is recording there, and nothing is transferred
when the remote ledger has not changed since the last sync.
"""

from __future__ import annotations

import contextlib
import gzip
import json
import os
import re
import shlex
import shutil
import socket
import sqlite3
import subprocess
import tempfile
import threading
import time
import zlib
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING

from .models import RemoteStatus
from .store import DB_FILENAME, MAX_REPORT_SOURCES, ReportSource, migrate_mirror

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

REMOTES_DIRNAME = "remotes"
REGISTRY_FILENAME = "remotes.json"
DEFAULT_REMOTE_DB = "~/.fast-agent/usage/" + DB_FILENAME
DEFAULT_SYNC_MINUTES = 30.0
DEFAULT_TIMEOUT_SECONDS = 120.0
DEFAULT_CONNECT_SECONDS = 10
AUTO_TIMEOUT_SECONDS = 45.0  # automatic refresh before a report: fail fast
AUTO_CONNECT_SECONDS = 5
MAX_REMOTES = MAX_REPORT_SOURCES - 1
LOCAL_ALIASES = frozenset({"local", "here", "this"})
_META_PREFIX = "USAGE-TRACKER-META "
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,39}")
_lock = threading.Lock()

# Runs on the remote under whatever Python is there (3.8+), read from stdin.
# stdout: the gzipped snapshot, or nothing when unchanged.  stderr: one META line.
_REMOTE_SCRIPT = r"""
import gzip, json, os, shutil, socket, sqlite3, sys, tempfile

SIGNATURE = __SIGNATURE__

def fingerprint(db):
    return json.dumps(db.execute(SIGNATURE).fetchall(), separators=(",", ":"))

def main():
    path = os.path.expanduser(sys.argv[1])
    known = sys.argv[2] if len(sys.argv) > 2 else ""
    meta = {"host": socket.gethostname()}
    if not os.path.isfile(path):
        meta["error"] = "no usage ledger at " + sys.argv[1]
        return meta
    fd, tmp = tempfile.mkstemp(suffix=".sqlite3")
    os.close(fd)
    try:
        src = sqlite3.connect(path, timeout=10)
        try:
            if known and known == fingerprint(src):
                meta["unchanged"] = True
                meta["fingerprint"] = known
                return meta
            dst = sqlite3.connect(tmp)
            try:
                src.backup(dst)
                # Taken from the snapshot itself, so it describes exactly what was sent.
                meta["fingerprint"] = fingerprint(dst)
            finally:
                dst.close()
        finally:
            src.close()
        meta["size"] = os.path.getsize(tmp)
        out = getattr(sys.stdout, "buffer", sys.stdout)
        with open(tmp, "rb") as raw, gzip.GzipFile(fileobj=out, mode="wb", compresslevel=3) as gz:
            shutil.copyfileobj(raw, gz, 1 << 20)
        out.flush()
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass
    return meta

try:
    result = main()
except Exception as exc:
    result = {"error": "%s: %s" % (type(exc).__name__, exc)}
sys.stderr.write("\n%s%s\n" % (sys.argv[3], json.dumps(result)))
sys.exit(1 if result.get("error") else 0)
"""

# One row of counters per report table: cheap, and it changes whenever rows are
# added, removed or repriced.  Compared instead of file times because reading a
# WAL-mode ledger can itself touch the files.
_SIGNATURE_SQL = " UNION ALL ".join(
    f"SELECT '{table}', COUNT(*), COALESCE(MAX(rowid), 0), {extra} FROM {table}"
    for table, extra in (
        ("calls", "COUNT(cost_usd) || ':' || TOTAL(cost_usd)"),
        ("turns", "''"),
        ("tool_runs", "''"),
        ("ptc_tool_calls", "''"),
    )
)
_REMOTE_SCRIPT = _REMOTE_SCRIPT.replace("__SIGNATURE__", repr(_SIGNATURE_SQL))


def _fingerprint(connection: sqlite3.Connection) -> str:
    return json.dumps(connection.execute(_SIGNATURE_SQL).fetchall(), separators=(",", ":"))


# Non-interactive SSH shells often miss ~/.local/bin (where uv installs live).
_REMOTE_LAUNCHER = (
    'PATH="$HOME/.local/bin:$PATH"; '
    "if command -v python3 >/dev/null 2>&1; then exec python3 - {args}; "
    "elif command -v python >/dev/null 2>&1; then exec python - {args}; "
    "elif command -v uv >/dev/null 2>&1; then exec uv run --no-project -q python - {args}; "
    "else echo 'no python3, python or uv on PATH' >&2; exit 127; fi"
)


@dataclass(frozen=True, slots=True)
class Remote:
    name: str
    target: str  # ssh destination (alias or user@host), or a filesystem path
    kind: str = "ssh"  # "ssh" | "path"
    fallbacks: tuple[str, ...] = ()  # other ssh destinations for the same machine
    db_path: str | None = None  # ledger path on the remote (ssh only)
    host: str | None = None  # hostname the remote reported
    synced_at: float | None = None
    last_attempt_at: float | None = None
    last_error: str | None = None
    fingerprint: str | None = None
    calls: int | None = None


# --------------------------------------------------------------------------- registry


def remotes_dir(data_dir: Path) -> Path:
    return Path(data_dir) / REMOTES_DIRNAME


def mirror_path(data_dir: Path, name: str) -> Path:
    return remotes_dir(data_dir) / f"{name}.sqlite3"


def local_machine_name(config: Mapping[str, object] | None = None) -> str:
    configured = (config or {}).get("machine_name")
    if isinstance(configured, str) and configured.strip():
        return configured.strip()[:40]
    with contextlib.suppress(OSError):
        host = socket.gethostname().split(".", 1)[0].strip()
        if host:
            return host[:40]
    return "local"


def load_remotes(data_dir: Path) -> list[Remote]:
    path = remotes_dir(data_dir) / REGISTRY_FILENAME
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    entries = payload.get("remotes") if isinstance(payload, dict) else None
    remotes: list[Remote] = []
    for entry in entries if isinstance(entries, list) else []:
        if not isinstance(entry, dict):
            continue
        name, target = entry.get("name"), entry.get("target")
        if not isinstance(name, str) or not _NAME.fullmatch(name) or not isinstance(target, str):
            continue
        known = {key: entry[key] for key in Remote.__slots__ if key in entry}
        extra = known.get("fallbacks")
        known["fallbacks"] = (
            tuple(item for item in extra if isinstance(item, str) and _valid_ssh_target(item))
            if isinstance(extra, list)
            else ()
        )
        try:
            remotes.append(Remote(**known))
        except TypeError:
            continue
    return remotes


def _save_remotes(data_dir: Path, remotes: Sequence[Remote]) -> None:
    directory = remotes_dir(data_dir)
    directory.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(OSError):
        directory.chmod(0o700)
    payload = {"schema": 1, "remotes": [asdict(remote) for remote in remotes]}
    path = directory / REGISTRY_FILENAME
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _update_remote(data_dir: Path, remote: Remote) -> None:
    with _lock:
        current = load_remotes(data_dir)
        if not any(item.name == remote.name for item in current):
            return  # removed while a sync was running
        _save_remotes(data_dir, [remote if item.name == remote.name else item for item in current])


def default_name(target: str) -> str:
    """``user@build-box.lan`` -> ``build-box``; ``/mnt/nas/usage.sqlite3`` -> ``nas``."""
    text = target.strip()
    if _is_path(text):
        path = Path(text)
        text = path.parent.name if path.suffix else path.name
    else:
        text = text.rsplit("@", 1)[-1].split(":", 1)[0].split(".", 1)[0]
    text = re.sub(r"[^A-Za-z0-9._-]+", "-", text).strip("-._")
    return text[:40] or "remote"


def _is_path(target: str) -> bool:
    return target.startswith(("/", "~", "./", "../")) or bool(re.match(r"^[A-Za-z]:[\\/]", target))


def _valid_ssh_target(target: str) -> bool:
    """A single ssh destination that can never be read as an ssh option."""
    return bool(target) and not target.startswith("-") and not any(ch.isspace() for ch in target)


def add_remote(
    data_dir: Path,
    target: str,
    *,
    name: str | None = None,
    db_path: str | None = None,
    fallbacks: Sequence[str] = (),
    local_name: str = "local",
) -> Remote:
    """Register a remote (replacing one of the same name); raises ``ValueError``.

    ``fallbacks`` are further SSH destinations for the *same* machine (say its
    VPN address), tried in order when the first cannot be reached.
    """
    target = target.strip()
    if not target:
        raise ValueError("Give an SSH host (as in ~/.ssh/config) or a path to a usage ledger.")
    if not _valid_ssh_target(target):
        raise ValueError(f"Not a valid SSH host or path: {target!r}")
    kind = "path" if _is_path(target) else "ssh"
    others = tuple(dict.fromkeys(item.strip() for item in fallbacks if item.strip() != target))
    for item in others:
        if not _valid_ssh_target(item) or _is_path(item):
            raise ValueError(f"Not a valid fallback SSH host: {item!r}")
    if others and kind == "path":
        raise ValueError("--fallback only applies to SSH remotes.")
    chosen = (name or default_name(target)).strip()
    if not _NAME.fullmatch(chosen):
        raise ValueError(
            f"Invalid remote name {chosen!r}: use letters, digits, '.', '_' or '-' (max 40)."
        )
    if chosen.casefold() in LOCAL_ALIASES or chosen.casefold() == local_name.casefold():
        raise ValueError(f"{chosen!r} names this machine; pick another with --name.")
    if kind == "path" and db_path:
        raise ValueError("--db only applies to SSH remotes; give the ledger path as the target.")
    remote = Remote(
        name=chosen, target=target, kind=kind, fallbacks=others, db_path=db_path or None
    )
    with _lock:
        current = [item for item in load_remotes(data_dir) if item.name != chosen]
        if len(current) >= MAX_REMOTES:
            raise ValueError(f"At most {MAX_REMOTES} remotes can be aggregated.")
        _save_remotes(data_dir, [*current, remote])
    # A replaced remote may point somewhere else: never reuse its mirror.
    with contextlib.suppress(OSError):
        mirror_path(data_dir, chosen).unlink()
    return remote


def remove_remote(data_dir: Path, name: str) -> bool:
    with _lock:
        current = load_remotes(data_dir)
        kept = [item for item in current if item.name.casefold() != name.casefold()]
        if len(kept) == len(current):
            return False
        _save_remotes(data_dir, kept)
        for item in current:
            if item.name.casefold() == name.casefold():
                with contextlib.suppress(OSError):
                    mirror_path(data_dir, item.name).unlink()
    return True


# --------------------------------------------------------------------------- syncing


class _Unreachable(RuntimeError):
    """ssh itself failed (no route, refused, auth): another destination may work."""


def _fetch_ssh(
    remote: Remote, dest: Path, timeout: float, connect_timeout: int = DEFAULT_CONNECT_SECONDS
) -> dict[str, object]:
    """Stream the remote snapshot into ``dest`` (gzipped); returns the remote's META.

    Destinations are tried in order; only a failure to connect moves on to the
    next one (a machine that answered but has no ledger is a definite answer).
    """
    if shutil.which("ssh") is None:
        raise RuntimeError("ssh is not installed")
    deadline = time.monotonic() + timeout
    failure: _Unreachable | None = None
    for target in (remote.target, *remote.fallbacks):
        remaining = deadline - time.monotonic()
        if remaining <= 1:
            break
        try:
            return _fetch_ssh_from(target, remote, dest, remaining, connect_timeout)
        except _Unreachable as exc:
            failure = failure or exc  # report the primary destination's reason
    if failure is None:
        raise RuntimeError(f"timed out after {timeout:.0f}s")
    raise RuntimeError(str(failure))


def _fetch_ssh_from(
    target: str, remote: Remote, dest: Path, timeout: float, connect_timeout: int
) -> dict[str, object]:
    marker = _META_PREFIX.strip() + f"-{os.getpid()}-{time.monotonic_ns()} "
    args = " ".join(
        shlex.quote(part)
        for part in (remote.db_path or DEFAULT_REMOTE_DB, remote.fingerprint or "", marker)
    )
    command = [
        "ssh",
        "-o", "BatchMode=yes",
        "-o", f"ConnectTimeout={max(1, min(connect_timeout, int(timeout)))}",
        "-o", "ConnectionAttempts=1",
        "-o", "ServerAliveInterval=5",
        "-o", "ServerAliveCountMax=3",
        "-T",
        "--",
        target,
        _REMOTE_LAUNCHER.format(args=args),
    ]  # fmt: skip
    with dest.open("wb") as out:
        try:
            proc = subprocess.run(
                command,
                input=_REMOTE_SCRIPT.encode("utf-8"),
                stdout=out,
                stderr=subprocess.PIPE,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired:
            raise _Unreachable(f"timed out after {timeout:.0f}s") from None
    stderr = proc.stderr.decode("utf-8", "replace")
    meta: dict[str, object] | None = None
    for line in stderr.splitlines():
        if line.startswith(marker):
            with contextlib.suppress(ValueError):
                loaded = json.loads(line[len(marker) :])
                if isinstance(loaded, dict):
                    meta = loaded
    if meta is None:
        if proc.returncode == 255:  # ssh's own exit status: the command never ran
            raise _Unreachable(_ssh_failure(stderr, proc.returncode))
        raise RuntimeError(_ssh_failure(stderr, proc.returncode))
    if meta.get("error"):
        raise RuntimeError(str(meta["error"]))
    if proc.returncode != 0:
        raise RuntimeError(_ssh_failure(stderr, proc.returncode))
    return meta


def _ssh_failure(stderr: str, code: int) -> str:
    lines = [line.strip() for line in stderr.splitlines() if line.strip()]
    lines = [line for line in lines if not line.startswith(_META_PREFIX.strip())]
    detail = lines[-1] if lines else f"ssh exited with status {code}"
    detail = re.sub(r"^ssh: ", "", detail)
    return detail[:200]


def _fetch_path(remote: Remote, dest: Path) -> dict[str, object]:
    """Snapshot a ledger on a mounted/synced path into ``dest`` (plain SQLite)."""
    source = Path(remote.target).expanduser()
    if source.is_dir():
        source = source / DB_FILENAME
    if not source.is_file():
        raise RuntimeError(f"no usage ledger at {remote.target}")
    meta: dict[str, object] = {"plain": True}
    try:
        # Read-only: a shared ledger is never written to from here.
        src = sqlite3.connect(f"{source.resolve().as_uri()}?mode=ro", uri=True, timeout=10)
        try:
            if remote.fingerprint and remote.fingerprint == _fingerprint(src):
                meta["unchanged"] = True
                meta["fingerprint"] = remote.fingerprint
                return meta
            dst = sqlite3.connect(str(dest))
            try:
                src.backup(dst)
                meta["fingerprint"] = _fingerprint(dst)
            finally:
                dst.close()
        finally:
            src.close()
    except sqlite3.Error as exc:
        raise RuntimeError(f"could not read ledger: {exc}") from None
    return meta


def sync_remote(
    data_dir: Path,
    remote: Remote,
    *,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    connect_timeout: int = DEFAULT_CONNECT_SECONDS,
) -> RemoteStatus:
    """Refresh ``remote``'s mirror.  Never raises: failures land in ``last_error``.

    The mirror is only replaced by a complete, validated snapshot, so a dropped
    connection or an unreachable machine leaves the previous one in place.
    """
    directory = remotes_dir(data_dir)
    directory.mkdir(parents=True, exist_ok=True)
    mirror = mirror_path(data_dir, remote.name)
    if not mirror.is_file():
        remote = replace(remote, fingerprint=None)  # nothing to be "unchanged" against
    now = time.time()
    changed: bool | None = None
    fd, tmp_name = tempfile.mkstemp(prefix=f".{remote.name}.", suffix=".part", dir=directory)
    os.close(fd)
    download = Path(tmp_name)
    snapshot = download.with_suffix(".sqlite3")
    try:
        if remote.kind == "path":
            meta = _fetch_path(remote, snapshot)
        else:
            meta = _fetch_ssh(remote, download, timeout, connect_timeout)
        host = meta.get("host")
        fingerprint = meta.get("fingerprint")
        updated = replace(
            remote,
            host=host if isinstance(host, str) and host else remote.host,
            last_attempt_at=now,
            synced_at=now,
            last_error=None,
        )
        if meta.get("unchanged"):
            changed = False
        else:
            if not meta.get("plain"):
                with gzip.open(download, "rb") as packed, snapshot.open("wb") as out:
                    shutil.copyfileobj(packed, out, 1 << 20)
            calls = migrate_mirror(snapshot)
            with contextlib.suppress(OSError):
                snapshot.chmod(0o600)
            os.replace(snapshot, mirror)
            for suffix in ("-wal", "-shm"):  # leftovers from an older mirror generation
                with contextlib.suppress(OSError):
                    Path(str(mirror) + suffix).unlink()
            changed = True
            updated = replace(
                updated,
                calls=calls,
                fingerprint=fingerprint if isinstance(fingerprint, str) else None,
            )
    except (OSError, EOFError, RuntimeError, sqlite3.Error, zlib.error) as exc:
        message = str(exc).strip() or type(exc).__name__
        if isinstance(exc, RuntimeError) and "newer than supported" in message:
            message = "remote runs a newer usage-tracker; update this machine's plugin"
        updated = replace(remote, last_attempt_at=now, last_error=message[:200])
    finally:
        for leftover in (download, snapshot):
            with contextlib.suppress(OSError):
                leftover.unlink()
    _update_remote(data_dir, updated)
    return status_of(data_dir, updated, changed=changed)


def status_of(data_dir: Path, remote: Remote, *, changed: bool | None = None) -> RemoteStatus:
    size: int | None = None
    with contextlib.suppress(OSError):
        size = mirror_path(data_dir, remote.name).stat().st_size
    return RemoteStatus(
        name=remote.name,
        target=remote.target,
        kind=remote.kind,
        fallbacks=remote.fallbacks,
        db_path=remote.db_path,
        host=remote.host,
        synced_at=remote.synced_at if size is not None else None,
        last_attempt_at=remote.last_attempt_at,
        last_error=remote.last_error,
        calls=remote.calls if size is not None else None,
        size_bytes=size,
        changed=changed,
    )


def sync_remotes(
    data_dir: Path,
    *,
    names: Sequence[str] = (),
    max_age_seconds: float | None = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    connect_timeout: int = DEFAULT_CONNECT_SECONDS,
    on_progress: Callable[[str], None] | None = None,
) -> list[RemoteStatus]:
    """Sync the named remotes (default: all), in parallel.

    With ``max_age_seconds`` only mirrors older than that are refreshed (and a
    remote that just failed is not retried until the same interval has passed,
    so an unreachable machine cannot slow down every ``/stats``).
    """
    remotes = load_remotes(data_dir)
    wanted = {name.casefold() for name in names}
    if wanted:
        remotes = [remote for remote in remotes if remote.name.casefold() in wanted]
    now = time.time()
    results: dict[str, RemoteStatus] = {}
    due: list[Remote] = []
    for remote in remotes:
        last = max(remote.synced_at or 0.0, remote.last_attempt_at or 0.0)
        fresh = max_age_seconds is not None and now - last < max_age_seconds
        if fresh and (mirror_path(data_dir, remote.name).is_file() or remote.last_error):
            results[remote.name] = status_of(data_dir, remote)
        else:
            due.append(remote)

    def _run(remote: Remote) -> None:
        if on_progress is not None:
            on_progress(remote.name)
        results[remote.name] = sync_remote(
            data_dir, remote, timeout=timeout, connect_timeout=connect_timeout
        )

    threads = [threading.Thread(target=_run, args=(remote,), daemon=True) for remote in due]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    return [results[remote.name] for remote in remotes if remote.name in results]


def needs_sync(data_dir: Path, max_age_seconds: float) -> bool:
    now = time.time()
    for remote in load_remotes(data_dir):
        last = max(remote.synced_at or 0.0, remote.last_attempt_at or 0.0)
        if now - last >= max_age_seconds:
            return True
    return False


# --------------------------------------------------------------------------- reporting


def report_sources(
    data_dir: Path,
    *,
    local_name: str,
    machine: str | None = None,
) -> tuple[list[ReportSource], str | None]:
    """Sources for a report, optionally narrowed to one machine.

    Returns ``(sources, machine_label)``; an empty list means "local ledger
    only, nothing to aggregate".  Raises ``ValueError`` for an unknown machine.
    """
    remotes = load_remotes(data_dir)
    local = ReportSource(local_name)
    mirrors = [
        ReportSource(
            remote.name,
            mirror_path(data_dir, remote.name),
            synced_at=remote.synced_at,
            last_error=remote.last_error,
        )
        for remote in remotes
    ]
    if machine is None:
        return ([local, *mirrors] if mirrors else []), None
    wanted = machine.strip().casefold()
    if wanted in LOCAL_ALIASES or wanted == local_name.casefold():
        return [], (local_name if mirrors else None)
    for source in mirrors:
        if source.name.casefold() == wanted:
            return [source], source.name
    known = ", ".join(["local", *(source.name for source in mirrors)])
    raise ValueError(f"Unknown machine {machine!r}: use one of {known}.")
