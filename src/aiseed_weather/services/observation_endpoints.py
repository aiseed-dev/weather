# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Yasuhiro / AIseed

"""Where the app reads JMA daily observation records from.

The records are the daily max / min / mean temperature, precipitation
and sunshine of every JMA station since 1880 (including closed
stations), redistributed by the 個人開発気象統計 site as NetCDF
(``/data/daily/``, built by ``WeatherStatic/export_dist.py``). JMA
itself publishes these one station-month at a time as HTML, which is
not something an app should scrape.

Like the JMA endpoints, this is not in ``config.toml``: there is one
source, it needs no credentials, and opening the view and pressing
取得 is the act of choosing it.

The site's address is weather.time-j.net (moved from the Pages address
on 2026-10-06 when the old WeatherCore system was retired). The Pages
address weather-dj7.pages.dev still serves the same files.
"""

from __future__ import annotations

BASE = "https://weather.time-j.net"

DAILY_MANIFEST = f"{BASE}/data/daily/manifest.json"
DAILY_FILE = f"{BASE}/data/daily/{{path}}"     # path from the manifest (years/2018.nc …)

USER_AGENT = "AIseed Weather/0.1 (+https://aiseed.dev)"

# Shown when the manifest (which carries the publisher's own wording) has
# not been read yet.
ATTRIBUTION = "出典: 気象庁ホームページ（気象庁のデータを編集・加工）"
SOURCE_NAME = "JMA daily observations (redistributed by 個人開発気象統計)"
