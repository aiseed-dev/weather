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
from datetime import date, timedelta
from pathlib import Path

import polars as pl

from aiseed_weather.models.point_location import Location
from aiseed_weather.services.open_meteo_archive import archive_dir

logger = logging.getLogger(__name__)


# Variables we compute stats for. Subset of the archive schema —
# weather_code and similar categorical columns wouldn't make sense to
# average, so they're excluded.
#
# **Precipitation is deliberately excluded.** A single typhoon or
# meso-convective event in one historical year dumps 100+ mm and
# completely dominates the per-day mean / std — the resulting
# "climatology" tells the analyst nothing useful about whether a
# given day's precip is anomalous. The hourly_records() function
# below still works for precipitation (showing actual extremes is
# informative), but the band overlay on the forecast chart is
# misleading and is therefore omitted. User decision; see the
# 2026-05-17 conversation.
_NUMERIC_VARS: tuple[str, ...] = (
    "temperature_2m",
    "relative_humidity_2m",
    "wind_speed_10m",
    "cloud_cover",
)


# Default rolling-window half-width for the per-hour climatology. A
# ±15-day window (31 days centred) matches the WMO convention noted
# in the climatology-analysis skill ("centered 31-day rolling mean")
# and pools ~930 obs / hour over a 30-year archive — robust against
# single-event outliers.
#
# The downside (adjacent forecast days share ~97 % of the pool, so
# the raw mean barely moves across the 15-day forecast and looks
# 'flat') is handled by the ``{var}_estimate`` column: each
# (month, day) is regressed independently against year, and the
# per-day slope variation makes the trend-projected estimate move
# day-to-day even when the unadjusted mean does not. The chart and
# daily summary display the estimate as the operational normal.
_DEFAULT_WINDOW_DAYS = 15


def _scan_month(root: Path, month: int) -> pl.LazyFrame | None:
    """Lazy-scan every Parquet file for the given month-of-year across
    all years in the archive. Returns ``None`` when no file exists
    yet for that month.

    Pre-check with ``Path.glob`` is necessary because
    ``pl.scan_parquet(pattern)`` accepts a glob string but only
    discovers it has zero matches at ``.collect()`` time — and then
    raises ``ComputeError`` ('expanded paths were empty'), not
    ``FileNotFoundError``, so a try/except around the scan call
    itself catches nothing useful. Resolving the glob ourselves and
    passing an explicit file list to scan_parquet makes the empty
    case deterministic: we return None here and the caller skips
    that month.
    """
    files = sorted(root.glob(f"*-{month:02d}.parquet"))
    if not files:
        return None
    return pl.scan_parquet([str(f) for f in files])


def _window_month_days(
    month: int, day: int, window_days: int,
) -> dict[int, list[int]]:
    """Enumerate (month → [days...]) the ±window_days neighbourhood
    of (month, day) covers.

    Uses 2001 (non-leap) as the reference year so Feb 29 doesn't
    appear in the window day list; archive rows from leap years
    still get pulled in (the filter is per-month-day, year-blind),
    they just round to Feb 28's window. Cross-year wrap (e.g. Dec 25
    window straddling into early January) is handled by iterating
    over the actual calendar days regardless of year boundary.
    """
    if month == 2 and day == 29:
        ref = date(2000, 2, 29)
    else:
        ref = date(2001, month, day)
    out: dict[int, set[int]] = {}
    for delta in range(-window_days, window_days + 1):
        d = ref + timedelta(days=delta)
        out.setdefault(d.month, set()).add(d.day)
    return {m: sorted(ds) for m, ds in out.items()}


def _trend_exprs(var: str, target_year: int) -> list[pl.Expr]:
    """Per-variable linear-regression expressions for use inside a
    ``.group_by(...).agg([...])`` call.

    Fits ``var ~ year`` across the ±15-day window's samples for one
    hour, giving:
      ``{var}_slope``     — change in the variable per year (e.g.
                            +0.04 °C/year for temperature warming)
      ``{var}_intercept`` — value at year 0 of the regression line
      ``{var}_estimate``  — slope × target_year + intercept, i.e.
                            'what the trend says the value should be
                            NOW' as opposed to the unadjusted 30-y
                            mean. Useful when climate change has
                            shifted the operational normal away from
                            the WMO 1991-2020 baseline.

    Null-safe: rows where either ``year`` or ``var`` is null are
    filtered out before the regression so a sparsely-populated
    archive doesn't return NaN coefficients.
    """
    x = pl.col("year").cast(pl.Float64)
    y = pl.col(var).cast(pl.Float64)
    valid = y.is_not_null() & x.is_not_null()
    x_f = x.filter(valid)
    y_f = y.filter(valid)
    mean_x = x_f.mean()
    mean_y = y_f.mean()
    # slope = Σ(x-x̄)(y-ȳ) / Σ(x-x̄)² — classical least-squares
    slope = (
        ((x_f - mean_x) * (y_f - mean_y)).sum()
        / ((x_f - mean_x) ** 2).sum()
    )
    intercept = mean_y - slope * mean_x
    estimate = intercept + slope * float(target_year)
    return [
        slope.alias(f"{var}_slope"),
        intercept.alias(f"{var}_intercept"),
        estimate.alias(f"{var}_estimate"),
    ]


def hourly_climatology(
    data_dir: Path,
    location: Location,
    month: int,
    day: int,
    window_days: int = _DEFAULT_WINDOW_DAYS,
    target_year: int | None = None,
) -> pl.DataFrame:
    """Per-hour stats for one calendar day, smoothed across a
    ±window_days neighbourhood.

    Returns a DataFrame indexed by ``hour`` (0..23) with mean / std /
    median / p25 / p75 / min / max / sample_count for every numeric
    variable, plus the linear-regression triple
    ``{var}_slope / _intercept / _estimate`` projecting each
    variable onto ``target_year`` (defaults to the current year).
    Each variable's stats live in their own columns named
    ``<var>_<stat>`` (e.g. ``temperature_2m_mean``,
    ``temperature_2m_estimate``).

    The window is centred on (month, day) and pools observations
    from ±``window_days`` calendar days at the same hour, across all
    archive years. With the default ±7-day half-width and a 30-year
    archive that's ~450 samples per hour — one warm spell in 2018
    stops dominating the May-17 13:00 mean while still leaving the
    seasonal trend visible across adjacent forecast days.
    """
    root = archive_dir(data_dir, location)
    months_to_days = _window_month_days(month, day, window_days)
    frames: list[pl.LazyFrame] = []
    for m, days in months_to_days.items():
        lf = _scan_month(root, m)
        if lf is None:
            continue
        frames.append(lf.filter(pl.col("day").is_in(days)))
    if not frames:
        return pl.DataFrame(schema={"hour": pl.Int8})

    combined = pl.concat(frames, how="vertical_relaxed")

    year_for_trend = target_year if target_year is not None else date.today().year
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
        aggs.extend(_trend_exprs(var, year_for_trend))
    aggs.append(pl.len().alias("sample_count"))

    return (
        combined
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


def daily_records(
    data_dir: Path,
    location: Location,
    month: int,
    day: int,
) -> dict[str, tuple[float, int]]:
    """All-time daily extremes on one specific (month, day) across
    every archive year.

    Returns a dict keyed by ``<variable>_<kind>`` where kind is one
    of ``high`` / ``low`` / ``wettest`` and the value is a
    ``(value, year)`` tuple. Empty when the archive has no data
    for that calendar day yet.

    Records are deliberately NOT smoothed across the ±15-day window —
    extremes are events tied to a specific date, not averages.
    The user wants to see 'the hottest May-17 ever recorded',
    not 'the average peak in the May-17 vicinity'.
    """
    # End-of-hour convention: archive rows from Open-Meteo are
    # end-of-hour aggregates (rain at timestamp 14:00 = the hour
    # 13-14h, 00:00 = previous day's last hour). A "May 17 daily
    # max" should bundle hours that fell during May 17 00..24 local
    # — under the raw storage that's timestamp ∈ (May 17 01:00,
    # ..., May 18 00:00). Pull both raw months in case day=last-of-
    # month wraps; filter on the shifted (-1 h) date.
    root = archive_dir(data_dir, location)
    target = date(2001, month, day) if not (month == 2 and day == 29) \
        else date(2000, 2, 29)
    next_day = target + timedelta(days=1)
    frames: list[pl.LazyFrame] = []
    for m in {target.month, next_day.month}:
        lf = _scan_month(root, m)
        if lf is not None:
            frames.append(lf)
    if not frames:
        return {}
    df = (
        pl.concat(frames, how="vertical_relaxed")
        .with_columns(
            pl.col("timestamp").dt.offset_by("-1h")
            .dt.month().cast(pl.Int8).alias("_d_month"),
            pl.col("timestamp").dt.offset_by("-1h")
            .dt.day().cast(pl.Int8).alias("_d_day"),
        )
        .filter(
            (pl.col("_d_month") == month)
            & (pl.col("_d_day") == day)
        )
        .collect()
    )
    if df.is_empty():
        return {}

    out: dict[str, tuple[float, int]] = {}
    record_vars = (
        "temperature_2m",
        "precipitation",
        "relative_humidity_2m",
        "wind_speed_10m",
        "cloud_cover",
    )
    for var in record_vars:
        if var not in df.columns:
            continue
        per_year = df.group_by("year", maintain_order=True).agg([
            pl.col(var).max().alias("daily_max"),
            pl.col(var).min().alias("daily_min"),
            pl.col(var).sum().alias("daily_sum"),
        ])
        if per_year.is_empty():
            continue
        years = per_year["year"].to_list()
        # arg_max / arg_min return None when the column is all-null;
        # guard against that with a truthiness check rather than a
        # raw int compare (None != int).
        i_max = per_year["daily_max"].arg_max()
        i_min = per_year["daily_min"].arg_min()
        i_sum = per_year["daily_sum"].arg_max()
        if i_max is not None:
            out[f"{var}_high"] = (
                float(per_year["daily_max"][i_max]),
                int(years[i_max]),
            )
        if i_min is not None:
            out[f"{var}_low"] = (
                float(per_year["daily_min"][i_min]),
                int(years[i_min]),
            )
        if i_sum is not None:
            out[f"{var}_wettest"] = (
                float(per_year["daily_sum"][i_sum]),
                int(years[i_sum]),
            )
    return out


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
        pl.col("timestamp").dt.year().cast(pl.Int32).alias("year"),
        pl.col("timestamp").dt.month().cast(pl.Int8).alias("month"),
        pl.col("timestamp").dt.day().cast(pl.Int8).alias("day"),
        pl.col("timestamp").dt.hour().cast(pl.Int8).alias("hour"),
    ])

    # Group forecast rows by (month, day) so we only run one
    # climatology query per unique calendar day in the window. Two
    # weeks of forecast = ~15 unique (month, day) combinations.
    unique_days = (
        enriched.select(["year", "month", "day"]).unique()
        .sort(["year", "month", "day"])
    )

    clim_frames: list[pl.DataFrame] = []
    for row in unique_days.iter_rows(named=True):
        clim = hourly_climatology(
            data_dir, location, int(row["month"]), int(row["day"]),
            target_year=int(row["year"]),
        )
        if clim.is_empty():
            continue
        clim = clim.with_columns([
            pl.lit(int(row["year"]), dtype=pl.Int32).alias("year"),
            pl.lit(int(row["month"]), dtype=pl.Int8).alias("month"),
            pl.lit(int(row["day"]), dtype=pl.Int8).alias("day"),
        ])
        clim_frames.append(clim)

    if not clim_frames:
        return enriched

    clim_all = pl.concat(clim_frames, how="vertical_relaxed")
    return enriched.join(
        clim_all, on=["year", "month", "day", "hour"], how="left",
    )
