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
import base64
import io

import flet as ft
import matplotlib.pyplot as plt
from matplotlib.figure import Figure

from aiseed_weather.figures.msl_chart import render_msl
from aiseed_weather.models.user_settings import UserSettings
from aiseed_weather.services.forecast_service import (
    ForecastDisabledError,
    ForecastRequest,
    ForecastService,
)
from aiseed_weather.services.run_selector import latest_available_run


def _figure_to_png_b64(fig: Figure, *, dpi: int = 120) -> str:
    # The on-screen render goes through PNG because Flet's MatplotlibChart
    # bridge has moved to a separate package across Flet versions. PNG via
    # `ft.Image(src_base64=...)` is the version-agnostic path.
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=dpi, bbox_inches="tight")
    plt.close(fig)
    return base64.b64encode(buf.getvalue()).decode("ascii")


@ft.component
def MapView(settings: UserSettings):
    state, set_state = ft.use_state("idle")  # idle | loading | ready | error | disabled
    image_b64, set_image_b64 = ft.use_state(None)
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
            b64 = await asyncio.to_thread(_figure_to_png_b64, fig)
            set_image_b64(b64)
            set_run_label(label)
            set_state("ready")
        except Exception as e:
            set_error(f"{type(e).__name__}: {e}")
            set_state("error")

    ft.use_effect(lambda: ft.context.page.run_task(load), [])

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
                    content=ft.Text("再取得 / Retry"),
                    on_click=lambda _: ft.context.page.run_task(load, force=True),
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
                        content=ft.Text("再取得"),
                        on_click=lambda _: ft.context.page.run_task(load, force=True),
                    ),
                ],
                alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
            ),
            ft.Container(
                content=ft.Image(
                    src_base64=image_b64,
                    fit=ft.ImageFit.CONTAIN,
                    expand=True,
                ),
                expand=True,
            ),
        ],
    )
