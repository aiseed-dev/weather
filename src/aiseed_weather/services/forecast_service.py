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
# Patch multiurl.http.robust() so it uses exponential backoff
# (5s → 60s, ×2 per attempt) and caps retries at 10. Worst-case retry
# budget per frame is now ~6 min instead of >16 hours.
def _install_multiurl_backoff_patch() -> None:
    import multiurl.http as _mh
    if getattr(_mh, "_aiseed_patched", False):
        return
    _orig = _mh.robust

    def _patched(call, maximum_tries=500, retry_after=120, mirrors=None):
        if not isinstance(retry_after, (list, tuple)):
            retry_after = (5, 60, 2)
        if maximum_tries > 10:
            maximum_tries = 10
        return _orig(call, maximum_tries, retry_after, mirrors)

    _mh.robust = _patched
    _mh._aiseed_patched = True
    logger.info(
        "Patched multiurl.http.robust: retry_after=(5, 60, 2) max_tries=10"
    )


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
    def __init__(self, settings: UserSettings):
        if settings.forecast_source == ForecastSource.NONE:
            raise ForecastDisabledError(
                "Forecast source is not configured. User opted into historical-only mode."
            )
        client_source = _CLIENT_SOURCE[settings.forecast_source]
        self._client = Client(source=client_source)
        self._cache_dir = resolved_data_dir(settings) / "ecmwf"
        self._cache_dir.mkdir(parents=True, exist_ok=True)

    async def fetch(self, request: ForecastRequest, *, force: bool = False) -> xr.Dataset:
        """Download (if needed) and decode a single forecast field.

        With ``force=True`` the cached GRIB2 is re-downloaded even if it
        already exists on disk — used by the UI Refresh button.
        """
        path = self._cache_path(request)
        if force or not path.exists() or path.stat().st_size == 0:
            await asyncio.to_thread(self._download, request, path)
        return await asyncio.to_thread(self._decode, path)

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
        # Hierarchical layout so a single run gathers all its fields under
        # one directory and many runs don't crowd a single flat folder.
        run_dir = self._cache_dir / r.run_time.strftime("%Y%m%d") / r.run_time.strftime("%Hz")
        run_dir.mkdir(parents=True, exist_ok=True)
        return run_dir / f"{r.param}_{r.step_hours}h.grib2"

    def _download(self, r: ForecastRequest, target: Path) -> None:
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
