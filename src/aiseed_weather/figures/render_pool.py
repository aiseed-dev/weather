# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Yasuhiro / AIseed

"""Sync render dispatcher.

The fast-path renderers (``msl_chart.render_msl`` and, as they migrate,
``t2m_chart``/``tp_chart``/``wind_chart``) keep the per-frame budget
inside a single UI frame — a few hundred milliseconds at most, all
inside numpy / contourpy / PIL C code. At that scale a thread pool or
process pool is pure overhead: workers spend more time on spawn /
import / IPC than they do rendering.

This module is therefore a thin sync dispatcher. Callers invoke
``render_layer(...)`` and it blocks until PNG bytes come back. If a
specific renderer regresses to multi-second wall time, wrap that call
site in ``asyncio.to_thread`` at the call site — but do NOT add a
pool back here as the default.

Naming note: the historical ``render_layer_in_pool`` name is kept as
an async-shaped alias so existing call sites keep working during the
migration. It is no longer "in a pool"; it just awaits the sync call.
"""

from __future__ import annotations

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
    import xarray as xr  # local: keeps module import light

    ds = xr.open_dataset(grib_path, engine="cfgrib")
    msl_overlay_ds = None
    if msl_overlay_path:
        msl_overlay_ds = xr.open_dataset(msl_overlay_path, engine="cfgrib")
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
        if layer_key == "wind10m":
            from aiseed_weather.figures.wind_chart import render_wind
            return render_wind(
                ds, region=region, run_id=run_id,
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


async def render_layer_in_pool(
    grib_path: Path,
    region: "Region",
    run_id: str,
    layer_key: str = "msl",
    *,
    msl_overlay_path: Path | None = None,
) -> bytes:
    """Async-shaped alias around :func:`render_layer`.

    Kept because the existing async call sites (``await
    render_layer_in_pool(...)``) read naturally. The body is sync —
    no pool, no executor — but exposing it as a coroutine lets the
    callers stay as they were while the underlying engine swap is
    transparent.
    """
    return render_layer(
        grib_path, region, run_id, layer_key,
        msl_overlay_path=msl_overlay_path,
    )


# Back-compat alias for any old callers that haven't been switched yet.
async def render_msl_in_pool(
    grib_path: Path, region: "Region", run_id: str,
) -> bytes:
    return render_layer(grib_path, region, run_id, "msl")


def shutdown_pool() -> None:
    """No-op now that the pool is gone. Kept so any teardown caller
    (tests, atexit hooks) doesn't crash on the missing symbol."""
    return None
