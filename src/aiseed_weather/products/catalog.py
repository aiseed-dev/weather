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
class DataSource:
    """One way to acquire a Product's data.

    A Product typically has multiple sources (an "official" path plus
    one or more cloud mirrors). The default chosen by each product is
    usually the fastest public mirror; users can override at runtime
    through the catalog dialog.
    """

    key: str            # stable identifier; ecmwf-opendata Client source name when applicable
    label: str          # short bilingual label for dropdown display
    endpoint: str       # URL or S3 bucket path
    transport: str      # "s3" | "http" | "zarr" | "opendap" | "websocket" | "ftp"
    region: str         # AWS region or geographic hint; empty if N/A
    status: Status
    notes: str = ""


# ECMWF Open Data sources are shared by every product served via the
# ecmwf-opendata Python client (HRES, AIFS Single, ENS, AIFS Ensemble).
# Source keys match the strings ecmwf-opendata's Client(source=...) accepts.
_ECMWF_OPENDATA_SOURCES: tuple[DataSource, ...] = (
    DataSource(
        key="aws",
        label="AWS (s3://ecmwf-forecasts, eu-central-1)",
        endpoint="s3://ecmwf-forecasts",
        transport="s3",
        region="eu-central-1",
        status=Status.IMPLEMENTED,
        notes="ecmwf-opendata の既定。低レイテンシ・広帯域。",
    ),
    DataSource(
        key="azure",
        label="Azure (Microsoft mirror, West Europe)",
        endpoint="https://ai4edataeuwest.blob.core.windows.net",
        transport="http",
        region="west-europe",
        status=Status.IMPLEMENTED,
        notes="ecmwf-opendata Client(source='azure')",
    ),
    DataSource(
        key="google",
        label="GCP (Google Cloud mirror, europe-west)",
        endpoint="gs://ecmwf-open-data",
        transport="http",
        region="europe-west",
        status=Status.IMPLEMENTED,
        notes="ecmwf-opendata Client(source='google')",
    ),
    DataSource(
        key="ecmwf",
        label="ECMWF Direct (data.ecmwf.int)",
        endpoint="https://data.ecmwf.int/forecasts",
        transport="http",
        region="reading-uk",
        status=Status.IMPLEMENTED,
        notes="本家サーバー。ミラーへの分散前段。混雑時は遅い。",
    ),
)


_NCEP_SOURCES: tuple[DataSource, ...] = (
    DataSource(
        key="aws_s3",
        label="AWS (s3://noaa-gfs-bdp-pds, us-east-1)",
        endpoint="s3://noaa-gfs-bdp-pds",
        transport="s3",
        region="us-east-1",
        status=Status.PLANNED,
        notes="NOAA Big Data Program ミラー。Requester pays では無い。",
    ),
    DataSource(
        key="nomads",
        label="NOMADS Direct (nomads.ncep.noaa.gov)",
        endpoint="https://nomads.ncep.noaa.gov",
        transport="http",
        region="us-east",
        status=Status.PLANNED,
    ),
    DataSource(
        key="gcp",
        label="GCP (gs://global-forecast-system)",
        endpoint="gs://global-forecast-system",
        transport="http",
        region="us-central",
        status=Status.PLANNED,
    ),
)


_ERA5_SOURCES: tuple[DataSource, ...] = (
    DataSource(
        key="aws_zarr",
        label="AWS Zarr (s3://ecmwf-era5)",
        endpoint="s3://ecmwf-era5",
        transport="zarr",
        region="eu-central-1",
        status=Status.PLANNED,
        notes="ECMWF 公式の Zarr ミラー。xarray + fsspec[s3] で直接読み込み可能。",
    ),
    DataSource(
        key="cds_api",
        label="Copernicus CDS API (要アカウント)",
        endpoint="https://cds.climate.copernicus.eu/api/v2",
        transport="http",
        region="reading-uk",
        status=Status.EXTERNAL_DEP,
        notes="個人アカウント必須。レート制限あり。",
    ),
    DataSource(
        key="planetary_computer",
        label="Microsoft Planetary Computer",
        endpoint="https://planetarycomputer.microsoft.com",
        transport="http",
        region="west-europe",
        status=Status.PLANNED,
    ),
)


_JMA_BOSAI_SOURCE = DataSource(
    key="jma_bosai",
    label="JMA bosai (www.jma.go.jp/bosai/...)",
    endpoint="https://www.jma.go.jp/bosai",
    transport="http",
    region="japan",
    status=Status.IMPLEMENTED,
    notes="気象庁の公式ナウキャスト/防災情報 REST。",
)


_JMA_SUPPORT_CENTER_SOURCES: tuple[DataSource, ...] = (
    DataSource(
        key="kishou_center",
        label="気象業務支援センター",
        endpoint="http://www.jmbsc.or.jp/",
        transport="http",
        region="japan",
        status=Status.EXTERNAL_DEP,
        notes="JMA メソモデル等の公式配信元。契約必要。",
    ),
    DataSource(
        key="kyoto_db",
        label="京大 生存圏研究所 GPV データベース",
        endpoint="http://database.rish.kyoto-u.ac.jp/arch/jmadata/",
        transport="http",
        region="japan",
        status=Status.EXTERNAL_DEP,
        notes="アカデミック向け再配布。MSM など。要確認。",
    ),
)


_OPEN_METEO_SOURCE = DataSource(
    key="open_meteo_api",
    label="Open-Meteo API (api.open-meteo.com)",
    endpoint="https://api.open-meteo.com",
    transport="http",
    region="europe",
    status=Status.IMPLEMENTED,
)


@dataclass(frozen=True)
class Product:
    key: str
    label_ja: str
    label_en: str
    tab: Tab
    category: Category
    agency: str
    spec: str             # "0.25° / 3h / T+0..T+360h" etc
    source_url: str
    backend: str          # short description of how we'd fetch it
    license_info: str
    status: Status
    notes: str = ""
    # Possible data acquisition paths. The first entry is the catalog-
    # level default; UI may override (e.g. ECMWF HRES picks up the
    # initial source from config.toml's forecast_source instead).
    sources: tuple[DataSource, ...] = ()
    default_source_key: str = ""
    # "atomic"      – every step of a cycle becomes available at the
    #                 same moment (typical of HRES IFS via Open Data).
    # "progressive" – each step is published as it's produced; the run
    #                 trickles out over a window (AIFS, typical of AI
    #                 inference where step N ships independently).
    # "unknown"     – not yet researched; used for catalog entries
    #                 we have not validated against the upstream.
    publication_mode: str = "unknown"
    # Average lag from cycle nominal time to when the cycle is fully
    # available, in hours. For atomic publishers this is the single
    # publication time; for progressive ones it's the time the LAST
    # step appears.
    publication_lag_h: float | None = None
    # Compact label for the left-panel display. The full bilingual
    # label_ja / label_en stays available for the catalog dialog and
    # the info-icon tooltip. Empty → display_name() falls back to
    # label_ja.
    short_label: str = ""

    def bilingual_label(self) -> str:
        # Display "日本語 / English" while we don't have language settings.
        if self.label_ja == self.label_en:
            return self.label_ja
        return f"{self.label_ja} / {self.label_en}"

    def display_name(self) -> str:
        """Short, single-line name for the prominent UI slot."""
        return self.short_label or self.label_ja

    def source_by_key(self, key: str) -> DataSource:
        for s in self.sources:
            if s.key == key:
                return s
        raise KeyError(f"Product {self.key!r} has no source {key!r}")


CATALOG: tuple[Product, ...] = (
    # ────────────────────────── MODELS tab ──────────────────────────
    # ── Medium range forecasts ──
    Product(
        key="ecmwf_hres",
        label_ja="ECMWF HRES (IFS 0.25° 決定論)",
        label_en="ECMWF HRES (IFS 0.25° deterministic)",
        short_label="ECMWF HRES (IFS 0.25°)",
        tab=Tab.MODELS,
        category=Category.MEDIUM_RANGE,
        agency="ECMWF",
        spec="0.25° / 00z+12z は T+360h まで (0..144h 3h, 144..360h 6h); "
             "06z+18z は T+144h まで 3h / 4 cycles/day (00,06,12,18 UTC)",
        source_url="https://www.ecmwf.int/en/forecasts/datasets/open-data",
        backend="ecmwf-opendata client → GRIB2 from s3://ecmwf-forecasts",
        license_info="CC-BY-4.0",
        status=Status.IMPLEMENTED,
        sources=_ECMWF_OPENDATA_SOURCES,
        default_source_key="aws",
        publication_mode="atomic",
        publication_lag_h=7.5,
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
        notes="HRES と同じ Open Data backend だが配信モデルが違う: "
              "AI 推論は step ごとに完成次第アップロードされる progressive "
              "publication なので、UI は step ごとに公開状況を probe する "
              "必要がある (HRES の atomic な 7.5h ラグの仮定は使えない)。",
        sources=_ECMWF_OPENDATA_SOURCES,
        default_source_key="aws",
        publication_mode="progressive",
        # First steps appear quickly (~cycle+1h?); full T+360h takes ~3-4h.
        # Empirical confirmation pending.
        publication_lag_h=4.0,
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
        sources=_NCEP_SOURCES,
        default_source_key="aws_s3",
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
        sources=(
            DataSource(
                key="dwd_opendata",
                label="DWD Open Data (opendata.dwd.de)",
                endpoint="https://opendata.dwd.de/weather/nwp/icon-eu",
                transport="http",
                region="germany",
                status=Status.PLANNED,
            ),
        ),
        default_source_key="dwd_opendata",
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
        sources=_JMA_SUPPORT_CENTER_SOURCES,
        default_source_key="kyoto_db",
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
        sources=_JMA_SUPPORT_CENTER_SOURCES,
        default_source_key="kyoto_db",
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
        sources=(
            DataSource(
                key="aws_s3",
                label="AWS (s3://noaa-hrrr-bdp-pds, us-east-1)",
                endpoint="s3://noaa-hrrr-bdp-pds",
                transport="s3",
                region="us-east-1",
                status=Status.PLANNED,
            ),
        ),
        default_source_key="aws_s3",
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
        sources=_ECMWF_OPENDATA_SOURCES,
        default_source_key="aws",
        publication_mode="atomic",
        publication_lag_h=7.5,
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
        notes="AIFS Single と同じく progressive publication の見込み。",
        sources=_ECMWF_OPENDATA_SOURCES,
        default_source_key="aws",
        publication_mode="progressive",
        publication_lag_h=5.0,
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
        sources=_ERA5_SOURCES,
        default_source_key="aws_zarr",
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
        sources=(
            DataSource(
                key="cds_api",
                label="Copernicus CDS API (要アカウント)",
                endpoint="https://cds.climate.copernicus.eu/api/v2",
                transport="http",
                region="reading-uk",
                status=Status.EXTERNAL_DEP,
            ),
        ),
        default_source_key="cds_api",
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
        sources=_JMA_SUPPORT_CENTER_SOURCES,
        default_source_key="kishou_center",
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
        sources=(_JMA_BOSAI_SOURCE,),
        default_source_key="jma_bosai",
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
        sources=(_JMA_BOSAI_SOURCE,),
        default_source_key="jma_bosai",
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
        sources=(
            DataSource(
                key="aws_noaa",
                label="AWS (s3://noaa-himawari9, us-east-1)",
                endpoint="s3://noaa-himawari9",
                transport="s3",
                region="us-east-1",
                status=Status.PLANNED,
                notes="NOAA Big Data Program ミラー。",
            ),
            DataSource(
                key="jma_msc",
                label="JMA MSC (data.jma.go.jp/mscweb)",
                endpoint="https://www.data.jma.go.jp/mscweb/data/himawari/",
                transport="http",
                region="japan",
                status=Status.PLANNED,
                notes="JMA 公式配信。レート制限あり。",
            ),
        ),
        default_source_key="aws_noaa",
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
        sources=(
            DataSource(
                key="aws_east",
                label="AWS (s3://noaa-goes16, GOES-East, us-east-1)",
                endpoint="s3://noaa-goes16",
                transport="s3",
                region="us-east-1",
                status=Status.PLANNED,
            ),
            DataSource(
                key="aws_west",
                label="AWS (s3://noaa-goes18, GOES-West, us-east-1)",
                endpoint="s3://noaa-goes18",
                transport="s3",
                region="us-east-1",
                status=Status.PLANNED,
            ),
            DataSource(
                key="gcp",
                label="GCP (gs://gcp-public-data-goes-16)",
                endpoint="gs://gcp-public-data-goes-16",
                transport="http",
                region="us-central",
                status=Status.PLANNED,
            ),
        ),
        default_source_key="aws_east",
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
        sources=(
            DataSource(
                key="dwd_opendata",
                label="DWD Open Data (opendata.dwd.de)",
                endpoint="https://opendata.dwd.de/weather/radar",
                transport="http",
                region="germany",
                status=Status.PLANNED,
            ),
        ),
        default_source_key="dwd_opendata",
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
        sources=(
            DataSource(
                key="ws_community",
                label="Blitzortung WebSocket (community)",
                endpoint="wss://ws.blitzortung.org/",
                transport="websocket",
                region="global",
                status=Status.PLANNED,
            ),
        ),
        default_source_key="ws_community",
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
        sources=(_JMA_BOSAI_SOURCE,),
        default_source_key="jma_bosai",
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
        sources=(
            DataSource(
                key="awc",
                label="NOAA Aviation Weather Center (aviationweather.gov)",
                endpoint="https://aviationweather.gov/data/api",
                transport="http",
                region="us",
                status=Status.PLANNED,
            ),
        ),
        default_source_key="awc",
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
        sources=(
            DataSource(
                key="ogimet",
                label="OGIMET (ogimet.com)",
                endpoint="https://www.ogimet.com/",
                transport="http",
                region="europe",
                status=Status.PLANNED,
            ),
            DataSource(
                key="noaa_isd",
                label="NOAA ISD (s3://noaa-global-hourly-pds)",
                endpoint="s3://noaa-global-hourly-pds",
                transport="s3",
                region="us-east-1",
                status=Status.PLANNED,
            ),
        ),
        default_source_key="ogimet",
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
        sources=(
            DataSource(
                key="wyoming",
                label="Univ. of Wyoming (weather.uwyo.edu)",
                endpoint="https://weather.uwyo.edu/upperair/sounding.html",
                transport="http",
                region="us-west",
                status=Status.PLANNED,
            ),
            DataSource(
                key="igra",
                label="NOAA IGRA v2 (ncei.noaa.gov)",
                endpoint="https://www.ncei.noaa.gov/data/integrated-global-radiosonde-archive",
                transport="http",
                region="us-east",
                status=Status.PLANNED,
            ),
        ),
        default_source_key="wyoming",
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
        sources=(_OPEN_METEO_SOURCE,),
        default_source_key="open_meteo_api",
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
        sources=(
            DataSource(
                key="dynamical_zarr",
                label="dynamical.org Zarr (s3://dynamical-ecmwf-ifs-ens)",
                endpoint="s3://dynamical-ecmwf-ifs-ens",
                transport="zarr",
                region="us-east-1",
                status=Status.PLANNED,
                notes="dynamical.org がメンテするアンサンブル Zarr ミラー。",
            ),
        ),
        default_source_key="dynamical_zarr",
    ),
)


@dataclass(frozen=True)
class DataField:
    """A specific meteorological field that a model produces.

    "Model decides where the bytes come from; Data decides what to
    fetch from those bytes; Layer decides how to draw them."

    Most numerical models share a common subset of fields (MSL, T2m,
    geopotential at standard pressure levels, etc.), so we list them
    once here. Whether any given model actually publishes a given
    field is a separate (per-product) consideration that's not modeled
    yet — for now status flags whether we render it.
    """

    key: str             # our internal short name (msl, t2m, gh500, ...)
    label_ja: str
    label_en: str
    unit: str            # display unit
    level: int | None    # pressure level in hPa; None = surface / single-level
    typical_layer: str   # default rendering style we apply
    status: Status
    # ECMWF Open Data param string (passed to ecmwf-opendata Client). Not
    # always the same as our key — ECMWF uses "2t" while we use "t2m" for
    # readability. Empty string for derived fields with no single param.
    ecmwf_param: str = ""
    notes: str = ""

    def bilingual_label(self) -> str:
        if self.label_ja == self.label_en:
            return self.label_ja
        return f"{self.label_ja} / {self.label_en}"

    def level_suffix(self) -> str:
        return "" if self.level is None else f" @ {self.level} hPa"


FIELDS: tuple[DataField, ...] = (
    # ---- Surface ----
    DataField(
        key="msl",
        label_ja="海面更正気圧",
        label_en="Mean sea level pressure",
        unit="hPa",
        level=None,
        typical_layer=(
            "発散カラー (1013 hPa 中心) + 等圧線 4 hPa / 太線 20 hPa"
        ),
        status=Status.IMPLEMENTED,
        ecmwf_param="msl",
    ),
    DataField(
        key="t2m",
        label_ja="2m気温",
        label_en="2-metre temperature",
        unit="°C",
        level=None,
        typical_layer="カラーシェーディング 2°C, 0°C 太線",
        status=Status.IMPLEMENTED,
        ecmwf_param="2t",
    ),
    DataField(
        key="d2m",
        label_ja="2m露点温度",
        label_en="2-metre dewpoint temperature",
        unit="°C",
        level=None,
        typical_layer="カラーシェーディング 2°C, 0°C 太線",
        status=Status.IMPLEMENTED,
        ecmwf_param="2d",
    ),
    DataField(
        key="skt",
        label_ja="地表温度",
        label_en="Skin temperature",
        unit="°C",
        level=None,
        typical_layer="カラーシェーディング 2°C, 0°C 太線",
        status=Status.IMPLEMENTED,
        ecmwf_param="skt",
        notes="地表面の放射温度。土壌・植生・水面・雪面を反映するため、"
              "標準的な気温予報には出ない地形依存パターンが見える。",
    ),
    DataField(
        key="sd",
        label_ja="積雪深 (水当量)",
        label_en="Snow depth (water equivalent)",
        unit="m",
        level=None,
        typical_layer="非線形カラーシェーディング (cm→m)",
        status=Status.IMPLEMENTED,
        ecmwf_param="sd",
        notes="ECMWF は雪水当量で公表 (積雪 1 m ≒ SWE 0.1-0.3 m)。"
              "Windy 等の一般予報には出ない量。",
    ),
    DataField(
        key="tcc",
        label_ja="全雲量",
        label_en="Total cloud cover",
        unit="0..1",
        level=None,
        typical_layer="モノクロシェーディング (0=快晴, 1=曇天)",
        status=Status.IMPLEMENTED,
        ecmwf_param="tcc",
    ),
    DataField(
        key="wind10m",
        label_ja="10m風",
        label_en="10-metre wind (u, v)",
        unit="m/s",
        level=None,
        typical_layer="風速シェーディング + 矢印 (ECMWF風)",
        status=Status.IMPLEMENTED,
        ecmwf_param="10u/10v",  # multi-param: single GRIB with both fields
        notes="ECMWF Open Data の 10u/10v を 1 ファイルで取得して合成描画。"
              "粒子アニメーションは未実装、当面は静的矢印。",
    ),
    DataField(
        key="gust",
        label_ja="突風 (10m)",
        label_en="10-metre wind gust",
        unit="m/s",
        level=None,
        typical_layer="カラーシェーディング",
        status=Status.PLANNED,
        ecmwf_param="10fg",
    ),
    DataField(
        key="tp",
        label_ja="積算降水量",
        label_en="Total precipitation",
        unit="mm",
        level=None,
        typical_layer="非線形カラーシェーディング (0.1/1/5/10/30/50/100 mm)",
        status=Status.IMPLEMENTED,
        ecmwf_param="tp",
        notes="ECMWF Open Data は accumulated since run start; 区間値は差分計算。",
    ),
    DataField(
        key="sp",
        label_ja="地表気圧",
        label_en="Surface pressure",
        unit="hPa",
        level=None,
        typical_layer="等圧線",
        status=Status.PLANNED,
        ecmwf_param="sp",
    ),
    # ---- Pressure levels ----
    # Generated as one DataField per (variable, level) combination so
    # the layer-dialog matrix has clean "variable row × level column"
    # navigation. All marked IMPLEMENTED because render_pool dispatches
    # them through the generic _scalar_chart pipeline.
    DataField(
        key="gh925", label_ja="925hPa高度",
        label_en="Geopotential height (925 hPa)",
        unit="m", level=925,
        typical_layer="等高度線", status=Status.IMPLEMENTED,
        ecmwf_param="gh",
    ),
    DataField(
        key="gh850", label_ja="850hPa高度",
        label_en="Geopotential height (850 hPa)",
        unit="m", level=850,
        typical_layer="等高度線", status=Status.IMPLEMENTED,
        ecmwf_param="gh",
    ),
    DataField(
        key="gh700", label_ja="700hPa高度",
        label_en="Geopotential height (700 hPa)",
        unit="m", level=700,
        typical_layer="等高度線", status=Status.IMPLEMENTED,
        ecmwf_param="gh",
    ),
    DataField(
        key="gh500", label_ja="500hPa高度",
        label_en="Geopotential height (500 hPa)",
        unit="m", level=500,
        typical_layer="等高度線 60 m, 5640 m 太線",
        status=Status.IMPLEMENTED, ecmwf_param="gh",
    ),
    DataField(
        key="gh300", label_ja="300hPa高度",
        label_en="Geopotential height (300 hPa)",
        unit="m", level=300,
        typical_layer="等高度線", status=Status.IMPLEMENTED,
        ecmwf_param="gh",
    ),
    DataField(
        key="gh200", label_ja="200hPa高度",
        label_en="Geopotential height (200 hPa)",
        unit="m", level=200,
        typical_layer="等高度線", status=Status.IMPLEMENTED,
        ecmwf_param="gh",
    ),
    DataField(
        key="t925", label_ja="925hPa気温",
        label_en="Temperature (925 hPa)",
        unit="°C", level=925,
        typical_layer="カラーシェーディング 4°C", status=Status.IMPLEMENTED,
        ecmwf_param="t",
    ),
    DataField(
        key="t850", label_ja="850hPa気温",
        label_en="Temperature (850 hPa)",
        unit="°C", level=850,
        typical_layer="カラーシェーディング 4°C, 0°C 太線",
        status=Status.IMPLEMENTED, ecmwf_param="t",
    ),
    DataField(
        key="t700", label_ja="700hPa気温",
        label_en="Temperature (700 hPa)",
        unit="°C", level=700,
        typical_layer="カラーシェーディング 4°C", status=Status.IMPLEMENTED,
        ecmwf_param="t",
    ),
    DataField(
        key="t500", label_ja="500hPa気温",
        label_en="Temperature (500 hPa)",
        unit="°C", level=500,
        typical_layer="カラーシェーディング 4°C", status=Status.IMPLEMENTED,
        ecmwf_param="t",
    ),
    DataField(
        key="t300", label_ja="300hPa気温",
        label_en="Temperature (300 hPa)",
        unit="°C", level=300,
        typical_layer="カラーシェーディング 4°C", status=Status.IMPLEMENTED,
        ecmwf_param="t",
    ),
    DataField(
        key="wind850", label_ja="850hPa風速",
        label_en="Wind speed (850 hPa)",
        unit="m/s", level=850,
        typical_layer="風速シェーディング", status=Status.IMPLEMENTED,
        ecmwf_param="u/v",
        notes="u/v 成分から √(u²+v²) を計算。方向矢印は未実装。",
    ),
    DataField(
        key="wind500", label_ja="500hPa風速",
        label_en="Wind speed (500 hPa)",
        unit="m/s", level=500,
        typical_layer="風速シェーディング", status=Status.IMPLEMENTED,
        ecmwf_param="u/v",
    ),
    DataField(
        key="wind250", label_ja="250hPa風速 (ジェット)",
        label_en="Wind speed (250 hPa, jet)",
        unit="m/s", level=250,
        typical_layer="風速シェーディング", status=Status.IMPLEMENTED,
        ecmwf_param="u/v",
        notes="対流圏上部のジェット気流。100 m/s 級のコアが見える。",
    ),
    DataField(
        key="w700", label_ja="700hPa鉛直流",
        label_en="Vertical velocity ω (700 hPa)",
        unit="Pa/s", level=700,
        typical_layer="上昇流域シェーディング (負値=上昇)",
        status=Status.IMPLEMENTED, ecmwf_param="w",
    ),
    DataField(
        key="w500", label_ja="500hPa鉛直流",
        label_en="Vertical velocity ω (500 hPa)",
        unit="Pa/s", level=500,
        typical_layer="上昇流域シェーディング",
        status=Status.IMPLEMENTED, ecmwf_param="w",
    ),
    DataField(
        key="r925", label_ja="925hPa相対湿度",
        label_en="Relative humidity (925 hPa)",
        unit="%", level=925,
        typical_layer="湿域シェーディング", status=Status.IMPLEMENTED,
        ecmwf_param="r",
    ),
    DataField(
        key="r850", label_ja="850hPa相対湿度",
        label_en="Relative humidity (850 hPa)",
        unit="%", level=850,
        typical_layer="湿域シェーディング", status=Status.IMPLEMENTED,
        ecmwf_param="r",
    ),
    DataField(
        key="r700", label_ja="700hPa相対湿度",
        label_en="Relative humidity (700 hPa)",
        unit="%", level=700,
        typical_layer="湿域シェーディング (>70%)", status=Status.IMPLEMENTED,
        ecmwf_param="r",
    ),
    # ---- Derived ----
    DataField(
        key="thickness_500_1000",
        label_ja="500-1000hPa層厚",
        label_en="500-1000 hPa thickness",
        unit="m",
        level=None,
        typical_layer="等層厚線 60 m, 5400m太線",
        status=Status.PLANNED,
        notes="gh@500 − gh@1000 で計算。",
    ),
    DataField(
        key="theta_e_850",
        label_ja="850hPa相当温位",
        label_en="850 hPa equivalent potential temperature",
        unit="K",
        level=850,
        typical_layer="等値線 3 K, 高 θe 域シェーディング",
        status=Status.PLANNED,
        notes="気温・湿度・気圧から計算。",
    ),
)


def field_by_key(k: str) -> DataField:
    for f in FIELDS:
        if f.key == k:
            return f
    raise KeyError(f"No field with key={k!r}")


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
