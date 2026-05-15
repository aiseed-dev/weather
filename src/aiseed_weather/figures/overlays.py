# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Yasuhiro / AIseed

"""Reusable overlay drawing helpers.

The chart architecture is base shading + optional overlays:
  base       — pcolormesh of any field with a colormap (msl, t2m, tp,
                wind speed, gh500, …)
  overlays   — contour lines (MSL isobars, gh isohypsae), wind arrows,
                H/L pressure markers, etc.

Renderers in figures/{msl,t2m,tp,wind}_chart.py accept overlay
datasets as optional kwargs and call helpers from this module after
they're done with their own base + base-specific overlays. Helpers
do not own a Figure — they just add artists to a given Axes.
"""

from __future__ import annotations

import cartopy.crs as ccrs
import numpy as np
import xarray as xr


# Synoptic convention: thin every 4 hPa, bold every 20 hPa.
_ISOBAR_LEVELS_THIN = np.arange(940, 1064, 4)
_ISOBAR_LEVELS_BOLD = np.arange(940, 1064, 20)


def add_msl_contours(
    ax,
    msl_ds: xr.Dataset,
    *,
    color: str = "black",
    thin_width: float = 0.5,
    bold_width: float = 1.1,
    alpha: float = 0.9,
    label: bool = True,
) -> None:
    """Stamp MSL isobars on top of an existing axes (typically a
    base-shaded chart of a different field).

    The MSL dataset is usually from a separate GRIB download; the
    caller is responsible for fetching it (with the same base time +
    step as the base data so the contours align with the shading).
    """
    msl_hpa = (msl_ds["msl"] / 100.0).values
    lons = msl_ds["longitude"].values
    lats = msl_ds["latitude"].values

    cs_thin = ax.contour(
        lons, lats, msl_hpa,
        levels=_ISOBAR_LEVELS_THIN,
        transform=ccrs.PlateCarree(),
        colors=color, linewidths=thin_width, alpha=alpha,
    )
    ax.contour(
        lons, lats, msl_hpa,
        levels=_ISOBAR_LEVELS_BOLD,
        transform=ccrs.PlateCarree(),
        colors=color, linewidths=bold_width, alpha=alpha,
    )
    if label:
        ax.clabel(cs_thin, inline=True, fontsize=6, fmt="%d")
