# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Yasuhiro / AIseed

"""Open-Meteo Ensemble API client.

The ECMWF IFS ENS feed (51 members) returns one column per member per
variable. We pivot that into a long DataFrame (one row per
(timestamp, member, variable)) so the stats step can group_by
timestamp and aggregate without dealing with hundreds of column
names. Spread / quantiles for the chart are derived from this long
frame at use time.

See docs/forecast-spec.md.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Iterable

import httpx
import polars as pl

from aiseed_weather.services.open_meteo_forecast import HOURLY_VARS

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class EnsembleResult:
    """Long-format ensemble result: one row per (timestamp, member,
    variable) for easy ``group_by("timestamp").agg(...)`` to compute
    mean / spread / quantiles."""

    model: str
    latitude: float
    longitude: float
    df: pl.DataFrame  # columns: timestamp, member, variable, value


_ENSEMBLE_URL = "https://ensemble-api.open-meteo.com/v1/ensemble"


# Open-Meteo reports ensemble members as suffixes: 'temperature_2m'
# is the control run, 'temperature_2m_member01' .. 'temperature_2m_member50'
# are the 50 perturbed members. Capture the variable name + member
# index so we can pivot.
_MEMBER_RE = re.compile(
    r"^(?P<var>.+?)(?:_member(?P<idx>\d+))?$",
)


def _to_long_dataframe(
    hourly: dict, hourly_vars: Iterable[str],
) -> pl.DataFrame:
    """Pivot Open-Meteo's wide hourly response (one column per
    (variable, member)) into a long DataFrame.

    Output columns: timestamp (UTC), member (int 0 = control), variable
    (str), value (Float64). The long shape is one row per (time,
    member, variable) which matches what the stats step wants.
    """
    schema = {
        "timestamp": pl.Datetime("us", time_zone="UTC"),
        "member": pl.Int16,
        "variable": pl.Utf8,
        "value": pl.Float64,
    }
    if not hourly or "time" not in hourly:
        return pl.DataFrame(schema=schema)

    times = hourly["time"]
    wanted = set(hourly_vars)
    frames: list[pl.DataFrame] = []
    for key, values in hourly.items():
        if key == "time":
            continue
        m = _MEMBER_RE.match(key)
        if not m:
            continue
        var = m.group("var")
        if var not in wanted:
            continue
        member = int(m.group("idx")) if m.group("idx") else 0
        frames.append(pl.DataFrame({
            "timestamp": times,
            "member": [member] * len(times),
            "variable": [var] * len(times),
            "value": values,
        }))
    if not frames:
        return pl.DataFrame(schema=schema)
    return (
        pl.concat(frames, how="vertical")
        .with_columns(
            pl.col("timestamp")
            .str.to_datetime(time_unit="us", time_zone="UTC"),
            pl.col("member").cast(pl.Int16),
            pl.col("value").cast(pl.Float64),
        )
        .sort(["variable", "member", "timestamp"])
    )


async def fetch_ensemble(
    *,
    latitude: float,
    longitude: float,
    client: httpx.AsyncClient,
    model: str = "ecmwf_ifs025",
    forecast_days: int = 15,
    hourly_vars: Iterable[str] = HOURLY_VARS,
) -> EnsembleResult:
    """Fetch the 51-member ensemble for the future window.

    Open-Meteo's ensemble endpoint is rate-limited identically to the
    forecast one. A single call covers all members at all hours for
    the requested variables — no pagination needed.
    """
    params: dict[str, str | float | int] = {
        "latitude": latitude,
        "longitude": longitude,
        "hourly": ",".join(hourly_vars),
        "models": model,
        "forecast_days": forecast_days,
        "timezone": "UTC",
    }
    logger.info(
        "Open-Meteo ensemble %s @ (%.3f, %.3f) forecast=%d",
        model, latitude, longitude, forecast_days,
    )
    resp = await client.get(_ENSEMBLE_URL, params=params, timeout=60.0)
    resp.raise_for_status()
    body = resp.json()
    return EnsembleResult(
        model=model,
        latitude=float(body.get("latitude", latitude)),
        longitude=float(body.get("longitude", longitude)),
        df=_to_long_dataframe(body.get("hourly", {}), hourly_vars),
    )


def aggregate_to_quantiles(long_df: pl.DataFrame) -> pl.DataFrame:
    """Reduce a long-format ensemble frame to per-(timestamp, variable)
    statistics. Returns columns:
        timestamp, variable, mean, std, p10, p50, p90.
    """
    if long_df.is_empty():
        return pl.DataFrame(schema={
            "timestamp": pl.Datetime("us", time_zone="UTC"),
            "variable": pl.Utf8,
            "mean": pl.Float64,
            "std": pl.Float64,
            "p10": pl.Float64,
            "p50": pl.Float64,
            "p90": pl.Float64,
        })
    return (
        long_df
        .group_by(["timestamp", "variable"])
        .agg([
            pl.col("value").mean().alias("mean"),
            pl.col("value").std().alias("std"),
            pl.col("value").quantile(0.10).alias("p10"),
            pl.col("value").quantile(0.50).alias("p50"),
            pl.col("value").quantile(0.90).alias("p90"),
        ])
        .sort(["variable", "timestamp"])
    )
