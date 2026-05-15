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


# ── Pressure-level fields ────────────────────────────────────────────
# Generated config-per-(variable, level) — render_pool dispatches all
# of them through this generic pipeline. The single-band GRIB at the
# requested levelist means the extractor just reads the variable;
# any residual level axis is squeezed.


def _squeeze_2d(arr: np.ndarray) -> np.ndarray:
    """Drop leading singleton dims (e.g. a single-level axis) so the
    result is (lat, lon). The fast pipeline assumes a 2D field."""
    while arr.ndim > 2:
        arr = arr[0]
    return arr


def _select_level(da, level: int):
    """Slice a pressure-level DataArray down to one hPa level.

    cfgrib exposes the level axis as ``isobaricInhPa`` for upper-air
    fields; multi-band GRIBs from the new fetch pipeline carry many
    levels in one variable. Falls back gracefully if the array is
    already 2D (single-level GRIB)."""
    if "isobaricInhPa" in da.dims:
        return da.sel(isobaricInhPa=level)
    return da


def _extract_kelvin_at_level(var_names: tuple[str, ...], level: int | None = None):
    def _fn(ds):
        for n in var_names:
            if n in ds.data_vars:
                da = ds[n] if level is None else _select_level(ds[n], level)
                return _squeeze_2d(np.asarray(da.values, dtype=np.float32)) - 273.15
        raise ValueError(
            f"None of {var_names!r} found in dataset; "
            f"vars={list(ds.data_vars)}",
        )
    return _fn


def _extract_wind_speed_at_level(level: int | None = None):
    def _fn(ds):
        u = v = None
        for n in ("u", "U"):
            if n in ds.data_vars:
                da = ds[n] if level is None else _select_level(ds[n], level)
                u = _squeeze_2d(np.asarray(da.values, dtype=np.float32))
                break
        for n in ("v", "V"):
            if n in ds.data_vars:
                da = ds[n] if level is None else _select_level(ds[n], level)
                v = _squeeze_2d(np.asarray(da.values, dtype=np.float32))
                break
        if u is None or v is None:
            raise ValueError(
                f"No u/v in dataset; vars={list(ds.data_vars)}",
            )
        return np.hypot(u, v)
    return _fn


def _extract_value_at_level(var_names: tuple[str, ...], level: int | None = None):
    def _fn(ds):
        for n in var_names:
            if n in ds.data_vars:
                da = ds[n] if level is None else _select_level(ds[n], level)
                return _squeeze_2d(np.asarray(da.values, dtype=np.float32))
        raise ValueError(
            f"None of {var_names!r} found in dataset; "
            f"vars={list(ds.data_vars)}",
        )
    return _fn


# Geopotential height bin layouts per level. Climatological centre
# values from ECMWF reanalysis (m); each level spans roughly ±300 m
# at low altitudes, growing to ±800 m at 50 hPa so the seasonal /
# synoptic swing covers most of the palette.
_GH_CENTRES = {
    1000: 100, 925: 760, 850: 1460, 700: 3010,
    600: 4360, 500: 5570, 400: 7180, 300: 9150,
    250: 10350, 200: 11750, 150: 13620, 100: 16180, 50: 20580,
}
_GH_HALFRANGE = {
    1000: 200, 925: 220, 850: 240, 700: 320,
    600: 380, 500: 440, 400: 520, 300: 620,
    250: 700, 200: 770, 150: 900, 100: 1050, 50: 1400,
}


def _gh_bounds_for(level: int) -> np.ndarray:
    centre = _GH_CENTRES[level]
    half = _GH_HALFRANGE[level]
    # 21 edges → 22 bins, same shape as the temperature palette.
    return np.linspace(
        centre - half, centre + half, 21, dtype=np.float32,
    )


# Generic 22-stop palette reused for every gh level (cool blue at
# the lower end → warm red at the upper end of the climatological
# range for that level). Same 22 colours as the temperature palette
# so the chart language stays consistent across layers.
_GH_PALETTE = _TEMP_PALETTE


# Vertical velocity ω (Pa/s) bin layout. Negative = upward motion
# (the synoptically interesting half); the palette emphasises that.
_W_BOUNDS = np.array(
    [-3.0, -2.0, -1.0, -0.5, -0.2, -0.05,
     0.05, 0.2, 0.5, 1.0, 2.0],
    dtype=np.float32,
)
_W_PALETTE = (
    "#1a4486",  # under: < -3 (strong upward)
    "#2860a8", "#3b7fc3", "#5c9bd0", "#88b9dd", "#bcd5ea",
    "#f4f4f4",  # near zero
    "#f4e4c8", "#f0b894", "#e98565", "#c4502b", "#7a1f15",  # downward
)
# under + len(bounds) bins; total = len(bounds) + 1 = 12. ✓


# Wind component (u, v) bins — diverging around zero. Same scale for
# every level so the eye can compare u@250 (jet) vs u@1000 (surface).
_UV_BOUNDS = np.array(
    [-60, -40, -25, -15, -10, -5, -2,
     2, 5, 10, 15, 25, 40, 60],
    dtype=np.float32,
)
_UV_PALETTE = (
    "#1a0030",  # under: < -60
    "#2c0a4d", "#3a4a9d", "#1b81c4", "#5cabc8", "#a8d3c4",
    "#e0eab2", "#f5f0a8",  # near zero (cream)
    "#facb68", "#f9b04e", "#ed7530", "#c93920", "#82130f",
    "#3c0404", "#1f0000",  # over: > 60
)
# bounds 14 entries → 15 bins. palette 15 entries. ✓


# Divergence (d) and vorticity (vo) are tiny numbers (∼10⁻⁵..10⁻⁴
# 1/s). One symmetric layout reused for both, scaled by 10⁻⁴ so the
# bin edges look like "-2..+2" mentally.
_DV_BOUNDS = np.array(
    [-2e-4, -1e-4, -5e-5, -2e-5, -1e-5,
     1e-5, 2e-5, 5e-5, 1e-4, 2e-4],
    dtype=np.float32,
)
_DV_PALETTE = (
    "#1a0030", "#2c0a4d", "#3a4a9d", "#1b81c4", "#84c0c8",
    "#f4f4f4",  # near zero
    "#f9e088", "#f9b04e", "#c93920", "#82130f", "#3c0404",
)
# bounds 10 → 11 bins. palette 11. ✓


# Specific humidity (q, kg/kg) — per-level bin layouts. Lower
# atmosphere holds 5–25 g/kg; the stratosphere is near-zero.
def _q_bounds_for(level: int) -> np.ndarray:
    # Top of the bin grid scales with level, dropping by ~half per
    # 100 hPa above 700 hPa. Below: bigger swings near surface.
    top = {
        1000: 0.025, 925: 0.022, 850: 0.020, 700: 0.014,
        600: 0.010, 500: 0.006, 400: 0.003, 300: 0.001,
        250: 0.0006, 200: 0.0003, 150: 0.0001, 100: 5e-5, 50: 2e-5,
    }.get(level, 0.001)
    # 11 edges → 12 bins.
    return np.linspace(0, top, 11, dtype=np.float32)


_Q_PALETTE = (
    "#f4f4f4",  # under: ≤ 0
    "#e8efe2", "#cfe2c8", "#a8d3b1", "#80c1a6",
    "#5cae9c", "#3a8fa0", "#216e9c", "#16548a", "#0e3d6e",
    "#0a3052", "#06223a",
)
# bounds 11 → 12 bins. palette 12. ✓


# Relative humidity (%) bin layout. 70%+ is the "moist plume" range.
_RH_BOUNDS = np.array(
    [10, 20, 30, 40, 50, 60, 70, 80, 90, 95], dtype=np.float32,
)
_RH_PALETTE = (
    "#704020",  # under (<10%, very dry)
    "#8a5532", "#a06848", "#bd8458", "#d8a268", "#e8c08c",
    "#cfd8c4", "#9fcfd0", "#5da3d0", "#1a6cbf", "#0a3a82",
)
# under + 10 bins = 11 entries. ✓


def _pl_config_for(var: str, level: int) -> ScalarLayerConfig:
    """Build a :class:`ScalarLayerConfig` for one (variable, level)
    pair from ECMWF Open Data's pressure-level catalogue.

    Every variable in PRESSURE_VARIABLES (catalog.py) maps here. The
    bounds + palette are picked per variable family; gh and q also
    vary their bounds by level."""
    if var == "gh":
        return ScalarLayerConfig(
            layer_key=f"gh{level}",
            bounds=_gh_bounds_for(level),
            palette=_GH_PALETTE,
            extractor=_extract_value_at_level(("gh", "z"), level=level),
        )
    if var == "t":
        return ScalarLayerConfig(
            layer_key=f"t{level}",
            bounds=_TEMP_BOUNDS,
            palette=_TEMP_PALETTE,
            extractor=_extract_kelvin_at_level(("t",), level=level),
            isoline_value=0.0,
        )
    if var == "u":
        return ScalarLayerConfig(
            layer_key=f"u{level}",
            bounds=_UV_BOUNDS,
            palette=_UV_PALETTE,
            extractor=_extract_value_at_level(("u",), level=level),
        )
    if var == "v":
        return ScalarLayerConfig(
            layer_key=f"v{level}",
            bounds=_UV_BOUNDS,
            palette=_UV_PALETTE,
            extractor=_extract_value_at_level(("v",), level=level),
        )
    if var == "w":
        return ScalarLayerConfig(
            layer_key=f"w{level}",
            bounds=_W_BOUNDS,
            palette=_W_PALETTE,
            extractor=_extract_value_at_level(("w",), level=level),
            isoline_value=0.0,
        )
    if var == "r":
        return ScalarLayerConfig(
            layer_key=f"r{level}",
            bounds=_RH_BOUNDS,
            palette=_RH_PALETTE,
            extractor=_extract_value_at_level(("r",), level=level),
        )
    if var == "q":
        return ScalarLayerConfig(
            layer_key=f"q{level}",
            bounds=_q_bounds_for(level),
            palette=_Q_PALETTE,
            extractor=_extract_value_at_level(("q",), level=level),
        )
    if var == "d":
        return ScalarLayerConfig(
            layer_key=f"d{level}",
            bounds=_DV_BOUNDS,
            palette=_DV_PALETTE,
            extractor=_extract_value_at_level(("d",), level=level),
        )
    if var == "vo":
        return ScalarLayerConfig(
            layer_key=f"vo{level}",
            bounds=_DV_BOUNDS,
            palette=_DV_PALETTE,
            extractor=_extract_value_at_level(("vo",), level=level),
        )
    raise ValueError(f"Unknown pressure-level variable {var!r}")


# Build configs for every pressure-level (variable, level) combo
# advertised in the catalogue. Wind speed (the derived √(u²+v²)
# layer at every level) is handled by wind_chart, which adds the
# direction arrows on top of the speed shading. Adding a new
# (variable, level) entry to the catalogue picks up a renderer here
# without further edits.
def _build_pressure_configs() -> list[ScalarLayerConfig]:
    from aiseed_weather.products.catalog import (
        PRESSURE_LEVELS_HPA, PRESSURE_VARIABLES,
    )
    out: list[ScalarLayerConfig] = []
    for (var, *_rest) in PRESSURE_VARIABLES:
        for level in PRESSURE_LEVELS_HPA:
            out.append(_pl_config_for(var, level))
    return out


_PRESSURE_CONFIGS: list[ScalarLayerConfig] = _build_pressure_configs()


# ── Additional surface configs ──────────────────────────────────────
# Sensible palette/bounds for the long tail of ECMWF Open Data surface
# variables (catalog._SURFACE_NEW). Each entry below has a small set
# of bin edges + a matching palette. Adding a new surface variable to
# the catalogue means adding one ScalarLayerConfig here and one chip
# stops entry in map_view.

# Surface pressure: ~960..1040 hPa
_SP_BOUNDS = np.linspace(960, 1040, 21, dtype=np.float32)
SP_CONFIG = ScalarLayerConfig(
    layer_key="sp",
    bounds=_SP_BOUNDS,
    palette=_GH_PALETTE,
    extractor=lambda ds: _squeeze_2d(
        np.asarray(ds["sp"].values, dtype=np.float32),
    ) / 100.0,  # Pa → hPa
)

# 10m wind components (separate from combined wind10m which uses
# wind_chart). Use the universal ±60 m/s diverging palette.
def _make_uv_component_config(key: str, var_names: tuple[str, ...]) -> ScalarLayerConfig:
    return ScalarLayerConfig(
        layer_key=key,
        bounds=_UV_BOUNDS,
        palette=_UV_PALETTE,
        extractor=_extract_value_at_level(var_names, level=None),
    )

U10M_CONFIG = _make_uv_component_config("u10m", ("10u", "u10"))
V10M_CONFIG = _make_uv_component_config("v10m", ("10v", "v10"))
U100M_CONFIG = _make_uv_component_config("u100m", ("100u", "u100"))
V100M_CONFIG = _make_uv_component_config("v100m", ("100v", "v100"))

# Wind gust (10fg) — non-negative speed; same palette as wind10m's.
_GUST_BOUNDS = np.array(
    [0, 5, 10, 15, 20, 25, 30, 35, 40, 50, 60, 75, 100],
    dtype=np.float32,
)
_GUST_PALETTE = (
    "#e6f4f5",
    "#e6f4f5", "#b8e0e8", "#83c8d4", "#52b0c0", "#3a98ad",
    "#7cba74", "#bccf4d", "#f3d33d", "#f59a35", "#e9572a",
    "#a72333", "#5a155f", "#5a155f",
)
GUST_CONFIG = ScalarLayerConfig(
    layer_key="gust",
    bounds=_GUST_BOUNDS,
    palette=_GUST_PALETTE,
    extractor=_extract_value_at_level(("10fg", "fg10", "i10fg"), level=None),
)

# 2t time-statistic siblings — reuse the temperature palette so the
# colour language matches t2m exactly.
def _make_t_stat_config(key: str, var: str) -> ScalarLayerConfig:
    return ScalarLayerConfig(
        layer_key=key,
        bounds=_TEMP_BOUNDS,
        palette=_TEMP_PALETTE,
        extractor=_extract_kelvin_at_level((var,), level=None),
        isoline_value=0.0,
    )

MN2T3_CONFIG = _make_t_stat_config("mn2t3", "mn2t3")
MX2T3_CONFIG = _make_t_stat_config("mx2t3", "mx2t3")
MN2T6_CONFIG = _make_t_stat_config("mn2t6", "mn2t6")
MX2T6_CONFIG = _make_t_stat_config("mx2t6", "mx2t6")

# Precip rate (tprate) — kg/m²/s. Multiply by 3600 to get mm/h for
# readability. Bounds match the tp palette.
TPRATE_CONFIG = ScalarLayerConfig(
    layer_key="tprate",
    bounds=np.array(
        [0.1, 0.5, 1, 2, 5, 10, 20, 30, 50, 75, 100, 150, 200],
        dtype=np.float32,
    ),
    palette=(
        "#f4f4f4", "#c8e6f5", "#9dd1ee", "#6cb6e0",
        "#3a92c8", "#1a73b3", "#2e8b3d", "#62b04f",
        "#a4cd47", "#f0d643", "#f59f1b", "#e54d24",
        "#a31a3a", "#5e1660",
    ),
    extractor=lambda ds: _squeeze_2d(
        np.asarray(ds["tprate"].values, dtype=np.float32),
    ) * 3600.0,  # mm/s → mm/h
)

# Runoff (ro), accumulated metres
RO_CONFIG = ScalarLayerConfig(
    layer_key="ro",
    bounds=np.array(
        [0.0001, 0.0005, 0.001, 0.002, 0.005, 0.01, 0.02, 0.05, 0.1, 0.2],
        dtype=np.float32,
    ),
    palette=(
        "#f4f4f4", "#dfeaf3", "#bcd5ea", "#88b9dd",
        "#5c9bd0", "#3b7fc3", "#2860a8", "#1a4486",
        "#0f2a5e", "#06223a", "#03162a",
    ),
    extractor=_extract_value_at_level(("ro",), level=None),
)

# Snowfall water equivalent (sf), m
SF_CONFIG = ScalarLayerConfig(
    layer_key="sf",
    bounds=np.array(
        [0.001, 0.002, 0.005, 0.01, 0.02, 0.05, 0.1, 0.2, 0.5],
        dtype=np.float32,
    ),
    palette=(
        "#f4f4f4", "#e8eff7", "#bcd5ea", "#88b9dd",
        "#5c9bd0", "#3b7fc3", "#2860a8", "#1a4486",
        "#0f2a5e", "#06223a",
    ),
    extractor=_extract_value_at_level(("sf",), level=None),
)

# Snow albedo (asn), 0..1
ASN_CONFIG = ScalarLayerConfig(
    layer_key="asn",
    bounds=np.linspace(0.1, 0.9, 11, dtype=np.float32),
    palette=(
        "#3a3a3a", "#5a5a5a", "#7a7a7a", "#9a9a9a",
        "#b8b8b8", "#cfcfcf", "#dedede", "#eaeaea",
        "#f3f3f3", "#fafafa", "#ffffff", "#ffffff",
    ),
    extractor=_extract_value_at_level(("asn",), level=None),
)

# Snow density (rsn), kg/m³ — fresh snow ~50, packed ~400
RSN_CONFIG = ScalarLayerConfig(
    layer_key="rsn",
    bounds=np.linspace(50, 450, 11, dtype=np.float32),
    palette=(
        "#f4f4f4", "#dfeaf3", "#bcd5ea", "#88b9dd",
        "#5c9bd0", "#3b7fc3", "#2860a8", "#1a4486",
        "#0f2a5e", "#06223a", "#03162a", "#02101e",
    ),
    extractor=_extract_value_at_level(("rsn",), level=None),
)

# Total column water vapour (tcwv), kg/m² — 0..70
TCWV_CONFIG = ScalarLayerConfig(
    layer_key="tcwv",
    bounds=np.linspace(5, 65, 13, dtype=np.float32),
    palette=(
        "#f4f4f4", "#e8efe2", "#cfe2c8", "#a8d3b1",
        "#80c1a6", "#5cae9c", "#3a8fa0", "#216e9c",
        "#16548a", "#0e3d6e", "#0a3052", "#06223a",
        "#02101e", "#000000",
    ),
    extractor=_extract_value_at_level(("tcwv",), level=None),
)

# Most-unstable CAPE (mucape), J/kg — 0..5000
MUCAPE_CONFIG = ScalarLayerConfig(
    layer_key="mucape",
    bounds=np.array(
        [50, 100, 250, 500, 750, 1000, 1500, 2000, 2500, 3500, 5000],
        dtype=np.float32,
    ),
    palette=(
        "#f4f4f4", "#e6e9c8", "#cad6a4", "#9ec280",
        "#74a85c", "#5a8d3a", "#7a8a26", "#a88a18",
        "#c66a14", "#a82418", "#82130f", "#3c0404",
    ),
    extractor=_extract_value_at_level(("mucape", "cape"), level=None),
)


# Accumulated radiation (J/m²) — split between mostly-non-negative
# fluxes (SW, downward LW) and diverging fluxes (net LW, OLR). Bins
# are scaled assuming a 3-hourly accumulation: 3600 J/m²/W × 3h
# ≈ 11 MJ/m² for 1 kW/m² → upper bin ~3e7 J/m².
_RAD_SW_BOUNDS = np.linspace(0, 3e7, 11, dtype=np.float32)
_RAD_SW_PALETTE = (
    "#0a3a82", "#1a6cbf", "#3b8fc8", "#74b1d3",
    "#bcd5ea", "#f4f4f4",
    "#f5db58", "#f9b840", "#f59030", "#ec6c28", "#bb2418", "#3c0404",
)

def _make_rad_sw_config(key: str, var_names: tuple[str, ...]) -> ScalarLayerConfig:
    return ScalarLayerConfig(
        layer_key=key,
        bounds=_RAD_SW_BOUNDS,
        palette=_RAD_SW_PALETTE,
        extractor=_extract_value_at_level(var_names, level=None),
    )

SSR_CONFIG  = _make_rad_sw_config("ssr",  ("ssr",))
SSRD_CONFIG = _make_rad_sw_config("ssrd", ("ssrd",))
STRD_CONFIG = _make_rad_sw_config("strd", ("strd",))

# Diverging radiation (net LW at surface, OLR at top) — symmetric
# around zero. OLR values are negative (outgoing).
_RAD_LW_BOUNDS = np.linspace(-2e7, 2e7, 13, dtype=np.float32)
_RAD_LW_PALETTE = (
    "#0a3a82", "#1a6cbf", "#3b8fc8", "#74b1d3",
    "#bcd5ea", "#dfeaf3", "#f4f4f4",
    "#f5db58", "#f9b840", "#f59030", "#ec6c28", "#bb2418", "#3c0404",
    "#1f0000",
)

def _make_rad_lw_config(key: str, var_names: tuple[str, ...]) -> ScalarLayerConfig:
    return ScalarLayerConfig(
        layer_key=key,
        bounds=_RAD_LW_BOUNDS,
        palette=_RAD_LW_PALETTE,
        extractor=_extract_value_at_level(var_names, level=None),
    )

STR_LW_CONFIG = _make_rad_lw_config("str_lw", ("str",))
TTR_CONFIG    = _make_rad_lw_config("ttr",    ("ttr",))

# Surface stress (N/m²·s accumulated). Small magnitudes; diverging.
_STRESS_BOUNDS = np.array(
    [-2e4, -1e4, -5e3, -2e3, -5e2,
     5e2, 2e3, 5e3, 1e4, 2e4],
    dtype=np.float32,
)
_STRESS_PALETTE = (
    "#1a0030", "#2c0a4d", "#3a4a9d", "#1b81c4", "#84c0c8",
    "#f4f4f4",
    "#f9e088", "#f9b04e", "#c93920", "#82130f", "#3c0404",
)
EWSS_CONFIG = ScalarLayerConfig(
    layer_key="ewss", bounds=_STRESS_BOUNDS, palette=_STRESS_PALETTE,
    extractor=_extract_value_at_level(("ewss",), level=None),
)
NSSS_CONFIG = ScalarLayerConfig(
    layer_key="nsss", bounds=_STRESS_BOUNDS, palette=_STRESS_PALETTE,
    extractor=_extract_value_at_level(("nsss",), level=None),
)

# Wave height (swh) in metres
SWH_CONFIG = ScalarLayerConfig(
    layer_key="swh",
    bounds=np.array(
        [0.5, 1, 1.5, 2, 3, 4, 5, 6, 8, 10, 14],
        dtype=np.float32,
    ),
    palette=(
        "#f4f4f4", "#e8efe2", "#bcd5ea", "#88b9dd",
        "#5c9bd0", "#3b7fc3", "#2860a8", "#1a4486",
        "#0f2a5e", "#a82418", "#82130f", "#3c0404",
    ),
    extractor=_extract_value_at_level(("swh",), level=None),
)

# Wave period (s) — same palette family across mp2 / mwp / pp1d
_WAVE_T_BOUNDS = np.array(
    [2, 4, 6, 8, 10, 12, 14, 16, 18, 20],
    dtype=np.float32,
)
_WAVE_T_PALETTE = (
    "#e8efe2", "#cfe2c8", "#a8d3b1", "#80c1a6",
    "#5cae9c", "#3a8fa0", "#216e9c", "#16548a",
    "#0e3d6e", "#0a3052", "#06223a",
)

def _make_wavet_config(key: str) -> ScalarLayerConfig:
    return ScalarLayerConfig(
        layer_key=key,
        bounds=_WAVE_T_BOUNDS,
        palette=_WAVE_T_PALETTE,
        extractor=_extract_value_at_level((key,), level=None),
    )

MP2_CONFIG = _make_wavet_config("mp2")
MWP_CONFIG = _make_wavet_config("mwp")
PP1D_CONFIG = _make_wavet_config("pp1d")

# Sea velocity components (m/s, diverging like uv)
SVE_CONFIG = ScalarLayerConfig(
    layer_key="sve", bounds=_UV_BOUNDS, palette=_UV_PALETTE,
    extractor=_extract_value_at_level(("sve",), level=None),
)
SVN_CONFIG = ScalarLayerConfig(
    layer_key="svn", bounds=_UV_BOUNDS, palette=_UV_PALETTE,
    extractor=_extract_value_at_level(("svn",), level=None),
)

# Sea ice thickness (m)
SITHICK_CONFIG = ScalarLayerConfig(
    layer_key="sithick",
    bounds=np.linspace(0.1, 5.0, 11, dtype=np.float32),
    palette=(
        "#063052", "#0f3a6e", "#1a548a", "#3b7fc3",
        "#5c9bd0", "#88b9dd", "#bcd5ea", "#dfeaf3",
        "#eef0f4", "#fafafa", "#ffffff", "#ffffff",
    ),
    extractor=_extract_value_at_level(("sithick",), level=None),
)

# Sea surface height (m), diverging around 0
_ZOS_BOUNDS = np.linspace(-2.0, 2.0, 13, dtype=np.float32)
ZOS_CONFIG = ScalarLayerConfig(
    layer_key="zos", bounds=_ZOS_BOUNDS, palette=_GH_PALETTE[:13] + (_GH_PALETTE[13],),
    extractor=_extract_value_at_level(("zos",), level=None),
)


_SURFACE_CONFIGS: list[ScalarLayerConfig] = [
    SP_CONFIG,
    U10M_CONFIG, V10M_CONFIG, U100M_CONFIG, V100M_CONFIG,
    GUST_CONFIG,
    MN2T3_CONFIG, MX2T3_CONFIG, MN2T6_CONFIG, MX2T6_CONFIG,
    TPRATE_CONFIG, RO_CONFIG, SF_CONFIG,
    ASN_CONFIG, RSN_CONFIG, TCWV_CONFIG, MUCAPE_CONFIG,
    SSR_CONFIG, SSRD_CONFIG, STRD_CONFIG,
    STR_LW_CONFIG, TTR_CONFIG,
    EWSS_CONFIG, NSSS_CONFIG,
    SWH_CONFIG, MP2_CONFIG, MWP_CONFIG, PP1D_CONFIG,
    SVE_CONFIG, SVN_CONFIG, SITHICK_CONFIG, ZOS_CONFIG,
]


# ── Soil layer configs (sot / vsw at layers 1-4) ────────────────────


def _soil_extractor(var_names: tuple[str, ...], layer: int, kelvin: bool):
    def _fn(ds):
        for n in var_names:
            if n in ds.data_vars:
                da = ds[n]
                if "depthBelowLandLayer" in da.dims:
                    da = da.sel(depthBelowLandLayer=layer)
                elif "soilLayer" in da.dims:
                    da = da.sel(soilLayer=layer)
                arr = _squeeze_2d(np.asarray(da.values, dtype=np.float32))
                return arr - 273.15 if kelvin else arr
        raise ValueError(
            f"None of {var_names!r} found in dataset; "
            f"vars={list(ds.data_vars)}",
        )
    return _fn


def _build_soil_configs() -> list[ScalarLayerConfig]:
    from aiseed_weather.products.catalog import SOIL_LAYERS

    out: list[ScalarLayerConfig] = []
    for layer in SOIL_LAYERS:
        # sot — soil temperature (K → °C)
        out.append(ScalarLayerConfig(
            layer_key=f"sot_{layer}",
            bounds=_TEMP_BOUNDS,
            palette=_TEMP_PALETTE,
            extractor=_soil_extractor(("sot", "stl"), layer, kelvin=True),
            isoline_value=0.0,
        ))
        # vsw — volumetric soil water (m³/m³), 0..0.5
        out.append(ScalarLayerConfig(
            layer_key=f"vsw_{layer}",
            bounds=np.linspace(0.05, 0.5, 11, dtype=np.float32),
            palette=(
                "#f4f4f4", "#e8efe2", "#cfe2c8", "#a8d3b1",
                "#80c1a6", "#5cae9c", "#3a8fa0", "#216e9c",
                "#16548a", "#0e3d6e", "#0a3052", "#06223a",
            ),
            extractor=_soil_extractor(("vsw", "swvl"), layer, kelvin=False),
        ))
    return out


_SOIL_CONFIGS: list[ScalarLayerConfig] = _build_soil_configs()


CONFIGS: dict[str, ScalarLayerConfig] = {
    cfg.layer_key: cfg
    for cfg in (
        D2M_CONFIG, SKT_CONFIG, SD_CONFIG, TCC_CONFIG,
        *_PRESSURE_CONFIGS,
        *_SURFACE_CONFIGS,
        *_SOIL_CONFIGS,
    )
}
