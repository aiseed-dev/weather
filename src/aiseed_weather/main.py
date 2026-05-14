# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Yasuhiro / AIseed

# Matplotlib must be configured for headless rendering before anything else
# imports pyplot. We render figures in worker threads via asyncio.to_thread,
# and any GUI backend (TkAgg, QtAgg, GTK) hangs when used off the main thread.
import matplotlib

matplotlib.use("Agg")

import logging
import os
import sys

# Configure logging at module import time, BEFORE the App tree is imported.
# forecast_service installs a multiurl monkey-patch at module import and
# logs a confirmation that we want to see in the dev terminal. If we wait
# until main() to configure logging, that patch message gets swallowed.
_LOG_LEVEL = os.environ.get("AISEED_WEATHER_LOG", "INFO").upper()
logging.basicConfig(
    level=_LOG_LEVEL,
    stream=sys.stderr,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

import flet as ft

from aiseed_weather.components.app import App


def main(page: ft.Page):
    page.title = "AIseed Weather"
    page.theme_mode = ft.ThemeMode.SYSTEM
    page.padding = 0
    page.render(App)


if __name__ == "__main__":
    ft.run(main)
