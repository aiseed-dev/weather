"""時別の平年値（build_hourly_normals.py の出力）を読み、ある時刻の平年気温を出す。

トップの地図（今の気温の平年差）で使う。時別の平年値を作ったのは主要 10 都市だけなので、
ほかの官署は「近い都市の日変化の形」を借りて推定する:

    平年(s, t) = 日平均気温の平年(s, d) + 偏差(c, t) × 日較差の平年(s) / 日較差の平年(c)

c は時別の平年値のある都市のうち、その日の値があって s に最も近いもの。偏差(c, t) は
c の時別の平年(t) − c の 24 値の平均（＝c の日平均気温の平年）。日較差の比は 0.5〜1.5 に抑える。
"""
from __future__ import annotations

import json
import math
from datetime import date, datetime, timedelta
from pathlib import Path

LEAP = 2000                       # 暦日の基準（2/29 を含む年）。build_hourly_normals.py と同じ
BORROW_SCALE = (0.5, 1.5)


def _row(obj: dict, month: int, day: int) -> list[int] | None:
    try:
        return obj["daily"][str(month)]["temp"][day - 1]
    except (KeyError, IndexError):
        return None


def normal_at(obj: dict, month: int, day: int, hour: int, minute: int = 0) -> float | None:
    """時別平年（出力 JSON を読んだ dict）から、JST の時刻 hour:minute の平年気温 [℃] を返す。

    毎正時の値の間は線形補間。0:00〜1:00 は「前日の 24 時」(=その日の 0:00) を使う。
    """
    cur = _row(obj, month, day)
    if cur is None:
        return None
    prev_d = date(LEAP, month, day) - timedelta(days=1)
    prev = _row(obj, prev_d.month, prev_d.day)
    t = hour + minute / 60.0                 # 0 <= t < 24
    pts = {0: (prev or cur)[23]}             # 0:00 = 前日 24 時（前日が無ければ当日の 24 時で代用）
    for i in range(24):
        pts[i + 1] = cur[i]
    h0 = int(math.floor(t))
    h1 = min(h0 + 1, 24)
    f = t - h0
    return (pts[h0] * (1 - f) + pts[h1] * f) / 10.0


class HourlyNormals:
    """master/normals_hourly/{code}.json と日別平年値から、各官署の時別の平年を出す。

    daily(code, elem, d) は日別平年値（×10 整数、elem は tavg / tmax / tmin）を返す関数。
    coords は {code: (lat, lon)}（距離は経度を cos 36° 倍した経緯度で測る）。
    """

    def __init__(self, directory: Path, daily, coords: dict[int, tuple[float, float]]):
        self.daily = daily
        self.coords = coords
        self.own: dict[int, dict] = {}
        if directory.is_dir():
            for p in sorted(directory.glob("*.json")):
                try:
                    self.own[int(p.stem)] = json.loads(p.read_text(encoding="utf-8"))
                except (ValueError, OSError):
                    continue

    def __bool__(self) -> bool:
        return bool(self.own)

    def _dist(self, a: int, b: int) -> float:
        (la1, lo1), (la2, lo2) = self.coords[a], self.coords[b]
        return math.hypot((lo1 - lo2) * math.cos(math.radians(36.0)), la1 - la2)

    def at(self, code: int, when: datetime) -> tuple[int | None, int | None]:
        """(平年気温 ×10 の整数, 日変化の形を借りた都市)。自前の値なら借り先は code 自身。"""
        return self._at(code, when.month, when.day, when.hour, when.minute)

    def curve(self, code: int, d: date) -> tuple[list[int] | None, int | None]:
        """その日の 0:00〜24:00 の毎正時の平年（25 値、×10）と借り先。ページ側で時刻に合わせて補間する。"""
        vals, src = [], None
        for h in range(25):
            v, src = self._at(code, d.month, d.day, h, 0)
            if v is None:
                return None, None
            vals.append(v)
        return vals, src

    def _at(self, code: int, m: int, d: int, h: int, mi: int) -> tuple[int | None, int | None]:
        if (m, d) == (2, 29):
            d = 28                           # 日別平年値に 2/29 が無いことがある
        if code in self.own:
            v = normal_at(self.own[code], m, d, h, mi)
            if v is not None:
                return round(v * 10), code
        if code not in self.coords:
            return None, None
        dd = date(LEAP, m, d)
        tavg, tmax, tmin = (self.daily(code, e, dd) for e in ("tavg", "tmax", "tmin"))
        if None in (tavg, tmax, tmin):
            return None, None
        cands = sorted((c for c in self.own if c != code and c in self.coords
                        and _row(self.own[c], m, d) is not None),
                       key=lambda c: self._dist(code, c))
        for c in cands:
            ctmax, ctmin = self.daily(c, "tmax", dd), self.daily(c, "tmin", dd)
            v = normal_at(self.own[c], m, d, h, mi)
            if v is None or ctmax is None or ctmin is None or ctmax <= ctmin:
                continue
            row = _row(self.own[c], m, d)
            dev = v * 10 - sum(row) / 24
            scale = min(max((tmax - tmin) / (ctmax - ctmin), BORROW_SCALE[0]), BORROW_SCALE[1])
            return round(tavg + dev * scale), c
        return None, None
