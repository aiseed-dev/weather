# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Yasuhiro / AIseed

"""Time-series chart on ``flet.canvas`` — Python-only, no WebView.

Replaces the matplotlib + ft.Image path for the on-screen chart in
the 地点 (point forecast) view. matplotlib stays for the 'download
PNG' option (publication-quality raster); this module is the
interactive primary display.

Why Canvas: the analysis loop wants a vector chart that can grow
hover / click handlers later. Flet 0.85's ``flet.canvas`` ships the
Line / Path / Text / Rect primitives plus ``Paint.stroke_dash_pattern``
for dashed lines and filled paths for bands. That's enough to draw
HRES + MSM + climatology + ensemble overlay in pure Python — no JS
templating, no WebView round-trip.

Read .agents/skills/chart-base-design for the palette principles
shared with the map renderers.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable, Iterable

import flet as ft
import flet.canvas as cv
import polars as pl


# Variable presentation table — mirrored from the matplotlib module
# so adding a variable in one place doesn't leave the other behind.
_VAR_INFO: dict[str, tuple[str, str]] = {
    "temperature_2m":     ("気温 / Temperature",       "°C"),
    "precipitation":      ("降水量 / Precipitation",    "mm/h"),
    "relative_humidity_2m": ("相対湿度 / Humidity",     "%"),
    "wind_speed_10m":     ("風速 / Wind speed",         "m/s"),
    "cloud_cover":        ("雲量 / Cloud cover",        "%"),
}


# Per-variable Y-axis preferences. Each entry collects:
#   grid_step       — every Nth value gets a gridline
#   label_every     — every Nth gridline gets a number label
#                     (smaller intervals stay un-labelled to keep the
#                      axis text readable)
#   fixed_min/max   — HARD bounds. Data outside this range is clipped
#                     visually (the line / band go off-canvas above /
#                     below). Used for variables with a known
#                     physical range (0..100 % for humidity / cloud).
#   soft_min/max    — bounds the axis MUST include. The axis extends
#                     past these if the data demands, but never
#                     contracts below them. Used for precipitation:
#                     the chart always reaches 25 mm/h so the analyst
#                     can immediately see whether '3 mm/h' is light
#                     drizzle (small bar) or 'heavy' (≈ top of axis);
#                     a real typhoon at 60 mm/h still pushes the
#                     axis up to fit.
@dataclass(frozen=True)
class _YAxisPref:
    grid_step: float
    label_every: int = 1
    fixed_min: float | None = None
    fixed_max: float | None = None
    soft_min: float | None = None
    soft_max: float | None = None


_Y_AXIS_PREFS: dict[str, _YAxisPref] = {
    "temperature_2m":       _YAxisPref(grid_step=2.5, label_every=2),
    "precipitation":        _YAxisPref(
        grid_step=5, label_every=1,
        fixed_min=0, soft_max=25,
    ),
    "relative_humidity_2m": _YAxisPref(
        grid_step=10, label_every=1,
        fixed_min=0, fixed_max=100,
    ),
    "wind_speed_10m":       _YAxisPref(
        grid_step=2.5, label_every=2,
        fixed_min=0,
    ),
    "cloud_cover":          _YAxisPref(
        grid_step=10, label_every=2,
        fixed_min=0, fixed_max=100,
    ),
    # MSL isn't a canvas-chart variable yet; the entry is here so
    # adding 海面気圧 to point-forecast just works.
    "msl":                  _YAxisPref(grid_step=4, label_every=5),
}


# Colours (chart-base-design — restrained palette).
_HRES = "#1c1c20"          # near-black
_MSM = "#56657a"           # slate
_CLIM_FILL = "#9aa0a8"     # neutral gray for the climatology band
_CLIM_LINE = "#5d6470"
_ENS_FILL = "#3478b8"      # cool blue
_NOW = "#b53a2a"           # warm red
_BG = "#f7f7f5"
_AXIS = "#202428"
_GRID = "#dddddd"


def _alpha(hex_color: str, alpha: float) -> str:
    """``#rrggbb`` + alpha → ``#rrggbbaa``. Flet canvas paints accept
    8-digit hex; doing the maths here keeps the call sites readable."""
    aa = format(max(0, min(255, int(alpha * 255))), "02x")
    return hex_color + aa


def _filtered_pairs(
    xs: list, ys: list,
) -> list[tuple[float, float, float, float]]:
    """Return only (t, v_lo, v_hi) tuples where both bounds are non-
    null. Climatology / ensemble columns can have ``None`` for
    timestamps the archive doesn't cover yet — those rows just
    drop out instead of crashing the path build."""
    out = []
    for t, lo, hi in zip(xs, ys[0], ys[1]):
        if lo is None or hi is None:
            continue
        out.append((t, lo, hi))
    return out


def _band_path(
    pairs: list[tuple[datetime, float, float]],
    x_of: Callable[[datetime], float],
    y_of: Callable[[float], float],
    color: str,
    alpha: float,
) -> cv.Path:
    """Build a filled polygon Path that walks forward along the lower
    edge and back along the upper edge, then closes."""
    elements: list = []
    t0, lo0, _hi0 = pairs[0]
    elements.append(cv.Path.MoveTo(x_of(t0), y_of(lo0)))
    for t, lo, _hi in pairs[1:]:
        elements.append(cv.Path.LineTo(x_of(t), y_of(lo)))
    for t, _lo, hi in reversed(pairs):
        elements.append(cv.Path.LineTo(x_of(t), y_of(hi)))
    elements.append(cv.Path.Close())
    return cv.Path(
        elements=elements,
        paint=ft.Paint(
            color=_alpha(color, alpha),
            style=ft.PaintingStyle.FILL,
        ),
    )


def _line_elements(
    xs: list[datetime],
    ys: list,
    x_of: Callable[[datetime], float],
    y_of: Callable[[float], float],
) -> list:
    """Build Path elements for a polyline. ``None`` values break the
    path — the next valid sample starts a new sub-path with MoveTo so
    gaps render as gaps instead of straight chords across nulls."""
    elements: list = []
    moved = False
    for t, v in zip(xs, ys):
        if v is None:
            moved = False
            continue
        if not moved:
            elements.append(cv.Path.MoveTo(x_of(t), y_of(v)))
            moved = True
        else:
            elements.append(cv.Path.LineTo(x_of(t), y_of(v)))
    return elements


def _ticks_at_step(
    v_min: float, v_max: float, step: float,
) -> tuple[list[float], float, float]:
    """Generate ticks at every ``step`` covering [v_min, v_max].

    Rounds the bounds outward to multiples of step so the first
    and last gridlines align with the panel edges. Used when a
    variable has an explicit ``_Y_AXIS_PREFS`` entry; otherwise
    the auto 'nice ticks' algorithm picks the step for us.
    """
    if step <= 0:
        return [v_min], v_min, v_max
    axis_min = math.floor(v_min / step) * step
    axis_max = math.ceil(v_max / step) * step
    ticks: list[float] = []
    cur = axis_min
    while cur <= axis_max + step * 1e-6:
        ticks.append(cur)
        cur += step
    return ticks, axis_min, axis_max


def _nice_y_ticks(
    v_min: float, v_max: float, n_target: int = 6,
) -> tuple[list[float], float, float, float]:
    """Pick round-number Y-axis ticks the analyst expects to read.

    Returns ``(ticks, axis_min, axis_max, step)`` where ``ticks`` are
    multiples of a step chosen from {1, 2, 2.5, 5} × 10^k. ``axis_min``
    / ``axis_max`` are the rounded bounds the chart should actually
    use — they extend slightly past ``v_min`` / ``v_max`` so the
    first and last ticks fall ON the axis ends, not floating off the
    edge. ``step`` lets the caller format labels consistently
    (integer when step ≥ 1 and the tick is whole, else one decimal).

    Same algorithm matplotlib uses for ``MaxNLocator``.
    """
    if v_max <= v_min:
        return [v_min], v_min, v_min + 1.0, 1.0
    raw_step = (v_max - v_min) / max(1, n_target - 1)
    magnitude = 10.0 ** math.floor(math.log10(raw_step))
    # Multipliers in increasing order — pick the smallest one whose
    # step (multiplier × magnitude) is at least the raw step.
    for mult in (1.0, 2.0, 2.5, 5.0, 10.0):
        step = mult * magnitude
        if step >= raw_step:
            break
    else:  # pragma: no cover — math.log10 already constrained this
        step = 10.0 * magnitude

    axis_min = math.floor(v_min / step) * step
    axis_max = math.ceil(v_max / step) * step
    ticks: list[float] = []
    cur = axis_min
    while cur <= axis_max + step * 1e-6:
        ticks.append(cur)
        cur += step
    return ticks, axis_min, axis_max, step


def _format_y_tick(value: float, step: float) -> str:
    """Integer label when step is whole and value snaps to an integer;
    otherwise pick a decimal count matching the step's precision."""
    if step >= 1.0 and abs(value - round(value)) < 1e-6:
        return str(int(round(value)))
    if step >= 0.1:
        return f"{value:.1f}"
    return f"{value:.2f}"


def _collect_all_values(
    hres: pl.DataFrame,
    msm: pl.DataFrame | None,
    ensemble: pl.DataFrame | None,
    variable: str,
) -> list[float]:
    """Walk every series we plan to plot and gather their non-null
    values so the y-axis bounds cover all of them."""
    out: list[float] = []
    for col in (variable, f"{variable}_p25", f"{variable}_p75"):
        if col in hres.columns:
            out.extend(hres[col].drop_nulls().to_list())
    if msm is not None and not msm.is_empty() and variable in msm.columns:
        out.extend(msm[variable].drop_nulls().to_list())
    if ensemble is not None and not ensemble.is_empty():
        ens = ensemble.filter(pl.col("variable") == variable)
        if not ens.is_empty():
            out.extend(ens["p10"].drop_nulls().to_list())
            out.extend(ens["p90"].drop_nulls().to_list())
    return out


def build_point_forecast_canvas(
    *,
    location_name: str,
    variable: str,
    hres_joined: pl.DataFrame,
    msm_df: pl.DataFrame | None,
    ensemble_quantiles: pl.DataFrame | None,
    now_utc: datetime | None = None,
    visible_window: tuple[datetime, datetime] | None = None,
    width: float = 2200.0,
    height: float = 700.0,
) -> cv.Canvas:
    """Render the point-forecast time series onto a Flet Canvas.

    ``visible_window`` (t_start, t_end) restricts the x-axis to a
    sub-range of the forecast — callers use this to implement a
    'zoom to N days' control. When ``None`` the chart spans the
    full hres_joined timestamp range.

    Returns a single ``flet.canvas.Canvas`` whose ``shapes`` list
    contains the background, axes, bands, lines, legend, and 'now'
    marker. The widget is intended to be wrapped in a horizontally-
    scrollable Row at the call site so wide views stay readable.

    Sync — fast (no I/O, just shape construction). Calling from the
    event loop is fine.
    """
    label_ja, unit = _VAR_INFO.get(variable, (variable, ""))
    pad_l, pad_r, pad_t, pad_b = 60.0, 16.0, 44.0, 36.0
    plot_w = width - pad_l - pad_r
    plot_h = height - pad_t - pad_b

    # No data → just paint a placeholder.
    if hres_joined.is_empty() or "timestamp" not in hres_joined.columns:
        return cv.Canvas(
            shapes=[
                cv.Rect(0, 0, width, height,
                        paint=ft.Paint(color=_BG, style=ft.PaintingStyle.FILL)),
                cv.Text(
                    width / 2, height / 2, "データなし",
                    style=ft.TextStyle(color=_AXIS, size=14),
                    alignment=ft.Alignment.CENTER,
                ),
            ],
            width=width, height=height,
        )

    ts: list[datetime] = hres_joined["timestamp"].to_list()
    # X-axis bounds: caller-provided visible window if given, else
    # the full data range. Out-of-window samples still get rendered
    # — the axes clip them visually, and the surrounding context
    # outside the visible band is rarely a perf issue at our sizes.
    if visible_window is not None:
        t_min, t_max = visible_window
    else:
        t_min, t_max = ts[0], ts[-1]
    span_seconds = (t_max - t_min).total_seconds() or 1.0

    def x_of(t: datetime) -> float:
        return pad_l + (t - t_min).total_seconds() / span_seconds * plot_w

    # Helper to skip samples entirely outside the visible window.
    # Drops both the line continuity (start a new sub-path on
    # re-entry) and the cost of computing x_of on far-away points.
    def _in_window(t: datetime) -> bool:
        return t_min <= t <= t_max

    vals = _collect_all_values(
        hres_joined, msm_df, ensemble_quantiles, variable,
    )
    if vals:
        raw_min, raw_max = min(vals), max(vals)
        # Small pad so the data doesn't kiss the axis frame before
        # nice-rounding pushes the bounds out further.
        pad_v = (raw_max - raw_min) * 0.05 or 1.0
        raw_min -= pad_v
        raw_max += pad_v
    else:
        raw_min, raw_max = 0.0, 1.0

    # Y axis: explicit per-variable preference wins (e.g. 気温 = 2.5 °C
    # gridline / 5 °C label, 海面気圧 = 4 hPa / 20 hPa, 湿度 = fixed
    # 0..100). Variables without a pref fall back to the auto 'nice
    # ticks' algorithm (rounds the step to {1, 2, 2.5, 5} × 10^k).
    # Either way the axis bounds extend outward to multiples of the
    # step so the first / last grid line sit on the panel edges.
    pref = _Y_AXIS_PREFS.get(variable)
    if pref is not None:
        # Apply hard / soft bounds before the tick rounding.
        if pref.fixed_min is not None:
            target_min = pref.fixed_min
        elif pref.soft_min is not None:
            target_min = min(pref.soft_min, raw_min)
        else:
            target_min = raw_min
        if pref.fixed_max is not None:
            target_max = pref.fixed_max
        elif pref.soft_max is not None:
            target_max = max(pref.soft_max, raw_max)
        else:
            target_max = raw_max
        y_ticks, v_min, v_max = _ticks_at_step(
            target_min, target_max, pref.grid_step,
        )
        # Hard bounds: trust them exactly so 'cloud cover 0..100'
        # never grows to 0..110 from the outward rounding.
        if pref.fixed_min is not None:
            v_min = pref.fixed_min
        if pref.fixed_max is not None:
            v_max = pref.fixed_max
        y_step = pref.grid_step
        label_every = pref.label_every
    else:
        y_ticks, v_min, v_max, y_step = _nice_y_ticks(
            raw_min, raw_max, n_target=6,
        )
        label_every = 1
    v_span = v_max - v_min or 1.0

    def y_of(v: float) -> float:
        return pad_t + (1.0 - (v - v_min) / v_span) * plot_h

    shapes: list = []

    # ── Background panel ────────────────────────────────────────
    shapes.append(cv.Rect(
        0, 0, width, height,
        paint=ft.Paint(color=_BG, style=ft.PaintingStyle.FILL),
    ))

    # ── Grid + Y axis labels (nice round numbers) ──────────────
    # Every tick gets a gridline (fine resolution for the eye); only
    # every Nth gets a number label (so the axis text doesn't crowd
    # at fine steps like 気温 2.5 °C).
    for i, v in enumerate(y_ticks):
        y = y_of(v)
        shapes.append(cv.Line(
            pad_l, y, pad_l + plot_w, y,
            paint=ft.Paint(color=_GRID, stroke_width=0.6),
        ))
        if i % label_every == 0:
            shapes.append(cv.Text(
                pad_l - 6, y - 6, _format_y_tick(v, y_step),
                style=ft.TextStyle(color=_AXIS, size=10),
                alignment=ft.Alignment.CENTER_RIGHT,
            ))

    # ── X axis ──────────────────────────────────────────────────
    # Tick cadence + label format scale with the visible span so
    # short windows (1日 / 3日) get hourly resolution while wide
    # ones (15日) stay clean with one tick per day.
    #
    # Time labels never include minutes — the axis is a continuous
    # time scale and the analyst cares about the hour, not the
    # minute. Midnight ticks promote to a date label so the day
    # boundary is visible at a glance.
    span_hours = span_seconds / 3600.0
    if span_hours <= 30:           # 1-day view
        tick_step = timedelta(hours=3)
    elif span_hours <= 96:         # 2-4 day window
        tick_step = timedelta(hours=6)
    else:                          # longer than ~4 days
        tick_step = timedelta(days=1)

    # Anchor on a tick boundary at or after t_min so the first
    # tick is exactly on a 3 h / 6 h / midnight mark.
    t_cur = t_min.replace(minute=0, second=0, microsecond=0)
    if tick_step >= timedelta(days=1):
        t_cur = t_cur.replace(hour=0)
        if t_cur < t_min:
            t_cur += timedelta(days=1)
    else:
        step_hours = int(tick_step.total_seconds() // 3600)
        t_cur = t_cur.replace(
            hour=(t_cur.hour // step_hours) * step_hours,
        )
        while t_cur < t_min:
            t_cur += tick_step

    while t_cur <= t_max:
        x = x_of(t_cur)
        is_midnight = t_cur.hour == 0
        shapes.append(cv.Line(
            x, pad_t, x, pad_t + plot_h,
            paint=ft.Paint(
                color=_GRID,
                stroke_width=0.9 if is_midnight else 0.5,
            ),
        ))
        # Midnight → date label. Other ticks → hour only (no minutes).
        label = (
            t_cur.strftime("%m-%d")
            if is_midnight else f"{t_cur.hour}"
        )
        shapes.append(cv.Text(
            x, pad_t + plot_h + 14, label,
            style=ft.TextStyle(color=_AXIS, size=10),
            alignment=ft.Alignment.CENTER,
        ))
        t_cur += tick_step

    # ── Climatology band (p25..p75) ────────────────────────────
    p25_col = f"{variable}_p25"
    p75_col = f"{variable}_p75"
    if p25_col in hres_joined.columns and p75_col in hres_joined.columns:
        pairs = _filtered_pairs(
            ts, (hres_joined[p25_col].to_list(),
                 hres_joined[p75_col].to_list()),
        )
        if len(pairs) >= 2:
            shapes.append(_band_path(pairs, x_of, y_of, _CLIM_FILL, 0.28))

    # ── Climatology mean (dotted) ──────────────────────────────
    mean_col = f"{variable}_mean"
    if mean_col in hres_joined.columns:
        elems = _line_elements(
            ts, hres_joined[mean_col].to_list(), x_of, y_of,
        )
        if elems:
            shapes.append(cv.Path(
                elements=elems,
                paint=ft.Paint(
                    color=_CLIM_LINE, stroke_width=1.0,
                    stroke_dash_pattern=[2.0, 3.0],
                    style=ft.PaintingStyle.STROKE,
                ),
            ))

    # ── Ensemble band (p10..p90, future only) ──────────────────
    if (
        ensemble_quantiles is not None
        and not ensemble_quantiles.is_empty()
    ):
        ens = ensemble_quantiles.filter(pl.col("variable") == variable)
        if not ens.is_empty():
            pairs = _filtered_pairs(
                ens["timestamp"].to_list(),
                (ens["p10"].to_list(), ens["p90"].to_list()),
            )
            if len(pairs) >= 2:
                shapes.append(_band_path(pairs, x_of, y_of, _ENS_FILL, 0.18))

    # ── MSM line (dashed reference) ────────────────────────────
    if (
        msm_df is not None
        and not msm_df.is_empty()
        and variable in msm_df.columns
    ):
        elems = _line_elements(
            msm_df["timestamp"].to_list(),
            msm_df[variable].to_list(),
            x_of, y_of,
        )
        if elems:
            shapes.append(cv.Path(
                elements=elems,
                paint=ft.Paint(
                    color=_MSM, stroke_width=1.0,
                    stroke_dash_pattern=[4.0, 3.0],
                    style=ft.PaintingStyle.STROKE,
                ),
            ))

    # ── HRES line (primary, on top) ─────────────────────────────
    if variable in hres_joined.columns:
        elems = _line_elements(
            ts, hres_joined[variable].to_list(), x_of, y_of,
        )
        if elems:
            shapes.append(cv.Path(
                elements=elems,
                paint=ft.Paint(
                    color=_HRES, stroke_width=1.6,
                    style=ft.PaintingStyle.STROKE,
                ),
            ))

    # ── 'Now' vertical marker ──────────────────────────────────
    now = now_utc or datetime.now(timezone.utc)
    if t_min <= now <= t_max:
        nx = x_of(now)
        shapes.append(cv.Line(
            nx, pad_t, nx, pad_t + plot_h,
            paint=ft.Paint(color=_NOW, stroke_width=1.0),
        ))
        shapes.append(cv.Text(
            nx + 4, pad_t + 2, "現在",
            style=ft.TextStyle(color=_NOW, size=10),
            alignment=ft.Alignment.TOP_LEFT,
        ))

    # ── Title + Y-axis label ────────────────────────────────────
    shapes.append(cv.Text(
        pad_l, 6, f"{location_name} — {label_ja}",
        style=ft.TextStyle(
            color=_AXIS, size=13, weight=ft.FontWeight.W_500,
        ),
        alignment=ft.Alignment.TOP_LEFT,
    ))
    shapes.append(cv.Text(
        4, pad_t + plot_h / 2, unit,
        style=ft.TextStyle(color=_AXIS, size=10),
        alignment=ft.Alignment.CENTER_LEFT,
    ))

    # ── Legend (top-right strip) ────────────────────────────────
    legend_items: list[tuple[str, str, str]] = [
        (_HRES,      "HRES",            "line"),
        (_CLIM_LINE, "平年 mean",        "dotted"),
        (_CLIM_FILL, "平年 p25..p75",   "fill"),
    ]
    if (
        ensemble_quantiles is not None
        and not ensemble_quantiles.is_empty()
    ):
        legend_items.append((_ENS_FILL, "ENS p10..p90", "fill"))
    if msm_df is not None and not msm_df.is_empty():
        legend_items.append((_MSM, "MSM (参考)", "dashed"))

    # Pack from right edge backwards
    lx = pad_l + plot_w
    ly = 6
    for color, label, kind in reversed(legend_items):
        # Estimate width — 7 px per character is rough but good enough
        # to keep items separated on the line.
        est_w = 10 + 7 * len(label) + 8
        lx -= est_w
        # Swatch
        if kind == "fill":
            shapes.append(cv.Rect(
                lx, ly + 2, 14, 8,
                paint=ft.Paint(
                    color=_alpha(color, 0.35),
                    style=ft.PaintingStyle.FILL,
                ),
            ))
        else:
            shapes.append(cv.Line(
                lx, ly + 6, lx + 14, ly + 6,
                paint=ft.Paint(
                    color=color, stroke_width=1.8,
                    stroke_dash_pattern=(
                        [3.0, 2.0] if kind == "dotted"
                        else [4.0, 3.0] if kind == "dashed"
                        else None
                    ),
                    style=ft.PaintingStyle.STROKE,
                ),
            ))
        shapes.append(cv.Text(
            lx + 18, ly + 1, label,
            style=ft.TextStyle(color=_AXIS, size=10),
            alignment=ft.Alignment.TOP_LEFT,
        ))

    return cv.Canvas(shapes=shapes, width=width, height=height)
