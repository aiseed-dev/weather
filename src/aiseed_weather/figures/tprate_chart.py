# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Yasuhiro / AIseed

"""Total precipitation rate (降水強度) — instantaneous mm/h.

Same four-layer structure as tp_chart, same Windy-calibrated palette
(1.5 / 2 / 3 / 7 / 10 / 20 / 30 mm/h). Differs from ``tp_chart`` only
in the extractor and the unit label: tp is cumulative amount in mm,
tprate is the precipitation rate at the validity time in mm/h.

**NWP precipitation accuracy caveat.** ECMWF Open Data tprate at the
0.25 ° grid (~28 km) is the output of a parameterised convection
scheme, not a resolved field. Synoptic-scale precipitation patterns
are trustworthy; sub-grid heavy-rain phenomena are NOT:

  * 線状降水帯 (linear rainbands, 20-50 km wide × 50-200 km long) —
    smoothed out across multiple grid cells, peak intensity lost.
  * ゲリラ豪雨 (localised convective cloudbursts, 5-20 km) — entire
    phenomenon is sub-grid; tprate gives 'convection is likely in
    this region' at best.
  * Typhoon eyewall peaks — under-resolved, peaks displaced.

This is **why this renderer must not share a palette with the JMA
radar layer** when that lands. Radar pixels at 1 km × 1 km / 5 min
report observed precipitation with full local intensity; NWP tprate
at 28 km / 3 h reports a regional average. Painting them in the same
colours would make a 20 mm/h NWP pixel look as authoritative as a
20 mm/h radar pixel, and an analyst would be wrong to read it that
way.

See chart-base-design skill for the differentiation principle.
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
# Reuse the tp Windy palette directly — same numerical bin values
# (1.5 / 2 / 3 / 7 / 10 / 20 / 30) apply both to 'mm in 3 hours'
# (tp accumulation) and to 'mm/h' (tprate). The colour at a given
# numerical level reads as the same precipitation intensity in both
# contexts; only the unit label on the legend differs.
from aiseed_weather.figures.tp_chart import (
    _LUT, _VMIN_MM, _VMAX_MM, _LEGEND_TICKS_MM, _DRY_THRESHOLD_MM,
    _DATA_TRANSPARENCY,
)
from aiseed_weather.figures.regions import GLOBAL

if TYPE_CHECKING:
    import xarray as xr

    from aiseed_weather.figures.regions import Region


# Reuse same scale as tp but document the unit change.
_VMIN_MMH = _VMIN_MM        # mm/h
_VMAX_MMH = _VMAX_MM
_LEGEND_TICKS_MMH = _LEGEND_TICKS_MM
_DRY_THRESHOLD_MMH = _DRY_THRESHOLD_MM


def _extract_tprate_mmh(ds: "xr.Dataset") -> np.ndarray:
    """ECMWF ``tprate`` is in kg m⁻² s⁻¹ ≡ mm/s. Convert to mm/h.

    Note the 3600 factor is the ONLY unit conversion; the field is
    already at the validity time of the GRIB message, so no
    time-integration trick is needed.
    """
    if "tprate" in ds.data_vars:
        return np.asarray(ds["tprate"].values, dtype=np.float32) * 3600.0
    raise ValueError(
        f"No 'tprate' (total precipitation rate) in dataset; "
        f"vars={list(ds.data_vars)}",
    )


def render_tprate(
    ds: "xr.Dataset",
    *,
    region: "Region" = GLOBAL,
    run_id: str,
) -> bytes:
    tprate_mmh = _extract_tprate_mmh(ds)
    longitudes = np.asarray(ds["longitude"].values, dtype=np.float32)
    latitudes = np.asarray(ds["latitude"].values, dtype=np.float32)

    if is_polar(region):
        v_global = source_grid_for_global(tprate_mmh, longitudes, latitudes)
        norm = np.clip((v_global - _VMIN_MMH) / (_VMAX_MMH - _VMIN_MMH), 0.0, 1.0)
        data_rgb = _LUT[(norm * 255.0).astype(np.uint8)]
        data_polar = apply_polar_reindex(data_rgb, region.key)
        base = base_map_rgb(region.key)
        if base is not None and base.shape == data_polar.shape:
            blended = _blend_with_transparency(base, data_polar, _DATA_TRANSPARENCY)
            mask = v_global >= _DRY_THRESHOLD_MMH
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

    tprate_mmh, longitudes, latitudes = _to_pixel_grid(
        tprate_mmh, longitudes, latitudes, region,
    )
    h, w = tprate_mmh.shape

    base = base_map_rgb(region.key)
    if base is None or base.shape != (h, w, 3):
        base = np.full((h, w, 3), 110, dtype=np.uint8)

    norm = np.clip((tprate_mmh - _VMIN_MMH) / (_VMAX_MMH - _VMIN_MMH), 0.0, 1.0)
    data_rgb = _LUT[(norm * 255.0).astype(np.uint8)]
    blended = _blend_with_transparency(base, data_rgb, _DATA_TRANSPARENCY)
    mask = tprate_mmh >= _DRY_THRESHOLD_MMH
    final_arr = base.copy()
    final_arr[mask] = blended[mask]

    apply_coastlines(final_arr, region.key)

    buf = io.BytesIO()
    Image.fromarray(final_arr, mode="RGB").save(
        buf, format="PNG", compress_level=1,
        pnginfo=_png_metadata(run_id),
    )
    return buf.getvalue()


def _png_metadata(run_id: str):
    info = _msl_png_metadata(run_id)
    info.add_text("Layer", "tprate")
    return info
