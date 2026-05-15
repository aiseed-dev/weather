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
# Diverging green → beige → brown around the synoptic range Windy
# uses on its MSL legend (990 .. 1030 hPa, see screenshot in the
# 2026-05-15 thread). The visible part of the LUT lines up with the
# pressure values an analyst actually reads from over Asia / Pacific
# every day — the deep-low / strong-high extremes saturate at the
# ends rather than wasting the palette on values that almost never
# occur in the operational record.
_VMIN_HPA = 990.0
_VMAX_HPA = 1030.0
# Legend-tick anchor positions in hPa. Used elsewhere when drawing
# the legend bar so the ticks line up with the palette breakpoints.
_LEGEND_TICKS_HPA = (990.0, 1000.0, 1010.0, 1020.0, 1030.0)


def _build_diverging_lut() -> np.ndarray:
    """256 × 3 uint8 LUT, green-low to brown-high through warm beige.

    The previous calibration desaturated the anchors to compensate
    for an opaque alpha-blend, which then forced alpha up to make
    the colours visible, which hid the base map. Inverting the
    trade-off: pull the anchors back toward pure hues so the colours
    carry weight at a lower alpha, letting the gray land/sea cue
    show through cleanly.
    """
    # Anchors aligned with the legend ticks Windy shows (990, 1000,
    # 1010, 1020, 1030 hPa — every 10 hPa). Round numbers, not the
    # textbook 1013.25 standard atmosphere, because:
    #   * the legend bar and the palette share their reference
    #     values, so a tick label and the colour it sits on line up
    #     exactly — no off-by-3-hPa ambiguity for the analyst
    #   * 10 hPa cadence matches the synoptic conventions for
    #     legend ticks on every other map service
    #   * 1010 is closer to the real global / regional mean pressure
    #     than 1013 anyway; the textbook standard atmosphere was a
    #     reflexive choice with no meteorological necessity
    anchors: list[tuple[float, tuple[int, int, int]]] = [
        (990.0,  (38, 110, 55)),    # deep saturated green   (low)
        (1000.0, (115, 180, 110)),  # lime green
        (1010.0, (230, 210, 165)),  # warm beige             (centre)
        (1020.0, (215, 140, 75)),   # saturated orange-tan
        (1030.0, (135, 55, 25)),    # deep rust brown        (high)
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


# ── Data overlay transparency ───────────────────────────────────────
# Convention: 0 = opaque (data fully covers the base map),
#             1 = fully transparent (data invisible, base only).
# Matches the Japanese 透明度 reading; higher = more transparent.
# Internally the blend math is base * t + data * (1 - t). At t=0.30
# the final pixel is 70% data + 30% base — the data colour reads as
# a clear tinted band while the base map and coastline stay visibly
# in front. User picked this after walking the t=0.85 → 0.40 → 0.30
# sequence on a regional crop.
_DATA_TRANSPARENCY = 0.30


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


def _blend_with_transparency(
    base: np.ndarray, overlay: np.ndarray, transparency: float,
) -> np.ndarray:
    """Per-pixel ``base * t + overlay * (1-t)`` in uint8 land.

    ``transparency`` follows the 透明度 convention: 0 = opaque (data
    fully covers the base), 1 = fully transparent (data invisible).
    Doing the maths in float32 once and casting back is faster than
    PIL's per-channel blend and avoids an alpha-channel allocation.
    """
    t = float(transparency)
    out = base.astype(np.float32) * t + overlay.astype(np.float32) * (1.0 - t)
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
    bg_rgb: tuple[int, ...],
    font: ImageFont.FreeTypeFont | ImageFont.ImageFont,
) -> None:
    """Rounded-rectangle pill centred at (cx, cy).

    ``bg_rgb`` may be a 3- or 4-tuple. The text fill is RGBA-padded so
    callers drawing onto an RGBA canvas see opaque white text rather
    than the canvas's transparent base bleeding through.
    """
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
    text_fill = (*_PILL_TEXT_RGB, 255) if len(bg_rgb) == 4 else _PILL_TEXT_RGB
    draw.text(
        (cx - tw // 2 - bbox[0], cy - th // 2 - bbox[1]),
        text, fill=text_fill, font=font,
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
            final = _blend_with_transparency(base, data_polar, _DATA_TRANSPARENCY)
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

    # 1. base map (layer 1) — sea / land at native resolution.
    # CRITICAL: the base map stays at native and is never put
    # through the supersample/LANCZOS round-trip. Earlier the whole
    # composite went through that cycle, and LANCZOS averaged the
    # land/sea pixels with their neighbours — collapsing the 30 unit
    # luminance gap and making the land cue disappear at high
    # transparency.
    base = base_map_rgb(region.key)
    if base is None or base.shape != (h, w, 3):
        base = np.full((h, w, 3), 110, dtype=np.uint8)

    # 2. data overlay (layer 2) — continuous LUT, blended at native.
    # At _DATA_TRANSPARENCY = 1.0 the blend collapses to the base
    # map untouched, which is what the variable's name promises.
    norm = np.clip(
        (msl_hpa - _VMIN_HPA) / (_VMAX_HPA - _VMIN_HPA), 0.0, 1.0,
    )
    data_rgb = _LUT[(norm * 255.0).astype(np.uint8)]
    composite = _blend_with_transparency(base, data_rgb, _DATA_TRANSPARENCY)

    # 3. isolines + pills (layers 3–4) — rendered on a TRANSPARENT
    # supersample layer (RGBA), then LANCZOS-downsampled to native
    # and alpha-composited onto the data-blended base. This is the
    # single layer that benefits from the round-trip (thin antialiased
    # white lines and pill borders); the base map below is untouched.
    smoothed = gaussian_filter(msl_hpa, sigma=_SMOOTH_SIGMA)
    overlay_native = _render_isolines_and_pills(smoothed, w, h)

    base_img = Image.fromarray(composite, mode="RGB").convert("RGBA")
    base_img.alpha_composite(overlay_native)
    final_arr = np.asarray(base_img.convert("RGB"), dtype=np.uint8).copy()

    # 5. coastline (layer 5) — stamped on the native composite so
    # the line keeps its full luminance.
    apply_coastlines(final_arr, region.key)

    buf = io.BytesIO()
    Image.fromarray(final_arr, mode="RGB").save(
        buf, format="PNG", compress_level=1,
        pnginfo=_png_metadata(run_id),
    )
    return buf.getvalue()


def _render_isolines_and_pills(
    smoothed_field: np.ndarray, w: int, h: int,
) -> Image.Image:
    """Build the isoline + pill layer as an RGBA Image at native size.

    Internally renders at ``_ISOLINE_SUPERSAMPLE`` × native resolution
    onto a fully-transparent RGBA canvas, then LANCZOS-downsamples to
    (w, h). The caller alpha-composites the result over the
    data-blended base, so only this single layer pays the smoothing
    cost — the base map and the coastline stay aliased / crisp.
    """
    ss = _ISOLINE_SUPERSAMPLE
    H, W = h * ss, w * ss
    overlay_ss = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay_ss)

    x_ss = np.arange(w, dtype=np.float32) * ss
    y_ss = np.arange(h, dtype=np.float32) * ss
    cgen = contourpy.contour_generator(x=x_ss, y=y_ss, z=smoothed_field)

    pill_candidates: list[tuple[int, np.ndarray]] = []
    for level in range(940, 1064, _THIN_INTERVAL_HPA):
        is_bold = (level % _BOLD_INTERVAL_HPA == 0)
        width = _ISOLINE_WIDTH_BOLD if is_bold else _ISOLINE_WIDTH_THIN
        for line in cgen.lines(float(level)):
            if line.shape[0] < _MIN_SEGMENT_VERTICES:
                continue
            draw.line(
                line.astype(np.int32).tolist(),
                fill=(*_ISOLINE_RGB, 255),
                width=width,
            )
            if is_bold:
                pill_candidates.append((level, line))

    for level, line in pill_candidates:
        anchor = _pick_pill_anchor(line)
        if anchor is None:
            continue
        cx, cy = anchor
        r, g, b = _palette_rgb_for(float(level))
        _draw_pill(
            draw, cx, cy, str(level),
            bg_rgb=(r, g, b, 255),
            font=_PILL_FONT_SS,
        )

    return overlay_ss.resize((w, h), Image.LANCZOS)


def _png_metadata(run_id: str):
    from PIL import PngImagePlugin

    info = PngImagePlugin.PngInfo()
    info.add_text("Software", "aiseed-weather")
    info.add_text("Source", "ECMWF Open Data (CC-BY-4.0)")
    info.add_text("Run", run_id)
    info.add_text("Layer", "msl")
    return info
