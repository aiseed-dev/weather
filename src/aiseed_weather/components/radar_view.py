# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Yasuhiro / AIseed

"""Rainfall radar nowcast view.

Stays idle on mount; fetches only when the user presses 取得 / Fetch.
Explicit Refresh forces a re-fetch ignoring cache. See the
`user-action-fetch` skill.
"""

from __future__ import annotations

import logging

import flet as ft

from aiseed_weather.models.user_settings import UserSettings, resolved_data_dir
from aiseed_weather.services.jma_radar_service import JmaRadarService

logger = logging.getLogger(__name__)


@ft.component
def RadarView(settings: UserSettings):
    state, set_state = ft.use_state("idle")  # idle | loading | ready | error
    snapshot, set_snapshot = ft.use_state(None)
    error, set_error = ft.use_state(None)
    progress, set_progress = ft.use_state("")

    service = JmaRadarService(data_dir=resolved_data_dir(settings))

    async def load(force: bool = False):
        set_state("loading")
        set_error(None)
        set_progress("Fetching radar tiles from JMA…")
        try:
            s = await service.fetch(force=force)
            set_snapshot(s)
            set_progress("")
            set_state("ready")
        except Exception as e:
            logger.exception("Radar view failed to fetch JMA snapshot")
            set_error(str(e))
            set_state("error")

    if state == "idle":
        return ft.Column(
            controls=[
                ft.Text("Rainfall Nowcast (JMA)", size=18, weight=ft.FontWeight.BOLD),
                ft.Text(
                    "Composite radar mosaic for Japan from JMA tiles.",
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
                ft.Text("Rainfall Nowcast (JMA)", size=18, weight=ft.FontWeight.BOLD),
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
                ft.Text("Rainfall Nowcast (JMA)", size=18, weight=ft.FontWeight.BOLD),
                ft.Text(f"Could not fetch radar: {error}", color=ft.Colors.RED),
                ft.FilledButton(
                    content=ft.Text("再取得 / Retry"),
                    on_click=lambda _: ft.context.page.run_task(load, force=True),
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
                        content=ft.Text("再取得"),
                        on_click=lambda _: ft.context.page.run_task(load, force=True),
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
            # tiles overlaid, then embed as PNG bytes via ft.Image(src=...).
            ft.Text("Radar image will render here", color=ft.Colors.GREY),
            ft.Text(
                "出典: 気象庁ホームページ\n編集・加工を行った旨と編集責任が利用者にあります",
                size=10,
                color=ft.Colors.GREY,
            ),
        ],
    )
