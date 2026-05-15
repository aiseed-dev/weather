# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Yasuhiro / AIseed

"""2-metre temperature chart (color-shaded with 0°C bold isotherm).

Unlike MSL which is just isobars on white, surface temperature is
typically presented as a continuous color field (-40°C to +40°C with
a diverging blue-white-red ramp) with a single bold 0°C contour and
a colorbar. This is the standard synoptic forecast presentation.

Per-frame rendering still goes through matplotlib + cartopy here;
optimization to a PIL fast path is Stage 2 work (the same renderer
architecture you'd build for msl will apply, plus a colormap LUT).
"""

from __future__ import annotations

import io

import cartopy.crs as ccrs
import matplotlib.colors as mcolors
import numpy as np
import pandas as pd
import xarray as xr
from matplotlib.backends.backend_agg import FigureCanvasAgg
from matplotlib.figure import Figure

from aiseed_weather.figures.footer import apply_footer
from aiseed_weather.figures.regions import GLOBAL, Region


_PROJECTIONS = {
    "robinson": ccrs.Robinson,
    "platecarree": ccrs.PlateCarree,
    "mercator": ccrs.Mercator,
    "north_polar": ccrs.NorthPolarStereo,
}

_FIG_SIZE = (12, 7)
_DPI = 120

# T2m colormap: -40°C to +40°C in 2°C bins. Diverging blue-white-red
# is the de-facto convention (NOAA, ECMWF Charts, JMA all use variants).
T2M_LEVELS = np.arange(-40, 42, 2)
# Hand-tuned synoptic palette: deep purple → blue → white → red → dark red.
T2M_COLORS = [
    "#2c0a4d", "#3c1d7a", "#43378e", "#3a4a9d", "#2965b2",
    "#1b81c4", "#3296c8", "#5cabc8", "#84c0c8", "#a8d3c4",
    "#c4dfba", "#e0eab2", "#f5f0a8", "#f9e088", "#facb68",
    "#f9b04e", "#f5933a", "#ed7530", "#de5526", "#c93920",
    "#a82418", "#82130f", "#580808", "#3c0404",
]
# Pad to one less than levels count (BoundaryNorm wants N colors for N+1 boundaries).
while len(T2M_COLORS) < len(T2M_LEVELS) - 1:
    T2M_COLORS.append(T2M_COLORS[-1])
T2M_CMAP = mcolors.ListedColormap(T2M_COLORS[: len(T2M_LEVELS) - 1])
T2M_CMAP.set_under("#1a0030")
T2M_CMAP.set_over("#1f0000")
T2M_NORM = mcolors.BoundaryNorm(T2M_LEVELS, T2M_CMAP.N, extend="both")


def _make_projection(name: str) -> ccrs.Projection:
    try:
        return _PROJECTIONS[name]()
    except KeyError as e:
        raise ValueError(f"Unknown projection '{name}'") from e


def _extract_t2m(ds: xr.Dataset) -> np.ndarray:
    """Return 2m temperature in °C. Variable name varies a bit between
    decoders — try the common ones in order before giving up."""
    for v in ("t2m", "2t"):
        if v in ds.data_vars:
            return ds[v].values - 273.15
    # Some GRIB decodings expose a generic 't' with a 'heightAboveGround'
    # coordinate at 2m. Last resort.
    if "t" in ds.data_vars:
        return ds["t"].values - 273.15
    raise ValueError(
        f"No 2m temperature variable in dataset; vars={list(ds.data_vars)}",
    )


def render_t2m(
    ds: xr.Dataset,
    *,
    region: Region = GLOBAL,
    run_id: str,
    dpi: int = _DPI,
    msl_overlay_ds: xr.Dataset | None = None,
) -> bytes:
    t_C = _extract_t2m(ds)
    longitudes = ds["longitude"].values
    latitudes = ds["latitude"].values

    fig = Figure(figsize=_FIG_SIZE)
    FigureCanvasAgg(fig)
    ax = fig.add_subplot(1, 1, 1, projection=_make_projection(region.projection))
    if region.extent is None:
        ax.set_global()
    else:
        ax.set_extent(region.extent, crs=ccrs.PlateCarree())

    # Shaded temperature field
    mesh = ax.pcolormesh(
        longitudes, latitudes, t_C,
        cmap=T2M_CMAP, norm=T2M_NORM,
        transform=ccrs.PlateCarree(),
        shading="auto",
    )

    # Coastlines + grid on top of shading (otherwise hidden)
    ax.coastlines(linewidth=0.6, color="black")
    ax.gridlines(draw_labels=False, linewidth=0.3, color="#666666", alpha=0.6)

    # 0°C bold isotherm — climatologically important boundary.
    ax.contour(
        longitudes, latitudes, t_C,
        levels=[0],
        transform=ccrs.PlateCarree(),
        colors="black", linewidths=1.3,
    )

    # Optional MSL contour overlay (must be fetched separately by caller).
    if msl_overlay_ds is not None:
        from aiseed_weather.figures.overlays import add_msl_contours
        add_msl_contours(ax, msl_overlay_ds)

    # Colorbar (horizontal, below chart). Sparse ticks every 10°C.
    cbar = fig.colorbar(
        mesh, ax=ax,
        orientation="horizontal", pad=0.04, shrink=0.7,
        extend="both",
    )
    cbar.set_label("2m Temperature [°C]")
    cbar.set_ticks(np.arange(-40, 41, 10))

    valid_time = pd.Timestamp(ds["valid_time"].values).strftime(
        "%Y-%m-%d %H:%M UTC",
    )
    fig.suptitle(f"2m Temperature [°C] — valid {valid_time}", fontsize=13)
    apply_footer(fig, data_source="ECMWF Open Data", run_id=run_id)

    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=dpi, bbox_inches="tight")
    return buf.getvalue()
