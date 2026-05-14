# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Yasuhiro / AIseed

import flet as ft

from aiseed_weather.components.amedas_view import AmedasView
from aiseed_weather.components.map_view import MapView
from aiseed_weather.components.radar_view import RadarView
from aiseed_weather.models import user_settings
from aiseed_weather.models.user_settings import LoadResult


@ft.component
def App():
    # Config is read once at startup. Editing the TOML and restarting is the
    # only way to change sources — see the `first-run-setup` skill.
    result, _ = ft.use_state(user_settings.load_or_init())
    active_view, set_active_view = ft.use_state("map")

    if result.status != "ok":
        return ft.SafeArea(expand=True, content=ConfigStatusPanel(result=result))

    settings = result.settings
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


@ft.component
def ConfigStatusPanel(result: LoadResult):
    if result.status == "created":
        title = "Config file created"
        title_color = ft.Colors.AMBER
        lines = [
            f"A template was written to:\n  {result.path}",
            'Edit it to choose data sources ("forecast_source", '
            '"historical_source", "point_source"), then restart the app.',
            'JMA radar and AMeDAS work even with every source set to "none".',
        ]
    else:  # "invalid"
        title = "Config file is invalid"
        title_color = ft.Colors.RED
        lines = [
            f"Path:\n  {result.path}",
            f"Reason:\n  {result.error}",
            "Fix the file and restart the app.",
        ]

    return ft.Container(
        padding=24,
        content=ft.Column(
            spacing=12,
            controls=[
                ft.Text(title, size=22, weight=ft.FontWeight.BOLD, color=title_color),
                *[ft.Text(line, size=13) for line in lines],
            ],
        ),
    )
