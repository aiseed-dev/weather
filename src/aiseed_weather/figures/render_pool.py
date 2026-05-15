# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Yasuhiro / AIseed

"""Multi-process render pool for synoptic charts.

matplotlib + cartopy is CPU-bound and not thread-safe at the pyplot
level, so we fan out renders across worker processes. Each worker
keeps its own per-region Figure cache (msl_chart._BASEMAP_CACHE);
after the first frame in a region "warms" that cache, subsequent
frames render at ~3s each.

Coordinated via asyncio: ``render_msl_in_pool`` returns a coroutine
that resolves to PNG bytes; the executor handles the IPC. With N
worker processes, throughput is roughly N× the sequential rate
(minus the cold-start cost of importing cartopy / matplotlib in
each worker, which is ~1s per worker but only once at first use).

Why processes, not threads
--------------------------
matplotlib's pyplot keeps global state, contourpy holds C++ state,
and cfgrib's eccodes binding releases the GIL only for parts of the
read path. True parallelism requires separate Python interpreters.

Why 'spawn' context
-------------------
Default 'fork' on Linux would inherit the main process state (Flet,
xarray, possibly half-initialised matplotlib). 'spawn' starts each
worker from scratch — slightly slower cold-start (~0.5s) but no
state inheritance footguns.
"""

from __future__ import annotations

import asyncio
import atexit
import logging
import multiprocessing as mp
import os
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

from aiseed_weather.figures.regions import Region

logger = logging.getLogger(__name__)


def _default_workers() -> int:
    """Leave one core free for the main UI / async loop."""
    n = os.cpu_count() or 2
    return max(1, n - 1)


_pool: ProcessPoolExecutor | None = None


def get_pool() -> ProcessPoolExecutor:
    """Lazy-init the process pool. Idempotent."""
    global _pool
    if _pool is None:
        ctx = mp.get_context("spawn")
        n = _default_workers()
        _pool = ProcessPoolExecutor(max_workers=n, mp_context=ctx)
        atexit.register(_pool.shutdown, wait=False)
        logger.info("Render pool started with %d workers (spawn)", n)
    return _pool


def shutdown_pool() -> None:
    """Stop the pool. Mostly for tests / explicit teardown."""
    global _pool
    if _pool is not None:
        _pool.shutdown(wait=False)
        _pool = None


def _worker_render(
    grib_path: str,
    region: Region,
    run_id: str,
    layer_key: str,
) -> bytes:
    """Worker entry point: decode GRIB + render → PNG bytes.

    Imports are deferred so each worker only pays the cartopy /
    matplotlib import cost once, lazily. Dispatch on layer_key picks
    the right renderer module. Adding a new layer = one elif here +
    a new figures/{layer}_chart.py file.
    """
    import xarray as xr

    ds = xr.open_dataset(grib_path, engine="cfgrib")
    try:
        if layer_key == "msl":
            from aiseed_weather.figures.msl_chart import render_msl
            return render_msl(ds, region=region, run_id=run_id)
        if layer_key == "t2m":
            from aiseed_weather.figures.t2m_chart import render_t2m
            return render_t2m(ds, region=region, run_id=run_id)
        if layer_key == "tp":
            from aiseed_weather.figures.tp_chart import render_tp
            return render_tp(ds, region=region, run_id=run_id)
        raise ValueError(f"No renderer wired for layer {layer_key!r}")
    finally:
        ds.close()


async def render_layer_in_pool(
    grib_path: Path,
    region: Region,
    run_id: str,
    layer_key: str = "msl",
) -> bytes:
    """Submit a render job to the pool and await the resulting PNG.

    The main asyncio loop is fully non-blocking — GRIB decode and the
    matplotlib render both happen in the worker process. layer_key
    decides which figures/*_chart.py module is invoked.
    """
    pool = get_pool()
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        pool,
        _worker_render,
        str(grib_path),
        region,
        run_id,
        layer_key,
    )


# Back-compat alias for any old callers that haven't been switched yet.
async def render_msl_in_pool(
    grib_path: Path, region: Region, run_id: str,
) -> bytes:
    return await render_layer_in_pool(grib_path, region, run_id, "msl")
