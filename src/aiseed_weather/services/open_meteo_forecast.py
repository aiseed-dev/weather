# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Yasuhiro / AIseed

"""Open-Meteo Forecast API client (main + JMA MSM reference).

This is one of three Open-Meteo clients in this package; the other two
are ``open_meteo_ensemble`` (51-member spread for the future window)
and ``open_meteo_archive`` (ERA5 reanalysis for the past, persisted to
Parquet). They are intentionally small modules — one purpose, no
shared state — because they make raw HTTPS calls with no API key and
no schema beyond the parameter list.

Open-Meteo is CC-BY-4.0; downstream figures must attribute it.
See docs/forecast-spec.md.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Iterable

import httpx
import polars as pl

logger = logging.getLogger(__name__)


# The full hourly variable list the spec asks for. Used by every
# Open-Meteo call (forecast / ensemble / archive) so the resulting
# DataFrames have the same columns regardless of source.
HOURLY_VARS: tuple[str, ...] = (
    "temperature_2m",
    "precipitation",
    "relative_humidity_2m",
    "wind_speed_10m",
    "cloud_cover",
    "weather_code",
)


# Public re-export of column names so plotting code can stay aligned
# with the API output without referencing the URL parameter strings.
COLS: tuple[str, ...] = (
    "timestamp",
    *HOURLY_VARS,
)


@dataclass(frozen=True)
class ForecastResult:
    """One forecast call's result. ``model`` is the Open-Meteo model
    string ('ecmwf_ifs', 'jma_msm', etc.) for downstream labelling."""

    model: str
    latitude: float
    longitude: float
    timezone: str
    df: pl.DataFrame


_FORECAST_URL = "https://api.open-meteo.com/v1/forecast"


def _hourly_to_polars(hourly: dict, hourly_vars: Iterable[str]) -> pl.DataFrame:
    """Convert Open-Meteo's ``hourly`` block (parallel arrays keyed by
    variable name) into a single Polars DataFrame indexed by
    ``timestamp`` in UTC microseconds.

    Open-Meteo returns ISO timestamps as strings; we cast to
    Datetime up-front so downstream Polars ops (filter, group_by,
    join) get a real temporal type.
    """
    if not hourly or "time" not in hourly:
        return pl.DataFrame(schema={"timestamp": pl.Datetime("us", time_zone="UTC")})
    times = hourly["time"]
    cols: dict[str, list] = {"timestamp": times}
    for name in hourly_vars:
        cols[name] = hourly.get(name, [None] * len(times))
    return (
        pl.DataFrame(cols)
        .with_columns(
            pl.col("timestamp")
            .str.to_datetime(time_unit="us", time_zone="UTC")
        )
        .sort("timestamp")
    )


async def fetch_forecast(
    *,
    latitude: float,
    longitude: float,
    client: httpx.AsyncClient,
    model: str = "ecmwf_ifs",
    past_days: int = 3,
    forecast_days: int = 15,
    hourly_vars: Iterable[str] = HOURLY_VARS,
) -> ForecastResult:
    """Single Forecast API call.

    Open-Meteo's ``/v1/forecast`` accepts a ``models`` parameter so the
    same endpoint serves both the main ECMWF run and the JMA MSM
    reference run. The caller picks which by passing ``model``.
    Returns parsed hourly data as a Polars DataFrame.

    Errors are not caught here — Open-Meteo's HTTP-level failures and
    network errors bubble to the caller, which is closer to the UI
    state and can decide whether to retry, show an error banner, or
    fall back to last-known values.
    """
    params: dict[str, str | float | int] = {
        "latitude": latitude,
        "longitude": longitude,
        "hourly": ",".join(hourly_vars),
        "models": model,
        "past_days": past_days,
        "forecast_days": forecast_days,
        "timezone": "UTC",
    }
    logger.info(
        "Open-Meteo forecast %s @ (%.3f, %.3f) past=%d forecast=%d",
        model, latitude, longitude, past_days, forecast_days,
    )
    resp = await client.get(_FORECAST_URL, params=params, timeout=30.0)
    resp.raise_for_status()
    body = resp.json()
    return ForecastResult(
        model=model,
        latitude=float(body.get("latitude", latitude)),
        longitude=float(body.get("longitude", longitude)),
        timezone=str(body.get("timezone", "UTC")),
        df=_hourly_to_polars(body.get("hourly", {}), hourly_vars),
    )
