"""Share-worthy, fixed-width terminal cards for the usage-tracker plugin.

Every card is exactly :data:`CARD_WIDTH` visible cells wide.  Lines are built
from segments (see ``viz.py``) whose widths are measured *before* any Rich
markup is added.  The Rich (``color=True``) and plain (``color=False``)
renderings share one layout, cell for cell; they differ only in chart "ink":
colour charts paint solid blocks and tell series apart by hue, plain charts
use textures (``█ ▓ ▒ ░ # = +``), because a share card has no colour.

Colour means the 16 standard ANSI colours only (see ``viz.py``): the cards
take their actual shades from the terminal theme.

Only single-width glyphs are used inside the frame (box drawing, blocks,
``·``, ``▸``, ``…``); dynamic strings (model and tool names, notices) are
sanitised so wide or zero-width characters can never break alignment, and
escaped so Rich never mistakes them for markup.

Privacy: cards never show file paths or session ids (the sweep summary shows
only the last two components of the swept directory).
"""

from __future__ import annotations

import math
import re
import textwrap
import time
import unicodedata
from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING

from . import viz
from .viz import Line, Seg

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

    from .models import (
        DailyUsagePoint,
        ModelUsageRow,
        PhaseBreakdown,
        PricingUpdateStatus,
        ProcessWaitSummary,
        PtcSummary,
        RemoteStatus,
        ServiceTierUsageRow,
        SweepSummary,
        ToolUsageRow,
        UsageReportData,
    )

__all__ = [
    "CARD_WIDTH",
    "BRAND",
    "render_usage_card",
    "render_share_card",
    "render_models_card",
    "group_models",
    "render_rhythm_card",
    "render_perf_card",
    "render_tools_card",
    "render_remotes_card",
    "render_sweep_summary",
    "render_pricing_status",
]

CARD_WIDTH = 68
PAD = 2
CONTENT = CARD_WIDTH - 2 - 2 * PAD  # 62 usable cells between the side borders
SPARK_W = 12  # KPI trend sparkline
BRAND = "fast-agent · /stats"
ELLIPSIS = "…"
SWATCH = "■"
MIX_METRICS = ("tokens", "cost", "calls")

# -- palette -------------------------------------------------------------------
S_BORDER = "dim"
S_DIM = "dim"
S_TITLE = "bold"
S_TOKENS = viz.C_TOKENS
S_TOKENS_BIG = f"bold {viz.C_TOKENS}"
S_BAR = viz.C_TOKENS
S_COST = viz.C_COST
S_COST_BIG = f"bold {viz.C_COST}"
S_CACHE = viz.C_CACHE
S_CACHE_BIG = f"bold {viz.C_CACHE}"
S_SESS = viz.C_SESSIONS
S_SESS_BIG = f"bold {viz.C_SESSIONS}"
S_SPEED = viz.C_SPEED
S_TOOLS = viz.C_TOOLS
S_PROCESS = viz.C_PROCESS
S_WARN = "yellow"
S_ERR = "red"
S_OK = "green"
S_BADGE = "bold magenta"

PHASES: tuple[tuple[str, str, str, str], ...] = (
    # key, label, plain glyph, colour
    ("wait", "wait", "░", viz.C_CACHE),
    ("stream", "stream", "█", "green"),
    ("tools", "tools", "▓", viz.C_TOOLS),
    ("process", "process", "▒", viz.C_PROCESS),
    ("other", "other", ":", viz.C_OTHER),
)


# =============================================================================
# Text sanitising, measuring and markup
# =============================================================================


def _clean(value: object) -> str:
    """Make a dynamic string safe for exact cell alignment (single-width only)."""
    out: list[str] = []
    for ch in unicodedata.normalize("NFC", str(value)):
        if ch in "\t\n\r\v\f":
            out.append(" ")
            continue
        cat = unicodedata.category(ch)
        if cat in ("Cc", "Cf", "Cs", "Co", "Cn", "Mn", "Me", "Zl", "Zp"):
            continue
        if unicodedata.combining(ch):
            continue
        if unicodedata.east_asian_width(ch) in ("W", "F"):
            out.append("?")
            continue
        if cat == "Zs":
            out.append(" ")
            continue
        out.append(ch)
    return "".join(out)


_DATE_SUFFIX = re.compile(r"[-@_](?:20\d{6}|20\d{2}-\d{2}-\d{2})$")


_ORG_PREFIX = re.compile(r"^[A-Za-z0-9_.-]+/(?=[A-Za-z0-9])")


def _display_name(name: str) -> str:
    """Model names lose ``org/`` prefixes and dated snapshot suffixes (``-20250929``)."""
    text = _clean(name).strip()
    short = _DATE_SUFFIX.sub("", _ORG_PREFIX.sub("", text))
    return short or text


def _width(segs: Iterable[Seg]) -> int:
    return sum(len(seg[0]) for seg in segs)


def _plain(seg: Seg) -> str:
    return seg[2] if len(seg) > 2 else seg[0]


def _fit(segs: Sequence[Seg], width: int, align: str = "left") -> Line:
    """Pad or truncate ``segs`` to exactly ``width`` cells."""
    width = max(0, width)
    total = _width(segs)
    if total > width:
        if width == 0:
            return []
        out: Line = []
        room = width - 1
        for seg in segs:
            if room <= 0:
                break
            piece = seg[0][:room]
            if piece:
                out.append((piece, seg[1], seg[2][:room]) if len(seg) > 2 else (piece, seg[1]))
            room -= len(piece)
        # Avoid "name …": drop trailing blanks before the ellipsis.
        while out and not out[-1][0].strip():
            out.pop()
        if out and len(out[-1]) == 2:
            out[-1] = (out[-1][0].rstrip(" "), out[-1][1])
        style = out[-1][1] if out else segs[0][1]
        out.append((ELLIPSIS, style))
        return _fit(out, width, align)
    gap = width - total
    if gap == 0:
        return list(segs)
    if align == "right":
        return [(" " * gap, "")] + list(segs)
    if align == "center":
        left = gap // 2
        return [(" " * left, "")] + list(segs) + [(" " * (gap - left), "")]
    return list(segs) + [(" " * gap, "")]


def _cell(text: str, width: int, style: str = "", align: str = "left") -> Line:
    return _fit([(text, style)], width, align)


def _join(*parts: Sequence[Seg]) -> Line:
    out: Line = []
    for part in parts:
        out.extend(part)
    return out


_TAG_LIKE = re.compile(r"(\\*)(\[[a-z#/@][^[]*?])")
_EMOJI_GUARD = "[i][/i]"


def _escape_chunk(text: str, before_tag: bool) -> str:
    """Escape like ``rich.markup.escape`` plus trailing-backslash safety."""
    text = _TAG_LIKE.sub(lambda m: f"{m.group(1)}{m.group(1)}\\{m.group(2)}", text)
    if before_tag:
        trailing = len(text) - len(text.rstrip("\\"))
        if trailing:
            text += "\\" * trailing
    return text


def _markup_text(text: str, before_tag: bool) -> str:
    if text.count(":") < 2:
        return _escape_chunk(text, before_tag)
    # Rich replaces ``:name:`` emoji codes; an empty tag after each colon splits
    # the text into chunks so no chunk can contain a complete ``:code:``.
    parts = text.split(":")
    pieces = [part + ":" for part in parts[:-1]] + [parts[-1]]
    escaped = [_escape_chunk(piece, True) for piece in pieces[:-1]]
    escaped.append(_escape_chunk(pieces[-1], before_tag))
    return _EMOJI_GUARD.join(escaped)


def _render_line(segs: Sequence[Seg], color: bool) -> str:
    if not color:
        return "".join(_plain(seg) for seg in segs)
    merged: list[list[str]] = []
    for seg in segs:
        text, style = seg[0], seg[1]
        if not text:
            continue
        if merged and merged[-1][1] == style:
            merged[-1][0] += text
        else:
            merged.append([text, style])
    out: list[str] = []
    for index, (text, style) in enumerate(merged):
        if style:
            out.append(f"[{style}]{_markup_text(text, True)}[/]")
        else:
            out.append(_markup_text(text, index + 1 < len(merged)))
    return "".join(out)


# =============================================================================
# Number / date formatting
# =============================================================================


def _compact(value: float) -> str:
    """148_200_000 -> '148.2M'; at most 6 characters for values < 1e18."""
    n = float(value)
    if not math.isfinite(n):
        return "-"
    sign = "-" if n < 0 else ""
    n = abs(n)
    if n < 999.5:
        return f"{sign}{n:.0f}"
    units = ("k", "M", "B", "T", "P", "E")
    index = -1
    while n >= 999.95 and index < len(units) - 1:
        n /= 1000.0
        index += 1
    text = f"{n:.1f}" if n < 999.95 else f"{n:.0f}"
    return f"{sign}{text}{units[index]}"


def _int(value: int) -> str:
    return f"{int(value):,}"


def _count(value: int) -> str:
    """Exact with separators up to 999,999 then compact (fits a 7-cell column)."""
    return _int(value) if abs(int(value)) < 1_000_000 else _compact(value)


def _fmt_cost(value: float) -> str:
    v = float(value)
    if not math.isfinite(v) or v <= 0:
        return "$0.00"
    if v < 0.01:
        return "<$0.01"
    if v < 999.995:
        return f"${v:,.2f}"
    if v < 99_999.5:
        return f"${v:,.0f}"
    return "$" + _compact(v)


def _fmt_pct(fraction: float | None) -> str:
    if fraction is None or not math.isfinite(fraction):
        return "-"
    pct = fraction * 100.0
    if pct <= 0:
        return "0%"
    if pct < 1:
        return "<1%"
    rounded = int(round(pct))
    if rounded >= 100 and fraction < 1:
        rounded = 99
    return f"{min(rounded, 999)}%"


def _fmt_dur(ms: float | None) -> str:
    if ms is None or not math.isfinite(ms):
        return "-"
    ms = max(0.0, float(ms))
    if ms < 999.5:
        return f"{ms:.0f}ms"
    s = ms / 1000.0
    if s < 9.995:
        return f"{s:.2f}s"
    if s < 59.95:
        return f"{s:.1f}s"
    total_s = int(round(s))
    if total_s < 3600:
        return f"{total_s // 60}m {total_s % 60:02d}s"
    total_m = total_s // 60
    if total_m < 24 * 60:
        return f"{total_m // 60}h {total_m % 60:02d}m"
    total_h = total_m // 60
    if total_h < 1000 * 24:
        return f"{total_h // 24}d {total_h % 24:02d}h"
    return f"{_compact(total_h // 24)}d"


def _fmt_tps(tps: float | None) -> str:
    if tps is None or not math.isfinite(tps) or tps < 0:
        return "-"
    if tps < 9.95:
        return f"{tps:.1f} t/s"
    if tps < 999.5:
        return f"{tps:.0f} t/s"
    return f"{_compact(tps)} t/s"


def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(str(value).strip()[:10])
    except ValueError:
        return None


def _fmt_day(day: date, with_year: bool = False) -> str:
    text = f"{day.strftime('%b')} {day.day:02d}"
    return f"{text}, {day.year}" if with_year else text


def _date_range(data: UsageReportData, short: bool = False) -> str:
    start = _parse_date(data.start_date)
    end = _parse_date(data.end_date)
    days = [d for d in (_parse_date(p.date_iso) for p in data.daily) if d]
    if start is None and days:
        start = min(days)
    if end is None and days:
        end = max(days)
    if start is None and end is None:
        return ""
    if start is None or end is None or start == end:
        only = start or end
        assert only is not None
        return _fmt_day(only, with_year=not short)
    if start > end:
        start, end = end, start
    if start.year == end.year:
        return f"{_fmt_day(start)} - {_fmt_day(end, with_year=not short)}"
    if short:
        return f"{start.strftime('%b %Y')} - {end.strftime('%b %Y')}"
    return f"{_fmt_day(start, with_year=True)} - {_fmt_day(end, with_year=True)}"


# =============================================================================
# Geometry helpers
# =============================================================================

_alloc = viz.alloc
_heat_levels = viz.heat_levels


def _bar(fraction: float, width: int, style: str, *, min_one: bool = True) -> Line:
    return viz.line_bar(fraction, width, style, min_one=min_one)


def _stacked(parts: Sequence[tuple[float, str, str]], width: int) -> Line:
    """``parts`` = (value, plain glyph, colour); a dotted track when empty."""
    return viz.stacked_bar([(value, style, glyph) for value, glyph, style in parts], width)


def _swatch(color: str, glyph: str = "█") -> Seg:
    """Legend chip: a coloured square, or the series' texture in plain mode."""
    return (SWATCH, color, glyph)


def _fmt_age(seconds: float | None) -> str:
    if seconds is None or not math.isfinite(seconds):
        return "never"
    seconds = max(0.0, seconds)
    if seconds < 90:
        return "just now"
    if seconds < 5400:
        return f"{round(seconds / 60)}m ago"
    if seconds < 36 * 3600:
        return f"{round(seconds / 3600)}h ago"
    return f"{round(seconds / 86400)}d ago"


# =============================================================================
# Card frame
# =============================================================================


class _Card:
    def __init__(self, color: bool) -> None:
        self.color = color
        self.lines: list[Line] = []

    # -- borders ---------------------------------------------------------------
    def top(self, title: Line | Sequence[Line], badge: Line | None = None) -> None:
        """``╭─ title ───── badge ─╮``; ``title`` may be a list of fallbacks."""
        variants: list[Line] = []
        if title and isinstance(title[0], list):
            for variant in title:
                if isinstance(variant, list):
                    variants.append(list(variant))
        else:
            variants.append([seg for seg in title if isinstance(seg, tuple)])
        avail = CARD_WIDTH - 2 - 3 - 1  # corners, "─ " + " ", min one fill
        badge_line: Line = []
        if badge:
            shortest = min(_width(v) for v in variants)
            max_badge = min(28, max(12, avail - 3 - shortest))
            badge_line = _fit(badge, min(_width(badge), max_badge))
            avail -= _width(badge_line) + 3  # " badge ─" -> one space + " ─"
        chosen = next((v for v in variants if _width(v) <= avail), variants[-1])
        title_line = _fit(chosen, min(_width(chosen), avail))
        fill = (
            CARD_WIDTH - 2 - 3 - _width(title_line) - (_width(badge_line) + 3 if badge_line else 0)
        )
        line: Line = [("╭─ ", S_BORDER), *title_line, (" " + "─" * fill, S_BORDER)]
        if badge_line:
            line += [(" ", ""), *badge_line, (" ─", S_BORDER)]
        line.append(("╮", S_BORDER))
        self.lines.append(line)

    def divider(self, title: str = "", detail: str = "") -> None:
        """Full-width ``├─ title · detail ──┤`` (``detail`` is dimmed)."""
        self.lines.append(_rule("├", "┤", title, CARD_WIDTH, detail))

    def bottom(self, note: Line | None = None) -> None:
        tail: Line = [(" ", S_BORDER), *_brand(), (" · /stats", S_DIM), (" ─╯", S_BORDER)]
        head_len = CARD_WIDTH - _width(tail)
        self.lines.append(_bottom_left(note, head_len) + tail)

    # -- rows ------------------------------------------------------------------
    def row(self, *parts: Sequence[Seg]) -> None:
        content = _fit(_join(*parts), CONTENT)
        self.lines.append(
            [("│", S_BORDER), (" " * PAD, ""), *content, (" " * PAD, ""), ("│", S_BORDER)]
        )

    def blank(self) -> None:
        self.row([])

    def wrapped(self, text: str, style: str = "", prefix: str = "", pstyle: str = "") -> None:
        body = _clean(text)
        indent = " " * len(prefix)
        for i, chunk in enumerate(textwrap.wrap(body, CONTENT - len(prefix)) or [""]):
            self.row([(prefix if i == 0 else indent, pstyle), (chunk, style)])

    def render(self, after: Sequence[Line] = ()) -> str:
        return "\n".join(_render_line(line, self.color) for line in [*self.lines, *after])


def _brand() -> Line:
    return [("fast-agent", viz.C_BRAND)]


def _rule(start: str, end: str, title: str, width: int, detail: str = "") -> Line:
    """``start─ title · detail ────end`` exactly ``width`` cells."""
    inner = width - len(start) - len(end)
    title = _clean(title)
    if title:
        room = inner - 4  # "─ " + " " + at least one "─"
        segs: Line = [(title, S_TITLE)]
        detail = _clean(detail)
        if detail:
            segs.append((f" · {detail}", S_DIM))
        label = _fit(segs, min(_width(segs), max(1, room)))
        fill = inner - 3 - _width(label)
        return [(start + "─ ", S_BORDER), *label, (" " + "─" * fill + end, S_BORDER)]
    return [(start + "─" * inner + end, S_BORDER)]


def _bottom_left(note: Line | None, width: int) -> Line:
    """``╰─ note ────`` exactly ``width`` cells (note dropped if it cannot fit)."""
    if note and _width(note) <= width - 5:
        fill = width - 3 - _width(note) - 1
        return [("╰─ ", S_BORDER), *note, (" " + "─" * fill, S_BORDER)]
    return [("╰" + "─" * (width - 1), S_BORDER)]


def _title(kind: str, data: UsageReportData | None = None) -> list[Line]:
    """Title variants, most to least detailed (the frame picks the first that fits)."""
    base: Line = [*_brand(), (f" {kind}", S_TITLE)]
    if data is None:
        return [base]
    window = _clean(data.window_label).strip()
    variants: list[Line] = []
    for span in (_date_range(data), _date_range(data, short=True), ""):
        detail = " · ".join(part for part in (window, span) if part)
        variants.append(base + [(f" · {detail}", S_DIM)] if detail else list(base))
    return variants


def _filter_badge(data: UsageReportData, extra: str = "") -> Line | None:
    parts = [extra] if extra else []
    if data.model_filter:
        parts.append(_clean(data.model_filter).strip())
    if data.machine_filter:
        parts.append("@" + _clean(data.machine_filter).strip())
    elif len(data.machines) > 1:
        parts.append(f"{len(data.machines)} machines")
    if not parts:
        return None
    return [("▸ ", S_BADGE), (" · ".join(parts), S_BADGE)]


KPI_WIDTHS = (16, 16, 16, 14)


def _kpi_rows(
    items: Sequence[tuple[str, str, str]],
    widths: Sequence[int] = KPI_WIDTHS,
    deltas: Sequence[str] = (),
    sparks: Sequence[Line | None] = (),
) -> list[Line]:
    """Big values over dim labels, optionally with ``▲12%`` deltas and trend sparks."""
    values: Line = []
    labels: Line = []
    trends: Line = []
    for index, ((value, style, label), width) in enumerate(zip(items, widths, strict=True)):
        values += _cell(value, width, style)
        delta = deltas[index] if index < len(deltas) else ""
        color = style.removeprefix("bold ")
        if delta and len(label) + 1 + len(delta) <= width:
            labels += _fit([(label, S_DIM), (" ", ""), (delta, color)], width)
        else:
            labels += _cell(label, width, S_DIM)
        trend = sparks[index] if index < len(sparks) else None
        trends += _fit(trend or [], width)
    rows = [values, labels]
    if any(spark for spark in sparks):
        rows.append(trends)
    return rows


def _delta(current: float | None, previous: float | None, *, points: bool = False) -> str:
    """``▲12%`` / ``▼3%`` against the previous window (``▲2pt`` for rates)."""
    if current is None or previous is None:
        return ""
    if not math.isfinite(current) or not math.isfinite(previous):
        return ""
    if points:
        diff = round((current - previous) * 100)
        if diff == 0:
            return ""
        return f"{'▲' if diff > 0 else '▼'}{abs(diff)}pt"
    if previous <= 0:
        return ""
    change = (current - previous) / previous
    arrow = "▲" if change > 0 else "▼"
    pct = abs(change) * 100
    if pct < 0.5:
        return ""
    if pct >= 999.5:
        return f"{arrow}{current / previous:.0f}x"
    return f"{arrow}{pct:.0f}%"


def _after_notice(notice: str | None) -> list[Line]:
    if not notice:
        return []
    text = _clean(notice).strip()
    lines = textwrap.wrap(text, CARD_WIDTH - 4) or [""]
    out: list[Line] = []
    for i, chunk in enumerate(lines):
        out.append([("  ▸ " if i == 0 else "    ", S_WARN), (chunk, S_DIM)])
    return out


# =============================================================================
# Shared report helpers
# =============================================================================


def _cost_display(data: UsageReportData) -> tuple[str, str]:
    """(text, style) for the headline cost; '~' marks partially-unpriced totals."""
    if data.priced_calls <= 0 and data.total_cost_usd <= 0:
        return "n/a", S_DIM
    text = _fmt_cost(data.total_cost_usd)
    if data.unpriced_calls > 0:
        text = "~" + text
    return text, S_COST_BIG


def _empty_usage_card(data: UsageReportData, color: bool, title_kind: str = "usage") -> str:
    card = _Card(color)
    card.top(_title(title_kind, data), _filter_badge(data))
    card.blank()
    card.row([("No usage recorded yet", S_TITLE), (" for this window.", S_DIM)])
    card.blank()
    card.row([("▸ ", S_TOKENS), ("chat a turn", ""), (" · usage is tracked automatically", S_DIM)])
    card.row([("▸ ", S_TOKENS), ("import history ", ""), ("/stats sweep <dir>", S_TOKENS)])
    card.blank()
    card.bottom()
    return card.render(_after_notice(data.upgrade_notice))


def _daily_points(data: UsageReportData) -> list[tuple[date | None, DailyUsagePoint | None]]:
    """One entry per day of the window, gap days included (``None`` point)."""
    by_day: dict[date, DailyUsagePoint] = {}
    for point in data.daily:
        day = _parse_date(point.date_iso)
        if day is None:  # unparseable dates: show the recorded points as they are
            return [(None, p) for p in data.daily]
        by_day[day] = point
    if not by_day:
        return []
    start, end = min(by_day), max(by_day)
    window_start = _parse_date(data.start_date)
    window_end = _parse_date(data.end_date)
    if window_start is not None and window_start < start:
        start = window_start
    if window_end is not None and window_end > end:
        end = window_end
    span = (end - start).days + 1
    if span > 20_000:  # defensive: absurd ranges fall back to recorded days only
        return [(day, by_day[day]) for day in sorted(by_day)]
    days = (start + timedelta(days=i) for i in range(span))
    return [(day, by_day.get(day)) for day in days]


def _daily_series(data: UsageReportData) -> tuple[list[int], date | None, date | None]:
    """Daily token totals with gap days filled in (when dates are parseable)."""
    points = _daily_points(data)
    series = [max(0, int(p.total_tokens)) if p is not None else 0 for _, p in points]
    if not points:
        return [], None, None
    return series, points[0][0], points[-1][0]


def _phase_total(phases: PhaseBreakdown) -> float:
    return sum(max(0.0, float(getattr(phases, f"{key}_ms") or 0.0)) for key, *_ in PHASES)


def _phase_parts(phases: PhaseBreakdown) -> list[tuple[str, str, str, str, float]]:
    out = []
    for key, label, glyph, style in PHASES:
        value = float(getattr(phases, f"{key}_ms") or 0.0)
        out.append((key, label, glyph, style, max(0.0, value if math.isfinite(value) else 0.0)))
    return out


class _ModelEntry:
    __slots__ = (
        "name", "members", "tokens", "cost", "calls", "priced", "unpriced", "tps", "cache",
        "other",
    )  # fmt: skip

    def __init__(self, rows: Sequence[ModelUsageRow], name: str, other: bool) -> None:
        self.name = name
        self.other = other
        self.members = frozenset(r.model for r in rows)
        self.tokens = sum(r.total_tokens for r in rows)
        self.cost = sum(r.cost_usd for r in rows)
        self.calls = sum(r.calls for r in rows)
        self.priced = sum(r.priced_calls for r in rows)
        self.unpriced = sum(r.unpriced_calls for r in rows)
        weighted = [(r.tps, max(1, r.calls)) for r in rows if r.tps is not None]
        weight = sum(w for _, w in weighted)
        self.tps = sum(t * w for t, w in weighted) / weight if weighted and weight else None
        inp = sum(r.input_tokens for r in rows)
        self.cache = sum(r.cache_read_tokens for r in rows) / inp if inp > 0 else None

    def value(self, metric: str) -> float:
        return float(getattr(self, metric if metric in MIX_METRICS else "tokens"))


def _model_entries(
    data: UsageReportData, max_models: int, metric: str = "tokens"
) -> list[_ModelEntry]:
    def rank(m: ModelUsageRow) -> tuple[float, int, int, str]:
        lead = {"cost": m.cost_usd, "calls": m.calls}.get(metric, m.total_tokens)
        return (-lead, -m.total_tokens, -m.calls, m.model)

    models = sorted(data.models, key=rank)
    max_models = max(1, max_models)
    if len(models) <= max_models:
        return [_ModelEntry([m], m.model, False) for m in models]
    head = models[: max_models - 1]
    tail = models[max_models - 1 :]
    entries = [_ModelEntry([m], m.model, False) for m in head]
    entries.append(_ModelEntry(tail, f"other ({len(tail)})", True))
    return entries


_TOTAL_KEY = "\x00total"


class _Legend:
    """Colour and plain-mode texture for each model shown on a card."""

    def __init__(self, data: UsageReportData, max_models: int, metric: str = "tokens") -> None:
        self.metric = metric if metric in MIX_METRICS else "tokens"
        self.entries = _model_entries(data, max_models, self.metric)
        named = [entry.name for entry in self.entries if not entry.other]
        colors = viz.model_colors(named)
        self.keys: list[str | None] = []  # stack order, bottom to top
        self._paint: dict[str | None, tuple[str, str]] = {
            None: (viz.C_OTHER, viz.PLAIN_OTHER),
            _TOTAL_KEY: (S_BAR, "█"),
        }
        for index, entry in enumerate(self.entries):
            if entry.other:
                self.keys.append(None)
            else:
                glyph = viz.PLAIN_FILLS[index % len(viz.PLAIN_FILLS)]
                self._paint[entry.name] = (colors[entry.name], glyph)
                self.keys.append(entry.name)
        if None not in self.keys:
            self.keys.append(None)  # unlisted models can still appear in daily data

    def key(self, model: str) -> str | None:
        return model if model in self._paint else None

    def paint(self, key: str | None) -> tuple[str, str]:
        return self._paint.get(key, self._paint[None])

    def entry_key(self, entry: _ModelEntry) -> str | None:
        return None if entry.other else entry.name

    def swatch(self, key: str | None) -> Seg:
        color, glyph = self.paint(key)
        return _swatch(color, glyph)


def group_models(data: UsageReportData) -> UsageReportData:
    """Merge models into families (``gemini-4``, ``claude``, ``gpt-6.*``).

    Latency percentiles cannot be merged, so grouped rows carry none: use this
    for the usage and model-mix cards, not the perf card.
    """
    from dataclasses import replace

    from .models import DailyModelPoint
    from .models import ModelUsageRow as Row

    names = [m.model for m in sorted(data.models, key=lambda m: (-m.total_tokens, m.model))]
    names += [p.model for p in data.daily_models if p.model not in names]
    labels = viz.family_labels(list(dict.fromkeys(names)))
    grouped: dict[str, list[ModelUsageRow]] = {}
    for model in data.models:
        grouped.setdefault(labels[model.model], []).append(model)
    rows: list[ModelUsageRow] = []
    for label, members in grouped.items():
        weighted = [(m.tps, max(1, m.calls)) for m in members if m.tps is not None]
        weight = sum(w for _, w in weighted)
        providers = dict.fromkeys(p.strip() for m in members for p in m.provider.split(","))
        rows.append(
            Row(
                model=label,
                provider=", ".join(p for p in providers if p),
                calls=sum(m.calls for m in members),
                input_tokens=sum(m.input_tokens for m in members),
                uncached_tokens=sum(m.uncached_tokens for m in members),
                cache_read_tokens=sum(m.cache_read_tokens for m in members),
                cache_write_tokens=sum(m.cache_write_tokens for m in members),
                output_tokens=sum(m.output_tokens for m in members),
                reasoning_tokens=sum(m.reasoning_tokens for m in members),
                cost_usd=sum(m.cost_usd for m in members),
                priced_calls=sum(m.priced_calls for m in members),
                unpriced_calls=sum(m.unpriced_calls for m in members),
                tps=sum(t * w for t, w in weighted) / weight if weight else None,
                streamed_calls=sum(m.streamed_calls for m in members),
                burst_calls=sum(m.burst_calls for m in members),
            )
        )
    rows.sort(key=lambda m: (-m.total_tokens, m.model))
    days: dict[tuple[str, str], list[float]] = {}
    for point in data.daily_models:
        slot = days.setdefault((point.date_iso, labels[point.model]), [0, 0.0, 0])
        slot[0] += point.total_tokens
        slot[1] += point.cost_usd
        slot[2] += point.calls
    daily = tuple(
        DailyModelPoint(day, label, int(tokens), cost, int(calls))
        for (day, label), (tokens, cost, calls) in sorted(days.items())
    )
    return replace(data, models=tuple(rows), daily_models=daily)


# =============================================================================
# Stacked daily charts
# =============================================================================


class _DailyMix:
    """Per-day values split by model, bucketed to fit a plot width."""

    def __init__(self, data: UsageReportData, legend: _Legend, width: int) -> None:
        points = _daily_points(data)
        metric = legend.metric
        attr = {"cost": "cost_usd", "calls": "calls"}.get(metric, "total_tokens")
        per_day: dict[date, dict[str | None, float]] = {}
        for item in data.daily_models:
            day = _parse_date(item.date_iso)
            if day is None:
                continue
            bucket = per_day.setdefault(day, {})
            key = legend.key(item.model)
            bucket[key] = bucket.get(key, 0.0) + max(0.0, float(getattr(item, attr)))
        columns: list[dict[str | None, float]] = []
        for day, point in points:
            if day is not None and day in per_day:
                columns.append(per_day[day])
            elif point is not None and not data.daily_models:
                # No per-model detail recorded: one series of daily totals.
                columns.append({_TOTAL_KEY: max(0.0, float(getattr(point, attr)))})
            else:
                columns.append({})
        days = [day for day, _ in points]
        self.per = max(1, math.ceil(len(columns) / max(1, width)))
        if self.per > 1:
            merged: list[dict[str | None, float]] = []
            for index in range(0, len(columns), self.per):
                bucket = {}
                for column in columns[index : index + self.per]:
                    for key, value in column.items():
                        bucket[key] = bucket.get(key, 0.0) + value
                merged.append(bucket)
            columns = merged
            days = days[:: self.per]
        self.days = days
        self.columns = columns
        self.totals = [sum(column.values()) for column in columns]
        self.width = width
        self.cols, self.gap = viz.column_layout(len(columns), width)
        self.col = min(self.cols, default=1)  # narrowest column: what a label must fit
        self.legend = legend

    @property
    def plot_width(self) -> int:
        return sum(self.cols) + self.gap * max(0, len(self.cols) - 1)

    @property
    def empty(self) -> bool:
        return not any(total > 0 for total in self.totals)

    def rows(self, height: int, *, share: bool) -> list[Line]:
        order = [_TOTAL_KEY, *self.legend.keys]
        stacks = [
            [(column[key], *self.legend.paint(key)) for key in order if column.get(key, 0) > 0]
            for column in self.columns
        ]
        scale = None if share else max(self.totals, default=0.0)
        return viz.stack_columns(stacks, height, col_width=self.cols, gap=self.gap, scale=scale)

    def peak(self) -> tuple[float, date | None]:
        if self.empty:
            return 0.0, None
        index = max(range(len(self.totals)), key=lambda i: self.totals[i])
        return self.totals[index], self.days[index]

    def axis(self) -> Line:
        """Day labels under the columns (or just the ends when columns are narrow)."""
        days = self.days
        if not days or any(day is None for day in days):
            return [(f"{len(self.columns)} days", S_DIM)]
        step = self.col + self.gap
        if self.col >= 6:
            labels = [f"{day:%a} {day.day:02d}" for day in days if day is not None]
        elif step >= 3:
            labels = [f"{day.day:02d}" for day in days if day is not None]
        else:
            first, last = days[0], days[-1]
            assert first is not None and last is not None
            long_range = (last - first).days > 330
            fmt = (lambda d: d.strftime("%b %Y")) if long_range else _fmt_day
            left, right = fmt(first), fmt(last)
            room = self.plot_width - len(left) - len(right)
            if first == last or room < 1:
                return [(left, S_DIM)]
            return [(left, S_DIM), (" " * room, ""), (right, S_DIM)]
        line: Line = []
        for index, label in enumerate(labels):
            line += _cell(label, self.col, S_DIM, "center")
            if self.gap and index + 1 < len(labels):
                line.append((" " * self.gap, ""))
        return line

    def totals_row(self, fmt) -> Line | None:  # noqa: ANN001
        if self.col < 6:
            return None
        line: Line = []
        for index, total in enumerate(self.totals):
            text = fmt(total) if total > 0 else "·"
            line += _cell(text, self.col, S_DIM, "center")
            if self.gap and index + 1 < len(self.totals):
                line.append((" " * self.gap, ""))
        return line


def _metric_fmt(metric: str):  # noqa: ANN202
    if metric == "cost":
        return _fmt_cost
    return _compact


# =============================================================================
# Usage card
# =============================================================================


def _empty_usage_card(data: UsageReportData, color: bool, title_kind: str = "usage") -> str:
    card = _Card(color)
    card.top(_title(title_kind, data), _filter_badge(data))
    card.blank()
    card.row([("No usage recorded yet", S_TITLE), (" for this window.", S_DIM)])
    card.blank()
    card.row([("▸ ", S_TOKENS), ("chat a turn", ""), (" · usage is tracked automatically", S_DIM)])
    card.row([("▸ ", S_TOKENS), ("import history ", ""), ("/stats sweep <dir>", S_TOKENS)])
    card.blank()
    card.bottom()
    return card.render(_after_notice(data.upgrade_notice))


def _prior_note(card: _Card, data: UsageReportData) -> None:
    """Spacer row under the title, explaining the KPI deltas when there are any."""
    if data.previous is None:
        card.blank()
        return
    window = _clean(data.window_label).strip().casefold().removeprefix("last ")
    card.row(
        _cell(f"▲▼ vs previous {window}" if window else "▲▼ vs previous", CONTENT, S_DIM, "right")
    )


def _kpi_sparks(data: UsageReportData, show_cost: bool) -> list[Line | None]:
    points = [point for _, point in _daily_points(data)]
    if len(points) < 2:
        return []

    def series(fn) -> list[float]:  # noqa: ANN001
        return [float(fn(point)) if point is not None else 0.0 for point in points]

    tokens = series(lambda p: p.total_tokens)
    second = series(lambda p: p.cost_usd) if show_cost else series(lambda p: p.calls)
    cache = series(lambda p: p.cache_read_tokens / p.input_tokens if p.input_tokens > 0 else 0.0)
    sessions = series(lambda p: p.sessions)
    count = len(points)
    width = count * max(1, SPARK_W // count) if count <= SPARK_W else SPARK_W
    out: list[Line | None] = []
    for values, color in (
        (tokens, S_TOKENS),
        (second, S_COST),
        (cache, S_CACHE),
        (sessions, S_SESS),
    ):
        out.append(viz.spark(values, width, color) if any(values) else None)
    return out


def _anatomy_rows(data: UsageReportData) -> list[Line]:
    """Where the tokens went: input split by caching, output split by reasoning."""
    bar_w = 32
    text_color = viz.C_ANSWER
    fresh = max(0, data.input_tokens - data.cache_read_tokens - data.cache_write_tokens)
    answer = max(0, data.output_tokens - data.reasoning_tokens)
    reasoning = min(data.output_tokens, max(0, data.reasoning_tokens))
    input_bar = viz.stacked_bar(
        [
            (data.cache_read_tokens, S_CACHE, "█"),
            (fresh, S_TOKENS, "▒"),
            (data.cache_write_tokens, viz.C_WRITE, "░"),
        ],
        bar_w,
    )
    output_bar = viz.stacked_bar([(answer, text_color, "█"), (reasoning, viz.C_REASON, "▒")], bar_w)
    reason_rate = reasoning / data.output_tokens if data.output_tokens > 0 else None
    rows: list[Line] = [
        [
            ("in   ", S_DIM),
            *input_bar,
            ("  ", ""),
            (f"{_compact(data.input_tokens):>6}", S_TOKENS),
            (" · ", S_DIM),
            (_fmt_pct(data.cache_hit_rate), S_CACHE),
            (" cached", S_DIM),
        ],
        [
            ("out  ", S_DIM),
            *output_bar,
            ("  ", ""),
            (f"{_compact(data.output_tokens):>6}", text_color),
            (" · ", S_DIM),
            (_fmt_pct(reason_rate), viz.C_REASON),
            (" reasoning", S_DIM),
        ],
    ]
    legend: Line = [("     ", "")]
    for index, (color, glyph, label) in enumerate(
        (
            (S_CACHE, "█", "cached"),
            (S_TOKENS, "▒", "fresh"),
            (viz.C_WRITE, "░", "cache write"),
            (text_color, "█", "answer"),
            (viz.C_REASON, "▒", "reasoning"),
        )
    ):
        if index:
            legend.append(("  ", ""))
        legend += [_swatch(color, glyph), (f" {label}", S_DIM)]
    rows.append(legend)
    return rows


def _daily_section(card: _Card, data: UsageReportData, legend: _Legend, height: int = 5) -> None:
    mix = _DailyMix(data, legend, CONTENT)
    peak, peak_day = mix.peak()
    detail = "by model" if data.daily_models else ""
    if mix.per > 1:
        detail = " · ".join(part for part in (detail, f"{mix.per}d per bar") if part)
    if peak > 0:
        when = f" {_fmt_day(peak_day)}" if peak_day is not None else ""
        detail = " · ".join(part for part in (detail, f"peak {_compact(peak)}{when}") if part)
    card.divider("daily tokens", detail)
    if mix.empty:
        card.row([("no daily activity", S_DIM)])
        return
    for line in mix.rows(height, share=False):
        card.row(line)
    card.row(mix.axis())


def _models_section(
    card: _Card,
    data: UsageReportData,
    show_cost: bool,
    legend: _Legend,
    mix: _DailyMix | None = None,
    show_tokens: bool = True,
) -> None:
    entries = legend.entries
    if not entries:
        card.row([("no per-model breakdown recorded", S_DIM)])
        return
    metric = legend.metric
    total = sum(e.value(metric) for e in entries) or 1
    has_speed = any(e.tps is not None for e in entries)
    name_w, share_w, tok_w = 16, 5, 7
    # (header, width, renderer)
    extra: list[tuple[str, int, str]] = []
    if show_cost:
        extra.append(("cost", 8, "cost"))
    else:
        extra.append(("cache", 5, "cache"))
    if not show_tokens:
        tok_w = -1  # column and its separator dropped: shares only
    if mix is not None:
        count = max(1, len(mix.columns))
        trend_w = count * max(1, 12 // count) if count <= 12 else 12
        extra.append(("daily" if show_tokens else "daily share", max(7, trend_w), "trend"))
    elif has_speed:
        extra.append(("speed", 8, "speed"))
    elif show_cost:
        extra.append(("cache", 8, "cache"))
    fixed = 2 + name_w + share_w + tok_w + sum(w for _, w, _ in extra)
    seps = 3 + len(extra)
    bar_w = CONTENT - fixed - seps

    header: Line = [*_cell("model", 2 + name_w, S_DIM), (" ", ""), (" " * bar_w, "")]
    header += [(" ", ""), *_cell("share", share_w, S_DIM, "right")]
    if show_tokens:
        header += [(" ", ""), *_cell("tokens", tok_w, S_DIM, "right")]
    for label, width, kind in extra:
        header += [(" ", ""), *_cell(label, width, S_DIM, "left" if kind == "trend" else "right")]
    card.row(header)

    for index, entry in enumerate(entries):
        key = legend.entry_key(entry)
        color, _glyph = legend.paint(key)
        share = entry.value(metric) / total
        name_style = S_DIM if entry.other else ("bold" if index == 0 else "")
        line: Line = [legend.swatch(key), (" ", "")]
        line += [*_cell(_display_name(entry.name), name_w, name_style), (" ", "")]
        line += viz.line_bar(share, bar_w, color)
        line += [(" ", ""), *_cell(_fmt_pct(share), share_w, color, "right")]
        if show_tokens:
            line += [(" ", ""), *_cell(_compact(entry.tokens), tok_w, S_TOKENS, "right")]
        for _, width, kind in extra:
            if kind == "cost":
                if entry.priced <= 0 and entry.cost <= 0:
                    text, style = "n/a", S_DIM
                else:
                    text = _fmt_cost(entry.cost)
                    text, style = ("~" + text if entry.unpriced else text), S_COST
            elif kind == "speed":
                text, style = _fmt_tps(entry.tps), (S_SPEED if entry.tps is not None else S_DIM)
            elif kind == "trend":
                assert mix is not None
                values = [column.get(key, 0.0) for column in mix.columns]
                if not show_tokens:  # the model's share of each day, not its volume
                    values = [
                        v / t if t > 0 else 0.0 for v, t in zip(values, mix.totals, strict=True)
                    ]
                    trend = viz.spark(values, width, color, peak=1.0)
                else:
                    trend = viz.spark(values, width, color)
                line += [(" ", ""), *_fit(trend, width)]
                continue
            else:
                text = _fmt_pct(entry.cache)
                style = S_CACHE if entry.cache is not None else S_DIM
            line += [(" ", ""), *_cell(text, width, style, "right")]
        card.row(line)


def _hour_grid(data: UsageReportData) -> list[list[int]]:
    """7 x 24 calls grid (4-hour slots are spread evenly when hours are unknown)."""
    hourly = [list(row[:24]) for row in list(data.rhythm.hourly)[:7]]
    if len(hourly) == 7 and all(len(row) == 24 for row in hourly):
        return [[max(0, int(v)) for v in row] for row in hourly]
    grid: list[list[int]] = []
    for row in list(data.rhythm.cells)[:7]:
        slots = [max(0, int(v)) for v in list(row)[:6]] + [0] * (6 - len(list(row)[:6]))
        grid.append([math.ceil(slots[hour // 4] / 4) for hour in range(24)])
    grid += [[0] * 24 for _ in range(7 - len(grid))]
    return grid


def _weekday_labels(data: UsageReportData) -> list[str]:
    defaults = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
    given = [_clean(w)[:3] for w in list(data.rhythm.weekday_labels or ())[:7]]
    return given + list(defaults[len(given) :])


def _busiest_slot(data: UsageReportData) -> str | None:
    grid = _hour_grid(data)
    hourly = len(data.rhythm.hourly) == 7
    best = (0, -1, -1)
    for r, row in enumerate(grid):
        for c, value in enumerate(row):
            if value > best[0]:
                best = (value, r, c)
    if best[0] <= 0:
        return None
    day = _weekday_labels(data)[best[1]]
    if hourly:
        return f"{day} {best[2]:02d}:00"
    start = best[2] // 4 * 4
    return f"{day} {start:02d}-{start + 4:02d}"


def _heat_legend() -> Line:
    legend: Line = [("less ", S_DIM), viz.heat_cell(0, 2)]
    for level in range(1, viz.HEAT_STEPS + 1):
        legend.append(viz.heat_cell(level, 2))
    legend.append((" more", S_DIM))
    return legend


def _punch_card(data: UsageReportData) -> list[Line]:
    """Weekday x hour heatmap with weekday shares and an hour-of-day profile."""
    grid = _hour_grid(data)
    levels = viz.heat_levels(grid, steps=viz.HEAT_STEPS)
    labels = _weekday_labels(data)
    day_totals = [sum(row) for row in grid]
    total = sum(day_totals)
    peak_day = max(day_totals, default=0)
    rows: list[Line] = [[("    ", ""), ("".join(f"{h:02d}    " for h in range(0, 24, 3)), S_DIM)]]
    for label, row, day_total in zip(labels, levels, day_totals, strict=True):
        line: Line = [(f"{label:<3} ", S_DIM)]
        line += [viz.heat_cell(level, 2) for level in row]
        line.append(("  ", ""))
        line += viz.line_bar(day_total / peak_day if peak_day else 0.0, 4, S_TOKENS)
        share = _fmt_pct(day_total / total) if total else "-"
        line.append((f"{share:>4}", S_TOKENS if day_total else S_DIM))
        rows.append(line)
    hours = [sum(row[hour] for row in grid) for hour in range(24)]
    rows.append([("all ", S_DIM), *viz.spark(hours, 48, S_TOKENS)])
    footer = _heat_legend()
    busiest = _busiest_slot(data)
    if busiest:
        footer += [("  ·  busiest ", S_DIM), (busiest, S_TOKENS)]
    rows.append(footer)
    return rows


def _machines_section(card: _Card, data: UsageReportData, show_cost: bool, now: float) -> None:
    machines = data.machines
    card.divider("machines", f"{len(machines)} aggregated")
    total = sum(m.total_tokens for m in machines) or 1
    name_w, share_w, tok_w, last_w, state_w = 13, 5, 7, 8, 8
    bar_w = CONTENT - 2 - name_w - share_w - tok_w - last_w - state_w - 5
    for index, machine in enumerate(machines):
        color = viz.MACHINE_COLORS[index % len(viz.MACHINE_COLORS)]
        share = machine.total_tokens / total
        if machine.is_local:
            state, state_style = "local", S_DIM
        elif machine.synced_at is None:
            state, state_style = "no data", S_WARN
        else:
            state = _fmt_age(now - machine.synced_at)
            state_style = S_WARN if machine.last_error else S_DIM
        last = _fmt_cost(machine.cost_usd) if show_cost else _count(machine.calls)
        line: Line = [_swatch(color, viz.PLAIN_FILLS[index % len(viz.PLAIN_FILLS)]), (" ", "")]
        line += [
            *_cell(_clean(machine.name), name_w, "bold" if machine.is_local else ""),
            (" ", ""),
        ]
        line += viz.line_bar(share, bar_w, color)
        line += [(" ", ""), *_cell(_fmt_pct(share), share_w, color, "right")]
        line += [(" ", ""), *_cell(_compact(machine.total_tokens), tok_w, S_TOKENS, "right")]
        line += [(" ", ""), *_cell(last, last_w, S_COST, "right")]
        line += [(" ", ""), *_cell(state, state_w, state_style, "right")]
        card.row(line)
    for machine in machines:
        if machine.last_error:
            card.wrapped(f"{machine.name}: {machine.last_error}", S_DIM, prefix="▸ ", pstyle=S_WARN)


def _usage_card(
    data: UsageReportData, color: bool, show_cost: bool, max_models: int, now: float | None = None
) -> _Card | None:
    if data.total_calls <= 0:
        return None
    card = _Card(color)
    card.top(_title("usage", data), _filter_badge(data))
    prev = data.previous
    _prior_note(card, data)

    if show_cost:
        cost_text, cost_style = _cost_display(data)
        second = (cost_text, cost_style, "est. cost")
        second_delta = _delta(data.total_cost_usd, prev.cost_usd) if prev else ""
    else:
        second = (_count(data.total_calls), S_COST_BIG, "llm calls")
        second_delta = _delta(data.total_calls, prev.calls) if prev else ""
    kpis = [
        (_compact(data.total_tokens), S_TOKENS_BIG, "tokens"),
        second,
        (_fmt_pct(data.cache_hit_rate), S_CACHE_BIG, "cache hit"),
        (_int(data.total_sessions), S_SESS_BIG, "sessions"),
    ]
    deltas: list[str] = []
    if prev is not None:
        deltas = [
            _delta(data.total_tokens, prev.total_tokens),
            second_delta,
            _delta(data.cache_hit_rate, prev.cache_hit_rate, points=True),
            _delta(data.total_sessions, prev.sessions),
        ]
    for line in _kpi_rows(kpis, KPI_WIDTHS, deltas, _kpi_sparks(data, show_cost)):
        card.row(line)
    card.blank()

    for line in _anatomy_rows(data):
        card.row(line)
    card.blank()
    days = "day" if data.active_days == 1 else "days"
    activity: Line = [
        (_int(data.active_days), ""),
        (f" active {days}", S_DIM),
        ("  ·  ", S_DIM),
        ("streak ", S_DIM),
        (f"{data.current_streak_days}d", ""),
        (" · best ", S_DIM),
        (f"{data.longest_streak_days}d", ""),
    ]
    if show_cost:
        activity += [("  ·  ", S_DIM), (_int(data.total_calls), ""), (" calls", S_DIM)]
    card.row(activity)
    if show_cost and data.unpriced_calls > 0:
        if data.priced_calls <= 0:
            note = "no pricing data for these models"
        else:
            total_calls = data.priced_calls + data.unpriced_calls
            note = f"~ {_int(data.unpriced_calls)} of {_int(total_calls)} calls unpriced"
        if data.pricing_version:
            note += f" · pricing {_clean(data.pricing_version)}"
        card.row([(note, S_WARN)])

    legend = _Legend(data, max_models)
    _daily_section(card, data, legend)

    card.divider("models")
    _models_section(card, data, show_cost, legend)

    tz = _clean(data.rhythm.timezone_label).strip() or "UTC"
    card.divider(f"rhythm · {tz}", "calls by weekday and hour")
    for line in _punch_card(data):
        card.row(line)

    if len(data.machines) > 1 or any(m.last_error for m in data.machines):
        _machines_section(card, data, show_cost, time.time() if now is None else now)

    note: Line | None = None
    if show_cost and data.pricing_version and not data.unpriced_calls:
        note = [(f"pricing {_clean(data.pricing_version)}", S_DIM)]
    card.bottom(note)
    return card


def render_usage_card(
    data: UsageReportData,
    *,
    color: bool = True,
    show_cost: bool = True,
    max_models: int = 6,
    now: float | None = None,
) -> str:
    """Main ``/stats`` overview card (68 columns)."""
    card = _usage_card(data, color, show_cost, max_models, now)
    if card is None:
        return _empty_usage_card(data, color)
    return card.render(_after_notice(data.upgrade_notice))


def render_share_card(data: UsageReportData, *, show_cost: bool = True) -> str:
    """Plain card in a ```text fence, ready to paste into Discord/Slack/GitHub/X.

    Shows the combined totals only: notices and machine names stay private.
    """
    from dataclasses import replace

    data = replace(data, upgrade_notice=None, machines=(), machine_filter=None)
    card = _usage_card(data, False, show_cost, 6)
    body = _empty_usage_card(data, False) if card is None else card.render()
    return f"```text\n{body}\n```"


# =============================================================================
# Models card
# =============================================================================


def _service_tier_rows_to_show(data: UsageReportData) -> tuple[ServiceTierUsageRow, ...]:
    """Return tier rows for models that have at least one non-standard tier."""
    non_standard_models = {
        row.model for row in data.service_tiers if row.service_tier != "standard"
    }
    if not non_standard_models:
        return ()
    return tuple(row for row in data.service_tiers if row.model in non_standard_models)


def _service_tiers_models_section(
    card: _Card,
    data: UsageReportData,
    show_cost: bool,
    legend: _Legend,
) -> None:
    rows = _service_tier_rows_to_show(data)
    if not rows:
        return
    card.divider("service tiers", "models with non-standard tiers")
    name_w, tier_w, calls_w, tok_w, cost_w = 18, 10, 6, 8, 9
    header: Line = [*_cell("model", 2 + name_w, S_DIM), (" ", "")]
    header += [*_cell("tier", tier_w, S_DIM), (" ", "")]
    header += [*_cell("calls", calls_w, S_DIM, "right"), (" ", "")]
    header += _cell("tokens", tok_w, S_DIM, "right")
    if show_cost:
        header += [(" ", ""), *_cell("cost", cost_w, S_DIM, "right")]
    card.row(header)
    limit = 10
    shown = rows[:limit]
    for row in shown:
        line: Line = [legend.swatch(row.model), (" ", "")]
        line += [*_cell(_display_name(row.model), name_w, ""), (" ", "")]
        tier_style = S_DIM if row.service_tier == "standard" else S_COST
        line += [*_cell(row.service_tier, tier_w, tier_style), (" ", "")]
        line += [*_cell(_int(row.calls), calls_w, "", "right"), (" ", "")]
        line += _cell(_compact(row.total_tokens), tok_w, S_TOKENS, "right")
        if show_cost:
            if row.priced_calls <= 0 and row.cost_usd <= 0:
                cost_text, cost_style = "n/a", S_DIM
            else:
                cost_text = _fmt_cost(row.cost_usd)
                cost_text, cost_style = (
                    ("~" + cost_text if row.unpriced_calls else cost_text),
                    S_COST,
                )
            line += [(" ", ""), *_cell(cost_text, cost_w, cost_style, "right")]
        card.row(line)
    if len(rows) > limit:
        card.row([(f"+ {len(rows) - limit} more tier rows", S_DIM)])


def _service_tiers_perf_section(
    card: _Card,
    data: UsageReportData,
    colors: dict[str, str],
) -> None:
    rows = _service_tier_rows_to_show(data)
    if not rows:
        return
    card.divider("by service tier", "ttft p50 · p90 · tok/s")
    name_w, tier_w, calls_w, dur_w, tps_w = 16, 10, 6, 7, 7
    header: Line = [*_cell("model", 2 + name_w, S_DIM), (" ", "")]
    header += [*_cell("tier", tier_w, S_DIM), (" ", "")]
    header += [*_cell("calls", calls_w, S_DIM, "right"), (" ", "")]
    header += [*_cell("p50", dur_w, S_DIM, "right"), (" ", "")]
    header += [*_cell("p90", dur_w, S_DIM, "right"), (" ", "")]
    header += _cell("tok/s", tps_w, S_DIM, "right")
    card.row(header)
    limit = 10
    shown = rows[:limit]
    for index, row in enumerate(shown):
        chip = colors.get(row.model, viz.C_OTHER)
        tps = "-" if row.tps is None else _fmt_tps(row.tps).removesuffix(" t/s")
        has_ttft = row.ttft_p50_ms is not None
        tier_style = S_DIM if row.service_tier == "standard" else S_COST
        line: Line = [_swatch(chip, viz.PLAIN_FILLS[index % len(viz.PLAIN_FILLS)]), (" ", "")]
        line += [*_cell(_display_name(row.model), name_w, ""), (" ", "")]
        line += [*_cell(row.service_tier, tier_w, tier_style), (" ", "")]
        line += [*_cell(_int(row.calls), calls_w, "", "right"), (" ", "")]
        line += [*_cell(_fmt_dur(row.ttft_p50_ms), dur_w, S_CACHE if has_ttft else S_DIM, "right"), (" ", "")]
        line += [*_cell(_fmt_dur(row.ttft_p90_ms), dur_w, S_DIM, "right"), (" ", "")]
        line += _cell(tps, tps_w, S_COST if row.tps is not None else S_DIM, "right")
        card.row(line)
    if len(rows) > limit:
        card.row([(f"+ {len(rows) - limit} more tier rows", S_DIM)])


def render_models_card(
    data: UsageReportData,
    *,
    color: bool = True,
    show_cost: bool = True,
    max_models: int = 8,
    metric: str = "tokens",
    height: int = 8,
    totals: bool = False,
) -> str:
    """``/stats models``: each day's model mix as a filled 100% stacked bar.

    The card is about proportions: raw token counts only appear with ``totals``.
    """
    metric = metric if metric in MIX_METRICS else "tokens"
    if metric == "cost" and not show_cost:
        metric = "tokens"
    if data.total_calls <= 0:
        return _empty_usage_card(data, color, "models")
    card = _Card(color)
    card.top(
        _title("models", data), _filter_badge(data, "" if metric == "tokens" else f"by {metric}")
    )
    card.blank()

    legend = _Legend(data, max_models, metric)
    fmt = _metric_fmt(metric)
    entries = legend.entries
    total = sum(entry.value(metric) for entry in entries)
    parts = [(entry.value(metric), *legend.paint(legend.entry_key(entry))) for entry in entries]
    card.row(viz.stacked_bar(parts, CONTENT))
    lead = entries[0] if entries else None
    caption: Line = [(f"share of {metric}", S_DIM)]
    if totals or metric == "cost":
        caption += [(" · ", S_DIM), (fmt(total), S_TOKENS), (" total", S_DIM)]
    if lead is not None and total > 0:
        lead_color, _ = legend.paint(legend.entry_key(lead))
        caption += [
            (" · top ", S_DIM),
            (_display_name(lead.name), lead_color),
            (" ", ""),
            (_fmt_pct(lead.value(metric) / total), lead_color),
        ]
    card.row(caption)

    axis_w = 6
    mix = _DailyMix(data, legend, CONTENT - axis_w)
    detail = f"% of {metric} per day" + (f" · {mix.per}d per bar" if mix.per > 1 else "")
    card.divider("daily share", detail)
    if mix.empty:
        card.row([("no daily activity", S_DIM)])
    else:
        rows = mix.rows(height, share=True)
        ticks = {0: "100%", height // 2: " 50%"}
        for index, line in enumerate(rows):
            label = ticks.get(index)
            axis: Line = [(f"{label} ┤" if label else "     │", S_DIM)]
            card.row(axis, line)
        card.row([("  0% └", S_DIM), ("─" * mix.plot_width, S_DIM)])
        card.row([(" " * axis_w, "")], mix.axis())
        totals_row = mix.totals_row(fmt) if totals or metric == "cost" else None
        if totals_row is not None:
            card.row([(" " * axis_w, "")], totals_row)

    card.divider("models", f"{len(data.models)} used")
    _models_section(card, data, show_cost, legend, mix, show_tokens=totals)
    _service_tiers_models_section(card, data, show_cost, legend)
    note: Line | None = None
    if show_cost and data.pricing_version and not data.unpriced_calls:
        note = [(f"pricing {_clean(data.pricing_version)}", S_DIM)]
    card.bottom(note)
    return card.render(_after_notice(data.upgrade_notice))


# =============================================================================
# Rhythm card
# =============================================================================


def _calendar_rows(data: UsageReportData) -> list[Line]:
    """Contribution-graph calendar: one column per week, one row per weekday."""
    points = [(day, point) for day, point in _daily_points(data) if day is not None]
    if not points:
        return []
    tokens = {day: (max(0, int(point.total_tokens)) if point else 0) for day, point in points}
    start, end = points[0][0], points[-1][0]
    first_monday = start - timedelta(days=start.weekday())
    weeks = ((end - first_monday).days // 7) + 1
    room = CONTENT - 4
    if weeks > room:  # keep the most recent weeks
        first_monday += timedelta(days=7 * (weeks - room))
        weeks = room
    if weeks <= 14:
        cell, gap = 3, 1
    elif weeks <= 19:
        cell, gap = 2, 1
    elif weeks <= room // 2:
        cell, gap = 2, 0
    else:
        cell, gap = 1, 0
    step = cell + gap

    grid = [
        [tokens.get(first_monday + timedelta(days=7 * week + weekday)) for week in range(weeks)]
        for weekday in range(7)
    ]
    levels = viz.heat_levels([[v or 0 for v in row] for row in grid], steps=viz.HEAT_STEPS)

    header = [" "] * (weeks * step)
    cursor = 0
    for week in range(weeks):
        monday = first_monday + timedelta(days=7 * week)
        days = [monday + timedelta(days=i) for i in range(7)]
        starts = next((d for d in days if d.day == 1), None)
        label_day = starts or (max(monday, start) if week == 0 else None)
        if label_day is None:
            continue
        label = label_day.strftime("%b")
        at = week * step
        if at < cursor or at + len(label) > len(header):
            continue
        header[at : at + len(label)] = label
        cursor = at + len(label) + 1
    rows: list[Line] = [[("    ", ""), ("".join(header).rstrip(), S_DIM)]]
    labels = _weekday_labels(data)
    for weekday in range(7):
        line: Line = [(f"{labels[weekday]:<3} ", S_DIM)]
        for week in range(weeks):
            value = grid[weekday][week]
            if value is None:
                line.append((" " * cell, ""))
            else:
                line.append(viz.heat_cell(levels[weekday][week], cell))
            if gap and week + 1 < weeks:
                line.append((" " * gap, ""))
        rows.append(line)
    series = [tokens[day] for day, _ in points]
    peak = max(series)
    footer = _heat_legend()
    if peak > 0:
        peak_day = points[series.index(peak)][0]
        footer += [
            ("  ·  peak ", S_DIM),
            (_compact(peak), S_TOKENS),
            (f" {_fmt_day(peak_day)}", S_DIM),
        ]
    rows.append(footer)
    return rows


def render_rhythm_card(data: UsageReportData, *, color: bool = True) -> str:
    """``/stats rhythm``: when you work — calendar, weekday x hour, streaks."""
    if data.total_calls <= 0:
        return _empty_usage_card(data, color, "rhythm")
    card = _Card(color)
    card.top(_title("rhythm", data), _filter_badge(data))
    card.blank()
    span = len(_daily_points(data))
    active = f"{data.active_days}/{span}" if span else _int(data.active_days)
    kpis = [
        (active, S_TOKENS_BIG, "active days"),
        (f"{data.current_streak_days}d", S_COST_BIG, "current streak"),
        (f"{data.longest_streak_days}d", S_CACHE_BIG, "best streak"),
        (_busiest_slot(data) or "-", S_SESS_BIG, "busiest hour"),
    ]
    for line in _kpi_rows(kpis):
        card.row(line)
    card.blank()
    if span >= 15:
        card.divider("calendar", "tokens per day")
        for line in _calendar_rows(data):
            card.row(line)
    tz = _clean(data.rhythm.timezone_label).strip() or "UTC"
    card.divider(f"week · {tz}", "calls by weekday and hour")
    for line in _punch_card(data):
        card.row(line)
    card.bottom()
    return card.render(_after_notice(data.upgrade_notice))


# =============================================================================
# Remotes card
# =============================================================================


def _fmt_bytes(size: int | None) -> str:
    if size is None:
        return "-"
    value = float(size)
    for unit in ("B", "kB", "MB", "GB"):
        if value < 999.5 or unit == "GB":
            return f"{value:.0f} {unit}" if unit in ("B", "kB") else f"{value:.1f} {unit}"
        value /= 1024.0
    return "-"


def render_remotes_card(
    remotes: Sequence[RemoteStatus],
    *,
    color: bool = True,
    local_name: str = "local",
    now: float | None = None,
) -> str:
    """``/stats remote``: configured remote ledgers and the state of their mirrors."""
    now = time.time() if now is None else now
    card = _Card(color)
    failed = sum(1 for remote in remotes if remote.last_error)
    if not remotes:
        badge: Line = [("▸ none configured", S_DIM)]
    elif failed:
        badge = [(f"▸ {failed} failing", S_WARN)]
    else:
        badge = [(f"▸ {len(remotes)} synced", S_OK)]
    card.top([*_brand(), (" remotes", S_TITLE)], badge)
    card.blank()
    card.row(
        [_swatch(viz.MACHINE_COLORS[0]), (" ", ""), (_clean(local_name), "bold")]
        + [("  this machine · live ledger", S_DIM)]
    )
    for index, remote in enumerate(remotes, start=1):
        chip_color = viz.MACHINE_COLORS[index % len(viz.MACHINE_COLORS)]
        glyph = viz.PLAIN_FILLS[index % len(viz.PLAIN_FILLS)]
        card.blank()
        if remote.last_error and remote.synced_at is None:
            state: Line = [("never synced", S_ERR)]
        elif remote.last_error:
            state = [(f"stale · {_fmt_age(now - (remote.synced_at or now))}", S_WARN)]
        elif remote.synced_at is None:
            state = [("not synced yet", S_WARN)]
        else:
            fresh = "up to date" if remote.changed is False else "synced"
            state = [(f"{fresh} · {_fmt_age(now - remote.synced_at)}", S_OK)]
        via = "ssh " if remote.kind == "ssh" else ""
        head: Line = [_swatch(chip_color, glyph), (" ", ""), (_clean(remote.name), "bold")]
        targets = " | ".join(_clean(item) for item in (remote.target, *remote.fallbacks))
        head.append((f"  {via}{targets}", S_DIM))
        room = CONTENT - _width(state) - 1
        card.row(_fit(head, room), [(" ", "")], state)
        facts: list[str] = []
        if remote.host:
            facts.append(f"host {_clean(remote.host)}")
        if remote.calls is not None:
            facts.append(f"{_count(remote.calls)} calls")
        if remote.size_bytes is not None:
            facts.append(_fmt_bytes(remote.size_bytes))
        if facts:
            card.row([("  " + " · ".join(facts), S_DIM)])
        if remote.last_error:
            card.wrapped(remote.last_error, S_ERR, prefix="  ▸ ", pstyle=S_ERR)
    card.blank()
    if remotes:
        card.row(
            [("▸ ", S_TOKENS), ("/stats sync", S_TOKENS), (" refreshes now", S_DIM)]
            + [(" · ", S_DIM), ("/stats --machine <name>", S_TOKENS), (" narrows", S_DIM)]
        )
    else:
        card.row(
            [("▸ ", S_TOKENS), ("/stats remote add <ssh-host>", S_TOKENS)]
            + [(" to include another machine", S_DIM)]
        )
    card.blank()
    card.bottom()
    return card.render()


# =============================================================================
# Perf card
# =============================================================================


def _phase_legend(phases: PhaseBreakdown) -> list[Line]:
    parts = _phase_parts(phases)
    total = sum(p[4] for p in parts) or 1.0
    items: list[Line] = []
    for key, label, glyph, style, value in parts:
        if key == "other" and value <= 0:
            continue
        items.append(
            [
                _swatch(style, glyph),
                (f" {label:<8}", ""),
                (f"{_fmt_dur(value):>8}", style),
                (f"{_fmt_pct(value / total):>6}", S_DIM),
            ]
        )
    rows: list[Line] = []
    half = (CONTENT - 4) // 2  # two columns of 29 with a 4-cell gutter
    for i in range(0, len(items), 2):
        line = _fit(items[i], half)
        if i + 1 < len(items):
            line += [("    ", ""), *_fit(items[i + 1], half)]
        rows.append(line)
    return rows


def _tool_bars(card: _Card, tools: Sequence[ToolUsageRow], limit: int, *, with_max: bool) -> None:
    ordered = sorted(tools, key=lambda t: (-t.total_ms, -t.runs, t.tool_name))
    limit = max(1, limit)
    shown: list[tuple[str, int, int, float, float, float, bool]] = [
        (
            _clean(t.tool_name),
            t.runs,
            t.effective_timed_runs,
            t.total_ms,
            t.avg_ms,
            t.max_ms,
            False,
        )
        for t in ordered[: limit if len(ordered) <= limit else limit - 1]
    ]
    if len(ordered) > limit:
        tail = ordered[limit - 1 :]
        runs = sum(t.runs for t in tail)
        timed = sum(t.effective_timed_runs for t in tail)
        total = sum(t.total_ms for t in tail)
        shown.append(
            (
                f"other ({len(tail)})",
                runs,
                timed,
                total,
                total / timed if timed else 0.0,
                max(t.max_ms for t in tail),
                True,
            )
        )
    peak = max((row[3] for row in shown), default=0.0) or 1.0
    name_w, runs_w, total_w, avg_w = 17, 6, 8, 7
    max_w = 7 if with_max else 0
    seps = 4 + (1 if with_max else 0)
    bar_w = CONTENT - name_w - runs_w - total_w - avg_w - max_w - seps

    header: Line = [*_cell("tool", name_w, S_DIM), (" ", ""), (" " * bar_w, "")]
    header += [(" ", ""), *_cell("runs", runs_w, S_DIM, "right")]
    header += [(" ", ""), *_cell("total", total_w, S_DIM, "right")]
    header += [(" ", ""), *_cell("avg", avg_w, S_DIM, "right")]
    if with_max:
        header += [(" ", ""), *_cell("max", max_w, S_DIM, "right")]
    card.row(header)
    for name, runs, timed, total, avg, mx, other in shown:
        line: Line = [*_cell(name, name_w, S_DIM if other else ""), (" ", "")]
        if timed <= 0:
            line += _bar(0.0, bar_w, S_DIM)
            total_text, total_style = "-", S_DIM
            avg_text, max_text = "-", "-"
        else:
            line += _bar(total / peak, bar_w, S_DIM if other else S_TOOLS)
            total_text = _fmt_dur(total) + ("*" if timed < runs else "")
            total_style = ""
            avg_text = _fmt_dur(avg)
            max_text = _fmt_dur(mx)
        line += [(" ", ""), *_cell(_compact(runs), runs_w, S_SPEED, "right")]
        line += [(" ", ""), *_cell(total_text, total_w, total_style, "right")]
        line += [(" ", ""), *_cell(avg_text, avg_w, S_DIM, "right")]
        if with_max:
            line += [(" ", ""), *_cell(max_text, max_w, S_DIM, "right")]
        card.row(line)


def _ptc_summary_rows(card: _Card, ptc: PtcSummary) -> None:
    """Compact PTC sandbox efficiency & script-error lines."""
    ratio = ptc.nested_calls / ptc.cells if ptc.cells > 0 else 0.0
    py_pct = ptc.pure_python_cells / ptc.cells if ptc.cells > 0 else 0.0
    card.row(
        [
            ("sandbox ", S_DIM),
            (_int(ptc.cells), S_TITLE),
            (" cells · ", S_DIM),
            (_int(ptc.nested_calls), S_SPEED),
            (f" calls ({ratio:.1f}/c) · ", S_DIM),
            (_fmt_pct(py_pct), S_CACHE),
            (" py-only", S_DIM),
        ]
    )
    if ptc.error_cells > 0:
        err_pct = ptc.error_cells / ptc.cells if ptc.cells > 0 else 0.0
        parts: list[str] = []
        if ptc.typecheck_errors:
            parts.append(f"{ptc.typecheck_errors} type")
        if ptc.syntax_errors:
            parts.append(f"{ptc.syntax_errors} syntax")
        if ptc.runtime_errors:
            parts.append(f"{ptc.runtime_errors} runtime")
        detail = f" · {' · '.join(parts)}" if parts else ""
        card.row(
            [
                ("errors  ", S_DIM),
                (_int(ptc.error_cells), S_WARN),
                (f" cells ({_fmt_pct(err_pct)}){detail}", S_DIM),
            ]
        )
    elif ptc.nested_error_calls > 0:
        card.row(
            [
                ("errors  ", S_DIM),
                (_int(ptc.nested_error_calls), S_WARN),
                (" nested tool errors", S_DIM),
            ]
        )


def _ptc_model_table(card: _Card, ptc: PtcSummary, limit: int = 6) -> None:
    """Per-model PTC efficiency & error breakdown (fits 62-cell CONTENT)."""
    if not ptc.by_model:
        return
    card.divider("sandbox by model")
    widths = (22, 6, 7, 7, 6, 9)  # 57 + 5 separators = 62
    heads = ("model", "cells", "calls/c", "py-only", "err%", "t/s/r")
    header: Line = []
    for i, (head, width) in enumerate(zip(heads, widths, strict=True)):
        if i:
            header.append((" ", ""))
        header += _cell(head, width, S_DIM, "left" if i == 0 else "right")
    card.row(header)

    rows = ptc.by_model
    shown = rows[: limit if len(rows) <= limit else limit - 1]
    for item in shown:
        ratio = item.nested_calls / item.cells if item.cells > 0 else 0.0
        py_pct = item.pure_python_cells / item.cells if item.cells > 0 else 0.0
        err_pct = item.error_cells / item.cells if item.cells > 0 else 0.0
        tsr = (
            f"{item.typecheck_errors}/{item.syntax_errors}/{item.runtime_errors}"
            if item.error_cells > 0
            else "-"
        )
        cells = (
            (_display_name(item.model), ""),
            (_compact(item.cells), S_SPEED),
            (f"{ratio:.1f}", ""),
            (_fmt_pct(py_pct), S_CACHE if item.pure_python_cells else S_DIM),
            (_fmt_pct(err_pct), S_WARN if item.error_cells else S_DIM),
            (tsr, S_WARN if item.error_cells else S_DIM),
        )
        line: Line = []
        for i, ((text, style), width) in enumerate(zip(cells, widths, strict=True)):
            if i:
                line.append((" ", ""))
            line += _cell(text, width, style, "left" if i == 0 else "right")
        card.row(line)
    if len(rows) > limit:
        card.row([(f"+ {len(rows) - (limit - 1)} more models", S_DIM)])


def render_perf_card(data: UsageReportData, *, color: bool = True) -> str:
    """``/stats perf``: agent time phases, model latency/throughput, top tools."""
    card = _Card(color)
    card.top(_title("perf", data), _filter_badge(data))
    card.blank()
    phases = data.phases
    if phases is not None and _phase_total(phases) > 0:
        turns = max(0, phases.turns)
        avg = phases.wall_ms / turns if turns else None
        kpis = [
            (_fmt_dur(phases.wall_ms), S_TOKENS_BIG, "agent time"),
            (_int(turns), S_SESS_BIG, "turns"),
            (_fmt_dur(avg), S_COST_BIG, "avg / turn"),
            (_int(data.total_calls), S_CACHE_BIG, "llm calls"),
        ]
        for line in _kpi_rows(kpis, (16, 16, 16, 14)):
            card.row(line)
        card.blank()
        parts = _phase_parts(phases)
        card.row(_stacked([(v, g, s) for _, _, g, s, v in parts], CONTENT))
        card.blank()
        for line in _phase_legend(phases):
            card.row(line)
    else:
        card.row([("No turn timing recorded yet", S_TITLE), (" · chat a turn to start", S_DIM)])
    card.blank()

    card.divider("latency & throughput", "● ttft p50 ━╸ p90")
    models = sorted(data.models, key=lambda m: (-m.calls, -m.total_tokens, m.model))
    colors = viz.model_colors(
        [m.model for m in sorted(data.models, key=lambda m: (-m.total_tokens, -m.calls, m.model))]
    )
    limit = 8
    shown = models[: limit if len(models) <= limit else limit - 1]
    name_w, calls_w, plot_w, dur_w, tps_w, burst_w = 14, 6, 12, 6, 5, 5
    header: Line = [*_cell("model", 2 + name_w, S_DIM), (" ", "")]
    header += [*_cell("calls", calls_w, S_DIM, "right"), (" ", "")]
    header += [*_cell("ttft", plot_w, S_DIM), (" ", "")]
    header += [*_cell("p50", dur_w, S_DIM, "right"), (" ", "")]
    header += [*_cell("p90", dur_w, S_DIM, "right"), (" ", "")]
    header += [*_cell("tok/s", tps_w, S_DIM, "right"), (" ", "")]
    header += _cell("burst", burst_w, S_DIM, "right")
    card.row(header)
    if not models:
        card.row([("no model calls recorded", S_DIM)])
    spans = [m.ttft_p90_ms or m.ttft_p50_ms or 0.0 for m in shown]
    scale = max(spans, default=0.0) * 1.0001
    for index, model in enumerate(shown):
        burst = model.burst_calls / model.calls if model.calls > 0 else None
        tps = "-" if model.tps is None else _fmt_tps(model.tps).removesuffix(" t/s")
        chip = colors.get(model.model, viz.C_OTHER)
        line: Line = [_swatch(chip, viz.PLAIN_FILLS[index % len(viz.PLAIN_FILLS)]), (" ", "")]
        line += [*_cell(_display_name(model.model), name_w, ""), (" ", "")]
        line += [*_cell(_int(model.calls), calls_w, "", "right"), (" ", "")]
        line += viz.range_plot(model.ttft_p50_ms, model.ttft_p90_ms, scale, plot_w, chip)
        has_ttft = model.ttft_p50_ms is not None
        line += [
            (" ", ""),
            *_cell(_fmt_dur(model.ttft_p50_ms), dur_w, S_CACHE if has_ttft else S_DIM, "right"),
        ]
        line += [(" ", ""), *_cell(_fmt_dur(model.ttft_p90_ms), dur_w, S_DIM, "right")]
        line += [(" ", ""), *_cell(tps, tps_w, S_COST if model.tps is not None else S_DIM, "right")]
        line += [
            (" ", ""),
            *_cell(_fmt_pct(burst), burst_w, S_PROCESS if burst else S_DIM, "right"),
        ]
        card.row(line)
    if len(models) > limit:
        card.row([(f"+ {len(models) - (limit - 1)} more models", S_DIM)])
    _service_tiers_perf_section(card, data, colors)

    if data.tools:
        divider = "top tools · unwrapped" if data.ptc is not None else "top tools · by total time"
        card.divider(divider)
        _tool_bars(card, data.tools, 5, with_max=False)
        if data.ptc is not None and data.ptc.unmeasured_nested_calls > 0:
            card.row([("* partial time · legacy multi-call sandbox cells", S_DIM)])
    card.bottom()
    return card.render(_after_notice(data.upgrade_notice))


# =============================================================================
# Tools card
# =============================================================================


def _process_waits_section(card: _Card, waits: ProcessWaitSummary) -> None:
    """Compact wait-percentile summary for blocking process-wait calls."""
    noun = "wait" if waits.waits == 1 else "waits"
    if waits.timed_waits > 0:
        total_text = _fmt_dur(waits.total_ms) + ("*" if waits.timed_waits < waits.waits else "")
        title = f"process waits · {_int(waits.waits)} {noun} · {total_text}"
    else:
        title = f"process waits · {_int(waits.waits)} {noun}"
    card.divider(title)
    if waits.timed_waits > 0:
        card.row(
            [
                ("p50 ", S_DIM),
                (_fmt_dur(waits.p50_ms), f"bold {S_PROCESS}"),
                (" · p90 ", S_DIM),
                (_fmt_dur(waits.p90_ms), S_PROCESS),
                (" · avg ", S_DIM),
                (_fmt_dur(waits.avg_ms), ""),
                (" · max ", S_DIM),
                (_fmt_dur(waits.max_ms), S_DIM),
            ]
        )
    if waits.by_tool:
        parts = [f"{_clean(name)} ({_compact(count)})" for name, count in waits.by_tool[:4]]
        if len(waits.by_tool) > 4:
            parts.append(f"+{len(waits.by_tool) - 4} more")
        card.row([(" · ".join(parts), S_DIM)])


def _tools_badge(data: UsageReportData, *, raw: bool) -> Line | None:
    if not raw:
        return _filter_badge(data)
    if data.model_filter:
        return [("▸ ", S_BADGE), (f"raw · {_clean(data.model_filter).strip()}", S_BADGE)]
    return [("▸ ", S_BADGE), ("raw", S_BADGE)]


def render_tools_card(
    data: UsageReportData,
    *,
    color: bool = True,
    max_tools: int = 12,
    raw: bool = False,
) -> str:
    """``/stats tools``: tool time, runs, process-wait stats and per-tool bars."""
    card = _Card(color)
    card.top(_title("tools", data), _tools_badge(data, raw=raw))
    card.blank()
    tools = list(data.raw_tools or data.tools) if raw else list(data.tools)
    ptc = None if raw else data.ptc
    waits = None if raw else data.process_waits
    if not tools and waits is None:
        card.row([("No tool runs recorded yet", S_TITLE), (" for this window.", S_DIM)])
        card.blank()
        card.bottom()
        return card.render(_after_notice(data.upgrade_notice))

    wall_tools = list(data.raw_tools) if data.raw_tools else tools
    wall_ms = sum(max(0.0, t.total_ms) for t in (tools if raw else wall_tools))
    tool_runs = sum(max(0, t.runs) for t in tools)
    extra_waits = waits.waits if waits is not None else 0
    runs = tool_runs + extra_waits
    wait_runs = (
        waits.waits if waits is not None else sum(max(0, t.process_wait_runs) for t in tools)
    )
    timed_runs = sum(max(0, t.effective_timed_runs) for t in tools) + (
        waits.timed_waits if waits is not None else 0
    )
    measured_ms = sum(max(0.0, t.total_ms) for t in tools) + (
        waits.total_ms if waits is not None else 0.0
    )
    avg_run_ms = (
        (wall_ms / runs if runs else None)
        if ptc is None and waits is None
        else (measured_ms / timed_runs if timed_runs else None)
    )
    kpis = [
        (_fmt_dur(wall_ms), f"bold {S_TOOLS}", "tool time"),
        (_int(runs), f"bold {S_SPEED}", "runs"),
        (_fmt_dur(avg_run_ms), S_COST_BIG, "avg / run"),
        (_fmt_pct(wait_runs / runs if runs else None), f"bold {S_PROCESS}", "process wait"),
    ]
    for line in _kpi_rows(kpis, (16, 16, 16, 14)):
        card.row(line)
    phases = data.phases
    if phases is not None and phases.wall_ms > 0:
        share = (phases.tools_ms + phases.process_ms) / phases.wall_ms
        card.blank()
        card.row(
            [
                ("tools + process = ", S_DIM),
                (_fmt_pct(share), f"bold {S_TOOLS}"),
                (" of agent time", S_DIM),
            ]
        )
    if ptc is not None:
        if phases is None or phases.wall_ms <= 0:
            card.blank()
        _ptc_summary_rows(card, ptc)

    card.blank()
    has_partial_footnote = False
    if tools:
        suffix = " · raw" if raw else (" · unwrapped" if ptc is not None else "")
        card.divider(f"top tools · {len(tools)} total{suffix}")
        _tool_bars(card, tools, max_tools, with_max=True)
        if ptc is not None and ptc.unmeasured_nested_calls > 0:
            card.row([("* partial time · legacy multi-call sandbox cells", S_DIM)])
            has_partial_footnote = True
        if ptc is not None and ptc.has_parallel_overlap:
            card.row([("▸ nested times may exceed wall time in parallel cells", S_DIM)])
    if waits is not None:
        _process_waits_section(card, waits)
        if waits.timed_waits < waits.waits and not has_partial_footnote:
            card.row([("* partial time · legacy multi-call sandbox cells", S_DIM)])
    if ptc is not None:
        _ptc_model_table(card, ptc)
    card.bottom()
    return card.render(_after_notice(data.upgrade_notice))


# =============================================================================
# Sweep summary
# =============================================================================


def _short_dir(path: str) -> str:
    parts = [p for p in re.split(r"[\\/]+", _clean(path).strip()) if p]
    if not parts:
        return "."
    tail = "/".join(parts[-2:])
    return tail if len(parts) <= 2 else f"…/{tail}"


def render_sweep_summary(summary: SweepSummary, *, color: bool = True) -> str:
    """Compact report after ``/stats sweep <dir> [--dry-run]``."""
    card = _Card(color)
    badge: Line | None = [("▸ dry run", S_WARN)] if summary.dry_run else None
    card.top(
        [
            *_brand(),
            (" sweep", S_TITLE),
            (f" · {_short_dir(summary.root_dir)}", S_DIM),
        ],
        badge,
    )
    card.blank()
    verb = "would import" if summary.dry_run else "imported"
    kpis = [
        (_count(summary.calls_imported), S_TOKENS_BIG, "calls"),
        (_compact(summary.tokens_imported), S_TOKENS_BIG, "tokens"),
        (
            _fmt_cost(summary.cost_usd_imported)
            if summary.unpriced_calls_imported == 0
            else "~" + _fmt_cost(summary.cost_usd_imported),
            S_COST_BIG,
            "est. cost",
        ),
        (_int(summary.sessions_scanned), S_SESS_BIG, "sessions"),
    ]
    for line in _kpi_rows(kpis, (16, 16, 16, 14)):
        card.row(line)
    card.blank()

    scanned = max(0, summary.files_scanned)
    unchanged = max(0, summary.files_skipped_unchanged)
    invalid = max(0, summary.files_skipped_invalid)
    fresh = max(0, scanned - unchanged - invalid)
    card.row(
        [("files ", S_DIM)],
        _stacked(
            [(fresh, "█", S_OK), (unchanged, "░", S_DIM), (invalid, "▒", S_WARN)],
            CONTENT - 6,
        ),
    )
    card.row(
        [("█ ", S_OK), (_int(fresh), ""), (" read", S_DIM), ("   ░ ", S_DIM), (_int(unchanged), "")]
        + [(" unchanged", S_DIM), ("   ▒ ", S_WARN), (_int(invalid), ""), (" invalid", S_DIM)]
        + [("   of ", S_DIM), (_int(scanned), ""), (" files", S_DIM)]
    )
    card.blank()
    card.row(
        [
            ("▸ ", S_TOKENS),
            (f"{verb} ", S_DIM),
            (_int(summary.calls_imported), ""),
            (" calls · ", S_DIM),
        ]
        + [(_int(summary.turns_imported), ""), (" turns · ", S_DIM)]
        + [(_int(summary.tool_runs_imported), ""), (" tool runs", S_DIM)]
    )
    if summary.calls_duplicate:
        card.row(
            [
                ("▸ ", S_DIM),
                (_int(summary.calls_duplicate), ""),
                (" duplicate calls skipped", S_DIM),
            ]
        )
    if summary.unpriced_calls_imported:
        card.row(
            [
                ("▸ ", S_WARN),
                (_int(summary.unpriced_calls_imported), S_WARN),
                (" calls without pricing", S_DIM),
            ]
        )
    if summary.include_all:
        card.row([("▸ ", S_DIM), ("all subtrees included (--all)", S_DIM)])
    elif summary.sessions_scanned == 0:
        card.row(
            [
                ("▸ ", S_DIM),
                ("scanned .fast-agent/sessions only · pass --all for all trees", S_DIM),
            ]
        )
    if summary.dry_run:
        card.row([("▸ ", S_WARN), ("dry run · nothing written · rerun without --dry-run", S_DIM)])
    card.blank()
    card.bottom()
    return card.render()


# =============================================================================
# Pricing status
# =============================================================================


def _fmt_epoch(value: float | None) -> str:
    if value is None:
        return "never"
    try:
        moment = datetime.fromtimestamp(float(value), tz=UTC)
    except (OverflowError, OSError, ValueError):
        return "unknown"
    return moment.strftime("%Y-%m-%d %H:%M UTC")


def _short_url(url: str) -> str:
    text = _clean(url).strip()
    text = re.sub(r"^[a-z]+://", "", text)
    if len(text) <= 48:
        return text
    host, _, rest = text.partition("/")
    tail = rest.rsplit("/", 1)[-1]
    short = f"{host}/…/{tail}"
    return short if len(short) <= 48 else text[:47] + ELLIPSIS


def render_pricing_status(status: PricingUpdateStatus, *, color: bool = True) -> str:
    """Status box for ``/stats pricing [status|update]``."""
    card = _Card(color)
    if status.last_error:
        badge: Line = [("▸ error", S_ERR)]
    elif status.upgrade_required:
        badge = [("▸ upgrade required", S_WARN)]
    else:
        badge = [("▸ up to date", S_OK)]
    card.top([*_brand(), (" pricing", S_TITLE)], badge)
    card.blank()
    label_w = 10

    def kv(label: str, value: Line) -> None:
        card.row([*_cell(label, label_w, S_DIM)], value)

    kv(
        "active",
        [(_clean(status.active_version), "bold"), (f"  ({_clean(status.active_source)})", S_DIM)],
    )
    kv("bundled", [(_clean(status.bundled_version), "")])
    kv(
        "cached",
        [(_clean(status.cached_version), "")] if status.cached_version else [("none", S_DIM)],
    )
    kv("checked", [(_fmt_epoch(status.last_checked_at), "" if status.last_checked_at else S_DIM)])
    if status.etag:
        kv("etag", [(_clean(status.etag), S_DIM)])
    kv("auto", [("on", S_OK)] if status.auto_update else [("off", S_DIM)])
    kv("source", [(_short_url(status.pricing_url), S_TOKENS)])
    if status.upgrade_required or status.upgrade_notice or status.last_error:
        card.blank()
    if status.upgrade_notice:
        card.wrapped(status.upgrade_notice, "", prefix="▸ ", pstyle=S_WARN)
    elif status.upgrade_required:
        card.row([("▸ ", S_WARN), ("update fast-agent to use the latest pricing schema", "")])
    if status.last_error:
        card.wrapped(f"last error: {status.last_error}", S_ERR, prefix="▸ ", pstyle=S_ERR)
    card.blank()
    card.bottom()
    return card.render()
