# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Yasuhiro / AIseed

"""First-run setup screen.

Three Radio sections (Forecast / Historical / Point forecast) where "None"
is the first and default option in every section. JMA is per-feature, not
shown here. No data API is touched from this screen — the choice is just
persisted via models.user_settings. See the `first-run-setup` skill.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Callable

import flet as ft

from aiseed_weather.models.user_settings import (
    ForecastSource,
    HistoricalSource,
    PointForecastSource,
    UserSettings,
)


_FORECAST_OPTIONS = (
    (ForecastSource.NONE, "None", "Operate in historical / nowcast-only mode"),
    (ForecastSource.ECMWF_AWS, "ECMWF Open Data via AWS",
     "Anonymous, fastest globally. CC-BY-4.0."),
    (ForecastSource.ECMWF_AZURE, "ECMWF Open Data via Azure",
     "Anonymous mirror. CC-BY-4.0."),
    (ForecastSource.ECMWF_GCP, "ECMWF Open Data via GCP",
     "Often best from Asia-Pacific. CC-BY-4.0."),
    (ForecastSource.ECMWF_DIRECT, "ECMWF direct",
     "Official endpoint, 500-connection limit. CC-BY-4.0."),
)

_HISTORICAL_OPTIONS = (
    (HistoricalSource.NONE, "None", "No climatology / anomaly features"),
    (HistoricalSource.ERA5_AWS, "ERA5 via AWS",
     "Anonymous, ~5-day lag from real-time. CC-BY-4.0."),
    (HistoricalSource.ERA5_CDS, "ERA5 via Copernicus CDS",
     "Requires free Copernicus account, more flexible queries. CC-BY-4.0."),
)

_POINT_OPTIONS = (
    (PointForecastSource.NONE, "None", "Disable point-forecast view"),
    (PointForecastSource.OPEN_METEO, "Open-Meteo",
     "Public API, free for personal use. CC-BY-4.0."),
)


def _radio_event_value(e) -> str:
    # Flet 0.85 has cases where RadioGroup state arrives as e.data rather than
    # e.control.value; accept either to stay robust across releases.
    val = getattr(getattr(e, "control", None), "value", None)
    if isinstance(val, str) and val:
        return val
    return str(getattr(e, "data", ""))


def _section(title: str, options, selected_value: str, on_change) -> ft.Control:
    return ft.Container(
        padding=ft.padding.symmetric(vertical=8),
        content=ft.Column(
            spacing=6,
            controls=[
                ft.Text(title, size=16, weight=ft.FontWeight.BOLD),
                ft.RadioGroup(
                    value=selected_value,
                    on_change=on_change,
                    content=ft.Column(
                        spacing=2,
                        controls=[
                            ft.Radio(
                                value=enum_value.value,
                                label=f"{name} — {description}",
                            )
                            for enum_value, name, description in options
                        ],
                    ),
                ),
            ],
        ),
    )


@ft.component
def SetupView(initial: UserSettings, on_complete: Callable[[UserSettings], None]):
    forecast, set_forecast = ft.use_state(initial.forecast_source)
    historical, set_historical = ft.use_state(initial.historical_source)
    point, set_point = ft.use_state(initial.point_source)

    def handle_continue(_):
        # The attribution paragraph is on screen above the button — pressing
        # Continue is the explicit acceptance signal recorded here.
        new = replace(
            initial,
            forecast_source=forecast,
            historical_source=historical,
            point_source=point,
            accepted_attribution_terms=True,
            setup_completed=True,
        )
        on_complete(new)

    return ft.Container(
        padding=24,
        expand=True,
        content=ft.Column(
            scroll=ft.ScrollMode.AUTO,
            expand=True,
            spacing=10,
            controls=[
                ft.Text("AIseed Weather — Setup", size=22, weight=ft.FontWeight.BOLD),
                ft.Text(
                    "This app shows weather data from public sources. Choose which "
                    "sources you want to use. \"None\" is a valid choice in every "
                    "section — JMA radar and AMeDAS are available regardless.",
                    size=13,
                ),
                _section(
                    "Forecast source (ECMWF Open Data)",
                    _FORECAST_OPTIONS,
                    forecast.value,
                    lambda e: set_forecast(ForecastSource(_radio_event_value(e))),
                ),
                _section(
                    "Historical source (ERA5)",
                    _HISTORICAL_OPTIONS,
                    historical.value,
                    lambda e: set_historical(HistoricalSource(_radio_event_value(e))),
                ),
                _section(
                    "Point forecast source",
                    _POINT_OPTIONS,
                    point.value,
                    lambda e: set_point(PointForecastSource(_radio_event_value(e))),
                ),
                ft.Divider(height=1),
                ft.Text(
                    "Data shown by this app is licensed under CC-BY-4.0. Exported "
                    "figures include attribution automatically; do not remove it "
                    "when sharing. Pressing Continue confirms acceptance of these "
                    "terms.",
                    size=12, color=ft.Colors.GREY,
                ),
                ft.Row(
                    alignment=ft.MainAxisAlignment.END,
                    controls=[
                        ft.FilledButton(
                            content=ft.Text("Continue"),
                            on_click=handle_continue,
                        ),
                    ],
                ),
            ],
        ),
    )
