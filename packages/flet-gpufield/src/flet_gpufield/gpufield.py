# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Yasuhiro / AIseed

"""気象の格子データを GPU で描く Flet の部品。

Python は格子データと表示の決まり（投影・配色・範囲）を渡すだけで、拡大・移動・風の粒子の
動きは Flutter 側（GPU のシェーダーと Dart）が 1 コマごとに描く。

時刻ごとのデータは子の `GpuFrame` で渡し、`step`（番号）を変えるだけで切り替える。
Flet は足された子だけを送るので、全時刻を順に足していけば、切り替えのたびに
データを送り直すことはない。
"""
from dataclasses import dataclass, field
from typing import Optional

import flet as ft

__all__ = ["GpuField", "GpuFrame", "GpuFieldStatsEvent"]


@dataclass
class GpuFieldStatsEvent(ft.Event["GpuField"]):
    """描画の速さの報告（約 2 秒ごと）。"""

    fps: float
    """1 秒あたりのコマ数。"""

    frame_ms: float
    """粒子の移動の計算にかかった時間の平均（ミリ秒）。"""

    particles: int
    """動かしている粒子の数。"""

    upload_ms: float
    """最後に時刻のデータを GPU 用の画像にするのにかかった時間（ミリ秒）。"""

    build_ms: float = 0.0
    """Flutter が測った 1 コマの組み立て（build）の時間の平均（ミリ秒）。"""

    raster_ms: float = 0.0
    """Flutter が測った 1 コマの描画（raster）の時間の平均（ミリ秒）。"""

    frames: int = 0
    """受け取っている時刻の数。"""


@ft.control("GpuFrame")
class GpuFrame(ft.BaseControl):
    """1 時刻分のデータ。

    - `scalar`: 色で描く量。格子点ごとに RGBA 4 バイト。RG が `vmin`〜`vmax` を 0〜65535 に
      詰めた値（R が上位）、A が 255 なら値あり・0 なら欠測。
    - `wind`: 風の粒子用の u・v。int16 little-endian（0.01 m/s 単位）で u を ny × nx、続けて v。
    - `contours` / `contours_bold`: 等値線。float32 LE の (経度, 緯度) の並び、線の切れ目は NaN。
    - `labels`: 等値線の数値。float32 LE の (経度, 緯度, 値) の並び。
    """

    index: int = 0
    scalar: Optional[bytes] = None
    wind: Optional[bytes] = None
    contours: Optional[bytes] = None
    contours_bold: Optional[bytes] = None
    labels: Optional[bytes] = None


@ft.control("GpuField")
class GpuField(ft.LayoutControl):
    """等緯度経度の格子（北から南の行順）を GPU で描く。"""

    nx: int = 0
    ny: int = 0
    lon0: ft.Number = 0.0
    """最初の列（西端）の格子点の経度。"""
    lat0: ft.Number = 0.0
    """最初の行（北端）の格子点の緯度。"""
    dlon: ft.Number = 0.25
    dlat: ft.Number = 0.25
    periodic: bool = False
    """経度方向に一周つながる格子か（全球）。"""

    projection: str = "plate_carree"
    """"plate_carree"（正距円筒）、"polar_north"・"polar_south"（極の正距方位）。"""

    center_lon: ft.Number = 137.0
    """最初の表示の中心の経度。極の投影では画面の真下に来る経度。"""
    center_lat: ft.Number = 36.0
    px_per_deg: ft.Number = 14.0
    """最初の表示の縮尺（1 度あたりの論理画素。極では余緯度 1 度あたり）。"""

    vmin: ft.Number = 0.0
    vmax: ft.Number = 1.0
    """配色の両端に対応する値（読み取りの値の換算にも使う）。"""

    lut: Optional[bytes] = None
    """配色。256 段 × RGBA8（vmin から vmax まで）。A が透明度。"""

    base: Optional[bytes] = None
    """陸海。格子点ごとに 1 バイト（1 が陸）。配色が透ける所に地色として出る。"""

    coast: Optional[bytes] = None
    """海岸線。float32 LE の (経度, 緯度) の並び、線の切れ目は NaN。"""

    label: str = ""
    unit: str = ""
    """読み取りに出す量の名前と単位。"""

    sampling: str = "bilinear"
    """"bilinear"（双一次補間）か "nearest"（最寄りの格子点）。階級のような値は "nearest" に
    する（階級の間を補間すると、観測されていない値の縁ができる）。"""

    classes: list[str] = field(default_factory=list)
    """値が階級の番号（0, 1, 2, …）のときの、番号ごとの名前。読み取りに数値の代わりに出す
    （空の名前は出さない。label・unit が空なら名前だけを出す）。"""

    frames: list[GpuFrame] = field(default_factory=list)
    """時刻ごとのデータ。"""

    step: int = 0
    """描く時刻（GpuFrame.index）。まだ届いていなければ、届くまで前の時刻を描く。"""

    particles: int = 10000
    """風の粒子の数（Intel 内蔵 GPU で 12,000 まで 60 fps）。"""

    show_particles: bool = True

    attribution: str = ""
    """右下に出す出典（図には必ず出典を付ける）。"""

    status_text: str = ""
    """左上に出す状態の知らせ（読み込み中、この時刻のデータが無い、など）。空なら出さない。"""

    on_ready: Optional[ft.ControlEventHandler["GpuField"]] = None
    """部品が画面に出たとき（実行環境に部品が入っている印）。"""

    on_stats: Optional[ft.EventHandler[GpuFieldStatsEvent]] = None
    """描画の速さの報告を受け取る。"""
