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


# Dewpoint: same -40..+40 bins as t2m so the eye doesn't have to
# re-calibrate when toggling between the two.
_TEMP_BOUNDS = np.arange(-40, 42, 2, dtype=np.float32)
_TEMP_PALETTE = (
    "#1a0030",
    "#2c0a4d", "#3c1d7a", "#43378e", "#3a4a9d", "#2965b2",
    "#1b81c4", "#3296c8", "#5cabc8", "#84c0c8", "#a8d3c4",
    "#c4dfba", "#e0eab2", "#f5f0a8", "#f9e088", "#facb68",
    "#f9b04e", "#f5933a", "#ed7530", "#de5526", "#c93920",
    "#a82418", "#82130f", "#580808", "#3c0404",
)
# Pad to len(bounds)+1 = 42 entries
_TEMP_PALETTE = _TEMP_PALETTE + ("#3c0404",) * (
    len(_TEMP_BOUNDS) + 1 - len(_TEMP_PALETTE) - 1
) + ("#1f0000",)


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
