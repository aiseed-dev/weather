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
# (Third-party deprecation noise — the cfgrib / xarray FutureWarning —
# is silenced in the package __init__.py so it covers tests and scripts
# too, not only this entry point.)
_LOG_LEVEL = os.environ.get("AISEED_WEATHER_LOG", "INFO").upper()
logging.basicConfig(
    level=_LOG_LEVEL,
    stream=sys.stderr,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

import flet as ft

from aiseed_weather.components.app import App
from aiseed_weather.models.user_settings import (
    load_window_state, save_window_state,
)


# Geometry defaults used when no prior window.json exists. Chosen so
# the control panel + chart + bottom panel all fit comfortably on a
# 1920×1080 display.
_DEFAULT_WIDTH = 1400
_DEFAULT_HEIGHT = 900


def _restore_window(page: ft.Page) -> None:
    state = load_window_state()
    page.window.width = float(state.get("width", _DEFAULT_WIDTH))
    page.window.height = float(state.get("height", _DEFAULT_HEIGHT))
    # top / left may be None (first run); leave the platform to centre
    # the window in that case.
    top = state.get("top")
    left = state.get("left")
    if top is not None:
        page.window.top = float(top)
    if left is not None:
        page.window.left = float(left)
    if state.get("maximized"):
        page.window.maximized = True


def _persist_on_window_event(page: ft.Page):
    """Save window geometry whenever Flet reports a relevant event.

    Flet fires resize / move / close events on the window. We persist
    on each so a hard kill mid-session still leaves the last seen
    position in window.json. Writes are atomic via temp + rename.
    """
    def handler(e):
        if e.type not in ("resize", "move", "close", "maximize", "unmaximize"):
            return
        try:
            save_window_state({
                "width": page.window.width,
                "height": page.window.height,
                "top": page.window.top,
                "left": page.window.left,
                "maximized": bool(page.window.maximized),
            })
        except Exception:
            # Persistence failure must never crash the UI.
            import logging
            logging.getLogger(__name__).exception(
                "Failed to save window state",
            )
    page.window.on_event = handler


def _configure(page: ft.Page) -> None:
    """One-time page setup before the App component is rendered.

    Sits in ``before_main`` so the page is fully configured (theme,
    window geometry, event handlers) by the time the declarative
    App tree first paints. Doing this work inside the App component
    itself would re-run on every re-render, which would either be
    wasteful (theme assign) or wrong (resetting saved window state).
    """
    page.title = "AIseed Weather"
    page.theme_mode = ft.ThemeMode.SYSTEM
    page.padding = 0
    _restore_window(page)
    _persist_on_window_event(page)


if __name__ == "__main__":
    # Flet 0.85+ declarative style: ``page.render(App)`` mounts the
    # root component; ``before_main`` runs once for page-level
    # configuration. The old ``ft.run(main)`` callback was equivalent
    # but conflated those two responsibilities into one function.
    ft.run(
        lambda page: page.render(App),
        before_main=_configure,
    )
