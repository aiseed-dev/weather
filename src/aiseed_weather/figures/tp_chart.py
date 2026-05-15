# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Yasuhiro / AIseed

"""Total precipitation — colour-only layered chart.

Read .agents/skills/chart-base-design before editing. Precipitation is
in the colour-only family: the surface field is too noisy /
spatially-discontinuous for clean iso-lines, so the chart relies on
the data palette alone.

Palette is calibrated to Windy's 3-hour precipitation legend
(1.5 / 2 / 3 / 7 / 10 / 20 / 30 mm). For ECMWF Open Data the ``tp``
variable is *cumulative* since the run start — caller is responsible
for differencing between consecutive steps if a 3-hour accumulation
is what the analyst actually wants on screen.

Two precipitation-specific traits the renderer handles:

  * **Below-threshold transparency.** Values below the lowest legend
    tick (1 mm) read as 'effectively no precipitation' — the base
    map shows through unchanged instead of getting a stale-faint
    colour wash everywhere it drizzled trace amounts.
  * **Non-uniform palette anchors.** The legend ticks are placed
    1.5, 2, 3, 7, 10, 20, 30 — sub-mm precision below 3 mm, then
    coarse steps for the heavier bands. np.interp handles the
    non-uniform x positions natively when sampling 256 LUT entries.
"""

from __future__ import annotations

import io
from typing import TYPE_CHECKING

import numpy as np
from PIL import Image

from aiseed_weather.figures._basemap import base_map_rgb
from aiseed_weather.figures._coastlines import apply_coastlines
from aiseed_weather.figures._fast import (
    apply_polar_reindex, is_polar, source_grid_for_global,
)
from aiseed_weather.figures.msl_chart import (
    _blend_with_transparency, _to_pixel_grid,
    _png_metadata as _msl_png_metadata,
)
from aiseed_weather.figures.regions import GLOBAL

if TYPE_CHECKING:
    import xarray as xr

    from aiseed_weather.figures.regions import Region


# ── Palette ─────────────────────────────────────────────────────────
_VMIN_MM = 0.0
_VMAX_MM = 30.0
# Windy 3-hour precipitation legend: 1.5, 2, 3, 7, 10, 20, 30 mm.
# Note the dense sub-mm cadence at the low end and the wider gap
# above 10 mm — operational reading wants to distinguish a light
# shower from drizzle but treats 20+ mm as 'heavy regardless'.
_LEGEND_TICKS_MM = (1.5, 2.0, 3.0, 7.0, 10.0, 20.0, 30.0)
# Pixels below 1 mm (just under the lowest tick) read as "effectively
# no precipitation" and pass through to the base map unchanged.
_DRY_THRESHOLD_MM = 1.0


def _build_sequential_lut() -> np.ndarray:
    """Sequential pale-cyan → blue → green → yellow → orange → magenta.

    Anchors line up with the Windy legend tick values. Linear
    interpolation between them, then sampled 256 times for the uint8
    LUT. Anchor RGB values are first-cut approximations of Windy's
    palette read from the screenshot the user shared on 2026-05-15;
    expect them to drift as the calibration converges.
    """
    anchors: list[tuple[float, tuple[int, int, int]]] = [
        (1.5,   (170, 220, 230)),   # pale cyan
        (2.0,   (120, 195, 220)),   # light cyan-blue
        (3.0,   (60, 140, 200)),    # mid blue
        (7.0,   (60, 175, 105)),    # green
        (10.0,  (220, 215, 75)),    # yellow
        (20.0,  (230, 145, 55)),    # orange
        (30.0,  (190, 80, 140)),    # magenta-pink
    ]
    xs = np.array(
        [(v - _VMIN_MM) / (_VMAX_MM - _VMIN_MM) for v, _ in anchors],
        dtype=np.float32,
    )
    rgb = np.array([c for _, c in anchors], dtype=np.float32)
    t = np.linspace(0.0, 1.0, 256, dtype=np.float32)
    out = np.empty((256, 3), dtype=np.float32)
    for ch in range(3):
        out[:, ch] = np.interp(t, xs, rgb[:, ch])
    return np.clip(out, 0, 255).astype(np.uint8)


_LUT: np.ndarray = _build_sequential_lut()


# Higher data weight than MSL/t2m: when a pixel IS wet, the analyst
# wants the precipitation band to read clearly. The 0.1 mm threshold
# below keeps dry regions free of the overlay entirely, so there's
# no land/sea visibility cost from being opaque where it matters.
_DATA_TRANSPARENCY = 0.20


# ── Entry point ─────────────────────────────────────────────────────


def _extract_tp_mm(ds: "xr.Dataset") -> np.ndarray:
    """ECMWF ``tp`` is metres of accumulated precipitation since the
    run start. Convert to mm here so the rest of the file is
    unit-clean."""
    if "tp" in ds.data_vars:
        return np.asarray(ds["tp"].values, dtype=np.float32) * 1000.0
    raise ValueError(
        f"No 'tp' (total precipitation) variable in dataset; "
        f"vars={list(ds.data_vars)}",
    )


def render_tp(
    ds: "xr.Dataset",
    *,
    region: "Region" = GLOBAL,
    run_id: str,
    msl_overlay_ds: "xr.Dataset | None" = None,  # API compat; unused
) -> bytes:
    tp_mm = _extract_tp_mm(ds)
    longitudes = np.asarray(ds["longitude"].values, dtype=np.float32)
    latitudes = np.asarray(ds["latitude"].values, dtype=np.float32)

    if is_polar(region):
        tp_global = source_grid_for_global(tp_mm, longitudes, latitudes)
        norm = np.clip((tp_global - _VMIN_MM) / (_VMAX_MM - _VMIN_MM), 0.0, 1.0)
        data_rgb = _LUT[(norm * 255.0).astype(np.uint8)]
        data_polar = apply_polar_reindex(data_rgb, region.key)
        base = base_map_rgb(region.key)
        if base is not None and base.shape == data_polar.shape:
            blended = _blend_with_transparency(base, data_polar, _DATA_TRANSPARENCY)
            mask = tp_global >= _DRY_THRESHOLD_MM
            # mask is on the source grid, reindex it through the polar
            # lookup so it lines up with data_polar's pixels
            from aiseed_weather.figures._fast import _polar_lookups
            tbl = _polar_lookups().get(region.key)
            if tbl is not None:
                lat_row, lon_col, valid = tbl
                mask_polar = mask[lat_row, lon_col] & valid
                final = base.copy()
                final[mask_polar] = blended[mask_polar]
            else:
                final = blended
        else:
            final = data_polar
        apply_coastlines(final, region.key)
        img = Image.fromarray(final, mode="RGB")
        buf = io.BytesIO()
        img.save(
            buf, format="PNG", compress_level=1,
            pnginfo=_png_metadata(run_id),
        )
        return buf.getvalue()

    tp_mm, longitudes, latitudes = _to_pixel_grid(
        tp_mm, longitudes, latitudes, region,
    )
    h, w = tp_mm.shape

    # 1. base map at native
    base = base_map_rgb(region.key)
    if base is None or base.shape != (h, w, 3):
        base = np.full((h, w, 3), 110, dtype=np.uint8)

    # 2. data overlay — only where there's actually precipitation.
    # Dry pixels (< 0.1 mm) keep the base map unchanged; wet pixels
    # get the blended palette colour.
    norm = np.clip((tp_mm - _VMIN_MM) / (_VMAX_MM - _VMIN_MM), 0.0, 1.0)
    data_rgb = _LUT[(norm * 255.0).astype(np.uint8)]
    blended = _blend_with_transparency(base, data_rgb, _DATA_TRANSPARENCY)
    mask = tp_mm >= _DRY_THRESHOLD_MM
    final_arr = base.copy()
    final_arr[mask] = blended[mask]

    # 3. coastline on top
    apply_coastlines(final_arr, region.key)

    buf = io.BytesIO()
    Image.fromarray(final_arr, mode="RGB").save(
        buf, format="PNG", compress_level=1,
        pnginfo=_png_metadata(run_id),
    )
    return buf.getvalue()


def _png_metadata(run_id: str):
    info = _msl_png_metadata(run_id)
    info.add_text("Layer", "tp")
    return info
