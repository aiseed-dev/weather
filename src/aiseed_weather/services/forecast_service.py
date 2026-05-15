# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Yasuhiro / AIseed

"""Service for fetching ECMWF Open Data forecasts.

The data source (AWS / Azure / GCP / direct) is chosen by the user via
config.toml. This service reads that setting; it never defaults to a
source on its own.

Implementation notes:
- "Latest run" is resolved by probing the server via Client.latest() —
  ECMWF's publication delay is variable, so any client-side heuristic is
  wrong some fraction of the time. The probe makes a few HEAD requests
  and is cheap.
- Decoding GRIB is CPU-bound; wrap with asyncio.to_thread in async methods.
- If the user has set forecast_source to NONE, instantiation raises so the
  UI can route to the historical-only flow.
"""

from __future__ import annotations

import asyncio
import logging
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


@dataclass(frozen=True)
class ForecastRequest:
    run_time: datetime  # UTC
    step_hours: int
    param: str          # ECMWF short name: "t2m", "msl", "u10", "v10", "tp", "gh"


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

    async def fetch(self, request: ForecastRequest, *, force: bool = False) -> xr.Dataset:
        """Download (if needed) and decode a single forecast field.

        With ``force=True`` the cached GRIB2 is re-downloaded even if it
        already exists on disk — used by the UI Refresh button.
        """
        path = self._cache_path(request)
        if force or not path.exists() or path.stat().st_size == 0:
            await asyncio.to_thread(self._download, request, path)
        return await asyncio.to_thread(self._decode, path)

    async def download(self, request: ForecastRequest, *, force: bool = False) -> Path:
        """Just download the GRIB to disk. No decode, no render.

        Used by the background acquisition loop, which is decoupled
        from rendering: rendering happens later, on demand, against
        whatever is currently on disk for the active region/layer.
        Returns the cached path (whether freshly downloaded or
        already present).
        """
        path = self._cache_path(request)
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
        return await asyncio.to_thread(self._decode, path)

    def is_cached(self, request: ForecastRequest) -> bool:
        path = self._cache_path(request)
        return path.exists() and path.stat().st_size > 0


def grib_cache_path(
    settings: UserSettings,
    run_time: datetime,
    step_hours: int,
    param: str = "msl",
) -> Path:
    """Compute the on-disk cache path for an ECMWF GRIB2 frame.

    Mirrors ForecastService._cache_path but is a free function so UI
    code can inspect cache state without going through the (potentially
    expensive, requires-network-ready settings) full service object.
    """
    return (
        resolved_data_dir(settings) / "ecmwf"
        / run_time.strftime("%Y%m%d")
        / run_time.strftime("%Hz")
        / f"{param}_{step_hours}h.grib2"
    )


def is_grib_cached(
    settings: UserSettings,
    run_time: datetime,
    step_hours: int,
    param: str = "msl",
) -> bool:
    p = grib_cache_path(settings, run_time, step_hours, param)
    return p.exists() and p.stat().st_size > 0


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
        # Hierarchical layout so a single run gathers all its fields under
        # one directory and many runs don't crowd a single flat folder.
        run_dir = self._cache_dir / r.run_time.strftime("%Y%m%d") / r.run_time.strftime("%Hz")
        run_dir.mkdir(parents=True, exist_ok=True)
        return run_dir / f"{r.param}_{r.step_hours}h.grib2"

    def _download(self, r: ForecastRequest, target: Path) -> None:
        _verify_multiurl_patch()
        self._client.retrieve(
            type="fc",
            step=r.step_hours,
            param=r.param,
            date=r.run_time.strftime("%Y-%m-%d"),
            time=r.run_time.hour,
            target=str(target),
        )

    def _decode(self, path: Path) -> xr.Dataset:
        return xr.open_dataset(path, engine="cfgrib")
