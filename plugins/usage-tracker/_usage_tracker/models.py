"""Shared data models for usage-tracker storage, sweep, pricing, and charts."""

from __future__ import annotations

from dataclasses import dataclass

PLUGIN_NAME = "usage-tracker"
PLUGIN_VERSION = "0.2.3"
SUPPORTED_PRICING_SCHEMAS = frozenset({"fast-agent.pricing/v1"})
DEFAULT_PRICING_URL = (
    "https://raw.githubusercontent.com/fast-agent-ai/card-packs/main/"
    "plugins/usage-tracker/pricing_catalog.json"
)


@dataclass(frozen=True, slots=True)
class ModelUsageRow:
    model: str
    provider: str
    calls: int
    input_tokens: int
    uncached_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    output_tokens: int
    reasoning_tokens: int
    cost_usd: float
    priced_calls: int
    unpriced_calls: int
    ttft_p50_ms: float | None = None
    ttft_p90_ms: float | None = None
    tps: float | None = None
    streamed_calls: int = 0
    burst_calls: int = 0

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    @property
    def cache_hit_rate(self) -> float | None:
        if self.input_tokens <= 0:
            return None
        return self.cache_read_tokens / self.input_tokens


@dataclass(frozen=True, slots=True)
class DailyUsagePoint:
    date_iso: str
    total_tokens: int
    input_tokens: int
    output_tokens: int
    cost_usd: float
    calls: int
    sessions: int
    cache_read_tokens: int = 0


@dataclass(frozen=True, slots=True)
class DailyModelPoint:
    """One model's usage on one day (feeds the stacked model-mix charts)."""

    date_iso: str
    model: str
    total_tokens: int
    cost_usd: float
    calls: int


@dataclass(frozen=True, slots=True)
class PeriodTotals:
    """Headline totals for the window immediately before the reported one."""

    total_tokens: int
    cost_usd: float
    calls: int
    sessions: int
    cache_hit_rate: float | None = None


@dataclass(frozen=True, slots=True)
class MachineUsageRow:
    """Usage attributed to one machine (the local ledger or a synced remote)."""

    name: str
    is_local: bool
    calls: int
    total_tokens: int
    cost_usd: float
    sessions: int
    synced_at: float | None = None  # epoch seconds; ``None`` for the local ledger
    last_error: str | None = None


@dataclass(frozen=True, slots=True)
class RemoteStatus:
    """A configured remote ledger and the state of its local mirror."""

    name: str
    target: str
    kind: str  # "ssh" | "path"
    db_path: str | None = None
    fallbacks: tuple[str, ...] = ()
    host: str | None = None
    synced_at: float | None = None
    last_attempt_at: float | None = None
    last_error: str | None = None
    calls: int | None = None
    size_bytes: int | None = None
    changed: bool | None = None  # set by a sync: did the mirror change?


@dataclass(frozen=True, slots=True)
class RhythmHeatmap:
    timezone_label: str
    window_days: int | None
    weekday_labels: tuple[str, ...]
    slot_labels: tuple[str, ...]
    cells: tuple[tuple[int, ...], ...]  # 7 rows (Mon..Sun) x 6 cols (4h slots)
    hourly: tuple[tuple[int, ...], ...] = ()  # 7 rows (Mon..Sun) x 24 cols (hours)


@dataclass(frozen=True, slots=True)
class PhaseBreakdown:
    turns: int
    wall_ms: float
    wait_ms: float
    stream_ms: float
    tools_ms: float
    process_ms: float
    other_ms: float


@dataclass(frozen=True, slots=True)
class ToolUsageRow:
    tool_name: str
    runs: int
    total_ms: float
    avg_ms: float
    max_ms: float
    process_wait_runs: int = 0
    timed_runs: int | None = None
    error_runs: int = 0

    @property
    def effective_timed_runs(self) -> int:
        return self.runs if self.timed_runs is None else self.timed_runs


@dataclass(frozen=True, slots=True)
class PtcModelRow:
    model: str
    cells: int
    nested_calls: int
    pure_python_cells: int
    error_cells: int
    syntax_errors: int = 0
    typecheck_errors: int = 0
    runtime_errors: int = 0


@dataclass(frozen=True, slots=True)
class PtcSummary:
    cells: int
    nested_calls: int
    pure_python_cells: int
    total_cell_ms: float
    error_cells: int = 0
    syntax_errors: int = 0
    typecheck_errors: int = 0
    runtime_errors: int = 0
    nested_error_calls: int = 0
    has_parallel_overlap: bool = False
    unmeasured_nested_calls: int = 0
    by_model: tuple[PtcModelRow, ...] = ()


@dataclass(frozen=True, slots=True)
class ProcessWaitSummary:
    """Wait statistics across blocking process-wait calls (``poll_process``, ``process(wait)``)."""

    waits: int
    timed_waits: int
    total_ms: float
    avg_ms: float | None = None
    p50_ms: float | None = None
    p90_ms: float | None = None
    max_ms: float | None = None
    by_tool: tuple[tuple[str, int], ...] = ()


@dataclass(frozen=True, slots=True)
class UsageReportData:
    window_label: str
    start_date: str | None
    end_date: str | None
    model_filter: str | None
    total_calls: int
    total_sessions: int
    active_days: int
    current_streak_days: int
    longest_streak_days: int
    input_tokens: int
    uncached_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    output_tokens: int
    reasoning_tokens: int
    total_cost_usd: float
    priced_calls: int
    unpriced_calls: int
    models: tuple[ModelUsageRow, ...]
    daily: tuple[DailyUsagePoint, ...]
    rhythm: RhythmHeatmap
    phases: PhaseBreakdown | None = None
    tools: tuple[ToolUsageRow, ...] = ()
    raw_tools: tuple[ToolUsageRow, ...] = ()
    ptc: PtcSummary | None = None
    process_waits: ProcessWaitSummary | None = None
    pricing_version: str | None = None
    upgrade_notice: str | None = None
    daily_models: tuple[DailyModelPoint, ...] = ()
    previous: PeriodTotals | None = None
    machines: tuple[MachineUsageRow, ...] = ()
    machine_filter: str | None = None

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    @property
    def cache_hit_rate(self) -> float | None:
        if self.input_tokens <= 0:
            return None
        return self.cache_read_tokens / self.input_tokens


@dataclass(frozen=True, slots=True)
class SweepProgress:
    phase: str  # "scanning" | "importing"
    current_dir: str
    dirs_scanned: int
    sessions_scanned: int
    files_scanned: int
    files_skipped_unchanged: int
    calls_imported: int


@dataclass(frozen=True, slots=True)
class SweepSummary:
    root_dir: str
    dry_run: bool
    sessions_scanned: int
    files_scanned: int
    files_skipped_unchanged: int
    files_skipped_invalid: int
    calls_imported: int
    calls_duplicate: int
    turns_imported: int
    tool_runs_imported: int
    tokens_imported: int
    cost_usd_imported: float
    unpriced_calls_imported: int
    include_all: bool = False


@dataclass(frozen=True, slots=True)
class PricingUpdateStatus:
    active_version: str
    active_source: str  # "cached" | "bundled"
    bundled_version: str
    cached_version: str | None
    last_checked_at: float | None
    etag: str | None
    upgrade_required: bool
    upgrade_notice: str | None
    auto_update: bool
    pricing_url: str
    last_error: str | None = None
