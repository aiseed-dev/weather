# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Yasuhiro / AIseed

import flet as ft

from aiseed_weather.models.user_settings import UserSettings


@ft.component
def MapView(settings: UserSettings):
    # Placeholder. Next steps:
    #   1. Add LayerSelector child (msl / 2t / wind / precip / gh500)
    #   2. Add MapCanvas that takes (run_time, layer) and renders via matplotlib
    #   3. Add TimeSlider for animation across forecast steps
    #
    # Available data sources are determined by `settings`. If the user did not
    # configure a forecast source, this view should default to historical or
    # show an explanation pointing back to Settings.
    selected_layer, set_layer = ft.use_state("msl")

    mode_label = (
        "Forecast + Historical"
        if settings.has_forecast() and settings.has_historical()
        else "Forecast only"
        if settings.has_forecast()
        else "Historical only"
        if settings.has_historical()
        else "No sources configured"
    )

    return ft.Column(
        expand=True,
        controls=[
            ft.Text("AIseed Weather", size=20, weight=ft.FontWeight.BOLD),
            ft.Text(f"Mode: {mode_label}", color=ft.Colors.GREY),
            ft.Text(f"Selected layer: {selected_layer}"),
            ft.Text("Map will render here", color=ft.Colors.GREY),
        ],
    )
