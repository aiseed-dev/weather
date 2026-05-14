# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Yasuhiro / AIseed

"""Map view: ECMWF synoptic chart.

Pipeline (only after the user presses 取得 / Fetch):
  probe latest run via ForecastService.latest_run → download GRIB2 →
  decode via cfgrib → render matplotlib figure → embed as PNG bytes.

No mount-time fetch — the view stays idle until the user asks. See the
`user-action-fetch` skill.
"""

from __future__ import annotations

import asyncio
import io
import logging
import time

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

logger = logging.getLogger(__name__)


def _figure_to_png_bytes(fig: Figure, *, dpi: int = 120) -> bytes:
    # Flet 0.85's ft.Image accepts raw bytes via src=; no base64 wrapping
    # needed. We render to PNG because matplotlib + Flet share no in-process
    # figure transport across Flet versions.
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=dpi, bbox_inches="tight")
    plt.close(fig)
    return buf.getvalue()


@ft.component
def MapView(settings: UserSettings):
    state, set_state = ft.use_state("idle")  # idle | loading | ready | error | disabled
    image_bytes, set_image_bytes = ft.use_state(None)
    error, set_error = ft.use_state(None)
    run_label, set_run_label = ft.use_state("")
    progress, set_progress = ft.use_state("")

    async def load(force: bool = False):
        set_state("loading")
        set_error(None)
        set_progress("Initializing forecast service…")
        try:
            service = ForecastService(settings)
        except ForecastDisabledError as e:
            set_error(str(e))
            set_state("disabled")
            return

        try:
            t0 = time.perf_counter()
            set_progress("Probing latest ECMWF run…")
            run_time = await service.latest_run(step_hours=0, param="msl")
            t_probe = time.perf_counter()
            label = f"{run_time:%Y%m%d %Hz} IFS"
            set_progress(f"Fetching MSL grid · {label}…")
            request = ForecastRequest(run_time=run_time, step_hours=0, param="msl")
            ds = await service.fetch(request)
            t_fetch = time.perf_counter()
            set_progress("Rendering chart (matplotlib + cartopy)…")
            fig = await asyncio.to_thread(
                render_msl, ds, projection="robinson", run_id=label,
            )
            t_render = time.perf_counter()
            set_progress("Encoding PNG…")
            png_bytes = await asyncio.to_thread(_figure_to_png_bytes, fig)
            t_encode = time.perf_counter()
            set_image_bytes(png_bytes)
            set_run_label(label)
            set_progress("")
            set_state("ready")
            logger.info(
                "Map load timing: probe=%.2fs fetch+decode=%.2fs render=%.2fs "
                "encode=%.2fs total=%.2fs",
                t_probe - t0,
                t_fetch - t_probe,
                t_render - t_fetch,
                t_encode - t_render,
                t_encode - t0,
            )
        except Exception as e:
            logger.exception("Map view failed to load forecast")
            set_error(f"{type(e).__name__}: {e}")
            set_state("error")

    if state == "disabled":
        return ft.Column(
            controls=[
                ft.Text("Map View (ECMWF)", size=18, weight=ft.FontWeight.BOLD),
                ft.Text(
                    error or "Forecast source is not configured.",
                    color=ft.Colors.GREY,
                ),
                ft.Text(
                    "Set forecast_source in config.toml to enable map views.",
                    color=ft.Colors.GREY,
                ),
            ],
        )

    if state == "idle":
        return ft.Column(
            controls=[
                ft.Text("Map View (ECMWF)", size=18, weight=ft.FontWeight.BOLD),
                ft.Text(
                    "MSL (mean sea level pressure) from the latest ECMWF IFS run.",
                    color=ft.Colors.GREY,
                ),
                ft.FilledButton(
                    content=ft.Text("取得 / Fetch"),
                    on_click=lambda _: ft.context.page.run_task(load),
                ),
            ],
        )

    if state == "loading":
        return ft.Column(
            controls=[
                ft.Text("Map View (ECMWF)", size=18, weight=ft.FontWeight.BOLD),
                ft.Row(
                    controls=[
                        ft.ProgressRing(width=20, height=20),
                        ft.Text(progress or "Loading…", color=ft.Colors.GREY),
                    ],
                    spacing=12,
                ),
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

    # state == "ready"
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
                    src=image_bytes,
                    fit=ft.BoxFit.CONTAIN,
                    expand=True,
                ),
                expand=True,
            ),
        ],
    )
