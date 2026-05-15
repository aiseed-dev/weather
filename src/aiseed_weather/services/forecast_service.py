# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Yasuhiro / AIseed

"""Service for fetching ECMWF Open Data forecasts.

The data source (AWS / Azure / GCP / direct) is chosen by the user via
config.toml. This service reads that setting; it never defaults to a
source on its own.

Download strategy: **bulk HTTPS GET per (cycle, step)**
------------------------------------------------------
We used to drive ``ecmwf-opendata`` ``Client.retrieve`` with a per-kind
param list, which under the hood fetches an ``.index`` sidecar and
issues HTTP Range requests for just the matching GRIB messages. That
sounds efficient until you measure it from a Japan-routed network:
each Range RTT against the Frankfurt S3 bucket cost ~250 ms, so a
30-param ``sfc`` fetch took ~24 s, and a single step (sfc + pl + sol)
took ~72 s. See ``tests/test_download_bench.py`` for the timings.

The fix is to stop being clever. The Open Data file
``{date}{time}-{step}h-oper-fc.grib2`` already contains every
parameter at every advertised level for that step. The Google Cloud
mirror serves the whole file in ~1.3 s from JP (edge-cached at
storage.googleapis.com), so one HTTPS GET per step replaces three
Range-pipelined retrievals — three orders of magnitude in TTFB cost.
The 150 MB-per-step disk price (~12 GB for an 80-step extended run)
is acceptable; CPU-side cfgrib decoding is far faster than network
RTT, so we'd rather pay disk than wait on Frankfurt.

Cache layout: one file per ``(cycle, step)``, kind-agnostic.
``ForecastRequest.kind`` no longer affects the cached filename — it
only steers the decode-time filter. Three concurrent ``download``
calls for different kinds at the same step share one HTTPS GET via
the per-path lock map.

Implementation notes
--------------------
- "Latest run" is still resolved by probing the server via
  ``ecmwf-opendata`` ``Client.latest()`` — ECMWF's publication delay
  is variable, so any client-side heuristic is wrong some fraction of
  the time. The probe makes a few HEAD requests and is cheap.
- Decoding GRIB is CPU-bound; wrap with ``asyncio.to_thread`` in
  async methods.
- If the user has set forecast_source to NONE, instantiation raises
  so the UI can route to the historical-only flow.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import xarray as xr
from ecmwf.opendata import Client

from aiseed_weather.models.user_settings import (
    ForecastSource,
    UserSettings,
    resolved_data_dir,
)

logger = logging.getLogger(__name__)


# ecmwf-opendata's Client.retrieve() does not forward retry kwargs to the
# underlying multiurl downloader. multiurl's default is retry_after=120s
# / maximum_tries=500 — fine for a one-off archival fetch, catastrophic
# when sequentially preloading 65 animation frames: a single S3 "503 Slow
# Down" pauses everything for 2 minutes, and S3 is happy to issue several
# of those when we hammer one bucket prefix.
#
# Why this is non-trivial to patch: ecmwf.opendata.client does
#   from multiurl import download, robust
# and then calls robust(self.session.get)(index_url, ...) directly,
# using multiurl's module-level robust() with its default 500/120s. So
# patching multiurl.http.robust = our_func is ineffective — by the time
# we run, ecmwf.opendata.client.robust is already bound to the original
# function object.
#
# The fix: rewrite the function's __defaults__ tuple. All import paths
# (multiurl, multiurl.http, multiurl.downloader, ecmwf.opendata.client)
# point at the same function object, so changing its defaults takes
# effect everywhere at once. We still also patch HTTPDownloaderBase
# instances as belt-and-braces for the (slower) download path.
import sys


def _install_multiurl_backoff_patch() -> None:
    import multiurl.http as _mh
    if getattr(_mh, "_aiseed_patched", False):
        return

    # 1) Rewrite the module-level robust() defaults. This catches
    # ecmwf-opendata's direct robust(session.get)(url) calls.
    new_defaults = (10, (5, 60, 2), None)  # (maximum_tries, retry_after, mirrors)
    _mh.robust.__defaults__ = new_defaults

    # 2) Force instance-level retry attributes via wrapped __init__ so
    # any future code path that uses HTTPDownloaderBase.robust() also
    # gets the policy, regardless of what kwargs were originally passed.
    _orig_init = _mh.HTTPDownloaderBase.__init__

    def _patched_init(self, *args, **kwargs):
        _orig_init(self, *args, **kwargs)
        self.retry_after = (5, 60, 2)
        self.maximum_retries = 10

    _mh.HTTPDownloaderBase.__init__ = _patched_init
    _mh.HTTPDownloaderBase.retry_after = (5, 60, 2)
    _mh.HTTPDownloaderBase.maximum_retries = 10
    _mh._aiseed_patched = True

    print(
        "[aiseed] multiurl backoff patch installed: "
        f"robust.__defaults__={new_defaults}, "
        "HTTPDownloaderBase retry_after=(5, 60, 2) max_tries=10",
        file=sys.stderr, flush=True,
    )
    logger.warning(
        "Installed multiurl backoff patch (robust defaults + instance attrs)",
    )


def _verify_multiurl_patch() -> None:
    """Sanity-check the patch state right before we kick off a download."""
    import multiurl.http as _mh
    if not getattr(_mh, "_aiseed_patched", False):
        print(
            "[aiseed] WARNING: multiurl patch missing at download time, "
            "re-applying",
            file=sys.stderr, flush=True,
        )
        _install_multiurl_backoff_patch()


_install_multiurl_backoff_patch()


_CLIENT_SOURCE = {
    ForecastSource.ECMWF_AWS: "aws",
    ForecastSource.ECMWF_AZURE: "azure",
    ForecastSource.ECMWF_GCP: "google",
    ForecastSource.ECMWF_DIRECT: "ecmwf",
}


# Per-mirror base URL for the bulk oper-fc.grib2 file. Path layout
# under each base is identical:
#   {date}/{HH}z/ifs/0p25/oper/{date}{HH}0000-{step}h-oper-fc.grib2
# Empirically (see tests/test_download_bench.py) the Google mirror is
# 5-100x faster than AWS from JP-routed networks because GCS serves
# from an Asia-Pacific edge PoP. AWS lands on Frankfurt direct.
_BULK_BASE: dict[str, str] = {
    "google": "https://storage.googleapis.com/ecmwf-open-data",
    "aws":    "https://ecmwf-forecasts.s3.eu-central-1.amazonaws.com",
    "ecmwf":  "https://data.ecmwf.int/forecasts",
    "azure":  "https://ai4edataeuwest.blob.core.windows.net/ecmwf",
}


def _bulk_url(source: str, run_time: datetime, step_hours: int) -> str:
    base = _BULK_BASE.get(source, _BULK_BASE["google"])
    return (
        f"{base}/{run_time:%Y%m%d}/{run_time:%H}z/ifs/0p25/oper/"
        f"{run_time:%Y%m%d}{run_time:%H}0000-{step_hours}h-oper-fc.grib2"
    )


# Catalogue of ECMWF Open Data param names we fetch in one go per
# kind. Computed once from the catalog so adding an implemented field
# automatically pulls its variable into the next download.
def _params_for_kind(kind: str) -> list[str]:
    """Distinct ECMWF Open Data param short-names that need to land in
    the multi-band GRIB for this kind."""
    # Local import to avoid an import cycle (catalog imports nothing
    # from services).
    from aiseed_weather.products.catalog import FIELDS, Status

    out: set[str] = set()
    for f in FIELDS:
        if f.status != Status.IMPLEMENTED:
            continue
        if f.kind != kind:
            continue
        for p in f.ecmwf_param.split("/"):
            if p:
                out.add(p)
    return sorted(out)


def _levels_in_use(kind: str = "pl") -> list[int]:
    """Distinct level values referenced by IMPLEMENTED fields of this
    kind.

    For ``"pl"`` that's pressure levels in hPa; for ``"sol"`` it's the
    soil layer numbers 1..4. Either way the download fetches them all
    in one call so layer × level switches stay local."""
    from aiseed_weather.products.catalog import FIELDS, Status

    return sorted(
        {f.level for f in FIELDS
         if f.status == Status.IMPLEMENTED and f.kind == kind
         and f.level is not None}
    )


@dataclass(frozen=True)
class ForecastRequest:
    """One unit of GRIB download / cache work.

    A request is a *(cycle, step, kind)* triple. The service downloads
    a single multi-band GRIB per request, containing every IMPLEMENTED
    param in the catalog that matches this kind (and, for ``pl``,
    every level we render at). The catalog is the single source of
    truth for what goes in.
    """

    run_time: datetime  # UTC
    step_hours: int
    kind: str = "sfc"  # "sfc" or "pl"

    def filename_part(self) -> str:
        """Filesystem-safe identifier for this kind."""
        return self.kind


class ForecastDisabledError(RuntimeError):
    """Raised when the user has not chosen a forecast source."""


class ForecastService:
    def __init__(
        self,
        settings: UserSettings,
        *,
        override_source: str | None = None,
    ):
        """Build a service against an ECMWF Open Data mirror.

        ``override_source``, when set, takes precedence over
        ``settings.forecast_source``. It must be one of the strings
        ecmwf-opendata's ``Client(source=...)`` accepts: 'aws',
        'azure', 'google', or 'ecmwf'. The UI passes this when the user
        picks a non-default mirror in the catalog dialog.
        """
        if override_source is None and settings.forecast_source == ForecastSource.NONE:
            raise ForecastDisabledError(
                "Forecast source is not configured. User opted into historical-only mode."
            )
        if override_source is not None:
            client_source = override_source
        else:
            client_source = _CLIENT_SOURCE[settings.forecast_source]
        self._client = Client(source=client_source)
        self._cache_dir = resolved_data_dir(settings) / "ecmwf"
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        self.client_source = client_source  # for logging / display
        # Per-(cache-path) locks so concurrent download() calls for
        # different kinds at the same (cycle, step) collapse onto one
        # HTTPS GET. Allocated lazily.
        self._dl_locks: dict[Path, asyncio.Lock] = {}

    def _lock_for(self, path: Path) -> asyncio.Lock:
        lock = self._dl_locks.get(path)
        if lock is None:
            lock = self._dl_locks[path] = asyncio.Lock()
        return lock

    async def fetch(self, request: ForecastRequest, *, force: bool = False) -> xr.Dataset:
        """Download (if needed) and decode a single forecast field.

        With ``force=True`` the cached GRIB2 is re-downloaded even if it
        already exists on disk — used by the UI Refresh button.
        """
        path = await self.download(request, force=force)
        return await asyncio.to_thread(self._decode, path, request.kind)

    async def download(self, request: ForecastRequest, *, force: bool = False) -> Path:
        """Bulk-download the per-step GRIB to disk if not already
        cached. Idempotent and dedup-safe across kinds.

        Used by the background acquisition loop, which is decoupled
        from rendering: rendering happens later, on demand, against
        whatever is currently on disk for the active region/layer.
        Returns the cached path (whether freshly downloaded or
        already present).
        """
        path = self._cache_path(request)
        if not force and path.exists() and path.stat().st_size > 0:
            return path
        async with self._lock_for(path):
            # Re-check after acquiring: another coroutine may have
            # raced ahead and filled the cache while we were waiting.
            if force or not path.exists() or path.stat().st_size == 0:
                await asyncio.to_thread(self._download, request, path)
        return path

    async def decode(self, request: ForecastRequest) -> xr.Dataset:
        """Decode an already-cached GRIB. Raises FileNotFoundError if
        the file isn't on disk — caller should check is_cached first
        or download() before decoding."""
        path = self._cache_path(request)
        if not path.exists() or path.stat().st_size == 0:
            raise FileNotFoundError(
                f"No cached GRIB for {request}: {path}",
            )
        return await asyncio.to_thread(self._decode, path, request.kind)

    def is_cached(self, request: ForecastRequest) -> bool:
        path = self._cache_path(request)
        return path.exists() and path.stat().st_size > 0

    async def latest_run(self, *, step_hours: int, param: str) -> datetime:
        """Return the run datetime of the most recent publicly available run
        that contains the requested field. Probes the server.
        """
        run = await asyncio.to_thread(
            self._client.latest, type="fc", step=step_hours, param=param,
        )
        # ecmwf-opendata returns a naive UTC datetime; attach the timezone so
        # callers can format/compare without surprises.
        if run.tzinfo is None:
            run = run.replace(tzinfo=timezone.utc)
        logger.info("ECMWF latest run for step=%s param=%s: %s", step_hours, param, run)
        return run

    def _cache_path(self, r: ForecastRequest) -> Path:
        # One bulk file per (cycle, step), kind-agnostic. Hierarchical
        # layout so a single run gathers all its fields under one
        # directory and many runs don't crowd a single flat folder.
        run_dir = self._cache_dir / r.run_time.strftime("%Y%m%d") / r.run_time.strftime("%Hz")
        run_dir.mkdir(parents=True, exist_ok=True)
        return run_dir / f"{r.step_hours}h.grib2"

    def _download(self, r: ForecastRequest, target: Path) -> None:
        """One HTTPS GET of the bulk per-step file from the user's mirror.

        Written to a ``.part`` sidecar then renamed atomically, so a
        crash mid-download never leaves a half-cached file that
        ``is_cached`` would mistake for valid.
        """
        url = _bulk_url(self.client_source, r.run_time, r.step_hours)
        tmp = target.with_suffix(target.suffix + ".part")
        logger.info("ECMWF bulk GET %s → %s", url, target.name)
        req = urllib.request.Request(
            url, headers={"User-Agent": "aiseed-weather/1.0"},
        )
        with urllib.request.urlopen(req) as resp, tmp.open("wb") as f:
            shutil.copyfileobj(resp, f, 1 << 20)
        tmp.replace(target)

    def _decode(self, path: Path, kind: str = "sfc") -> xr.Dataset:
        return _decode_kind(path, kind)


def _decode_kind(path: Path, kind: str) -> xr.Dataset:
    """Open a bulk ECMWF Open Data GRIB and return the dataset that
    matches the requested kind.

    The bulk file is heterogeneous (msl at meanSea, 2t at
    heightAboveGround=2, gh at 13 isobaric levels, sot at 4 soil
    layers, …) so ``xr.open_dataset`` alone raises
    ``DatasetBuildError``. We use ``cfgrib.open_datasets`` to get one
    hypercube per ``typeOfLevel`` and then:

    * ``"pl"``  → the single hypercube on ``isobaricInhPa``
    * ``"sol"`` → the single hypercube on the soil-layer axis (cfgrib
      may name it ``soilLayer`` or ``depthBelowLandLayer`` depending
      on eccodes version, so we accept either)
    * ``"sfc"`` → merge every remaining hypercube. Each surface
      variable lives in exactly one hypercube; merging with
      ``compat="override"`` lets the renderer keep using
      ``ds["2t"]``-style access.
    """
    import cfgrib

    dss = cfgrib.open_datasets(str(path))

    if kind == "pl":
        for d in dss:
            if "isobaricInhPa" in d.dims:
                return d
        raise KeyError(f"no pressure-level data in {path}")

    if kind == "sol":
        for d in dss:
            if "soilLayer" in d.dims or "depthBelowLandLayer" in d.dims:
                return d
        raise KeyError(f"no soil data in {path}")

    # sfc: everything that is neither pressure-level nor soil
    sfc = [
        d for d in dss
        if "isobaricInhPa" not in d.dims
        and "soilLayer" not in d.dims
        and "depthBelowLandLayer" not in d.dims
    ]
    if not sfc:
        raise KeyError(f"no surface data in {path}")
    # Each hypercube carries its own scalar typeOfLevel coordinate,
    # which is what triggers the DatasetBuildError on a plain
    # open_dataset. compat='override' tells xr.merge that conflicting
    # non-dimension coords are OK to drop — we don't care which scalar
    # 'typeOfLevel' wins because every data_var still knows its own.
    merged = xr.merge(sfc, compat="override")
    return _restore_grib_shortnames(merged)


# cfgrib applies a "make the xarray name nicer" rewrite for surface
# height-above-ground parameters: it moves the height suffix to the
# right of the variable name, producing ``t2m`` for ECMWF's ``2t``,
# ``u10`` for ``10u``, ``fg10`` for ``10fg``, etc. The rest of this
# codebase, the catalog, and ECMWF documentation all use the
# GRIB-native names with the height on the left. Rather than chase
# the dual form into every renderer, we put the canonical name back
# at decode time. Adding (not replacing) keeps the cfgrib-style name
# also accessible, so existing code that probed both forms keeps
# working — there's nothing to migrate.
_GRIB_NATIVE_ALIASES: dict[str, str] = {
    # cfgrib name -> GRIB-native short name
    "t2m":   "2t",
    "d2m":   "2d",
    "u10":   "10u",
    "v10":   "10v",
    "fg10":  "10fg",
    "i10fg": "10fg",   # instantaneous form, same physical quantity
    "u100":  "100u",
    "v100":  "100v",
}


def _restore_grib_shortnames(ds: xr.Dataset) -> xr.Dataset:
    """Add GRIB-native aliases (``2t``, ``10u``, …) for any cfgrib
    auto-renamed surface variable.

    We add rather than rename so both forms resolve. Aliases share
    the underlying DataArray — no copy, no extra memory.
    """
    additions = {
        native: ds[cfg]
        for cfg, native in _GRIB_NATIVE_ALIASES.items()
        if cfg in ds.data_vars and native not in ds.data_vars
    }
    if additions:
        ds = ds.assign(**additions)
    return ds


def grib_cache_path(
    settings: UserSettings,
    run_time: datetime,
    step_hours: int,
    kind: str = "sfc",  # kept for API compat; bulk file is kind-agnostic
) -> Path:
    """Compute the on-disk cache path for one (cycle, step) bulk GRIB.

    Mirrors :meth:`ForecastService._cache_path` but is a free function
    so UI code can inspect cache state without going through the
    (potentially expensive, requires-network-ready settings) full
    service object. The ``kind`` argument is accepted for backward
    compatibility with the per-kind cache layout and is intentionally
    ignored — every kind shares the same bulk file.
    """
    del kind
    return (
        resolved_data_dir(settings) / "ecmwf"
        / run_time.strftime("%Y%m%d")
        / run_time.strftime("%Hz")
        / f"{step_hours}h.grib2"
    )


def is_grib_cached(
    settings: UserSettings,
    run_time: datetime,
    step_hours: int,
    kind: str = "sfc",
) -> bool:
    p = grib_cache_path(settings, run_time, step_hours, kind)
    return p.exists() and p.stat().st_size > 0


def decode_kind(path: Path, kind: str) -> xr.Dataset:
    """Public wrapper around :func:`_decode_kind` for code outside
    this module that opens cached bulk GRIBs directly (the render
    worker, mostly).
    """
    return _decode_kind(path, kind)


async def probe_cycle_complete(
    cycle_dt: datetime,
    last_step_h: int,
    *,
    timeout: float = 5.0,
) -> bool:
    """Quick HEAD against the cycle's last-step .index file on AWS S3.

    Returns True if that file exists on the public AWS mirror; False
    on 404, timeout, or any other transport error. We hit the AWS S3
    mirror (s3.eu-central-1.amazonaws.com) rather than data.ecmwf.int
    because the dissemination server returns 403 for unauthenticated
    HEAD requests, while the S3 mirror accepts them on the public
    bucket. The two are byte-identical so existence on S3 is also
    truth on dissemination.

    Because HRES publication is atomic per cycle, presence of the
    last step's .index file means every step in the cycle is
    available; absence means the cycle is not yet published.
    """
    import httpx

    stamp = cycle_dt.strftime("%Y%m%d%H%M%S")
    date_dir = cycle_dt.strftime("%Y%m%d")
    hh = cycle_dt.strftime("%Hz")
    url = (
        f"https://ecmwf-forecasts.s3.eu-central-1.amazonaws.com/"
        f"{date_dir}/{hh}/ifs/0p25/oper/"
        f"{stamp}-{last_step_h}h-oper-fc.index"
    )
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            r = await client.head(url, follow_redirects=True)
        return r.status_code == 200
    except Exception as e:
        logger.debug(
            "probe_cycle_complete(%s, %dh) failed: %s",
            cycle_dt, last_step_h, e,
        )
        return False
