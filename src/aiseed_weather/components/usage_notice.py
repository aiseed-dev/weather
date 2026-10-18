# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Yasuhiro / AIseed

"""One-time notice for users whose config.toml has no ``usage_report`` key.

They never saw the first-run panel, so the usage report must be announced
before it starts (services/usage_report.start sends nothing on this run).
"""

import logging

import flet as ft

from aiseed_weather.models import user_settings

logger = logging.getLogger(__name__)


@ft.component
def UsageNotice():
    visible, set_visible = ft.use_state(True)
    open_error, set_open_error = ft.use_state("")

    def open_config(e):
        try:
            user_settings.open_in_text_editor(user_settings.config_path())
            set_open_error("")
        except Exception as exc:
            logger.exception("Opening the config file failed")
            set_open_error(f"開けませんでした: {type(exc).__name__}: {exc}")

    if not visible:
        return ft.Container()

    return ft.Container(
        bgcolor=ft.Colors.AMBER_100,
        padding=ft.Padding.symmetric(horizontal=12, vertical=8),
        content=ft.Row(
            vertical_alignment=ft.CrossAxisAlignment.START,
            controls=[
                ft.Icon(ft.Icons.INFO_OUTLINE, color=ft.Colors.AMBER_900, size=20),
                ft.Column(
                    expand=True, spacing=2,
                    controls=[
                        ft.Text(
                            "利用の記録について",
                            size=13, weight=ft.FontWeight.BOLD,
                            color=ft.Colors.BLACK,
                        ),
                        ft.Text(
                            "次の起動から、起動した日に 1 回だけ、アプリの版と OS を作者の解析へ送ります"
                            "（使われている台数を知るためです）。人や PC を見分ける情報・地点・設定・"
                            "見たものは送りません。送りたくなければ、設定ファイルに "
                            "usage_report = false と書いてください。",
                            size=12, color=ft.Colors.BLACK,
                        ),
                        *(
                            [ft.Text(open_error, size=12, color=ft.Colors.RED)]
                            if open_error else []
                        ),
                    ],
                ),
                ft.TextButton(
                    "設定ファイルを開く",
                    icon=ft.Icons.EDIT_NOTE,
                    on_click=open_config,
                ),
                ft.IconButton(
                    icon=ft.Icons.CLOSE,
                    icon_size=18,
                    icon_color=ft.Colors.BLACK,
                    tooltip="閉じる",
                    on_click=lambda _: set_visible(False),
                ),
            ],
        ),
    )
