# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Yasuhiro / AIseed

"""配布用のアプリの入口。

本体（aiseed_weather.main）を読み込むと xarray・cfgrib・matplotlib なども読み込まれ、Windows の
初回は 40 秒ほどかかる。その間 Flet の画面の側は Python を待ちきれず、白いままになった
（2026-10-16、win11）。なので先に Flet だけで「起動しています」を出し、本体は別のスレッドで
読み込んでから画面を差し替える。開発（flet run）は今までどおり本体を直接走らせる。
"""
import asyncio
import importlib
import logging
import os

import flet as ft

# 利用の記録（services/usage_report.py）が、配布版と手元の実行を見分ける印。本体を読み込む前に置く
os.environ["AISEED_WEATHER_BUILD"] = "release"


async def boot(page: ft.Page) -> None:
    page.title = "AIseed Weather"
    page.add(ft.Container(
        expand=True,
        alignment=ft.Alignment.CENTER,
        content=ft.Column(
            tight=True,
            horizontal_alignment=ft.CrossAxisAlignment.CENTER,
            controls=[ft.ProgressRing(),
                      ft.Text("AIseed Weather を起動しています…"),
                      ft.Text("初めて開くときは 1 分ほどかかります。", size=12, color=ft.Colors.GREY)],
        ),
    ))
    try:
        app = await asyncio.to_thread(importlib.import_module, "aiseed_weather.main")
    except Exception as exc:
        logging.getLogger(__name__).exception("AIseed Weather の読み込みに失敗しました")
        page.controls.clear()
        page.add(ft.Text(f"起動できませんでした: {type(exc).__name__}: {exc}", selectable=True))
        return
    app._configure(page)
    page.render(app.App)


ft.run(boot)
