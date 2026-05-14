# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Yasuhiro / AIseed

import flet as ft

from aiseed_weather.components.app import App


def main(page: ft.Page):
    page.title = "AIseed Weather"
    page.theme_mode = ft.ThemeMode.SYSTEM
    page.padding = 0
    page.render(App)


if __name__ == "__main__":
    ft.run(main)
