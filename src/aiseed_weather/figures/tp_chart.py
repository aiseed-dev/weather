# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Yasuhiro / AIseed

"""Total precipitation — colour-only layered chart.

Read .agents/skills/chart-base-design before editing. Precipitation is
in the colour-only family: the surface field is too noisy /
spatially-discontinuous for clean iso-lines, so the chart relies on
the data palette alone.

Two precipitation-specific traits the renderer handles:

  * **Below-threshold transparency.** Values below 0.1 mm read as
    'no precipitation' by JMA convention — the base map should show
    through unchanged instead of getting a stale-faint-colour wash
    over every dry patch.
  * **Non-uniform palette anchors.** Precipitation is heavily
    skewed: most pixels are 0–2 mm, a handful are 50+ mm. Anchors
    spaced at ~log-2 cadence (0.5 / 1 / 5 / 10 / 20 / 50 / 100 / 200
    mm) keep visible discrimination across that range without
    needing a true log-norm transform on the continuous LUT.
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
_VMAX_MM = 200.0
# Anchors at JMA / WMO bands: 0.5, 1, 5, 10, 20, 50, 100, 200 mm.
# Non-uniform spacing (~log-2) keeps fine resolution at low values
# (where most observations sit) and still saturates predictably at
# extreme totals.
_LEGEND_TICKS_MM = (0.5, 1.0, 5.0, 10.0, 20.0, 50.0, 100.0, 200.0)
# Pixels below this read as "no precipitation" — fully transparent,
# base map shown unchanged.
_DRY_THRESHOLD_MM = 0.1


def _build_sequential_lut() -> np.ndarray:
    """Sequential pale-cyan → blue → green → yellow → red → purple.

    Anchors are at the JMA precipitation tick values. Linear
    interpolation between them in the [0, vmax] range, then sampled
    256 times for the uint8 LUT. Because the anchor xs are spaced
    non-uniformly, np.interp does the right thing without us having
    to switch to a log-norm transform on the data side.
    """
    anchors: list[tuple[float, tuple[int, int, int]]] = [
        (0.5,   (200, 230, 250)),   # very pale cyan
        (1.0,   (165, 215, 240)),   # pale cyan
        (5.0,   (100, 175, 215)),   # mid blue
        (10.0,  (50, 125, 200)),    # deeper blue
        (20.0,  (50, 165, 90)),     # green
        (50.0,  (200, 200, 60)),    # yellow-green / yellow
        (100.0, (235, 130, 50)),    # orange-red
        (200.0, (135, 30, 95)),     # crimson-purple
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
