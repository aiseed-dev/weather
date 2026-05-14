# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Yasuhiro / AIseed

"""AMeDAS ground observation map view.

Stays idle on mount; fetches only when the user presses 取得 / Fetch.
Default variable is temperature; the user can switch to precipitation,
wind, sunshine, or snow depth. See the `user-action-fetch` skill.
"""

from __future__ import annotations

import logging

import flet as ft

from aiseed_weather.models.user_settings import UserSettings, resolved_data_dir
from aiseed_weather.services.jma_amedas_service import JmaAmedasService

logger = logging.getLogger(__name__)


VARIABLES = (
    ("temperature", "気温"),
    ("precipitation1h", "降水量 (1h)"),
    ("wind", "風"),
    ("sun1h", "日照 (1h)"),
    ("snow", "積雪深"),
)


@ft.component
def AmedasView(settings: UserSettings):
    state, set_state = ft.use_state("idle")
    snapshot, set_snapshot = ft.use_state(None)
    error, set_error = ft.use_state(None)
    variable, set_variable = ft.use_state("temperature")
    progress, set_progress = ft.use_state("")

    service = JmaAmedasService(data_dir=resolved_data_dir(settings))

    async def load(force: bool = False):
        set_state("loading")
        set_error(None)
        set_progress("Fetching AMeDAS snapshot from JMA…")
        try:
            s = await service.fetch(force=force)
            set_snapshot(s)
            set_progress("")
            set_state("ready")
        except Exception as e:
            logger.exception("AMeDAS view failed to fetch JMA snapshot")
            set_error(str(e))
            set_state("error")

    if state == "idle":
        return ft.Column(
            controls=[
                ft.Text("AMeDAS (JMA)", size=18, weight=ft.FontWeight.BOLD),
                ft.Text(
                    "Latest ground observations from ~1,300 AMeDAS stations.",
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
                ft.Text("AMeDAS (JMA)", size=18, weight=ft.FontWeight.BOLD),
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
                ft.Text("AMeDAS (JMA)", size=18, weight=ft.FontWeight.BOLD),
                ft.Text(f"Could not fetch AMeDAS: {error}", color=ft.Colors.RED),
                ft.FilledButton(
                    content=ft.Text("再取得 / Retry"),
                    on_click=lambda _: ft.context.page.run_task(load, force=True),
                ),
            ],
        )

    return ft.Column(
        controls=[
            ft.Row(
                controls=[
                    ft.Text("AMeDAS (JMA)", size=18, weight=ft.FontWeight.BOLD),
                    ft.FilledButton(
                        content=ft.Text("再取得"),
                        on_click=lambda _: ft.context.page.run_task(load, force=True),
                    ),
                ],
                alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
            ),
            ft.Text(
                f"As of {snapshot.timestamp:%Y-%m-%d %H:%M JST} · "
                f"fetched {snapshot.fetched_at:%H:%M JST}",
                size=12,
                color=ft.Colors.GREY,
            ),
            ft.Dropdown(
                value=variable,
                options=[ft.dropdown.Option(key=k, text=label) for k, label in VARIABLES],
                on_change=lambda e: set_variable(e.control.value),
            ),
            # TODO(agent): render the map of stations with the selected variable.
            # See figures/ for a Japan basemap with scatter markers colored by value.
            ft.Text("AMeDAS map will render here", color=ft.Colors.GREY),
            ft.Text(
                "出典: 気象庁ホームページ\n編集・加工を行った旨と編集責任が利用者にあります",
                size=10,
                color=ft.Colors.GREY,
            ),
        ],
    )
