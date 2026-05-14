# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Yasuhiro / AIseed

import flet as ft

from aiseed_weather.components.amedas_view import AmedasView
from aiseed_weather.components.map_view import MapView
from aiseed_weather.components.radar_view import RadarView
from aiseed_weather.components.setup_view import SetupView
from aiseed_weather.models import user_settings


@ft.component
def App():
    # Route at startup based on whether the user has completed first-run setup.
    # SafeArea at root keeps the app honest on mobile-style window sizes too,
    # even though the primary target is desktop.
    settings, set_settings = ft.use_state(user_settings.load())
    # Top-level navigation: which view the user is currently looking at.
    # Each view is responsible for its own data fetching (see user-action-fetch skill).
    active_view, set_active_view = ft.use_state("map")

    def handle_setup_done(new_settings):
        user_settings.save(new_settings)
        set_settings(new_settings)

    if not settings.setup_completed:
        return ft.SafeArea(
            expand=True,
            content=SetupView(initial=settings, on_complete=handle_setup_done),
        )

    body = {
        "map": lambda: MapView(settings=settings),
        "radar": lambda: RadarView(),
        "amedas": lambda: AmedasView(),
    }[active_view]()

    nav = ft.NavigationBar(
        selected_index={"map": 0, "radar": 1, "amedas": 2}[active_view],
        on_change=lambda e: set_active_view(
            ["map", "radar", "amedas"][e.control.selected_index],
        ),
        destinations=[
            ft.NavigationBarDestination(icon=ft.Icons.PUBLIC, label="Map"),
            ft.NavigationBarDestination(icon=ft.Icons.RADAR, label="Radar"),
            ft.NavigationBarDestination(icon=ft.Icons.SENSORS, label="AMeDAS"),
        ],
    )

    return ft.SafeArea(
        expand=True,
        content=ft.Column(
            expand=True,
            controls=[
                ft.Container(content=body, expand=True),
                nav,
            ],
        ),
    )
