# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Yasuhiro / AIseed

"""10 m wind chart — speed shading + direction arrows.

Windy.com renders wind as moving particle animations, which looks
gorgeous but needs a GPU canvas and per-frame animation that doesn't
fit our PNG-based pipeline. As a static analogue we use the ECMWF
chart convention: pcolormesh of wind speed (m/s) with a non-linear
binned palette, plus quiver arrows showing direction. Arrow density
is subsampled so the chart reads cleanly at any zoom.

Particle animation could come later via:
  - A WebGL renderer in a Flet WebView, or
  - Pre-rendered short-loop animations (advect particles for N steps
    on the same wind field, output as animated PNG/WebP).

Both are infrastructure changes beyond the scope of this commit.
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

# Wind speed bins in m/s. Beaufort-ish progression: calm → light air →
# breeze → gale → storm → hurricane. Standard meteorological scheme.
WIND_BOUNDS_MS = [0, 2, 5, 8, 10, 12, 15, 20, 25, 30, 40, 50, 60]
WIND_COLORS = [
    "#e6f4f5",  # 0-2   calm: pale cyan
    "#b8e0e8",  # 2-5
    "#83c8d4",  # 5-8
    "#52b0c0",  # 8-10
    "#3a98ad",  # 10-12 saturated cyan
    "#7cba74",  # 12-15 green (moderate breeze)
    "#bccf4d",  # 15-20 yellow-green
    "#f3d33d",  # 20-25 yellow (near gale)
    "#f59a35",  # 25-30 orange (gale)
    "#e9572a",  # 30-40 red (strong gale)
    "#a72333",  # 40-50 crimson (storm)
    "#5a155f",  # 50-60+ purple (hurricane)
]
WIND_CMAP = mcolors.ListedColormap(WIND_COLORS)
WIND_CMAP.set_under(WIND_COLORS[0])
WIND_CMAP.set_over(WIND_COLORS[-1])
WIND_NORM = mcolors.BoundaryNorm(WIND_BOUNDS_MS, WIND_CMAP.N, extend="max")


def _make_projection(name: str) -> ccrs.Projection:
    try:
        return _PROJECTIONS[name]()
    except KeyError as e:
        raise ValueError(f"Unknown projection '{name}'") from e


def _extract_uv10(ds: xr.Dataset) -> tuple[np.ndarray, np.ndarray]:
    """Return (u, v) at 10 m in m/s. ECMWF/cfgrib names vary."""
    u_arr = v_arr = None
    for u_name in ("u10", "10u"):
        if u_name in ds.data_vars:
            u_arr = ds[u_name].values
            break
    for v_name in ("v10", "10v"):
        if v_name in ds.data_vars:
            v_arr = ds[v_name].values
            break
    if u_arr is None or v_arr is None:
        raise ValueError(
            f"No 10 m wind components in dataset; "
            f"vars={list(ds.data_vars)}",
        )
    return u_arr, v_arr


def _arrow_subsample(shape: tuple[int, int], region: Region) -> tuple[int, int]:
    """Pick (step_lat, step_lon) so we get ~25-40 arrows across.

    Too many and the chart becomes a black mat; too few and structure
    is lost. Global gets coarser sampling than a regional zoom because
    the same screen real estate has to cover more grid cells.
    """
    n_lat, n_lon = shape
    if region.extent is None:
        target_arrows_lon, target_arrows_lat = 48, 24
    else:
        target_arrows_lon, target_arrows_lat = 32, 22
    step_lon = max(1, n_lon // target_arrows_lon)
    step_lat = max(1, n_lat // target_arrows_lat)
    return step_lat, step_lon


def render_wind(
    ds: xr.Dataset,
    *,
    region: Region = GLOBAL,
    run_id: str,
    dpi: int = _DPI,
    msl_overlay_ds: xr.Dataset | None = None,
) -> bytes:
    u, v = _extract_uv10(ds)
    wspd = np.sqrt(u * u + v * v)
    longitudes = ds["longitude"].values
    latitudes = ds["latitude"].values

    fig = Figure(figsize=_FIG_SIZE)
    FigureCanvasAgg(fig)
    ax = fig.add_subplot(1, 1, 1, projection=_make_projection(region.projection))
    if region.extent is None:
        ax.set_global()
    else:
        ax.set_extent(region.extent, crs=ccrs.PlateCarree())

    # Base: wind-speed shading.
    mesh = ax.pcolormesh(
        longitudes, latitudes, wspd,
        cmap=WIND_CMAP, norm=WIND_NORM,
        transform=ccrs.PlateCarree(),
        shading="auto",
    )

    ax.coastlines(linewidth=0.6, color="#222222")
    ax.gridlines(draw_labels=False, linewidth=0.3, color="#666666", alpha=0.5)

    # Overlay: direction arrows (subsampled).
    step_lat, step_lon = _arrow_subsample(u.shape, region)
    ax.quiver(
        longitudes[::step_lon],
        latitudes[::step_lat],
        u[::step_lat, ::step_lon],
        v[::step_lat, ::step_lon],
        transform=ccrs.PlateCarree(),
        # scale_units='width' makes the arrow size relative to the
        # axes width; larger scale → smaller arrows. Tuned by eye.
        scale=400,
        scale_units="width",
        width=0.0015,
        headwidth=4,
        headlength=5,
        color="black",
        alpha=0.75,
    )

    # Optional MSL contour overlay (e.g. wind speed + isobars).
    if msl_overlay_ds is not None:
        from aiseed_weather.figures.overlays import add_msl_contours
        add_msl_contours(ax, msl_overlay_ds)

    cbar = fig.colorbar(
        mesh, ax=ax,
        orientation="horizontal", pad=0.04, shrink=0.7,
        extend="max",
        spacing="uniform",
    )
    cbar.set_label("10 m wind speed [m/s]")
    cbar.set_ticks(WIND_BOUNDS_MS)
    cbar.ax.tick_params(labelsize=8)

    valid_time = pd.Timestamp(ds["valid_time"].values).strftime(
        "%Y-%m-%d %H:%M UTC",
    )
    fig.suptitle(f"10 m wind [m/s] — valid {valid_time}", fontsize=13)
    apply_footer(fig, data_source="ECMWF Open Data", run_id=run_id)

    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=dpi, bbox_inches="tight")
    return buf.getvalue()
