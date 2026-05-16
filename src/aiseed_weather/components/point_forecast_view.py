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
from datetime import date, datetime
from pathlib import Path

import flet as ft
import httpx
import polars as pl

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


# ── Add-location dialog ─────────────────────────────────────────────


def _build_add_location_dialog(
    on_submit, on_cancel,
) -> ft.AlertDialog:
    """Modal that takes a name + lat + lon and calls ``on_submit(loc)``.

    Kept as a plain factory rather than a @ft.component because
    ``ft.use_dialog`` wants an AlertDialog directly, and the form
    state is small enough not to justify its own component.
    """
    name_field = ft.TextField(label="場所の名前 / Name", autofocus=True)
    lat_field = ft.TextField(
        label="緯度 / Latitude (-90..90)", keyboard_type=ft.KeyboardType.NUMBER,
    )
    lon_field = ft.TextField(
        label="経度 / Longitude (-180..180)", keyboard_type=ft.KeyboardType.NUMBER,
    )
    error_text = ft.Text("", color=ft.Colors.RED, size=12)

    def _submit(_e=None):
        try:
            lat = float(lat_field.value or "")
            lon = float(lon_field.value or "")
        except ValueError:
            error_text.value = "緯度・経度は数値で入力してください"
            return
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            error_text.value = "緯度 -90..90 / 経度 -180..180 の範囲で入力"
            return
        name = (name_field.value or "").strip() or f"{lat:.2f},{lon:.2f}"
        on_submit(Location.new(name=name, latitude=lat, longitude=lon))

    return ft.AlertDialog(
        modal=True,
        title=ft.Text("地点を追加"),
        content=ft.Column(
            tight=True,
            controls=[name_field, lat_field, lon_field, error_text],
        ),
        actions=[
            ft.TextButton("キャンセル", on_click=lambda _: on_cancel()),
            ft.FilledButton("追加", on_click=_submit),
        ],
    )


# ── Forecast-table renderer ────────────────────────────────────────


def _format_value(value, *, fmt: str = ".1f") -> str:
    if value is None:
        return "—"
    try:
        return format(float(value), fmt)
    except (TypeError, ValueError):
        return "—"


def _forecast_table(df: pl.DataFrame, label: str) -> ft.Control:
    """Render a forecast DataFrame as a Flet DataTable.

    Columns shown: timestamp, temp, precip, wind, RH, cloud. If the
    DataFrame carries the climatology join columns
    (``temperature_2m_mean`` / ``_std``), an extra two columns are
    shown so the analyst can read anomaly + Z-score inline.
    """
    if df.is_empty():
        return ft.Text(f"{label}: データなし", color=ft.Colors.GREY)

    has_clim = "temperature_2m_mean" in df.columns

    header_cells = [
        ft.DataColumn(ft.Text("時刻 UTC")),
        ft.DataColumn(ft.Text("℃")),
        ft.DataColumn(ft.Text("mm/h")),
        ft.DataColumn(ft.Text("RH %")),
        ft.DataColumn(ft.Text("風 m/s")),
        ft.DataColumn(ft.Text("雲量 %")),
    ]
    if has_clim:
        header_cells.extend([
            ft.DataColumn(ft.Text("平年℃")),
            ft.DataColumn(ft.Text("Δ℃ (Z)")),
        ])

    rows: list[ft.DataRow] = []
    # Cap row count so the page doesn't render hundreds of rows. The
    # chart view (next iteration) will replace this anyway.
    for row in df.head(48).iter_rows(named=True):
        ts: datetime = row["timestamp"]
        cells = [
            ft.DataCell(ft.Text(ts.strftime("%m-%d %H:%M"))),
            ft.DataCell(ft.Text(_format_value(row.get("temperature_2m")))),
            ft.DataCell(ft.Text(_format_value(row.get("precipitation"), fmt=".1f"))),
            ft.DataCell(ft.Text(_format_value(row.get("relative_humidity_2m"), fmt=".0f"))),
            ft.DataCell(ft.Text(_format_value(row.get("wind_speed_10m"), fmt=".1f"))),
            ft.DataCell(ft.Text(_format_value(row.get("cloud_cover"), fmt=".0f"))),
        ]
        if has_clim:
            clim_mean = row.get("temperature_2m_mean")
            clim_std = row.get("temperature_2m_std")
            forecast_t = row.get("temperature_2m")
            if clim_mean is not None and forecast_t is not None:
                delta = float(forecast_t) - float(clim_mean)
                z = delta / float(clim_std) if clim_std else None
                anomaly_text = (
                    f"{delta:+.1f} ({z:+.1f}σ)"
                    if z is not None else f"{delta:+.1f}"
                )
            else:
                anomaly_text = "—"
            cells.extend([
                ft.DataCell(ft.Text(_format_value(clim_mean))),
                ft.DataCell(ft.Text(anomaly_text)),
            ])
        rows.append(ft.DataRow(cells=cells))

    return ft.Column(
        controls=[
            ft.Text(label, size=16, weight=ft.FontWeight.BOLD),
            ft.DataTable(columns=header_cells, rows=rows),
        ],
    )


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

    archive_progress, set_archive_progress = ft.use_state(None)
    # ``None`` when no archive build is running; otherwise (done, total)

    show_dialog, set_show_dialog = ft.use_state(False)

    # Chart state. ``variable`` drives which value series is plotted;
    # ``chart_png`` holds the rendered matplotlib bytes for ft.Image.
    # We re-render the chart whenever the variable or the snapshot
    # changes (kicked off from the corresponding async handler).
    variable, set_variable = ft.use_state(_CHART_VARIABLES[0][0])
    chart_png, set_chart_png = ft.use_state(None)

    selected_location = next(
        (loc for loc in locations if loc.name == selected_name),
        None,
    )

    # ── async handlers ────────────────────────────────────────────

    async def _render_chart_for(snap: _ForecastSnapshot, var: str) -> None:
        """Render the chart PNG off-loop and stash it in use_state.
        Idempotent — safe to call after a variable change or a new
        snapshot."""
        try:
            png = await asyncio.to_thread(
                render_point_forecast,
                location_name=snap.location_name,
                variable=var,
                hres_joined=snap.hres_df,
                msm_df=snap.msm_df,
                ensemble_quantiles=snap.ensemble_quantiles,
            )
            set_chart_png(png)
        except Exception:
            logger.exception("Chart render failed (variable=%s)", var)
            set_chart_png(None)

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
            logger.info("load_forecast: state=ready, rendering chart")
            # Render the chart for the currently-selected variable. The
            # render runs on a thread, so the 'ready' state has already
            # painted the table by the time the chart PNG lands.
            await _render_chart_for(snap, variable)
            logger.info("load_forecast: chart rendered")
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

    # Auto-fetch when selection changes
    def on_select_location(e):
        name = e.control.value
        set_selected_name(name)
        loc = next((l for l in locations if l.name == name), None)
        if loc is not None:
            ft.context.page.run_task(load_forecast, loc)

    def on_select_variable(e):
        new_var = e.control.value
        set_variable(new_var)
        if forecast_data is not None:
            ft.context.page.run_task(_render_chart_for, forecast_data, new_var)

    # ── dialog (declarative) ─────────────────────────────────────
    dialog = _build_add_location_dialog(
        on_submit=lambda loc: ft.context.page.run_task(add_location_flow, loc),
        on_cancel=lambda: set_show_dialog(False),
    ) if show_dialog else None
    ft.use_dialog(dialog)

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

    rows: list[ft.Control] = [
        header,
        ft.Row(controls=[
            location_picker,
            variable_picker,
            ft.FilledButton(
                "更新",
                on_click=lambda _: (
                    ft.context.page.run_task(load_forecast, selected_location)
                    if selected_location else None
                ),
            ),
        ]),
    ]

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
        # Chart first — primary visualisation per spec step 10. The
        # PNG bytes land asynchronously after the data fetch
        # completes; while it's None the spinner-style placeholder
        # keeps the table readable so the analyst isn't blocked.
        if chart_png is not None:
            rows.append(ft.Container(
                content=ft.Image(
                    src=chart_png, fit=ft.BoxFit.CONTAIN,
                ),
                padding=ft.Padding.symmetric(vertical=8, horizontal=0),
            ))
        else:
            rows.append(ft.Row(controls=[
                ft.ProgressRing(width=14, height=14),
                ft.Text("チャートを描画中…", color=ft.Colors.GREY),
            ]))
        rows.append(ft.Divider())
        rows.append(_forecast_table(
            forecast_data.hres_df, forecast_data.hres_label,
        ))
        if forecast_data.msm_df is not None and forecast_data.msm_label:
            rows.append(ft.Divider())
            rows.append(_forecast_table(
                forecast_data.msm_df, forecast_data.msm_label,
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
