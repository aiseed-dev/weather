# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Yasuhiro / AIseed

"""Nowcast frames for the GPU map (flet-gpufield).

The radar arrives as Web-Mercator tiles (``jma_radar_service``) and the
GPU view draws a regular latitude/longitude grid, so each frame is
resampled onto one, keeping the strongest class where several radar
pixels meet a cell (the same rule as the PNG figure). The field's value
is the class — 0 no echo, 1 … 8 JMA's bins, 9 no data (outside the
radars' range, or a radar down) — and is drawn with nearest sampling:
classes are categories, and interpolating between them would draw
intensities that were never observed.

Areas and their tile zooms are ``_japan_map.AREAS`` (``moving_zoom``):
zoom-6 tiles (~2 km) everywhere. JMA builds each lower zoom as the
maximum of the 4 × 4 pixels below it, so a coarser zoom inflates heavy
rain (zoom 4 tripled the area of ≥ 10 mm/h against zoom 10). A frame is
one byte per cell (``GpuFrame.scalar8``): the whole country is ~1.5 M
cells, which as 16-bit RGBA would be 6 MB a frame.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from functools import lru_cache

import numpy as np
from matplotlib.colors import to_rgb
from PIL import Image, ImageDraw

from aiseed_weather.figures._japan_map import (
    JMA_PRECIP_COLOURS,
    JMA_PRECIP_LABELS,
    JapanArea,
    outline,
)
from aiseed_weather.figures.radar_map import NO_DATA_RGBA, no_data_mask, resample_to_frame

NO_DATA = 9
N_LEVELS = NO_DATA          # values 0 … 9 span the field's range
# Readout by class: nothing for "no echo", JMA's bin, or the reason for no value.
CLASS_LABELS = ("", *(f"降水強度 {b} mm/h" for b in JMA_PRECIP_LABELS), "観測範囲外・欠測")


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


def area_grid(area: JapanArea) -> RadarGrid:
    return grid_for(area.extent, area.moving_zoom)


def pack_frame(classes: np.ndarray, lons: np.ndarray, lats: np.ndarray,
               grid: RadarGrid, no_data=None) -> bytes:
    """One frame for ``GpuFrame.scalar8``: one byte per cell, value k as k / 9 of 255.

    ``no_data`` is JMA's no-data area (polygons of (lon, lat) rings), or None.
    """
    cells = resample_to_frame(classes, lons, lats, grid.extent, grid.nx, grid.ny)
    if no_data is not None:
        cells[no_data_mask(no_data, grid.extent, grid.nx, grid.ny)] = NO_DATA
    return np.rint(cells.astype(np.float32) * (255.0 / N_LEVELS)).astype(np.uint8).tobytes()


def lut_rgba() -> bytes:
    """256 × RGBA: transparent for no echo, JMA's colours, then the no-data veil."""
    level = np.rint(np.linspace(0, N_LEVELS, 256)).astype(int)
    colours = np.array([[round(c * 255) for c in to_rgb(h)] for h in JMA_PRECIP_COLOURS])
    rgba = np.zeros((256, 4), np.uint8)
    echo = (level > 0) & (level < NO_DATA)
    rgba[echo, :3] = colours[level[echo] - 1]
    rgba[echo, 3] = 255
    rgba[level == NO_DATA] = [round(c * 255) for c in NO_DATA_RGBA]
    return rgba.tobytes()


@lru_cache(maxsize=8)
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
