# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Yasuhiro / AIseed

"""First-run setup screen.

The user picks which data sources to use. The app does not preselect or
recommend anything; "None" is always an option. See the first-run-setup
skill for the full UX rules.
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


@ft.component
def SetupView(initial: UserSettings, on_complete: Callable[[UserSettings], None]):
    # Implementation note for the agent:
    # - Render three grouped Radio sections, one per source category
    # - Each section: "None" option listed first, then each available source
    # - Each source row shows: name, one-line description, license link
    # - Attribution checkbox at the bottom, required to enable Continue button
    # - On Continue: build a UserSettings with setup_completed=True and call on_complete
    # - Do not call data APIs from here. No probe requests.
    forecast, set_forecast = ft.use_state(initial.forecast_source)
    historical, set_historical = ft.use_state(initial.historical_source)
    point, set_point = ft.use_state(initial.point_source)
    accepted, set_accepted = ft.use_state(initial.accepted_attribution_terms)

    can_continue = accepted  # All "None" is a valid choice; only attribution gates entry.

    def handle_continue(_):
        new = replace(
            initial,
            forecast_source=forecast,
            historical_source=historical,
            point_source=point,
            accepted_attribution_terms=accepted,
            setup_completed=True,
        )
        on_complete(new)

    return ft.Container(
        padding=24,
        content=ft.Column(
            controls=[
                ft.Text("AIseed Weather — Setup", size=22, weight=ft.FontWeight.BOLD),
                ft.Text(
                    "Choose which public data sources you want this viewer to use. "
                    "You can change these later in Settings.",
                    size=14,
                ),
                # TODO(agent): render three grouped sections here.
                # See .agents/skills/first-run-setup/SKILL.md for the full spec.
                ft.Text(f"Forecast: {forecast.value}"),
                ft.Text(f"Historical: {historical.value}"),
                ft.Text(f"Point forecast: {point.value}"),
                ft.Checkbox(
                    label=(
                        "I understand that data shown by this app is licensed under "
                        "CC-BY-4.0. Exported figures include attribution automatically; "
                        "I will not remove it when sharing."
                    ),
                    value=accepted,
                    on_change=lambda e: set_accepted(e.control.value),
                ),
                ft.FilledButton(
                    content=ft.Text("Continue"),
                    on_click=handle_continue,
                    disabled=not can_continue,
                ),
            ],
            spacing=14,
        ),
    )