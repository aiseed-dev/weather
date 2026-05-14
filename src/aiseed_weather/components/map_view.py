# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Yasuhiro / AIseed

"""Map view: ECMWF synoptic chart.

Pipeline: pick latest published run → download MSL field via ForecastService
→ decode GRIB2 → render matplotlib figure → embed in Flet via
MatplotlibChart. Mount-time fetch + explicit Refresh, no polling
(`user-action-fetch` skill).
"""

from __future__ import annotations

import asyncio

import flet as ft
from flet.matplotlib_chart import MatplotlibChart

from aiseed_weather.figures.msl_chart import render_msl
from aiseed_weather.models.user_settings import UserSettings
from aiseed_weather.services.forecast_service import (
    ForecastDisabledError,
    ForecastRequest,
    ForecastService,
)
from aiseed_weather.services.run_selector import latest_available_run


@ft.component
def MapView(settings: UserSettings):
    state, set_state = ft.use_state("idle")  # idle | loading | ready | error | disabled
    figure, set_figure = ft.use_state(None)
    error, set_error = ft.use_state(None)
    run_label, set_run_label = ft.use_state("")

    async def load(force: bool = False):
        set_state("loading")
        try:
            service = ForecastService(settings)
        except ForecastDisabledError as e:
            set_error(str(e))
            set_state("disabled")
            return

        try:
            run_time = latest_available_run()
            label = f"{run_time:%Y%m%d %Hz} IFS"
            request = ForecastRequest(run_time=run_time, step_hours=0, param="msl")
            ds = await service.fetch(request)
            fig = await asyncio.to_thread(
                render_msl, ds, projection="robinson", run_id=label,
            )
            set_figure(fig)
            set_run_label(label)
            set_state("ready")
        except Exception as e:
            set_error(f"{type(e).__name__}: {e}")
            set_state("error")

    ft.use_effect(lambda: ft.run_task(load), deps=[])

    if state == "disabled":
        return ft.Column(
            controls=[
                ft.Text("Map View (ECMWF)", size=18, weight=ft.FontWeight.BOLD),
                ft.Text(
                    error or "Forecast source is not configured.",
                    color=ft.Colors.GREY,
                ),
                ft.Text(
                    "Pick an ECMWF mirror in Settings to enable map views.",
                    color=ft.Colors.GREY,
                ),
            ],
        )

    if state in ("idle", "loading"):
        return ft.Column(
            controls=[
                ft.Text("Map View (ECMWF)", size=18, weight=ft.FontWeight.BOLD),
                ft.ProgressRing(),
                ft.Text("Fetching MSL field…", color=ft.Colors.GREY),
            ],
        )

    if state == "error":
        return ft.Column(
            controls=[
                ft.Text("Map View (ECMWF)", size=18, weight=ft.FontWeight.BOLD),
                ft.Text(f"Could not render map: {error}", color=ft.Colors.RED),
                ft.FilledButton(
                    text="再取得 / Retry",
                    on_click=lambda _: ft.run_task(lambda: load(force=True)),
                ),
            ],
        )

    return ft.Column(
        expand=True,
        controls=[
            ft.Row(
                controls=[
                    ft.Text(
                        f"MSL · ECMWF IFS · {run_label}",
                        size=16, weight=ft.FontWeight.BOLD,
                    ),
                    ft.FilledButton(
                        text="再取得",
                        on_click=lambda _: ft.run_task(lambda: load(force=True)),
                    ),
                ],
                alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
            ),
            ft.Container(
                content=MatplotlibChart(figure=figure, expand=True, isolated=True),
                expand=True,
            ),
        ],
    )
