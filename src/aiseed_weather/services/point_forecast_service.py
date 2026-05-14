# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Yasuhiro / AIseed

"""Service for point-based weather forecasts via Open-Meteo.

This is a supporting feature, not the project's core. It is enabled only if
the user opted into Open-Meteo at first run. The main map view is the ECMWF
grid workflow (see forecast_service.py).

Implementation notes:
- Uses httpx directly rather than the openmeteo-requests client.
  The official client adds FlatBuffer decoding and request-cache integration,
  but for our use case (occasional fetches, cached via our own logic) plain
  JSON over httpx is simpler and has fewer dependencies.
- Methods are async; httpx supports async natively.
- Cache is implemented at this layer (file-based, 1 hour window).
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import httpx
import numpy as np

from aiseed_weather.models.user_settings import (
    PointForecastSource,
    UserSettings,
    resolved_data_dir,
)


FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
ECMWF_URL = "https://api.open-meteo.com/v1/ecmwf"
ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"

CACHE_WINDOW_SECONDS = 3600  # Open-Meteo updates forecasts hourly upstream


class PointForecastDisabledError(RuntimeError):
    """Raised when the user has not opted into a point-forecast source."""


@dataclass(frozen=True)
class PointForecast:
    latitude: float
    longitude: float
    timezone: str
    hourly_times: np.ndarray
    # variable name -> hourly values
    hourly: dict[str, np.ndarray]


# Default hourly variables for the "full set" point view.
# Documented here so the index→variable mapping is never a guess.
DEFAULT_HOURLY = (
    "temperature_2m",
    "relative_humidity_2m",
    "dew_point_2m",
    "apparent_temperature",
    "precipitation",
    "rain",
    "snowfall",
    "pressure_msl",
    "cloud_cover",
    "wind_speed_10m",
    "wind_direction_10m",
    "wind_gusts_10m",
    "shortwave_radiation",
    "soil_temperature_0cm",
    "soil_moisture_0_to_1cm",
)


class PointForecastService:
    def __init__(self, settings: UserSettings):
        if settings.point_source == PointForecastSource.NONE:
            raise PointForecastDisabledError(
                "Point forecast source is not configured."
            )
        self._cache_dir = resolved_data_dir(settings) / "openmeteo"
        self._cache_dir.mkdir(parents=True, exist_ok=True)

    async def fetch(
        self,
        latitude: float,
        longitude: float,
        variables: tuple[str, ...] = DEFAULT_HOURLY,
        forecast_days: int = 7,
        prefer_ecmwf: bool = False,
        force: bool = False,
    ) -> PointForecast:
        cache_path = self._cache_path(latitude, longitude, variables, forecast_days, prefer_ecmwf)
        if not force and self._cache_is_fresh(cache_path):
            return self._load_from_cache(cache_path, variables)

        params = {
            "latitude": latitude,
            "longitude": longitude,
            "hourly": ",".join(variables),
            "timezone": "auto",
            "forecast_days": forecast_days,
        }
        url = ECMWF_URL if prefer_ecmwf else FORECAST_URL

        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.get(url, params=params)
            response.raise_for_status()
            data = response.json()

        cache_path.write_text(json.dumps(data), encoding="utf-8")
        return self._decode(data, variables)

    def _cache_path(
        self,
        lat: float, lon: float,
        variables: tuple[str, ...],
        days: int,
        prefer_ecmwf: bool,
    ) -> Path:
        # Encode request parameters in the filename so different requests
        # never share a cache entry.
        var_hash = abs(hash(variables)) % (10 ** 8)
        model = "ecmwf" if prefer_ecmwf else "best"
        return self._cache_dir / f"{lat:.4f}_{lon:.4f}_{model}_{days}d_{var_hash}.json"

    def _cache_is_fresh(self, path: Path) -> bool:
        if not path.exists():
            return False
        age = time.time() - path.stat().st_mtime
        return age < CACHE_WINDOW_SECONDS

    def _load_from_cache(self, path: Path, variables: tuple[str, ...]) -> PointForecast:
        data = json.loads(path.read_text(encoding="utf-8"))
        return self._decode(data, variables)

    def _decode(self, data: dict, variables: tuple[str, ...]) -> PointForecast:
        hourly = data["hourly"]
        # Open-Meteo returns ISO 8601 strings; convert to numpy datetime64 for ease of use.
        times = np.array(hourly["time"], dtype="datetime64[s]")
        decoded = {name: np.asarray(hourly[name], dtype=float) for name in variables}
        return PointForecast(
            latitude=float(data["latitude"]),
            longitude=float(data["longitude"]),
            timezone=data.get("timezone", "UTC"),
            hourly_times=times,
            hourly=decoded,
        )