# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Yasuhiro / AIseed

"""JMA rainfall nowcast as a map of Japan or one of its regions.

The radar arrives on the Web-Mercator pixel grid of its tiles and is
resampled onto the Japan frame shared with the AMeDAS map
(``_japan_map``), so the two figures line up. Where several radar pixels
fall into one figure pixel the strongest class is kept: a heavy-rain
core a few kilometres across must not vanish when the national view is
drawn at ~4 km per pixel.

Colours are JMA's precipitation-intensity scheme, which the user knows
from JMA's own maps (jma-data-access). Where JMA has no data (outside
the radars' range, or a radar down) the map is veiled in light gray, so
"not observed" never reads as "no rain".
"""

from __future__ import annotations

import io
import math
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import numpy as np
from matplotlib.backends.backend_agg import FigureCanvasAgg
from matplotlib.cm import ScalarMappable
from matplotlib.colors import BoundaryNorm, ListedColormap, to_rgba
from matplotlib.figure import Figure
from matplotlib.patches import Rectangle
from PIL import Image, ImageDraw

from aiseed_weather.figures._fonts import configure_cjk_font
from aiseed_weather.figures._japan_map import (
    ASPECT_LAT,
    AXIS_FG,
    BG,
    EXTENT,
    JMA_PRECIP_BOUNDS,
    JMA_PRECIP_COLOURS,
    japan_axes,
)
from aiseed_weather.figures.footer import apply_footer

configure_cjk_font()

_JST = ZoneInfo("Asia/Tokyo")
_FIG_W = 8.0                  # inches; the map is 0.80 of it, as on the AMeDAS map
_MAP_LEFT, _MAP_W = 0.07, 0.80
_TOP_IN, _BOTTOM_IN = 0.86, 1.03
# Class 0 (no echo) is transparent; classes 1 … 8 are JMA's colours.
_CLASS_RGBA = np.array([(0.0, 0.0, 0.0, 0.0)] + [to_rgba(c) for c in JMA_PRECIP_COLOURS])
# Light veil over the gray base where JMA has no data.
NO_DATA_RGBA = (0.93, 0.93, 0.95, 0.45)


def _bin_starts(centres: np.ndarray, edges: np.ndarray) -> tuple[np.ndarray, int]:
    """First source index of each target bin, and the end of the last bin.

    Both arrays increase. A bin no source centre falls into gets the next
    source pixel, which is how a finer target picks the nearest value.
    """
    starts = np.searchsorted(centres, edges[:-1], side="left")
    stop = int(np.searchsorted(centres, edges[-1], side="left"))
    return np.minimum(starts, max(stop - 1, 0)), stop


def resample_to_frame(classes: np.ndarray, lons: np.ndarray, lats: np.ndarray,
                      extent: tuple[float, float, float, float],
                      width: int, height: int) -> np.ndarray:
    """Max-pool intensity classes onto a regular ``height × width`` lon/lat grid.

    ``lons`` increase along columns and ``lats`` decrease along rows (pixel
    centres). The result's row 0 is the northern edge of ``extent``.
    """
    lon0, lon1, lat0, lat1 = extent
    col_starts, col_stop = _bin_starts(lons, np.linspace(lon0, lon1, width + 1))
    row_starts, row_stop = _bin_starts(-lats, -np.linspace(lat1, lat0, height + 1))
    if col_stop == 0 or row_stop == 0:
        return np.zeros((height, width), np.uint8)
    out = np.maximum.reduceat(classes[:row_stop, :col_stop], row_starts, axis=0)
    return np.maximum.reduceat(out, col_starts, axis=1)


def no_data_mask(polygons, extent: tuple[float, float, float, float],
                 width: int, height: int) -> np.ndarray:
    """Cells of a ``height × width`` lon/lat grid whose centre is in the no-data area.

    Each polygon is (exterior, hole, …); rings toggle cells (even–odd), so a
    hole — the observed area — clears what its exterior set. PIL fills the
    pixels a ring passes through, so an edge may land a cell either way —
    finer than JMA's own stair-stepped coverage edge.
    """
    lon0, lon1, lat0, lat1 = extent
    sx, sy = width / (lon1 - lon0), height / (lat1 - lat0)
    out = np.zeros((height, width), bool)
    for rings in polygons:
        inside = np.zeros((height, width), bool)
        for ring in rings:
            img = Image.new("1", (width, height), 0)
            # Cell (i, j) is centred on integer coordinates, as PIL draws them.
            x = (ring[:, 0] - lon0) * sx - 0.5
            y = (lat1 - ring[:, 1]) * sy - 0.5
            ImageDraw.Draw(img).polygon(list(zip(x.tolist(), y.tolist())), fill=1)
            inside ^= np.asarray(img, bool)
        out |= inside
    return out


def _layout(extent: tuple[float, float, float, float], aspect_lat: float):
    """Figure size and the map / colour-bar rectangles for an extent."""
    map_w = _FIG_W * _MAP_W
    map_h = map_w * (extent[3] - extent[2]) / ((extent[1] - extent[0])
                                               * math.cos(math.radians(aspect_lat)))
    fig_h = _TOP_IN + map_h + _BOTTOM_IN
    bottom, height = _BOTTOM_IN / fig_h, map_h / fig_h
    return ((_FIG_W, fig_h), (_MAP_LEFT, bottom, _MAP_W, height),
            (0.89, bottom, 0.025, height))


def render_radar_map(classes: np.ndarray, lons: np.ndarray, lats: np.ndarray, *,
                     valid: datetime, base: datetime, attribution: str,
                     extent: tuple[float, float, float, float] = EXTENT, area_name: str = "",
                     no_data=None, note: str = "", dpi: int = 100) -> bytes:
    """PNG of one nowcast frame. ``valid`` and ``base`` are aware datetimes.

    ``no_data`` is JMA's no-data area (polygons of (lon, lat) rings), or None.
    """
    aspect_lat = ASPECT_LAT if extent == EXTENT else (extent[2] + extent[3]) / 2
    figsize, map_rect, bar_rect = _layout(extent, aspect_lat)
    width, height = int(round(figsize[0] * map_rect[2] * dpi)), int(round(figsize[1] * map_rect[3] * dpi))
    grid = resample_to_frame(classes, lons, lats, extent, width, height)

    fig = Figure(figsize=figsize, facecolor=BG)
    FigureCanvasAgg(fig)
    fig_h = figsize[1]
    # The radar covers the land, so the coastline is drawn over it.
    ax = japan_axes(fig, map_rect, extent=extent, aspect_lat=aspect_lat, coast_zorder=0.7)
    ax.imshow(_CLASS_RGBA[grid], extent=extent, origin="upper", interpolation="nearest",
              aspect=ax.get_aspect(), zorder=0.6)
    unobserved = (no_data_mask(no_data, extent, width, height) if no_data is not None
                  else np.zeros((height, width), bool))
    if unobserved.any():
        veil = np.zeros((height, width, 4))
        veil[unobserved] = NO_DATA_RGBA
        ax.imshow(veil, extent=extent, origin="upper", interpolation="nearest",
                  aspect=ax.get_aspect(), zorder=0.65)

    cmap = ListedColormap(JMA_PRECIP_COLOURS)
    norm = BoundaryNorm(JMA_PRECIP_BOUNDS, cmap.N)
    cb = fig.colorbar(ScalarMappable(norm=norm, cmap=cmap), cax=fig.add_axes(bar_rect))
    cb.set_ticks(JMA_PRECIP_BOUNDS[:-1])
    cb.set_label("降水強度（mm/h）", color=AXIS_FG, fontsize=9)
    cb.ax.tick_params(labelsize=8, colors=AXIS_FG)

    jst, utc = valid.astimezone(_JST), valid.astimezone(UTC)
    lead = int((valid - base).total_seconds() // 60)
    kind = f"予測 +{lead} 分" if lead else "観測"
    title = f"降水ナウキャスト {kind}  {jst:%Y-%m-%d %H:%M} JST（{utc:%H:%M} UTC）"
    fig.text(0.07, 1 - 0.39 / fig_h, title, fontsize=12, color=AXIS_FG, ha="left", va="top",
             weight="bold")
    subtitle = "気象庁 高解像度降水ナウキャスト" + (f" · {area_name}" if area_name else "")
    if lead:
        subtitle += f" · 初期時刻 {base.astimezone(_JST):%H:%M} JST（{base.astimezone(UTC):%H:%M} UTC）"
    fig.text(0.07, 1 - 0.645 / fig_h, subtitle, fontsize=8.5, color="#6b7078",
             ha="left", va="top")
    below = 0.645 / fig_h
    x = 0.07
    if unobserved.any():
        fig.add_artist(Rectangle((x, below - 0.12 / fig_h), 0.025, 0.1 / fig_h,
                                 transform=fig.transFigure, facecolor="#585c64",
                                 edgecolor="#9a9ea6", linewidth=0.5))
        fig.add_artist(Rectangle((x, below - 0.12 / fig_h), 0.025, 0.1 / fig_h,
                                 transform=fig.transFigure, facecolor=NO_DATA_RGBA,
                                 edgecolor="none"))
        fig.text(x + 0.032, below, "観測範囲外・欠測", fontsize=8, color="#6b7078",
                 ha="left", va="top")
        x += 0.2
    if note:
        fig.text(x, below, note, fontsize=8, color="#6b7078", ha="left", va="top")
    apply_footer(fig, data_source="JMA 高解像度降水ナウキャスト",
                 run_id=f"{jst:%Y-%m-%d %H:%M} JST", license_text=attribution, run_label="valid")
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=dpi, facecolor=fig.get_facecolor(), metadata={
        "Title": title, "Software": "AIseed Weather v0.1 (https://aiseed.dev)",
        "Source": "JMA high-resolution precipitation nowcast (hrpns)", "Copyright": attribution,
        "Description": (f"Precipitation intensity valid {utc:%Y-%m-%dT%H:%MZ}, "
                        f"base {base.astimezone(UTC):%Y-%m-%dT%H:%MZ}, "
                        f"lon {extent[0]}–{extent[1]}, lat {extent[2]}–{extent[3]}"),
    })
    return buf.getvalue()
