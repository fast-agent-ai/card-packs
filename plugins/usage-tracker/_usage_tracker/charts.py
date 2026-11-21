"""Share-worthy, fixed-width terminal cards for the usage-tracker plugin.

Every card is exactly :data:`CARD_WIDTH` visible cells wide.  Lines are built
from ``(text, style)`` segments whose widths are measured *before* any Rich
markup is added, so the plain (``color=False``) and Rich (``color=True``)
renderings always contain the identical visible text: colour mode only wraps
segments in ``[style]...[/]`` tags and escapes anything Rich could mistake for
markup (``[tag]`` look-alikes, trailing backslashes, ``:emoji:`` codes).

Only single-width glyphs are used inside the frame (box drawing, shade/blocks,
``·``, ``▸``, ``…``); dynamic strings (model and tool names, notices) are
sanitised so wide or zero-width characters can never break alignment.

Privacy: cards never show file paths or session ids (the sweep summary shows
only the last two components of the swept directory).
"""

from __future__ import annotations

import math
import re
import textwrap
import unicodedata
from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

    from .models import (
        ModelUsageRow,
        PhaseBreakdown,
        PricingUpdateStatus,
        ProcessWaitSummary,
        PtcSummary,
        SweepSummary,
        ToolUsageRow,
        UsageReportData,
    )

__all__ = [
    "CARD_WIDTH",
    "BRAND",
    "render_usage_card",
    "render_share_card",
    "render_perf_card",
    "render_tools_card",
    "render_sweep_summary",
    "render_pricing_status",
]

CARD_WIDTH = 68
PAD = 2
CONTENT = CARD_WIDTH - 2 - 2 * PAD  # 62 usable cells between the side borders
LEFT_W = 24  # lower panel: heatmap column
RIGHT_W = CONTENT - LEFT_W - 2 * PAD - 1  # 33: sparkline / cache / time column
SPLIT_COL = 1 + PAD + LEFT_W + PAD  # column of the inner vertical rule (29)
SPARK_W = 30
BRAND = "fast-agent · /stats"
ELLIPSIS = "…"

# -- palette -------------------------------------------------------------------
S_BORDER = "dim"
S_DIM = "dim"
S_TITLE = "bold"
S_BRAND = "bold bright_cyan"
S_TOKENS = "bright_cyan"
S_TOKENS_BIG = "bold bright_cyan"
S_BAR = "cyan"
S_PEAK = "bold bright_cyan"
S_TRACK = "dim"
S_COST = "green"
S_COST_BIG = "bold green"
S_CACHE = "yellow"
S_CACHE_BIG = "bold yellow"
S_SESS_BIG = "bold magenta"
S_SPEED = "bright_blue"
S_WARN = "yellow"
S_ERR = "red"
S_OK = "green"
S_BADGE = "bold magenta"

PHASES: tuple[tuple[str, str, str, str], ...] = (
    # key, label, glyph, style
    ("wait", "wait", "░", "yellow"),
    ("stream", "stream", "█", "green"),
    ("tools", "tools", "▓", "blue"),
    ("process", "process", "▒", "magenta"),
    ("other", "other", "·", "dim"),
)

HEAT_GLYPHS = ("··", "░░", "▒▒", "▓▓", "██")
HEAT_STYLES = ("dim", "#155e75", "#0891b2", "#22d3ee", "bold #a5f3fc")
SPARK_BLOCKS = " ▁▂▃▄▅▆▇█"

Seg = tuple[str, str]
Line = list[Seg]


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


def _display_name(name: str) -> str:
    """Model names lose dated snapshot suffixes (``-20250929``) for display."""
    text = _clean(name).strip()
    short = _DATE_SUFFIX.sub("", text)
    return short or text


def _width(segs: Iterable[Seg]) -> int:
    return sum(len(text) for text, _ in segs)


def _fit(segs: Sequence[Seg], width: int, align: str = "left") -> Line:
    """Pad or truncate ``segs`` to exactly ``width`` cells."""
    width = max(0, width)
    total = _width(segs)
    if total > width:
        if width == 0:
            return []
        out: Line = []
        room = width - 1
        for text, style in segs:
            if room <= 0:
                break
            piece = text[:room]
            if piece:
                out.append((piece, style))
            room -= len(piece)
        # Avoid "name …": drop trailing blanks before the ellipsis.
        while out and not out[-1][0].strip():
            out.pop()
        if out:
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
        return "".join(text for text, _ in segs)
    merged: list[list[str]] = []
    for text, style in segs:
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


def _alloc(values: Sequence[float], width: int) -> list[int]:
    """Largest-remainder allocation of ``width`` cells; nonzero values get >= 1."""
    clean = [max(0.0, float(v)) if math.isfinite(v) else 0.0 for v in values]
    total = sum(clean)
    if width <= 0 or total <= 0:
        return [0 for _ in clean]
    raw = [v / total * width for v in clean]
    cells = [int(math.floor(r)) for r in raw]
    order = sorted(range(len(raw)), key=lambda i: raw[i] - cells[i], reverse=True)
    for i in order[: width - sum(cells)]:
        cells[i] += 1
    # Give visible slivers to tiny-but-present values, stealing from the largest.
    for i, v in enumerate(clean):
        if v > 0 and cells[i] == 0:
            donor = max(range(len(cells)), key=lambda j: cells[j])
            if cells[donor] > 1:
                cells[donor] -= 1
                cells[i] = 1
    return cells


def _bar(fraction: float, width: int, style: str, *, min_one: bool = True) -> Line:
    fraction = 0.0 if not math.isfinite(fraction) else max(0.0, min(1.0, fraction))
    filled = int(round(fraction * width))
    if min_one and fraction > 0 and filled == 0:
        filled = 1
    return [("█" * filled, style), ("░" * (width - filled), S_TRACK)]


def _stacked(parts: Sequence[tuple[float, str, str]], width: int) -> Line:
    """``parts`` = (value, glyph, style); empty when nothing to show."""
    cells = _alloc([value for value, _, _ in parts], width)
    if sum(cells) == 0:
        return [("·" * width, S_TRACK)]
    return [(glyph * n, style) for n, (_, glyph, style) in zip(cells, parts, strict=True) if n]


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

    def divider(self, title: str = "", right: str | None = None) -> None:
        """Full-width ├─ title ──┤ or split ├─ left ──┬─ right ──┤."""
        if right is None:
            self.lines.append(_rule("├", "┤", title, CARD_WIDTH))
            return
        left = _rule("├", "", title, SPLIT_COL)
        right_part = _rule("┬", "┤", right, CARD_WIDTH - SPLIT_COL)
        self.lines.append(left + right_part)

    def bottom(self, note: Line | None = None, split: bool = False) -> None:
        brand: Line = [("fast-agent", S_BRAND), (" · /stats", S_DIM)]
        tail: Line = [(" ", S_BORDER), *brand, (" ─╯", S_BORDER)]
        if split:
            left_len = SPLIT_COL
            right_len = CARD_WIDTH - SPLIT_COL
            left = _bottom_left(note, left_len)
            fill = right_len - 1 - _width(tail)
            self.lines.append(left + [("┴" + "─" * fill, S_BORDER)] + tail)
            return
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

    def split_rows(self, left: Sequence[Line], right: Sequence[Line]) -> None:
        for index in range(max(len(left), len(right))):
            lhs = _fit(left[index] if index < len(left) else [], LEFT_W)
            rhs = _fit(right[index] if index < len(right) else [], RIGHT_W)
            self.lines.append(
                [
                    ("│", S_BORDER),
                    (" " * PAD, ""),
                    *lhs,
                    (" " * PAD, ""),
                    ("│", S_BORDER),
                    (" " * PAD, ""),
                    *rhs,
                    (" " * PAD, ""),
                    ("│", S_BORDER),
                ]
            )

    def wrapped(self, text: str, style: str = "", prefix: str = "", pstyle: str = "") -> None:
        body = _clean(text)
        indent = " " * len(prefix)
        for i, chunk in enumerate(textwrap.wrap(body, CONTENT - len(prefix)) or [""]):
            self.row([(prefix if i == 0 else indent, pstyle), (chunk, style)])

    def render(self, after: Sequence[Line] = ()) -> str:
        return "\n".join(_render_line(line, self.color) for line in [*self.lines, *after])


def _rule(start: str, end: str, title: str, width: int) -> Line:
    """``start─ title ────end`` exactly ``width`` cells (end may be empty)."""
    inner = width - len(start) - len(end)
    title = _clean(title)
    if title:
        room = inner - 4  # "─ " + " " + at least one "─"
        label = _fit([(title, S_TITLE)], min(len(title), max(1, room)))
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
    base: Line = [("fast-agent", S_BRAND), (f" {kind}", S_TITLE)]
    if data is None:
        return [base]
    window = _clean(data.window_label).strip()
    variants: list[Line] = []
    for span in (_date_range(data), _date_range(data, short=True), ""):
        detail = " · ".join(part for part in (window, span) if part)
        variants.append(base + [(f" · {detail}", S_DIM)] if detail else list(base))
    return variants


def _filter_badge(data: UsageReportData) -> Line | None:
    if not data.model_filter:
        return None
    return [("▸ ", S_BADGE), (_clean(data.model_filter).strip(), S_BADGE)]


def _kpi_rows(items: Sequence[tuple[str, str, str]], widths: Sequence[int]) -> list[Line]:
    values: Line = []
    labels: Line = []
    for (value, style, label), width in zip(items, widths, strict=True):
        values += _cell(value, width, style)
        labels += _cell(label, width, S_DIM)
    return [values, labels]


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
# Usage card
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


def _daily_series(data: UsageReportData) -> tuple[list[int], date | None, date | None]:
    """Daily totals with gap days filled in (when dates are parseable)."""
    points: list[tuple[date, int]] = []
    for point in data.daily:
        day = _parse_date(point.date_iso)
        if day is None:
            return [max(0, int(p.total_tokens)) for p in data.daily], None, None
        points.append((day, max(0, int(point.total_tokens))))
    if not points:
        return [], None, None
    by_day: dict[date, int] = {}
    for day, tokens in points:
        by_day[day] = by_day.get(day, 0) + tokens
    start = min(by_day)
    end = max(by_day)
    window_start = _parse_date(data.start_date)
    window_end = _parse_date(data.end_date)
    if window_start is not None and window_start < start:
        start = window_start
    if window_end is not None and window_end > end:
        end = window_end
    span = (end - start).days + 1
    if span > 20_000:  # defensive: absurd ranges fall back to recorded days only
        days = sorted(by_day)
        return [by_day[d] for d in days], days[0], days[-1]
    series = [by_day.get(start + timedelta(days=i), 0) for i in range(span)]
    return series, start, end


def _sparkline(values: Sequence[int], width: int = SPARK_W) -> tuple[Line, Line, int]:
    """Two-row sparkline (16 levels). Returns (top, bottom, days_per_column)."""
    n = len(values)
    if n == 0:
        return [], [], 1
    per = max(1, math.ceil(n / width))
    buckets: list[float] = []
    for i in range(0, n, per):
        chunk = values[i : i + per]
        buckets.append(sum(chunk) / len(chunk))
    count = len(buckets)
    col = 1
    gap = 0
    if count <= width // 2:
        col = max(1, min(4, width // count))
        gap = 1 if col >= 2 else 0
    peak = max(buckets)
    peak_index = buckets.index(peak) if peak > 0 else -1
    top: Line = []
    bottom: Line = []
    for index, value in enumerate(buckets):
        bar_w = col - gap
        level = 0 if peak <= 0 or value <= 0 else max(1, math.ceil(value / peak * 16))
        style = S_PEAK if index == peak_index else S_BAR
        if level == 0:
            top.append((" " * bar_w, ""))
            bottom.append(("·" * bar_w, S_TRACK))
        else:
            hi = SPARK_BLOCKS[level - 8] if level > 8 else " "
            lo = SPARK_BLOCKS[min(level, 8)]
            top.append((hi * bar_w, style if level > 8 else ""))
            bottom.append((lo * bar_w, style))
        if gap:
            top.append((" " * gap, ""))
            bottom.append((" " * gap, ""))
    return top, bottom, per


def _heat_levels(cells: Sequence[Sequence[int]]) -> list[list[int]]:
    """Map raw counts to 0..4 blending sqrt magnitude with rank (quantile).

    The rank half keeps quiet-but-real slots visible even when one monster
    session dwarfs the rest of the week; the sqrt half keeps magnitude honest.
    """
    flat = [max(0, int(v)) for row in cells for v in row]
    nonzero = sorted(set(v for v in flat if v > 0))
    if not nonzero:
        return [[0 for _ in row] for row in cells]
    peak = nonzero[-1]
    rank = {v: (i + 1) / len(nonzero) for i, v in enumerate(nonzero)}
    out: list[list[int]] = []
    for row in cells:
        levels: list[int] = []
        for raw in row:
            v = max(0, int(raw))
            if v <= 0:
                levels.append(0)
                continue
            score = 0.5 * math.sqrt(v / peak) + 0.5 * rank[v]
            levels.append(max(1, min(4, math.ceil(score * 4 - 1e-9))))
        out.append(levels)
    return out


def _heatmap_rows(data: UsageReportData) -> list[Line]:
    rhythm = data.rhythm
    weekdays = list(rhythm.weekday_labels or ())
    defaults = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
    weekdays = [_clean(w)[:3] for w in weekdays[:7]] + list(defaults[len(weekdays[:7]) :])
    slots = [_clean(s)[:2] for s in (rhythm.slot_labels or ())[:6]]
    slots += [f"{h:02d}" for h in range(len(slots) * 4, 24, 4)]
    raw_cells = [list(row[:6]) + [0] * (6 - len(row[:6])) for row in list(rhythm.cells)[:7]]
    raw_cells += [[0] * 6 for _ in range(7 - len(raw_cells))]
    levels = _heat_levels(raw_cells)
    rows: list[Line] = [[("    ", ""), (" ".join(f"{s:<2}" for s in slots), S_DIM)]]
    for label, row in zip(weekdays, levels, strict=True):
        line: Line = [(f"{label:<3} ", S_DIM)]
        for i, level in enumerate(row):
            if i:
                line.append((" ", ""))
            line.append((HEAT_GLYPHS[level], HEAT_STYLES[level]))
        rows.append(line)
    rows.append([])
    legend: Line = [("less ", S_DIM)]
    for i, (glyph, style) in enumerate(zip(HEAT_GLYPHS, HEAT_STYLES, strict=True)):
        if i:
            legend.append((" ", ""))
        legend.append((glyph, style))
    legend.append((" more", S_DIM))
    rows.append(legend)
    return rows


def _busiest_slot(data: UsageReportData) -> str | None:
    rhythm = data.rhythm
    best = (0, -1, -1)
    for r, row in enumerate(list(rhythm.cells)[:7]):
        for c, value in enumerate(list(row)[:6]):
            if int(value) > best[0]:
                best = (int(value), r, c)
    if best[0] <= 0:
        return None
    defaults = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
    weekdays = list(rhythm.weekday_labels or defaults)
    day = _clean(weekdays[best[1]] if best[1] < len(weekdays) else defaults[best[1]])[:3]
    slots = [_clean(s)[:2] for s in (rhythm.slot_labels or ())]
    start = slots[best[2]] if best[2] < len(slots) else f"{best[2] * 4:02d}"
    end = slots[best[2] + 1] if best[2] + 1 < len(slots) else f"{(best[2] + 1) * 4:02d}"
    return f"{day} {start}-{end}"


def _right_panel(data: UsageReportData) -> list[Line]:
    rows: list[Line] = []
    series, start, end = _daily_series(data)
    top, bottom, _per = _sparkline(series)
    if series and any(series):
        rows.append(top)
        rows.append(bottom)
        spark_w = _width(bottom)
        if start is not None and end is not None:
            long_range = (end - start).days > 330
            fmt = (lambda d: d.strftime("%b %Y")) if long_range else _fmt_day
            first, last = fmt(start), fmt(end)
            if start == end:
                axis: Line = [(first, S_DIM)]
            elif spark_w >= len(first) + len(last) + 1:
                axis = [
                    (first, S_DIM),
                    (" " * (spark_w - len(first) - len(last)), ""),
                    (last, S_DIM),
                ]
            else:
                axis = [(f"{first} - {last}", S_DIM)]
        else:
            axis = [(f"{len(series)} days", S_DIM)]
        rows.append(axis)
        peak = max(series)
        active = [v for v in series if v > 0]
        avg = sum(active) / len(active) if active else 0
        stat: Line = [("peak ", S_DIM), (_compact(peak), S_TOKENS)]
        if start is not None:
            peak_day = start + timedelta(days=series.index(peak))
            stat.append((f" {_fmt_day(peak_day)}", S_DIM))
        stat += [(" · avg ", S_DIM), (_compact(avg), S_TOKENS)]
        rows.append(stat)
    else:
        rows += [[("no daily activity", S_DIM)], [], [], []]

    hit = data.cache_hit_rate
    pct = _fmt_pct(hit)
    rows.append([])
    rows.append(
        [("cache ", S_DIM), *_bar(hit or 0.0, RIGHT_W - 6 - 5, S_CACHE), (f"{pct:>5}", S_CACHE)]
    )

    phases = data.phases
    if phases is not None and _phase_total(phases) > 0:
        parts = _phase_parts(phases)
        rows.append(
            [("time  ", S_DIM), *_stacked([(v, g, s) for _, _, g, s, v in parts], RIGHT_W - 6)]
        )
        shown = [p for p in parts if p[4] > 0]
        total = sum(p[4] for p in parts)
        items: list[Line] = []
        for key, label, glyph, style, value in shown:
            short = "proc" if key == "process" else label
            items.append(
                [(glyph, style), (f" {short:<7}", S_DIM), (f"{_fmt_pct(value / total):>4}", style)]
            )
        for i in range(0, min(len(items), 4), 2):
            pair = items[i : i + 2]
            line: Line = _fit(pair[0], 16)
            if len(pair) > 1:
                line += [(" ", ""), *_fit(pair[1], 16)]
            rows.append(line)
        if len(items) > 4:
            rows.append(_fit(items[4], 16))
    else:
        busiest = _busiest_slot(data)
        rows.append([])
        if busiest:
            rows.append([("busiest ", S_DIM), (busiest, S_TOKENS)])
        window_days = len(series) if series else 0
        if window_days:
            rows.append(
                [
                    ("active  ", S_DIM),
                    (f"{data.active_days}", S_TOKENS),
                    (f" of {window_days} days", S_DIM),
                ]
            )
    return rows


def _phase_total(phases: PhaseBreakdown) -> float:
    return sum(max(0.0, float(getattr(phases, f"{key}_ms") or 0.0)) for key, *_ in PHASES)


def _phase_parts(phases: PhaseBreakdown) -> list[tuple[str, str, str, str, float]]:
    out = []
    for key, label, glyph, style in PHASES:
        value = float(getattr(phases, f"{key}_ms") or 0.0)
        out.append((key, label, glyph, style, max(0.0, value if math.isfinite(value) else 0.0)))
    return out


class _ModelEntry:
    __slots__ = ("name", "tokens", "cost", "priced", "unpriced", "tps", "cache", "other")

    def __init__(self, rows: Sequence[ModelUsageRow], name: str, other: bool) -> None:
        self.name = name
        self.other = other
        self.tokens = sum(r.total_tokens for r in rows)
        self.cost = sum(r.cost_usd for r in rows)
        self.priced = sum(r.priced_calls for r in rows)
        self.unpriced = sum(r.unpriced_calls for r in rows)
        weighted = [(r.tps, max(1, r.calls)) for r in rows if r.tps is not None]
        weight = sum(w for _, w in weighted)
        self.tps = sum(t * w for t, w in weighted) / weight if weighted and weight else None
        inp = sum(r.input_tokens for r in rows)
        self.cache = sum(r.cache_read_tokens for r in rows) / inp if inp > 0 else None


def _model_entries(data: UsageReportData, max_models: int) -> list[_ModelEntry]:
    models = sorted(data.models, key=lambda m: (-m.total_tokens, -m.calls, m.model))
    max_models = max(1, max_models)
    if len(models) <= max_models:
        return [_ModelEntry([m], m.model, False) for m in models]
    head = models[: max_models - 1]
    tail = models[max_models - 1 :]
    entries = [_ModelEntry([m], m.model, False) for m in head]
    entries.append(_ModelEntry(tail, f"other ({len(tail)})", True))
    return entries


def _models_section(card: _Card, data: UsageReportData, show_cost: bool, max_models: int) -> None:
    entries = _model_entries(data, max_models)
    if not entries:
        card.row([("no per-model breakdown recorded", S_DIM)])
        return
    total = sum(e.tokens for e in entries) or 1
    has_speed = any(e.tps is not None for e in entries)
    name_w, share_w, tok_w = 18, 5, 7
    # (header, width, renderer)
    extra: list[tuple[str, int, str]] = []
    if show_cost:
        extra.append(("cost", 8, "cost"))
        extra.append(("speed", 8, "speed") if has_speed else ("cache", 8, "cache"))
    else:
        extra.append(("cache", 5, "cache"))
        if has_speed:
            extra.append(("speed", 8, "speed"))
    fixed = name_w + share_w + tok_w + sum(w for _, w, _ in extra)
    seps = 3 + len(extra)
    bar_w = CONTENT - fixed - seps

    header: Line = [*_cell("model", name_w, S_DIM), (" ", ""), (" " * bar_w, "")]
    header += [(" ", ""), *_cell("share", share_w, S_DIM, "right")]
    header += [(" ", ""), *_cell("tokens", tok_w, S_DIM, "right")]
    for label, width, _ in extra:
        header += [(" ", ""), *_cell(label, width, S_DIM, "right")]
    card.row(header)

    for index, entry in enumerate(entries):
        share = entry.tokens / total
        name_style = S_DIM if entry.other else ("bold" if index == 0 else "")
        line: Line = [*_cell(_display_name(entry.name), name_w, name_style), (" ", "")]
        line += _bar(share, bar_w, S_DIM if entry.other else S_BAR)
        line += [(" ", ""), *_cell(_fmt_pct(share), share_w, S_TOKENS, "right")]
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
            else:
                text = _fmt_pct(entry.cache)
                style = S_CACHE if entry.cache is not None else S_DIM
            line += [(" ", ""), *_cell(text, width, style, "right")]
        card.row(line)


def _usage_card(
    data: UsageReportData, color: bool, show_cost: bool, max_models: int
) -> _Card | None:
    if data.total_calls <= 0:
        return None
    card = _Card(color)
    card.top(_title("usage", data), _filter_badge(data))
    card.blank()

    if show_cost:
        cost_text, cost_style = _cost_display(data)
        second = (cost_text, cost_style, "est. cost")
    else:
        second = (_count(data.total_calls), S_COST_BIG, "llm calls")
    kpis = [
        (_compact(data.total_tokens), S_TOKENS_BIG, "tokens"),
        second,
        (_fmt_pct(data.cache_hit_rate), S_CACHE_BIG, "cache hit"),
        (_int(data.total_sessions), S_SESS_BIG, "sessions"),
    ]
    for line in _kpi_rows(kpis, (16, 16, 16, 14)):
        card.row(line)
    card.blank()

    card.row(
        [("in ", S_DIM), (_compact(data.input_tokens), S_TOKENS)]
        + [(" (", S_DIM), (_compact(data.cache_read_tokens), S_CACHE), (" cached)", S_DIM)]
        + [("  ·  ", S_DIM), ("out ", S_DIM), (_compact(data.output_tokens), S_TOKENS)]
        + [(" (", S_DIM), (_compact(data.reasoning_tokens), S_TOKENS), (" reasoning)", S_DIM)]
    )
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

    card.divider("models")
    _models_section(card, data, show_cost, max_models)

    tz = _clean(data.rhythm.timezone_label).strip() or "UTC"
    series_len = len(_daily_series(data)[0])
    per = max(1, math.ceil(series_len / SPARK_W)) if series_len else 1
    right_title = "daily tokens" + (f" · {per}d per bar" if per > 1 else "")
    card.divider(f"rhythm · {tz}", right=right_title)
    card.split_rows(_heatmap_rows(data), _right_panel(data))

    note: Line | None = None
    if show_cost and data.pricing_version and not data.unpriced_calls:
        note = [(f"pricing {_clean(data.pricing_version)}", S_DIM)]
    card.bottom(note, split=True)
    return card


def render_usage_card(
    data: UsageReportData,
    *,
    color: bool = True,
    show_cost: bool = True,
    max_models: int = 6,
) -> str:
    """Main ``/stats`` overview card (68 columns)."""
    card = _usage_card(data, color, show_cost, max_models)
    if card is None:
        return _empty_usage_card(data, color)
    return card.render(_after_notice(data.upgrade_notice))


def render_share_card(data: UsageReportData, *, show_cost: bool = True) -> str:
    """Plain card in a ```text fence, ready to paste into Discord/Slack/GitHub/X."""
    card = _usage_card(data, False, show_cost, 6)
    if card is None:
        body = _empty_usage_card(_without_notice(data), False)
    else:
        body = card.render()
    return f"```text\n{body}\n```"


def _without_notice(data: UsageReportData) -> UsageReportData:
    from dataclasses import replace

    return replace(data, upgrade_notice=None)


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
                (glyph, style),
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
            line += _bar(total / peak, bar_w, S_DIM if other else "blue")
            total_text = _fmt_dur(total) + ("*" if timed < runs else "")
            total_style = ""
            avg_text = _fmt_dur(avg)
            max_text = _fmt_dur(mx)
        line += [(" ", ""), *_cell(_compact(runs), runs_w, "bright_blue", "right")]
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
            (_int(ptc.nested_calls), "bright_blue"),
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
            (_compact(item.cells), "bright_blue"),
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

    card.divider("latency & throughput")
    widths = (20, 6, 9, 9, 7, 6)
    heads = ("model", "calls", "ttft p50", "ttft p90", "tok/s", "burst")
    header: Line = []
    for i, (head, width) in enumerate(zip(heads, widths, strict=True)):
        if i:
            header.append((" ", ""))
        header += _cell(head, width, S_DIM, "left" if i == 0 else "right")
    card.row(header)
    models = sorted(data.models, key=lambda m: (-m.calls, -m.total_tokens, m.model))
    if not models:
        card.row([("no model calls recorded", S_DIM)])
    limit = 8
    for model in models[: limit if len(models) <= limit else limit - 1]:
        burst = model.burst_calls / model.calls if model.calls > 0 else None
        tps = "-" if model.tps is None else _fmt_tps(model.tps).removesuffix(" t/s")
        cells = (
            (_display_name(model.model), ""),
            (_int(model.calls), ""),
            (_fmt_dur(model.ttft_p50_ms), S_WARN if model.ttft_p50_ms is not None else S_DIM),
            (_fmt_dur(model.ttft_p90_ms), S_WARN if model.ttft_p90_ms is not None else S_DIM),
            (tps, S_COST if model.tps is not None else S_DIM),
            (_fmt_pct(burst), "magenta" if burst else S_DIM),
        )
        line: Line = []
        for i, ((text, style), width) in enumerate(zip(cells, widths, strict=True)):
            if i:
                line.append((" ", ""))
            line += _cell(text, width, style, "left" if i == 0 else "right")
        card.row(line)
    if len(models) > limit:
        card.row([(f"+ {len(models) - (limit - 1)} more models", S_DIM)])

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
                (_fmt_dur(waits.p50_ms), "bold magenta"),
                (" · p90 ", S_DIM),
                (_fmt_dur(waits.p90_ms), "magenta"),
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
        (_fmt_dur(wall_ms), "bold blue", "tool time"),
        (_int(runs), "bold bright_blue", "runs"),
        (_fmt_dur(avg_run_ms), S_COST_BIG, "avg / run"),
        (_fmt_pct(wait_runs / runs if runs else None), "bold magenta", "process wait"),
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
                (_fmt_pct(share), "bold blue"),
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
            ("fast-agent", S_BRAND),
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
    card.top([("fast-agent", S_BRAND), (" pricing", S_TITLE)], badge)
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
