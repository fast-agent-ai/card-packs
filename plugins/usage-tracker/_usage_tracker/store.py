"""SQLite (WAL) store for model calls, turn phases, and tool runs.

Rows are keyed by deterministic content hashes, so live recording, session sweeps,
resumed sessions, and forked sessions (which copy history) never double-count.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import math
import sqlite3
import threading
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta, tzinfo
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Self

from .models import (
    DailyModelPoint,
    DailyUsagePoint,
    MachineUsageRow,
    ModelUsageRow,
    PeriodTotals,
    PhaseBreakdown,
    ProcessWaitSummary,
    PtcModelRow,
    PtcSummary,
    RhythmHeatmap,
    ToolUsageRow,
    UsageReportData,
)
from .perf import PTC_TOOL_NAME, turn_phases
from .pricing import (
    extract_cache_write_splits,
    normalized_service_tier,
    price_call,
    price_stored_call,
    provider_name,
)
from .pricing_updater import default_usage_dir

if TYPE_CHECKING:
    from collections.abc import Generator, Iterable, Mapping, Sequence

    from .perf import TurnRecord
    from .pricing import Attempt, PricingCatalog

SCHEMA_VERSION = 3
DB_FILENAME = "usage.sqlite3"
UNPRICEABLE = "unpriceable"
"""``pricing_version`` for calls no catalog can price (incomplete or inconsistent usage)."""
WEEKDAY_LABELS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
SLOT_LABELS = ("00", "04", "08", "12", "16", "20")
_SLOT_HOURS = 4

type TzMode = Literal["utc", "local"]

_SCHEMA = """
CREATE TABLE IF NOT EXISTS calls (
    attempt_hash TEXT PRIMARY KEY,
    timestamp_ms INTEGER NOT NULL,
    session_id TEXT,
    provider TEXT NOT NULL,
    upstream_provider TEXT,
    usage_schema TEXT,
    model TEXT NOT NULL,
    service_tier TEXT,
    input_tokens INTEGER NOT NULL DEFAULT 0,
    uncached_tokens INTEGER,
    cache_read_tokens INTEGER NOT NULL DEFAULT 0,
    cache_write_tokens INTEGER NOT NULL DEFAULT 0,
    cache_write_1h_tokens INTEGER,
    cache_write_12h_tokens INTEGER,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    reasoning_tokens INTEGER NOT NULL DEFAULT 0,
    tool_calls INTEGER NOT NULL DEFAULT 0,
    cost_usd REAL,
    pricing_version TEXT,
    duration_ms REAL,
    ttft_ms REAL,
    gen_ms REAL,
    is_burst INTEGER NOT NULL DEFAULT 0,
    source TEXT NOT NULL DEFAULT 'live'
);
CREATE TABLE IF NOT EXISTS turns (
    turn_hash TEXT PRIMARY KEY,
    timestamp_ms INTEGER NOT NULL,
    session_id TEXT,
    calls_count INTEGER NOT NULL,
    wall_ms REAL NOT NULL,
    wait_ms REAL NOT NULL,
    stream_ms REAL NOT NULL,
    tools_ms REAL NOT NULL,
    process_ms REAL NOT NULL,
    other_ms REAL NOT NULL,
    source TEXT NOT NULL DEFAULT 'live'
);
CREATE TABLE IF NOT EXISTS tool_runs (
    run_hash TEXT PRIMARY KEY,
    turn_hash TEXT NOT NULL,
    timestamp_ms INTEGER NOT NULL,
    session_id TEXT,
    model TEXT,
    tool_name TEXT NOT NULL,
    duration_ms REAL NOT NULL,
    is_process_wait INTEGER NOT NULL DEFAULT 0,
    is_error INTEGER NOT NULL DEFAULT 0,
    error_kind TEXT,
    nested_calls INTEGER NOT NULL DEFAULT 0,
    has_ptc_meta INTEGER NOT NULL DEFAULT 0,
    source TEXT NOT NULL DEFAULT 'live'
);
CREATE TABLE IF NOT EXISTS ptc_tool_calls (
    call_hash TEXT PRIMARY KEY,
    parent_run_hash TEXT NOT NULL,
    turn_hash TEXT NOT NULL,
    timestamp_ms INTEGER NOT NULL,
    session_id TEXT,
    model TEXT,
    tool_name TEXT NOT NULL,
    duration_ms REAL,
    start_offset_ms REAL,
    is_error INTEGER NOT NULL DEFAULT 0,
    is_process_wait INTEGER NOT NULL DEFAULT 0,
    status TEXT,
    source TEXT NOT NULL DEFAULT 'live'
);
CREATE TABLE IF NOT EXISTS swept_files (
    path TEXT PRIMARY KEY,
    mtime_ns INTEGER NOT NULL,
    size INTEGER NOT NULL,
    swept_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_calls_timestamp ON calls(timestamp_ms);
CREATE INDEX IF NOT EXISTS idx_calls_model_timestamp ON calls(model, timestamp_ms);
CREATE INDEX IF NOT EXISTS idx_turns_timestamp ON turns(timestamp_ms);
CREATE INDEX IF NOT EXISTS idx_tool_runs_timestamp ON tool_runs(timestamp_ms);
CREATE INDEX IF NOT EXISTS idx_tool_runs_tool_name ON tool_runs(tool_name);
CREATE INDEX IF NOT EXISTS idx_ptc_calls_timestamp ON ptc_tool_calls(timestamp_ms);
CREATE INDEX IF NOT EXISTS idx_ptc_calls_tool_name ON ptc_tool_calls(tool_name);
CREATE INDEX IF NOT EXISTS idx_ptc_calls_parent ON ptc_tool_calls(parent_run_hash);
CREATE INDEX IF NOT EXISTS idx_calls_unpriced
    ON calls(timestamp_ms)
    WHERE cost_usd IS NULL AND pricing_version IS NULL;
"""

_V3_TOOL_RUN_COLUMNS: tuple[tuple[str, str], ...] = (
    ("model", "TEXT"),
    ("is_error", "INTEGER NOT NULL DEFAULT 0"),
    ("error_kind", "TEXT"),
    ("nested_calls", "INTEGER NOT NULL DEFAULT 0"),
    ("has_ptc_meta", "INTEGER NOT NULL DEFAULT 0"),
)

# Future migrations: SCHEMA_VERSION -> statements upgrading from the previous version.
_MIGRATIONS: dict[int, str] = {
    2: (
        "CREATE INDEX IF NOT EXISTS idx_calls_unpriced "
        "ON calls(timestamp_ms) "
        "WHERE cost_usd IS NULL AND pricing_version IS NULL;"
    ),
    3: (
        "CREATE TABLE IF NOT EXISTS ptc_tool_calls ("
        "    call_hash TEXT PRIMARY KEY,"
        "    parent_run_hash TEXT NOT NULL,"
        "    turn_hash TEXT NOT NULL,"
        "    timestamp_ms INTEGER NOT NULL,"
        "    session_id TEXT,"
        "    model TEXT,"
        "    tool_name TEXT NOT NULL,"
        "    duration_ms REAL,"
        "    start_offset_ms REAL,"
        "    is_error INTEGER NOT NULL DEFAULT 0,"
        "    is_process_wait INTEGER NOT NULL DEFAULT 0,"
        "    status TEXT,"
        "    source TEXT NOT NULL DEFAULT 'live'"
        ");"
        "CREATE INDEX IF NOT EXISTS idx_ptc_calls_timestamp ON ptc_tool_calls(timestamp_ms);"
        "CREATE INDEX IF NOT EXISTS idx_ptc_calls_tool_name ON ptc_tool_calls(tool_name);"
        "CREATE INDEX IF NOT EXISTS idx_ptc_calls_parent ON ptc_tool_calls(parent_run_hash);"
    ),
}


# --------------------------------------------------------------------------- paths


def default_db_path() -> Path:
    return default_usage_dir() / DB_FILENAME


def resolve_db_path(config: Mapping[str, object] | None = None) -> Path:
    """Config ``db_path``, else ``data_dir/usage.sqlite3``, else the default."""
    config = config or {}
    db_path = config.get("db_path")
    if isinstance(db_path, str) and db_path.strip():
        return Path(db_path).expanduser()
    data_dir = config.get("data_dir")
    if isinstance(data_dir, str) and data_dir.strip():
        return Path(data_dir).expanduser() / DB_FILENAME
    return default_db_path()


# --------------------------------------------------------------------------- hashing


def _digest(*parts: object) -> str:
    payload = json.dumps(parts, separators=(",", ":"), ensure_ascii=False, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


def _enum_text(value: object) -> str | None:
    if value is None:
        return None
    text = getattr(value, "value", value)
    return text if isinstance(text, str) else str(text)


def attempt_hash(attempt: Attempt) -> str:
    """Identity of one provider attempt, independent of session and recording path.

    Floats survive the JSON round trip through saved history exactly, so a live
    ``TurnUsage`` and its persisted copy hash identically.
    """
    return _digest(
        "attempt",
        provider_name(attempt),
        attempt.upstream_provider,
        attempt.model,
        float(attempt.timestamp),
        attempt.prompt.total,
        attempt.prompt.uncached,
        attempt.prompt.cache_read,
        attempt.prompt.cache_write,
        attempt.completion.total,
        attempt.completion.reasoning,
    )


def turn_hash(turn_record: TurnRecord) -> str | None:
    """Identity of a turn from its history timing (``None`` without timed calls)."""
    if not turn_record.calls:
        return None
    calls = sorted((call.end, call.duration_ms) for call in turn_record.calls)
    return _digest("turn", calls)


def run_hash(turn: str, index: int, name: str, start: float, duration_ms: float) -> str:
    return _digest("tool", turn, index, name, start, duration_ms)


def ptc_call_hash(
    parent_run: str,
    index: int,
    tool_name: str,
    start_offset_ms: float | None,
    duration_ms: float | None,
) -> str:
    return _digest("ptc_call", parent_run, index, tool_name, start_offset_ms, duration_ms)


# --------------------------------------------------------------------------- connection

_schema_ready: set[str] = set()
_schema_lock = threading.Lock()


def connect(db_path: Path) -> sqlite3.Connection:
    """Open the store (creating or migrating it); autocommit with explicit transactions."""
    db_path = Path(db_path).expanduser()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(str(db_path), timeout=2.0, isolation_level=None)
    try:
        connection.execute("PRAGMA busy_timeout=2000")
        mode_row = connection.execute("PRAGMA journal_mode").fetchone()
        if mode_row is None or str(mode_row[0]).casefold() != "wal":
            connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=NORMAL")
        key = str(db_path.resolve())
        with _schema_lock:
            if key not in _schema_ready or not _has_schema(connection):
                _ensure_schema(connection)
                _schema_ready.add(key)
    except BaseException:
        connection.close()
        raise
    return connection


def _has_schema(connection: sqlite3.Connection) -> bool:
    row = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_meta'"
    ).fetchone()
    return row is not None


def _ensure_schema(connection: sqlite3.Connection) -> None:
    if _has_schema(connection):
        existing = connection.execute(
            "SELECT value FROM schema_meta WHERE key='schema_version'"
        ).fetchone()
        if existing is not None:
            current_version = int(existing[0])
            if current_version == SCHEMA_VERSION:
                return
            if current_version > SCHEMA_VERSION:
                raise RuntimeError(
                    f"usage database schema {current_version} is newer than "
                    f"supported {SCHEMA_VERSION}"
                )
    connection.execute("BEGIN IMMEDIATE")
    try:
        connection.execute(
            "CREATE TABLE IF NOT EXISTS schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        row = connection.execute(
            "SELECT value FROM schema_meta WHERE key='schema_version'"
        ).fetchone()
        version = int(row[0]) if row is not None else 0
        if version > SCHEMA_VERSION:
            raise RuntimeError(
                f"usage database schema {version} is newer than supported {SCHEMA_VERSION}"
            )
        if version == 0:
            for statement in _SCHEMA.split(";"):
                if statement.strip():
                    connection.execute(statement)
        else:
            for target in range(version + 1, SCHEMA_VERSION + 1):
                if target == 3:
                    existing_cols = {
                        row[1]
                        for row in connection.execute("PRAGMA table_info(tool_runs)").fetchall()
                    }
                    for col_name, col_def in _V3_TOOL_RUN_COLUMNS:
                        if col_name not in existing_cols:
                            connection.execute(
                                f"ALTER TABLE tool_runs ADD COLUMN {col_name} {col_def}"
                            )
                for statement in _MIGRATIONS.get(target, "").split(";"):
                    if statement.strip():
                        connection.execute(statement)
        connection.execute(
            "INSERT OR REPLACE INTO schema_meta (key, value) VALUES ('schema_version', ?)",
            (str(SCHEMA_VERSION),),
        )
        connection.execute("COMMIT")
    except BaseException:
        connection.execute("ROLLBACK")
        raise


# --------------------------------------------------------------------------- recording


@dataclass(frozen=True, slots=True)
class RecordResult:
    calls_inserted: int = 0
    calls_duplicate: int = 0
    turns_inserted: int = 0
    tool_runs_inserted: int = 0
    tokens_inserted: int = 0
    cost_usd_inserted: float = 0.0
    unpriced_inserted: int = 0

    def as_tuple(self) -> tuple[int, int, int]:
        return self.calls_inserted, self.turns_inserted, self.tool_runs_inserted


@dataclass(frozen=True, slots=True)
class _CallRow:
    attempt_hash: str
    values: tuple[object, ...]
    tokens: int
    cost_usd: float | None


_CALL_COLUMNS = (
    "attempt_hash, timestamp_ms, session_id, provider, upstream_provider, usage_schema, "
    "model, service_tier, input_tokens, uncached_tokens, cache_read_tokens, "
    "cache_write_tokens, cache_write_1h_tokens, cache_write_12h_tokens, output_tokens, "
    "reasoning_tokens, tool_calls, cost_usd, pricing_version, duration_ms, ttft_ms, "
    "gen_ms, is_burst, source"
)
_INSERT_CALL = f"INSERT OR IGNORE INTO calls ({_CALL_COLUMNS}) VALUES ({', '.join('?' * 24)})"
_INSERT_TURN = (
    "INSERT OR IGNORE INTO turns (turn_hash, timestamp_ms, session_id, calls_count, "
    "wall_ms, wait_ms, stream_ms, tools_ms, process_ms, other_ms, source) "
    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
)
_INSERT_TOOL_RUN = (
    "INSERT OR IGNORE INTO tool_runs (run_hash, turn_hash, timestamp_ms, session_id, model, "
    "tool_name, duration_ms, is_process_wait, is_error, error_kind, nested_calls, "
    "has_ptc_meta, source) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
)
_INSERT_PTC_CALL = (
    "INSERT OR IGNORE INTO ptc_tool_calls (call_hash, parent_run_hash, turn_hash, timestamp_ms, "
    "session_id, model, tool_name, duration_ms, start_offset_ms, is_error, is_process_wait, "
    "status, source) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
)
_BACKFILL_CALL_SESSION = (
    "UPDATE calls SET session_id = ? WHERE attempt_hash = ? AND session_id IS NULL"
)
_BACKFILL_TURN_SESSION = (
    "UPDATE turns SET session_id = ? WHERE turn_hash = ? AND session_id IS NULL"
)
_BACKFILL_TOOL_SESSION = (
    "UPDATE tool_runs SET session_id = ? WHERE run_hash = ? AND session_id IS NULL"
)
_BACKFILL_PTC_CALL_SESSION = (
    "UPDATE ptc_tool_calls SET session_id = ? WHERE call_hash = ? AND session_id IS NULL"
)


@dataclass(frozen=True, slots=True)
class PreparedTurn:
    """Pre-computed rows for one turn, ready for a short write transaction."""

    session_id: str | None
    call_rows: tuple[_CallRow, ...]
    turn_row: tuple[object, ...] | None
    tool_rows: tuple[tuple[object, ...], ...]
    ptc_rows: tuple[tuple[object, ...], ...] = ()


def _call_row(
    attempt: Attempt,
    *,
    session_id: str | None,
    catalog: PricingCatalog,
    source: str,
    burst_ms: float,
) -> _CallRow:
    key = attempt_hash(attempt)
    pricing = price_call(attempt, catalog=catalog)
    if pricing.cost_usd is not None:
        version = pricing.pricing_version
    else:
        version = None if pricing.repriceable else UNPRICEABLE
    one_hour, twelve_hour = extract_cache_write_splits(attempt)
    timing = attempt.timing
    duration_ms = float(timing.duration_ms) if timing is not None else None
    ttft_ms = float(timing.ttft_ms) if timing is not None and timing.ttft_ms is not None else None
    gen_ms = (
        max(0.0, duration_ms - ttft_ms) if duration_ms is not None and ttft_ms is not None else None
    )
    is_burst = int(gen_ms is not None and gen_ms < burst_ms)
    input_tokens = attempt.prompt.total or 0
    output_tokens = attempt.completion.total or 0
    values = (
        key,
        round(float(attempt.timestamp) * 1000),
        session_id,
        provider_name(attempt),
        attempt.upstream_provider,
        _enum_text(attempt.usage_schema),
        attempt.model,
        normalized_service_tier(attempt),
        input_tokens,
        attempt.prompt.uncached,
        attempt.prompt.cache_read or 0,
        attempt.prompt.cache_write or 0,
        one_hour,
        twelve_hour,
        output_tokens,
        attempt.completion.reasoning or 0,
        attempt.tool_calls or 0,
        pricing.cost_usd,
        version,
        duration_ms,
        ttft_ms,
        gen_ms,
        is_burst,
        source,
    )
    return _CallRow(key, values, input_tokens + output_tokens, pricing.cost_usd)


def _turn_rows(
    turn_record: TurnRecord | None,
    attempts: Sequence[Attempt],
    *,
    session_id: str | None,
    source: str,
    burst_ms: float,
    timestamp: float | None,
) -> tuple[tuple[Any, ...] | None, list[tuple[Any, ...]], list[tuple[Any, ...]]]:
    if turn_record is None or not turn_record.calls:
        return None, [], []
    key = turn_hash(turn_record)
    assert key is not None
    phases = turn_phases(turn_record, burst_ms)
    mono_start = min(call.start for call in turn_record.calls)

    # History timing uses a monotonic clock; anchor it to wall-clock attempt times.
    starts = [
        float(attempt.timestamp)
        - (attempt.timing.duration_ms / 1000 if attempt.timing is not None else 0.0)
        for attempt in attempts
    ]
    if starts:
        epoch_start = min(starts)
    elif timestamp is not None:
        epoch_start = timestamp
    elif source == "live":
        epoch_start = time.time()
    else:
        # Never fabricate "now" for undated historical turns during a sweep.
        return None, [], []
    turn_ms = round(epoch_start * 1000)
    turn_row = (
        key,
        turn_ms,
        session_id,
        len(turn_record.calls),
        sum(phases.values()),
        phases["wait"],
        phases["stream"],
        phases["tools"],
        phases["process"],
        phases["other"],
        source,
    )
    attempt_models = {attempt.model for attempt in attempts if attempt.model}
    fallback_model = next(iter(attempt_models)) if len(attempt_models) == 1 else None

    tool_rows: list[tuple[Any, ...]] = []
    ptc_rows: list[tuple[Any, ...]] = []
    for index, tool in enumerate(turn_record.tools):
        parent_hash = run_hash(key, index, tool.name, tool.start, tool.duration_ms)
        tool_ts = turn_ms + max(0, round((tool.start - mono_start) * 1000))
        tool_model = tool.model or fallback_model
        tool_rows.append(
            (
                parent_hash,
                key,
                tool_ts,
                session_id,
                tool_model,
                tool.name,
                tool.duration_ms,
                int(tool.process_wait),
                int(tool.is_error),
                tool.error_kind,
                len(tool.nested_calls),
                int(tool.has_ptc_meta),
                source,
            )
        )
        for nested_index, nested in enumerate(tool.nested_calls):
            offset_ms = round(nested.start_offset_ms) if nested.start_offset_ms is not None else 0
            ptc_rows.append(
                (
                    ptc_call_hash(
                        parent_hash,
                        nested_index,
                        nested.tool_name,
                        nested.start_offset_ms,
                        nested.duration_ms,
                    ),
                    parent_hash,
                    key,
                    tool_ts + max(0, offset_ms),
                    session_id,
                    tool_model,
                    nested.tool_name,
                    nested.duration_ms,
                    nested.start_offset_ms,
                    int(nested.is_error),
                    int(nested.process_wait),
                    nested.status,
                    source,
                )
            )
    return turn_row, tool_rows, ptc_rows


def prepare_turn(
    *,
    session_id: str | None,
    attempts: Iterable[Attempt],
    turn_record: TurnRecord | None,
    catalog: PricingCatalog,
    source: str = "live",
    burst_ms: float = 500.0,
    timestamp: float | None = None,
) -> PreparedTurn:
    """Compute hashed/priced rows in Python without holding a SQLite lock."""
    attempt_tuple = tuple(attempts)
    call_rows = tuple(
        _call_row(
            attempt,
            session_id=session_id,
            catalog=catalog,
            source=source,
            burst_ms=burst_ms,
        )
        for attempt in attempt_tuple
    )
    turn_row, tool_rows, ptc_rows = _turn_rows(
        turn_record,
        attempt_tuple,
        session_id=session_id,
        source=source,
        burst_ms=burst_ms,
        timestamp=timestamp,
    )
    return PreparedTurn(
        session_id=session_id,
        call_rows=call_rows,
        turn_row=turn_row,
        tool_rows=tuple(tool_rows),
        ptc_rows=tuple(ptc_rows),
    )


class UsageStore:
    """One connection to the usage database; use as a context manager."""

    def __init__(self, db_path: Path | None = None) -> None:
        self.db_path = Path(db_path) if db_path is not None else default_db_path()
        self.connection = connect(self.db_path)

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def close(self) -> None:
        self.connection.close()

    @contextlib.contextmanager
    def transaction(self) -> Generator[None]:
        """One write transaction; joins an enclosing one instead of nesting."""
        connection = self.connection
        if connection.in_transaction:
            yield
            return
        connection.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            connection.execute("ROLLBACK")
            raise
        connection.execute("COMMIT")

    # ----------------------------------------------------------------- writes

    def record_turn(
        self,
        *,
        session_id: str | None,
        attempts: Iterable[Attempt],
        turn_record: TurnRecord | None,
        catalog: PricingCatalog,
        source: str = "live",
        burst_ms: float = 500.0,
        timestamp: float | None = None,
        dry_run: bool = False,
        seen: set[str] | None = None,
    ) -> RecordResult:
        """Write a turn's calls, phases, and tool runs in one transaction.

        ``dry_run`` reports what would be inserted without writing; pass a shared
        ``seen`` set to also deduplicate across turns within one dry run.
        """
        prepared = prepare_turn(
            session_id=session_id,
            attempts=attempts,
            turn_record=turn_record,
            catalog=catalog,
            source=source,
            burst_ms=burst_ms,
            timestamp=timestamp,
        )
        return self.record_prepared_turns((prepared,), dry_run=dry_run, seen=seen)

    def record_prepared_turns(
        self,
        turns: Sequence[PreparedTurn],
        *,
        dry_run: bool = False,
        seen: set[str] | None = None,
        swept_file: tuple[str, int, int] | None = None,
    ) -> RecordResult:
        """Commit (or dry-run plan) pre-computed turns with minimal write-lock hold."""
        if dry_run:
            shared_seen = seen if seen is not None else set()
            inserted_calls: list[_CallRow] = []
            total_calls = turns_inserted = tools_inserted = 0
            for turn in turns:
                total_calls += len(turn.call_rows)
                for row in turn.call_rows:
                    if row.attempt_hash not in shared_seen and not self._exists(
                        "calls", "attempt_hash", row.attempt_hash
                    ):
                        inserted_calls.append(row)
                    shared_seen.add(row.attempt_hash)
                if turn.turn_row is not None:
                    key = f"turn:{turn.turn_row[0]}"
                    if key not in shared_seen and not self._exists(
                        "turns", "turn_hash", str(turn.turn_row[0])
                    ):
                        turns_inserted += 1
                    shared_seen.add(key)
                for tool_row in turn.tool_rows:
                    key = f"tool:{tool_row[0]}"
                    if key not in shared_seen and not self._exists(
                        "tool_runs", "run_hash", str(tool_row[0])
                    ):
                        tools_inserted += 1
                    shared_seen.add(key)
            return _result(inserted_calls, total_calls, turns_inserted, tools_inserted)

        inserted_calls = []
        total_calls = turns_inserted = tools_inserted = 0
        connection = self.connection
        with self.transaction():
            for turn in turns:
                total_calls += len(turn.call_rows)
                sid = turn.session_id
                for row in turn.call_rows:
                    if connection.execute(_INSERT_CALL, row.values).rowcount:
                        inserted_calls.append(row)
                    elif sid is not None:
                        connection.execute(_BACKFILL_CALL_SESSION, (sid, row.attempt_hash))
                if turn.turn_row is not None:
                    if connection.execute(_INSERT_TURN, turn.turn_row).rowcount:
                        turns_inserted += 1
                    elif sid is not None:
                        connection.execute(_BACKFILL_TURN_SESSION, (sid, turn.turn_row[0]))
                for tool_row in turn.tool_rows:
                    if connection.execute(_INSERT_TOOL_RUN, tool_row).rowcount:
                        tools_inserted += 1
                    elif sid is not None:
                        connection.execute(_BACKFILL_TOOL_SESSION, (sid, tool_row[0]))
                for ptc_row in turn.ptc_rows:
                    inserted = connection.execute(_INSERT_PTC_CALL, ptc_row).rowcount
                    if not inserted and sid is not None:
                        connection.execute(_BACKFILL_PTC_CALL_SESSION, (sid, ptc_row[0]))
            if swept_file is not None:
                self.mark_swept(*swept_file)
        if seen is not None:
            for turn in turns:
                seen.update(row.attempt_hash for row in turn.call_rows)
        return _result(inserted_calls, total_calls, turns_inserted, tools_inserted)

    def _exists(self, table: str, column: str, key: str) -> bool:
        query = f"SELECT 1 FROM {table} WHERE {column} = ?"
        return self.connection.execute(query, (key,)).fetchone() is not None

    def file_unchanged(self, path: str, mtime_ns: int, size: int) -> bool:
        row = self.connection.execute(
            "SELECT mtime_ns, size FROM swept_files WHERE path = ?", (path,)
        ).fetchone()
        return row is not None and row[0] == mtime_ns and row[1] == size

    def mark_swept(self, path: str, mtime_ns: int, size: int) -> None:
        self.connection.execute(
            "INSERT OR REPLACE INTO swept_files (path, mtime_ns, size, swept_at) "
            "VALUES (?, ?, ?, ?)",
            (path, mtime_ns, size, int(time.time() * 1000)),
        )

    def has_unpriced(self) -> bool:
        row = self.connection.execute(
            "SELECT 1 FROM calls WHERE cost_usd IS NULL AND pricing_version IS NULL LIMIT 1"
        ).fetchone()
        return row is not None

    def unpriced_count(self) -> int:
        row = self.connection.execute(
            "SELECT COUNT(*) FROM calls WHERE cost_usd IS NULL AND pricing_version IS NULL"
        ).fetchone()
        return int(row[0])

    def reprice_unpriced(self, catalog: PricingCatalog) -> int:
        """Price calls that lacked a matching rate when recorded; returns rows updated."""
        connection = self.connection
        rows = connection.execute(
            "SELECT attempt_hash, provider, upstream_provider, usage_schema, model, "
            "service_tier, timestamp_ms, input_tokens, uncached_tokens, "
            "cache_read_tokens, cache_write_tokens, cache_write_1h_tokens, "
            "cache_write_12h_tokens, output_tokens FROM calls "
            "WHERE cost_usd IS NULL AND pricing_version IS NULL"
        ).fetchall()
        updates: list[tuple[float, str, str]] = []
        for row in rows:
            cost = price_stored_call(
                provider=row[1],
                upstream_provider=row[2],
                usage_schema=row[3],
                model=row[4],
                service_tier=row[5],
                timestamp=row[6] / 1000,
                input_tokens=row[7],
                uncached_tokens=row[8],
                cache_read_tokens=row[9],
                cache_write_tokens=row[10],
                cache_write_1h_tokens=row[11],
                cache_write_12h_tokens=row[12],
                output_tokens=row[13],
                catalog=catalog,
            )
            if cost is not None:
                updates.append((cost, catalog.catalog_version, row[0]))
        if not updates:
            return 0
        with self.transaction():
            cursor = connection.executemany(
                "UPDATE calls SET cost_usd = ?, pricing_version = ? "
                "WHERE attempt_hash = ? AND cost_usd IS NULL",
                updates,
            )
            updated = cursor.rowcount
        return updated if updated >= 0 else len(updates)

    # ----------------------------------------------------------------- reads

    def query_report(
        self,
        *,
        window: str = "30d",
        model_filter: str | None = None,
        tz_mode: TzMode = "utc",
        now: datetime | float | None = None,
        pricing_version: str | None = None,
        upgrade_notice: str | None = None,
    ) -> UsageReportData:
        return _query_report(
            self.connection,
            window=window,
            model_filter=model_filter,
            tz_mode=tz_mode,
            now=now,
            pricing_version=pricing_version,
            upgrade_notice=upgrade_notice,
            sources=(),
            machine_filter=None,
        )


def _result(inserted: list[_CallRow], total_calls: int, turns: int, tools: int) -> RecordResult:
    return RecordResult(
        calls_inserted=len(inserted),
        calls_duplicate=total_calls - len(inserted),
        turns_inserted=turns,
        tool_runs_inserted=tools,
        tokens_inserted=sum(row.tokens for row in inserted),
        cost_usd_inserted=sum(row.cost_usd for row in inserted if row.cost_usd is not None),
        unpriced_inserted=sum(row.cost_usd is None for row in inserted),
    )


# --------------------------------------------------------------------------- module API


def record_turn(
    db_path: Path,
    *,
    session_id: str | None,
    attempts: Iterable[Attempt],
    turn_record: TurnRecord | None,
    catalog: PricingCatalog,
    source: str = "live",
    burst_ms: float = 500.0,
) -> tuple[int, int, int]:
    """Record one turn; returns ``(calls_inserted, turns_inserted, tool_runs_inserted)``."""
    with UsageStore(db_path) as store:
        return store.record_turn(
            session_id=session_id,
            attempts=attempts,
            turn_record=turn_record,
            catalog=catalog,
            source=source,
            burst_ms=burst_ms,
        ).as_tuple()


def reprice_unpriced(db_path: Path, catalog: PricingCatalog) -> int:
    with UsageStore(db_path) as store:
        return store.reprice_unpriced(catalog)


def unpriced_count(db_path: Path) -> int:
    """Calls a newer catalog might price (cheap; used to speed up pricing checks)."""
    with UsageStore(db_path) as store:
        return store.unpriced_count()


def query_report(
    db_path: Path,
    *,
    window: str = "30d",
    model_filter: str | None = None,
    tz_mode: TzMode = "utc",
    now: datetime | float | None = None,
    pricing_version: str | None = None,
    upgrade_notice: str | None = None,
    sources: Sequence[ReportSource] = (),
    machine_filter: str | None = None,
) -> UsageReportData:
    """Build a report from the local ledger, optionally merged with mirrored remotes.

    ``sources`` lists the machines to aggregate (see :func:`open_report_connection`).
    With no sources the report covers the local ledger only, exactly as before.
    """
    if not sources:
        with UsageStore(db_path) as store:
            report = store.query_report(
                window=window,
                model_filter=model_filter,
                tz_mode=tz_mode,
                now=now,
                pricing_version=pricing_version,
                upgrade_notice=upgrade_notice,
            )
        return replace(report, machine_filter=machine_filter) if machine_filter else report
    connection, used = open_report_connection(db_path, sources)
    try:
        return _query_report(
            connection,
            window=window,
            model_filter=model_filter,
            tz_mode=tz_mode,
            now=now,
            pricing_version=pricing_version,
            upgrade_notice=upgrade_notice,
            sources=used,
            machine_filter=machine_filter,
        )
    finally:
        connection.close()


# --------------------------------------------------------------------------- aggregation


@dataclass(frozen=True, slots=True)
class ReportSource:
    """One machine's ledger taking part in an aggregated report.

    ``path`` is ``None`` for the local ledger, otherwise a local mirror of a
    remote machine's ``usage.sqlite3`` (see ``remotes.py``).
    """

    name: str
    path: Path | None = None
    synced_at: float | None = None
    last_error: str | None = None


MAX_REPORT_SOURCES = 8  # SQLite attaches at most 10 databases by default
_REPORT_TABLES: tuple[tuple[str, str], ...] = (
    ("calls", "attempt_hash"),
    ("turns", "turn_hash"),
    ("tool_runs", "run_hash"),
    ("ptc_tool_calls", "call_hash"),
)


def migrate_mirror(path: Path) -> int:
    """Bring a mirrored ledger up to this plugin's schema; returns its call count.

    Raises ``RuntimeError`` when the mirror was written by a newer plugin and
    ``sqlite3.DatabaseError`` when the file is not a usable ledger.
    """
    connection = sqlite3.connect(str(path), timeout=2.0, isolation_level=None)
    try:
        if not _has_schema(connection):
            raise sqlite3.DatabaseError("not a usage-tracker ledger")
        _ensure_schema(connection)
        # A mirror is a private single-writer copy: keep it one self-contained file.
        connection.execute("PRAGMA journal_mode=DELETE")
        return int(connection.execute("SELECT COUNT(*) FROM calls").fetchone()[0])
    finally:
        connection.close()


def open_report_connection(
    db_path: Path, sources: Sequence[ReportSource]
) -> tuple[sqlite3.Connection, tuple[ReportSource, ...]]:
    """Open a read connection whose report tables span every usable source.

    Each mirror is attached and the four report tables are shadowed by TEMP
    views that union the sources and add an ``origin`` column.  TEMP objects
    win unqualified name lookups, so the ordinary report SQL runs unchanged.
    Rows are content-hashed, so a session recorded on two machines (a synced or
    shared sessions directory) is counted once, for the first source listing it.

    The connection must only be used for reading.  Returns it together with
    the sources that were usable; unusable mirrors come back with ``last_error``.
    """
    connection = connect(db_path)
    try:
        usable: list[tuple[str, ReportSource]] = []  # (schema, source)
        result: list[ReportSource] = []
        columns = {
            table: [row[1] for row in connection.execute(f"PRAGMA main.table_info({table})")]
            for table, _key in _REPORT_TABLES
        }
        for source in sources:
            if source.path is None:
                usable.append(("main", source))
                result.append(source)
                continue
            if len(usable) >= MAX_REPORT_SOURCES:
                result.append(_source_error(source, "too many machines to aggregate"))
                continue
            schema = f"remote{len(usable)}"
            try:
                if not source.path.is_file():
                    raise FileNotFoundError("not synced yet")
                migrate_mirror(source.path)
                connection.execute(f"ATTACH DATABASE ? AS {schema}", (str(source.path),))
                try:
                    for table, _key in _REPORT_TABLES:
                        have = {
                            row[1]
                            for row in connection.execute(f"PRAGMA {schema}.table_info({table})")
                        }
                        missing = [name for name in columns[table] if name not in have]
                        if missing:
                            raise sqlite3.DatabaseError(f"mirror is missing {table}.{missing[0]}")
                except BaseException:
                    connection.execute(f"DETACH DATABASE {schema}")
                    raise
            except (OSError, RuntimeError, sqlite3.Error) as exc:
                # Why the last sync failed says more than "the mirror is missing".
                reason = source.last_error or str(exc) or type(exc).__name__
                result.append(_source_error(source, reason))
                continue
            usable.append((schema, source))
            result.append(source)

        for table, key in _REPORT_TABLES:
            names = ", ".join(columns[table])
            selects: list[str] = []
            seen: list[str] = []
            for schema, source in usable:
                where = "".join(
                    f" AND {key} NOT IN (SELECT {key} FROM {earlier}.{table})" for earlier in seen
                )
                selects.append(
                    f"SELECT {names}, {_sql_text(source.name)} AS origin "
                    f"FROM {schema}.{table} WHERE 1{where}"
                )
                seen.append(schema)
            if not selects:
                selects.append(f"SELECT {names}, NULL AS origin FROM main.{table} WHERE 0")
            connection.execute(f"CREATE TEMP VIEW {table} AS " + " UNION ALL ".join(selects))
        return connection, tuple(result)
    except BaseException:
        connection.close()
        raise


def _sql_text(value: str) -> str:
    """SQL string literal (views cannot take bound parameters)."""
    return "'" + value.replace("'", "''") + "'"


def _source_error(source: ReportSource, error: str) -> ReportSource:
    return ReportSource(source.name, source.path, source.synced_at, error)


def _has_origin(connection: sqlite3.Connection) -> bool:
    row = connection.execute(
        "SELECT 1 FROM sqlite_temp_master WHERE type='view' AND name='calls'"
    ).fetchone()
    return row is not None


# --------------------------------------------------------------------------- reporting


@dataclass(frozen=True, slots=True)
class Window:
    label: str
    days: int | None = None
    hours: int | None = None

    @property
    def heatmap_days(self) -> int | None:
        if self.days is not None:
            return self.days
        if self.hours is not None:
            return max(1, math.ceil(self.hours / 24))
        return None


def parse_window(window: str) -> Window:
    """``7d``/``30d``/``90d``/``<N>d``, ``24h``/``<N>h``, or ``all``."""
    text = window.strip().casefold()
    if text in {"all", "all-time", "alltime"}:
        return Window("All time")
    unit = text[-1:]
    number = text[:-1]
    if unit in {"d", "h"} and number.isdigit() and int(number) > 0:
        count = int(number)
        if unit == "d":
            return Window(f"Last {count} day{'s' if count != 1 else ''}", days=count)
        return Window(f"Last {count} hour{'s' if count != 1 else ''}", hours=count)
    raise ValueError(f"Unknown window {window!r}: use 7d, 30d, 90d, <N>d, 24h, or all")


def _timezone(tz_mode: TzMode) -> tuple[tzinfo | None, str]:
    if tz_mode == "utc":
        return UTC, "UTC"
    if tz_mode == "local":
        return None, datetime.now().astimezone().tzname() or "local"
    raise ValueError("tz_mode must be 'utc' or 'local'")


def _to_datetime(timestamp_ms: int, tz: tzinfo | None) -> datetime:
    instant = datetime.fromtimestamp(timestamp_ms / 1000, UTC)
    return instant.astimezone(tz) if tz is not None else instant.astimezone()


def _day_start_ms(day: date, tz: tzinfo | None) -> int:
    midnight = datetime(day.year, day.month, day.day, tzinfo=tz)
    if tz is None:
        midnight = midnight.astimezone()  # naive local midnight -> aware local
    return round(midnight.timestamp() * 1000)


def _now(now: datetime | float | None) -> datetime:
    if now is None:
        return datetime.now(UTC)
    if isinstance(now, datetime):
        return now if now.tzinfo is not None else now.astimezone()
    return datetime.fromtimestamp(now, UTC)


def _percentile(values: list[float], quantile: float) -> float | None:
    """Nearest-rank percentile, matching ``Perf.ttft``."""
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(0, math.ceil(quantile * len(ordered)) - 1)]


def _streaks(active: set[date], start: date, today: date) -> tuple[int, int]:
    """``(current, longest)`` runs of consecutive active days within ``start..today``.

    Today without activity yet does not break the current streak.
    """
    day = today if today in active else today - timedelta(days=1)
    current = 0
    while day in active and day >= start:
        current += 1
        day -= timedelta(days=1)
    longest = run = 0
    previous: date | None = None
    for day in sorted(day for day in active if start <= day <= today):
        run = run + 1 if previous is not None and day - previous == timedelta(days=1) else 1
        longest = max(longest, run)
        previous = day
    return current, longest


def _query_report(
    connection: sqlite3.Connection,
    *,
    window: str,
    model_filter: str | None,
    tz_mode: TzMode,
    now: datetime | float | None,
    pricing_version: str | None,
    upgrade_notice: str | None,
    sources: Sequence[ReportSource] = (),
    machine_filter: str | None = None,
) -> UsageReportData:
    spec = parse_window(window)
    tz, tz_label = _timezone(tz_mode)
    current = _now(now)
    current_local = current.astimezone(tz) if tz is not None else current.astimezone()
    today = current_local.date()
    start_ms: int | None
    if spec.days is not None:
        start_day: date | None = today - timedelta(days=spec.days - 1)
        start_ms = _day_start_ms(start_day, tz)
    elif spec.hours is not None:
        start_ms = round(current.timestamp() * 1000) - spec.hours * 3_600_000
        start_day = _to_datetime(start_ms, tz).date()
    else:
        start_day, start_ms = None, None
    lower = start_ms if start_ms is not None else -(2**62)
    needle = model_filter.strip().casefold() if model_filter and model_filter.strip() else None

    origin = "origin" if _has_origin(connection) else "NULL"
    rows = connection.execute(
        "SELECT timestamp_ms, session_id, provider, model, input_tokens, uncached_tokens, "
        "cache_read_tokens, cache_write_tokens, output_tokens, reasoning_tokens, cost_usd, "
        f"ttft_ms, gen_ms, is_burst, {origin} FROM calls WHERE timestamp_ms >= ? "
        "ORDER BY timestamp_ms",
        (lower,),
    ).fetchall()
    if needle is not None:
        rows = [row for row in rows if needle in row[3].casefold()]

    by_model: dict[str, list[tuple]] = defaultdict(list)
    by_origin: dict[str, list[tuple]] = defaultdict(list)
    daily_calls: dict[date, list[tuple]] = defaultdict(list)
    daily_model_calls: dict[tuple[date, str], list[tuple]] = defaultdict(list)
    cells = [[0] * len(SLOT_LABELS) for _ in WEEKDAY_LABELS]
    hourly = [[0] * 24 for _ in WEEKDAY_LABELS]
    for row in rows:
        by_model[row[3]].append(row)
        if row[14] is not None:
            by_origin[row[14]].append(row)
        moment = _to_datetime(row[0], tz)
        daily_calls[moment.date()].append(row)
        daily_model_calls[(moment.date(), row[3])].append(row)
        cells[moment.weekday()][moment.hour // _SLOT_HOURS] += 1
        hourly[moment.weekday()][moment.hour] += 1

    models = sorted(
        (_model_row(model, model_rows) for model, model_rows in by_model.items()),
        key=lambda item: (-item.total_tokens, item.model),
    )

    if start_day is not None:
        first_day, last_day = start_day, today
    elif daily_calls:
        first_day, last_day = min(daily_calls), max(daily_calls)
    else:
        first_day = last_day = None
    daily: list[DailyUsagePoint] = []
    if first_day is not None and last_day is not None:
        day = first_day
        while day <= last_day:
            day_rows = daily_calls.get(day, [])
            input_tokens = sum(row[4] for row in day_rows)
            output_tokens = sum(row[8] for row in day_rows)
            daily.append(
                DailyUsagePoint(
                    date_iso=day.isoformat(),
                    total_tokens=input_tokens + output_tokens,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    cost_usd=sum(row[10] for row in day_rows if row[10] is not None),
                    calls=len(day_rows),
                    sessions=len({row[1] for row in day_rows if row[1] is not None}),
                    cache_read_tokens=sum(row[6] for row in day_rows),
                )
            )
            day += timedelta(days=1)
    daily_models = tuple(
        DailyModelPoint(
            date_iso=day.isoformat(),
            model=model,
            total_tokens=sum(row[4] + row[8] for row in model_rows),
            cost_usd=sum(row[10] for row in model_rows if row[10] is not None),
            calls=len(model_rows),
        )
        for (day, model), model_rows in sorted(daily_model_calls.items())
    )
    machines = tuple(
        MachineUsageRow(
            name=source.name,
            is_local=source.path is None,
            calls=len(origin_rows),
            total_tokens=sum(row[4] + row[8] for row in origin_rows),
            cost_usd=sum(row[10] for row in origin_rows if row[10] is not None),
            sessions=len({row[1] for row in origin_rows if row[1] is not None}),
            synced_at=source.synced_at,
            last_error=source.last_error,
        )
        for source in sources
        for origin_rows in (by_origin.get(source.name, []),)
    )
    previous = _previous_totals(connection, spec, lower, needle)
    active = set(daily_calls)
    current_streak, longest_streak = _streaks(
        active, first_day if first_day is not None else today, today
    )

    phases: PhaseBreakdown | None = None
    if needle is None:
        # Turns are not attributed to models, so phase breakdowns are only
        # reported unfiltered.
        phases = _phase_breakdown(connection, lower)
    tools, raw_tools, ptc, process_waits = _tool_views_and_ptc(connection, lower, needle)

    priced = [row[10] for row in rows if row[10] is not None]
    return UsageReportData(
        window_label=spec.label,
        start_date=first_day.isoformat() if first_day is not None else None,
        end_date=last_day.isoformat() if last_day is not None else None,
        model_filter=model_filter.strip() if needle is not None and model_filter else None,
        total_calls=len(rows),
        total_sessions=len({row[1] for row in rows if row[1] is not None}),
        active_days=len(active),
        current_streak_days=current_streak,
        longest_streak_days=longest_streak,
        input_tokens=sum(row[4] for row in rows),
        uncached_tokens=sum(_uncached(row) for row in rows),
        cache_read_tokens=sum(row[6] for row in rows),
        cache_write_tokens=sum(row[7] for row in rows),
        output_tokens=sum(row[8] for row in rows),
        reasoning_tokens=sum(row[9] for row in rows),
        total_cost_usd=sum(priced),
        priced_calls=len(priced),
        unpriced_calls=len(rows) - len(priced),
        models=tuple(models),
        daily=tuple(daily),
        rhythm=RhythmHeatmap(
            timezone_label=tz_label,
            window_days=spec.heatmap_days,
            weekday_labels=WEEKDAY_LABELS,
            slot_labels=SLOT_LABELS,
            cells=tuple(tuple(row) for row in cells),
            hourly=tuple(tuple(row) for row in hourly),
        ),
        phases=phases,
        tools=tools,
        raw_tools=raw_tools,
        ptc=ptc,
        process_waits=process_waits,
        pricing_version=pricing_version,
        upgrade_notice=upgrade_notice,
        daily_models=daily_models,
        previous=previous,
        machines=machines if len(sources) > 1 or any(s.path for s in sources) else (),
        machine_filter=machine_filter,
    )


def _previous_totals(
    connection: sqlite3.Connection, spec: Window, lower: int, needle: str | None
) -> PeriodTotals | None:
    """Totals for the equally long window that ended where this one starts."""
    if spec.days is not None:
        span_ms = spec.days * 86_400_000
    elif spec.hours is not None:
        span_ms = spec.hours * 3_600_000
    else:
        return None
    clause, extra = _model_where(needle)
    row = connection.execute(
        "SELECT COUNT(*), TOTAL(input_tokens), TOTAL(output_tokens), TOTAL(cost_usd), "
        "COUNT(DISTINCT session_id), TOTAL(cache_read_tokens) FROM calls "
        f"WHERE timestamp_ms >= ? AND timestamp_ms < ?{clause}",
        (lower - span_ms, lower, *extra),
    ).fetchone()
    if not row or not row[0]:
        return None
    input_tokens = int(row[1])
    return PeriodTotals(
        total_tokens=input_tokens + int(row[2]),
        cost_usd=float(row[3]),
        calls=int(row[0]),
        sessions=int(row[4]),
        cache_hit_rate=(int(row[5]) / input_tokens) if input_tokens > 0 else None,
    )


def _uncached(row: tuple) -> int:
    if row[5] is not None:
        return row[5]
    return max(0, row[4] - row[6] - row[7])


def _model_row(model: str, rows: list[tuple]) -> ModelUsageRow:
    providers = Counter(row[2] for row in rows)
    ttfts = [row[11] for row in rows if row[11] is not None]
    streamed = [row for row in rows if row[11] is not None and row[13] == 0]
    decoding = [row for row in streamed if row[12] is not None and row[12] > 0]
    gen_ms = sum(row[12] for row in decoding)
    tokens = sum(row[8] for row in decoding)
    priced = [row[10] for row in rows if row[10] is not None]
    return ModelUsageRow(
        model=model,
        provider=", ".join(name for name, _count in providers.most_common()),
        calls=len(rows),
        input_tokens=sum(row[4] for row in rows),
        uncached_tokens=sum(_uncached(row) for row in rows),
        cache_read_tokens=sum(row[6] for row in rows),
        cache_write_tokens=sum(row[7] for row in rows),
        output_tokens=sum(row[8] for row in rows),
        reasoning_tokens=sum(row[9] for row in rows),
        cost_usd=sum(priced),
        priced_calls=len(priced),
        unpriced_calls=len(rows) - len(priced),
        ttft_p50_ms=_percentile(ttfts, 0.5),
        ttft_p90_ms=_percentile(ttfts, 0.9),
        tps=tokens / (gen_ms / 1000) if gen_ms > 0 and tokens > 0 else None,
        streamed_calls=len(streamed),
        burst_calls=sum(row[13] == 1 for row in rows),
    )


def _phase_breakdown(connection: sqlite3.Connection, lower: int) -> PhaseBreakdown | None:
    row = connection.execute(
        "SELECT COUNT(*), TOTAL(wall_ms), TOTAL(wait_ms), TOTAL(stream_ms), "
        "TOTAL(tools_ms), TOTAL(process_ms), TOTAL(other_ms) FROM turns "
        "WHERE timestamp_ms >= ?",
        (lower,),
    ).fetchone()
    if not row or not row[0]:
        return None
    return PhaseBreakdown(
        turns=int(row[0]),
        wall_ms=row[1],
        wait_ms=row[2],
        stream_ms=row[3],
        tools_ms=row[4],
        process_ms=row[5],
        other_ms=row[6],
    )


def _model_where(needle: str | None, column: str = "model") -> tuple[str, tuple[object, ...]]:
    if needle is None:
        return "", ()
    return f" AND INSTR(LOWER(COALESCE({column}, '')), ?) > 0", (needle,)


def _raw_tool_rows(
    connection: sqlite3.Connection,
    lower: int,
    needle: str | None,
) -> tuple[ToolUsageRow, ...]:
    clause, extra = _model_where(needle)
    rows = connection.execute(
        "SELECT tool_name, COUNT(*), TOTAL(duration_ms), MAX(duration_ms), "
        "SUM(is_process_wait), SUM(is_error) FROM tool_runs "
        f"WHERE timestamp_ms >= ?{clause} "
        "GROUP BY tool_name ORDER BY TOTAL(duration_ms) DESC, tool_name",
        (lower, *extra),
    ).fetchall()
    return tuple(
        ToolUsageRow(
            tool_name=name,
            runs=int(runs),
            total_ms=total,
            avg_ms=total / runs,
            max_ms=maximum,
            process_wait_runs=int(waits or 0),
            timed_runs=int(runs),
            error_runs=int(errors or 0),
        )
        for name, runs, total, maximum, waits, errors in rows
    )


def _canonical_tool_name(name: str, display_by_key: dict[str, str]) -> str:
    """Case-fold TitleCase harness variants (``Process``/``Bash``) while keeping camelCase."""
    key = name.casefold()
    candidate = name.lower() if name.istitle() else name
    existing = display_by_key.get(key)
    if existing is None:
        display_by_key[key] = candidate
        return key
    if candidate.islower() and not existing.islower():
        display_by_key[key] = candidate
    return key


def _tool_views_and_ptc(
    connection: sqlite3.Connection,
    lower: int,
    needle: str | None,
) -> tuple[
    tuple[ToolUsageRow, ...],
    tuple[ToolUsageRow, ...],
    PtcSummary | None,
    ProcessWaitSummary | None,
]:
    raw_tools = _raw_tool_rows(connection, lower, needle)
    if not raw_tools:
        return (), (), None, None

    clause, extra = _model_where(needle)
    has_ptc = any(row.tool_name == PTC_TOOL_NAME for row in raw_tools)

    # Buckets keyed by casefolded tool name ->
    # [runs, timed_runs, total_ms, max_ms, process_wait_runs, error_runs]
    display_by_key: dict[str, str] = {}
    buckets: dict[str, list[float]] = {}
    wait_durations: list[float] = []
    wait_counts_by_key: Counter[str] = Counter()

    def _add_active(
        name: str,
        *,
        runs: int = 1,
        timed: int = 1,
        total_ms: float = 0.0,
        max_ms: float = 0.0,
        errors: int = 0,
    ) -> None:
        key = _canonical_tool_name(name, display_by_key)
        entry = buckets.get(key)
        if entry is None:
            buckets[key] = [
                float(runs),
                float(timed),
                float(total_ms),
                float(max_ms),
                0.0,
                float(errors),
            ]
        else:
            entry[0] += runs
            entry[1] += timed
            entry[2] += total_ms
            if max_ms > entry[3]:
                entry[3] = max_ms
            entry[5] += errors

    def _add_wait(name: str, duration_ms: float | None) -> None:
        key = _canonical_tool_name(name, display_by_key)
        wait_counts_by_key[key] += 1
        if duration_ms is not None:
            wait_durations.append(float(duration_ms))

    # Direct non-PTC tool runs: active runs go to top tools; blocking waits go to process_waits.
    direct_active = connection.execute(
        "SELECT tool_name, COUNT(*), TOTAL(duration_ms), MAX(duration_ms), SUM(is_error) "
        "FROM tool_runs WHERE timestamp_ms >= ? AND tool_name != ? "
        f"AND is_process_wait = 0{clause} GROUP BY tool_name",
        (lower, PTC_TOOL_NAME, *extra),
    ).fetchall()
    for name, runs, total_ms, max_ms, errors in direct_active:
        _add_active(
            name,
            runs=int(runs),
            timed=int(runs),
            total_ms=float(total_ms or 0.0),
            max_ms=float(max_ms or 0.0),
            errors=int(errors or 0),
        )

    direct_waits = connection.execute(
        "SELECT tool_name, duration_ms FROM tool_runs "
        f"WHERE timestamp_ms >= ? AND tool_name != ? AND is_process_wait = 1{clause}",
        (lower, PTC_TOOL_NAME, *extra),
    ).fetchall()
    for name, dur_ms in direct_waits:
        _add_wait(name, float(dur_ms))

    ptc_summary: PtcSummary | None = None
    if has_ptc:
        model_rows_raw = connection.execute(
            "SELECT COALESCE(model, 'unknown'), COUNT(*), SUM(nested_calls), "
            "SUM(CASE WHEN has_ptc_meta = 1 AND nested_calls = 0 AND is_error = 0 "
            "THEN 1 ELSE 0 END), "
            "TOTAL(duration_ms), SUM(is_error), "
            "SUM(CASE WHEN error_kind = 'syntax' THEN 1 ELSE 0 END), "
            "SUM(CASE WHEN error_kind = 'typecheck' THEN 1 ELSE 0 END), "
            "SUM(CASE WHEN error_kind = 'runtime' THEN 1 ELSE 0 END) "
            f"FROM tool_runs WHERE timestamp_ms >= ? AND tool_name = ?{clause} "
            "GROUP BY COALESCE(model, 'unknown') "
            "ORDER BY COUNT(*) DESC, COALESCE(model, 'unknown')",
            (lower, PTC_TOOL_NAME, *extra),
        ).fetchall()

        by_model = tuple(
            PtcModelRow(
                model=model,
                cells=int(cells),
                nested_calls=int(nested or 0),
                pure_python_cells=int(pure_py or 0),
                error_cells=int(errs or 0),
                syntax_errors=int(syn or 0),
                typecheck_errors=int(tc or 0),
                runtime_errors=int(rt or 0),
            )
            for model, cells, nested, pure_py, _ms, errs, syn, tc, rt in model_rows_raw
        )
        total_cells = sum(row.cells for row in by_model)
        total_cell_ms = sum(float(row[4] or 0.0) for row in model_rows_raw)
        total_pure_py = sum(row.pure_python_cells for row in by_model)
        total_error_cells = sum(row.error_cells for row in by_model)
        total_syntax = sum(row.syntax_errors for row in by_model)
        total_typecheck = sum(row.typecheck_errors for row in by_model)
        total_runtime = sum(row.runtime_errors for row in by_model)

        # Surface 0-nested-call pure-Python cells and opaque legacy cells cleanly.
        special_cells = connection.execute(
            "SELECT has_ptc_meta, COUNT(*), TOTAL(duration_ms), MAX(duration_ms) "
            f"FROM tool_runs WHERE timestamp_ms >= ? AND tool_name = ? "
            f"AND nested_calls = 0 AND is_error = 0{clause} "
            "GROUP BY has_ptc_meta",
            (lower, PTC_TOOL_NAME, *extra),
        ).fetchall()
        for has_meta, count, total_ms, max_ms in special_cells:
            if not count:
                continue
            label = "sandbox (python)" if has_meta else f"{PTC_TOOL_NAME} (legacy)"
            _add_active(
                label,
                runs=int(count),
                timed=int(count),
                total_ms=float(total_ms or 0.0),
                max_ms=float(max_ms or 0.0),
            )

        # Fetch all nested PTC calls in window alongside parent cell info for attribution.
        p_clause, p_extra = _model_where(needle, column="p.model")
        nested_rows = connection.execute(
            "SELECT p.parent_run_hash, p.tool_name, p.duration_ms, p.is_error, p.is_process_wait, "
            "r.duration_ms, r.nested_calls, r.is_error "
            "FROM ptc_tool_calls p "
            "LEFT JOIN tool_runs r ON r.run_hash = p.parent_run_hash "
            f"WHERE p.timestamp_ms >= ?{p_clause}",
            (lower, *p_extra),
        ).fetchall()

        nested_error_calls = 0
        unmeasured_nested_calls = 0
        parent_nested_sum: dict[str, tuple[float, float]] = {}
        for (
            parent_hash,
            tool_name,
            dur_ms,
            is_err,
            is_wait,
            parent_ms,
            parent_nested,
            parent_err,
        ) in nested_rows:
            nested_error_calls += int(is_err or 0)
            eff_ms: float | None
            if dur_ms is not None:
                eff_ms = float(dur_ms)
                if parent_ms is not None:
                    prev_sum, prev_wall = parent_nested_sum.get(
                        parent_hash, (0.0, float(parent_ms))
                    )
                    parent_nested_sum[parent_hash] = (prev_sum + eff_ms, prev_wall)
            elif (
                parent_ms is not None and int(parent_nested or 0) == 1 and not int(parent_err or 0)
            ):
                eff_ms = float(parent_ms)
            else:
                eff_ms = None
                unmeasured_nested_calls += 1

            if int(is_wait or 0):
                _add_wait(tool_name, eff_ms)
            else:
                _add_active(
                    tool_name,
                    runs=1,
                    timed=1 if eff_ms is not None else 0,
                    total_ms=eff_ms or 0.0,
                    max_ms=eff_ms or 0.0,
                    errors=int(is_err or 0),
                )

        has_parallel_overlap = any(
            nested_sum > wall_ms * 1.05 and nested_sum - wall_ms > 5.0
            for nested_sum, wall_ms in parent_nested_sum.values()
        )

        ptc_summary = PtcSummary(
            cells=total_cells,
            nested_calls=len(nested_rows),
            pure_python_cells=total_pure_py,
            total_cell_ms=total_cell_ms,
            error_cells=total_error_cells,
            syntax_errors=total_syntax,
            typecheck_errors=total_typecheck,
            runtime_errors=total_runtime,
            nested_error_calls=nested_error_calls,
            has_parallel_overlap=has_parallel_overlap,
            unmeasured_nested_calls=unmeasured_nested_calls,
            by_model=by_model,
        )

    unwrapped = tuple(
        ToolUsageRow(
            tool_name=display_by_key[key],
            runs=int(vals[0]),
            total_ms=vals[2],
            avg_ms=(vals[2] / vals[1]) if vals[1] > 0 else 0.0,
            max_ms=vals[3],
            process_wait_runs=int(vals[4]),
            timed_runs=int(vals[1]),
            error_runs=int(vals[5]),
        )
        for key, vals in sorted(
            buckets.items(),
            key=lambda item: (-item[1][2], -item[1][0], display_by_key[item[0]]),
        )
    )

    process_waits: ProcessWaitSummary | None = None
    total_waits = sum(wait_counts_by_key.values())
    if total_waits > 0:
        timed_waits = len(wait_durations)
        total_wait_ms = sum(wait_durations)
        by_tool = tuple(
            (display_by_key[key], count)
            for key, count in sorted(
                wait_counts_by_key.items(),
                key=lambda item: (-item[1], display_by_key[item[0]]),
            )
        )
        process_waits = ProcessWaitSummary(
            waits=total_waits,
            timed_waits=timed_waits,
            total_ms=total_wait_ms,
            avg_ms=(total_wait_ms / timed_waits) if timed_waits > 0 else None,
            p50_ms=_percentile(wait_durations, 0.5),
            p90_ms=_percentile(wait_durations, 0.9),
            max_ms=max(wait_durations) if timed_waits > 0 else None,
            by_tool=by_tool,
        )

    return unwrapped, raw_tools, ptc_summary, process_waits
