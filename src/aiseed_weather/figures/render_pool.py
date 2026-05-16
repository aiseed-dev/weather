# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Yasuhiro / AIseed

"""Render dispatcher — sync core, async wrapper that offloads to a thread.

The fast-path renderers keep the per-frame budget inside a single UI
frame — a few hundred milliseconds at most, all inside numpy /
contourpy / PIL C code. At that scale a process pool is pure
overhead: workers spend more time on spawn / import / IPC than they
do rendering. We don't have one.

But we DO offload to a worker thread via ``asyncio.to_thread``. cfgrib
(eccodes), numpy, and PIL all release the GIL during their native
sections, so the offload genuinely yields the event loop to the UI
during a render — keeping the app responsive when an animation
preload pumps dozens of renders back-to-back.

``render_layer`` is the sync core for callers that already have a
worker thread of their own (CLI scripts, tests). ``render_layer_async``
is the async entry point UI code should use.

Naming note: the historical ``render_layer_in_pool`` name is kept as
an alias that DOES now offload — the previous body was a sync call
in async clothing, which lied to ``await`` and froze the UI. Fixed.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import xarray as xr

    from aiseed_weather.figures.regions import Region

logger = logging.getLogger(__name__)


def render_layer(
    grib_path: Path,
    region: "Region",
    run_id: str,
    layer_key: str = "msl",
    *,
    msl_overlay_path: Path | None = None,
) -> bytes:
    """Decode GRIB(s) + render → PNG bytes.

    Sync. Returns when the render is complete. Caller is responsible
    for invoking from a context that tolerates the blocking call —
    fast renderers (msl) take ~200 ms which is fine on the event
    loop; slower ones (t2m/tp/wind still on matplotlib) freeze the UI
    until migrated.
    """
    # The cached file is the bulk per-step oper-fc.grib2, which mixes
    # surface / pressure / soil hypercubes. A plain xr.open_dataset
    # raises on it. Route through decode_kind, which picks (and for
    # surface, merges) the right subset based on layer_key's kind.
    from aiseed_weather.products.catalog import field_by_key
    from aiseed_weather.services.forecast_service import decode_kind

    try:
        kind = field_by_key(layer_key).kind
    except KeyError:
        # Unknown layer key — fall back to surface so msl-like layers
        # still render rather than erroring out before the renderer
        # gets a chance to produce a 'no data' placeholder.
        kind = "sfc"

    ds = decode_kind(grib_path, kind)
    # MSL overlay is always surface.
    msl_overlay_ds = decode_kind(msl_overlay_path, "sfc") if msl_overlay_path else None
    try:
        if layer_key == "msl":
            from aiseed_weather.figures.msl_chart import render_msl
            return render_msl(ds, region=region, run_id=run_id)
        if layer_key == "t2m":
            from aiseed_weather.figures.t2m_chart import render_t2m
            return render_t2m(
                ds, region=region, run_id=run_id,
                msl_overlay_ds=msl_overlay_ds,
            )
        if layer_key == "tp":
            from aiseed_weather.figures.tp_chart import render_tp
            return render_tp(
                ds, region=region, run_id=run_id,
                msl_overlay_ds=msl_overlay_ds,
            )
        if layer_key.startswith("wind"):
            # wind10m → surface 10 m; wind100m → surface 100 m;
            # wind250 / wind500 / ... → pressure-level wind at that
            # hPa. The wind renderer reads u and v from the multi-
            # band GRIB and draws speed shading + direction arrows at
            # any level.
            from aiseed_weather.figures.wind_chart import render_wind
            from aiseed_weather.products.catalog import field_by_key
            try:
                fld = field_by_key(layer_key)
                level = fld.level
                # ecmwf_param is "<u>/<v>"; the GRIB short names from
                # cfgrib can be either ECMWF-style ("10u", "100u") or
                # xarray-friendly ("u10", "u100"), so we try both.
                u_p, v_p = fld.ecmwf_param.split("/")
                _SURFACE_U = {
                    "10u":  ("10u", "u10"),
                    "100u": ("100u", "u100"),
                }
                _SURFACE_V = {
                    "10v":  ("10v", "v10"),
                    "100v": ("100v", "v100"),
                }
                if level is None:
                    u_names = _SURFACE_U.get(u_p, (u_p,))
                    v_names = _SURFACE_V.get(v_p, (v_p,))
                else:
                    u_names = ("u",)
                    v_names = ("v",)
            except (KeyError, ValueError):
                level = None
                u_names = ("10u", "u10")
                v_names = ("10v", "v10")
            return render_wind(
                ds, region=region, run_id=run_id, level=level,
                u_names=u_names, v_names=v_names, layer_key=layer_key,
                msl_overlay_ds=msl_overlay_ds,
            )
        # Generic scalar layers (dewpoint, snow depth, total cloud
        # cover, skin temperature, ...). Each one is a config entry
        # in _scalar_chart.CONFIGS — no per-layer module needed.
        from aiseed_weather.figures._scalar_chart import CONFIGS, render_scalar
        if layer_key in CONFIGS:
            return render_scalar(
                ds, region=region, run_id=run_id,
                config=CONFIGS[layer_key],
            )
        raise ValueError(f"No renderer wired for layer {layer_key!r}")
    finally:
        ds.close()
        if msl_overlay_ds is not None:
            msl_overlay_ds.close()


async def render_layer_async(
    grib_path: Path,
    region: "Region",
    run_id: str,
    layer_key: str = "msl",
    *,
    msl_overlay_path: Path | None = None,
) -> bytes:
    """Async entry point that offloads the render to a worker thread.

    Use from UI code so the event loop stays responsive while a chart
    renders. The work runs under ``asyncio.to_thread``; cfgrib, numpy
    and PIL release the GIL during their native sections, so the UI
    actually gets time to repaint between frames.

    The thread-pool dispatch overhead is ~0.1 ms — negligible next to
    the render itself (10-50 ms for the fast-path layered renderer,
    longer for any not-yet-migrated matplotlib path).
    """
    return await asyncio.to_thread(
        render_layer,
        grib_path, region, run_id, layer_key,
        msl_overlay_path=msl_overlay_path,
    )


# Back-compat alias. The pre-fix version of this name was async-
# shaped but ran the heavy work synchronously — see the module
# docstring for the lie that caused. This alias now does the right
# thing (offload via to_thread) so call sites keep working.
render_layer_in_pool = render_layer_async


async def render_msl_in_pool(
    grib_path: Path, region: "Region", run_id: str,
) -> bytes:
    return await asyncio.to_thread(
        render_layer, grib_path, region, run_id, "msl",
    )


def shutdown_pool() -> None:
    """No-op now that the pool is gone. Kept so any teardown caller
    (tests, atexit hooks) doesn't crash on the missing symbol."""
    return None
