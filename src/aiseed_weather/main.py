# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Yasuhiro / AIseed

import logging
import os
import sys

import flet as ft

from aiseed_weather.components.app import App


def _configure_logging() -> None:
    # Flet swallows exceptions raised inside components and only surfaces a
    # short string to the UI. Without logging to stderr the user has no way
    # to see tracebacks. Default to INFO; override with AISEED_WEATHER_LOG.
    level = os.environ.get("AISEED_WEATHER_LOG", "INFO").upper()
    logging.basicConfig(
        level=level,
        stream=sys.stderr,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def main(page: ft.Page):
    _configure_logging()
    page.title = "AIseed Weather"
    page.theme_mode = ft.ThemeMode.SYSTEM
    page.padding = 0
    page.render(App)


if __name__ == "__main__":
    ft.run(main)
