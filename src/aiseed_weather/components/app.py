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

    # ──────────────────────────────────────────────────────────
    # App-level GPV fetch session.
    #
    # Lives at App scope (not MapView) so the running download
    # survives tab navigation. The Map view kicks off fetches but
    # the lifecycle is owned here; the global banner above the nav
    # shows progress + stop button from any tab.
    # ──────────────────────────────────────────────────────────
    fetch_running, set_fetch_running = ft.use_state(False)
    fetch_progress, set_fetch_progress = ft.use_state(
        lambda: {"done": 0, "total": 0}
    )
    fetch_status_text, set_fetch_status_text = ft.use_state("")
    # Mutable holder for the asyncio task + cancel_event. use_state
    # with a lazy initializer returns the same dict object across
    # renders, so writes survive.
    fetch_task_ref, _ = ft.use_state(
        lambda: {"task": None, "cancel_event": None}
    )

    def stop_fetch():
        ev = fetch_task_ref.get("cancel_event")
        if ev is not None:
            ev.set()
        task = fetch_task_ref.get("task")
        if task is not None and not task.done():
            task.cancel()
        fetch_task_ref["task"] = None
        fetch_task_ref["cancel_event"] = None
        set_fetch_running(False)
        set_fetch_status_text("")

    fetch_session = {
        "running": fetch_running,
        "set_running": set_fetch_running,
        "progress": fetch_progress,
        "set_progress": set_fetch_progress,
        "status_text": fetch_status_text,
        "set_status_text": set_fetch_status_text,
        "task_ref": fetch_task_ref,
        "stop": stop_fetch,
    }

    if result.status != "ok":
        return ft.SafeArea(expand=True, content=ConfigStatusPanel(result=result))

    settings = result.settings
    body = {
        "map": lambda: MapView(settings=settings, fetch_session=fetch_session),
        "radar": lambda: RadarView(settings=settings),
        "amedas": lambda: AmedasView(settings=settings),
    }[active_view]()

    # Global fetch banner — visible above the navigation bar
    # whenever a download is in flight, on ANY tab. Lets the user
    # walk away from the Map tab without losing visibility on the
    # running background fetch.
    done = fetch_progress.get("done", 0)
    total = fetch_progress.get("total", 0)
    fetch_banner = ft.Container(
        height=44,
        bgcolor=ft.Colors.PRIMARY_CONTAINER,
        padding=ft.Padding.symmetric(horizontal=16, vertical=4),
        visible=fetch_running,
        content=ft.Row(
            alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
            controls=[
                ft.Row(
                    spacing=10,
                    controls=[
                        ft.ProgressRing(
                            width=16, height=16,
                            color=ft.Colors.ON_PRIMARY_CONTAINER,
                        ),
                        ft.Text(
                            (
                                f"GPV 取得中  ·  {done}/{total} frames"
                                + (f"  ·  {fetch_status_text}" if fetch_status_text else "")
                            ),
                            size=12,
                            color=ft.Colors.ON_PRIMARY_CONTAINER,
                        ),
                    ],
                ),
                ft.FilledTonalButton(
                    "停止 / Stop",
                    icon=ft.Icons.STOP_CIRCLE,
                    on_click=lambda _: stop_fetch(),
                ),
            ],
        ),
    )

    nav = ft.NavigationBar(
        selected_index={"map": 0, "radar": 1, "amedas": 2}[active_view],
        on_change=lambda e: set_active_view(
            ["map", "radar", "amedas"][e.control.selected_index],
        ),
        destinations=[
            ft.NavigationBarDestination(
                icon=ft.Icons.PUBLIC, label="モデル / Models",
            ),
            ft.NavigationBarDestination(
                icon=ft.Icons.RADAR, label="ナウキャスト / Nowcast",
            ),
            ft.NavigationBarDestination(
                icon=ft.Icons.LOCATION_ON, label="地点 / Points",
            ),
        ],
    )

    return ft.SafeArea(
        expand=True,
        content=ft.Column(
            expand=True,
            spacing=0,
            controls=[
                ft.Container(content=body, expand=True),
                fetch_banner,
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
