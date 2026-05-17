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


def _nice_y_ticks(
    v_min: float, v_max: float, n: int = 6,
) -> list[float]:
    """Pick ``n`` evenly-spaced ticks across (v_min, v_max). Simple
    linear placement; a matplotlib-style 'nice round numbers'
    rounding can come later if the labels read awkward."""
    if v_max <= v_min:
        return [v_min]
    return [v_min + (v_max - v_min) * i / (n - 1) for i in range(n)]


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
    width: float = 2200.0,
    height: float = 500.0,
) -> cv.Canvas:
    """Render the point-forecast time series onto a Flet Canvas.

    Returns a single ``flet.canvas.Canvas`` whose ``shapes`` list
    contains the background, axes, bands, lines, legend, and 'now'
    marker. The widget is intended to be wrapped in a horizontally-
    scrollable Row at the call site so the wide canvas stays readable.

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
    t_min, t_max = ts[0], ts[-1]
    span_seconds = (t_max - t_min).total_seconds() or 1.0

    def x_of(t: datetime) -> float:
        return pad_l + (t - t_min).total_seconds() / span_seconds * plot_w

    vals = _collect_all_values(
        hres_joined, msm_df, ensemble_quantiles, variable,
    )
    if vals:
        v_min, v_max = min(vals), max(vals)
        pad_v = (v_max - v_min) * 0.05 or 1.0
        v_min -= pad_v
        v_max += pad_v
    else:
        v_min, v_max = 0.0, 1.0
    v_span = v_max - v_min or 1.0

    def y_of(v: float) -> float:
        return pad_t + (1.0 - (v - v_min) / v_span) * plot_h

    shapes: list = []

    # ── Background panel ────────────────────────────────────────
    shapes.append(cv.Rect(
        0, 0, width, height,
        paint=ft.Paint(color=_BG, style=ft.PaintingStyle.FILL),
    ))

    # ── Grid + Y axis labels ────────────────────────────────────
    for v in _nice_y_ticks(v_min, v_max, n=6):
        y = y_of(v)
        shapes.append(cv.Line(
            pad_l, y, pad_l + plot_w, y,
            paint=ft.Paint(color=_GRID, stroke_width=0.6),
        ))
        shapes.append(cv.Text(
            pad_l - 6, y - 6, f"{v:.1f}",
            style=ft.TextStyle(color=_AXIS, size=10),
            alignment=ft.Alignment.CENTER_RIGHT,
        ))

    # ── X axis (daily tick at 00:00 UTC of each day in range) ──
    t_cur = t_min.replace(hour=0, minute=0, second=0, microsecond=0)
    if t_cur < t_min:
        t_cur += timedelta(days=1)
    while t_cur <= t_max:
        x = x_of(t_cur)
        shapes.append(cv.Line(
            x, pad_t, x, pad_t + plot_h,
            paint=ft.Paint(color=_GRID, stroke_width=0.6),
        ))
        shapes.append(cv.Text(
            x, pad_t + plot_h + 14, t_cur.strftime("%m-%d"),
            style=ft.TextStyle(color=_AXIS, size=10),
            alignment=ft.Alignment.CENTER,
        ))
        t_cur += timedelta(days=1)

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
