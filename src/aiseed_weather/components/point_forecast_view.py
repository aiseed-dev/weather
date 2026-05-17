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
_CHART_VARIABLES: tuple[tuple[str, str], ...] = (
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
    )]
    return df.select(keep).write_csv(separator="\t")


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
        # Climatology daily mean = average of the 24 hourly means
        if has_clim:
            clim = hourly_climatology(
                data_dir, location, int(row["d_month"]), int(row["d_day"]),
            )
            clim_col = f"{variable}_mean"
            if not clim.is_empty() and clim_col in clim.columns:
                clim_daily = clim[clim_col].mean()
                cells.append(ft.DataCell(
                    ft.Text(_format_value(clim_daily, fmt=".1f")),
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
    new_err, set_new_err = ft.use_state("")

    def _reset_dialog() -> None:
        set_new_name("")
        set_new_lat("")
        set_new_lon("")
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
        name = new_name.strip() or f"{lat:.2f},{lon:.2f}"
        loc = Location.new(name=name, latitude=lat, longitude=lon)
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
                disabled=forecast_data is None,
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
    elif forecast_state == "ready" and forecast_data is not None:
        # Day-range buttons. Active choice = FilledButton (high
        # contrast), inactive = OutlinedButton — Material has no
        # native SegmentedButton in Flet 0.85, so this row-of-
        # buttons pattern fills the role.
        def _day_button(n: int) -> ft.Control:
            label = "全期間" if n >= 15 else f"{n}日"
            if visible_days == n:
                return ft.FilledButton(
                    label,
                    on_click=lambda _: set_visible_days(n),
                )
            return ft.OutlinedButton(
                label,
                on_click=lambda _: set_visible_days(n),
            )

        rows.append(ft.Row(controls=[
            ft.Text("表示日数:", size=12, color=ft.Colors.GREY),
            _day_button(1),
            _day_button(3),
            _day_button(7),
            _day_button(15),
        ]))

        # Visible-window calculation: 'now' sits at 25% from the
        # left so the analyst sees a slice of the past for context
        # and most of the chart for the forecast they actually
        # care about. 全期間 (15日) falls back to the full
        # hres_joined range so we don't clip the past 3 days
        # ECMWF returns.
        now_utc = datetime.now(timezone.utc).replace(
            minute=0, second=0, microsecond=0,
        )
        if visible_days >= 15:
            visible_window = None  # full range
        else:
            span = timedelta(days=visible_days)
            visible_window = (
                now_utc - span * 0.25,
                now_utc + span * 0.75,
            )

        # Canvas width scales with visible days so short windows
        # actually fit the viewport. 180 px / day is a comfortable
        # density; the 1100 px floor keeps even '1日' from
        # collapsing to a useless sliver.
        canvas_width = max(1100, int(visible_days * 180))

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
        # Canvas height 700 — matches build_point_forecast_canvas's
        # new default and gives Y-axis gridlines room when variables
        # with fine steps (気温 2.5 °C, MSL 4 hPa) put 10-20 of them
        # on the panel.
        rows.append(ft.Container(
            content=ft.Row(
                controls=[
                    ft.Container(
                        content=chart_canvas,
                        width=canvas_width,
                        height=700,
                    ),
                ],
                scroll=ft.ScrollMode.AUTO,
            ),
            padding=ft.Padding.symmetric(vertical=8, horizontal=0),
            height=720,
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
