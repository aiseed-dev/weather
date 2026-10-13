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

# Rainfall nowcast (高解像度降水ナウキャスト). N1 lists the observations of
# the past 3 hours (basetime == validtime), N2 the forecasts from the
# latest basetime (+5 … +60 min). Each entry names the elements it has.
RADAR_TARGET_TIMES = "https://www.jma.go.jp/bosai/jmatile/data/nowc/targetTimes_N1.json"
RADAR_TARGET_TIMES_FORECAST = (
    "https://www.jma.go.jp/bosai/jmatile/data/nowc/targetTimes_N2.json"
)
RADAR_ELEMENT = "hrpns"
# 256 px Web-Mercator XYZ tiles; times are UTC (YYYYmmddHHMMSS). Checked
# 2026-10-13: only even z carry data — an odd z, or z ≥ 12, returns a
# blank tile.
RADAR_TILE_TEMPLATE = (
    "https://www.jma.go.jp/bosai/jmatile/data/nowc/"
    "{basetime}/none/{validtime}/surf/{element}/{z}/{x}/{y}.png"
)
# Where the frame has no data (outside the radars' range, or a radar
# down): GeoJSON of one polygon, the world with the observed area as its
# hole. JMA's viewer reads it per frame (nowc.properties.xml, imageType
# hrpns_nd type="geojson").
RADAR_NODATA_TEMPLATE = (
    "https://www.jma.go.jp/bosai/jmatile/data/nowc/"
    "{basetime}/none/{validtime}/surf/{element}_nd/data.geojson"
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

# Forecast (府県天気予報)
# AREA_TABLE: nation-wide area hierarchy (centers → offices → class10 →
# class15 → class20). The forecast endpoint is keyed by 'office' code
# (6-digit, e.g. '130000' Tokyo). The class10 layer (e.g. '130010'
# 東京地方) is what the forecast JSON's per-area arrays index on.
AREA_TABLE = "https://www.jma.go.jp/bosai/common/const/area.json"
FORECAST_OFFICE = (
    "https://www.jma.go.jp/bosai/forecast/data/forecast/{office_code}.json"
)

# Attribution text required for any output using JMA data.
ATTRIBUTION = "出典: 気象庁ホームページ (https://www.jma.go.jp/)"
ATTRIBUTION_PROCESSED = (
    "出典: 気象庁ホームページ\n"
    "編集・加工を行った旨と編集責任が利用者にあります"
)
# The same on one line, for figure footers.
ATTRIBUTION_PROCESSED_INLINE = (
    "出典: 気象庁ホームページ（編集・加工を行った旨と編集責任が利用者にあります）"
)
