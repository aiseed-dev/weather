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
# Order matters: this list drives both the catalog dialog ordering
# and which mirror the bench/docs treat as the canonical first
# choice. Google is first because we now download the bulk
# oper-fc.grib2 per step rather than Range-fetching individual
# params, and GCS is the only mirror that edge-caches into
# Asia-Pacific. From Japan it's ~1-2 s/step; AWS Frankfurt is
# ~20-30 s for the same payload. See tests/test_download_bench.py.
_ECMWF_OPENDATA_SOURCES: tuple[DataSource, ...] = (
    DataSource(
        key="google",
        label="GCP (Google Cloud mirror, edge-cached)",
        endpoint="https://storage.googleapis.com/ecmwf-open-data",
        transport="http",
        region="global-edge",
        status=Status.IMPLEMENTED,
        notes=(
            "推奨。Asia-Pacific エッジから配信されるため日本から "
            "150 MB の per-step bulk が 1-2 秒で取れる。"
        ),
    ),
    DataSource(
        key="aws",
        label="AWS (s3://ecmwf-forecasts, eu-central-1)",
        endpoint="s3://ecmwf-forecasts",
        transport="s3",
        region="eu-central-1",
        status=Status.IMPLEMENTED,
        notes=(
            "Frankfurt 直配信。欧州/北米からは速いが、日本からは "
            "RTT がそのまま乗って 1 ステップ 20-30 秒。"
        ),
    ),
    DataSource(
        key="azure",
        label="Azure (Microsoft mirror, West Europe)",
        endpoint="https://ai4edataeuwest.blob.core.windows.net",
        transport="http",
        region="west-europe",
        status=Status.IMPLEMENTED,
        notes="AWS と同じ欧州配置。レイテンシ特性も近い。",
    ),
    DataSource(
        key="ecmwf",
        label="ECMWF Direct (data.ecmwf.int)",
        endpoint="https://data.ecmwf.int/forecasts",
        transport="http",
        region="reading-uk",
        status=Status.IMPLEMENTED,
        notes=(
            "本家サーバー。500-connection 制限で 403 を返すことが多い。"
            "ミラーが全部落ちた時の最後の手段。"
        ),
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

    @property
    def kind(self) -> str:
        """Which multi-band GRIB file this field lives in.

        ``"sfc"`` for surface / single-level fields (msl, 2t, sd, …);
        ``"pl"`` for pressure-level fields (gh / t / u / v / w / r at
        a specific hPa level);
        ``"sol"`` for soil-layer fields (sot / vsw at layer 1–4).

        The forecast service downloads one multi-band GRIB per
        ``(cycle, step, kind)``; layer change within the same kind
        doesn't trigger a fetch.
        """
        if self.ecmwf_param in _SOIL_PARAMS:
            return "sol"
        return "pl" if self.level is not None else "sfc"


# ECMWF Open Data soil-layer params live under levtype="sol" with
# levelist=[1,2,3,4]. Anchor the discriminator here so DataField.kind
# can detect them before the catalogue itself is built.
_SOIL_PARAMS: frozenset[str] = frozenset(("sot", "vsw"))


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
        typical_layer="風速シェーディング",
        status=Status.IMPLEMENTED,
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
        status=Status.IMPLEMENTED,
        ecmwf_param="sp",
    ),
    # ---- Pressure levels ----
    # Generated programmatically across every (param, level) in
    # ECMWF Open Data's pl product. The forecast service downloads
    # all of these in a single multi-band GRIB per cycle/step (one
    # request per kind), so switching variable or level at view time
    # is a pure re-read — no network. See
    # .agents/skills/data-flow/SKILL.md.
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


# ── Pressure-level catalogue (generated) ────────────────────────────
# ECMWF Open Data publishes 9 variables on 13 standard hPa levels. We
# expose every combination so the layer-dialog matrix presents the
# full grid; the forecast service bundles all of them into one
# multi-band GRIB per kind per step, so the catalogue size does not
# multiply network round-trips.

PRESSURE_LEVELS_HPA: tuple[int, ...] = (
    1000, 925, 850, 700, 600, 500, 400, 300, 250, 200, 150, 100, 50,
)

# (ecmwf short, ja label, en label, unit, default style hint)
PRESSURE_VARIABLES: tuple[tuple[str, str, str, str, str], ...] = (
    ("gh", "高度",       "Geopotential height", "m",     "等高度線・カラーシェーディング"),
    ("t",  "気温",       "Temperature",         "°C",    "カラーシェーディング 4°C"),
    ("u",  "東西風成分", "U-component of wind", "m/s",   "西風 (正) ↔ 東風 (負) シェーディング"),
    ("v",  "南北風成分", "V-component of wind", "m/s",   "北風 (正) ↔ 南風 (負) シェーディング"),
    ("w",  "鉛直流",     "Vertical velocity ω", "Pa/s",  "上昇流 (負) シェーディング"),
    ("r",  "相対湿度",   "Relative humidity",   "%",     "湿域シェーディング"),
    ("q",  "比湿",       "Specific humidity",   "kg/kg", "水蒸気量シェーディング"),
    ("d",  "発散",       "Divergence",          "1/s",   "発散 (正) ↔ 収束 (負) シェーディング"),
    ("vo", "渦度",       "Vorticity (relative)", "1/s",  "正渦 (反時計) ↔ 負渦 (時計) シェーディング"),
)

_PRESSURE_FIELDS = tuple(
    DataField(
        key=f"{var}{level}",
        label_ja=f"{level}hPa{ja}",
        label_en=f"{en} ({level} hPa)",
        unit=unit,
        level=level,
        typical_layer=style,
        status=Status.IMPLEMENTED,
        ecmwf_param=var,
    )
    for var, ja, en, unit, style in PRESSURE_VARIABLES
    for level in PRESSURE_LEVELS_HPA
)

# Derived: √(u² + v²) — wind speed — at every pressure level. Sits
# next to the u and v rows in the matrix so the user can pick either
# the components or the magnitude.
_WIND_SPEED_FIELDS = tuple(
    DataField(
        key=f"wind{level}",
        label_ja=f"{level}hPa風速",
        label_en=f"Wind speed ({level} hPa)",
        unit="m/s",
        level=level,
        typical_layer="風速シェーディング",
        status=Status.IMPLEMENTED,
        # ecmwf_param 'u/v' marks this as a derived field; the
        # service-side _params_for_kind splits on '/' so both
        # components are still in the multi-band pl GRIB.
        ecmwf_param="u/v",
    )
    for level in PRESSURE_LEVELS_HPA
)

FIELDS = FIELDS + _PRESSURE_FIELDS + _WIND_SPEED_FIELDS


# ── Surface catalogue (generated) ──────────────────────────────────
# Every short_name ECMWF Open Data publishes on the sfc surface
# product set. The most-used ones (msl, t2m, d2m, skt, sd, tcc,
# wind10m, tp) are kept as the explicit entries above so the panel
# chips have a curated short-list to draw from; the rest land here.
# Variables we don't yet have a sensible palette for stay
# Status.PLANNED.

# (ecmwf_param, key, ja, en, unit, status, notes)
_SURFACE_NEW: tuple[tuple[str, str, str, str, str, Status, str], ...] = (
    # ── Wind components (separate from the combined wind10m) ──
    ("10u",   "u10m",  "10m東西風成分",   "10m U wind",            "m/s",   Status.IMPLEMENTED, ""),
    ("10v",   "v10m",  "10m南北風成分",   "10m V wind",            "m/s",   Status.IMPLEMENTED, ""),
    ("100u",  "u100m", "100m東西風成分",  "100m U wind",           "m/s",   Status.IMPLEMENTED, ""),
    ("100v",  "v100m", "100m南北風成分",  "100m V wind",           "m/s",   Status.IMPLEMENTED, ""),
    # ── Temperature statistics ──
    ("mn2t3", "mn2t3", "3時間最低気温",   "Min 2t (last 3h)",      "°C",    Status.IMPLEMENTED, ""),
    ("mx2t3", "mx2t3", "3時間最高気温",   "Max 2t (last 3h)",      "°C",    Status.IMPLEMENTED, ""),
    ("mn2t6", "mn2t6", "6時間最低気温",   "Min 2t (last 6h)",      "°C",    Status.IMPLEMENTED, ""),
    ("mx2t6", "mx2t6", "6時間最高気温",   "Max 2t (last 6h)",      "°C",    Status.IMPLEMENTED, ""),
    # ── Pressure (surface, vs MSL) ──
    # (sp explicit entry above is PLANNED; promote here)
    # ── Precipitation extras ──
    ("tprate","tprate","降水強度",        "Total precipitation rate","mm/h",  Status.IMPLEMENTED, "瞬時値 (検証時刻). NWP は強雨域の精度が低く 線状降水帯 / ゲリラ豪雨は再現不能 — レーダー観測と同じ palette にしてはならない"),
    ("ptype", "ptype", "降水タイプ",      "Precipitation type",    "—",     Status.IMPLEMENTED, "離散カテゴリ: 0=なし,1=雨,3=凍結雨,5=雪,6=みぞれ,7=湿雪,8=雹"),
    ("ro",    "ro",    "流出量 (累積)",   "Runoff",                "m",     Status.IMPLEMENTED, ""),
    # ── Snow extras ──
    ("sf",    "sf",    "降雪量 (SWE)",    "Snowfall (SWE)",        "m",     Status.IMPLEMENTED, ""),
    ("asn",   "asn",   "雪面アルベド",    "Snow albedo",           "0..1",  Status.IMPLEMENTED, ""),
    ("rsn",   "rsn",   "雪密度",          "Snow density",          "kg/m³", Status.IMPLEMENTED, ""),
    # ── Atmospheric moisture column ──
    ("tcwv",  "tcwv",  "可降水量",        "TCWV",                  "kg/m²", Status.IMPLEMENTED, ""),
    # ── CAPE ──
    ("mucape","mucape","最不安定 CAPE",   "MU-CAPE",               "J/kg",  Status.IMPLEMENTED, ""),
    # ── Radiation (累積 J/m²; ECMWF Open Data 仕様) ──
    ("ssr",   "ssr",   "正味短波 (累積)", "Surface net SW",        "J/m²",  Status.IMPLEMENTED, ""),
    ("ssrd",  "ssrd",  "下向き短波 (累積)", "Surface SW down",     "J/m²",  Status.IMPLEMENTED, ""),
    ("str",   "str_lw","正味長波 (累積)", "Surface net LW",        "J/m²",  Status.IMPLEMENTED, "ECMWF 'str' but key avoids Python str() collision in dict access"),
    ("strd",  "strd",  "下向き長波 (累積)", "Surface LW down",     "J/m²",  Status.IMPLEMENTED, ""),
    ("ttr",   "ttr",   "OLR 上端長波 (累積)", "TOA net LW (OLR)",  "J/m²",  Status.IMPLEMENTED, ""),
    # ── Turbulent stress (時間積分) ──
    ("ewss",  "ewss",  "東向き応力 (累積)", "Eastward stress",     "N/m²",  Status.IMPLEMENTED, ""),
    ("nsss",  "nsss",  "北向き応力 (累積)", "Northward stress",    "N/m²",  Status.IMPLEMENTED, ""),
    # ── Wave / sea-state ──
    ("swh",   "swh",   "有義波高",        "Sig. wave height",      "m",     Status.IMPLEMENTED, ""),
    ("mwp",   "mwp",   "平均波周期",      "Mean wave period",      "s",     Status.IMPLEMENTED, ""),
    ("mp2",   "mp2",   "ゼロクロス周期",  "Mean zero-crossing T",  "s",     Status.IMPLEMENTED, ""),
    ("pp1d",  "pp1d",  "ピーク波周期",    "Peak wave period",      "s",     Status.IMPLEMENTED, ""),
    ("mwd",   "mwd",   "平均波向",        "Mean wave direction",   "deg",   Status.IMPLEMENTED, "円形量 (0..360°) — 周期的 HSV パレット"),
    # ── Sea / sea-ice ──
    ("sve",   "sve",   "東向き海流",      "Eastward sea velocity", "m/s",   Status.IMPLEMENTED, ""),
    ("svn",   "svn",   "北向き海流",      "Northward sea velocity","m/s",   Status.IMPLEMENTED, ""),
    ("sithick","sithick","海氷厚",        "Sea ice thickness",     "m",     Status.IMPLEMENTED, ""),
    ("zos",   "zos",   "海面高度",        "Sea surface height",    "m",     Status.IMPLEMENTED, ""),
    # ── Static (step=0) — fetch handling differs, keep PLANNED ──
    ("z",     "z_sfc", "ジオポテンシャル (地表)", "Geopotential (sfc)","m²/s²", Status.IMPLEMENTED, "static field (step=0 のみ実値); 山岳=高,海=0"),
    ("lsm",   "lsm",   "海陸マスク",      "Land-sea mask",         "0..1",  Status.IMPLEMENTED, "static field (0=海, 1=陸)"),
    ("sdor",  "sdor",  "地形標準偏差",    "Sub-grid orog. stddev", "m",     Status.IMPLEMENTED, "static field (粗格子内の地形ばらつき)"),
    ("slor",  "slor",  "地形傾斜",        "Sub-grid orog. slope",  "—",     Status.IMPLEMENTED, "static field (粗格子内の代表傾斜)"),
    # ── Currently unavailable ──
    ("t20d",  "t20d",  "20°C 等温面深さ", "Depth of 20°C isotherm","m",     Status.PLANNED,     "currently unavailable (2024-03-11 per ECMWF docs)"),
    ("sav300","sav300","300m 平均塩分",   "Avg salinity (top 300m)","psu",  Status.PLANNED,     "currently unavailable"),
)

_SURFACE_FIELDS = tuple(
    DataField(
        key=key,
        label_ja=ja,
        label_en=en,
        unit=unit,
        level=None,
        typical_layer="カラーシェーディング",
        status=status,
        ecmwf_param=ecmwf,
        notes=notes,
    )
    for ecmwf, key, ja, en, unit, status, notes in _SURFACE_NEW
)


# Derived: 100m wind speed from (100u, 100v). Stored alongside the
# wind10m surface entry so the matrix's wind row covers both 10m and
# 100m heights.
_WIND100M_FIELD = DataField(
    key="wind100m",
    label_ja="100m風速",
    label_en="100m wind speed",
    unit="m/s",
    level=None,
    typical_layer="風速シェーディング + 矢印",
    status=Status.IMPLEMENTED,
    ecmwf_param="100u/100v",
    notes="100m 風成分から √(u²+v²) を計算。",
)


# ── Soil-layer catalogue (generated) ───────────────────────────────
# ECMWF Open Data's soil product set publishes soil temperature (sot)
# and volumetric soil water (vsw) on 4 standard layers:
#   1: 0-7 cm, 2: 7-28 cm, 3: 28-100 cm, 4: 100-289 cm
# Same multi-band-GRIB-per-kind pattern as pressure levels.

SOIL_LAYERS: tuple[int, ...] = (1, 2, 3, 4)
SOIL_LAYER_DEPTH: dict[int, str] = {
    1: "0-7 cm", 2: "7-28 cm", 3: "28-100 cm", 4: "100-289 cm",
}

# (ecmwf_param, ja, en, unit)
SOIL_VARIABLES: tuple[tuple[str, str, str, str], ...] = (
    ("sot", "土壌温度", "Soil temperature",      "°C"),
    ("vsw", "土壌水分", "Volumetric soil water", "m³/m³"),
)

_SOIL_FIELDS = tuple(
    DataField(
        key=f"{var}_{layer}",
        label_ja=f"{ja} (層{layer}: {SOIL_LAYER_DEPTH[layer]})",
        label_en=f"{en} (layer {layer}: {SOIL_LAYER_DEPTH[layer]})",
        unit=unit,
        # The ECMWF Open Data sol product uses levelist=[1,2,3,4] to
        # select layers; we mirror that on ``level`` so the existing
        # multi-band extractor logic (".sel by depthBelowLandLayer")
        # threads through without a separate axis name.
        level=layer,
        typical_layer="カラーシェーディング",
        status=Status.IMPLEMENTED,
        ecmwf_param=var,
    )
    for var, ja, en, unit in SOIL_VARIABLES
    for layer in SOIL_LAYERS
)


FIELDS = FIELDS + _SURFACE_FIELDS + (_WIND100M_FIELD,) + _SOIL_FIELDS


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
