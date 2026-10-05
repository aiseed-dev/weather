# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Yasuhiro / AIseed

"""Figures for JMA daily observation records (the 観測 view).

Two figures, both returned as PNG bytes for ``ft.Image`` and re-rendered
at export DPI for saving (never the screen figure reused):

* ``render_ranking`` — a ranked horizontal bar chart (stations meeting a
  condition on a day, or a period statistic). The rank and the value
  carry the information; one restrained colour per element family.
* ``render_station_series`` — one station's daily max / min / mean
  temperature over a period, with daily precipitation underneath.

Every figure carries the shared footer (source, observed period,
attribution) and the same provenance as PNG text chunks.
"""

from __future__ import annotations

import io
from dataclasses import dataclass
from datetime import UTC, date, datetime

import matplotlib.dates as mdates
import numpy as np
from matplotlib.backends.backend_agg import FigureCanvasAgg
from matplotlib.figure import Figure

from aiseed_weather.figures._fonts import configure_cjk_font
from aiseed_weather.figures.footer import apply_footer

configure_cjk_font()

_BG = "#f7f7f5"
_AXIS_FG = "#202428"
_GRID = "#d8d8d4"
_WARM = "#b5533c"          # ≥ on temperature, maxima
_COOL = "#3d6fa6"          # < on temperature, minima, precipitation
_SUN = "#c08a1e"
_MEAN = "#6b7078"


@dataclass(frozen=True)
class Provenance:
    data_source: str
    period_id: str             # "2018-07-23" or "2018-07-01..2018-07-31"
    license_text: str

    def png_metadata(self, title: str) -> dict[str, str]:
        return {
            "Title": title,
            "Software": "AIseed Weather v0.1 (https://aiseed.dev)",
            "Source": self.data_source,
            "Copyright": self.license_text,
            "Description": f"Observed period {self.period_id}",
            "Creation Time": datetime.now(UTC).isoformat(timespec="seconds"),
        }


def bar_colour(element: str, descending: bool) -> str:
    if element == "sun":
        return _SUN
    if element == "precip":
        return _COOL
    return _WARM if descending else _COOL


def _to_png(fig: Figure, prov: Provenance, title: str, dpi: int) -> bytes:
    FigureCanvasAgg(fig)
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=dpi, facecolor=fig.get_facecolor(),
                metadata=prov.png_metadata(title))
    return buf.getvalue()


def render_ranking(entries: list[tuple[int, str, float, bool]], *, title: str, subtitle: str,
                   unit: str, colour: str, prov: Provenance, value_format: str = "{:.1f}",
                   zero_based: bool = True, dpi: int = 100) -> bytes:
    """entries: [(rank, station label, value, 資料不足)] in rank order (already cut to size).

    ``zero_based`` draws bars from zero, for quantities where zero means
    none (days, mm, hours). Temperature has no such zero, so it is drawn
    as dots on an axis fitted to the values instead of bars that would
    all look the same length."""
    n = max(len(entries), 1)
    head_in, row_in, foot_in = 1.0, 0.26, 0.95       # title band, per row, axis label + footer
    height = head_in + row_in * n + foot_in
    fig = Figure(figsize=(8.0, height), facecolor=_BG)
    ax = fig.add_axes((0.30, foot_in / height, 0.62, row_in * n / height))
    ax.set_facecolor(_BG)
    y = np.arange(len(entries))[::-1]
    vals = np.array([e[2] for e in entries]) if entries else np.array([])
    if len(entries):
        if zero_based:
            lo = min(0.0, float(vals.min()))
            ax.barh(y, vals - lo, left=lo, color=colour, alpha=0.85, height=0.7)
        else:
            lo = float(vals.min()) - max(0.5, float(np.ptp(vals)) * 0.08)
            ax.hlines(y, lo, vals, color=colour, alpha=0.35, linewidth=1.0)
            ax.plot(vals, y, "o", color=colour, markersize=5)
        span = float(vals.max() - lo) or 1.0
        for yi, (_rank, _label, v, short) in zip(y, entries, strict=True):
            ax.text(v + span * 0.01, yi, value_format.format(v) + (" ]" if short else ""),
                    va="center", ha="left", fontsize=8, color=_AXIS_FG)
        ax.set_yticks(y, [f"{r:>3d}  {label}" for r, label, _, _ in entries], fontsize=8)
        ax.set_xlim(lo, float(vals.max()) + span * 0.12)
    else:
        ax.set_yticks([])
        ax.text(0.5, 0.5, "該当なし", transform=ax.transAxes, ha="center", va="center")
    ax.tick_params(colors=_AXIS_FG, length=0)
    ax.set_xlabel(unit, color=_AXIS_FG, fontsize=9)
    ax.grid(axis="x", color=_GRID, linewidth=0.6)
    ax.set_axisbelow(True)
    for s in ("top", "right", "left"):
        ax.spines[s].set_visible(False)
    fig.text(0.02, 1 - 0.25 / height, title, fontsize=12, color=_AXIS_FG,
             ha="left", va="top", weight="bold")
    fig.text(0.02, 1 - 0.58 / height, subtitle, fontsize=8.5, color=_MEAN,
             ha="left", va="top")
    apply_footer(fig, data_source=prov.data_source, run_id=prov.period_id,
                 license_text=prov.license_text, run_label="period")
    return _to_png(fig, prov, title, dpi)


def render_station_series(first: date, vals: dict[str, np.ndarray], *, title: str,
                          prov: Provenance, dpi: int = 100) -> bytes:
    """Daily tmax / tmin / tavg lines with daily precipitation bars underneath."""
    n = len(vals["tmax"])
    days = np.array([np.datetime64(first, "D") + np.timedelta64(i, "D") for i in range(n)])
    fig = Figure(figsize=(10.0, 5.6), facecolor=_BG)
    gs = fig.add_gridspec(2, 1, height_ratios=(3, 1), left=0.07, right=0.98,
                          top=0.90, bottom=0.14, hspace=0.08)
    ax_t = fig.add_subplot(gs[0])
    ax_p = fig.add_subplot(gs[1], sharex=ax_t)
    lw = 1.2 if n <= 120 else 0.7
    for el, colour, label, style in (("tmax", _WARM, "日最高気温", "-"),
                                     ("tmin", _COOL, "日最低気温", "-"),
                                     ("tavg", _MEAN, "日平均気温", "--")):
        ax_t.plot(days, vals[el], color=colour, linewidth=lw, linestyle=style, label=label)
    ax_t.set_ylabel("°C", color=_AXIS_FG)
    ax_t.legend(loc="upper left", fontsize=8, ncol=3, framealpha=0.85)
    p = np.nan_to_num(vals["precip"], nan=0.0)
    ax_p.bar(days, p, width=1.0, color=_COOL, alpha=0.8)
    ax_p.set_ylabel("mm", color=_AXIS_FG)
    for ax in (ax_t, ax_p):
        ax.set_facecolor(_BG)
        ax.grid(color=_GRID, linewidth=0.6)
        ax.tick_params(colors=_AXIS_FG, labelsize=8)
        for s in ("top", "right"):
            ax.spines[s].set_visible(False)
    ax_t.tick_params(labelbottom=False)
    loc = mdates.AutoDateLocator()
    ax_p.xaxis.set_major_locator(loc)
    ax_p.xaxis.set_major_formatter(mdates.ConciseDateFormatter(loc))
    fig.text(0.07, 0.965, title, fontsize=12, color=_AXIS_FG, ha="left", va="top", weight="bold")
    apply_footer(fig, data_source=prov.data_source, run_id=prov.period_id,
                 license_text=prov.license_text, run_label="period")
    return _to_png(fig, prov, title, dpi)
