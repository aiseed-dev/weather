# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Yasuhiro / AIseed

"""Central registry of JMA endpoint URLs.

The JMA endpoints used by this app are not a documented public API. URLs and
schemas can change without notice. Centralizing them here means a JMA change
needs one fix, not many.

Verify these URLs against the JMA website at the start of any JMA-related
implementation task. The structure documented here is as of 2026-05; subject
to change.
"""

from __future__ import annotations

USER_AGENT = "AIseed Weather/0.1 (+https://aiseed.dev)"

# Rainfall nowcast (高解像度降水ナウキャスト)
RADAR_TARGET_TIMES = "https://www.jma.go.jp/bosai/jmatile/data/nowc/targetTimes_N1.json"
# Tile URL template; fill in basetime, validtime, z, x, y.
# Verify the path structure (`surf/hrpns/...`) against current JMA convention.
RADAR_TILE_TEMPLATE = (
    "https://www.jma.go.jp/bosai/jmatile/data/nowc/"
    "{basetime}/none/{validtime}/surf/hrpns/{z}/{x}/{y}.png"
)

# AMeDAS observations
AMEDAS_STATION_TABLE = "https://www.jma.go.jp/bosai/amedas/const/amedastable.json"
AMEDAS_LATEST_TIME = "https://www.jma.go.jp/bosai/amedas/data/latest_time.txt"
AMEDAS_MAP_SNAPSHOT = (
    "https://www.jma.go.jp/bosai/amedas/data/map/{timestamp}.json"
)
AMEDAS_POINT_SERIES = (
    "https://www.jma.go.jp/bosai/amedas/data/point/{station_id}/{yyyymmdd}_{hh}.json"
)

# Attribution text required for any output using JMA data.
ATTRIBUTION = "出典: 気象庁ホームページ (https://www.jma.go.jp/)"
ATTRIBUTION_PROCESSED = (
    "出典: 気象庁ホームページ\n"
    "編集・加工を行った旨と編集責任が利用者にあります"
)
