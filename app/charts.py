"""Small server-rendered SVG bar charts (no JS library, works offline).

Marks follow the dataviz rules used in this project: thin bars, 4px rounded data ends
anchored to the baseline, 2px gaps between adjacent bars / stacked segments, a recessive
grid, one y-axis, text in ink colors (never the series color). Every bar carries
data-tip text for the hover tooltip (see static/app.js). Time runs right-to-left (RTL UI).
"""
from __future__ import annotations

import math
from html import escape

from markupsafe import Markup

# Validated with the dataviz palette checker on white (all checks pass).
SERIES = ["#0a8f6e", "#3442b0", "#eb6834"]
INK_2 = "#4d5a55"
MUTED = "#7a8681"
GRID = "#e6eae5"


def nice_max(value: float) -> float:
    if value <= 0:
        return 1.0
    exp = 10 ** math.floor(math.log10(value))
    for step in (1, 1.2, 1.5, 2, 2.5, 3, 4, 5, 6, 8, 10):
        if value <= step * exp:
            return step * exp
    return 10 * exp


def short_money(value: float) -> str:
    value = float(value)
    if abs(value) >= 1_000_000:
        return f"{value / 1_000_000:.1f}".rstrip("0").rstrip(".") + "م"
    if abs(value) >= 1_000:
        return f"{value / 1_000:.1f}".rstrip("0").rstrip(".") + "ك"
    return f"{value:,.0f}"


def _bar_path(x: float, y: float, w: float, h: float, r: float = 4) -> str:
    """Rectangle with only the top (data-end) corners rounded."""
    if h <= 0:
        return ""
    r = min(r, w / 2, h)
    return (f"M{x:.1f},{y + h:.1f} V{y + r:.1f} Q{x:.1f},{y:.1f} {x + r:.1f},{y:.1f} "
            f"H{x + w - r:.1f} Q{x + w:.1f},{y:.1f} {x + w:.1f},{y + r:.1f} V{y + h:.1f} Z")


def bar_chart(labels: list[str], series: list[dict], *, stacked: bool = False, height: int = 230,
              width: int = 640, money: bool = True, aria: str = "") -> Markup:
    """series: [{"name": str, "values": [..]}] (one value per label). Labels are oldest→newest."""
    pad_top, pad_bottom, pad_axis = 14, 34, 52
    plot_h = height - pad_top - pad_bottom
    plot_w = width - pad_axis
    n = max(len(labels), 1)
    if stacked:
        top = max((sum(s["values"][i] or 0 for s in series) for i in range(n)), default=0)
    else:
        top = max((v or 0 for s in series for v in s["values"]), default=0)
    ymax = nice_max(top)
    fmt = short_money if money else (lambda v: f"{v:,.0f}")

    out = [f'<svg viewBox="0 0 {width} {height}" class="chart" role="img" aria-label="{escape(aria)}" '
           f'preserveAspectRatio="xMidYMid meet">']
    # grid + y labels (axis sits on the right in RTL)
    for k in range(5):
        v = ymax * k / 4
        y = pad_top + plot_h - plot_h * k / 4
        out.append(f'<line x1="0" x2="{plot_w}" y1="{y:.1f}" y2="{y:.1f}" stroke="{GRID}" stroke-width="1"/>')
        out.append(f'<text x="{plot_w + 8}" y="{y + 4:.1f}" fill="{MUTED}" font-size="11" text-anchor="start">{fmt(v)}</text>')

    band = plot_w / n
    group_w = band * (0.62 if not stacked else 0.42)
    for i, label in enumerate(labels):
        # RTL: newest (last label) on the left, oldest on the right
        cx = plot_w - band * (i + 0.5)
        x0 = cx - group_w / 2
        tip_lines = [label]
        for s in series:
            v = s["values"][i] or 0
            tip_lines.append(f"{s['name']}: {v:,.2f}" if money else f"{s['name']}: {v:,.0f}")
        tip = escape(" | ".join(tip_lines))
        out.append(f'<g class="bar-group" data-tip="{tip}" tabindex="0">')
        out.append(f'<rect x="{cx - band / 2:.1f}" y="{pad_top}" width="{band:.1f}" height="{plot_h}" fill="transparent"/>')
        if stacked:
            y_cursor = pad_top + plot_h
            for si, s in enumerate(series):
                v = s["values"][i] or 0
                h = plot_h * v / ymax
                if h <= 0:
                    continue
                gap = 2 if y_cursor < pad_top + plot_h else 0
                is_top = all((series[sj]["values"][i] or 0) == 0 for sj in range(si + 1, len(series)))
                y = y_cursor - h
                hh = max(h - gap, 0.5)
                if is_top:
                    out.append(f'<path d="{_bar_path(x0, y, group_w, hh)}" fill="{SERIES[si]}"/>')
                else:
                    out.append(f'<rect x="{x0:.1f}" y="{y:.1f}" width="{group_w:.1f}" height="{hh:.1f}" fill="{SERIES[si]}"/>')
                y_cursor = y
        else:
            m = len(series)
            bw = (group_w - 2 * (m - 1)) / m
            for si, s in enumerate(series):
                v = s["values"][i] or 0
                h = plot_h * v / ymax
                # first series on the right (reading order in RTL)
                x = x0 + group_w - (si + 1) * bw - si * 2
                if h > 0:
                    out.append(f'<path d="{_bar_path(x, pad_top + plot_h - h, bw, h)}" fill="{SERIES[si]}"/>')
        out.append("</g>")
        out.append(f'<text x="{cx:.1f}" y="{height - 12}" fill="{INK_2}" font-size="12" text-anchor="middle">{escape(label)}</text>')
    out.append(f'<line x1="0" x2="{plot_w}" y1="{pad_top + plot_h}" y2="{pad_top + plot_h}" stroke="#c9d1cb" stroke-width="1"/>')
    out.append("</svg>")
    return Markup("".join(out))


def legend(names: list[str]) -> Markup:
    items = "".join(f'<span class="legend-item"><i style="background:{SERIES[i]}"></i>{escape(n)}</span>'
                    for i, n in enumerate(names))
    return Markup(f'<div class="legend">{items}</div>')
