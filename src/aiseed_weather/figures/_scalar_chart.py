# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Yasuhiro / AIseed

"""Generic scalar-field renderer + per-layer configuration.

The msl / t2m / tp / wind10m charts each have unique features
(isobar fan, vector quiver, etc.) that justify dedicated modules.
Most other surface layers are just "binned LUT + optional single
isoline + coastlines + PNG metadata" — a fixed pipeline parameterised
by a few values. Rather than copy-paste 50 lines per layer, this
module exposes:

* ``ScalarLayerConfig`` — palette + bounds + extractor + isoline.
* ``render_scalar(ds, region, run_id, config)`` — runs the pipeline.
* ``CONFIGS`` — registered configs by ``layer_key``.

Adding a new layer = one ``ScalarLayerConfig`` entry plus a row in
catalog.FIELDS plus a key in map_view._LAYER_GRADIENT_STOPS /
_LAYER_LEGEND_TICKS for the chip + legend.
"""

from __future__ import annotations

import io
from dataclasses import dataclass, field
from typing import Callable, TYPE_CHECKING

import contourpy
import numpy as np
from PIL import Image, ImageDraw

from aiseed_weather.figures._coastlines import apply_coastlines
from aiseed_weather.figures._fast import (
    apply_binned_lut, crop_grid, is_polar, palette_to_lut, shade_for_region,
)

if TYPE_CHECKING:
    import xarray as xr

    from aiseed_weather.figures.regions import Region


@dataclass(frozen=True)
class ScalarLayerConfig:
    """Renderable description of a one-variable surface layer."""

    layer_key: str
    bounds: np.ndarray  # bin edges; len(bounds) → len(palette) - 1
    palette: tuple[str, ...]  # hex stops; under, then per-bin, then over
    # Pulls the value array (in display units) out of an xarray Dataset.
    extractor: Callable[["xr.Dataset"], np.ndarray]
    # Optional emphasised isoline (e.g. 0 °C). Drawn on PlateCarree
    # regions; polar regions skip it (forward projection of the
    # polyline isn't free and the colour alone usually suffices).
    isoline_value: float | None = None
    isoline_color: tuple[int, int, int] = (245, 245, 240)


def _lut_for(config: ScalarLayerConfig) -> np.ndarray:
    return palette_to_lut(list(config.palette))


def render_scalar(
    ds: "xr.Dataset",
    *,
    region: "Region",
    run_id: str,
    config: ScalarLayerConfig,
) -> bytes:
    """Run the binned-LUT pipeline for one scalar layer."""
    data = config.extractor(ds)
    longitudes = ds["longitude"].values
    latitudes = ds["latitude"].values
    lut = _lut_for(config)

    rgb = shade_for_region(
        lambda arr: apply_binned_lut(arr, config.bounds, lut),
        data, longitudes, latitudes, region,
    )
    apply_coastlines(rgb, region.key)
    img = Image.fromarray(rgb, mode="RGB")

    if config.isoline_value is not None and not is_polar(region):
        data_crop, _, _ = crop_grid(data, longitudes, latitudes, region)
        h, w = rgb.shape[:2]
        x_pix = np.arange(w, dtype=np.float32)
        y_pix = np.arange(h, dtype=np.float32)
        cgen = contourpy.contour_generator(x=x_pix, y=y_pix, z=data_crop)
        draw = ImageDraw.Draw(img)
        for line in cgen.lines(float(config.isoline_value)):
            if len(line) >= 2:
                draw.line(
                    [(float(p[0]), float(p[1])) for p in line],
                    fill=config.isoline_color, width=2,
                )

    buf = io.BytesIO()
    from PIL import PngImagePlugin

    info = PngImagePlugin.PngInfo()
    info.add_text("Software", "aiseed-weather")
    info.add_text("Source", "ECMWF Open Data (CC-BY-4.0)")
    info.add_text("Run", run_id)
    info.add_text("Layer", config.layer_key)
    img.save(buf, format="PNG", pnginfo=info)
    return buf.getvalue()


# ── Per-layer configs ───────────────────────────────────────────────


def _extract_kelvin_as_celsius(*names: str):
    """Build an extractor that pulls the first matching variable from
    the dataset and converts K → °C. Used for dewpoint / skin temp /
    soil temp, which all arrive in Kelvin but display in Celsius."""
    def _fn(ds):
        for n in names:
            if n in ds.data_vars:
                return np.asarray(ds[n].values, dtype=np.float32) - 273.15
        raise ValueError(
            f"None of {names!r} found in dataset; "
            f"vars={list(ds.data_vars)}",
        )
    return _fn


# Shared temperature bin grid for d2m / skt. 4 °C resolution gives
# 22 bins (under + 20 internal + over), and the palette below has
# exactly that many entries so each bin gets a distinct colour. An
# earlier 2 °C grid was paired with only 25 palette colours, so
# digitize indices past ~bin 25 all aliased to the same dark-red
# "over" colour — every value above ~8 °C rendered identical.
_TEMP_BOUNDS = np.arange(-40, 44, 4, dtype=np.float32)
_TEMP_PALETTE = (
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
    "#e8e8b8",  #   0.. 4
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
)
assert len(_TEMP_PALETTE) == len(_TEMP_BOUNDS) + 1, (
    f"palette={len(_TEMP_PALETTE)} vs bins={len(_TEMP_BOUNDS) + 1}"
)


D2M_CONFIG = ScalarLayerConfig(
    layer_key="d2m",
    bounds=_TEMP_BOUNDS,
    palette=_TEMP_PALETTE,
    extractor=_extract_kelvin_as_celsius("d2m", "2d"),
    isoline_value=0.0,
)

SKT_CONFIG = ScalarLayerConfig(
    layer_key="skt",
    bounds=_TEMP_BOUNDS,
    palette=_TEMP_PALETTE,
    extractor=_extract_kelvin_as_celsius("skt"),
    isoline_value=0.0,
)


# Snow depth in metres water equivalent; bin edges chosen to match
# the JMA snow-cover product (a few cm visible from the lightest
# bin, escalating to "100+ cm" deep snow at the top).
SD_CONFIG = ScalarLayerConfig(
    layer_key="sd",
    bounds=np.array(
        [0.01, 0.02, 0.05, 0.1, 0.2, 0.5, 1.0, 2.0],
        dtype=np.float32,
    ),
    palette=(
        "#f4f4f4",  # under: no snow
        "#dfeaf3", "#bcd5ea", "#88b9dd", "#5c9bd0",
        "#3b7fc3", "#2860a8", "#1a4486", "#0f2a5e",
    ),
    extractor=lambda ds: np.asarray(
        ds["sd"].values if "sd" in ds.data_vars else ds["sde"].values,
        dtype=np.float32,
    ),
)


# Total cloud cover, fraction 0..1. Light greys so the chart still
# reads as a chart and not a wash. Under (clear sky) is intentionally
# matched to the panel background so cloudless regions disappear.
TCC_CONFIG = ScalarLayerConfig(
    layer_key="tcc",
    bounds=np.array(
        [0.1, 0.25, 0.4, 0.55, 0.7, 0.85, 0.95], dtype=np.float32,
    ),
    palette=(
        "#1e2230",  # under: <10% (clear) — matches dark panel background
        "#2c3142", "#3e4458", "#54596d", "#737888",
        "#9aa0ad", "#c7cad3", "#eef0f4",
    ),
    extractor=lambda ds: np.asarray(
        ds["tcc"].values, dtype=np.float32,
    ),
)


CONFIGS: dict[str, ScalarLayerConfig] = {
    cfg.layer_key: cfg
    for cfg in (D2M_CONFIG, SKT_CONFIG, SD_CONFIG, TCC_CONFIG)
}
