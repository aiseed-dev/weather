# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Yasuhiro / AIseed

"""JMA rainfall nowcast as a map of Japan.

The radar arrives on the Web-Mercator pixel grid of its tiles and is
resampled onto the Japan frame shared with the AMeDAS map
(``_japan_map``), so the two figures line up. Where several radar pixels
fall into one figure pixel the strongest class is kept: a heavy-rain
core a few kilometres across must not vanish when the national view is
drawn at ~4 km per pixel.

Colours are JMA's precipitation-intensity scheme, which the user knows
from JMA's own maps (jma-data-access).
"""

from __future__ import annotations

import io
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import numpy as np
from matplotlib.backends.backend_agg import FigureCanvasAgg
from matplotlib.cm import ScalarMappable
from matplotlib.colors import BoundaryNorm, ListedColormap, to_rgba
from matplotlib.figure import Figure

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
_FIGSIZE = (8.0, 8.6)
_AXES_RECT = (0.07, 0.12, 0.80, 0.78)
# Class 0 (no echo) is transparent; classes 1 … 8 are JMA's colours.
_CLASS_RGBA = np.array([(0.0, 0.0, 0.0, 0.0)] + [to_rgba(c) for c in JMA_PRECIP_COLOURS])


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


def _grid_size(dpi: int, extent: tuple[float, float, float, float]) -> tuple[int, int]:
    """Pixel size of the map area, with the frame's cos-latitude aspect."""
    box_w = _AXES_RECT[2] * _FIGSIZE[0] * dpi
    box_h = _AXES_RECT[3] * _FIGSIZE[1] * dpi
    ratio = (extent[3] - extent[2]) / ((extent[1] - extent[0]) * np.cos(np.radians(ASPECT_LAT)))
    if box_w * ratio <= box_h:
        return int(round(box_w)), int(round(box_w * ratio))
    return int(round(box_h / ratio)), int(round(box_h))


def render_radar_map(classes: np.ndarray, lons: np.ndarray, lats: np.ndarray, *,
                     valid: datetime, base: datetime, attribution: str,
                     note: str = "", dpi: int = 100) -> bytes:
    """PNG of one nowcast frame. ``valid`` and ``base`` are aware datetimes."""
    width, height = _grid_size(dpi, EXTENT)
    grid = resample_to_frame(classes, lons, lats, EXTENT, width, height)

    fig = Figure(figsize=_FIGSIZE, facecolor=BG)
    FigureCanvasAgg(fig)
    # The radar covers the land, so the coastline is drawn over it.
    ax = japan_axes(fig, _AXES_RECT, coast_zorder=0.7)
    ax.imshow(_CLASS_RGBA[grid], extent=EXTENT, origin="upper", interpolation="nearest",
              aspect=ax.get_aspect(), zorder=0.6)

    cmap = ListedColormap(JMA_PRECIP_COLOURS)
    norm = BoundaryNorm(JMA_PRECIP_BOUNDS, cmap.N)
    cb = fig.colorbar(ScalarMappable(norm=norm, cmap=cmap),
                      cax=fig.add_axes((0.89, 0.12, 0.025, 0.78)))
    cb.set_ticks(JMA_PRECIP_BOUNDS[:-1])
    cb.set_label("降水強度（mm/h）", color=AXIS_FG, fontsize=9)
    cb.ax.tick_params(labelsize=8, colors=AXIS_FG)

    jst, utc = valid.astimezone(_JST), valid.astimezone(UTC)
    lead = int((valid - base).total_seconds() // 60)
    kind = f"予測 +{lead} 分" if lead else "観測"
    title = f"降水ナウキャスト {kind}  {jst:%Y-%m-%d %H:%M} JST（{utc:%H:%M} UTC）"
    fig.text(0.07, 0.955, title, fontsize=12, color=AXIS_FG, ha="left", va="top", weight="bold")
    subtitle = "気象庁 高解像度降水ナウキャスト"
    if lead:
        subtitle += f" · 初期時刻 {base.astimezone(_JST):%H:%M} JST（{base.astimezone(UTC):%H:%M} UTC）"
    fig.text(0.07, 0.925, subtitle, fontsize=8.5, color="#6b7078", ha="left", va="top")
    if note:
        fig.text(0.07, 0.075, note, fontsize=8, color="#6b7078", ha="left", va="top")
    apply_footer(fig, data_source="JMA 高解像度降水ナウキャスト",
                 run_id=f"{jst:%Y-%m-%d %H:%M} JST", license_text=attribution, run_label="valid")
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=dpi, facecolor=fig.get_facecolor(), metadata={
        "Title": title, "Software": "AIseed Weather v0.1 (https://aiseed.dev)",
        "Source": "JMA high-resolution precipitation nowcast (hrpns)", "Copyright": attribution,
        "Description": (f"Precipitation intensity valid {utc:%Y-%m-%dT%H:%MZ}, "
                        f"base {base.astimezone(UTC):%Y-%m-%dT%H:%MZ}"),
    })
    return buf.getvalue()
