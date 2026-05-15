# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Yasuhiro / AIseed

"""Mean sea level pressure chart — fast numpy + PIL pipeline.

Replaces the cartopy + matplotlib renderer (5-15 s/frame) with a pure
matrix pipeline that stays in C-backed libraries:

1. Extract MSL slab from xarray → numpy array (Pa → hPa).
2. Optional region crop via numpy slicing.
3. Apply diverging colormap LUT (numpy fancy indexing) → RGB array.
4. Compute isobar lines via contourpy (C++).
5. Rasterize lines onto the RGB image via PIL.ImageDraw (C).
6. Encode PNG via PIL (C).

No matplotlib, no cartopy, no Python pixel loops. Per-frame cost is
~150–400 ms for a global field at native resolution, dominated by the
contour computation. Threading/pooling is therefore unnecessary at this
scale — the original 5-15 s/frame motivated process-pool fanout, but
once the per-frame work fits inside a typical UI frame budget the
serial path is simpler and equally fast for the realistic ≤65-frame
plan.

Projection support
------------------
The fast path uses a direct lat/lon → pixel mapping — equivalent to
PlateCarree. For regional presets (Japan, East Asia, etc.) this is
visually correct for the small extents involved. Global views are
also rendered in PlateCarree rather than the previous Robinson; the
user gives up the pole-friendly aesthetic in exchange for an order-
of-magnitude speed-up. A Robinson/Mercator mode can be added later
with a numpy lookup-table reprojection if needed.
"""

from __future__ import annotations

import io
from typing import TYPE_CHECKING

import numpy as np
from PIL import Image, ImageDraw

import contourpy

if TYPE_CHECKING:
    import xarray as xr

    from aiseed_weather.figures.regions import Region


# Display range — same convention as the synoptic charts the user is
# used to. Standard atmosphere (1013.25 hPa) sits at LUT index 128.
_VMIN_HPA = 940.0
_VMAX_HPA = 1064.0


def _build_diverging_lut() -> np.ndarray:
    """Diverging blue→white→red LUT, 256 entries × 3 channels uint8.

    Computed in numpy with linear interpolation between three stops —
    no matplotlib import on the hot path.
    """
    t = np.linspace(0.0, 1.0, 256, dtype=np.float32)
    blue = np.array([25, 50, 130], dtype=np.float32)
    white = np.array([255, 255, 255], dtype=np.float32)
    red = np.array([130, 25, 50], dtype=np.float32)

    out = np.empty((256, 3), dtype=np.float32)
    lo_mask = t < 0.5
    s_lo = (t[lo_mask] * 2.0)[:, None]
    out[lo_mask] = blue * (1 - s_lo) + white * s_lo

    hi_mask = ~lo_mask
    s_hi = ((t[hi_mask] - 0.5) * 2.0)[:, None]
    out[hi_mask] = white * (1 - s_hi) + red * s_hi

    return np.clip(out, 0, 255).astype(np.uint8)


_LUT: np.ndarray = _build_diverging_lut()  # (256, 3) uint8


def _to_pixel_grid(
    msl_hpa: np.ndarray,
    longitudes: np.ndarray,
    latitudes: np.ndarray,
    region: "Region",
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Normalise the GRIB grid for image-space rendering.

    Returns (msl_top_down, lons_ascending, lats_top_down). The MSL
    array is reshaped so row 0 is the northernmost row (PIL convention)
    and the longitude axis is rolled to a contiguous [-180, 180) frame
    cropped to the region extent.
    """
    # Latitudes from ECMWF Open Data run 90 → -90 (decreasing). Flip to
    # ascending first so we can lat_mask cleanly; we'll flip again at the
    # end to put north at the top of the image.
    if latitudes[0] > latitudes[-1]:
        msl_hpa = msl_hpa[::-1]
        latitudes = latitudes[::-1]

    # Longitudes from ECMWF run 0 → 359.75. For regions that straddle
    # the prime meridian (e.g. Europe at -25..50) we need a [-180, 180)
    # frame. Roll the array so longitudes are contiguous in the new
    # frame.
    if longitudes.max() > 180.0:
        # Split at lon ≥ 180 and shift those to negative values; then
        # concatenate west + east halves so the new longitude axis runs
        # -180 → 180 monotonically.
        is_west = longitudes >= 180.0
        new_lon = np.concatenate(
            [longitudes[is_west] - 360.0, longitudes[~is_west]],
        )
        msl_hpa = np.concatenate(
            [msl_hpa[:, is_west], msl_hpa[:, ~is_west]], axis=1,
        )
        longitudes = new_lon

    if region.extent is not None:
        lon_min, lon_max, lat_min, lat_max = region.extent
        lon_mask = (longitudes >= lon_min) & (longitudes <= lon_max)
        lat_mask = (latitudes >= lat_min) & (latitudes <= lat_max)
        msl_hpa = msl_hpa[np.ix_(lat_mask, lon_mask)]
        longitudes = longitudes[lon_mask]
        latitudes = latitudes[lat_mask]

    # Flip vertically: row 0 must be northernmost for image-space output.
    msl_hpa = msl_hpa[::-1]
    latitudes = latitudes[::-1]
    return msl_hpa, longitudes, latitudes


def render_msl(ds: "xr.Dataset", *, region: "Region", run_id: str) -> bytes:
    """Render an MSL chart to PNG bytes.

    PlateCarree regions use the existing crop + LUT + contour path.
    Polar regions (Arctic / Antarctic centred hemispheres) bypass the
    crop and instead reindex a global LUT result through the
    precomputed polar lookup table from :mod:`_fast`. Contour drawing
    is skipped on polar projections — it would need a separate
    forward-projection step that doesn't pay for itself on the
    once-per-render budget.
    """
    from aiseed_weather.figures._coastlines import apply_coastlines
    from aiseed_weather.figures._fast import (
        apply_polar_reindex, is_polar, source_grid_for_global,
    )

    msl_hpa = (ds["msl"].values / 100.0).astype(np.float32)
    longitudes = np.asarray(ds["longitude"].values, dtype=np.float32)
    latitudes = np.asarray(ds["latitude"].values, dtype=np.float32)

    if is_polar(region):
        # Normalise to the global source frame the polar lookup was
        # built against, then color-shade and reindex.
        msl_global = source_grid_for_global(msl_hpa, longitudes, latitudes)
        norm = np.clip(
            (msl_global - _VMIN_HPA) / (_VMAX_HPA - _VMIN_HPA), 0.0, 1.0,
        )
        indices = (norm * 255.0).astype(np.uint8)
        rgb_source = _LUT[indices]
        rgb = apply_polar_reindex(rgb_source, region.key)
        apply_coastlines(rgb, region.key)
        img = Image.fromarray(rgb, mode="RGB")
        buf = io.BytesIO()
        img.save(
            buf, format="PNG", compress_level=1,
            pnginfo=_png_metadata(run_id),
        )
        return buf.getvalue()

    msl_hpa, longitudes, latitudes = _to_pixel_grid(
        msl_hpa, longitudes, latitudes, region,
    )
    h, w = msl_hpa.shape

    # ── Color shading ────────────────────────────────────────────────
    norm = np.clip(
        (msl_hpa - _VMIN_HPA) / (_VMAX_HPA - _VMIN_HPA), 0.0, 1.0,
    )
    indices = (norm * 255.0).astype(np.uint8)
    rgb = _LUT[indices]  # (h, w, 3) uint8

    # Coastlines: precomputed per-region boolean mask, stamped in place
    # via numpy fancy indexing. No projection, no line drawing — the
    # rasterisation happened once on the dev machine; runtime cost is
    # a single boolean assign.
    apply_coastlines(rgb, region.key)

    # ── Isobar overlay ───────────────────────────────────────────────
    # contourpy returns each contour level as a list of (N, 2) float
    # arrays in (x, y) pixel coordinates. We feed it grid-index axes
    # so the output is already in image-pixel space.
    x_pix = np.arange(w, dtype=np.float32)
    y_pix = np.arange(h, dtype=np.float32)
    cgen = contourpy.contour_generator(x=x_pix, y=y_pix, z=msl_hpa)

    img = Image.fromarray(rgb, mode="RGB")
    draw = ImageDraw.Draw(img)

    # Thin isobars at 4 hPa, bold every 20 hPa — synoptic convention.
    # Per-polyline Python overhead dominates the cost on the global
    # frame (~2400 segments). contourpy hands us float32 (N, 2) arrays;
    # ``.astype(int).tolist()`` materialises them as int pixel coords
    # in one C call instead of the per-element ``float(p[0])`` loop
    # the earlier comprehension forced. Saves ~70 ms on GLOBAL.
    for level in range(940, 1064, 4):
        is_bold = (level % 20 == 0)
        width = 2 if is_bold else 1
        for line in cgen.lines(float(level)):
            if len(line) >= 2:
                draw.line(
                    line.astype(np.int32).tolist(),
                    # Pale yellow isobars — Windy-style: warm-but-
                    # subtle, readable on every part of the diverging
                    # blue-white-red shading.
                    fill=(255, 240, 150),
                    width=width,
                )

    # ── Encode PNG ───────────────────────────────────────────────────
    # compress_level=1 — zlib's fastest setting. PIL's default is 6,
    # which spends ~140 ms of the budget on a 1440×721 RGB frame.
    # Level 1 produces ~30% larger files (still well under 1 MB for
    # MSL) but cuts the encode to ~25 ms. For figure export the user
    # path can opt into level 6 separately; this entry point is for
    # the live viewer.
    buf = io.BytesIO()
    img.save(
        buf,
        format="PNG",
        compress_level=1,
        # PNG metadata: run id + source attribution for downstream
        # provenance without rendering pixels for it.
        pnginfo=_png_metadata(run_id),
    )
    return buf.getvalue()


def _png_metadata(run_id: str):
    """Build a minimal PngImagePlugin.PngInfo with provenance keys.

    Lives in metadata rather than on-image text so the fast path stays
    free of pixel-space drawing. Downstream code that wants a visible
    footer can render it separately (e.g. as a Flet Text below the
    image).
    """
    from PIL import PngImagePlugin

    info = PngImagePlugin.PngInfo()
    info.add_text("Software", "aiseed-weather")
    info.add_text("Source", "ECMWF Open Data (CC-BY-4.0)")
    info.add_text("Run", run_id)
    return info
