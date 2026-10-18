# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Yasuhiro / AIseed

"""Once-a-day "this install was started" note to the author's own analytics.

A user whose config.toml predates the key has seen no notice, so the first
start only shows one and records that in usage.json; sending begins at the
next start.

It is a send, not a fetch: nothing comes back that the app uses. The body
carries the app version, OS and build kind and nothing that tells one
person or PC from another (no sid / vid), so the number of notes per day is
the number of installs used that day. The user can stop it
with ``usage_report = false`` in config.toml. Failures stay silent and the
day is retried at the next start.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from datetime import date
from pathlib import Path

import httpx
from platformdirs import user_config_dir

import aiseed_weather
from aiseed_weather.models.user_settings import UserSettings

logger = logging.getLogger(__name__)

ENDPOINT = "https://analytics.aiseed.dev/v1/app"
_SITE = "weather.time-j.net"
_TIMEOUT_S = 5.0

# desktop/src/main.py sets this to "release" before importing the app.
BUILD_ENV = "AISEED_WEATHER_BUILD"


def usage_path() -> Path:
    return Path(user_config_dir("aiseed-weather")) / "usage.json"


def os_name() -> str:
    if sys.platform == "win32":
        return "windows"
    if sys.platform == "darwin":
        return "macos"
    return "linux"


def build_kind() -> str:
    return "release" if os.environ.get(BUILD_ENV) == "release" else "source"


_OS_LABEL = {"windows": "Windows", "macos": "macOS", "linux": "Linux"}


def user_agent() -> str:
    return (
        f"AIseed Weather/{aiseed_weather.__version__} "
        f"({_OS_LABEL[os_name()]}; {build_kind()})"
    )


def payload() -> dict:
    return {
        "site": _SITE,
        "version": aiseed_weather.__version__,
        "os": os_name(),
        "channel": build_kind(),
    }


def _load_state() -> dict:
    try:
        data = json.loads(usage_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _save_state(**changes: str) -> None:
    """Merge into usage.json, so recording a send keeps the notice date."""
    state = {**_load_state(), **changes}
    path = usage_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state), encoding="utf-8")
    tmp.replace(path)


def _last_sent() -> str | None:
    value = _load_state().get("last_sent")
    return value if isinstance(value, str) else None


def notice_due(settings: UserSettings) -> bool:
    """True on the one start where an existing user should be told first."""
    return (
        settings.usage_report
        and not settings.usage_report_written
        and "notice_shown" not in _load_state()
    )


def record_notice(today: date | None = None) -> None:
    try:
        _save_state(notice_shown=(today or date.today()).isoformat())
    except Exception as exc:
        logger.info("usage notice was not recorded: %s: %s", type(exc).__name__, exc)


async def start(
    settings: UserSettings,
    *,
    client: httpx.AsyncClient | None = None,
    today: date | None = None,
) -> str:
    """What the app does once at start: "notice", "sent" or "skipped"."""
    if notice_due(settings):
        record_notice(today)
        return "notice"
    sent = await send_if_due(settings, client=client, today=today)
    return "sent" if sent else "skipped"


async def send_if_due(
    settings: UserSettings,
    *,
    client: httpx.AsyncClient | None = None,
    today: date | None = None,
) -> bool:
    """Send today's note unless it is switched off or already sent.

    Returns whether a note was accepted. Never raises: a failure only
    leaves the day unrecorded, so the next start tries again.
    """
    if not settings.usage_report:
        return False
    today = today or date.today()
    try:
        if _last_sent() == today.isoformat():
            return False
        own_client = client is None
        client = client or httpx.AsyncClient(timeout=_TIMEOUT_S)
        try:
            # No Origin header: it is not a browser, and faking one would
            # misstate what is sending.
            response = await client.post(
                ENDPOINT, json=payload(),
                headers={"User-Agent": user_agent()},
                timeout=_TIMEOUT_S,
            )
        finally:
            if own_client:
                await client.aclose()
        if not response.is_success:
            logger.info("usage report was not accepted: HTTP %s", response.status_code)
            return False
        _save_state(last_sent=today.isoformat())
        return True
    except Exception as exc:
        logger.info("usage report failed: %s: %s", type(exc).__name__, exc)
        return False
