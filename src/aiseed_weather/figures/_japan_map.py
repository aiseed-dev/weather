# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Yasuhiro / AIseed

"""The Japan map frame shared by the AMeDAS and radar figures.

Both figures draw the same latitude/longitude frame (x axis scaled by
cos 36°) on the app's gray land / sea base with a dark coastline
(chart-base-design), so a station map and a radar map of the same
moment line up when compared. The outline is Natural Earth 10m,
precomputed for this frame by ``_precompute_japan_coast.py`` — the
0.25° masks the model charts use are coarser than station spacing.

Also holds JMA's precipitation colours, which both figures use:
observed precipitation must not share the NWP palette
(chart-base-design), and JMA's scheme is what the user recognises.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from matplotlib.axes import Axes
from matplotlib.collections import PolyCollection
from matplotlib.figure import Figure

from aiseed_weather.figures._basemap import COASTLINE_RGB, LAND_RGB, SEA_RGB

EXTENT = (122.5, 149.0, 24.0, 46.0)        # lon_min, lon_max, lat_min, lat_max
ASPECT_LAT = 36.0                          # the x axis is scaled by cos of this latitude
BG = "#f7f7f5"
FRAME = "#585c64"
AXIS_FG = "#202428"
_COAST_PATH = Path(__file__).parent / "_japan_coastline.npz"

# 気象庁の降水量・降水強度の配色（mm、mm/h）と、凡例での各段の書き方
JMA_PRECIP_BOUNDS = (0.0, 1.0, 5.0, 10.0, 20.0, 30.0, 50.0, 80.0, 1000.0)
JMA_PRECIP_COLOURS = ("#f2f2ff", "#a0d2ff", "#218cff", "#0041ff",
                      "#faf500", "#ff9900", "#ff2800", "#b40068")
JMA_PRECIP_LABELS = ("〜1", "1〜5", "5〜10", "10〜20", "20〜30", "30〜50", "50〜80", "80〜")


@dataclass(frozen=True)
class JapanArea:
    """A part of the frame, and the JMA radar tile zoom each nowcast view uses there.

    JMA builds each lower zoom as the maximum of the pixels below it, so the
    zoom sets how much heavy rain is inflated, not only how sharp it looks.
    ``figure_zoom``: a tile pixel about the size of a figure pixel at 200 dpi
    (~2 km for the whole country, ~0.5 km for a region). ``moving_zoom``:
    ~2 km on the GPU map, where the whole country is 22 tiles a frame.
    """

    key: str
    name: str
    extent: tuple[float, float, float, float]
    figure_zoom: int
    moving_zoom: int

    @property
    def mid_lat(self) -> float:
        return (self.extent[2] + self.extent[3]) / 2


# Regions stay inside the frame, whose outline the land and coastline come from.
AREAS = (
    JapanArea("japan", "全国", EXTENT, figure_zoom=6, moving_zoom=6),
    JapanArea("north", "北日本", (138.5, 146.5, 38.5, 45.8), figure_zoom=8, moving_zoom=6),
    JapanArea("east", "東日本", (135.0, 142.5, 33.8, 39.5), figure_zoom=8, moving_zoom=6),
    JapanArea("west", "西日本", (129.0, 136.4, 31.0, 36.5), figure_zoom=8, moving_zoom=6),
    JapanArea("southwest", "南西諸島", (122.5, 131.0, 24.0, 30.5), figure_zoom=8, moving_zoom=6),
)


def area(key: str) -> JapanArea:
    return next((a for a in AREAS if a.key == key), AREAS[0])


def tile_km(zoom: int, lat: float) -> float:
    """Ground size of one radar tile pixel at ``lat``."""
    return 40075.0 * math.cos(math.radians(lat)) / (256 * 2 ** zoom)


def _load_outline() -> tuple[list[np.ndarray], np.ndarray] | None:
    if not _COAST_PATH.exists():
        return None
    with np.load(_COAST_PATH) as d:
        land, coast = d["land"], d["coast"]
    breaks = np.nonzero(np.isnan(land[:, 0]))[0]
    rings = [r for r in np.split(land, breaks) if len(r) > 3]
    return [r[~np.isnan(r[:, 0])] for r in rings], coast


_OUTLINE = _load_outline()


def outline() -> tuple[list[np.ndarray], np.ndarray] | None:
    """(land rings, coastline) as (lon, lat) vertices; the coastline breaks at NaN rows."""
    return _OUTLINE


def rgb01(c) -> tuple[float, float, float]:
    return tuple(float(x) / 255 for x in c)


def _ticks(lo: float, hi: float) -> list[int]:
    span = hi - lo
    step = 5 if span > 12 else (2 if span > 5 else 1)
    return list(range(math.ceil(lo / step) * step, math.floor(hi) + 1, step))


def japan_axes(fig: Figure, rect: tuple[float, float, float, float], *,
               extent: tuple[float, float, float, float] = EXTENT,
               aspect_lat: float = ASPECT_LAT, coast_zorder: float = 0.6) -> Axes:
    """Axes with the sea, land, coastline, graticule and ticks of the Japan frame.

    ``extent`` may be a part of the frame (a region); the x axis is scaled
    by cos ``aspect_lat``. Land sits at zorder 0.5. Data drawn above
    ``coast_zorder`` hides the coastline; a raster layer that covers the
    land should go below it.
    """
    ax = fig.add_axes(rect)
    ax.set_facecolor(rgb01(SEA_RGB))
    if _OUTLINE is not None:
        rings, coast = _OUTLINE
        ax.add_collection(PolyCollection(rings, facecolors=[rgb01(LAND_RGB)], edgecolors="none",
                                         zorder=0.5))
        ax.plot(coast[:, 0], coast[:, 1], color=rgb01(COASTLINE_RGB), linewidth=0.5,
                zorder=coast_zorder)
    ax.set_xlim(extent[0], extent[1])
    ax.set_ylim(extent[2], extent[3])
    ax.set_aspect(1 / np.cos(np.radians(aspect_lat)))
    ax.grid(color="#ffffff", linewidth=0.5, alpha=0.35)
    xs, ys = _ticks(extent[0], extent[1]), _ticks(extent[2], extent[3])
    ax.set_xticks(xs, [f"{x}°E" for x in xs])
    ax.set_yticks(ys, [f"{y}°N" for y in ys])
    ax.tick_params(colors=AXIS_FG, labelsize=8, length=0)
    for s in ax.spines.values():
        s.set_color(FRAME)
    return ax
