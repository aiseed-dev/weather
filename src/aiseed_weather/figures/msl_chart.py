# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Yasuhiro / AIseed

"""Mean sea level pressure synoptic chart.

Renders MSL isobars at the 4 hPa interval (synoptic convention) on a chosen
projection and returns PNG bytes.

Performance note
----------------
Building a cartopy axes with coastlines + gridlines costs about 2 seconds
per call — and that work is identical for every frame of the same region.
We cache one Figure + Axes per Region key (module-level dict) and reuse
it across frames, removing only the per-frame artists (contours, clabels,
suptitle, footer) between savefigs. Per-frame work drops from ~5s to ~3s.

Thread safety
-------------
The cache and the figures inside it are protected by a module-level
RLock because matplotlib Figures aren't safe to mutate concurrently.
Renders go through asyncio.to_thread (worker pool) so they may overlap
in time; the lock serialises figure access. Within one process this is
fine; multi-process parallelism would have a per-process cache.
"""

from __future__ import annotations

import io
import threading

import cartopy.crs as ccrs
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

# region.key -> (Figure, Axes) cache. Each entry holds a fully-prepared
# basemap (projection set, coastlines drawn, gridlines drawn). Frames
# add per-frame artists, savefig, then remove them.
_BASEMAP_CACHE: dict[str, tuple[Figure, "ccrs.GeoAxes"]] = {}
_CACHE_LOCK = threading.RLock()


def _make_projection(name: str) -> ccrs.Projection:
    try:
        return _PROJECTIONS[name]()
    except KeyError as e:
        raise ValueError(
            f"Unknown projection '{name}'. Choose one of {sorted(_PROJECTIONS)}."
        ) from e


def _build_basemap(region: Region) -> tuple[Figure, "ccrs.GeoAxes"]:
    """Construct a fresh Figure + GeoAxes with terrain/grid drawn.

    Uses Figure() + FigureCanvasAgg directly (not pyplot) so the cache
    survives without polluting pyplot's global current-figure state.
    """
    fig = Figure(figsize=_FIG_SIZE)
    FigureCanvasAgg(fig)  # registers the canvas on fig.canvas
    ax = fig.add_subplot(1, 1, 1, projection=_make_projection(region.projection))
    if region.extent is None:
        ax.set_global()
    else:
        ax.set_extent(region.extent, crs=ccrs.PlateCarree())
    ax.coastlines(linewidth=0.6, color="#444444")
    ax.gridlines(draw_labels=False, linewidth=0.3, color="#888888", alpha=0.5)
    return fig, ax


def _get_basemap(region: Region) -> tuple[Figure, "ccrs.GeoAxes"]:
    with _CACHE_LOCK:
        cached = _BASEMAP_CACHE.get(region.key)
        if cached is not None and region.key != "custom":
            return cached
        # Custom regions get a fresh basemap each time (their bounds
        # may differ even with the same key); presets cache forever.
        fig_ax = _build_basemap(region)
        if region.key != "custom":
            _BASEMAP_CACHE[region.key] = fig_ax
        return fig_ax


def render_msl(
    ds: xr.Dataset,
    *,
    region: Region = GLOBAL,
    run_id: str,
    dpi: int = _DPI,
) -> bytes:
    """Render MSL isobars on the cached basemap and return PNG bytes."""
    msl_hpa = ds["msl"] / 100.0
    longitudes = ds["longitude"].values
    latitudes = ds["latitude"].values

    with _CACHE_LOCK:
        fig, ax = _get_basemap(region)

        # Per-frame artists: contours + clabels + suptitle + footer.
        # We collect them all so we can remove() at the end and keep
        # the basemap pristine for the next render.
        per_frame_artists = []

        # Isobars at 4 hPa, bold every 20 hPa (synoptic convention).
        levels_thin = np.arange(940, 1064, 4)
        levels_bold = np.arange(940, 1064, 20)
        cs_thin = ax.contour(
            longitudes, latitudes, msl_hpa,
            levels=levels_thin,
            transform=ccrs.PlateCarree(),
            colors="black", linewidths=0.5,
        )
        cs_bold = ax.contour(
            longitudes, latitudes, msl_hpa,
            levels=levels_bold,
            transform=ccrs.PlateCarree(),
            colors="black", linewidths=1.0,
        )
        clabels = ax.clabel(cs_thin, inline=True, fontsize=6, fmt="%d")

        per_frame_artists.append(cs_thin)
        per_frame_artists.append(cs_bold)
        per_frame_artists.extend(clabels)

        valid_time = pd.Timestamp(ds["valid_time"].values).strftime(
            "%Y-%m-%d %H:%M UTC",
        )
        title_artist = fig.suptitle(
            f"MSL [hPa] — valid {valid_time}", fontsize=13,
        )
        per_frame_artists.append(title_artist)

        footer_artist = apply_footer(
            fig, data_source="ECMWF Open Data", run_id=run_id,
        )
        per_frame_artists.append(footer_artist)

        try:
            buf = io.BytesIO()
            fig.savefig(buf, format="png", dpi=dpi, bbox_inches="tight")
            return buf.getvalue()
        finally:
            # Strip the per-frame artists so the next render starts
            # against a clean basemap. ContourSet.remove() drops every
            # collection; Text.remove() detaches from the figure.
            for art in per_frame_artists:
                try:
                    art.remove()
                except (AttributeError, ValueError, NotImplementedError):
                    # Some artist subclasses don't support remove()
                    # cleanly; leak rather than crash. Worst case the
                    # next frame has stale text overlap (visible in DPR
                    # but recoverable by region toggle).
                    pass


def clear_basemap_cache() -> None:
    """Drop all cached Figures (releases ~10MB each). Call this if
    region presets are edited at runtime, or for memory-pressure
    cleanup. The next render rebuilds the basemap as needed."""
    with _CACHE_LOCK:
        for fig, _ in _BASEMAP_CACHE.values():
            # Closing via Agg canvas releases native resources.
            fig.clear()
        _BASEMAP_CACHE.clear()
