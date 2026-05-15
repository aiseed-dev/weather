# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Yasuhiro / AIseed

"""Shared layered chart renderer.

Read .agents/skills/chart-base-design before editing. This module
implements the four-layer composite for any ``ChartSpec``:

    1. gray base map (sea / land)               — native resolution
    2. data overlay, alpha-blended onto base    — native resolution
    3. isolines + pill labels on bold lines     — RGBA supersample
       (only when the spec opts in)
    4. coastline                                — native, on top

Replaces the per-variable Python files (msl_chart, t2m_chart,
tp_chart, tprate_chart) that all hand-coded the same composite.
Those files become thin wrappers that call ``render(SPEC, …)``.
"""

from __future__ import annotations

import io
from typing import TYPE_CHECKING

import contourpy
import numpy as np
from PIL import Image, ImageDraw, ImageFont, PngImagePlugin
from scipy.ndimage import gaussian_filter

from aiseed_weather.figures._basemap import base_map_rgb
from aiseed_weather.figures._chart_spec import ChartSpec
from aiseed_weather.figures._coastlines import apply_coastlines
from aiseed_weather.figures._fast import (
    apply_polar_reindex, is_polar, source_grid_for_global,
)
from aiseed_weather.figures._palette import (
    build_continuous_lut, palette_rgb_for,
)

if TYPE_CHECKING:
    import xarray as xr

    from aiseed_weather.figures.regions import Region


# ── Constants shared across all charts ──────────────────────────────
# These are calibration points (see skill) — the values are tied to
# each other and to the way PIL's antialiasing rounds, so think
# carefully before tweaking just one.
_ISOLINE_RGB = (255, 255, 255)            # white, single colour
_ISOLINE_WIDTH_THIN = 1                   # at supersample resolution
_ISOLINE_WIDTH_BOLD = 2                   # at supersample resolution
_ISOLINE_SUPERSAMPLE = 2                  # 2× then LANCZOS = ~0.5 px line
_PILL_TEXT_RGB = (255, 255, 255)
_PILL_PAD_X = 4
_FALLBACK_BASE_RGB = 110                  # used when no precomputed mask


# ── Font loading ────────────────────────────────────────────────────


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


_PILL_FONT_SS = _load_font(11 * _ISOLINE_SUPERSAMPLE)


# ── Grid normalisation helper ───────────────────────────────────────


def _to_pixel_grid(
    field: np.ndarray,
    longitudes: np.ndarray,
    latitudes: np.ndarray,
    region: "Region",
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Crop to region.extent and orient to north-up image space.

    Handles ECMWF Open Data's 90 → -90 / 0 → 359.75 raw orientation,
    rolling longitudes to [-180, 180) so prime-meridian-straddling
    crops work, then flipping vertically so row 0 = northernmost.
    """
    field = np.asarray(field, dtype=np.float32)
    while field.ndim > 2:
        field = field[0]
    longitudes = np.asarray(longitudes, dtype=np.float32)
    latitudes = np.asarray(latitudes, dtype=np.float32)

    if latitudes[0] > latitudes[-1]:
        field = field[::-1]
        latitudes = latitudes[::-1]

    if longitudes.max() > 180.0:
        is_west = longitudes >= 180.0
        new_lon = np.concatenate(
            [longitudes[is_west] - 360.0, longitudes[~is_west]],
        )
        field = np.concatenate(
            [field[:, is_west], field[:, ~is_west]], axis=1,
        )
        longitudes = new_lon

    if region.extent is not None:
        lon_min, lon_max, lat_min, lat_max = region.extent
        lon_mask = (longitudes >= lon_min) & (longitudes <= lon_max)
        lat_mask = (latitudes >= lat_min) & (latitudes <= lat_max)
        field = field[np.ix_(lat_mask, lon_mask)]
        longitudes = longitudes[lon_mask]
        latitudes = latitudes[lat_mask]

    field = field[::-1]
    latitudes = latitudes[::-1]
    return field, longitudes, latitudes


# ── Compositing primitives ──────────────────────────────────────────


def _blend(
    base: np.ndarray, overlay: np.ndarray, transparency: float,
) -> np.ndarray:
    """Per-pixel ``base * t + overlay * (1-t)`` in uint8 land."""
    t = float(transparency)
    out = base.astype(np.float32) * t + overlay.astype(np.float32) * (1.0 - t)
    return np.clip(out, 0, 255).astype(np.uint8)


def _pick_pill_anchor(line: np.ndarray) -> tuple[int, int] | None:
    """Pick a point on a polyline for the pill label.

    Picks the segment with the largest horizontal extent so the pill
    sits along a roughly-horizontal stretch. Returns integer pixel
    coordinates or ``None`` for degenerate lines.

    Calibration point (chart-base-design): the placement quality is
    intentionally minimal — replace when a real placement model that
    avoids stacking pills lands.
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
    bg_rgba: tuple[int, int, int, int],
    font: ImageFont.FreeTypeFont | ImageFont.ImageFont,
) -> None:
    """Rounded-rectangle pill centred at (cx, cy) with white text."""
    bbox = draw.textbbox((0, 0), text, font=font)
    tw = bbox[2] - bbox[0]
    th = bbox[3] - bbox[1]
    pad_y = max(1, th // 6)
    w = tw + 2 * _PILL_PAD_X
    h = th + 2 * pad_y
    x0 = cx - w // 2
    y0 = cy - h // 2
    radius = h // 2
    draw.rounded_rectangle(
        (x0, y0, x0 + w, y0 + h), radius=radius, fill=bg_rgba,
    )
    draw.text(
        (cx - tw // 2 - bbox[0], cy - th // 2 - bbox[1]),
        text, fill=(*_PILL_TEXT_RGB, 255), font=font,
    )


def _format_iso_label(value: float) -> str:
    """Compact pill text. Integer when the value is whole, else 1 dp."""
    if abs(value - round(value)) < 1e-6:
        return str(int(round(value)))
    return f"{value:.1f}"


def _render_isolines_layer(
    field: np.ndarray,
    w: int, h: int,
    spec: ChartSpec,
    lut: np.ndarray,
) -> Image.Image:
    """Render isolines + pills as RGBA at native (w, h) size.

    Internally drawn at supersample, then LANCZOS-downsampled so the
    final lines read as antialiased ~0.5 px without depending on a
    vector toolkit.
    """
    iso = spec.isolines
    assert iso is not None  # caller guarded
    ss = _ISOLINE_SUPERSAMPLE
    W, H = w * ss, h * ss
    overlay = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)

    smoothed = (
        gaussian_filter(field, sigma=iso.smooth_sigma)
        if iso.smooth_sigma > 0
        else field
    )

    x_ss = np.arange(w, dtype=np.float32) * ss
    y_ss = np.arange(h, dtype=np.float32) * ss
    cgen = contourpy.contour_generator(x=x_ss, y=y_ss, z=smoothed)

    # Iterate the thin grid covering [vmin, vmax]; thin lines drawn at
    # every thin_interval, bolder lines at every bold_interval.
    step = iso.thin_interval
    bold_step = iso.bold_interval
    # Round the start to a multiple of step so the integer/half cadence
    # behaves predictably for negative ranges (e.g. -30..30 isotherms).
    start = step * int(np.floor(spec.vmin / step))
    levels = np.arange(start, spec.vmax + 1e-6, step, dtype=np.float64)

    pill_candidates: list[tuple[float, np.ndarray]] = []
    for level in levels:
        is_bold = abs((level / bold_step) - round(level / bold_step)) < 1e-6
        width = _ISOLINE_WIDTH_BOLD if is_bold else _ISOLINE_WIDTH_THIN
        for line in cgen.lines(float(level)):
            if line.shape[0] < iso.min_segment_vertices:
                continue
            draw.line(
                line.astype(np.int32).tolist(),
                fill=(*_ISOLINE_RGB, 255),
                width=width,
            )
            if is_bold and iso.with_pills:
                pill_candidates.append((float(level), line))

    for level, line in pill_candidates:
        anchor = _pick_pill_anchor(line)
        if anchor is None:
            continue
        cx, cy = anchor
        r, g, b = palette_rgb_for(level, lut, spec.vmin, spec.vmax)
        _draw_pill(
            draw, cx, cy,
            _format_iso_label(level),
            bg_rgba=(r, g, b, 255),
            font=_PILL_FONT_SS,
        )

    return overlay.resize((w, h), Image.LANCZOS)


# ── PNG metadata ────────────────────────────────────────────────────


def _png_metadata(layer: str, run_id: str) -> PngImagePlugin.PngInfo:
    info = PngImagePlugin.PngInfo()
    info.add_text("Software", "aiseed-weather")
    info.add_text("Source", "ECMWF Open Data (CC-BY-4.0)")
    info.add_text("Run", run_id)
    info.add_text("Layer", layer)
    return info


# ── Top-level entry point ───────────────────────────────────────────


def render(
    spec: ChartSpec,
    ds: "xr.Dataset",
    *,
    region: "Region",
    run_id: str,
) -> bytes:
    """Render ``spec`` against the dataset and region, return PNG bytes.

    The single point of dispatch for all variables migrated to the
    layered design. Polar regions use a simplified path (no isolines)
    because forward-projecting contour polylines through the polar
    lookup is a separate problem the regional view doesn't need.
    """
    data = spec.extractor(ds)
    longitudes = np.asarray(ds["longitude"].values, dtype=np.float32)
    latitudes = np.asarray(ds["latitude"].values, dtype=np.float32)
    lut = build_continuous_lut(spec.anchors, spec.vmin, spec.vmax)

    if is_polar(region):
        return _render_polar(
            spec, data, longitudes, latitudes, region, run_id, lut,
        )

    data_cropped, _, _ = _to_pixel_grid(data, longitudes, latitudes, region)
    h, w = data_cropped.shape

    # 1. base map
    base = base_map_rgb(region.key)
    if base is None or base.shape != (h, w, 3):
        base = np.full((h, w, 3), _FALLBACK_BASE_RGB, dtype=np.uint8)

    # 2. data overlay (with optional dry-pixel pass-through)
    norm = np.clip(
        (data_cropped - spec.vmin) / (spec.vmax - spec.vmin), 0.0, 1.0,
    )
    data_rgb = lut[(norm * 255.0).astype(np.uint8)]
    blended = _blend(base, data_rgb, spec.transparency)
    if spec.dry_threshold is not None:
        mask = data_cropped >= spec.dry_threshold
        final_arr = base.copy()
        final_arr[mask] = blended[mask]
    else:
        final_arr = blended.copy()

    # 3. isolines layer (RGBA, alpha-composited at native)
    if spec.isolines is not None:
        overlay = _render_isolines_layer(data_cropped, w, h, spec, lut)
        base_img = Image.fromarray(final_arr, mode="RGB").convert("RGBA")
        base_img.alpha_composite(overlay)
        final_arr = np.asarray(base_img.convert("RGB"), dtype=np.uint8).copy()

    # 4. coastline on top of the native composite
    apply_coastlines(final_arr, region.key)

    buf = io.BytesIO()
    Image.fromarray(final_arr, mode="RGB").save(
        buf, format="PNG", compress_level=1,
        pnginfo=_png_metadata(spec.label, run_id),
    )
    return buf.getvalue()


def _render_polar(
    spec: ChartSpec,
    data: np.ndarray,
    longitudes: np.ndarray,
    latitudes: np.ndarray,
    region: "Region",
    run_id: str,
    lut: np.ndarray,
) -> bytes:
    """Polar path — base + data overlay + coastline. No isolines."""
    data_global = source_grid_for_global(data, longitudes, latitudes)
    norm = np.clip(
        (data_global - spec.vmin) / (spec.vmax - spec.vmin), 0.0, 1.0,
    )
    data_rgb = lut[(norm * 255.0).astype(np.uint8)]
    data_polar = apply_polar_reindex(data_rgb, region.key)

    base = base_map_rgb(region.key)
    if base is None or base.shape != data_polar.shape:
        final = data_polar.copy()
    else:
        blended = _blend(base, data_polar, spec.transparency)
        if spec.dry_threshold is not None:
            mask_global = data_global >= spec.dry_threshold
            from aiseed_weather.figures._fast import _polar_lookups
            tbl = _polar_lookups().get(region.key)
            if tbl is not None:
                lat_row, lon_col, valid = tbl
                mask_polar = mask_global[lat_row, lon_col] & valid
                final = base.copy()
                final[mask_polar] = blended[mask_polar]
            else:
                final = blended.copy()
        else:
            final = blended.copy()

    apply_coastlines(final, region.key)

    buf = io.BytesIO()
    Image.fromarray(final, mode="RGB").save(
        buf, format="PNG", compress_level=1,
        pnginfo=_png_metadata(spec.label, run_id),
    )
    return buf.getvalue()
