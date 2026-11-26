"""Drawing primitives for the usage-tracker cards.

Everything here returns *lines of segments*.  A segment is ``(text, style)``
or ``(text, style, plain_text)``:

- ``style`` is a Rich style string built only from the 16 standard ANSI colours
  and attributes (``"bold bright_cyan"``, ``"red on blue"``), so every card
  follows the terminal's own theme.  No 256-colour or RGB values, ever.
- ``plain_text`` (same cell width as ``text``) is what the uncoloured rendering
  shows instead.  Colour charts paint with solid blocks and tell series apart
  by hue; the plain rendering (share cards, ``--plain``, non-TUI clients) has no
  hue, so series get distinct textures (``█ ▓ ▒ ░ # = +``, and ``:`` for "other") instead.

Sub-cell precision comes from the eighth-block glyphs: a cell that straddles
two series is drawn as the lower series' partial block on the upper series'
background colour, so stacked charts resolve 8 steps per text row (or column).
"""

from __future__ import annotations

import math
import re
import zlib
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence

Seg = tuple[str, ...]
Line = list[Seg]

V_EIGHTHS = " ▁▂▃▄▅▆▇█"
H_EIGHTHS = " ▏▎▍▌▋▊▉█"
PLAIN_FILLS = "█▓▒░#=+"
PLAIN_OTHER = ":"
INK = frozenset(V_EIGHTHS[1:] + H_EIGHTHS[1:] + PLAIN_FILLS + PLAIN_OTHER + "■")
"""Glyphs that may differ between the colour and plain renderings of one cell."""

# -- palette -------------------------------------------------------------------
# Standard ANSI colour names only: the terminal theme decides the actual shades.
C_TOKENS = "bright_cyan"
C_COST = "green"
C_CACHE = "yellow"
C_SESSIONS = "magenta"
C_SPEED = "bright_blue"
C_TOOLS = "blue"
C_PROCESS = "magenta"
C_WRITE = "bright_red"
C_ANSWER = "green"
C_REASON = "bright_magenta"
C_OTHER = "bright_black"
C_BRAND = "bold bright_cyan"
SERIES_COLORS = (
    "red", "green", "blue", "yellow", "magenta", "cyan",
    "bright_red", "bright_green", "bright_blue", "bright_yellow", "bright_magenta", "bright_cyan",
)  # fmt: skip
MACHINE_COLORS = ("bright_cyan", "bright_magenta", "yellow", "green", "red", "blue", "cyan")
ANSI_COLORS = frozenset(
    name
    for base in ("black", "red", "green", "yellow", "blue", "magenta", "cyan", "white")
    for name in (base, f"bright_{base}")
)
ANSI_ATTRIBUTES = frozenset({"bold", "dim", "on"})


# -- model colours ---------------------------------------------------------------

# Preferred colours per model family, first choice first.
_FAMILIES: tuple[tuple[re.Pattern[str], tuple[str, ...]], ...] = (
    (re.compile(r"claude|opus|sonnet|haiku"), ("red", "bright_red", "bright_yellow")),
    (
        re.compile(r"gpt|codex|davinci|chatgpt|(?:^|[^a-z0-9])o[134](?:-|$)"),
        ("green", "bright_green", "cyan"),
    ),
    (re.compile(r"gemini|gemma"), ("blue", "bright_blue")),
    (re.compile(r"grok"), ("magenta", "bright_magenta")),
    (re.compile(r"deepseek"), ("bright_blue", "blue")),
    (re.compile(r"kimi|moonshot"), ("bright_magenta", "magenta")),
    (re.compile(r"glm|zhipu"), ("cyan", "bright_cyan")),
    (re.compile(r"qwen|qwq"), ("magenta", "bright_magenta")),
    (re.compile(r"llama|muse"), ("bright_cyan", "cyan")),
    (re.compile(r"mistral|codestral|devstral|magistral|pixtral"), ("yellow", "bright_yellow")),
    (re.compile(r"hunyuan|\bhy\d|tencent"), ("bright_yellow", "yellow")),
    (re.compile(r"minimax"), ("bright_red", "red")),
)


def _family_colors(name: str) -> tuple[str, ...]:
    low = name.casefold()
    for pattern, colors in _FAMILIES:
        if pattern.search(low):
            return colors
    stem = re.match(r"[a-z]+", low.rsplit("/", 1)[-1])
    key = stem.group(0) if stem else low
    return (SERIES_COLORS[zlib.crc32(key.encode("utf-8")) % len(SERIES_COLORS)],)


def model_colors(names: Sequence[str]) -> dict[str, str]:
    """ANSI colours for model names (given most-used first).

    A model takes its family's colour when it is free (Claude red, GPT green,
    Gemini blue, ...), so the leading model of each family looks the same on
    every card; siblings and clashes take the family's next choice, then any
    unused colour.  Beyond twelve models colours repeat.
    """
    colors: dict[str, str] = {}
    used: set[str] = set()
    for name in names:
        if name in colors:
            continue
        preferred = _family_colors(name)
        choice = next(
            (color for color in (*preferred, *SERIES_COLORS) if color not in used),
            preferred[len(colors) % len(preferred)],
        )
        used.add(choice)
        colors[name] = choice
    return colors


# -- model families --------------------------------------------------------------

_ORG_PREFIX = re.compile(r"^[A-Za-z0-9_.-]+/(?=[A-Za-z0-9])")
_CLAUDE_TIERS = frozenset({"opus", "sonnet", "haiku"})


def model_family(name: str) -> tuple[str, bool]:
    """``(family, has_minor_version)`` for a model name.

    The family is the vendor's model line plus its major version when the name
    leads with one: ``gemini-4-neon`` -> ``gemini-4``, ``gpt-6.1-sol`` ->
    ``gpt-6`` (with a minor version), ``claude-opus-5.5`` -> ``claude``.
    """
    text = _ORG_PREFIX.sub("", name.strip())
    tokens = [token for token in re.split(r"[-_:@/ ]+", text) if token]
    if not tokens:
        return name.strip() or "unknown", False
    head = tokens[0]
    if head.casefold() in _CLAUDE_TIERS:
        return "claude", False
    if len(tokens) > 1 and re.fullmatch(r"\d+(?:\.\d+)*", tokens[1]):
        major, _, minor = tokens[1].partition(".")
        return f"{head}-{major}", bool(minor)
    return head, False


def family_labels(names: Sequence[str]) -> dict[str, str]:
    """Display label of each model's family; ``gpt-6.*`` when minor versions are merged."""
    families: dict[str, list[str]] = {}
    minors: set[str] = set()
    keys: dict[str, str] = {}
    for name in names:
        family, minor = model_family(name)
        key = family.casefold()
        families.setdefault(key, []).append(family)
        keys[name] = key
        if minor:
            minors.add(key)
    labels = {
        key: members[0] + (".*" if key in minors else "") for key, members in families.items()
    }
    return {name: labels[key] for name, key in keys.items()}


# -- geometry ------------------------------------------------------------------


def alloc(values: Sequence[float], width: int) -> list[int]:
    """Largest-remainder split of ``width`` units; nonzero values get >= 1."""
    clean = [max(0.0, float(v)) if math.isfinite(v) else 0.0 for v in values]
    total = sum(clean)
    if width <= 0 or total <= 0:
        return [0 for _ in clean]
    raw = [v / total * width for v in clean]
    cells = [math.floor(r) for r in raw]
    order = sorted(range(len(raw)), key=lambda i: raw[i] - cells[i], reverse=True)
    for i in order[: width - sum(cells)]:
        cells[i] += 1
    for i, v in enumerate(clean):
        if v > 0 and cells[i] == 0:
            donor = max(range(len(cells)), key=lambda j: cells[j])
            if cells[donor] > 1:
                cells[donor] -= 1
                cells[i] = 1
    return cells


def _spans(units: Sequence[int]) -> list[tuple[int, int, int]]:
    """(start, end, index) for each nonzero run of ``units`` laid end to end."""
    out: list[tuple[int, int, int]] = []
    cursor = 0
    for index, size in enumerate(units):
        if size > 0:
            out.append((cursor, cursor + size, index))
            cursor += size
    return out


def _cell(
    spans: Sequence[tuple[int, int, int]], lo: int
) -> tuple[int, int | None, int | None, int | None]:
    """Resolve one 8-unit cell starting at ``lo``.

    Returns ``(split, first, second, dominant)``: ``first`` fills the first
    ``split`` eighths and ``second`` the rest (``None`` = empty).
    """
    hi = lo + 8
    inside = [
        (min(end, hi) - max(start, lo), max(start, lo), min(end, hi), index)
        for start, end, index in spans
        if start < hi and end > lo
    ]
    if not inside:
        return 0, None, None, None
    filled_to = max(end for _, _, end, _ in inside)
    dominant = max(inside, key=lambda item: (item[0], -item[1]))[3]
    if len(inside) == 1 and inside[0][0] == 8:
        return 8, inside[0][3], inside[0][3], dominant
    ranked = sorted(inside, key=lambda item: (-item[0], item[1]))
    if filled_to < hi and (len(ranked) == 1 or hi - filled_to >= ranked[1][0]):
        # The empty remainder outweighs any second series: series, then nothing.
        return max(1, min(7, filled_to - lo)), ranked[0][3], None, dominant
    first, second = sorted(ranked[:2], key=lambda item: item[1])
    boundary = round((first[2] + second[1]) / 2)
    return max(1, min(7, boundary - lo)), first[3], second[3], dominant


# -- horizontal bars -------------------------------------------------------------


def line_bar(
    fraction: float,
    width: int,
    color: str,
    *,
    track: str = "dim",
    min_one: bool = True,
) -> Line:
    """Thin ``━━━━╸────`` bar (identical glyphs in colour and plain)."""
    fraction = 0.0 if not math.isfinite(fraction) else max(0.0, min(1.0, fraction))
    halves = round(fraction * width * 2)
    if min_one and fraction > 0 and halves == 0:
        halves = 1
    full, half = divmod(halves, 2)
    out: Line = []
    if full:
        out.append(("━" * full, color))
    if half:
        out.append(("╸", color))
    rest = width - full - half
    if rest > 0:
        out.append(("─" * rest, track))
    return out


def stacked_bar(parts: Sequence[tuple[float, str, str]], width: int, empty: str = "dim") -> Line:
    """Solid horizontal stack; ``parts`` = (value, colour, plain glyph)."""
    units = alloc([value for value, _, _ in parts], width * 8)
    if sum(units) == 0:
        return [("·" * width, empty)]
    spans = _spans(units)
    out: Line = []
    for column in range(width):
        split, first, second, dominant = _cell(spans, column * 8)
        plain = parts[dominant][2] if dominant is not None else " "
        if first is None:
            out.append((" ", "", " "))
        elif split == 8 or first == second:
            out.append(("█", parts[first][1], plain))
        elif second is None:
            out.append((H_EIGHTHS[split], parts[first][1], plain))
        else:
            out.append((H_EIGHTHS[split], f"{parts[first][1]} on {parts[second][1]}", plain))
    return out


# -- vertical stacks -------------------------------------------------------------


def stack_columns(
    columns: Sequence[Sequence[tuple[float, str, str]]],
    height: int,
    *,
    col_width: int | Sequence[int] = 1,
    gap: int = 0,
    scale: float | None = None,
    empty: str = "dim",
) -> list[Line]:
    """Stacked vertical bars, top row first.

    Each column is a bottom-to-top list of ``(value, colour, plain glyph)``.
    ``scale=None`` draws every column full height (a 100% share chart);
    otherwise heights are relative to ``scale`` (the tallest column's total).
    ``col_width`` is one width for all columns or a width per column.
    """
    widths = [col_width] * len(columns) if isinstance(col_width, int) else list(col_width)
    resolved: list[list[tuple[int, int | None, int | None, int | None]]] = []
    for column in columns:
        total = sum(max(0.0, value) for value, _, _ in column)
        if total <= 0:
            resolved.append([(0, None, None, None)] * height)
            continue
        if scale is None:
            fill = height * 8
        else:
            fill = max(1, min(height * 8, round(total / scale * height * 8))) if scale > 0 else 0
        spans = _spans(alloc([value for value, _, _ in column], fill))
        resolved.append([_cell(spans, row * 8) for row in range(height)])

    lines: list[Line] = []
    for row in range(height - 1, -1, -1):
        line: Line = []
        for index, column in enumerate(columns):
            width = widths[index]
            split, first, second, dominant = resolved[index][row]
            if first is None:
                idle = row == 0 and not any(cell[1] is not None for cell in resolved[index])
                line.append((("·" if idle else " ") * width, empty if idle else ""))
            else:
                # Plain: a texture when the cell is at least half full (always on the baseline).
                covered = 8 if second is not None else split
                show = covered >= 4 or row == 0
                plain = (column[dominant][2] if show and dominant is not None else " ") * width
                if split == 8 or first == second:
                    line.append(("█" * width, column[first][1], plain))
                elif second is None:
                    line.append((V_EIGHTHS[split] * width, column[first][1], plain))
                else:
                    style = f"{column[first][1]} on {column[second][1]}"
                    line.append((V_EIGHTHS[split] * width, style, plain))
            if gap and index + 1 < len(columns):
                line.append((" " * gap, ""))
        lines.append(line)
    return lines


def column_layout(count: int, width: int, max_col: int = 6) -> tuple[list[int], int]:
    """``(column widths, gap)`` fitting ``count`` columns into ``width`` cells.

    Few columns are equal bars with gaps.  Many columns touch (a filled area)
    and share the whole width, so some are one cell wider than others.
    """
    if count <= 0:
        return [], 0
    gap = 2 if count <= 8 else (1 if count <= 20 else 0)
    col = (width + gap) // count - gap
    while col < 1 and gap > 0:
        gap -= 1
        col = (width + gap) // count - gap
    if gap > 0 or col >= max_col or col < 1:
        return [max(1, min(max_col, col))] * count, gap
    span = min(width, count * max_col)
    return [(i + 1) * span // count - i * span // count for i in range(count)], 0


# -- small charts ----------------------------------------------------------------


def resample(values: Sequence[float], width: int) -> list[float]:
    """Stretch or average ``values`` onto exactly ``width`` points."""
    n = len(values)
    if n == 0 or width <= 0:
        return []
    if n >= width:
        out = []
        for i in range(width):
            lo = i * n // width
            hi = max(lo + 1, (i + 1) * n // width)
            chunk = values[lo:hi]
            out.append(sum(chunk) / len(chunk))
        return out
    return [values[min(n - 1, i * n // width)] for i in range(width)]


def spark(
    values: Sequence[float],
    width: int,
    color: str,
    *,
    floor: str = "dim",
    peak: float | None = None,
) -> Line:
    """One-row sparkline; ``·`` where idle.  ``peak`` fixes the scale (default: the max)."""
    points = resample([max(0.0, float(v)) for v in values], width)
    if not points:
        return [(" " * width, "")]
    peak = max(points) if peak is None else peak
    out: Line = []
    for value in points:
        if peak <= 0 or value <= 0:
            out.append(("·", floor))
        else:
            out.append((V_EIGHTHS[max(1, min(8, math.ceil(value / peak * 8)))], color))
    return out


def heat_levels(cells: Sequence[Sequence[float]], steps: int = 4) -> list[list[int]]:
    """Map raw counts to ``0..steps``, blending sqrt magnitude with rank.

    The rank half keeps quiet-but-real cells visible even when one monster
    session dwarfs the rest; the sqrt half keeps magnitude honest.
    """
    flat = [max(0, int(v)) for row in cells for v in row]
    nonzero = sorted({v for v in flat if v > 0})
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
            levels.append(max(1, min(steps, math.ceil(score * steps - 1e-9))))
        out.append(levels)
    return out


HEAT_STYLES = ("dim cyan", "cyan", "bright_cyan", "bold bright_white")
HEAT_GLYPHS = "░▒▓█"
HEAT_STEPS = len(HEAT_STYLES)
_HEAT_PLAIN = HEAT_GLYPHS


def heat_cell(level: int, width: int = 1, glyph: str | None = None) -> Seg:
    """One heatmap cell for ``level`` in ``0..HEAT_STEPS``."""
    if level <= 0:
        return ("·" * width, "dim")
    level = min(level, HEAT_STEPS)
    ch = HEAT_GLYPHS[level - 1] if glyph is None else glyph
    return (ch * width, HEAT_STYLES[level - 1], _HEAT_PLAIN[level - 1] * width)


def range_plot(low: float | None, high: float | None, scale: float, width: int, color: str) -> Line:
    """``··●━━━╸···``: a marker at ``low`` with a whisker out to ``high``."""
    if low is None or scale <= 0 or width <= 0:
        return [("·" * width, "dim")]
    start = max(0, min(width - 1, int(low / scale * width)))
    end = start if high is None else max(start, min(width - 1, int(high / scale * width)))
    out: Line = []
    if start:
        out.append(("·" * start, "dim"))
    out.append(("●", f"bold {color}"))
    if end > start:
        if end - start > 1:
            out.append(("━" * (end - start - 1), color))
        out.append(("╸", color))
    if width - end - 1 > 0:
        out.append(("·" * (width - end - 1), "dim"))
    return out
