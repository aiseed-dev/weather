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


# ── Polar reindex ────────────────────────────────────────────────────
# For ARCTIC / ANTARCTIC the renderer takes the *source* 721×1440 RGB
# array (produced by the normal LUT step against a global-extent crop)
# and reindexes it into an 800×800 polar disc using a precomputed
# lookup table. The lookup table is generated once on a developer
# machine — see _precompute_coastlines.py — and shipped as a .npz.


_POLAR_LOOKUPS_CACHE: "dict[str, tuple[np.ndarray, np.ndarray, np.ndarray]] | None" = None
# Out-of-disc pixels render as this background (dark grey to match the
# Flet dark theme; light-mode users see a soft contrast against panels).
_POLAR_BACKGROUND = np.array([30, 30, 36], dtype=np.uint8)


def _load_polar_lookups():
    import pathlib

    path = pathlib.Path(__file__).parent / "_polar_lookups.npz"
    if not path.exists():
        return {}
    out: dict[str, tuple[np.ndarray, np.ndarray, np.ndarray]] = {}
    with np.load(path) as data:
        keys = sorted({name.split("__")[0] for name in data.files})
        for key in keys:
            try:
                lat_row = np.asarray(data[f"{key}__lat_row"], dtype=np.int32)
                lon_col = np.asarray(data[f"{key}__lon_col"], dtype=np.int32)
                valid = np.asarray(data[f"{key}__valid"], dtype=bool)
            except KeyError:
                continue
            out[key] = (lat_row, lon_col, valid)
    return out


def _polar_lookups() -> "dict[str, tuple[np.ndarray, np.ndarray, np.ndarray]]":
    global _POLAR_LOOKUPS_CACHE
    if _POLAR_LOOKUPS_CACHE is None:
        _POLAR_LOOKUPS_CACHE = _load_polar_lookups()
    return _POLAR_LOOKUPS_CACHE


def is_polar(region: "Region") -> bool:
    return region.projection in ("north_polar", "south_polar")


def apply_polar_reindex(
    rgb_source: np.ndarray, region_key: str,
) -> np.ndarray:
    """Reindex a global (721, 1440, 3) RGB into the precomputed polar
    disc for ``region_key``.

    ``rgb_source`` is the colour-shaded global field at the ECMWF
    0.25° grid AFTER the standard top-down lat / [-180, 180) lon
    normalisation (see :func:`crop_grid` with ``region=GLOBAL``).
    The lookup table indices match that frame.

    Out-of-disc pixels are filled with a neutral background so the
    chart reads as a circular medallion against the panel.
    """
    table = _polar_lookups().get(region_key)
    if table is None:
        return rgb_source
    lat_row, lon_col, valid = table
    out = rgb_source[lat_row, lon_col]
    out[~valid] = _POLAR_BACKGROUND
    return out


def source_grid_for_global(
    raw_data: np.ndarray,
    longitudes: np.ndarray,
    latitudes: np.ndarray,
) -> np.ndarray:
    """Normalise raw GRIB grid orientation without cropping.

    Used by polar renderers, which need the *global* source field in
    the standard top-down / [-180, 180) frame so the polar lookup
    indices land on the right cells.
    """
    from aiseed_weather.figures.regions import GLOBAL

    data, _, _ = crop_grid(raw_data, longitudes, latitudes, GLOBAL)
    return data


def shade_for_region(
    value_to_rgb,
    raw_data: np.ndarray,
    longitudes: np.ndarray,
    latitudes: np.ndarray,
    region: "Region",
) -> np.ndarray:
    """Single dispatch for PlateCarree crop vs. polar reindex.

    ``value_to_rgb`` is a callable that takes a 2D value array and
    returns the (H, W, 3) uint8 colour-shaded image. For PlateCarree
    regions we crop the raw data to the region extent first; for
    polar regions we shade the whole global field then reindex
    through the precomputed lookup. Either way the caller is shielded
    from the projection split.
    """
    if is_polar(region):
        data_global = source_grid_for_global(raw_data, longitudes, latitudes)
        return apply_polar_reindex(value_to_rgb(data_global), region.key)
    data, _, _ = crop_grid(raw_data, longitudes, latitudes, region)
    return value_to_rgb(data)
