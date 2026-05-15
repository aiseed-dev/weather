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
from scipy.ndimage import gaussian_filter

from aiseed_weather.figures._basemap import base_map_rgb
from aiseed_weather.figures._coastlines import apply_coastlines
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
    """256 × 3 uint8 LUT, green-low to brown-high through warm beige.

    The previous calibration desaturated the anchors to compensate
    for an opaque alpha-blend, which then forced alpha up to make
    the colours visible, which hid the base map. Inverting the
    trade-off: pull the anchors back toward pure hues so the colours
    carry weight at a lower alpha, letting the gray land/sea cue
    show through cleanly.
    """
    # Anchor (data_value, RGB). Anchors clustered near the typical
    # data range (980–1030 hPa over a regional view) so the visible
    # part of the LUT lands in the saturated middle bands, not in
    # the pale centre.
    anchors: list[tuple[float, tuple[int, int, int]]] = [
        (940.0,  (38, 110, 55)),    # deep saturated green
        (990.0,  (115, 180, 110)),  # lime green
        (1013.0, (230, 210, 165)),  # warm beige (atmosphere mean)
        (1030.0, (215, 140, 75)),   # saturated orange-tan
        (1064.0, (135, 55, 25)),    # deep rust brown
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
# Calibration point — see skill. Tied to the palette saturation:
# bumping the LUT anchors toward pure hues lets us drop alpha back
# to 0.45 so the gray base map (land vs sea) reads clearly under
# the data. The two knobs trade off — saturated palette + low
# alpha beats desaturated palette + high alpha because the latter
# kills the geographic cue.
_DATA_ALPHA = 0.45


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
# Supersample-scaled font so the pill stays the same visual size after
# the final Lanczos downsample. Kept as a calibrated multiple of the
# base font rather than re-derived each call.
_PILL_FONT_SS = _load_font(11 * 2)
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
# Calibration points (see skill). The pipeline renders isolines at
# ISOLINE_SUPERSAMPLE × native resolution with width=1, then Lanczos-
# downsamples to native — yielding an *effective* 1/SUPERSAMPLE px
# line that reads as thin and antialiased without depending on a
# specific drawing toolkit's stroke model.
_ISOLINE_SUPERSAMPLE = 2
_ISOLINE_WIDTH_THIN = 1   # at the supersample resolution
_ISOLINE_WIDTH_BOLD = 2   # at the supersample resolution
_THIN_INTERVAL_HPA = 2
_BOLD_INTERVAL_HPA = 20
# Pre-contour Gaussian smoothing. σ=3 grid cells (≈0.75° at 0.25°
# resolution) suppresses the small-scale noise that turns 2 hPa
# isobars into a tangle of fragments near orography and weak
# pressure gradients, without rounding off any synoptic feature.
_SMOOTH_SIGMA = 3.0
# Drop polyline fragments shorter than this many contourpy vertices.
# A 30-vertex line at the 0.25° grid is ~7.5° of arc — well above
# the resolution where short fragments stop carrying information.
_MIN_SEGMENT_VERTICES = 30


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
        # Coastline ON TOP of the alpha-blended composite so the line
        # keeps its full luminance instead of being diluted to a
        # mid-tone by the blend.
        apply_coastlines(final, region.key)
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

    # 1. base map (layer 1) — sea / land only
    base = base_map_rgb(region.key)
    if base is None or base.shape != (h, w, 3):
        base = np.full((h, w, 3), 110, dtype=np.uint8)

    # 2. data overlay (layer 2) — continuous LUT, alpha-blended
    norm = np.clip(
        (msl_hpa - _VMIN_HPA) / (_VMAX_HPA - _VMIN_HPA), 0.0, 1.0,
    )
    data_rgb = _LUT[(norm * 255.0).astype(np.uint8)]
    final = _alpha_blend(base, data_rgb, _DATA_ALPHA)

    # 3. isolines + pills (layers 3–4) — drawn on a SUPER-SAMPLED
    # copy. Upsample the composite with NEAREST so the gray base
    # stays crisp, then draw white lines at width=1 in the
    # supersample frame. A final LANCZOS downsample yields an
    # antialiased ~0.5 px line on the native frame.
    #
    # The coastline is deliberately NOT stamped on the composite
    # yet — it would be blurred by the LANCZOS round-trip, washing
    # out from the chosen near-black to a muddy mid-tone. We stamp
    # it after step 5 instead.
    ss = _ISOLINE_SUPERSAMPLE
    H, W = h * ss, w * ss
    img_ss = Image.fromarray(final, mode="RGB").resize(
        (W, H), Image.NEAREST,
    )
    draw_ss = ImageDraw.Draw(img_ss)

    # Pre-smooth the MSL field to suppress small-scale noise. Without
    # this every weak gradient near orography spawns a litter of
    # short isobar fragments that clutter the chart but don't carry
    # synoptic meaning.
    smoothed = gaussian_filter(msl_hpa, sigma=_SMOOTH_SIGMA)

    x_ss = np.arange(w, dtype=np.float32) * ss
    y_ss = np.arange(h, dtype=np.float32) * ss
    cgen = contourpy.contour_generator(x=x_ss, y=y_ss, z=smoothed)

    pill_candidates: list[tuple[int, np.ndarray]] = []
    for level in range(940, 1064, _THIN_INTERVAL_HPA):
        is_bold = (level % _BOLD_INTERVAL_HPA == 0)
        width = _ISOLINE_WIDTH_BOLD if is_bold else _ISOLINE_WIDTH_THIN
        for line in cgen.lines(float(level)):
            if line.shape[0] < _MIN_SEGMENT_VERTICES:
                continue
            draw_ss.line(
                line.astype(np.int32).tolist(),
                fill=_ISOLINE_RGB,
                width=width,
            )
            if is_bold:
                pill_candidates.append((level, line))

    # Pills also drawn in the supersample frame so their rounded
    # corners benefit from the Lanczos downsample.
    for level, line in pill_candidates:
        anchor = _pick_pill_anchor(line)
        if anchor is None:
            continue
        cx, cy = anchor
        _draw_pill(
            draw_ss, cx, cy, str(level),
            bg_rgb=_palette_rgb_for(float(level)),
            font=_PILL_FONT_SS,
        )

    img = img_ss.resize((w, h), Image.LANCZOS)

    # 5. coastline (layer 5) — stamped on the native-resolution
    # output so it stays as a crisp 1 px near-black line. Earlier
    # this was applied before the supersample round-trip and the
    # LANCZOS downsample blurred it to a muddy mid-tone.
    final_arr = np.asarray(img, dtype=np.uint8).copy()
    apply_coastlines(final_arr, region.key)
    img = Image.fromarray(final_arr, mode="RGB")

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
