# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Yasuhiro / AIseed

"""Open-Meteo Historical Weather (ERA5) client + monthly Parquet
archive.

Reads from ``/v1/archive`` (ERA5 reanalysis), appends to per-location,
per-month Parquet files under the user's data dir. Stats (mean, std,
quantiles, records) are NOT precomputed — Polars queries them
on-demand against the raw monthly files so any future statistic
(decadal trend, return-period extremes, etc.) is still available
without redownloading.

See docs/forecast-spec.md sections 'データソース' and '過去データの
取得戦略'.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Iterable

import httpx
import polars as pl

from aiseed_weather.models.point_location import (
    Location, location_safe_dirname,
)
from aiseed_weather.services.open_meteo_forecast import HOURLY_VARS

logger = logging.getLogger(__name__)


# Parquet schema. Datetime in UTC microseconds matches the forecast
# / ensemble frames so joins on timestamp work without casts.
HISTORICAL_SCHEMA: dict[str, pl.DataType] = {
    "timestamp": pl.Datetime("us", time_zone="UTC"),
    "year": pl.Int32,
    "month": pl.Int8,
    "day": pl.Int8,
    "hour": pl.Int8,
    "temperature_2m": pl.Float32,
    "precipitation": pl.Float32,
    "relative_humidity_2m": pl.Float32,
    "wind_speed_10m": pl.Float32,
    "cloud_cover": pl.Float32,
    "weather_code": pl.Int16,
}


_ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"


@dataclass(frozen=True)
class FetchPlan:
    """One API call's date span. Built by ``plan_initial_archive`` /
    ``plan_daily_update``; consumed by ``run_plan_async`` to actually
    make the calls and persist rows."""

    target_year: int
    start_date: date  # inclusive
    end_date: date    # inclusive


def archive_dir(data_dir: Path, location: Location) -> Path:
    return data_dir / "point_forecast" / "archive" / location_safe_dirname(
        location.name,
    )


def _monthly_path(dir_path: Path, year: int, month: int) -> Path:
    return dir_path / f"{year:04d}-{month:02d}.parquet"


async def fetch_archive_span(
    *,
    location: Location,
    start: date,
    end: date,
    client: httpx.AsyncClient,
    hourly_vars: Iterable[str] = HOURLY_VARS,
) -> pl.DataFrame:
    """Single Historical Weather API call. Returns a DataFrame with
    the persistence schema's columns (year/month/day/hour decomposed
    so the climatology group_by works without per-row datetime
    decomposition at query time)."""
    params: dict[str, str | float] = {
        "latitude": location.latitude,
        "longitude": location.longitude,
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
        "hourly": ",".join(hourly_vars),
        "timezone": "UTC",
    }
    logger.info(
        "Open-Meteo archive %s..%s @ (%.3f, %.3f)",
        start, end, location.latitude, location.longitude,
    )
    resp = await client.get(_ARCHIVE_URL, params=params, timeout=60.0)
    resp.raise_for_status()
    body = resp.json()
    hourly = body.get("hourly") or {}
    if "time" not in hourly:
        return pl.DataFrame(schema=HISTORICAL_SCHEMA)
    times = hourly["time"]
    cols: dict[str, list] = {"timestamp": times}
    for name in hourly_vars:
        cols[name] = hourly.get(name, [None] * len(times))
    df = (
        pl.DataFrame(cols)
        .with_columns(
            pl.col("timestamp")
            .str.to_datetime(time_unit="us", time_zone="UTC")
        )
        .with_columns([
            pl.col("timestamp").dt.year().cast(pl.Int32).alias("year"),
            pl.col("timestamp").dt.month().cast(pl.Int8).alias("month"),
            pl.col("timestamp").dt.day().cast(pl.Int8).alias("day"),
            pl.col("timestamp").dt.hour().cast(pl.Int8).alias("hour"),
        ])
        .select(list(HISTORICAL_SCHEMA.keys()))
    )
    # Cast each value column to the declared schema type so the on-
    # disk Parquet stays small and joins don't have dtype surprises.
    casts = [
        pl.col(name).cast(dtype, strict=False)
        for name, dtype in HISTORICAL_SCHEMA.items()
        if name != "timestamp"
    ]
    return df.with_columns(casts).sort("timestamp")


def append_to_monthly_parquet(
    archive_root: Path, df: pl.DataFrame,
) -> int:
    """Persist ``df`` rows into per-month Parquet files under
    ``archive_root``. Existing rows for the same (year, month, day,
    hour) keys are replaced — Polars ``unique`` keeps the new value
    if a row collides.

    Returns the number of rows written across all touched months.
    """
    if df.is_empty():
        return 0
    archive_root.mkdir(parents=True, exist_ok=True)
    written = 0
    # ``year`` and ``month`` are already columns in df; group on them
    # to figure out which monthly files need touching.
    grouped = df.group_by(["year", "month"], maintain_order=True)
    for (y, m), group in grouped:
        path = _monthly_path(archive_root, int(y), int(m))
        if path.exists():
            try:
                existing = pl.read_parquet(path)
                combined = pl.concat([existing, group], how="vertical_relaxed")
            except (OSError, pl.exceptions.ComputeError):
                logger.exception(
                    "Reading existing %s failed; overwriting", path,
                )
                combined = group
        else:
            combined = group
        # Dedupe by hour-of-the-month; keep last value (most recently
        # fetched wins) so re-running an update converges.
        combined = combined.unique(
            subset=["timestamp"], keep="last",
        ).sort("timestamp")
        combined.write_parquet(path)
        written += combined.height
    return written


def plan_initial_archive(
    *, today: date, years: int = 30, window_days: int = 7,
) -> list[FetchPlan]:
    """One ``FetchPlan`` per year, each spanning ``today ± window_days``
    in that historical year. Produces ``years`` API calls — the spec
    estimates 30 — and roughly 30 × 24 × (2*window_days + 1) rows of
    new climatology data per location.
    """
    out: list[FetchPlan] = []
    for years_ago in range(1, years + 1):
        year = today.year - years_ago
        # Anchor at the same month/day in the older year. Use a
        # fallback for Feb 29 anchors landing on a non-leap year.
        try:
            anchor = date(year, today.month, today.day)
        except ValueError:
            anchor = date(year, today.month, 28)
        out.append(FetchPlan(
            target_year=year,
            start_date=anchor - timedelta(days=window_days),
            end_date=anchor + timedelta(days=window_days),
        ))
    return out


def plan_daily_update(
    *, target_date: date, years: int = 30,
) -> list[FetchPlan]:
    """One ``FetchPlan`` per year for the single ``target_date`` in
    each historical year — used to top up after the initial archive
    has been built."""
    out: list[FetchPlan] = []
    for years_ago in range(1, years + 1):
        year = target_date.year - years_ago
        try:
            anchor = date(year, target_date.month, target_date.day)
        except ValueError:
            anchor = date(year, target_date.month, 28)
        out.append(FetchPlan(
            target_year=year,
            start_date=anchor,
            end_date=anchor,
        ))
    return out


async def run_plan_async(
    *,
    location: Location,
    plans: list[FetchPlan],
    data_dir: Path,
    client: httpx.AsyncClient,
) -> AsyncIterator[tuple[int, int]]:
    """Execute a plan list and yield (done, total) progress tuples.

    Async generator: the UI ``yield`` loop reads each (done, total)
    update and refreshes its progress bar. One yield per completed
    API call, so a 30-year initial archive emits 30 updates.
    """
    archive_root = archive_dir(data_dir, location)
    archive_root.mkdir(parents=True, exist_ok=True)
    total = len(plans)
    for done_idx, plan in enumerate(plans, start=1):
        df = await fetch_archive_span(
            location=location,
            start=plan.start_date,
            end=plan.end_date,
            client=client,
        )
        if not df.is_empty():
            append_to_monthly_parquet(archive_root, df)
        yield done_idx, total


def has_archive_for_day(
    data_dir: Path, location: Location, target_day: date,
    years: int = 30,
) -> bool:
    """Cheap check used to skip a daily update when the archive already
    has the full N-year span for that day. Only looks at the relevant
    monthly file; doesn't load the whole archive."""
    root = archive_dir(data_dir, location)
    path = _monthly_path(root, target_day.year, target_day.month)
    if not path.exists():
        # We could still have data in the older years — but if the
        # current month's file doesn't exist, the daily update is
        # cheap so just return False and let it run.
        pass
    # Look at every monthly file matching the target month and count
    # distinct years that contain that day.
    pattern = str(root / f"*-{target_day.month:02d}.parquet")
    try:
        df = pl.scan_parquet(pattern).filter(
            pl.col("day") == target_day.day,
        ).select("year").unique().collect()
    except (FileNotFoundError, pl.exceptions.ComputeError):
        return False
    return df.height >= years
