# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Yasuhiro / AIseed

"""Total precipitation chart with non-linear colormap.

Precipitation is highly skewed: most cells get 0 mm, a few get tens
to hundreds. A linear colormap wastes 90% of the dynamic range. The
standard meteorological scheme uses discrete bins on a logarithmic-
ish progression so trace amounts (0.1-1 mm) and extreme events
(>100 mm) are both readable at a glance.

ECMWF Open Data publishes "tp" as accumulated total precipitation
from the run start, in metres. We convert to mm here. Per-period
totals (e.g. 3h or 6h rainfall) need a difference between successive
steps — that's a follow-up; current rendering shows accumulated since
T+0h.
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

# Non-linear bins in mm — mirrors JMA/Windy/ECMWF charts. Bins are
# (0.1, 0.5, 1, 2, 5, 10, 20, 30, 50, 75, 100, 150, 200, ∞).
TP_BOUNDS_MM = [0.1, 0.5, 1, 2, 5, 10, 20, 30, 50, 75, 100, 150, 200]
TP_COLORS = [
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
TP_CMAP = mcolors.ListedColormap(TP_COLORS)
TP_CMAP.set_under((0, 0, 0, 0))  # transparent below 0.1 mm
TP_CMAP.set_over(TP_COLORS[-1])
TP_NORM = mcolors.BoundaryNorm(TP_BOUNDS_MM, TP_CMAP.N, extend="max")


def _make_projection(name: str) -> ccrs.Projection:
    try:
        return _PROJECTIONS[name]()
    except KeyError as e:
        raise ValueError(f"Unknown projection '{name}'") from e


def _extract_tp_mm(ds: xr.Dataset) -> np.ndarray:
    """Return total precipitation in mm. ECMWF publishes it in metres
    of water equivalent under the variable name 'tp'."""
    if "tp" in ds.data_vars:
        return ds["tp"].values * 1000.0  # m → mm
    raise ValueError(
        f"No 'tp' (total precipitation) variable in dataset; "
        f"vars={list(ds.data_vars)}",
    )


def render_tp(
    ds: xr.Dataset,
    *,
    region: Region = GLOBAL,
    run_id: str,
    dpi: int = _DPI,
    msl_overlay_ds: xr.Dataset | None = None,
) -> bytes:
    tp_mm = _extract_tp_mm(ds)
    longitudes = ds["longitude"].values
    latitudes = ds["latitude"].values

    fig = Figure(figsize=_FIG_SIZE)
    FigureCanvasAgg(fig)
    ax = fig.add_subplot(1, 1, 1, projection=_make_projection(region.projection))
    if region.extent is None:
        ax.set_global()
    else:
        ax.set_extent(region.extent, crs=ccrs.PlateCarree())

    # (Future: ax.add_feature(cartopy.feature.LAND, facecolor="#f0eee8")
    # so dry land doesn't read as "no data". Skipped for now to keep
    # this renderer focused on the actual precip layer; the absence of
    # color is also a valid signal for "no rain".)

    mesh = ax.pcolormesh(
        longitudes, latitudes, tp_mm,
        cmap=TP_CMAP, norm=TP_NORM,
        transform=ccrs.PlateCarree(),
        shading="auto",
    )

    ax.coastlines(linewidth=0.6, color="#333333")
    ax.gridlines(draw_labels=False, linewidth=0.3, color="#888888", alpha=0.5)

    # Optional MSL contour overlay — Windy-style "rain + isobars".
    if msl_overlay_ds is not None:
        from aiseed_weather.figures.overlays import add_msl_contours
        add_msl_contours(ax, msl_overlay_ds)

    cbar = fig.colorbar(
        mesh, ax=ax,
        orientation="horizontal", pad=0.04, shrink=0.7,
        extend="max",
        spacing="uniform",  # equal-width bins regardless of value spread
    )
    cbar.set_label(
        "Total precipitation since run start [mm]",
    )
    # Show every label since bins are coarse but meaningful.
    cbar.set_ticks(TP_BOUNDS_MM)
    cbar.ax.tick_params(labelsize=8)

    valid_time = pd.Timestamp(ds["valid_time"].values).strftime(
        "%Y-%m-%d %H:%M UTC",
    )
    fig.suptitle(
        f"Total precipitation [mm, since T+0h] — valid {valid_time}",
        fontsize=13,
    )
    apply_footer(fig, data_source="ECMWF Open Data", run_id=run_id)

    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=dpi, bbox_inches="tight")
    return buf.getvalue()
