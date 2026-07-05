"""推計気象分布（気象庁 suikei）から「現在の天気」を取得する。

https://www.jma.go.jp/bosai/suikei/ の配信データ（1km メッシュの推計値・毎時更新）を使う。
天気はタイル PNG（512px・z=10）で配信されるため、
都市の緯度経度 → Web メルカトルのタイル座標・ピクセル位置 → ピクセル色 → 天気カテゴリ
という手順でサンプリングする。

  targetTimes: jmatile/data/suikeikishou/targetTimes.json（basetime/validtime は UTC）
  タイル URL : jmatile/data/suikeikishou/{basetime}/none/{validtime}/surf/wthr/{z}/{x}/{y}.png
               タイルは 512px（x, y は 256px スキームの 1/2）。z は偶数のみ、実在最大 z=10。

色 → 天気の対応は凡例 SVG（suikei/images/legend_jp_normal_wm.svg）から取得した確定値
（東京=くもり・大阪=雨・那覇=晴れ の実況と一致することを 2026-07-05 に検証済み）。
"""
from __future__ import annotations

import io
import json
import math
from datetime import datetime, timedelta, timezone

from weatherlib import jma

URL_TARGET_TIMES = "https://www.jma.go.jp/bosai/jmatile/data/suikeikishou/targetTimes.json"
URL_TILE = ("https://www.jma.go.jp/bosai/jmatile/data/suikeikishou/"
            "{bt}/none/{vt}/surf/{elem}/{z}/{x}/{y}.png")
# z=8（1px ≈ 0.6km）はデータの 1km メッシュより細かいため、推計分布の精度をそのまま
# 引き出せる（z=10 と 57 都市で判定完全一致を検証済み・2026-07-05）。
# タイル数は 57 都市で 22 枚（z=10 の 50 枚から半減以下）と負荷・精度の最適点。
ZOOM = 8
TILE_SIZE = 512

# 凡例 SVG の色（RGB） → 天気カテゴリ
WTHR_COLORS = {
    (255, 170, 0): "晴れ",
    (170, 170, 170): "くもり",
    (0, 65, 255): "雨",
    (160, 210, 255): "雨または雪",
    (242, 242, 255): "雪",
}
# 天気カテゴリ → 予報アイコン用の気象庁天気コード（weather_img フィルタで画像名に変換）
WTHR_CODES = {"晴れ": "100", "くもり": "200", "雨": "300", "雨または雪": "340", "雪": "400"}


def latest_wthr_time() -> tuple[str, str, datetime]:
    """最新の wthr の (basetime, validtime, JST 時刻) を返す。"""
    tt = json.loads(jma.http_get(URL_TARGET_TIMES))
    for e in tt:  # 新しい順
        if "wthr" in e.get("elements", []):
            bt, vt = e["basetime"], e["validtime"]
            utc = datetime.strptime(vt, "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
            jst = (utc + timedelta(hours=9)).replace(tzinfo=None)
            return bt, vt, jst
    raise RuntimeError("targetTimes に wthr がありません")


def tile_of(lat: float, lon: float) -> tuple[int, int, int, int]:
    """緯度経度 → (タイル x, タイル y, ピクセル x, ピクセル y)。512px タイル。"""
    world = 2 ** ZOOM * 256                      # 256px スキームの世界ピクセル幅
    xf = (lon + 180) / 360 * world
    lr = math.radians(lat)
    yf = (1 - math.log(math.tan(lr) + 1 / math.cos(lr)) / math.pi) / 2 * world
    return int(xf // TILE_SIZE), int(yf // TILE_SIZE), int(xf % TILE_SIZE), int(yf % TILE_SIZE)


class WthrSampler:
    """タイルをキャッシュしながら複数地点の天気をサンプリングする。"""

    def __init__(self, basetime: str, validtime: str):
        self.bt, self.vt = basetime, validtime
        self._tiles: dict[tuple[int, int], "object"] = {}

    def _tile(self, x: int, y: int):
        key = (x, y)
        if key not in self._tiles:
            from PIL import Image
            url = URL_TILE.format(bt=self.bt, vt=self.vt, elem="wthr", z=ZOOM, x=x, y=y)
            try:
                data = jma.http_get(url)
                self._tiles[key] = Image.open(io.BytesIO(data)).convert("RGBA")
            except Exception:
                self._tiles[key] = None
        return self._tiles[key]

    def weather_at(self, lat: float, lon: float) -> str | None:
        """地点の天気カテゴリ。データなし（透明・海上等）は None。

        直上のピクセルが透明の場合は周囲 ±2px を走査する（海沿いの観測所対策）。
        """
        x, y, px, py = tile_of(lat, lon)
        im = self._tile(x, y)
        if im is None:
            return None
        for d in range(0, 3):
            for dx in range(-d, d + 1):
                for dy in range(-d, d + 1):
                    qx, qy = px + dx, py + dy
                    if not (0 <= qx < TILE_SIZE and 0 <= qy < TILE_SIZE):
                        continue
                    r, g, b, a = im.getpixel((qx, qy))
                    if a == 0:
                        continue
                    w = WTHR_COLORS.get((r, g, b))
                    if w:
                        return w
        return None

    @property
    def tiles_fetched(self) -> int:
        return len(self._tiles)


# ---------------------------------------------------------------- 現在気温（アメダス最新 10 分値）

URL_LATEST_TIME = "https://www.jma.go.jp/bosai/amedas/data/latest_time.txt"


def latest_amedas_map() -> tuple[datetime, dict[str, int | None]]:
    """最新 10 分値の (時刻 JST, {アメダス番号: 気温 ×10}) を返す。"""
    ts_s = jma.http_get(URL_LATEST_TIME).decode().strip()
    ts = datetime.fromisoformat(ts_s).replace(tzinfo=None)
    data = jma.fetch_amedas_map(ts)
    return ts, {a: v["temp"] for a, v in data.items()}
