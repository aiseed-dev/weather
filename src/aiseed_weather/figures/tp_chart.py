# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Yasuhiro / AIseed

"""Total precipitation chart — numpy + PIL fast pipeline.

Same C-backed pipeline as msl_chart / t2m_chart. Precipitation uses
a non-linear binned palette so trace amounts and extreme events are
both readable: bin edges are (0.1, 0.5, 1, 2, 5, 10, 20, 30, 50, 75,
100, 150, 200) mm; under-range is rendered transparent so dry land
shows the underlying base.

Skipped vs. the matplotlib version (Stage 2 work):
* MSL overlay (msl_overlay_ds) — argument kept for API compat,
  ignored on this path.
* Coastlines / gridlines / colorbar / title / footer — captured in
  PNG metadata instead of rasterised onto the image.
"""

from __future__ import annotations

import io
from typing import TYPE_CHECKING

import numpy as np
from PIL import Image

from aiseed_weather.figures._fast import (
    apply_binned_lut, palette_to_lut, shade_for_region,
)
from aiseed_weather.figures.regions import GLOBAL

if TYPE_CHECKING:
    import xarray as xr

    from aiseed_weather.figures.regions import Region


# Non-linear bins in mm — JMA / Windy / ECMWF Charts convention.
TP_BOUNDS_MM: np.ndarray = np.array(
    [0.1, 0.5, 1, 2, 5, 10, 20, 30, 50, 75, 100, 150, 200], dtype=np.float32,
)

# 14 entries: under (transparent → background grey here) + 13 bins.
_TP_PALETTE: list[str] = [
    "#f4f4f4",  # under: < 0.1 mm (dry; near-white so colored bins pop)
    "#c8e6f5",  # 0.1-0.5  very pale blue
    "#9dd1ee",  # 0.5-1
    "#6cb6e0",  # 1-2
    "#3a92c8",  # 2-5
    "#1a73b3",  # 5-10
    "#2e8b3d",  # 10-20    green
    "#62b04f",  # 20-30
    "#a4cd47",  # 30-50    yellow-green
    "#f0d643",  # 50-75    yellow
    "#f59f1b",  # 75-100   orange
    "#e54d24",  # 100-150  red
    "#a31a3a",  # 150-200  crimson
    "#5e1660",  # >200     purple
]
_TP_LUT: np.ndarray = palette_to_lut(_TP_PALETTE)


def _extract_tp_mm(ds: "xr.Dataset") -> np.ndarray:
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
    msl_overlay_ds: "xr.Dataset | None" = None,
) -> bytes:
    tp_mm = _extract_tp_mm(ds)
    longitudes = ds["longitude"].values
    latitudes = ds["latitude"].values

    rgb = shade_for_region(
        lambda arr: apply_binned_lut(arr, TP_BOUNDS_MM, _TP_LUT),
        tp_mm, longitudes, latitudes, region,
    )
    from aiseed_weather.figures._coastlines import apply_coastlines
    apply_coastlines(rgb, region.key)

    img = Image.fromarray(rgb, mode="RGB")

    buf = io.BytesIO()
    from PIL import PngImagePlugin

    info = PngImagePlugin.PngInfo()
    info.add_text("Software", "aiseed-weather")
    info.add_text("Source", "ECMWF Open Data (CC-BY-4.0)")
    info.add_text("Run", run_id)
    info.add_text("Layer", "tp")
    img.save(buf, format="PNG", compress_level=1, pnginfo=info)
    return buf.getvalue()
