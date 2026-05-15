# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Yasuhiro / AIseed

"""2-metre temperature chart — numpy + PIL fast pipeline.

Replaces the previous cartopy + matplotlib version (5-15 s/frame)
with the same C-backed pipeline as msl_chart: numpy LUT for the
discrete diverging palette, contourpy for the 0 °C isotherm, PIL
for line rasterisation and PNG encoding.

Skipped vs. the matplotlib version (Stage 2 work):
* MSL overlay (msl_overlay_ds) — argument kept for API compat but
  ignored. Numpy overlay contour fill will be wired through the
  same path as the base 0 °C contour.
* Coastlines and gridlines.
* Colorbar / title / footer — kept as PNG metadata only; UI renders
  textual context next to the image.
"""

from __future__ import annotations

import io
from typing import TYPE_CHECKING

import contourpy
import numpy as np
from PIL import Image, ImageDraw

from aiseed_weather.figures._fast import (
    apply_binned_lut, palette_to_lut, shade_for_region,
)
from aiseed_weather.figures.regions import GLOBAL

if TYPE_CHECKING:
    import xarray as xr

    from aiseed_weather.figures.regions import Region


# Bin edges in °C at 4 °C resolution. arange(-40, 44, 4) gives 21
# edges → 22 bins counting under (<-40) and over (>40). Earlier
# versions used a 2 °C grid but the palette only had 24 colour stops,
# so digitize indices past ~bin 25 all aliased to the same dark-red
# "over" colour — every value above ~8 °C rendered identical. The
# 4 °C grid lets a hand-tuned palette of exactly 22 entries align
# one-to-one with the bins.
T2M_BOUNDS_C: np.ndarray = np.arange(-40, 44, 4, dtype=np.float32)

# 22 entries = under + 20 internal bins + over. Stops chosen so the
# freezing line falls on a near-white/cream cell and the warm bins
# climb through yellow → orange → red → dark red, à la Windy.
_T2M_PALETTE: list[str] = [
    "#1a0030",  # under:  <-40
    "#27093f",  # -40..-36
    "#341a5f",  # -36..-32
    "#412b7f",  # -32..-28
    "#3c428f",  # -28..-24
    "#2f5aa3",  # -24..-20
    "#2b7ab3",  # -20..-16
    "#2f93c3",  # -16..-12
    "#52a8d0",  # -12..-8
    "#80c0d8",  #  -8..-4
    "#b8d8d0",  #  -4.. 0
    "#e8e8b8",  #   0.. 4   (freezing line on the warm side)
    "#d8e88c",  #   4.. 8
    "#c4e060",  #   8..12
    "#f5db58",  #  12..16
    "#f9b840",  #  16..20
    "#f59030",  #  20..24
    "#ec6c28",  #  24..28
    "#d84820",  #  28..32
    "#bb2418",  #  32..36
    "#8a1414",  #  36..40
    "#1f0000",  # over:   >40
]
assert len(_T2M_PALETTE) == len(T2M_BOUNDS_C) + 1, (
    f"palette={len(_T2M_PALETTE)} vs bins={len(T2M_BOUNDS_C) + 1}"
)
_T2M_LUT: np.ndarray = palette_to_lut(_T2M_PALETTE)


def _extract_t2m_c(ds: "xr.Dataset") -> np.ndarray:
    """Return 2 m temperature in °C. Variable name varies between
    decoders; try the common ones in order."""
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
    msl_overlay_ds: "xr.Dataset | None" = None,
) -> bytes:
    """Render a T2m chart to PNG bytes."""
    t_c = _extract_t2m_c(ds)
    longitudes = ds["longitude"].values
    latitudes = ds["latitude"].values

    rgb = shade_for_region(
        lambda arr: apply_binned_lut(arr, T2M_BOUNDS_C, _T2M_LUT),
        t_c, longitudes, latitudes, region,
    )

    from aiseed_weather.figures._coastlines import apply_coastlines
    from aiseed_weather.figures._fast import is_polar
    apply_coastlines(rgb, region.key)

    img = Image.fromarray(rgb, mode="RGB")

    # 0 °C isotherm on PlateCarree only — polar would need a forward
    # projection of the polyline vertices and isn't worth the wire
    # for the freezing line specifically.
    if not is_polar(region):
        h, w = rgb.shape[:2]
        # Recompute the cropped value field for contourpy. Cheap;
        # already in the pipeline.
        from aiseed_weather.figures._fast import crop_grid
        t_crop, _, _ = crop_grid(t_c, longitudes, latitudes, region)
        x_pix = np.arange(w, dtype=np.float32)
        y_pix = np.arange(h, dtype=np.float32)
        draw = ImageDraw.Draw(img)
        cgen = contourpy.contour_generator(x=x_pix, y=y_pix, z=t_crop)
        for line in cgen.lines(0.0):
            if len(line) >= 2:
                draw.line(
                    [(float(p[0]), float(p[1])) for p in line],
                    # Off-white 0 °C line — Windy-style. Black would
                    # disappear into the deep-purple cold end of the
                    # diverging palette.
                    fill=(245, 245, 240), width=2,
                )

    buf = io.BytesIO()
    from PIL import PngImagePlugin

    info = PngImagePlugin.PngInfo()
    info.add_text("Software", "aiseed-weather")
    info.add_text("Source", "ECMWF Open Data (CC-BY-4.0)")
    info.add_text("Run", run_id)
    info.add_text("Layer", "t2m")
    img.save(buf, format="PNG", compress_level=1, pnginfo=info)
    return buf.getvalue()
