# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Yasuhiro / AIseed

"""AMeDAS ground observation map view.

Implements the standard fetch-on-open pattern. Default variable is
temperature; the user can switch to precipitation, wind, sunshine, or
snow depth.
"""

from __future__ import annotations

import flet as ft

from aiseed_weather.services.jma_amedas_service import JmaAmedasService


VARIABLES = (
    ("temperature", "気温"),
    ("precipitation1h", "降水量 (1h)"),
    ("wind", "風"),
    ("sun1h", "日照 (1h)"),
    ("snow", "積雪深"),
)


@ft.component
def AmedasView():
    state, set_state = ft.use_state("idle")
    snapshot, set_snapshot = ft.use_state(None)
    error, set_error = ft.use_state(None)
    variable, set_variable = ft.use_state("temperature")

    service = JmaAmedasService()

    async def load(force: bool = False):
        set_state("loading")
        try:
            s = await service.fetch(force=force)
            set_snapshot(s)
            set_state("ready")
        except Exception as e:
            set_error(str(e))
            set_state("error")

    ft.use_effect(lambda: ft.run_task(load), deps=[])

    if state in ("idle", "loading"):
        return ft.Column(
            controls=[
                ft.Text("AMeDAS (JMA)", size=18, weight=ft.FontWeight.BOLD),
                ft.ProgressRing(),
                ft.Text("Fetching ground observations…", color=ft.Colors.GREY),
            ],
        )

    if state == "error":
        return ft.Column(
            controls=[
                ft.Text("AMeDAS (JMA)", size=18, weight=ft.FontWeight.BOLD),
                ft.Text(f"Could not fetch AMeDAS: {error}", color=ft.Colors.RED),
                ft.FilledButton(
                    text="再取得 / Retry",
                    on_click=lambda _: ft.run_task(lambda: load(force=True)),
                ),
            ],
        )

    return ft.Column(
        controls=[
            ft.Row(
                controls=[
                    ft.Text("AMeDAS (JMA)", size=18, weight=ft.FontWeight.BOLD),
                    ft.FilledButton(
                        text="再取得",
                        on_click=lambda _: ft.run_task(lambda: load(force=True)),
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
