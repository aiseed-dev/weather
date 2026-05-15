# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Yasuhiro / AIseed

"""2-metre temperature — same four-layer structure as msl_chart.

Read .agents/skills/chart-base-design before editing. Palette anchors
match Windy's t2m legend (every 10 °C from -20 to +40), so a tick on
the legend lines up exactly with the colour at that isotherm.
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
# Anchors aligned with the Windy legend ticks every 10 °C from -20 to
# +40. Diverging around 0 °C (pale neutral), violet/blue on the cold
# side, yellow/orange/red on the warm side. Values are first-cut
# approximations of the Windy palette — adjust if needed.
_VMIN_C = -20.0
_VMAX_C = 40.0
_LEGEND_TICKS_C = (-20.0, -10.0, 0.0, 10.0, 20.0, 30.0, 40.0)


def _build_diverging_lut() -> np.ndarray:
    anchors: list[tuple[float, tuple[int, int, int]]] = [
        (-20.0, (50, 30, 165)),     # saturated indigo (cold extreme)
        (-10.0, (40, 115, 225)),    # vivid blue
        (0.0,   (230, 225, 215)),   # warm pale (freezing pivot)
        (10.0,  (190, 220, 100)),   # light green-yellow
        (20.0,  (245, 200, 80)),    # yellow
        (30.0,  (220, 110, 60)),    # orange-red
        (40.0,  (135, 30, 30)),     # dark red (warm extreme)
    ]
    xs = np.array(
        [(v - _VMIN_C) / (_VMAX_C - _VMIN_C) for v, _ in anchors],
        dtype=np.float32,
    )
    rgb = np.array([c for _, c in anchors], dtype=np.float32)
    t = np.linspace(0.0, 1.0, 256, dtype=np.float32)
    out = np.empty((256, 3), dtype=np.float32)
    for ch in range(3):
        out[:, ch] = np.interp(t, xs, rgb[:, ch])
    return np.clip(out, 0, 255).astype(np.uint8)


_LUT: np.ndarray = _build_diverging_lut()


def _palette_rgb_for(value_c: float) -> tuple[int, int, int]:
    norm = max(0.0, min(1.0, (value_c - _VMIN_C) / (_VMAX_C - _VMIN_C)))
    idx = int(round(norm * 255.0))
    r, g, b = _LUT[idx]
    return int(r), int(g), int(b)


_DATA_TRANSPARENCY = 0.30


# ── Entry point ─────────────────────────────────────────────────────


def _extract_t2m_c(ds: "xr.Dataset") -> np.ndarray:
    """Return 2 m temperature in °C, K → °C. Variable name varies
    between decoders so try the common ones in order."""
    for v in ("t2m", "2t"):
        if v in ds.data_vars:
            return np.asarray(ds[v].values, dtype=np.float32) - 273.15
    if "t" in ds.data_vars:
        return np.asarray(ds["t"].values, dtype=np.float32) - 273.15
    raise ValueError(
        f"No 2m temperature variable in dataset; "
        f"vars={list(ds.data_vars)}",
    )


def render_t2m(
    ds: "xr.Dataset",
    *,
    region: "Region" = GLOBAL,
    run_id: str,
    msl_overlay_ds: "xr.Dataset | None" = None,  # kept for API compat; unused
) -> bytes:
    t_c = _extract_t2m_c(ds)
    longitudes = np.asarray(ds["longitude"].values, dtype=np.float32)
    latitudes = np.asarray(ds["latitude"].values, dtype=np.float32)

    if is_polar(region):
        t_global = source_grid_for_global(t_c, longitudes, latitudes)
        norm = np.clip((t_global - _VMIN_C) / (_VMAX_C - _VMIN_C), 0.0, 1.0)
        data_rgb = _LUT[(norm * 255.0).astype(np.uint8)]
        data_polar = apply_polar_reindex(data_rgb, region.key)
        base = base_map_rgb(region.key)
        if base is not None and base.shape == data_polar.shape:
            final = _blend_with_transparency(base, data_polar, _DATA_TRANSPARENCY)
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

    t_c, longitudes, latitudes = _to_pixel_grid(
        t_c, longitudes, latitudes, region,
    )
    h, w = t_c.shape

    # 1. base map at native
    base = base_map_rgb(region.key)
    if base is None or base.shape != (h, w, 3):
        base = np.full((h, w, 3), 110, dtype=np.uint8)

    # 2. data overlay blended at native. No isotherm / pill layer:
    # temperature is well carried by the continuous palette alone,
    # so adding 2 °C / 10 °C isotherm fans only clutters the chart
    # for an analyst who reads the colour directly. MSL needs lines
    # because pressure-gradient shape is the synoptic information;
    # temperature-gradient shape is already encoded in the colour.
    norm = np.clip((t_c - _VMIN_C) / (_VMAX_C - _VMIN_C), 0.0, 1.0)
    data_rgb = _LUT[(norm * 255.0).astype(np.uint8)]
    final_arr = _blend_with_transparency(base, data_rgb, _DATA_TRANSPARENCY).copy()

    # 3. coastline on top of the data-blended base
    apply_coastlines(final_arr, region.key)

    buf = io.BytesIO()
    Image.fromarray(final_arr, mode="RGB").save(
        buf, format="PNG", compress_level=1,
        pnginfo=_png_metadata(run_id),
    )
    return buf.getvalue()


def _png_metadata(run_id: str):
    info = _msl_png_metadata(run_id)
    # _msl_png_metadata stamps "Layer = msl"; override.
    info.add_text("Layer", "t2m")
    return info
