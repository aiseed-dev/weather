# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Yasuhiro / AIseed

"""「このアプリについて」: the version, where to ask, and what is sent.

Always one click away so the usage report is never something the user has
to find in a file first.
"""

import logging

import flet as ft

import aiseed_weather
from aiseed_weather.models import project_links, user_settings
from aiseed_weather.models.user_settings import UserSettings
from aiseed_weather.services import usage_report

logger = logging.getLogger(__name__)


def _links() -> list[tuple[str, str]]:
    candidates = [
        ("サイト（使い方）", project_links.SITE_URL),
        ("GitHub", project_links.GITHUB_URL),
        ("Facebook ページ", project_links.FACEBOOK_URL),
        ("Discord（質問・話し合い）", project_links.DISCORD_URL),
    ]
    return [(label, url) for label, url in candidates if url]


@ft.component
def AboutButton(settings: UserSettings):
    show, set_show = ft.use_state(False)
    open_error, set_open_error = ft.use_state("")

    def open_config(e):
        try:
            user_settings.open_in_text_editor(user_settings.config_path())
            set_open_error("")
        except Exception as exc:
            logger.exception("Opening the config file failed")
            set_open_error(f"開けませんでした: {type(exc).__name__}: {exc}")

    build_label = (
        "配布版" if usage_report.build_kind() == "release" else "手元の実行"
    )
    report_state = (
        "送っています" if settings.usage_report else "送っていません"
    )

    ft.use_dialog(
        ft.AlertDialog(
            title=ft.Text("AIseed Weather"),
            content=ft.Column(
                tight=True, scroll=ft.ScrollMode.AUTO, width=420, spacing=8,
                controls=[
                    ft.Text(
                        f"版 {aiseed_weather.__version__}（{build_label}）",
                        size=13,
                    ),
                    ft.Row(
                        wrap=True, spacing=0,
                        controls=[
                            ft.TextButton(label, url=url)
                            for label, url in _links()
                        ],
                    ),
                    ft.Divider(),
                    ft.Text("利用の記録", size=13, weight=ft.FontWeight.BOLD),
                    ft.Text(f"いま: {report_state}", size=13),
                    ft.Text(
                        "起動した日に 1 回だけ、アプリの版と OS を作者の解析へ送ります。"
                        "人や PC を見分ける情報・地点・設定・見たものは送りません。"
                        "止めるには、設定ファイルで usage_report = false にして、"
                        "アプリを起動し直してください。",
                        size=12,
                    ),
                    ft.Row(
                        wrap=True, spacing=0,
                        controls=[
                            ft.TextButton(
                                "設定ファイルを開く",
                                icon=ft.Icons.EDIT_NOTE,
                                on_click=open_config,
                            ),
                            ft.TextButton(
                                "送る内容の説明",
                                url=project_links.KAISEKI_URL,
                            ),
                        ],
                    ),
                    *(
                        [ft.Text(open_error, size=12, color=ft.Colors.RED)]
                        if open_error else []
                    ),
                    ft.Divider(),
                    ft.Text(
                        "データの出典: ECMWF Open Data（CC-BY-4.0）、"
                        "Open-Meteo（CC-BY-4.0）、気象庁ホームページ。"
                        "コードは AGPL-3.0-or-later。",
                        size=11, color=ft.Colors.GREY,
                    ),
                ],
            ),
            actions=[ft.TextButton("閉じる", on_click=lambda _: set_show(False))],
            on_dismiss=lambda _: set_show(False),
        )
        if show else None,
    )

    return ft.IconButton(
        icon=ft.Icons.INFO_OUTLINE,
        icon_size=18,
        tooltip="このアプリについて",
        on_click=lambda _: set_show(True),
    )
