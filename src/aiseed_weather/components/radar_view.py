# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Yasuhiro / AIseed

"""Rainfall radar nowcast view.

Implements the standard fetch-on-open pattern described in the
user-action-fetch skill: mount triggers a fetch, cache is reused if fresh,
no background polling, explicit Refresh button.
"""

from __future__ import annotations

import flet as ft

from aiseed_weather.services.jma_radar_service import JmaRadarService


@ft.component
def RadarView():
    state, set_state = ft.use_state("idle")  # idle | loading | ready | error
    snapshot, set_snapshot = ft.use_state(None)
    error, set_error = ft.use_state(None)

    service = JmaRadarService()

    async def load(force: bool = False):
        set_state("loading")
        try:
            s = await service.fetch(force=force)
            set_snapshot(s)
            set_state("ready")
        except Exception as e:
            set_error(str(e))
            set_state("error")

    # Mount-time fetch. Runs once per RadarView instance.
    ft.use_effect(lambda: ft.run_task(load), deps=[])

    if state in ("idle", "loading"):
        return ft.Column(
            controls=[
                ft.Text("Rainfall Nowcast (JMA)", size=18, weight=ft.FontWeight.BOLD),
                ft.ProgressRing(),
                ft.Text("Fetching radar tiles…", color=ft.Colors.GREY),
            ],
        )

    if state == "error":
        return ft.Column(
            controls=[
                ft.Text("Rainfall Nowcast (JMA)", size=18, weight=ft.FontWeight.BOLD),
                ft.Text(f"Could not fetch radar: {error}", color=ft.Colors.RED),
                ft.FilledButton(
                    text="再取得 / Retry",
                    on_click=lambda _: ft.run_task(lambda: load(force=True)),
                ),
            ],
        )

    # state == "ready"
    return ft.Column(
        controls=[
            ft.Row(
                controls=[
                    ft.Text("Rainfall Nowcast (JMA)", size=18, weight=ft.FontWeight.BOLD),
                    ft.FilledButton(
                        text="再取得",
                        on_click=lambda _: ft.run_task(lambda: load(force=True)),
                    ),
                ],
                alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
            ),
            ft.Text(
                f"Valid {snapshot.validtime} · fetched {snapshot.fetched_at:%H:%M JST}",
                size=12,
                color=ft.Colors.GREY,
            ),
            # TODO(agent): render the composited radar image here.
            # Use the figures/ layer to build a Japan basemap with the radar
            # tiles overlaid, then embed via ft.MatplotlibChart.
            ft.Text("Radar image will render here", color=ft.Colors.GREY),
            ft.Text(
                "出典: 気象庁ホームページ\n編集・加工を行った旨と編集責任が利用者にあります",
                size=10,
                color=ft.Colors.GREY,
            ),
        ],
    )
