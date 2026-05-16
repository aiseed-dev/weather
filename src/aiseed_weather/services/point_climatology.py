# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Yasuhiro / AIseed

"""Compute historical statistics from the per-location Parquet archive.

The archive stores raw hourly rows (see ``open_meteo_archive``).
This module exposes two read-only views the UI needs:

  * ``hourly_climatology(location, month, day)`` — for a given
    calendar day, the per-hour mean / std / quantiles across all
    years in the archive. Drives the gray-band overlay on the
    forecast time series.
  * ``hourly_records(location, month, day)`` — historical extremes
    (min, max) for the same calendar day, for optional 'historical
    record' callouts.

Stats are computed against the raw Parquet files via Polars'
``scan_parquet`` lazy interface, so disk I/O is restricted to the
month's slice and the column subset the caller actually needs.

See docs/forecast-spec.md section '統計と不確実性の計算'.
"""

from __future__ import annotations

import logging
from pathlib import Path

import polars as pl

from aiseed_weather.models.point_location import Location
from aiseed_weather.services.open_meteo_archive import archive_dir

logger = logging.getLogger(__name__)


# Variables we compute stats for. Subset of the archive schema —
# weather_code and similar categorical columns wouldn't make sense to
# average, so they're excluded.
_NUMERIC_VARS: tuple[str, ...] = (
    "temperature_2m",
    "precipitation",
    "relative_humidity_2m",
    "wind_speed_10m",
    "cloud_cover",
)


def _scan_month(root: Path, month: int) -> pl.LazyFrame | None:
    """Lazy-scan every Parquet file for the given month-of-year across
    all years in the archive. Returns ``None`` if no file exists yet
    (e.g. before the initial archive build has completed)."""
    pattern = str(root / f"*-{month:02d}.parquet")
    try:
        return pl.scan_parquet(pattern)
    except FileNotFoundError:
        return None


def hourly_climatology(
    data_dir: Path,
    location: Location,
    month: int,
    day: int,
) -> pl.DataFrame:
    """Per-hour stats for one calendar day across all archive years.

    Returns a DataFrame indexed by ``hour`` (0..23) with mean / std /
    median / p25 / p75 / min / max / sample_count for every numeric
    variable. Each variable's stats live in their own columns named
    ``<var>_<stat>`` (e.g. ``temperature_2m_mean``).
    """
    root = archive_dir(data_dir, location)
    lf = _scan_month(root, month)
    if lf is None:
        return pl.DataFrame(schema={"hour": pl.Int8})

    aggs: list[pl.Expr] = []
    for var in _NUMERIC_VARS:
        aggs.extend([
            pl.col(var).mean().alias(f"{var}_mean"),
            pl.col(var).std().alias(f"{var}_std"),
            pl.col(var).median().alias(f"{var}_median"),
            pl.col(var).quantile(0.25).alias(f"{var}_p25"),
            pl.col(var).quantile(0.75).alias(f"{var}_p75"),
            pl.col(var).min().alias(f"{var}_min"),
            pl.col(var).max().alias(f"{var}_max"),
        ])
    aggs.append(pl.len().alias("sample_count"))

    return (
        lf.filter(pl.col("day") == day)
        .group_by("hour")
        .agg(aggs)
        .sort("hour")
        .collect()
    )


def hourly_records(
    data_dir: Path,
    location: Location,
    month: int,
    day: int,
) -> pl.DataFrame:
    """All-time records (min/max + year of occurrence) per hour.

    Useful for callouts like "the forecast is within 0.5 °C of the
    30-year record for this hour". Returns one row per hour with
    columns ``temp_record_high``, ``temp_record_high_year``, and the
    same for low plus precipitation max.
    """
    root = archive_dir(data_dir, location)
    lf = _scan_month(root, month)
    if lf is None:
        return pl.DataFrame(schema={"hour": pl.Int8})

    # Polars supports argmax-style "year of max" by joining the
    # per-hour max value back against the original frame. Doing it
    # in a single .agg() keeps the operation lazy + small.
    daily = lf.filter(pl.col("day") == day).collect()
    if daily.is_empty():
        return pl.DataFrame(schema={"hour": pl.Int8})

    record_frames: list[pl.DataFrame] = []
    for hour, group in daily.group_by("hour", maintain_order=True):
        hi_idx = group["temperature_2m"].arg_max()
        lo_idx = group["temperature_2m"].arg_min()
        prcp_idx = group["precipitation"].arg_max()
        row = {
            "hour": int(hour[0] if isinstance(hour, tuple) else hour),
            "temp_record_high": (
                float(group["temperature_2m"][hi_idx])
                if hi_idx is not None else None
            ),
            "temp_record_high_year": (
                int(group["year"][hi_idx])
                if hi_idx is not None else None
            ),
            "temp_record_low": (
                float(group["temperature_2m"][lo_idx])
                if lo_idx is not None else None
            ),
            "temp_record_low_year": (
                int(group["year"][lo_idx])
                if lo_idx is not None else None
            ),
            "precip_record_max": (
                float(group["precipitation"][prcp_idx])
                if prcp_idx is not None else None
            ),
            "precip_record_max_year": (
                int(group["year"][prcp_idx])
                if prcp_idx is not None else None
            ),
        }
        record_frames.append(pl.DataFrame([row]))
    return pl.concat(record_frames, how="vertical").sort("hour")


def join_forecast_with_climatology(
    forecast_df: pl.DataFrame,
    data_dir: Path,
    location: Location,
) -> pl.DataFrame:
    """Attach ``temperature_2m_mean / std / p25 / p75`` (and similar
    for every numeric variable) to each forecast row by matching
    (month, day, hour).

    The result drives the chart: each timestamp carries its forecast
    value AND the climatological band for the same calendar hour. UI
    code can compute anomaly / Z-score from the joined columns
    without re-querying the archive.
    """
    if forecast_df.is_empty():
        return forecast_df

    # Decompose timestamp → month/day/hour so we can join on them.
    enriched = forecast_df.with_columns([
        pl.col("timestamp").dt.month().cast(pl.Int8).alias("month"),
        pl.col("timestamp").dt.day().cast(pl.Int8).alias("day"),
        pl.col("timestamp").dt.hour().cast(pl.Int8).alias("hour"),
    ])

    # Group forecast rows by (month, day) so we only run one
    # climatology query per unique calendar day in the window. Two
    # weeks of forecast = ~15 unique (month, day) combinations.
    unique_days = (
        enriched.select(["month", "day"]).unique().sort(["month", "day"])
    )

    clim_frames: list[pl.DataFrame] = []
    for row in unique_days.iter_rows(named=True):
        clim = hourly_climatology(
            data_dir, location, int(row["month"]), int(row["day"]),
        )
        if clim.is_empty():
            continue
        clim = clim.with_columns([
            pl.lit(int(row["month"]), dtype=pl.Int8).alias("month"),
            pl.lit(int(row["day"]), dtype=pl.Int8).alias("day"),
        ])
        clim_frames.append(clim)

    if not clim_frames:
        return enriched

    clim_all = pl.concat(clim_frames, how="vertical_relaxed")
    return enriched.join(
        clim_all, on=["month", "day", "hour"], how="left",
    )
