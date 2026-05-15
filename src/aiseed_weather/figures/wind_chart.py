# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Yasuhiro / AIseed

"""10 m wind chart — speed shading + direction arrows, fast pipeline.

Same C-backed pipeline as the other layers (numpy LUT + PIL). The
direction arrows are subsampled and drawn as line segments + a small
chevron head via :class:`PIL.ImageDraw.Draw.line`, which is a C call.
No matplotlib quiver, no per-vertex Python loop.

Skipped vs. the matplotlib version (Stage 2 work):
* MSL overlay (msl_overlay_ds) — argument kept for API compat,
  ignored on this path.
* Coastlines / gridlines / colorbar / title / footer.
"""

from __future__ import annotations

import io
import math
from typing import TYPE_CHECKING

import numpy as np
from PIL import Image, ImageDraw

from aiseed_weather.figures._fast import (
    apply_binned_lut, crop_grid, is_polar, palette_to_lut, shade_for_region,
)
from aiseed_weather.figures.regions import GLOBAL

if TYPE_CHECKING:
    import xarray as xr

    from aiseed_weather.figures.regions import Region


# Wind speed bin edges in m/s (Beaufort-ish progression).
WIND_BOUNDS_MS: np.ndarray = np.array(
    [0, 2, 5, 8, 10, 12, 15, 20, 25, 30, 40, 50, 60], dtype=np.float32,
)

# 14 entries: under (calm) + 13 bins.
_WIND_PALETTE: list[str] = [
    "#e6f4f5",  # under: ~calm
    "#e6f4f5", "#b8e0e8", "#83c8d4", "#52b0c0", "#3a98ad",
    "#7cba74", "#bccf4d", "#f3d33d", "#f59a35", "#e9572a",
    "#a72333", "#5a155f", "#5a155f",
]
_WIND_LUT: np.ndarray = palette_to_lut(_WIND_PALETTE)

# Arrow density targets — keep total arrows ≤ ~700 so the line-drawing
# loop stays inside a few-millisecond budget on full global resolution.
_TARGET_ARROWS_GLOBAL = (32, 18)   # (lon, lat)
_TARGET_ARROWS_REGION = (24, 16)


def _extract_uv10(ds: "xr.Dataset") -> tuple[np.ndarray, np.ndarray]:
    u = v = None
    for name in ("u10", "10u"):
        if name in ds.data_vars:
            u = np.asarray(ds[name].values, dtype=np.float32)
            break
    for name in ("v10", "10v"):
        if name in ds.data_vars:
            v = np.asarray(ds[name].values, dtype=np.float32)
            break
    if u is None or v is None:
        raise ValueError(
            f"No 10 m wind components in dataset; "
            f"vars={list(ds.data_vars)}",
        )
    return u, v


def _arrow_steps(shape: tuple[int, int], region: "Region") -> tuple[int, int]:
    """Pick (step_lat, step_lon) so the total arrow count stays bounded."""
    n_lat, n_lon = shape
    target_lon, target_lat = (
        _TARGET_ARROWS_GLOBAL if region.extent is None
        else _TARGET_ARROWS_REGION
    )
    return max(1, n_lat // target_lat), max(1, n_lon // target_lon)


def _draw_arrows(
    draw: ImageDraw.ImageDraw,
    u: np.ndarray,
    v: np.ndarray,
    step_lat: int,
    step_lon: int,
    pixel_per_ms: float,
) -> None:
    """Rasterise direction arrows onto ``draw``.

    Subsamples u/v to (lat//step, lon//step), then draws each arrow
    as a shaft + 2-line chevron head. PIL.ImageDraw.line is C-level;
    the Python loop here is ~700 iterations at most, each invoking
    one C call per line segment.
    """
    u_sub = u[::step_lat, ::step_lon]
    v_sub = v[::step_lat, ::step_lon]
    h, w = u_sub.shape

    # Pixel coordinates of arrow bases — image rows go top→bottom but
    # u/v have already been flipped to match by crop_grid earlier.
    xs = (np.arange(w) + 0.5) * step_lon
    ys = (np.arange(h) + 0.5) * step_lat

    # Vector → pixel deltas. v points geographic-north (positive lat) so
    # in image space we flip its sign (image y grows downward).
    dx = (u_sub * pixel_per_ms).astype(np.float32)
    dy = (-v_sub * pixel_per_ms).astype(np.float32)

    head_len = 4.0
    head_angle = math.radians(28.0)
    cos_h, sin_h = math.cos(head_angle), math.sin(head_angle)

    for j in range(h):
        for i in range(w):
            dxi = float(dx[j, i])
            dyi = float(dy[j, i])
            if dxi == 0.0 and dyi == 0.0:
                continue
            x0 = float(xs[i])
            y0 = float(ys[j])
            x1 = x0 + dxi
            y1 = y0 + dyi
            draw.line(
                [(x0, y0), (x1, y1)], fill=(0, 0, 0), width=1,
            )
            # Chevron head: rotate the shaft vector by ±head_angle
            # and scale to head_len, then draw two short lines back
            # from the tip.
            mag = math.hypot(dxi, dyi)
            if mag < 1.0:
                continue
            ux, uy = dxi / mag, dyi / mag
            # Rotate (ux, uy) by ±head_angle for the two head legs.
            for sign in (+1, -1):
                rx = ux * cos_h - sign * uy * sin_h
                ry = uy * cos_h + sign * ux * sin_h
                draw.line(
                    [(x1, y1), (x1 - rx * head_len, y1 - ry * head_len)],
                    fill=(0, 0, 0), width=1,
                )


def render_wind(
    ds: "xr.Dataset",
    *,
    region: "Region" = GLOBAL,
    run_id: str,
    msl_overlay_ds: "xr.Dataset | None" = None,
) -> bytes:
    u, v = _extract_uv10(ds)
    wspd = np.hypot(u, v)
    longitudes = ds["longitude"].values
    latitudes = ds["latitude"].values

    rgb = shade_for_region(
        lambda arr: apply_binned_lut(arr, WIND_BOUNDS_MS, _WIND_LUT),
        wspd, longitudes, latitudes, region,
    )
    from aiseed_weather.figures._coastlines import apply_coastlines
    apply_coastlines(rgb, region.key)
    img = Image.fromarray(rgb, mode="RGB")

    # Direction arrows on PlateCarree only. Polar would need a per-
    # vertex forward projection of u/v; the speed shading already
    # carries the magnitude information.
    if not is_polar(region):
        u_img, _, _ = crop_grid(u, longitudes, latitudes, region)
        v_img, _, _ = crop_grid(v, longitudes, latitudes, region)
        draw = ImageDraw.Draw(img)
        step_lat, step_lon = _arrow_steps(u_img.shape, region)
        pixel_per_ms = min(step_lon, step_lat) * 0.35
        _draw_arrows(draw, u_img, v_img, step_lat, step_lon, pixel_per_ms)

    buf = io.BytesIO()
    from PIL import PngImagePlugin

    info = PngImagePlugin.PngInfo()
    info.add_text("Software", "aiseed-weather")
    info.add_text("Source", "ECMWF Open Data (CC-BY-4.0)")
    info.add_text("Run", run_id)
    info.add_text("Layer", "wind10m")
    img.save(buf, format="PNG", pnginfo=info)
    return buf.getvalue()
