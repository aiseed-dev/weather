# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Yasuhiro / AIseed

"""Point-forecast view (the 地点 / Points tab).

Read .agents/skills/chart-base-design (palette principles), the
flet-component-basics skill (declarative @ft.component + hooks), and
docs/forecast-spec.md (data integration) before editing.

Scope of this commit:
  * location dialog (add / select)
  * Open-Meteo HRES main forecast — past 3 / future 15 days
  * MSM reference forecast (when location is inside Japan)
  * initial 30-year ERA5 archive build kicked off when a location is
    added — progress reported as 'X/30 年'
  * Polars climatology stats joined into the forecast table (mean +
    band columns) so anomaly / Z-score is readable per row

Not yet wired here: chart drawing (the table comes first per spec
step 3-7), ensemble band overlay (step 9), historical-record
callouts (optional). Those come in a follow-up commit.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import flet as ft
import httpx
import polars as pl

from aiseed_weather.figures.canvas_timeseries import (
    build_point_forecast_canvas,
)
from aiseed_weather.figures.point_forecast_chart import (
    render_point_forecast,
)
from aiseed_weather.models.point_location import (
    Location,
    load_locations,
    save_locations,
)
from aiseed_weather.models.user_settings import UserSettings, resolved_data_dir
from aiseed_weather.services.open_meteo_archive import (
    plan_daily_update,
    plan_initial_archive,
    run_plan_async,
    has_archive_for_day,
)
from aiseed_weather.services.open_meteo_ensemble import (
    aggregate_to_quantiles,
    fetch_ensemble,
)
from aiseed_weather.services.open_meteo_forecast import (
    HOURLY_VARS,
    ForecastResult,
    fetch_forecast,
)
from aiseed_weather.services.jma_amedas_service import (
    AmedasStation, JmaAmedasService, haversine_km, nearest_stations,
)
from aiseed_weather.services.jma_forecast_service import (
    JmaForecastService,
)
from aiseed_weather.services.point_climatology import (
    daily_records,
    hourly_climatology,
    join_forecast_with_climatology,
)

logger = logging.getLogger(__name__)


# Variable the chart can plot. Keys must match Open-Meteo's hourly
# variable names (and the corresponding climatology join column
# prefixes). Display labels come from point_forecast_chart's
# _VAR_INFO; we keep them aligned here.
#
# 'overview' is a synthetic key: it doesn't drive a chart, it
# switches the view to the weekly + hourly summary cards. Listed
# first so it's the landing page for a freshly-opened location.
_OVERVIEW_KEY = "overview"
_CHART_VARIABLES: tuple[tuple[str, str], ...] = (
    (_OVERVIEW_KEY,        "概要"),
    ("temperature_2m",     "気温 (°C)"),
    ("precipitation",      "降水量 (mm/h)"),
    ("relative_humidity_2m", "相対湿度 (%)"),
    ("wind_speed_10m",     "風速 (m/s)"),
    ("cloud_cover",        "雲量 (%)"),
)


# Forecast snapshot held in use_state. ``eq=False`` is critical:
# Polars DataFrames define ``__eq__`` as element-wise comparison
# (returns a frame of bools, not a single bool), which crashes the
# default dataclass __eq__ when use_state tries ``prev != new`` to
# decide whether to re-render. With eq=False each instance compares
# unequal to every other (identity equality), so every set call
# triggers a re-render — which is exactly what we want here since
# we only construct a new _ForecastSnapshot on a completed fetch.
@dataclass(frozen=True, eq=False)
class _ForecastSnapshot:
    hres_label: str
    hres_df: pl.DataFrame
    msm_label: str | None
    msm_df: pl.DataFrame | None
    ensemble_quantiles: pl.DataFrame | None
    location_name: str


# ── Page-level singleton service lookup ─────────────────────────────


def _get_file_picker() -> ft.FilePicker:
    """Find the FilePicker already attached to page.services, or
    register a new one. Idempotent — calling on every render returns
    the same instance.

    Storing the picker in ``use_ref`` would violate flet-declarative
    (no Control instances in refs); a page-level singleton sidesteps
    that without leaking a picker per re-render.
    """
    page = ft.context.page
    services = list(getattr(page, "services", None) or [])
    for s in services:
        if isinstance(s, ft.FilePicker):
            return s
    fp = ft.FilePicker()
    services.append(fp)
    page.services = services
    return fp


# ── Forecast-table renderer ────────────────────────────────────────


def _format_value(value, *, fmt: str = ".1f") -> str:
    if value is None:
        return "—"
    try:
        return format(float(value), fmt)
    except (TypeError, ValueError):
        return "—"


# Per-variable daily aggregation spec. Each variable picks the
# columns that actually make sense for its daily summary
# (temperature wants mean/max/min, precipitation wants daily sum
# and peak hourly, etc.). Variables not listed fall back to
# (mean, max, min).
_DailyColSpec = tuple[str, str, str]  # (display_label, polars_op, value_fmt)

_DAILY_COLS_BY_VAR: dict[str, tuple[_DailyColSpec, ...]] = {
    "temperature_2m": (
        ("平均", "mean", ".1f"),
        ("最高", "max",  ".1f"),
        ("最低", "min",  ".1f"),
    ),
    "precipitation": (
        ("日合計 (mm)",        "sum", ".1f"),
        ("時間最大 (mm/h)",    "max", ".1f"),
    ),
    "relative_humidity_2m": (
        ("平均", "mean", ".0f"),
        ("最高", "max",  ".0f"),
        ("最低", "min",  ".0f"),
    ),
    "wind_speed_10m": (
        ("平均", "mean", ".1f"),
        ("最大", "max",  ".1f"),
    ),
    "cloud_cover": (
        ("平均", "mean", ".0f"),
    ),
}

_RECORD_LABEL_BY_VAR: dict[str, tuple[str, str]] = {
    # (high label, low label)
    "temperature_2m":       ("過去最高", "過去最低"),
    "precipitation":        ("過去最大日合計", ""),
    "relative_humidity_2m": ("過去最高", "過去最低"),
    "wind_speed_10m":       ("過去最大", ""),
    "cloud_cover":          ("過去最大", "過去最少"),
}


def _hourly_tsv(df: pl.DataFrame) -> str:
    """Polars DataFrame → tab-separated string, ready for
    ``page.set_clipboard`` so the user can paste straight into Excel
    / a notebook / wherever they actually analyse the numbers."""
    if df.is_empty():
        return ""
    # Drop the climatology join columns from the copy — they're
    # derived, the user only really wants the raw forecast.
    keep = [c for c in df.columns if not (
        c.startswith("temperature_2m_") and c not in ("temperature_2m",)
    ) and c not in ("month", "day", "hour")]
    keep = [c for c in keep if not (
        c.endswith("_mean") or c.endswith("_std") or c.endswith("_p25")
        or c.endswith("_p75") or c.endswith("_median")
        or c.endswith("_min") or c.endswith("_max")
        or c.endswith("_slope") or c.endswith("_intercept")
        or c.endswith("_estimate")
    )]
    return df.select(keep).write_csv(separator="\t")


# WMO weather code → (label, Material icon name, accent colour).
# Mirrors what a Japanese TV / newspaper forecast shows at a glance.
# When we collapse a day to a single 'representative' condition we
# take the max code in the day because the WMO ordering puts the
# more disruptive phenomena (rain → snow → thunder) above the benign
# ones (clear → cloud), so 'worst of the day wins' is the right
# default for a headline impression.
_WEATHER_BY_CODE: dict[int, tuple[str, str, str]] = {
    0:  ("快晴",     ft.Icons.WB_SUNNY,     "#f5a623"),
    1:  ("晴れ",     ft.Icons.WB_SUNNY,     "#f5a623"),
    2:  ("晴時々曇", ft.Icons.WB_CLOUDY,    "#c8a14e"),
    3:  ("曇り",     ft.Icons.CLOUD,        "#7c8693"),
    45: ("霧",       ft.Icons.FOGGY,        "#9aa0a8"),
    48: ("霧",       ft.Icons.FOGGY,        "#9aa0a8"),
    51: ("霧雨",     ft.Icons.GRAIN,        "#6aa8d6"),
    53: ("霧雨",     ft.Icons.GRAIN,        "#6aa8d6"),
    55: ("霧雨",     ft.Icons.GRAIN,        "#5b95c9"),
    56: ("着氷雨",   ft.Icons.AC_UNIT,      "#5a6fa5"),
    57: ("着氷雨",   ft.Icons.AC_UNIT,      "#5a6fa5"),
    61: ("小雨",     ft.Icons.GRAIN,        "#5b95c9"),
    63: ("雨",       ft.Icons.UMBRELLA,     "#3a78b3"),
    65: ("強雨",     ft.Icons.UMBRELLA,     "#234b86"),
    66: ("着氷雨",   ft.Icons.AC_UNIT,      "#5a6fa5"),
    67: ("着氷雨",   ft.Icons.AC_UNIT,      "#5a6fa5"),
    71: ("小雪",     ft.Icons.AC_UNIT,      "#a5c1d8"),
    73: ("雪",       ft.Icons.AC_UNIT,      "#8aa9c6"),
    75: ("大雪",     ft.Icons.AC_UNIT,      "#647db0"),
    77: ("霧雪",     ft.Icons.AC_UNIT,      "#8aa9c6"),
    80: ("にわか雨", ft.Icons.GRAIN,        "#5b95c9"),
    81: ("にわか雨", ft.Icons.UMBRELLA,     "#3a78b3"),
    82: ("豪雨",     ft.Icons.UMBRELLA,     "#234b86"),
    85: ("にわか雪", ft.Icons.AC_UNIT,      "#8aa9c6"),
    86: ("大雪",     ft.Icons.AC_UNIT,      "#647db0"),
    95: ("雷雨",     ft.Icons.THUNDERSTORM, "#8a4a99"),
    96: ("雷雨雹",   ft.Icons.THUNDERSTORM, "#8a4a99"),
    99: ("雷雨雹",   ft.Icons.THUNDERSTORM, "#8a4a99"),
}


def _weather_descr(code) -> tuple[str, str, str]:
    if code is None:
        return ("—", ft.Icons.HELP_OUTLINE, ft.Colors.GREY)
    try:
        c = int(code)
    except (TypeError, ValueError):
        return ("—", ft.Icons.HELP_OUTLINE, ft.Colors.GREY)
    return _WEATHER_BY_CODE.get(
        c, (f"code {c}", ft.Icons.HELP_OUTLINE, ft.Colors.GREY),
    )


def _location_zoneinfo(location: Location) -> ZoneInfo:
    """ZoneInfo for the location, falling back to UTC if the saved
    name is unparseable. The dialog validates input at write time
    so this fallback only kicks in for legacy / hand-edited entries.
    """
    try:
        return ZoneInfo(location.timezone)
    except (ZoneInfoNotFoundError, ValueError, TypeError):
        return ZoneInfo("UTC")


def _forecast_summary_strip(
    forecast_df: pl.DataFrame, location: Location, days: int = 7,
) -> ft.Control:
    """Classic horizontal forecast-summary strip: one card per day
    with weather icon, Japanese condition label, high/low
    temperatures and daily precipitation. Mirrors the at-a-glance
    headline a Japanese TV / newspaper forecast presents so the
    analyst sees the week's overall impression before drilling into
    the variable-by-variable charts. Day boundaries respect the
    location's saved timezone (a forecast 'day' is 0–24 h local
    clock, not 0–24 h UTC).
    """
    if forecast_df.is_empty():
        return ft.Container()

    tz = _location_zoneinfo(location)
    today_local = datetime.now(tz).date()
    daily = (
        forecast_df
        .with_columns(
            pl.col("timestamp")
            .dt.convert_time_zone(str(tz))
            .dt.date()
            .alias("date")
        )
        # Drop the past_days=3 history slice the forecast fetch
        # always carries — a 'weekly forecast' headline shouldn't
        # show days that have already happened.
        .filter(pl.col("date") >= today_local)
        .group_by("date")
        .agg([
            pl.col("temperature_2m").max().alias("t_high"),
            pl.col("temperature_2m").min().alias("t_low"),
            pl.col("precipitation").sum().alias("p_sum"),
            pl.col("weather_code").max().alias("wcode"),
        ])
        .sort("date")
        .head(days)
    )

    today = datetime.now(tz).date()
    weekday_jp = ("月", "火", "水", "木", "金", "土", "日")
    cards: list[ft.Control] = []
    for row in daily.iter_rows(named=True):
        d: date = row["date"]
        delta = (d - today).days
        if delta == 0:
            day_label = "今日"
        elif delta == 1:
            day_label = "明日"
        elif delta == 2:
            day_label = "明後日"
        else:
            day_label = f"({weekday_jp[d.weekday()]})"
        # Saturday blue / Sunday red — Japanese calendar convention.
        day_color = (
            "#2c7fb8" if d.weekday() == 5
            else "#c0392b" if d.weekday() == 6
            else None
        )
        label, icon, color = _weather_descr(row["wcode"])
        p_sum = row["p_sum"]
        precip_text = (
            f"{p_sum:.1f} mm" if p_sum is not None and p_sum > 0.05
            else "—"
        )
        precip_color = "#3a78b3" if (p_sum or 0) > 0.05 else ft.Colors.GREY

        cards.append(ft.Container(
            content=ft.Column(
                controls=[
                    ft.Text(
                        day_label, size=12, weight=ft.FontWeight.BOLD,
                        color=day_color,
                    ),
                    ft.Text(d.strftime("%m/%d"), size=10, color=ft.Colors.GREY),
                    ft.Icon(icon, color=color, size=40),
                    ft.Text(label, size=11),
                    ft.Row(controls=[
                        ft.Text(
                            _format_value(row["t_high"], fmt=".0f"),
                            color="#c0392b", weight=ft.FontWeight.BOLD,
                            size=14,
                        ),
                        ft.Text("/", color=ft.Colors.GREY),
                        ft.Text(
                            _format_value(row["t_low"], fmt=".0f"),
                            color="#2c7fb8", weight=ft.FontWeight.BOLD,
                            size=14,
                        ),
                        ft.Text("°", size=10, color=ft.Colors.GREY),
                    ], alignment=ft.MainAxisAlignment.CENTER, spacing=2),
                    ft.Text(precip_text, size=11, color=precip_color),
                ],
                horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                spacing=2,
            ),
            padding=ft.Padding.all(8),
            border=ft.Border.all(width=1, color=ft.Colors.BLACK26),
            border_radius=6,
            width=92,
        ))

    return ft.Column(controls=[
        ft.Text(
            f"週間天気予報 ({location.timezone})",
            size=14, weight=ft.FontWeight.BOLD,
        ),
        ft.Row(
            controls=cards, spacing=6, wrap=False,
            scroll=ft.ScrollMode.AUTO,
        ),
    ])


def _hourly_forecast_strip(
    forecast_df: pl.DataFrame, location: Location, hours: int = 48,
) -> ft.Control:
    """Hour-by-hour forecast strip for the next ``hours`` hours from
    'now' in the location's timezone. Mirrors tenki.jp / Yahoo 天気's
    hourly tables: time, weather icon, temperature, precipitation
    per cell. Horizontally scrollable so 48 h fits without forcing
    a wide layout — each cell is intentionally narrow (~52 px) to
    keep the strip dense enough to read trends along it.

    A vertical separator card marks where each calendar day starts,
    so the user can still see 'today / tomorrow / day-after' at a
    glance even when the strip is mid-scroll.
    """
    if forecast_df.is_empty():
        return ft.Container()

    tz = _location_zoneinfo(location)
    now_local = datetime.now(tz).replace(minute=0, second=0, microsecond=0)
    end_local = now_local + timedelta(hours=hours)

    hourly = (
        forecast_df
        .with_columns(
            pl.col("timestamp")
            .dt.convert_time_zone(str(tz))
            .alias("local_ts")
        )
        .filter(
            (pl.col("local_ts") >= now_local)
            & (pl.col("local_ts") < end_local)
        )
        .sort("local_ts")
    )
    if hourly.is_empty():
        return ft.Container()

    cells: list[ft.Control] = []
    prev_date: date | None = None
    today = now_local.date()
    for row in hourly.iter_rows(named=True):
        ts: datetime = row["local_ts"]
        d = ts.date()
        if d != prev_date:
            delta = (d - today).days
            head_label = (
                "今日" if delta == 0
                else "明日" if delta == 1
                else "明後日" if delta == 2
                else d.strftime("%a")
            )
            head_color = (
                "#2c7fb8" if d.weekday() == 5
                else "#c0392b" if d.weekday() == 6
                else "#2c3e50"
            )
            cells.append(ft.Container(
                content=ft.Column(
                    controls=[
                        ft.Text(
                            head_label, size=12,
                            weight=ft.FontWeight.BOLD,
                            color=head_color,
                        ),
                        ft.Text(d.strftime("%m/%d"), size=9,
                                color=ft.Colors.GREY),
                    ],
                    horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                    spacing=0,
                ),
                padding=ft.Padding.symmetric(horizontal=6, vertical=8),
                bgcolor="#eef3f8",
                border_radius=4,
                width=44,
                alignment=ft.Alignment.CENTER,
            ))
            prev_date = d

        label, icon, color = _weather_descr(row.get("weather_code"))
        temp = row.get("temperature_2m")
        precip = row.get("precipitation")
        # Warm / cold tinting on the temperature digit — matches the
        # red-hot / blue-cold convention the weekly card already uses.
        if temp is None:
            temp_color = ft.Colors.GREY
        elif temp >= 28:
            temp_color = "#c0392b"
        elif temp <= 5:
            temp_color = "#2c7fb8"
        else:
            temp_color = None
        cells.append(ft.Container(
            content=ft.Column(
                controls=[
                    ft.Text(f"{ts.hour:02d}", size=10,
                            color=ft.Colors.GREY),
                    ft.Icon(icon, color=color, size=22),
                    ft.Text(
                        _format_value(temp, fmt=".0f") + "°",
                        size=12, weight=ft.FontWeight.BOLD,
                        color=temp_color,
                    ),
                    ft.Text(
                        f"{precip:.1f}"
                        if precip is not None and precip > 0.05 else "",
                        size=9, color="#3a78b3",
                    ),
                ],
                horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                spacing=1,
            ),
            padding=ft.Padding.symmetric(horizontal=2, vertical=4),
            border=ft.Border.all(width=1, color=ft.Colors.BLACK12),
            border_radius=4,
            width=50,
        ))

    return ft.Column(controls=[
        ft.Text(
            f"時間別予報 ({hours}時間)",
            size=14, weight=ft.FontWeight.BOLD,
        ),
        ft.Row(
            controls=cells, spacing=2, wrap=False,
            scroll=ft.ScrollMode.AUTO,
        ),
    ])


# AMeDAS wind directions are reported as a 16-point compass index
# (1=N, 2=NNE, ..., 16=NNW); 0 means calm. Map to the conventional
# Japanese labels used on TV / newspaper weather pages.
_WIND_DIR_JP: tuple[str, ...] = (
    "静穏", "北", "北北東", "北東", "東北東",
    "東", "東南東", "南東", "南南東", "南",
    "南南西", "南西", "西南西", "西",
    "西北西", "北西", "北北西",
)


def _amedas_card(
    stations: list[tuple[AmedasStation, dict[str, float]]],
    snapshot_time: datetime,
    home_lat: float,
    home_lon: float,
) -> ft.Control:
    """AMeDAS observation card. One row per selected station with
    temperature / humidity / 1-hour rain / wind speed-direction.
    Missing values per-station fall back to '—' so the card stays
    aligned even when one station only publishes rainfall.
    """
    rows: list[ft.Control] = []
    header = ft.Row(controls=[
        ft.Text("観測所", weight=ft.FontWeight.BOLD, width=140),
        ft.Text("距離", weight=ft.FontWeight.BOLD, width=60),
        ft.Text("気温", weight=ft.FontWeight.BOLD, width=70),
        ft.Text("湿度", weight=ft.FontWeight.BOLD, width=60),
        ft.Text("1h降水", weight=ft.FontWeight.BOLD, width=70),
        ft.Text("風", weight=ft.FontWeight.BOLD, width=130),
    ])
    rows.append(header)
    for station, obs in stations:
        dist = haversine_km(
            home_lat, home_lon, station.latitude, station.longitude,
        )
        temp = obs.get("temp")
        humidity = obs.get("humidity")
        rain_1h = obs.get("prcp_1h")
        wind_spd = obs.get("wind_speed")
        wind_dir_idx = obs.get("wind_dir")
        if wind_spd is None:
            wind_text = "—"
        else:
            try:
                idx = int(wind_dir_idx) if wind_dir_idx is not None else 0
            except (TypeError, ValueError):
                idx = 0
            dir_label = _WIND_DIR_JP[idx] if 0 <= idx < len(_WIND_DIR_JP) else "—"
            wind_text = f"{wind_spd:.1f} m/s {dir_label}"
        rows.append(ft.Row(controls=[
            ft.Text(station.name_kanji or station.station_id, width=140,
                    size=12),
            ft.Text(f"{dist:.1f} km", width=60, size=12,
                    color=ft.Colors.GREY),
            ft.Text(
                _format_value(temp, fmt=".1f") + (" °C" if temp is not None else ""),
                width=70, size=12,
                color="#c0392b" if (temp or 0) >= 28
                else "#2c7fb8" if (temp is not None and temp <= 5) else None,
            ),
            ft.Text(
                _format_value(humidity, fmt=".0f") + (" %" if humidity is not None else ""),
                width=60, size=12,
            ),
            ft.Text(
                _format_value(rain_1h, fmt=".1f") + (" mm" if rain_1h is not None else ""),
                width=70, size=12,
                color="#3a78b3" if (rain_1h or 0) > 0.05 else None,
            ),
            ft.Text(wind_text, width=130, size=12),
        ]))

    return ft.Container(
        content=ft.Column(controls=[
            ft.Row(controls=[
                ft.Icon(ft.Icons.SENSORS, color="#2c7fb8", size=18),
                ft.Text(
                    f"AMeDAS 観測 ({snapshot_time:%m/%d %H:%M} JST 時点)",
                    size=14, weight=ft.FontWeight.BOLD,
                ),
            ]),
            *rows,
            ft.Text(
                "出典: 気象庁ホームページ (https://www.jma.go.jp/)",
                size=10, color=ft.Colors.GREY,
            ),
        ], spacing=4),
        padding=ft.Padding.all(10),
        border=ft.Border.all(width=1, color=ft.Colors.BLACK26),
        border_radius=6,
    )


def _jma_forecast_card(forecast) -> ft.Control:
    """JMA 府県天気予報 card: short-term (today / tomorrow /
    day-after) headline weather + pop %, followed by the week-ahead
    strip (date / icon / min-max temp / pop / reliability).
    ``forecast`` is the ForecastBundle dataclass from
    jma_forecast_service.
    """
    short_rows: list[ft.Control] = []
    today = datetime.now().date()
    for day in forecast.short_term[:3]:
        delta = (day.date - today).days
        day_label = (
            "今日" if delta == 0
            else "明日" if delta == 1
            else "明後日" if delta == 2
            else day.date.strftime("%m/%d")
        )
        text = day.weather_text or "—"
        pop_text = (
            f"降水確率 {day.precip_prob_pct} %"
            if day.precip_prob_pct is not None else ""
        )
        short_rows.append(ft.Row(controls=[
            ft.Text(day_label, weight=ft.FontWeight.BOLD,
                    size=13, width=70),
            ft.Text(text, size=12, expand=True),
            ft.Text(pop_text, size=11, color=ft.Colors.GREY),
        ]))

    week_cards: list[ft.Control] = []
    weekday_jp = ("月", "火", "水", "木", "金", "土", "日")
    for day in forecast.week_ahead:
        if day.date <= today:
            continue
        delta = (day.date - today).days
        day_label = (
            "明日" if delta == 1
            else "明後日" if delta == 2
            else f"({weekday_jp[day.date.weekday()]})"
        )
        day_color = (
            "#2c7fb8" if day.date.weekday() == 5
            else "#c0392b" if day.date.weekday() == 6 else None
        )
        label, icon, color = _weather_descr(day.weather_code)
        rel_color = (
            "#27ae60" if day.reliability == "A"
            else "#e67e22" if day.reliability == "B"
            else "#c0392b" if day.reliability == "C" else ft.Colors.GREY
        )
        week_cards.append(ft.Container(
            content=ft.Column(
                controls=[
                    ft.Text(day_label, size=11, weight=ft.FontWeight.BOLD,
                            color=day_color),
                    ft.Text(day.date.strftime("%m/%d"),
                            size=9, color=ft.Colors.GREY),
                    ft.Icon(icon, color=color, size=28),
                    ft.Text(label, size=10),
                    ft.Row(controls=[
                        ft.Text(_format_value(day.temp_max, fmt=".0f"),
                                color="#c0392b",
                                weight=ft.FontWeight.BOLD, size=12),
                        ft.Text("/", color=ft.Colors.GREY, size=10),
                        ft.Text(_format_value(day.temp_min, fmt=".0f"),
                                color="#2c7fb8",
                                weight=ft.FontWeight.BOLD, size=12),
                    ], alignment=ft.MainAxisAlignment.CENTER, spacing=2),
                    ft.Text(
                        f"{day.precip_prob_pct} %"
                        if day.precip_prob_pct is not None else "—",
                        size=10, color="#3a78b3",
                    ),
                    ft.Text(
                        f"信頼度 {day.reliability}"
                        if day.reliability else "",
                        size=9, color=rel_color,
                    ),
                ],
                horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                spacing=1,
            ),
            padding=ft.Padding.all(6),
            border=ft.Border.all(width=1, color=ft.Colors.BLACK12),
            border_radius=4,
            width=72,
        ))

    return ft.Container(
        content=ft.Column(controls=[
            ft.Row(controls=[
                ft.Icon(ft.Icons.CLOUD_QUEUE, color="#34495e", size=18),
                ft.Text(
                    "JMA 府県天気予報",
                    size=14, weight=ft.FontWeight.BOLD,
                ),
                ft.Container(expand=True),
                ft.Text(
                    f"発表: {forecast.report_datetime:%Y-%m-%d %H:%M} "
                    f"({forecast.publishing_office})",
                    size=10, color=ft.Colors.GREY,
                ),
            ]),
            *short_rows,
            ft.Divider(),
            ft.Text("週間予報", size=12, weight=ft.FontWeight.BOLD),
            ft.Row(controls=week_cards, spacing=4, wrap=False,
                   scroll=ft.ScrollMode.AUTO)
            if week_cards else ft.Text("週間予報なし", color=ft.Colors.GREY),
            ft.Text(
                "出典: 気象庁ホームページ (https://www.jma.go.jp/)",
                size=10, color=ft.Colors.GREY,
            ),
        ], spacing=6),
        padding=ft.Padding.all(10),
        border=ft.Border.all(width=1, color=ft.Colors.BLACK26),
        border_radius=6,
    )


def _daily_summary_table(
    forecast_df: pl.DataFrame,
    variable: str,
    data_dir: Path,
    location: Location,
) -> ft.Control:
    """Daily aggregation of the forecast + per-day historical records
    + per-day climatology mean (when the variable has one). Each
    forecast day is one row.

    Layout chosen so the analyst sees per-day numbers (the unit they
    actually plan around) without having to scroll through the 432
    hourly samples we used to render. Records pull from
    ``daily_records`` in point_climatology.
    """
    if forecast_df.is_empty():
        return ft.Text("日次サマリ: データなし", color=ft.Colors.GREY)

    cols_spec = _DAILY_COLS_BY_VAR.get(
        variable,
        (("平均", "mean", ".1f"), ("最大", "max", ".1f"), ("最小", "min", ".1f")),
    )
    high_label, low_label = _RECORD_LABEL_BY_VAR.get(
        variable, ("過去最高", "過去最低"),
    )
    has_clim = variable != "precipitation"

    # Daily aggregates from the forecast.
    aggs = [getattr(pl.col(variable), op)().alias(f"d_{op}")
            for _, op, _ in cols_spec]
    # ``sum`` aliased as d_sum; ``max`` aliased as d_max, etc. Keep
    # any op the spec asks for so the row build below can index by
    # f"d_{op}".
    daily = (
        forecast_df.with_columns(
            pl.col("timestamp").dt.date().alias("date"),
            pl.col("timestamp").dt.month().cast(pl.Int8).alias("d_month"),
            pl.col("timestamp").dt.day().cast(pl.Int8).alias("d_day"),
        )
        .group_by(["date", "d_month", "d_day"])
        .agg(aggs)
        .sort("date")
    )

    columns = [ft.DataColumn(ft.Text("日付", weight=ft.FontWeight.BOLD))]
    for label, _op, _fmt in cols_spec:
        columns.append(ft.DataColumn(ft.Text(label, weight=ft.FontWeight.BOLD)))
    if has_clim:
        columns.append(ft.DataColumn(ft.Text("平年", weight=ft.FontWeight.BOLD)))
        columns.append(ft.DataColumn(ft.Text("推計値", weight=ft.FontWeight.BOLD)))
    if high_label:
        columns.append(ft.DataColumn(ft.Text(high_label, weight=ft.FontWeight.BOLD)))
    if low_label:
        columns.append(ft.DataColumn(ft.Text(low_label, weight=ft.FontWeight.BOLD)))

    rows: list[ft.DataRow] = []
    for row in daily.iter_rows(named=True):
        cells = [ft.DataCell(ft.Text(row["date"].strftime("%m-%d")))]
        for _label, op, fmt in cols_spec:
            cells.append(ft.DataCell(
                ft.Text(_format_value(row.get(f"d_{op}"), fmt=fmt)),
            ))
        # Climatology daily mean = average of the 24 hourly means.
        # 推計値 (estimate) = same average over the per-hour linear-
        # regression projection onto this forecast day's year, i.e.
        # what the trend says today's normal should be after climate
        # shift is accounted for.
        if has_clim:
            clim = hourly_climatology(
                data_dir, location, int(row["d_month"]), int(row["d_day"]),
                target_year=row["date"].year,
            )
            clim_col = f"{variable}_mean"
            est_col = f"{variable}_estimate"
            if not clim.is_empty() and clim_col in clim.columns:
                cells.append(ft.DataCell(
                    ft.Text(_format_value(clim[clim_col].mean(), fmt=".1f")),
                ))
            else:
                cells.append(ft.DataCell(ft.Text("—")))
            if not clim.is_empty() and est_col in clim.columns:
                cells.append(ft.DataCell(
                    ft.Text(_format_value(clim[est_col].mean(), fmt=".1f")),
                ))
            else:
                cells.append(ft.DataCell(ft.Text("—")))

        records = daily_records(
            data_dir, location, int(row["d_month"]), int(row["d_day"]),
        )
        if high_label:
            # For precipitation, the 'high' record is wettest-day total;
            # for the rest it's the highest single hourly value.
            key = "_wettest" if variable == "precipitation" else "_high"
            rec = records.get(f"{variable}{key}")
            cells.append(ft.DataCell(ft.Text(
                f"{rec[0]:.1f} ({rec[1]})" if rec else "—",
            )))
        if low_label:
            rec = records.get(f"{variable}_low")
            cells.append(ft.DataCell(ft.Text(
                f"{rec[0]:.1f} ({rec[1]})" if rec else "—",
            )))
        rows.append(ft.DataRow(cells=cells))

    return ft.Column(controls=[
        ft.Text("日次サマリ / Daily summary",
                size=14, weight=ft.FontWeight.BOLD),
        ft.DataTable(columns=columns, rows=rows),
    ])


# ── Async work driven by the component ────────────────────────────


async def _build_initial_archive_for(
    location: Location, data_dir: Path,
    on_progress,
) -> None:
    """Drive the 30-year initial archive build for ``location``,
    reporting progress via ``on_progress(done, total)``."""
    today = date.today()
    plans = plan_initial_archive(today=today, years=30, window_days=7)
    async with httpx.AsyncClient() as client:
        async for done, total in run_plan_async(
            location=location,
            plans=plans,
            data_dir=data_dir,
            client=client,
        ):
            on_progress(done, total)


async def _ensure_daily_archive_for(
    location: Location, data_dir: Path,
) -> None:
    """Top up today's row across all 30 years if not already there.
    Cheap — typically 0 or ~30 calls. Called on each open of the view
    so the climatology join sees the freshest possible same-day data."""
    today = date.today()
    if has_archive_for_day(data_dir, location, today, years=30):
        return
    plans = plan_daily_update(target_date=today, years=30)
    async with httpx.AsyncClient() as client:
        async for _done, _total in run_plan_async(
            location=location,
            plans=plans,
            data_dir=data_dir,
            client=client,
        ):
            pass


async def _fetch_all(
    location: Location,
) -> tuple[ForecastResult, ForecastResult | None, pl.DataFrame | None]:
    """Fetch HRES + (MSM if Japan) + ensemble quantiles, all
    concurrently. Three Open-Meteo endpoints, one shared
    ``AsyncClient`` so the underlying HTTP/2 connection pool is
    reused across calls.

    Returns:
      hres                 — main deterministic forecast (always)
      msm_or_none          — JMA MSM reference (Japan only)
      ensemble_quantiles   — per-(timestamp, variable) p10 / p50 /
                              p90 / mean / std reduction of the
                              51 member ENS run, or None on failure
                              (ensemble being optional, the chart
                              renders without it just fine).
    """
    async with httpx.AsyncClient() as client:
        hres_task = asyncio.create_task(fetch_forecast(
            latitude=location.latitude,
            longitude=location.longitude,
            client=client,
            model="ecmwf_ifs",
            past_days=3,
            forecast_days=15,
        ))
        if location.is_japan:
            msm_task: asyncio.Task | None = asyncio.create_task(
                fetch_forecast(
                    latitude=location.latitude,
                    longitude=location.longitude,
                    client=client,
                    model="jma_msm",
                    past_days=1,
                    forecast_days=4,
                ),
            )
        else:
            msm_task = None

        ens_task = asyncio.create_task(fetch_ensemble(
            latitude=location.latitude,
            longitude=location.longitude,
            client=client,
            model="ecmwf_ifs025",
            forecast_days=15,
        ))

        hres = await hres_task
        msm = await msm_task if msm_task is not None else None
        try:
            ens = await ens_task
            ensemble_quantiles = aggregate_to_quantiles(ens.df)
        except Exception:
            # Ensemble is optional decoration — if Open-Meteo's ensemble
            # endpoint rate-limits or 5xxs, the chart still shows the
            # HRES line + climatology band.
            logger.exception("Ensemble fetch failed; chart will skip it")
            ensemble_quantiles = None
    return hres, msm, ensemble_quantiles


# ── Entry component ────────────────────────────────────────────────


@ft.component
def PointForecastView(settings: UserSettings):
    data_dir = resolved_data_dir(settings)

    # Loaded once per mount; refreshed in-place when the user adds a
    # new location.
    locations, set_locations = ft.use_state(load_locations(data_dir))
    selected_name, set_selected_name = ft.use_state(
        locations[0].name if locations else None,
    )

    forecast_state, set_forecast_state = ft.use_state("idle")
    # forecast_state values:
    #   idle           — no location picked or initial mount
    #   fetching       — forecast HTTP in flight
    #   ready          — forecast df + (optional) MSM df + climatology
    #   error          — last fetch raised; carry message in error_msg
    forecast_data, set_forecast_data = ft.use_state(None)
    error_msg, set_error_msg = ft.use_state("")

    # Last successful fetch wall-clock time. Drives the '最終更新'
    # header text so the analyst can tell whether the on-screen
    # values are from the most recent ECMWF run (6h cadence,
    # processed ~3h after run time) or stale from a previous
    # session.
    last_fetched_at, set_last_fetched_at = ft.use_state(None)

    archive_progress, set_archive_progress = ft.use_state(None)
    # ``None`` when no archive build is running; otherwise (done, total)

    # Add-location dialog state. Per flet-declarative the inputs
    # live in use_state (not in Control.value) so re-renders don't
    # drop the user's typing and the submit handler reads the
    # latest values from the closure capture, not from a stale
    # control reference.
    show_dialog, set_show_dialog = ft.use_state(False)
    new_name, set_new_name = ft.use_state("")
    new_lat, set_new_lat = ft.use_state("")
    new_lon, set_new_lon = ft.use_state("")
    # IANA timezone for the location's local clock. Blank → derive
    # from lat/lon (JP bbox → Asia/Tokyo, else UTC). Data fetches
    # always run in UTC; this only drives the chart's display tz.
    new_tz, set_new_tz = ft.use_state("")
    new_err, set_new_err = ft.use_state("")

    # JMA overview side-data (AMeDAS observations + JMA forecast).
    # Populated by load_forecast() for JP locations; value is None
    # before/after the fetch or for non-JP locations. Errors live in
    # their own slot so a transient JMA failure doesn't blow away
    # the cached value from a previous successful fetch.
    jma_overview_data, set_jma_overview_data = ft.use_state(None)
    jma_overview_error, set_jma_overview_error = ft.use_state("")

    # Per-location JMA / AMeDAS settings dialog state. ``settings_target``
    # holds the location being edited (None = closed); ``settings_data``
    # holds the freshly-loaded option lists (offices + nearest stations)
    # and is None while the async loader is running. The form fields
    # live in their own use_state slots so the dialog stays responsive
    # without touching the saved Location until the user clicks 保存.
    settings_target, set_settings_target = ft.use_state(None)
    settings_loading, set_settings_loading = ft.use_state(False)
    settings_data, set_settings_data = ft.use_state(None)
    settings_office, set_settings_office = ft.use_state("")
    settings_a1, set_settings_a1 = ft.use_state("")
    settings_a2, set_settings_a2 = ft.use_state("")
    settings_a3, set_settings_a3 = ft.use_state("")
    settings_tz, set_settings_tz = ft.use_state("")
    settings_err, set_settings_err = ft.use_state("")

    def _reset_dialog() -> None:
        set_new_name("")
        set_new_lat("")
        set_new_lon("")
        set_new_tz("")
        set_new_err("")

    def _cancel_new_location():
        set_show_dialog(False)
        _reset_dialog()

    # Chart state. ``variable`` drives which value series is plotted.
    # The chart itself is a Flet ``flet.canvas.Canvas`` built every
    # render — no caching needed, since the shape construction is
    # pure Python (~1 ms for the 60-ish shapes in a full chart) and
    # the layout is automatically reactive to forecast_data changes.
    # matplotlib stays around purely as the publication export path
    # (PNG ダウンロード button below).
    variable, set_variable = ft.use_state(_CHART_VARIABLES[0][0])
    # Chart visible window in days. Buttons let the analyst zoom in
    # to a few days for legibility, or out to the full HRES range
    # for a panoramic look. Default 7 = one week.
    visible_days, set_visible_days = ft.use_state(7)
    # Horizontal pan offset (in hours). 0 = window centred on 'now'
    # per the day-range rule (25 % past / 75 % future). Pan buttons
    # shift the window left/right; resets to 0 on day-range change.
    pan_offset_h, set_pan_offset_h = ft.use_state(0)

    # Download flow: a single async coroutine that opens the
    # save-file picker and writes the matplotlib PNG to the chosen
    # path. The FilePicker itself is fetched via _get_file_picker
    # (page-level singleton) so no Control instance ever lives in
    # component state.
    download_error, set_download_error = ft.use_state(None)
    # 'コピー' feedback. Set after a successful clipboard write,
    # cleared on the next variable / day-range change.
    copy_msg, set_copy_msg = ft.use_state("")

    async def _save_chart_png():
        if forecast_data is None:
            return
        set_download_error(None)
        fp = _get_file_picker()
        safe_loc = forecast_data.location_name.replace("/", "_")
        try:
            chosen = await fp.save_file(
                dialog_title="チャートを PNG で保存",
                file_name=f"{safe_loc}_{variable}.png",
                allowed_extensions=["png"],
            )
        except Exception as exc:
            logger.exception("save_file dialog failed")
            set_download_error(f"{type(exc).__name__}: {exc}")
            return
        if not chosen:
            return
        try:
            png_bytes = await asyncio.to_thread(
                render_point_forecast,
                location_name=forecast_data.location_name,
                variable=variable,
                hres_joined=forecast_data.hres_df,
                msm_df=forecast_data.msm_df,
                ensemble_quantiles=forecast_data.ensemble_quantiles,
            )
            await asyncio.to_thread(Path(chosen).write_bytes, png_bytes)
            logger.info(
                "Chart PNG saved → %s (%.1f KB)",
                chosen, len(png_bytes) / 1024,
            )
        except Exception as exc:
            logger.exception("PNG export failed")
            set_download_error(f"{type(exc).__name__}: {exc}")

    def on_download_click(_e):
        ft.context.page.run_task(_save_chart_png)

    def on_copy_hourly(_e):
        """Copy the HRES hourly forecast as TSV. The user said the
        on-screen hourly table can go away as long as the data is
        copy-able, so this is its replacement — paste into Excel /
        a notebook / wherever the analysis lives."""
        if forecast_data is None:
            return
        text = _hourly_tsv(forecast_data.hres_df)
        if not text:
            return
        try:
            ft.context.page.set_clipboard(text)
            set_copy_msg(
                f"コピー済 ({forecast_data.hres_df.height} 行)",
            )
        except Exception as exc:
            logger.exception("Clipboard copy failed")
            set_copy_msg(f"コピーに失敗: {type(exc).__name__}")

    selected_location = next(
        (loc for loc in locations if loc.name == selected_name),
        None,
    )

    # ── async handlers ────────────────────────────────────────────

    async def load_forecast(loc: Location):
        logger.info("load_forecast: start %s (%.3f, %.3f)",
                    loc.name, loc.latitude, loc.longitude)
        set_forecast_state("fetching")
        set_error_msg("")
        # Drop any JMA overview data from the previous selection
        # before we start so the overview doesn't briefly show stale
        # numbers attributed to the new location.
        set_jma_overview_data(None)
        set_jma_overview_error("")
        try:
            await _ensure_daily_archive_for(loc, data_dir)
            logger.info("load_forecast: archive ensured")
            hres, msm, ensemble_quantiles = await _fetch_all(loc)
            logger.info(
                "load_forecast: HRES=%d, MSM=%s, ENS=%s",
                hres.df.height,
                "yes" if msm else "no",
                "yes" if ensemble_quantiles is not None else "no",
            )
            joined = await asyncio.to_thread(
                join_forecast_with_climatology, hres.df, data_dir, loc,
            )
            logger.info("load_forecast: climatology joined")
            snap = _ForecastSnapshot(
                hres_label=f"ECMWF IFS HRES @ {loc.name}",
                hres_df=joined,
                msm_label=(
                    f"参考: JMA MSM @ {loc.name}"
                    if msm is not None else None
                ),
                msm_df=msm.df if msm is not None else None,
                ensemble_quantiles=ensemble_quantiles,
                location_name=loc.name,
            )
            set_forecast_data(snap)
            set_forecast_state("ready")
            set_last_fetched_at(datetime.now())
            logger.info(
                "load_forecast: state=ready (canvas re-renders inline)",
            )
        except Exception as exc:  # noqa: BLE001 — surface to user
            logger.exception("Forecast fetch failed for %s", loc.name)
            set_error_msg(f"{type(exc).__name__}: {exc}")
            set_forecast_state("error")
            return
        # JMA overview side-load. Only fires for JP locations and only
        # if the main forecast succeeded — keeps the chart usable even
        # when JMA's endpoint is having a bad day. Failures land in
        # the overview-page error slot, not the page-level one.
        if loc.is_japan:
            try:
                await _load_jma_overview(loc)
            except Exception as exc:  # noqa: BLE001
                logger.exception("JMA overview load failed for %s", loc.name)
                set_jma_overview_error(f"{type(exc).__name__}: {exc}")

    async def _load_jma_overview(loc: Location):
        """Fetch AMeDAS snapshot + JMA forecast for the location's
        configured (or auto-resolved) area / stations. Caches in the
        service layer so a session's repeated overview opens cost a
        single HTTP round-trip per JMA endpoint."""
        amedas_svc = JmaAmedasService(data_dir=data_dir)
        jma_svc = JmaForecastService(data_dir=data_dir)
        stations_dict = await amedas_svc.stations()
        snapshot = await amedas_svc.fetch()

        # AMeDAS station list — saved IDs if any, else nearest 3.
        station_ids = list(loc.amedas_station_ids)
        if not station_ids:
            near = nearest_stations(
                stations_dict, loc.latitude, loc.longitude, limit=3,
            )
            station_ids = [s.station_id for s, _ in near]
        amedas_picked: list[tuple[AmedasStation, dict[str, float]]] = []
        for sid in station_ids:
            station = stations_dict.get(sid)
            if station is None:
                continue
            obs = snapshot.observations.get(sid, {})
            amedas_picked.append((station, obs))

        # Forecast area resolution — saved office code if any, else
        # auto-derived. ``class10`` is the per-area key inside the
        # forecast payload; we need it both for the saved-office case
        # and the auto case.
        office_code = loc.jma_area_code
        class10_code = ""
        if office_code:
            area_table = await jma_svc.area_table()
            for code, info in (area_table.get("class10s") or {}).items():
                if str(info.get("parent")) == office_code:
                    class10_code = str(code)
                    break
        else:
            resolved = await jma_svc.resolve_area(
                loc.latitude, loc.longitude, stations_dict,
            )
            if resolved is not None:
                office_code = resolved.office_code
                class10_code = resolved.class10_code

        forecast = None
        if office_code and class10_code:
            try:
                forecast = await jma_svc.fetch(office_code, class10_code)
            except Exception:  # noqa: BLE001
                logger.exception(
                    "JMA forecast fetch failed for office=%s", office_code,
                )

        set_jma_overview_data({
            "snapshot_time": snapshot.timestamp,
            "stations": amedas_picked,
            "forecast": forecast,
        })

    async def add_location_flow(loc: Location):
        # Persist + select + kick off initial archive build, all in
        # the same handler so the user sees a single coherent
        # progression: dialog closes → name in dropdown → progress
        # bar appears → fetch begins.
        new_list = [*locations, loc]
        save_locations(data_dir, new_list)
        set_locations(new_list)
        set_selected_name(loc.name)
        set_show_dialog(False)

        set_archive_progress((0, 30))

        def _on_progress(done: int, total: int):
            set_archive_progress((done, total))

        try:
            await _build_initial_archive_for(loc, data_dir, _on_progress)
        except Exception:
            logger.exception("Initial archive build failed for %s", loc.name)
        set_archive_progress(None)
        # Now do the forecast fetch — climatology will be available.
        await load_forecast(loc)

    # Auto-fetch on tab mount: if a location is already selected and
    # we haven't fetched yet, trigger the load. forecast_state moves
    # to 'fetching' on first call so subsequent re-renders skip this
    # branch. Avoids the user having to click 更新 every time they
    # open the app.
    if (
        forecast_state == "idle"
        and selected_location is not None
        and archive_progress is None
    ):
        logger.info("PointForecastView: auto-fetch on mount")
        ft.context.page.run_task(load_forecast, selected_location)

    # Periodic background refresh. ECMWF runs every 6 h, Open-Meteo
    # has the new data ~3 h after the run time, so checking every 3 h
    # is enough to catch all four daily runs without spamming the
    # endpoint. The task is held in a use_ref so the spawn runs once
    # per session (we don't restart it on every re-render).
    refresh_task_ref = ft.use_ref(None)

    async def _periodic_refresh_loop():
        while True:
            await asyncio.sleep(3 * 3600)  # 3 hours
            loc = next(
                (l for l in locations if l.name == selected_name), None,
            )
            if loc is None:
                continue
            logger.info("PointForecastView: periodic auto-refresh")
            try:
                await load_forecast(loc)
            except Exception:
                logger.exception("Periodic refresh raised; will retry")

    if refresh_task_ref.current is None:
        refresh_task_ref.current = ft.context.page.run_task(
            _periodic_refresh_loop,
        )

    # Auto-fetch when selection changes
    def on_select_location(e):
        name = e.control.value
        set_selected_name(name)
        loc = next((l for l in locations if l.name == name), None)
        if loc is not None:
            ft.context.page.run_task(load_forecast, loc)

    def on_select_variable(e):
        # The Canvas is rebuilt every render; just bumping the
        # variable state is enough to redraw the chart with the new
        # series.
        set_variable(e.control.value)

    # ── JMA / AMeDAS settings (per-location) ─────────────────────
    async def _open_settings(loc: Location):
        """Populate the per-location settings dialog. Pulls the
        AMeDAS station table + JMA area table once (both cached on
        disk for 7-30 days), computes the nearest 30 AMeDAS stations
        to the location, and auto-resolves the JMA office code. The
        dialog stays in a 'loading…' state until this completes."""
        set_settings_target(loc)
        set_settings_data(None)
        set_settings_err("")
        set_settings_loading(True)
        set_settings_tz(loc.timezone)
        try:
            amedas_svc = JmaAmedasService(data_dir=data_dir)
            jma_svc = JmaForecastService(data_dir=data_dir)
            stations_dict = await amedas_svc.stations()
            near = nearest_stations(
                stations_dict, loc.latitude, loc.longitude, limit=30,
            )
            area_table = await jma_svc.area_table()
            auto_res = await jma_svc.resolve_area(
                loc.latitude, loc.longitude, stations_dict,
            )
            # Office hierarchy: keep entries that have a parent (i.e.
            # actual prefectural/regional forecast offices), drop
            # higher-level 'centers' which the forecast endpoint won't
            # accept.
            offices_raw = area_table.get("offices") or {}
            offices = sorted([
                (str(code), str(info.get("name") or code))
                for code, info in offices_raw.items()
                if info.get("parent")
            ], key=lambda kv: kv[0])
            data = {
                "offices": offices,
                "near_stations": near,
                "auto_office": (
                    auto_res.office_code if auto_res else ""
                ),
                "auto_office_name": (
                    auto_res.office_name if auto_res else ""
                ),
            }
            set_settings_data(data)
            # Form defaults: saved value if any, else auto-pick.
            set_settings_office(
                loc.jma_area_code
                or (auto_res.office_code if auto_res else ""),
            )
            saved_ids = list(loc.amedas_station_ids)
            near_ids = [s.station_id for s, _ in near]
            set_settings_a1(
                saved_ids[0] if len(saved_ids) > 0
                else (near_ids[0] if len(near_ids) > 0 else ""),
            )
            set_settings_a2(
                saved_ids[1] if len(saved_ids) > 1
                else (near_ids[1] if len(near_ids) > 1 else ""),
            )
            set_settings_a3(
                saved_ids[2] if len(saved_ids) > 2
                else (near_ids[2] if len(near_ids) > 2 else ""),
            )
        except Exception as exc:
            logger.exception("Settings dialog data load failed")
            set_settings_err(
                f"設定データの取得に失敗: {type(exc).__name__}: {exc}",
            )
        finally:
            set_settings_loading(False)

    def _close_settings():
        set_settings_target(None)
        set_settings_data(None)
        set_settings_err("")

    def _save_settings():
        loc = settings_target
        if loc is None:
            return
        tz_input = settings_tz.strip()
        if tz_input:
            try:
                ZoneInfo(tz_input)
            except ZoneInfoNotFoundError:
                set_settings_err(f"未知の timezone: {tz_input}")
                return
        ids = tuple(
            s for s in (settings_a1, settings_a2, settings_a3) if s
        )
        # Dedupe while preserving order — picking the same station
        # twice would just waste a card slot.
        seen: set[str] = set()
        ids_dedup: list[str] = []
        for s in ids:
            if s in seen:
                continue
            seen.add(s)
            ids_dedup.append(s)
        updated = loc.with_jma_settings(
            jma_area_code=settings_office or "",
            amedas_station_ids=tuple(ids_dedup),
            timezone_name=tz_input or loc.timezone,
        )
        new_list = [
            updated if l.name == loc.name else l for l in locations
        ]
        save_locations(data_dir, new_list)
        set_locations(new_list)
        _close_settings()

    def on_settings_click(loc: Location):
        ft.context.page.run_task(_open_settings, loc)

    # ── Add-location dialog ──────────────────────────────────────
    # Built inline in render() per flet-declarative: the AlertDialog
    # is a frozen-diff descendant of state, so reconstructing each
    # render is the idiomatic pattern (the framework keeps cursor
    # state in the TextFields across re-renders via the use_dialog
    # hook).
    def _submit_new_location():
        try:
            lat = float(new_lat)
            lon = float(new_lon)
        except ValueError:
            set_new_err("緯度・経度は数値で入力してください")
            return
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            set_new_err("緯度 -90..90 / 経度 -180..180 の範囲で入力")
            return
        # ZoneInfo accepts the IANA name; reject early if malformed
        # so the user sees the error here, not 8 layers down the
        # chart pipeline.
        tz_input = new_tz.strip()
        if tz_input:
            try:
                ZoneInfo(tz_input)
            except ZoneInfoNotFoundError:
                set_new_err(f"未知の timezone: {tz_input}")
                return
        name = new_name.strip() or f"{lat:.2f},{lon:.2f}"
        loc = Location.new(
            name=name, latitude=lat, longitude=lon,
            timezone_name=tz_input or None,
        )
        set_show_dialog(False)
        _reset_dialog()
        ft.context.page.run_task(add_location_flow, loc)

    add_dialog = ft.AlertDialog(
        modal=True,
        title=ft.Text("地点を追加"),
        content=ft.Column(tight=True, controls=[
            ft.TextField(
                label="場所の名前 / Name",
                value=new_name,
                autofocus=True,
                on_change=lambda e: set_new_name(e.control.value),
            ),
            ft.TextField(
                label="緯度 / Latitude (-90..90)",
                value=new_lat,
                keyboard_type=ft.KeyboardType.NUMBER,
                on_change=lambda e: set_new_lat(e.control.value),
            ),
            ft.TextField(
                label="経度 / Longitude (-180..180)",
                value=new_lon,
                keyboard_type=ft.KeyboardType.NUMBER,
                on_change=lambda e: set_new_lon(e.control.value),
            ),
            ft.TextField(
                label="タイムゾーン / Timezone (IANA, 空欄で自動)",
                value=new_tz,
                hint_text="Asia/Tokyo, Europe/London, America/New_York …",
                on_change=lambda e: set_new_tz(e.control.value),
            ),
            ft.Text(new_err, color=ft.Colors.RED, size=12) if new_err
            else ft.Container(height=0),
        ]),
        actions=[
            ft.TextButton(
                "キャンセル",
                on_click=lambda _: _cancel_new_location(),
            ),
            ft.FilledButton(
                "追加",
                on_click=lambda _: _submit_new_location(),
            ),
        ],
    ) if show_dialog else None
    ft.use_dialog(add_dialog)

    # ── Per-location JMA / AMeDAS settings dialog ────────────────
    def _station_label(s: AmedasStation, dist_km: float) -> str:
        # Append the station-type letter and distance so the user has
        # something to break ties on when two nearby stations have
        # the same name. Type 'A' = full instrument; 'B' lacks
        # temperature; rainfall-only stations show only rain. The
        # cards in the overview gracefully degrade when a chosen
        # station doesn't publish a given variable.
        type_tag = f" [{s.station_type}]" if s.station_type else ""
        return f"{s.name_kanji}{type_tag} ({dist_km:.1f} km)"

    settings_dialog: ft.Control | None = None
    if settings_target is not None:
        if settings_loading or settings_data is None:
            settings_body: list[ft.Control] = [
                ft.Row(controls=[
                    ft.ProgressRing(width=16, height=16),
                    ft.Text("JMA / AMeDAS の選択肢を読み込み中…"),
                ]),
            ]
            if settings_err:
                settings_body.append(
                    ft.Text(settings_err, color=ft.Colors.RED, size=12),
                )
        else:
            near = settings_data["near_stations"]
            offices = settings_data["offices"]
            auto_office_code = settings_data.get("auto_office", "")
            auto_office_name = settings_data.get("auto_office_name", "")
            station_options = [
                ft.dropdown.Option(key="", text="（指定なし）")
            ] + [
                ft.dropdown.Option(
                    key=s.station_id,
                    text=_station_label(s, dist_km),
                )
                for s, dist_km in near
            ]
            office_options = [
                ft.dropdown.Option(key="", text="（指定なし）")
            ] + [
                ft.dropdown.Option(key=code, text=f"{code} {name}")
                for code, name in offices
            ]
            auto_hint = (
                f"自動推定: {auto_office_code} {auto_office_name}"
                if auto_office_code else "自動推定: なし"
            )
            settings_body = [
                ft.Text(
                    f"{settings_target.name}  "
                    f"({settings_target.latitude:.3f}, "
                    f"{settings_target.longitude:.3f})",
                    size=12, color=ft.Colors.GREY,
                ),
                ft.TextField(
                    label="タイムゾーン (IANA)",
                    value=settings_tz,
                    hint_text="Asia/Tokyo, Europe/London …",
                    on_change=lambda e: set_settings_tz(e.control.value),
                ),
                ft.Divider(),
                ft.Text("JMA 府県天気予報",
                        weight=ft.FontWeight.BOLD, size=13),
                ft.Text(auto_hint, size=11, color=ft.Colors.GREY),
                ft.Dropdown(
                    label="予報エリア (office code)",
                    value=settings_office,
                    options=office_options,
                    on_select=lambda e: set_settings_office(
                        e.control.value or "",
                    ),
                    width=380,
                ),
                ft.Divider(),
                ft.Text("AMeDAS 観測所 (最大 3 か所、近い順から選択)",
                        weight=ft.FontWeight.BOLD, size=13),
                ft.Dropdown(
                    label="1 番目",
                    value=settings_a1,
                    options=station_options,
                    on_select=lambda e: set_settings_a1(
                        e.control.value or "",
                    ),
                    width=380,
                ),
                ft.Dropdown(
                    label="2 番目",
                    value=settings_a2,
                    options=station_options,
                    on_select=lambda e: set_settings_a2(
                        e.control.value or "",
                    ),
                    width=380,
                ),
                ft.Dropdown(
                    label="3 番目",
                    value=settings_a3,
                    options=station_options,
                    on_select=lambda e: set_settings_a3(
                        e.control.value or "",
                    ),
                    width=380,
                ),
            ]
            if not settings_target.is_japan:
                settings_body.insert(2, ft.Text(
                    "※ この地点は日本国外のため JMA / AMeDAS は"
                    "取得できません。タイムゾーンのみ編集できます。",
                    size=11, color=ft.Colors.ORANGE,
                ))
            if settings_err:
                settings_body.append(
                    ft.Text(settings_err, color=ft.Colors.RED, size=12),
                )

        settings_dialog = ft.AlertDialog(
            modal=True,
            title=ft.Text(f"設定: {settings_target.name}"),
            content=ft.Column(
                controls=settings_body, tight=True, scroll=ft.ScrollMode.AUTO,
                height=520, width=420,
            ),
            actions=[
                ft.TextButton(
                    "キャンセル", on_click=lambda _: _close_settings(),
                ),
                ft.FilledButton(
                    "保存", on_click=lambda _: _save_settings(),
                    disabled=settings_loading,
                ),
            ],
        )
    ft.use_dialog(settings_dialog)

    # ── render branches ─────────────────────────────────────────

    header = ft.Row(
        controls=[
            ft.Text("地点予報 / Point forecast", size=18,
                    weight=ft.FontWeight.BOLD),
            ft.Container(expand=True),
            ft.FilledButton(
                "＋ 場所を追加",
                on_click=lambda _: set_show_dialog(True),
            ),
        ],
    )

    if not locations:
        return ft.Column(controls=[
            header,
            ft.Text(
                "まだ場所が登録されていません。右上の「場所を追加」から、"
                "緯度経度を入力して始めてください。",
                color=ft.Colors.GREY,
            ),
            ft.Text(
                "Open-Meteo の HRES 9km 予報と過去 30 年の ERA5 を組み合わせて、"
                "予報値・平年値・予報の不確実性を 1 つの画面に重ねます。",
                color=ft.Colors.GREY, size=12,
            ),
        ])

    # ``ft.Dropdown`` in Flet 0.85 fires ``on_select`` (not ``on_change`` —
    # that's the NavigationBar / TextField shape). The callback receives
    # an event whose ``control.value`` is the selected option's key.
    location_picker = ft.Dropdown(
        value=selected_name,
        options=[
            ft.dropdown.Option(key=loc.name, text=loc.name)
            for loc in locations
        ],
        on_select=on_select_location,
        width=240,
    )

    variable_picker = ft.Dropdown(
        value=variable,
        options=[
            ft.dropdown.Option(key=k, text=label)
            for k, label in _CHART_VARIABLES
        ],
        on_select=on_select_variable,
        width=200,
    )

    # 'Last updated' caption — auto-refresh runs every 3 h so the
    # analyst usually doesn't touch the manual refresh button. We
    # still expose it as a small icon button rather than a primary
    # FilledButton so it doesn't dominate the toolbar.
    if last_fetched_at is not None:
        updated_caption = ft.Text(
            f"最終更新: {last_fetched_at:%H:%M}",
            size=11, color=ft.Colors.GREY,
        )
    else:
        updated_caption = ft.Text("", size=11)

    rows: list[ft.Control] = [
        header,
        ft.Row(controls=[
            location_picker,
            variable_picker,
            ft.IconButton(
                icon=ft.Icons.REFRESH,
                tooltip="再取得 / Refresh now",
                on_click=lambda _: (
                    ft.context.page.run_task(load_forecast, selected_location)
                    if selected_location else None
                ),
            ),
            ft.IconButton(
                icon=ft.Icons.DOWNLOAD,
                tooltip="PNG ダウンロード (matplotlib)",
                on_click=on_download_click,
                disabled=(
                    forecast_data is None or variable == _OVERVIEW_KEY
                ),
            ),
            ft.IconButton(
                icon=ft.Icons.SETTINGS,
                tooltip="この地点の設定 (タイムゾーン / JMA / AMeDAS)",
                on_click=lambda _: (
                    on_settings_click(selected_location)
                    if selected_location else None
                ),
                disabled=selected_location is None,
            ),
            updated_caption,
        ]),
    ]
    if download_error:
        rows.append(ft.Text(
            f"保存に失敗しました: {download_error}",
            color=ft.Colors.RED, size=11,
        ))

    if archive_progress is not None:
        done, total = archive_progress
        rows.append(ft.Row(controls=[
            ft.ProgressRing(width=16, height=16),
            ft.Text(
                f"過去 30 年のデータを構築中… {done} / {total} 年",
                color=ft.Colors.GREY,
            ),
        ]))

    if forecast_state == "fetching":
        rows.append(ft.Row(controls=[
            ft.ProgressRing(width=16, height=16),
            ft.Text(
                f"{selected_name} の予報を取得中…",
                color=ft.Colors.GREY,
            ),
        ]))
    elif forecast_state == "error":
        rows.append(ft.Text(
            f"予報の取得に失敗しました: {error_msg}",
            color=ft.Colors.RED,
        ))
    elif (
        forecast_state == "ready" and forecast_data is not None
        and variable == _OVERVIEW_KEY
    ):
        # 概要 (overview) view: classic at-a-glance summary cards
        # only — no chart, no per-variable controls. Top is the
        # weekly strip (one card per local day for ~7 days), below
        # is the hour-by-hour strip for the next 48 h so the user
        # can see how the day actually unfolds. Both pivot off the
        # location's timezone so day boundaries match the local
        # clock, not UTC.
        if selected_location is not None:
            rows.append(_forecast_summary_strip(
                forecast_data.hres_df, selected_location,
            ))
            rows.append(ft.Divider())
            rows.append(_hourly_forecast_strip(
                forecast_data.hres_df, selected_location, hours=48,
            ))
            rows.append(ft.Divider())
            # JMA / AMeDAS cards — JP locations only. While the side-
            # fetch is still in flight we show a quiet placeholder so
            # the user knows more data is coming, rather than a blank
            # space that looks finished. Errors land here too instead
            # of replacing the main forecast.
            if selected_location.is_japan:
                if jma_overview_data is None and not jma_overview_error:
                    rows.append(ft.Row(controls=[
                        ft.ProgressRing(width=14, height=14),
                        ft.Text(
                            "JMA / AMeDAS のデータを取得中…",
                            size=12, color=ft.Colors.GREY,
                        ),
                    ]))
                if jma_overview_error:
                    rows.append(ft.Text(
                        f"JMA / AMeDAS の取得に失敗: {jma_overview_error}",
                        color=ft.Colors.RED, size=12,
                    ))
                if jma_overview_data is not None:
                    if jma_overview_data["stations"]:
                        rows.append(_amedas_card(
                            jma_overview_data["stations"],
                            jma_overview_data["snapshot_time"],
                            selected_location.latitude,
                            selected_location.longitude,
                        ))
                    if jma_overview_data["forecast"] is not None:
                        rows.append(_jma_forecast_card(
                            jma_overview_data["forecast"],
                        ))

    elif forecast_state == "ready" and forecast_data is not None:
        # Day-range buttons. Active choice = FilledButton (high
        # contrast), inactive = OutlinedButton — Material has no
        # native SegmentedButton in Flet 0.85, so this row-of-
        # buttons pattern fills the role. Day-range change resets
        # the pan offset so the new range starts centred on 'now'.
        def _on_day_click(n: int):
            set_visible_days(n)
            set_pan_offset_h(0)

        def _day_button(n: int) -> ft.Control:
            label = "全期間" if n >= 15 else f"{n}日"
            if visible_days == n:
                return ft.FilledButton(
                    label,
                    on_click=lambda _, days=n: _on_day_click(days),
                )
            return ft.OutlinedButton(
                label,
                on_click=lambda _, days=n: _on_day_click(days),
            )

        # 全期間 (15日) shows the entire data range and ignores both
        # pan offset and date picker — disable those controls so the
        # UI doesn't pretend they work in that mode.
        zoomed_in = visible_days < 15
        pan_step_h = max(6, int(visible_days * 12))

        # Pre-compute the date set + current anchor so both the
        # toolbar pan buttons and the bottom day-jump strip can
        # reach them.
        now_utc = datetime.now(timezone.utc).replace(
            minute=0, second=0, microsecond=0,
        )
        available_dates: list[date] = []
        anchor_date = (now_utc + timedelta(hours=pan_offset_h)).date()
        if zoomed_in and not forecast_data.hres_df.is_empty():
            ts_col = forecast_data.hres_df["timestamp"]
            cur_d = ts_col.min().date()
            d_max = ts_col.max().date()
            while cur_d <= d_max:
                available_dates.append(cur_d)
                cur_d += timedelta(days=1)

        rows.append(ft.Row(controls=[
            ft.Text("表示日数:", size=12, color=ft.Colors.GREY),
            _day_button(1),
            _day_button(3),
            _day_button(7),
            _day_button(15),
            ft.Container(width=20),
            ft.IconButton(
                icon=ft.Icons.CHEVRON_LEFT,
                tooltip=f"← {pan_step_h}時間前",
                on_click=lambda _: set_pan_offset_h(pan_offset_h - pan_step_h),
                disabled=not zoomed_in,
            ),
            ft.IconButton(
                icon=ft.Icons.MY_LOCATION,
                tooltip="現在に戻す",
                on_click=lambda _: set_pan_offset_h(0),
                disabled=(not zoomed_in) or pan_offset_h == 0,
            ),
            ft.IconButton(
                icon=ft.Icons.CHEVRON_RIGHT,
                tooltip=f"→ {pan_step_h}時間後",
                on_click=lambda _: set_pan_offset_h(pan_offset_h + pan_step_h),
                disabled=not zoomed_in,
            ),
        ]))

        # Visible-window calculation. Base: 'now' sits at 25 % from
        # the left so the analyst sees a slice of the past for
        # context and most of the chart for the forecast they
        # actually care about. ``pan_offset_h`` then shifts the
        # whole window left/right when the user clicks the
        # pan buttons. 全期間 (15日) ignores both rules and shows
        # the data's full extent.
        # (now_utc was already computed above for the date dropdown.)
        if visible_days >= 15:
            visible_window = None  # full range
        else:
            span = timedelta(days=visible_days)
            anchor = now_utc + timedelta(hours=pan_offset_h)
            visible_window = (
                anchor - span * 0.25,
                anchor + span * 0.75,
            )

        # Canvas width fits the typical desktop viewport so we don't
        # have to rely on horizontal scrolling — pan buttons above
        # navigate longer ranges instead. 1400 px is a balance
        # between 'wide enough for 7-day view' and 'fits a 1366-px
        # laptop screen'; ListView-based scroll didn't render a
        # usable scrollbar on Flet 0.85 so the canvas now lives
        # inside the regular column without a wrapper.
        canvas_width = 1400

        chart_canvas = build_point_forecast_canvas(
            location_name=forecast_data.location_name,
            variable=variable,
            hres_joined=forecast_data.hres_df,
            msm_df=forecast_data.msm_df,
            ensemble_quantiles=forecast_data.ensemble_quantiles,
            now_utc=now_utc,
            visible_window=visible_window,
            width=canvas_width,
        )
        rows.append(ft.Container(
            content=chart_canvas,
            width=canvas_width,
            height=700,
            padding=ft.Padding.symmetric(vertical=8, horizontal=0),
        ))

        # Day-jump strip — one button per calendar day in the
        # forecast range, placed under the chart so the analyst can
        # click straight to a specific date instead of stepping
        # ◀ / ▶. Active = anchor date (recentred on noon UTC),
        # today is also distinguished so it's findable at a glance.
        # Hidden in 全期間 mode because pan/anchor are no-ops there.
        if zoomed_in and available_dates:
            today = now_utc.date()

            def _on_day_jump(d: date):
                new_anchor = datetime(
                    d.year, d.month, d.day, 12, 0, tzinfo=timezone.utc,
                )
                delta_h = int(
                    (new_anchor - now_utc).total_seconds() / 3600,
                )
                set_pan_offset_h(delta_h)

            day_jump_controls: list[ft.Control] = []
            for d in available_dates:
                label = (
                    f"{d:%m-%d}"
                    + ("\n今日" if d == today else "")
                )
                if d == anchor_date:
                    btn = ft.FilledButton(
                        label,
                        on_click=lambda _, dd=d: _on_day_jump(dd),
                    )
                else:
                    btn = ft.OutlinedButton(
                        label,
                        on_click=lambda _, dd=d: _on_day_jump(dd),
                    )
                day_jump_controls.append(btn)
            rows.append(ft.Row(
                controls=day_jump_controls,
                wrap=True,
                spacing=4,
                run_spacing=4,
            ))
        rows.append(ft.Divider())

        # Copy button — replaces the on-screen hourly DataTable. The
        # user explicitly asked to drop the table list in favour of
        # a 'copy to clipboard' control so they can paste the raw
        # numbers wherever they actually want to work on them.
        rows.append(ft.Row(controls=[
            ft.OutlinedButton(
                "時間別データをコピー (TSV)",
                icon=ft.Icons.CONTENT_COPY,
                on_click=on_copy_hourly,
            ),
            ft.Text(copy_msg, color=ft.Colors.GREEN, size=12)
            if copy_msg else ft.Container(width=0),
        ]))

        # Daily summary: per-day forecast aggregates + climatology
        # mean + 30-y records for the same calendar day. Replaces
        # the previous hourly table.
        if selected_location is not None:
            rows.append(_daily_summary_table(
                forecast_data.hres_df,
                variable,
                data_dir,
                selected_location,
            ))

    rows.append(ft.Text(
        "Weather data by Open-Meteo (CC-BY 4.0).  ECMWF IFS HRES + ENS, ERA5 reanalysis, JMA MSM.",
        size=10, color=ft.Colors.GREY,
    ))

    return ft.Column(
        controls=rows,
        scroll=ft.ScrollMode.AUTO,
        expand=True,
    )
