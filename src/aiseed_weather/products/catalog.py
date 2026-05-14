# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Yasuhiro / AIseed

"""Catalog of meteorological data products this app knows about.

This module is intentionally documentation-heavy. The expert user should
be able to skim it and learn what data exists, who publishes it, under
what terms, and where each entry lives on our roadmap.

Status flags
------------
IMPLEMENTED
    Wired through end-to-end. The user can select it and get a chart.
PLANNED
    Data source is known and publicly accessible; no code connects it
    yet, but adding it is straightforward integration work. The entry
    exists to advertise the roadmap and let the user see the spec.
EXTERNAL_DEP
    Requires non-trivial work outside this repo: paid API keys, MARS,
    Copernicus CDS auth, JMA 業務支援センター 契約, etc.
OUT_OF_SCOPE
    Intentionally excluded (e.g. seasonal forecasts not in Open Data).

Tab membership
--------------
Each product belongs to exactly one of the three UI tabs:

  MODELS   gridded numerical forecasts and reanalyses
  NOWCAST  imagery: radar, satellite, lightning
  POINTS   station observations and point forecasts

A product can be overlaid on top of another later (planned), but its
primary home tab does not change.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class Tab(Enum):
    MODELS = "models"
    NOWCAST = "nowcast"
    POINTS = "points"


class Category(Enum):
    SHORT_RANGE = "short_range"      # 1-3 day numerical forecast
    MEDIUM_RANGE = "medium_range"    # 3-10 day numerical forecast
    EXTENDED = "extended"            # 10-15 day numerical forecast
    REANALYSIS = "reanalysis"        # ERA5, JRA, ...
    RADAR = "radar"
    SATELLITE = "satellite"
    LIGHTNING = "lightning"
    OBSERVATION = "observation"      # AMeDAS, METAR, SYNOP, ...
    POINT_FORECAST = "point_forecast"


# Display order — drives the dialog section ordering.
CATEGORY_ORDER: tuple[Category, ...] = (
    Category.SHORT_RANGE,
    Category.MEDIUM_RANGE,
    Category.EXTENDED,
    Category.REANALYSIS,
    Category.RADAR,
    Category.SATELLITE,
    Category.LIGHTNING,
    Category.OBSERVATION,
    Category.POINT_FORECAST,
)


CATEGORY_LABELS: dict[Category, str] = {
    Category.SHORT_RANGE: "数値予報・短期 / Short-range forecast",
    Category.MEDIUM_RANGE: "数値予報・中期 / Medium-range forecast",
    Category.EXTENDED: "数値予報・長期 / Extended-range forecast",
    Category.REANALYSIS: "再解析 / Reanalysis",
    Category.RADAR: "レーダー / Radar",
    Category.SATELLITE: "衛星 / Satellite",
    Category.LIGHTNING: "雷 / Lightning",
    Category.OBSERVATION: "観測 / Observations",
    Category.POINT_FORECAST: "地点予報 / Point forecast",
}


class Status(Enum):
    IMPLEMENTED = "implemented"
    PLANNED = "planned"
    EXTERNAL_DEP = "external_dep"
    OUT_OF_SCOPE = "out_of_scope"


STATUS_LABELS: dict[Status, str] = {
    Status.IMPLEMENTED: "✓ 実装済み / Implemented",
    Status.PLANNED: "◐ 計画中 / Planned",
    Status.EXTERNAL_DEP: "⋯ 要外部連携 / External dependency",
    Status.OUT_OF_SCOPE: "✗ 対象外 / Out of scope",
}


@dataclass(frozen=True)
class Product:
    key: str
    label_ja: str
    label_en: str
    tab: Tab
    category: Category
    agency: str
    spec: str             # "0.25° / 3h / T+0..T+240h" etc
    source_url: str
    backend: str          # short description of how we'd fetch it
    license_info: str
    status: Status
    notes: str = ""

    def bilingual_label(self) -> str:
        # Display "日本語 / English" while we don't have language settings.
        if self.label_ja == self.label_en:
            return self.label_ja
        return f"{self.label_ja} / {self.label_en}"


CATALOG: tuple[Product, ...] = (
    # ────────────────────────── MODELS tab ──────────────────────────
    # ── Medium range forecasts ──
    Product(
        key="ecmwf_hres",
        label_ja="ECMWF HRES (IFS 0.25° 決定論)",
        label_en="ECMWF HRES (IFS 0.25° deterministic)",
        tab=Tab.MODELS,
        category=Category.MEDIUM_RANGE,
        agency="ECMWF",
        spec="0.25° / 3h up to T+144h, 6h to T+240h / 4 cycles/day (00,06,12,18 UTC)",
        source_url="https://www.ecmwf.int/en/forecasts/datasets/open-data",
        backend="ecmwf-opendata client → GRIB2 from s3://ecmwf-forecasts",
        license_info="CC-BY-4.0",
        status=Status.IMPLEMENTED,
    ),
    Product(
        key="ecmwf_aifs_single",
        label_ja="ECMWF AIFS Single (AI 決定論)",
        label_en="ECMWF AIFS Single (AI-driven deterministic)",
        tab=Tab.MODELS,
        category=Category.MEDIUM_RANGE,
        agency="ECMWF",
        spec="0.25° / 6h up to T+360h",
        source_url="https://www.ecmwf.int/en/about/media-centre/aifs-blog",
        backend="ecmwf-opendata client with model='aifs'",
        license_info="CC-BY-4.0",
        status=Status.PLANNED,
        notes="Same Open Data backend as HRES — mostly an integration story.",
    ),
    Product(
        key="ncep_gfs",
        label_ja="NCEP GFS (米 0.25°)",
        label_en="NCEP GFS (NOAA 0.25°)",
        tab=Tab.MODELS,
        category=Category.MEDIUM_RANGE,
        agency="NOAA / NCEP",
        spec="0.25° / 3h / T+0..T+384h, 4 cycles/day",
        source_url="https://www.nco.ncep.noaa.gov/pmb/products/gfs/",
        backend="nomads.ncep.noaa.gov or s3://noaa-gfs-bdp-pds",
        license_info="public domain (U.S. government work)",
        status=Status.PLANNED,
    ),
    Product(
        key="dwd_icon",
        label_ja="DWD ICON-EU (ドイツ気象局、欧州)",
        label_en="DWD ICON-EU (Germany, Europe domain)",
        tab=Tab.MODELS,
        category=Category.MEDIUM_RANGE,
        agency="DWD",
        spec="0.0625° / 1h / T+0..T+120h",
        source_url="https://opendata.dwd.de/weather/nwp/icon-eu/",
        backend="DWD Open Data, GRIB2",
        license_info="DL-DE→BY-2.0",
        status=Status.PLANNED,
    ),

    # ── Short range / regional forecasts ──
    Product(
        key="jma_msm",
        label_ja="JMA MSM (メソモデル、5km 日本)",
        label_en="JMA MSM (mesoscale model, 5km Japan)",
        tab=Tab.MODELS,
        category=Category.SHORT_RANGE,
        agency="JMA",
        spec="5km / 1h / T+0..T+39h or T+78h, 8 cycles/day",
        source_url="https://www.jma.go.jp/jma/kishou/know/whitep/1-3-2.html",
        backend="気象業務支援センター 経由 (公式 Open Data 無し)",
        license_info="出典: 気象庁ホームページ",
        status=Status.EXTERNAL_DEP,
        notes="Open Data 配信が無いため、京大データベース・大学経由配信・GPV/気象データサイト 等の検討が必要。",
    ),
    Product(
        key="jma_lfm",
        label_ja="JMA LFM (局地モデル、2km 日本)",
        label_en="JMA LFM (local model, 2km Japan)",
        tab=Tab.MODELS,
        category=Category.SHORT_RANGE,
        agency="JMA",
        spec="2km / 1h / T+0..T+10h, 24 cycles/day",
        source_url="https://www.jma.go.jp/jma/kishou/know/whitep/1-3-2.html",
        backend="気象業務支援センター 経由",
        license_info="出典: 気象庁ホームページ",
        status=Status.EXTERNAL_DEP,
    ),
    Product(
        key="ncep_hrrr",
        label_ja="NCEP HRRR (米 3km、毎時更新)",
        label_en="NCEP HRRR (NOAA 3km, hourly cycles)",
        tab=Tab.MODELS,
        category=Category.SHORT_RANGE,
        agency="NOAA / NCEP",
        spec="3km / 15min / T+0..T+18h or T+48h",
        source_url="https://rapidrefresh.noaa.gov/hrrr/",
        backend="s3://noaa-hrrr-bdp-pds",
        license_info="public domain",
        status=Status.PLANNED,
    ),

    # ── Extended / ensemble ──
    Product(
        key="ecmwf_ens",
        label_ja="ECMWF ENS (51 メンバーアンサンブル)",
        label_en="ECMWF ENS (51-member ensemble)",
        tab=Tab.MODELS,
        category=Category.EXTENDED,
        agency="ECMWF",
        spec="0.25° / 51 members + control / T+0..T+360h",
        source_url="https://www.ecmwf.int/en/forecasts/datasets/open-data",
        backend="ecmwf-opendata 'enfo' stream",
        license_info="CC-BY-4.0",
        status=Status.PLANNED,
        notes="Mean / spread / probability-of-exceedance / plume などの集約 view 必要。",
    ),
    Product(
        key="ecmwf_aifs_ens",
        label_ja="ECMWF AIFS Ensemble (AI アンサンブル)",
        label_en="ECMWF AIFS Ensemble (AI ensemble)",
        tab=Tab.MODELS,
        category=Category.EXTENDED,
        agency="ECMWF",
        spec="0.25° / メンバー数公表中 / 中期〜延長範囲",
        source_url="https://www.ecmwf.int/en/about/media-centre/aifs-blog",
        backend="ecmwf-opendata client with AIFS ensemble streams",
        license_info="CC-BY-4.0",
        status=Status.PLANNED,
    ),

    # ── Reanalysis ──
    Product(
        key="era5",
        label_ja="ECMWF ERA5 (再解析、0.25°、1940-現在)",
        label_en="ECMWF ERA5 (reanalysis, 0.25°, 1940-present)",
        tab=Tab.MODELS,
        category=Category.REANALYSIS,
        agency="ECMWF / Copernicus",
        spec="0.25° / 1h / 1940-present, ~250 surface + multi-level fields",
        source_url="https://cds.climate.copernicus.eu/datasets/reanalysis-era5-single-levels",
        backend="s3://ecmwf-era5 (Zarr), via xarray + fsspec[s3]",
        license_info="CC-BY-4.0",
        status=Status.PLANNED,
        notes="絶対日時軸（run+step ではない）。日時ピッカー UI が要る。気候値・偏差計算のベース。",
    ),
    Product(
        key="era5_land",
        label_ja="ECMWF ERA5-Land (陸面再解析、0.1°)",
        label_en="ECMWF ERA5-Land (land reanalysis, 0.1°)",
        tab=Tab.MODELS,
        category=Category.REANALYSIS,
        agency="ECMWF / Copernicus",
        spec="0.1° / 1h / 1950-present, 陸面特化 (土壌湿度、雪、河川)",
        source_url="https://cds.climate.copernicus.eu/datasets/reanalysis-era5-land",
        backend="Copernicus CDS API (要アカウント)",
        license_info="CC-BY-4.0",
        status=Status.EXTERNAL_DEP,
    ),
    Product(
        key="jra3q",
        label_ja="JMA JRA-3Q (気象庁第3世代再解析)",
        label_en="JMA JRA-3Q (third-generation Japanese reanalysis)",
        tab=Tab.MODELS,
        category=Category.REANALYSIS,
        agency="JMA",
        spec="TL479 (~40km) / 3h or 6h / 1947-present",
        source_url="https://jra.kishou.go.jp/JRA-3Q/index_en.html",
        backend="気象業務支援センター, アカデミック向け配布",
        license_info="JMA license",
        status=Status.EXTERNAL_DEP,
    ),

    # ────────────────────────── NOWCAST tab ──────────────────────────
    Product(
        key="jma_radar",
        label_ja="JMA 降水ナウキャスト (レーダー)",
        label_en="JMA rainfall nowcast (radar composite)",
        tab=Tab.NOWCAST,
        category=Category.RADAR,
        agency="JMA",
        spec="1km / 5min / 直近1時間 + T+0..T+60min 予報",
        source_url="https://www.jma.go.jp/bosai/nowc/",
        backend="JMA tile server (XYZ)",
        license_info="出典: 気象庁ホームページ",
        status=Status.IMPLEMENTED,
        notes="現状はサービス骨組みのみ。タイル合成と地図描画は TODO。",
    ),
    Product(
        key="jma_analysis_precip",
        label_ja="JMA 解析雨量",
        label_en="JMA analysis precipitation",
        tab=Tab.NOWCAST,
        category=Category.RADAR,
        agency="JMA",
        spec="1km / 30min / 雨量計とレーダー合成",
        source_url="https://www.jma.go.jp/bosai/nowc/",
        backend="JMA tile server",
        license_info="出典: 気象庁ホームページ",
        status=Status.PLANNED,
    ),
    Product(
        key="himawari",
        label_ja="ひまわり9号 (静止気象衛星)",
        label_en="Himawari-9 (geostationary satellite)",
        tab=Tab.NOWCAST,
        category=Category.SATELLITE,
        agency="JMA",
        spec="0.5-2km / 10min / 可視・赤外・水蒸気",
        source_url="https://www.data.jma.go.jp/mscweb/data/himawari/",
        backend="JMA imagery server, or s3://noaa-himawari9",
        license_info="出典: 気象庁ホームページ (NOAA 配信は public domain)",
        status=Status.PLANNED,
    ),
    Product(
        key="goes",
        label_ja="GOES-16/18 (米国静止衛星)",
        label_en="GOES-16/18 (U.S. geostationary)",
        tab=Tab.NOWCAST,
        category=Category.SATELLITE,
        agency="NOAA",
        spec="0.5-2km / 10min / ABI 全チャネル",
        source_url="https://registry.opendata.aws/noaa-goes/",
        backend="s3://noaa-goes16, s3://noaa-goes18",
        license_info="public domain",
        status=Status.PLANNED,
    ),
    Product(
        key="dwd_radar",
        label_ja="DWD RADOLAN (ドイツレーダー合成)",
        label_en="DWD RADOLAN (German radar composite)",
        tab=Tab.NOWCAST,
        category=Category.RADAR,
        agency="DWD",
        spec="1km / 5min / RADOLAN composite",
        source_url="https://opendata.dwd.de/weather/radar/",
        backend="DWD Open Data",
        license_info="DL-DE→BY-2.0",
        status=Status.PLANNED,
    ),
    Product(
        key="blitzortung",
        label_ja="Blitzortung 雷観測 (コミュニティ)",
        label_en="Blitzortung lightning network",
        tab=Tab.NOWCAST,
        category=Category.LIGHTNING,
        agency="Blitzortung.org",
        spec="リアルタイム雷検知 (遅延数十秒)、全球カバレッジ",
        source_url="https://www.blitzortung.org/",
        backend="WebSocket / HTTP stream (community)",
        license_info="non-commercial, attribution required",
        status=Status.PLANNED,
    ),

    # ────────────────────────── POINTS tab ──────────────────────────
    Product(
        key="jma_amedas",
        label_ja="JMA AMeDAS (地域気象観測網)",
        label_en="JMA AMeDAS (regional weather observations)",
        tab=Tab.POINTS,
        category=Category.OBSERVATION,
        agency="JMA",
        spec="~1300観測点 / 10min / 気温・降水・風・日照・積雪等",
        source_url="https://www.jma.go.jp/bosai/amedas/",
        backend="JMA REST endpoints (bosai)",
        license_info="出典: 気象庁ホームページ",
        status=Status.IMPLEMENTED,
        notes="現状はサービス骨組みのみ。地図描画と変数切替は TODO。",
    ),
    Product(
        key="metar",
        label_ja="METAR (空港気象通報)",
        label_en="METAR (aviation weather reports)",
        tab=Tab.POINTS,
        category=Category.OBSERVATION,
        agency="ICAO / NOAA",
        spec="世界 ~9000空港 / 30-60min / 国際標準フォーマット",
        source_url="https://aviationweather.gov/data/api/",
        backend="aviationweather.gov REST API",
        license_info="public domain",
        status=Status.PLANNED,
    ),
    Product(
        key="synop",
        label_ja="SYNOP (地上気象通報)",
        label_en="SYNOP (surface synoptic observations)",
        tab=Tab.POINTS,
        category=Category.OBSERVATION,
        agency="WMO",
        spec="全球地上観測点 / 1-6h / WMO 標準",
        source_url="https://www.ogimet.com/",
        backend="OGIMET, NOAA, etc.",
        license_info="public domain / various",
        status=Status.PLANNED,
    ),
    Product(
        key="radiosonde",
        label_ja="高層観測 (ラジオゾンデ)",
        label_en="Upper-air soundings (radiosonde)",
        tab=Tab.POINTS,
        category=Category.OBSERVATION,
        agency="WMO / Univ. Wyoming archive",
        spec="00/12 UTC / 全球 ~700地点 / 鉛直プロファイル",
        source_url="https://weather.uwyo.edu/upperair/sounding.html",
        backend="Univ. of Wyoming archive, IGRA (NOAA)",
        license_info="public domain",
        status=Status.PLANNED,
    ),
    Product(
        key="open_meteo",
        label_ja="Open-Meteo 地点予報 (multi-model)",
        label_en="Open-Meteo point forecast (multi-model)",
        tab=Tab.POINTS,
        category=Category.POINT_FORECAST,
        agency="Open-Meteo",
        spec="任意緯経度 / 1h / 各国モデルアンサンブル",
        source_url="https://open-meteo.com/",
        backend="open-meteo REST API",
        license_info="CC-BY-4.0",
        status=Status.IMPLEMENTED,
        notes="現状サービス層は実装済み、UI 統合が次のステップ。",
    ),
    Product(
        key="ecmwf_ens_plume",
        label_ja="ECMWF ENS plume (アンサンブルプルーム)",
        label_en="ECMWF ENS plume (ensemble plume)",
        tab=Tab.POINTS,
        category=Category.POINT_FORECAST,
        agency="ECMWF",
        spec="任意地点でアンサンブル全メンバーの時系列",
        source_url="https://www.ecmwf.int/en/forecasts/datasets/open-data",
        backend="dynamical.org Zarr (s3://dynamical-ecmwf-ifs-ens) 経由",
        license_info="CC-BY-4.0",
        status=Status.PLANNED,
    ),
)


def by_key(k: str) -> Product:
    for p in CATALOG:
        if p.key == k:
            return p
    raise KeyError(f"No product with key={k!r}")


def for_tab(tab: Tab) -> tuple[Product, ...]:
    return tuple(p for p in CATALOG if p.tab == tab)


def grouped_by_category(tab: Tab) -> list[tuple[Category, tuple[Product, ...]]]:
    """Return [(category, products_in_that_category), ...] for the tab.

    Categories appear in CATEGORY_ORDER; empty categories are omitted.
    Within a category, products keep their CATALOG declaration order.
    """
    by_cat: dict[Category, list[Product]] = {c: [] for c in CATEGORY_ORDER}
    for p in for_tab(tab):
        by_cat[p.category].append(p)
    return [
        (c, tuple(items)) for c in CATEGORY_ORDER if (items := by_cat[c])
    ]
