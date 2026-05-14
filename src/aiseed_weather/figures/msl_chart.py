# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Yasuhiro / AIseed

"""Mean sea level pressure synoptic chart.

Renders MSL isobars at the 4 hPa interval (synoptic convention) on a chosen
projection. Returns a matplotlib Figure; embeds nothing Flet-aware. See the
`weather-rendering` skill for layer conventions.
"""

from __future__ import annotations

import cartopy.crs as ccrs
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import xarray as xr
from matplotlib.figure import Figure

from aiseed_weather.figures.footer import apply_footer
from aiseed_weather.figures.regions import GLOBAL, Region


_PROJECTIONS = {
    "robinson": ccrs.Robinson,
    "platecarree": ccrs.PlateCarree,
    "mercator": ccrs.Mercator,
    "north_polar": ccrs.NorthPolarStereo,
}


def _make_projection(name: str) -> ccrs.Projection:
    try:
        return _PROJECTIONS[name]()
    except KeyError as e:
        raise ValueError(
            f"Unknown projection '{name}'. Choose one of {sorted(_PROJECTIONS)}."
        ) from e


def render_msl(
    ds: xr.Dataset,
    *,
    region: Region = GLOBAL,
    run_id: str,
) -> Figure:
    msl_hpa = ds["msl"] / 100.0
    longitudes = ds["longitude"].values
    latitudes = ds["latitude"].values

    fig = plt.figure(figsize=(12, 7))
    ax = plt.axes(projection=_make_projection(region.projection))
    if region.extent is None:
        ax.set_global()
    else:
        # cartopy expects (lon_min, lon_max, lat_min, lat_max).
        ax.set_extent(region.extent, crs=ccrs.PlateCarree())
    ax.coastlines(linewidth=0.6, color="#444444")
    ax.gridlines(draw_labels=False, linewidth=0.3, color="#888888", alpha=0.5)

    # Isobars at 4 hPa, bold every 20 hPa (synoptic convention).
    levels_thin = np.arange(940, 1064, 4)
    levels_bold = np.arange(940, 1064, 20)
    cs_thin = ax.contour(
        longitudes, latitudes, msl_hpa,
        levels=levels_thin,
        transform=ccrs.PlateCarree(),
        colors="black", linewidths=0.5,
    )
    ax.contour(
        longitudes, latitudes, msl_hpa,
        levels=levels_bold,
        transform=ccrs.PlateCarree(),
        colors="black", linewidths=1.0,
    )
    ax.clabel(cs_thin, inline=True, fontsize=6, fmt="%d")

    valid_time = pd.Timestamp(ds["valid_time"].values).strftime("%Y-%m-%d %H:%M UTC")
    fig.suptitle(f"MSL [hPa] — valid {valid_time}", fontsize=13)
    apply_footer(fig, data_source="ECMWF Open Data", run_id=run_id)
    return fig
