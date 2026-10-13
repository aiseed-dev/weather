# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Yasuhiro / AIseed

"""Nowcast frames for the GPU map (flet-gpufield).

The radar arrives as Web-Mercator tiles (``jma_radar_service``) and the
GPU view draws a regular latitude/longitude grid, so each frame is
resampled onto one, keeping the strongest class where several radar
pixels meet a cell (the same rule as the PNG figure). The classes 0 … 8
are the field's value and are drawn with nearest sampling: they are
categories, and interpolating between them would draw intensities that
were never observed.

The whole country uses zoom-4 tiles (~8 km), the ones JMA's own
nationwide animation shows; a region uses zoom-6 tiles (~2 km). Either
way a frame is 4 – 9 tiles, so the past hour and the hour ahead
(25 frames) arrive in under a minute at the four requests a second JMA
is asked for.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from functools import lru_cache

import numpy as np
from matplotlib.colors import to_rgb
from PIL import Image, ImageDraw

from aiseed_weather.figures._japan_map import (
    EXTENT,
    JMA_PRECIP_COLOURS,
    JMA_PRECIP_LABELS,
    outline,
)
from aiseed_weather.figures.radar_map import resample_to_frame

N_CLASSES = 8
# Readout names by class; class 0 (no echo) shows no value.
CLASS_LABELS = ("", *JMA_PRECIP_LABELS)


@dataclass(frozen=True)
class RadarGrid:
    """``ny × nx`` cells tiling ``extent``; row 0 is the northern edge."""

    extent: tuple[float, float, float, float]
    nx: int
    ny: int

    @property
    def dlon(self) -> float:
        return (self.extent[1] - self.extent[0]) / self.nx

    @property
    def dlat(self) -> float:
        return (self.extent[3] - self.extent[2]) / self.ny

    @property
    def lon0(self) -> float:
        """Longitude of the first column's cell centre."""
        return self.extent[0] + self.dlon / 2

    @property
    def lat0(self) -> float:
        """Latitude of the first (northern) row's cell centre."""
        return self.extent[3] - self.dlat / 2


def grid_for(extent: tuple[float, float, float, float], zoom: int) -> RadarGrid:
    """Cells about one tile pixel across: the radar's own resolution at ``zoom``."""
    px = 360.0 / (256 * 2 ** zoom)
    mid = math.radians((extent[2] + extent[3]) / 2)
    return RadarGrid(extent, nx=round((extent[1] - extent[0]) / px),
                     ny=round((extent[3] - extent[2]) / (px * math.cos(mid))))


@dataclass(frozen=True)
class RadarArea:
    key: str
    label: str
    extent: tuple[float, float, float, float]
    zoom: int

    @property
    def grid(self) -> RadarGrid:
        return grid_for(self.extent, self.zoom)


# Regions stay inside the Japan frame, whose outline the land mask and
# coastline come from.
AREAS = (
    RadarArea("japan", "全国（約 8 km）", EXTENT, 4),
    RadarArea("north", "北日本（約 2 km）", (138.5, 146.5, 38.5, 45.8), 6),
    RadarArea("east", "東日本（約 2 km）", (135.0, 142.5, 33.8, 39.5), 6),
    RadarArea("west", "西日本（約 2 km）", (129.0, 136.4, 31.0, 36.5), 6),
    RadarArea("southwest", "南西諸島（約 2 km）", (122.5, 131.0, 24.0, 30.5), 6),
)


def area(key: str) -> RadarArea:
    return next((a for a in AREAS if a.key == key), AREAS[0])


def pack_frame(classes: np.ndarray, lons: np.ndarray, lats: np.ndarray,
               grid: RadarGrid) -> bytes:
    """One frame as the field's RGBA: class k is k / 8 of the 16-bit range."""
    cells = resample_to_frame(classes, lons, lats, grid.extent, grid.nx, grid.ny)
    q = cells.astype(np.uint32) * 65535 // N_CLASSES
    rgba = np.zeros(cells.shape + (4,), np.uint8)
    rgba[..., 0] = q >> 8
    rgba[..., 1] = q & 255
    rgba[..., 3] = 255
    return rgba.tobytes()


def lut_rgba() -> bytes:
    """256 × RGBA: JMA's colour for classes 1 … 8, transparent for 0 (no echo)."""
    cls = np.rint(np.linspace(0, N_CLASSES, 256)).astype(int)
    colours = np.array([[round(c * 255) for c in to_rgb(h)] for h in JMA_PRECIP_COLOURS])
    rgba = np.zeros((256, 4), np.uint8)
    echo = cls > 0
    rgba[echo, :3] = colours[cls[echo] - 1]
    rgba[echo, 3] = 255
    return rgba.tobytes()


@lru_cache(maxsize=4)
def land_mask(grid: RadarGrid) -> bytes:
    """1 byte per cell, 1 on land (Natural Earth 10m, the AMeDAS map's outline)."""
    img = Image.new("L", (grid.nx, grid.ny), 0)
    shapes = outline()
    if shapes is not None:
        draw = ImageDraw.Draw(img)
        for ring in shapes[0]:
            # Cell (i, j) is centred on integer coordinates, as PIL draws them.
            x = (ring[:, 0] - grid.lon0) / grid.dlon
            y = (grid.lat0 - ring[:, 1]) / grid.dlat
            draw.polygon(list(zip(x.tolist(), y.tolist())), fill=1)
    return np.asarray(img, np.uint8).tobytes()


@lru_cache(maxsize=1)
def coastline() -> bytes:
    """float32 (lon, lat) vertices, NaN between lines (the AMeDAS map's 10m coastline)."""
    shapes = outline()
    if shapes is None:
        return b""
    return shapes[1].astype("<f4").tobytes()
