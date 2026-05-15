# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Yasuhiro / AIseed

"""Mean sea level pressure chart — color-shaded with isobar overlay.

Modern weather visualization (Windy, ECMWF Charts, JMA web) presents
pressure as both a continuous color field AND classical isobars on
top — the color carries the magnitude, the isobars carry the slope
and the synoptic structure (4 hPa interval, bold every 20 hPa).

This renderer:
1. Shades the field with a diverging palette centered on the
   standard atmosphere (1013.25 hPa). Low pressure trends blue,
   high pressure trends red, with white near standard.
2. Overlays thin (4 hPa) and bold (20 hPa) isobars in black.
3. Adds inline labels via matplotlib's clabel (Windy-style pill
   labels are a Stage-2 PIL refactor — not done yet).
4. Adds a horizontal colorbar.

Per-frame cost is ~5s on cartopy + matplotlib. Stage 2's fast PIL
path can apply: numpy colormap LUT + scipy.ndimage reproject (~150
ms) and PIL line drawing for the contour overlay.

Previous versions of this module cached a per-region Figure to
amortise the cartopy basemap setup. With the colorbar (which adds
an extra Axes per render) and pcolormesh (which adds a QuadMesh
collection) the artist-lifecycle code got fragile, so the cache is
gone for now in favour of correctness — each render builds a fresh
Figure. This matches the per-frame budget of the T2m and TP
renderers.
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

# Diverging palette centered on 1013 hPa. White / near-white at the
# standard atmosphere means isobars overlaid on top stay legible
# everywhere.
MSL_CMAP_NAME = "RdBu_r"  # red high, blue low — meteorological convention
MSL_VMIN_HPA = 940.0
MSL_VMAX_HPA = 1064.0
MSL_VCENTER_HPA = 1013.0  # standard atmosphere

# Synoptic isobar cadence: thin 4 hPa, bold 20 hPa.
ISOBAR_LEVELS_THIN = np.arange(940, 1064, 4)
ISOBAR_LEVELS_BOLD = np.arange(940, 1064, 20)


def _make_projection(name: str) -> ccrs.Projection:
    try:
        return _PROJECTIONS[name]()
    except KeyError as e:
        raise ValueError(f"Unknown projection '{name}'") from e


def render_msl(
    ds: xr.Dataset,
    *,
    region: Region = GLOBAL,
    run_id: str,
    dpi: int = _DPI,
) -> bytes:
    msl_hpa = (ds["msl"] / 100.0).values
    longitudes = ds["longitude"].values
    latitudes = ds["latitude"].values

    fig = Figure(figsize=_FIG_SIZE)
    FigureCanvasAgg(fig)
    ax = fig.add_subplot(1, 1, 1, projection=_make_projection(region.projection))

    if region.extent is None:
        ax.set_global()
    else:
        ax.set_extent(region.extent, crs=ccrs.PlateCarree())

    # ---- Base: color-shaded pressure field ----
    # TwoSlopeNorm asymmetrically maps to put white at 1013 hPa even
    # though our range isn't symmetric around it.
    norm = mcolors.TwoSlopeNorm(
        vmin=MSL_VMIN_HPA, vcenter=MSL_VCENTER_HPA, vmax=MSL_VMAX_HPA,
    )
    mesh = ax.pcolormesh(
        longitudes, latitudes, msl_hpa,
        cmap=MSL_CMAP_NAME, norm=norm,
        transform=ccrs.PlateCarree(),
        shading="auto",
        # Moderate alpha so the contours stamped on top remain crisp
        # without hiding the colour gradient.
        alpha=0.7,
    )

    # ---- Coastlines + grid above shading, below contours ----
    ax.coastlines(linewidth=0.6, color="#333333")
    ax.gridlines(draw_labels=False, linewidth=0.3, color="#666666", alpha=0.5)

    # ---- Overlay: classical isobars ----
    cs_thin = ax.contour(
        longitudes, latitudes, msl_hpa,
        levels=ISOBAR_LEVELS_THIN,
        transform=ccrs.PlateCarree(),
        colors="black", linewidths=0.5,
    )
    ax.contour(
        longitudes, latitudes, msl_hpa,
        levels=ISOBAR_LEVELS_BOLD,
        transform=ccrs.PlateCarree(),
        colors="black", linewidths=1.1,
    )
    ax.clabel(cs_thin, inline=True, fontsize=6, fmt="%d")

    # ---- Colorbar ----
    cbar = fig.colorbar(
        mesh, ax=ax,
        orientation="horizontal", pad=0.04, shrink=0.7,
    )
    cbar.set_label("MSL [hPa]")
    cbar.set_ticks(np.arange(940, 1065, 20))

    valid_time = pd.Timestamp(ds["valid_time"].values).strftime(
        "%Y-%m-%d %H:%M UTC",
    )
    fig.suptitle(f"MSL [hPa] — valid {valid_time}", fontsize=13)
    apply_footer(fig, data_source="ECMWF Open Data", run_id=run_id)

    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=dpi, bbox_inches="tight")
    return buf.getvalue()
