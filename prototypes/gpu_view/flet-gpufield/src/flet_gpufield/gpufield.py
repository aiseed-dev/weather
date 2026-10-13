# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Yasuhiro / AIseed

"""気象の格子データを GPU で描く Flet の部品（試作）。

Python は格子データと表示の決まり（配色・範囲・層）を渡すだけで、拡大・移動・風の粒子の
動きは Flutter 側（GPU のシェーダーと Dart）が 1 コマごとに描く。時刻や層を変えたときにだけ
データを渡し直す。
"""
from dataclasses import dataclass
from typing import Optional

import flet as ft

__all__ = ["GpuField", "GpuFieldStatsEvent"]


@dataclass
class GpuFieldStatsEvent(ft.Event["GpuField"]):
    """描画の速さの報告（約 2 秒ごと）。"""

    fps: float
    """1 秒あたりのコマ数。"""

    frame_ms: float
    """1 コマの計算にかかった時間の平均（粒子の移動と描き込み、ミリ秒）。"""

    particles: int
    """動かしている粒子の数。"""

    upload_ms: float
    """最後に格子データを GPU 用の画像に詰めるのにかかった時間（ミリ秒）。"""

    build_ms: float = 0.0
    """Flutter が測った 1 コマの組み立て（build）の時間の平均（ミリ秒）。"""

    raster_ms: float = 0.0
    """Flutter が測った 1 コマの描画（raster、GPU への命令）の時間の平均（ミリ秒）。"""


@ft.control("GpuField")
class GpuField(ft.LayoutControl):
    """気象の格子データ（正距円筒の等間隔格子）を GPU で描く。

    格子は北から南の行順。`field` は float32 little-endian で、チャンネルごとに
    ny × nx を並べたもの（試作では [t2m(℃), u10, v10] の 3 チャンネル）。
    """

    field: Optional[bytes] = None
    """格子データ。float32 LE、[t2m, u10, v10] × ny × nx。"""

    nx: int = 0
    ny: int = 0
    lon0: ft.Number = 0.0
    """最初の列（西端）の格子点の経度。"""
    lat0: ft.Number = 0.0
    """最初の行（北端）の格子点の緯度。"""
    dlon: ft.Number = 0.25
    dlat: ft.Number = 0.25

    layer: str = "t2m"
    """色で描く量。"t2m"（2m 気温）か "wind"（10m 風速）。"""

    vmin: ft.Number = -30.0
    vmax: ft.Number = 40.0
    """配色の両端に対応する値。"""

    lut: Optional[bytes] = None
    """配色。256 段 × RGBA8（vmin から vmax まで）。"""

    coast: Optional[bytes] = None
    """海岸線。float32 LE の (経度, 緯度) の並び、線の切れ目は NaN。"""

    particles: int = 10000
    """風の粒子の数（Intel 内蔵 GPU で 12,000 まで 60 fps）。"""

    show_particles: bool = True

    trail: str = "vertices"
    """尾の描き方。"vertices"（既定・速い）、"history"・"hairline"（線で描く）、
    "image"（前のコマを画像に焼いて重ねる）。速さの比べは Dart 側の説明。"""

    attribution: str = ""
    """右下に出す出典（図には必ず出典を付ける）。"""

    center_lon: ft.Number = 137.0
    center_lat: ft.Number = 36.0
    px_per_deg: ft.Number = 14.0
    """最初の表示の中心と縮尺（1 度あたりの画素数）。"""

    on_stats: Optional[ft.EventHandler[GpuFieldStatsEvent]] = None
    """描画の速さの報告を受け取る。"""
