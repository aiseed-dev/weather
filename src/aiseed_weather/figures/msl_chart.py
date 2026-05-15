# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Yasuhiro / AIseed

"""Mean sea level pressure — reference implementation of the
four-layer chart design.

Read .agents/skills/chart-base-design before editing. This module is
the working example of:

  1. gray base map (sea / land / dark coastline)
  2. transparent diverging data overlay (continuous LUT, alpha-blended)
  3. thin white isobars at 2 hPa
  4. pill labels placed on the isobars

Everything stays in numpy + PIL + contourpy; cartopy is precompute-
only, and matplotlib is not imported.

The values here (palette anchors, alpha, pill geometry) are first
cuts. The skill calls them out as calibration points expected to
evolve.
"""

from __future__ import annotations

import io
from typing import TYPE_CHECKING

import contourpy
import numpy as np
from PIL import Image, ImageDraw, ImageFont

from aiseed_weather.figures._basemap import base_map_rgb
from aiseed_weather.figures._fast import (
    apply_polar_reindex, is_polar, source_grid_for_global,
)

if TYPE_CHECKING:
    import xarray as xr

    from aiseed_weather.figures.regions import Region


# ── Palette ─────────────────────────────────────────────────────────
# Diverging green → beige → brown, anchored on 1013 hPa at the centre.
# Five stops are linearly interpolated to 256 LUT entries. The shades
# are taken from observation of Windy's MSL palette, not a calibrated
# colorimetric reference — adjust as needed.
_VMIN_HPA = 940.0
_VMAX_HPA = 1064.0
_VANCHOR_HPA = 1013.0


def _build_diverging_lut() -> np.ndarray:
    """256 × 3 uint8 LUT, green-low to brown-high through pale beige.

    The LUT is built once at import. The center (LUT index where
    1013 hPa lands) is the lightest neutral; values fall away
    symmetrically in luminance toward the green and brown ends.
    """
    # Anchor (data_value, RGB) pairs. The data_value is mapped to a
    # normalised position in [0, 1] using vmin / vmax.
    anchors: list[tuple[float, tuple[int, int, int]]] = [
        (940.0,  (74, 122, 78)),    # deep green
        (990.0,  (138, 176, 136)),  # pale green
        (1013.0, (216, 208, 184)),  # pale beige (atmosphere mean)
        (1030.0, (184, 144, 112)),  # tan
        (1064.0, (110, 72, 48)),    # dark brown
    ]
    xs = np.array(
        [(v - _VMIN_HPA) / (_VMAX_HPA - _VMIN_HPA) for v, _ in anchors],
        dtype=np.float32,
    )
    rgb = np.array([c for _, c in anchors], dtype=np.float32)

    t = np.linspace(0.0, 1.0, 256, dtype=np.float32)
    out = np.empty((256, 3), dtype=np.float32)
    for ch in range(3):
        out[:, ch] = np.interp(t, xs, rgb[:, ch])
    return np.clip(out, 0, 255).astype(np.uint8)


_LUT: np.ndarray = _build_diverging_lut()


def _palette_rgb_for(value_hpa: float) -> tuple[int, int, int]:
    """Look up the palette colour at one MSL value — used for pill
    label backgrounds so they share the data overlay's colour scale."""
    norm = max(0.0, min(1.0, (value_hpa - _VMIN_HPA) / (_VMAX_HPA - _VMIN_HPA)))
    idx = int(round(norm * 255.0))
    r, g, b = _LUT[idx]
    return int(r), int(g), int(b)


# ── Alpha-blend parameters ──────────────────────────────────────────
# Calibration point — see skill. 0.5 keeps the gray base map clearly
# visible while the data colour still reads as a tinted region.
_DATA_ALPHA = 0.5


# ── Pill label font ─────────────────────────────────────────────────
# DejaVu Sans is reliably present on Linux installs of matplotlib /
# fonttools; falls back to PIL's bundled default bitmap font if not.
def _load_font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    for path in (
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    ):
        try:
            return ImageFont.truetype(path, size=size)
        except OSError:
            continue
    return ImageFont.load_default()


_PILL_FONT = _load_font(11)
_PILL_PAD_X = 4
_PILL_TEXT_RGB = (255, 255, 255)


# ── Geometry helpers ────────────────────────────────────────────────


def _to_pixel_grid(
    msl_hpa: np.ndarray,
    longitudes: np.ndarray,
    latitudes: np.ndarray,
    region: "Region",
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """North-up image-space slab, cropped to ``region.extent``."""
    if latitudes[0] > latitudes[-1]:
        msl_hpa = msl_hpa[::-1]
        latitudes = latitudes[::-1]
    if longitudes.max() > 180.0:
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

    msl_hpa = msl_hpa[::-1]
    latitudes = latitudes[::-1]
    return msl_hpa, longitudes, latitudes


def _alpha_blend(
    base: np.ndarray, overlay: np.ndarray, alpha: float,
) -> np.ndarray:
    """Per-pixel ``base * (1-a) + overlay * a`` in uint8 land.

    Doing the maths in float32 once and casting back is faster than
    PIL's per-channel blend for a 1M-pixel image and avoids the alpha
    channel allocation entirely.
    """
    a = float(alpha)
    out = base.astype(np.float32) * (1.0 - a) + overlay.astype(np.float32) * a
    return np.clip(out, 0, 255).astype(np.uint8)


def _pick_pill_anchor(line: np.ndarray) -> tuple[int, int] | None:
    """Pick a point on a polyline for the pill label.

    Picks the segment with the largest horizontal extent so the pill
    sits along a roughly-horizontal stretch and reads upright. Returns
    integer pixel coordinates or ``None`` for degenerate lines.

    Placement quality is a known calibration point — see the skill.
    The current heuristic is intentionally minimal; replace when we
    have a real placement model that avoids stacking pills on top of
    coastlines and other isolines.
    """
    if line.shape[0] < 2:
        return None
    dx = np.abs(np.diff(line[:, 0]))
    if dx.size == 0:
        return None
    j = int(np.argmax(dx))
    cx = (line[j, 0] + line[j + 1, 0]) * 0.5
    cy = (line[j, 1] + line[j + 1, 1]) * 0.5
    return int(round(cx)), int(round(cy))


def _draw_pill(
    draw: ImageDraw.ImageDraw,
    cx: int, cy: int,
    text: str,
    bg_rgb: tuple[int, int, int],
    font: ImageFont.FreeTypeFont | ImageFont.ImageFont,
) -> None:
    """Rounded-rectangle pill centred at (cx, cy)."""
    bbox = draw.textbbox((0, 0), text, font=font)
    tw = bbox[2] - bbox[0]
    th = bbox[3] - bbox[1]
    pad_x = _PILL_PAD_X
    pad_y = max(1, th // 6)
    w = tw + 2 * pad_x
    h = th + 2 * pad_y
    x0 = cx - w // 2
    y0 = cy - h // 2
    radius = h // 2
    draw.rounded_rectangle(
        (x0, y0, x0 + w, y0 + h), radius=radius, fill=bg_rgb,
    )
    draw.text(
        (cx - tw // 2 - bbox[0], cy - th // 2 - bbox[1]),
        text, fill=_PILL_TEXT_RGB, font=font,
    )


# ── Entry point ─────────────────────────────────────────────────────


_ISOLINE_RGB = (255, 255, 255)            # white, single colour
_ISOLINE_WIDTH_THIN = 1
_ISOLINE_WIDTH_BOLD = 2
_THIN_INTERVAL_HPA = 2
_BOLD_INTERVAL_HPA = 20


def render_msl(ds: "xr.Dataset", *, region: "Region", run_id: str) -> bytes:
    msl_hpa = (ds["msl"].values / 100.0).astype(np.float32)
    longitudes = np.asarray(ds["longitude"].values, dtype=np.float32)
    latitudes = np.asarray(ds["latitude"].values, dtype=np.float32)

    if is_polar(region):
        # Polar path: skip isobars + pill labels (forward-projecting
        # contour polylines through the polar lookup would need a
        # separate code path, deferred until the polar view earns it).
        msl_global = source_grid_for_global(msl_hpa, longitudes, latitudes)
        norm = np.clip(
            (msl_global - _VMIN_HPA) / (_VMAX_HPA - _VMIN_HPA), 0.0, 1.0,
        )
        data_rgb = _LUT[(norm * 255.0).astype(np.uint8)]
        data_polar = apply_polar_reindex(data_rgb, region.key)
        base = base_map_rgb(region.key)
        if base is not None and base.shape == data_polar.shape:
            final = _alpha_blend(base, data_polar, _DATA_ALPHA)
        else:
            final = data_polar
        img = Image.fromarray(final, mode="RGB")
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

    # 1. base map (layer 1)
    base = base_map_rgb(region.key)
    if base is None or base.shape != (h, w, 3):
        # No precomputed base for this region — fall back to a flat
        # mid-gray. Chart still renders, just without land/sea cue.
        base = np.full((h, w, 3), 110, dtype=np.uint8)

    # 2. data overlay (layer 2) — continuous LUT, alpha-blended
    norm = np.clip(
        (msl_hpa - _VMIN_HPA) / (_VMAX_HPA - _VMIN_HPA), 0.0, 1.0,
    )
    data_rgb = _LUT[(norm * 255.0).astype(np.uint8)]
    final = _alpha_blend(base, data_rgb, _DATA_ALPHA)

    # 3. isolines (layer 3) — white, single colour
    x_pix = np.arange(w, dtype=np.float32)
    y_pix = np.arange(h, dtype=np.float32)
    cgen = contourpy.contour_generator(x=x_pix, y=y_pix, z=msl_hpa)

    img = Image.fromarray(final, mode="RGB")
    draw = ImageDraw.Draw(img)

    # Collect line geometry as we draw, so the pill pass can reuse it
    # without re-tracing.
    pill_candidates: list[tuple[int, np.ndarray]] = []
    for level in range(940, 1064, _THIN_INTERVAL_HPA):
        is_bold = (level % _BOLD_INTERVAL_HPA == 0)
        width = _ISOLINE_WIDTH_BOLD if is_bold else _ISOLINE_WIDTH_THIN
        for line in cgen.lines(float(level)):
            if line.shape[0] < 2:
                continue
            draw.line(
                line.astype(np.int32).tolist(),
                fill=_ISOLINE_RGB,
                width=width,
            )
            if is_bold and line.shape[0] >= 8:
                # First cut: pill only on bold isobars (every 20 hPa).
                # Pills on every 2 hPa line would crowd out the chart.
                pill_candidates.append((level, line))

    # 4. pill labels (layer 4)
    for level, line in pill_candidates:
        anchor = _pick_pill_anchor(line)
        if anchor is None:
            continue
        cx, cy = anchor
        _draw_pill(
            draw, cx, cy, str(level),
            bg_rgb=_palette_rgb_for(float(level)),
            font=_PILL_FONT,
        )

    buf = io.BytesIO()
    img.save(
        buf, format="PNG", compress_level=1,
        pnginfo=_png_metadata(run_id),
    )
    return buf.getvalue()


def _png_metadata(run_id: str):
    from PIL import PngImagePlugin

    info = PngImagePlugin.PngInfo()
    info.add_text("Software", "aiseed-weather")
    info.add_text("Source", "ECMWF Open Data (CC-BY-4.0)")
    info.add_text("Run", run_id)
    info.add_text("Layer", "msl")
    return info
