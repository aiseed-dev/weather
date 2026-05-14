# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Yasuhiro / AIseed

"""Service for fetching ECMWF Open Data forecasts.

The data source (AWS / Azure / GCP / direct) is chosen by the user at first
run and stored in user_settings. This service reads that setting; it never
defaults to a source on its own.

Implementation notes:
- Open Data is available 6 hours after each run; run_selector enforces this.
- Decoding GRIB is CPU-bound; wrap with asyncio.to_thread in async methods.
- If the user has set forecast_source to NONE, instantiation raises so the
  UI can route to the historical-only flow.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import xarray as xr
from ecmwf.opendata import Client
from platformdirs import user_cache_dir

from aiseed_weather.models.user_settings import ForecastSource, UserSettings


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
        self._cache_dir = Path(user_cache_dir("aiseed-weather")) / "grib"
        self._cache_dir.mkdir(parents=True, exist_ok=True)

    async def fetch(self, request: ForecastRequest) -> xr.Dataset:
        """Download (if needed) and decode a single forecast field."""
        path = self._cache_path(request)
        if not path.exists() or path.stat().st_size == 0:
            await asyncio.to_thread(self._download, request, path)
        return await asyncio.to_thread(self._decode, path)

    def _cache_path(self, r: ForecastRequest) -> Path:
        stamp = r.run_time.strftime("%Y%m%d_%Hz")
        return self._cache_dir / f"{stamp}_{r.step_hours}h_{r.param}.grib2"

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
