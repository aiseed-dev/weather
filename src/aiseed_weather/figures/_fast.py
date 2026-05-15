# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Yasuhiro / AIseed

"""Shared helpers for the numpy + PIL fast-path renderers.

All operations stay in C-backed libraries (numpy, contourpy, PIL).
No Python pixel loops. No matplotlib / cartopy on the render path.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import numpy as np

if TYPE_CHECKING:
    from aiseed_weather.figures.regions import Region


def crop_grid(
    data: np.ndarray,
    longitudes: np.ndarray,
    latitudes: np.ndarray,
    region: "Region",
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Normalise the GRIB grid into image space + crop to region.

    Returns (data_image, lons_ascending, lats_top_down). ``data_image``
    row 0 is the northernmost row (PIL convention). Longitudes are in
    [-180, 180) and contiguous within the cropped slab.

    Accepts data shaped (lat, lon) — the ECMWF Open Data convention.
    Extra leading dimensions (e.g. a single-step time axis) are
    squeezed.
    """
    data = np.asarray(data, dtype=np.float32)
    while data.ndim > 2:
        data = data[0]
    longitudes = np.asarray(longitudes, dtype=np.float32)
    latitudes = np.asarray(latitudes, dtype=np.float32)

    # Latitudes 90 → -90 → flip to ascending for clean masking, then
    # flip again at the end so the image has north at the top.
    if latitudes[0] > latitudes[-1]:
        data = data[::-1]
        latitudes = latitudes[::-1]

    # Longitudes 0 → 359.75 → roll to [-180, 180) so regional crops
    # straddling the prime meridian work.
    if longitudes.max() > 180.0:
        is_west = longitudes >= 180.0
        new_lon = np.concatenate(
            [longitudes[is_west] - 360.0, longitudes[~is_west]],
        )
        data = np.concatenate([data[:, is_west], data[:, ~is_west]], axis=1)
        longitudes = new_lon

    if region.extent is not None:
        lon_min, lon_max, lat_min, lat_max = region.extent
        lon_mask = (longitudes >= lon_min) & (longitudes <= lon_max)
        lat_mask = (latitudes >= lat_min) & (latitudes <= lat_max)
        data = data[np.ix_(lat_mask, lon_mask)]
        longitudes = longitudes[lon_mask]
        latitudes = latitudes[lat_mask]

    # Flip vertically so row 0 = north.
    data = data[::-1]
    latitudes = latitudes[::-1]
    return data, longitudes, latitudes


def bounds_of(longitudes: np.ndarray, latitudes: np.ndarray) -> tuple[float, float, float, float]:
    """Geographic bounding box of an image-space grid: (lon_min, lon_max,
    lat_min, lat_max). Latitudes here are the top-down array from
    :func:`crop_grid`, so lat_max is at row 0 and lat_min at the last
    row."""
    return (
        float(longitudes[0]),
        float(longitudes[-1]),
        float(latitudes[-1]),
        float(latitudes[0]),
    )


def palette_to_lut(colors: list[str]) -> np.ndarray:
    """Hex color strings → (N, 3) uint8 LUT for ``np.take``-style lookup.

    Accepts colors as "#rrggbb" or "#rrggbbaa"; alpha is dropped because
    the fast renderers emit opaque RGB (transparency would force RGBA
    PNG and an alpha-aware composite).
    """
    out = np.empty((len(colors), 3), dtype=np.uint8)
    for i, c in enumerate(colors):
        c = c.lstrip("#")
        out[i, 0] = int(c[0:2], 16)
        out[i, 1] = int(c[2:4], 16)
        out[i, 2] = int(c[4:6], 16)
    return out


def apply_binned_lut(
    data: np.ndarray, bounds: np.ndarray, lut: np.ndarray,
) -> np.ndarray:
    """Discrete colormap via numpy ``digitize`` + fancy indexing.

    ``bounds`` defines the bin edges (M values → M+1 bins counting
    under-range and over-range). ``lut`` is (M+1, 3) uint8. Returns
    (H, W, 3) uint8.
    """
    indices = np.digitize(data, bounds)
    return lut[indices]
